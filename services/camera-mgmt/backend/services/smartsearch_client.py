"""smartsearch_client.py — thin client for the CLIP index service.

The index is built by the GPU appliance (person crops off Kafka, vehicles off
the ANPR ingest) and lives with the GPU that builds it, so this is an HTTP hop
rather than a shared directory like the DeepStream projection.

The service has no auth of its own, and on this deployment it is not even
co-located — it answers on a public name, so anything that can route to it can
query the index. That is the reason browsers reach it through ``/api/search/*``
rather than directly, and the reason ``SMARTSEARCH_API_KEY`` exists for when
the service starts checking one.

Two upstream quirks this module works around, both verified against the live
service:

* ``camera_name`` is resolved through the service's own camera table, which
  does not track this registry. A real slug comes back
  ``"unknown camera 'cam34-n17v' — ignored"`` and the query silently runs
  unfiltered. ``sensor_id`` is the parameter that actually filters, so that is
  the one used here.
* ``sensor_ids`` (plural) used to be accepted and ignored. The index now honours
  it, so a query is narrowed to this VMS's cameras upstream instead of fetching
  another site's page and discarding all of it. The registry filter still runs
  on our side — it enforces recorder and retention rules the index knows nothing
  about — so the over-fetch stays.

  An index that predates that change drops the field silently, which degrades to
  the old behaviour rather than failing. ``stats`` is the one place that
  distinguishes the two: it asks for a scoped count and treats a response
  without ``scoped: true`` as unusable, because an unscoped total is the whole
  shared collection and would be a number from other deployments.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)

# Generous but bounded: a CLIP text encode plus a vector search, over a link
# that may be a tunnel to another site.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class SearchUnavailable(RuntimeError):
    """The index service could not be reached or refused the request."""

    def __init__(
        self, message: str, status_code: int = 502, configured: bool = True
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        #: False means no index is deployed at all. That is a supported
        #: deployment, not a fault, and the UI must say so differently — an
        #: operator told "unreachable" goes looking for a broken service.
        self.configured = configured


class SearchNotConfigured(SearchUnavailable):
    """No CLIP index is deployed on this installation."""

    def __init__(self) -> None:
        super().__init__(
            "Smart Search is not configured on this installation. "
            "Recording, playback and analytics are unaffected.",
            status_code=503,
            configured=False,
        )


def is_configured() -> bool:
    """Whether an index endpoint is set at all.

    An empty ``SMARTSEARCH_API_URL`` would otherwise build a relative URL,
    which httpx rejects as a transport error — reported as an outage of a
    service that was never deployed.
    """
    return bool(settings.smartsearch_api_url.strip())


def _base() -> str:
    return settings.smartsearch_api_url.rstrip("/")


def _headers() -> dict[str, str]:
    key = settings.smartsearch_api_key
    return {"X-API-Key": key} if key else {}


async def _call(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    params: Optional[list[tuple[str, str]]] = None,
) -> Any:
    if not is_configured():
        raise SearchNotConfigured()
    url = f"{_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(
                method, url, json=payload, params=params, headers=_headers()
            )
    except httpx.RequestError as exc:
        log.warning("smartsearch.unreachable", path=path, error=str(exc))
        raise SearchUnavailable(
            "The search index service is unreachable. Recording and playback are unaffected."
        ) from exc

    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                detail = body["detail"]
        except ValueError:
            pass
        log.warning("smartsearch.error", path=path, status=resp.status_code, detail=detail)
        # 4xx from the index is a bad query, not an outage — pass it through so
        # the operator sees the real reason instead of "unavailable".
        raise SearchUnavailable(
            detail or f"The search index rejected the request (HTTP {resp.status_code}).",
            status_code=resp.status_code if resp.status_code < 500 else 502,
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise SearchUnavailable("The search index returned a malformed response.") from exc


def _clean(payload: dict) -> dict:
    """Drop unset filters — the API treats ``null`` as unset but ``""`` as a
    literal value to match."""
    return {k: v for k, v in payload.items() if v not in (None, "")}


async def search_people(
    *,
    query: str,
    top_k: int,
    score_threshold: float,
    sensor_id: Optional[str] = None,
    sensor_ids: Optional[list[str]] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    sort_by: str = "time",
    sort_dir: str = "desc",
) -> list[dict]:
    payload = _clean({
        "query": query,
        "top_k": top_k,
        "score_threshold": score_threshold,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "sensor_id": sensor_id,
        "time_from": time_from,
        "time_to": time_to,
    })
    # Sent separately from _clean: an empty list is meaningful (this VMS has no
    # searchable cameras, so nothing may come back) and _clean keeps it, whereas
    # None means "do not scope" and must not be sent at all.
    if sensor_ids is not None:
        payload["sensor_ids"] = sensor_ids
    body = await _call("POST", "/person/search", payload)
    return list(body.get("results") or [])


async def search_people_by_image(
    *,
    image: bytes,
    top_k: int,
    score_threshold: float,
    sensor_ids: Optional[list[str]] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
) -> list[dict]:
    """Search-by-example against the person index (a playback annotation crop).

    Not routed through ``_call``: the body is raw image bytes, not JSON — the
    index deliberately takes them that way so it needs no multipart parser
    (see its /person/search_by_image). Filters ride the query string; the
    repeated ``sensor_ids`` params are the same registry scoping the text
    search sends, for the same reason — an empty list means "nothing may come
    back", never "no filter"."""
    if not is_configured():
        raise SearchNotConfigured()
    params: list[tuple[str, str]] = [
        ("top_k", str(top_k)), ("score_threshold", str(score_threshold)),
    ]
    if sensor_ids is not None:
        params += [("sensor_ids", s) for s in sensor_ids]
    if time_from:
        params.append(("time_from", time_from))
    if time_to:
        params.append(("time_to", time_to))
    url = f"{_base()}/person/search_by_image"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url, content=image, params=params,
                headers={**_headers(), "Content-Type": "image/jpeg"},
            )
    except httpx.RequestError as exc:
        log.warning("smartsearch.unreachable", path="/person/search_by_image", error=str(exc))
        raise SearchUnavailable(
            "The search index service is unreachable. Recording and playback are unaffected."
        ) from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                detail = body["detail"]
        except ValueError:
            pass
        log.warning("smartsearch.error", path="/person/search_by_image",
                    status=resp.status_code, detail=detail)
        raise SearchUnavailable(
            detail or f"The search index rejected the request (HTTP {resp.status_code}).",
            status_code=resp.status_code if resp.status_code < 500 else 502,
        )
    try:
        return list(resp.json().get("results") or [])
    except ValueError as exc:
        raise SearchUnavailable("The search index returned a malformed response.") from exc


async def search_faces_by_image(
    *,
    images: list[bytes],
    top_k: int,
    score_threshold: float,
    camera_ids: Optional[list[str]] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    min_width_px: int = 0,
) -> dict:
    """Search the face index by photograph.

    RETURNS THE WHOLE BODY, not just `results`, which is the one place this
    differs from the person search-by-example beside it. The index answers with
    `faces_detected` as well, and "your photo has no findable face in it" is a
    different problem from "nobody matches" — collapsing them to an empty list
    would tell the operator to go looking for a person who may be right there.
    """
    if not is_configured():
        raise SearchNotConfigured()
    params: list[tuple[str, str]] = [
        ("top_k", str(top_k)), ("score_threshold", str(score_threshold)),
    ]
    if camera_ids is not None:
        params += [("camera_ids", c) for c in camera_ids]
    if time_from:
        params.append(("time_from", time_from))
    if time_to:
        params.append(("time_to", time_to))
    if min_width_px:
        params.append(("min_width_px", str(min_width_px)))
    if len(images) == 1:
        # The original contract, byte for byte, so an index that predates
        # multi-photo search answers a single-photo search exactly as before.
        send: dict[str, Any] = {
            "content": images[0],
            "headers": {**_headers(), "Content-Type": "image/jpeg"},
        }
    else:
        # Several photos of one person, pooled by the index. No Content-Type
        # set here: httpx writes the multipart boundary into it.
        send = {
            "files": [("photos", (f"photo-{n}", img, "application/octet-stream"))
                      for n, img in enumerate(images, start=1)],
            "headers": _headers(),
        }
    url = f"{_base()}/faces/search_by_image"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, params=params, **send)
    except httpx.RequestError as exc:
        log.warning("smartsearch.unreachable", path="/faces/search_by_image",
                    error=str(exc))
        raise SearchUnavailable(
            "The search index service is unreachable. Recording and playback are unaffected."
        ) from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                detail = body["detail"]
        except ValueError:
            pass
        log.warning("smartsearch.error", path="/faces/search_by_image",
                    status=resp.status_code, detail=detail)
        raise SearchUnavailable(
            detail or f"The search index rejected the request (HTTP {resp.status_code}).",
            status_code=resp.status_code if resp.status_code < 500 else 502,
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise SearchUnavailable("The search index returned a malformed response.") from exc
    return body if isinstance(body, dict) else {"results": []}


async def recent_faces(*, camera_ids: Optional[list[str]] = None,
                       limit: int = 60, min_width_px: int = 0) -> dict:
    """The Faces tab's opening gallery. Same scoping rules as every other read."""
    if not is_configured():
        raise SearchNotConfigured()
    params: list[tuple[str, str]] = [("limit", str(limit))]
    if camera_ids is not None:
        params += [("camera_ids", c) for c in camera_ids]
    if min_width_px:
        params.append(("min_width_px", str(min_width_px)))
    body = await _call("GET", "/faces/recent", params=params)
    return body if isinstance(body, dict) else {"results": []}


async def search_vehicles(
    *,
    query: str,
    top_k: int,
    score_threshold: float,
    plate: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    color: Optional[str] = None,
    camera_ids: Optional[list[str]] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    sort_by: str = "time",
    sort_dir: str = "desc",
) -> list[dict]:
    body = await _call("POST", "/vehicles/search", _clean({
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "query": query,
        "top_k": top_k,
        "score_threshold": score_threshold,
        "plate": plate,
        "vehicle_type": vehicle_type,
        "color": color,
        "camera_ids": camera_ids,
        "time_from": time_from,
        "time_to": time_to,
    }))
    return list(body.get("results") or [])


async def search_plates(
    *,
    plate: str,
    camera_ids: list[str],
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    limit: int = 200,
) -> dict:
    """Plate sightings, grouped by plate.

    Deliberately a different call from ``search_vehicles``: a plate is an exact
    identifier, so there is no query string, no score threshold and no
    similarity ranking to tie-break with. It also needs no text encoder
    upstream, which is why plate lookup survives an index whose model failed to
    load.
    """
    params: list[tuple[str, str]] = [("plate", plate)]
    params += [("camera_ids", c) for c in camera_ids]
    if time_from:
        params.append(("time_from", time_from))
    if time_to:
        params.append(("time_to", time_to))
    params.append(("limit", str(limit)))
    body = await _call("GET", "/plates/search", params=params)
    return body if isinstance(body, dict) else {"plates": []}


async def stats(slugs: list[str]) -> dict[str, dict]:
    """How much of each domain belongs to *these* cameras. Doubles as the
    reachability probe.

    The collection is shared, so the unscoped total is other deployments' data
    and must never be shown here. Each domain names the camera column
    differently — persons store ``sensor_id``, vehicles carry ``camera_id`` as a
    dynamic field — hence the per-domain parameter name.

    ``scoped`` is false when the index ignored the filter (an older build), and
    the count is then reported as unknown rather than as this VMS's."""
    out: dict[str, dict] = {}
    for domain, path, param in (
        ("people", "/person/stats", "sensor_ids"),
        ("vehicles", "/vehicles/stats", "camera_ids"),
    ):
        # Still called with no slugs, because this is also the reachability
        # probe — but an empty filter reads as "unscoped" upstream, so the
        # answer is settled here: no cameras, nothing in the index is ours.
        body = await _call("GET", path, params=[(param, s) for s in slugs])
        scoped = bool(body.get("scoped")) or not slugs
        count = 0 if not slugs else int(body.get("vectors_count") or 0)
        out[domain] = {
            "vectors_count": count if scoped else None,
            "scoped": scoped,
            "status": str(body.get("status") or "unknown"),
        }
    return out


# ── Scoped erasure (data-subject requests) ───────────────────────────────────
# Every other call in this module speaks to the index this VMS QUERIES
# (smartsearch_api_url). Erasure is the one operation that must also reach the
# index this VMS FEEDS (smartsearch_index_url), and the two are separate
# settings on purpose: a VMS can query a remote index without feeding one, feed
# a local one it also queries, or do both against different hosts.
#
# Erasing only one of them leaves the guarantee broken in a way nobody sees.
# Miss the fed index and the crops stay on this appliance's disk. Miss the
# queried index and the person is still findable in the product. So the targets
# are the DISTINCT configured bases of both, and on the usual deployment — where
# they are the same URL — that is one call.

def _erase_targets() -> list[str]:
    """Distinct configured index bases, deduplicated by normalised URL."""
    seen: list[str] = []
    for raw in (settings.smartsearch_index_url, settings.smartsearch_api_url):
        base = (raw or "").strip().rstrip("/")
        if base and base not in seen:
            seen.append(base)
    return seen


async def erase_range(slug: str, from_iso: str, to_iso: str) -> list[dict[str, Any]]:
    """Erase indexed crops for `slug` in [from_iso, to_iso] from every index.

    Returns one result per target, each with ``ok``. An empty list means no
    index is configured on this installation — nothing to erase, which is a
    success, not a silent skip; the caller records that distinction.

    Failure is never swallowed and never softened. Unlike the best-effort sync
    calls in smartsearch_sync, a caller here is deciding whether to tell a data
    subject that their images have been destroyed, so anything short of a 200
    with ``status: ok`` is reported as ``ok: False``.

    ``status: incomplete`` is one of those failures: it means the index deleted
    its rows but could not unlink some crop files, so JPEGs of the person are
    still on disk. Rows gone and images present is exactly the state that looks
    erased from every screen in the product and is not.

    An index that does not implement this endpoint (an older build, or the
    retiring clip-service) answers 404/405 and is reported as a failure. That is
    the correct answer: a searchable copy exists that this VMS cannot destroy,
    and the operator has to know rather than have it rounded up to success.
    """
    results: list[dict[str, Any]] = []
    for base in _erase_targets():
        entry: dict[str, Any] = {"index_url": base, "ok": False}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.delete(
                    f"{base}/erase",
                    params={"camera": slug, "from": from_iso, "to": to_iso},
                    headers=_headers(),
                )
        except httpx.RequestError as exc:
            log.warning("smartsearch.erase.unreachable", index=base, slug=slug,
                        error=str(exc))
            entry["error"] = f"Search index unreachable: {exc}"
            results.append(entry)
            continue

        if resp.status_code != 200:
            log.warning("smartsearch.erase.failed", index=base, slug=slug,
                        status=resp.status_code, detail=resp.text[:200])
            entry["error"] = (
                f"Search index returned {resp.status_code}"
                + (" — this index has no erasure endpoint"
                   if resp.status_code in (404, 405) else "")
            )
            results.append(entry)
            continue

        body = resp.json() if resp.content else {}
        entry.update({
            "persons_deleted": int(body.get("persons_deleted") or 0),
            "vehicles_deleted": int(body.get("vehicles_deleted") or 0),
            # Faces are a third table in the index. An erasure that deleted
            # them and did not SAY so under-reports what the erasure record
            # holds — and a missing key reads as zero, silently.
            "faces_deleted": int(body.get("faces_deleted") or 0),
            "crops_unlinked": int(body.get("crops_unlinked") or 0),
            "crops_failed": int(body.get("crops_failed") or 0),
        })
        if body.get("status") == "ok":
            entry["ok"] = True
        else:
            entry["error"] = (
                f"Index erased {entry['persons_deleted'] + entry['vehicles_deleted'] + entry['faces_deleted']} "
                f"row(s) but {entry['crops_failed']} crop file(s) remain on disk"
            )
        results.append(entry)
    return results
