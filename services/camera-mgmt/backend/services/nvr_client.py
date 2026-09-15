"""nvr_client.py — camera-registry → NVR recording sync.

The NVR service records whatever cameras it is told about at runtime
(POST/DELETE /cameras/{name}), but that state is **in-memory only** — an NVR
restart loses every runtime-added camera. So this module provides both:

  • event hooks   — called from the camera CRUD endpoints (create/update/delete)
  • reconcile_loop — a background task that periodically re-asserts the desired
    state (enabled registry cameras present, disabled/deleted ones absent)

Cameras are recorded FROM THE RELAY (rtsp://<SERVER_IP>:<RTSP_PORT>/<slug>), so
each physical camera keeps exactly one upstream connection. The NVR camera name
is the slug — the same ``nvr_camera_name`` join key downstream AI pipelines use.

All calls are best-effort: a down NVR must never fail a registry operation
(mirrors the DB-commits-before-relay-calls convention in routers/cameras.py);
the reconcile loop heals any missed sync on its next pass.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)

_client: Optional[httpx.AsyncClient] = None


def _base() -> str:
    return settings.nvr_api_url.rstrip("/")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ── Primitive operations (idempotent) ─────────────────────────────────────────

def _record_url(slug: str) -> str:
    # Loopback by default: NVR and MediaMTX share the host, and this keeps
    # recording working even if SERVER_IP is wrong or the box changes networks.
    return f"rtsp://{settings.nvr_record_host}:{settings.rtsp_port}/{slug}"


async def add_camera(
    slug: str,
    retention_days: int | None = None,
    masks: list | None = None,
    groom_after_days: int | None = None,
) -> bool:
    """Start recording ``slug`` from its relay URL. 409 (already exists) is OK.

    ``masks`` (normalized polygons) opts the camera into the NVR's masked
    recording path — the region is burned into every segment (re-encode).

    ``groom_after_days`` is sent only when the camera has its own override:
    None omits it and the NVR's appliance groom default applies, 0 means never
    groom this track, >0 is a threshold in days. The test is `is not None`, not
    truthiness — 0 is the sub track's whole point (grooming rewrites footage
    keyframe-only, which is what a scrub track exists not to be) and a truthy
    test dropped it, handing the sub the appliance default instead.
    """
    import json as _json

    params = {
        "rtsp_url": _record_url(slug),
        "retention_days": retention_days or settings.nvr_default_retention_days,
    }
    if groom_after_days is not None:
        params["groom_after_days"] = groom_after_days
    if masks:
        params["masks"] = _json.dumps(masks)
    try:
        resp = await _get_client().post(f"{_base()}/cameras/{slug}", params=params)
        if resp.status_code in (200, 409):
            log.info("nvr.sync.add.ok", slug=slug, existed=resp.status_code == 409)
            return True
        log.warning("nvr.sync.add.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.add.unreachable", slug=slug, error=str(exc))
    return False


async def set_retention(slug: str, retention_days: int | None) -> bool:
    """Push a camera's retention policy in place (None = appliance default).

    Unlike ``add_camera`` this works on an already-recording camera (the NVR's
    add endpoint 409s without touching retention) and on cameras with no live
    worker, whose footage keeps aging out under the policy.
    """
    days = retention_days or settings.nvr_default_retention_days
    try:
        resp = await _get_client().put(
            f"{_base()}/cameras/{slug}/retention", params={"retention_days": days}
        )
        if resp.status_code == 200:
            log.info("nvr.sync.retention.ok", slug=slug, days=days)
            return True
        log.warning("nvr.sync.retention.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.retention.unreachable", slug=slug, error=str(exc))
    return False


async def set_groom(slug: str, groom_after_days: int | None) -> bool:
    """Push a camera's groom-after override in place. Same live-or-not
    semantics as set_retention.

    None clears the override back to the NVR's appliance default, 0 means never
    groom this track, >0 is a threshold in days. The parameter is OMITTED to
    clear rather than sent as 0: collapsing both onto 0 (`groom_after_days or 0`)
    made "never" and "use the default" the same request, so a sub track could
    not opt out of grooming at all.
    """
    params: dict[str, int] = {}
    if groom_after_days is not None:
        params["groom_after_days"] = groom_after_days
    try:
        resp = await _get_client().put(
            f"{_base()}/cameras/{slug}/groom", params=params
        )
        if resp.status_code == 200:
            log.info("nvr.sync.groom.ok", slug=slug,
                     policy=("default" if groom_after_days is None
                             else "never" if groom_after_days == 0 else groom_after_days))
            return True
        log.warning("nvr.sync.groom.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.groom.unreachable", slug=slug, error=str(exc))
    return False


async def invalidate_codec_cache(slug: str) -> bool:
    """Tell the NVR that this recording name's codec may have changed.

    The NVR caches codec/resolution per recording name to avoid an ffprobe on
    every playlist. A sub track re-resolved onto a different profile keeps its
    name and changes its codec, so the cached answer becomes a lie — playback
    serves raw HEVC through the stream-copy path and the player goes black with
    nothing logged. Call this from anywhere that repoints a track's source.

    Best-effort like the other sync ops: the NVR's own TTL bounds the damage if
    this call is lost, which is exactly what the TTL is there for.
    """
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}/codec-cache")
        if resp.status_code in (200, 404):
            log.info("nvr.sync.codec_cache.invalidated", slug=slug)
            return True
        log.warning("nvr.sync.codec_cache.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.codec_cache.unreachable", slug=slug, error=str(exc))
    return False


async def remove_camera(slug: str) -> bool:
    """Stop recording ``slug``. 404 (already gone) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}")
        if resp.status_code in (200, 404):
            log.info("nvr.sync.remove.ok", slug=slug, missing=resp.status_code == 404)
            return True
        log.warning("nvr.sync.remove.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.remove.unreachable", slug=slug, error=str(exc))
    return False


async def purge_recordings(slug: str) -> bool:
    """Permanently delete all recorded footage for ``slug``. 404 (no footage) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}/recordings")
        if resp.status_code in (200, 404):
            log.info("nvr.sync.purge.ok", slug=slug, missing=resp.status_code == 404)
            return True
        log.warning("nvr.sync.purge.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.purge.unreachable", slug=slug, error=str(exc))
    return False


async def erase_range(slug: str, from_iso: str, to_iso: str,
                      missing_ok: bool = False) -> Optional[dict[str, Any]]:
    """Scoped erasure: delete segments fully inside [from_iso, to_iso].

    Returns the NVR's payload (deleted segment list, bytes freed, preserved
    boundary overlaps) on success, or None on any failure. Unlike the
    best-effort sync ops above, the caller MUST treat None as a hard failure:
    a caller that reports erasure as complete has made a claim about deleted
    footage, and it has to be true.

    ``missing_ok`` turns the NVR's 404 (this recording name has no indexed
    footage) into an empty success instead of a failure. It exists for the sub
    track: most cameras have never recorded one, and "there is nothing here to
    erase" is a complete answer to an erasure request, not a failed one. Only
    404 is softened — unreachable and refused stay hard failures, because those
    are the cases where footage may still exist and we cannot see it.
    """
    try:
        resp = await _get_client().delete(
            f"{_base()}/cameras/{slug}/recordings/range",
            params={"from": from_iso, "to": to_iso},
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404 and missing_ok:
            log.info("nvr.erase.no_such_track", slug=slug)
            return {"status": "ok", "camera": slug, "from": from_iso, "to": to_iso,
                    "segments_deleted": 0, "bytes_freed": 0, "deleted": [],
                    "skipped_partial_overlap": []}
        log.warning("nvr.erase.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.erase.unreachable", slug=slug, error=str(exc))
    return None


async def erase_range_all_tracks(
    slug: str, from_iso: str, to_iso: str
) -> Optional[dict[str, Any]]:
    """Scoped erasure across EVERY recording track of a camera.

    A camera with a sub track holds two copies of the same time window under two
    recording names. Erasing only `<slug>` left a full low-res copy of exactly
    the footage a data principal asked to have deleted sitting on disk, while
    the erasure register and the audit record both said fulfilled — the erasure
    was not partial, the claim about it was false.

    BOTH tracks are erased with ``missing_ok``, the main included: a main whose
    segments all aged out (shorter retention than the sub, or a prior erasure)
    404s while `<slug>_sub` still holds the window — aborting on that 404 left
    the low-res copy in place with the register saying fulfilled, the exact bug
    the whole-camera purge fan-out fixed. "This track has no footage there" is
    a complete answer for either track; unreachable and refused stay hard
    failures. The returned payload is the main's, with the sub's deletions
    folded into the totals and its segment list appended, so a caller embedding
    this in an audit record reports what was actually removed across both
    tracks. None if EITHER track failed — a half-done erasure must never read
    as done.
    """
    from ..models import sub_recording_name

    main = await erase_range(slug, from_iso, to_iso, missing_ok=True)
    if main is None:
        return None
    sub = await erase_range(sub_recording_name(slug), from_iso, to_iso,
                            missing_ok=True)
    if sub is None:
        log.error("nvr.erase.sub_track_failed", slug=slug,
                  detail="main erased; low-res copy may remain")
        return None
    return {
        **main,
        "segments_deleted": main.get("segments_deleted", 0) + sub.get("segments_deleted", 0),
        "bytes_freed": main.get("bytes_freed", 0) + sub.get("bytes_freed", 0),
        "deleted": [*main.get("deleted", []), *sub.get("deleted", [])],
        "skipped_partial_overlap": [*main.get("skipped_partial_overlap", []),
                                    *sub.get("skipped_partial_overlap", [])],
        # Named per track so the audit record shows the sub was considered at
        # all — "0 segments" and "never asked" have to stay distinguishable
        # years later, the same reason the search index reports [].
        "tracks_erased": {slug: main.get("segments_deleted", 0),
                          sub_recording_name(slug): sub.get("segments_deleted", 0)},
    }


class ClipUnavailable(Exception):
    """The NVR could not produce the requested clip.

    Carries the NVR's own reason. This exists because the previous contract —
    return None on ANY failure — erased the difference between "there is no
    footage in that window", "the NVR is down" and "you asked for more than
    this NVR will extract". The caller could only report the first two, so a
    3-hour export refused for exceeding a limit was shown to the operator as
    missing footage, and they went looking for recordings that were there.
    """


def _refusal_reason(resp: "httpx.Response") -> str:
    """A sentence from the NVR's error body, or a bare status if it has none."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        parts = [str(detail[k]) for k in ("error", "detail") if detail.get(k)]
        if parts:
            return " — ".join(parts)
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, list) and detail:                  # pydantic 422
        first = detail[0]
        if isinstance(first, dict) and first.get("msg"):
            loc = ".".join(str(p) for p in first.get("loc", [])[-1:])
            return f"{loc}: {first['msg']}" if loc else str(first["msg"])
    return f"HTTP {resp.status_code}"


@dataclass(frozen=True)
class Clip:
    """An extracted clip and what the recorder said about the window it came from.

    `coverage` is the fraction of the REQUESTED window the recorder actually
    held segments for. The NVR omits the header when coverage is full, so a
    missing header means 1.0 — but an UNPARSEABLE one means "it told us
    something we did not understand", which must never be read as full
    coverage: that is the direction that produces a false attestation.
    """
    data: bytes
    coverage: Optional[float]
    warning: Optional[str] = None


def _parse_coverage(raw: str) -> Optional[float]:
    """None when the header cannot be trusted, never a guess."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("nvr.clip.coverage_unparseable", raw=raw)
        return None
    if not 0.0 <= value <= 1.0:
        log.warning("nvr.clip.coverage_out_of_range", raw=raw)
        return None
    return value


async def fetch_clip(slug: str, from_iso: str, to_iso: str) -> "Clip":
    """Pull an MP4 clip for slug over [from_iso, to_iso] from the NVR's /clip
    endpoint (remux; stream-copy). Returns a Clip, or raises
    ClipUnavailable with a reason an operator can act on.

    The /clip endpoint centres on a timestamp with before/after, so we map the
    window onto that: centre = midpoint, before/after = half-span.
    """
    from datetime import datetime
    try:
        f = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
        t = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClipUnavailable(
            f"Clip window for {slug} is not a valid timestamp range "
            f"({from_iso}..{to_iso})."
        ) from exc
    span = (t - f).total_seconds()
    if span <= 0:
        raise ClipUnavailable(
            f"Clip window for {slug} ends at or before it starts "
            f"({from_iso}..{to_iso})."
        )
    half = span / 2.0
    centre = f + (t - f) / 2
    centre_iso = centre.isoformat().replace("+00:00", "Z")

    # The read budget must EXCEED the NVR's own ffmpeg budget, which scales with
    # the clip. If this timed out first we would report a transport failure for
    # a request the NVR was still serving correctly — the client's own deadline
    # masquerading as the recorder's fault. 300 s was a constant sized for the
    # short clips /clip used to allow.
    read_timeout = max(300.0, 120.0 + 1.5 * span)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=5.0)
        ) as client:
            resp = await client.get(f"{_base()}/clip", params={
                "camera": slug, "timestamp": centre_iso, "before": half, "after": half,
            })
    except httpx.HTTPError as exc:
        log.warning("nvr.clip.unreachable", slug=slug, error=str(exc))
        raise ClipUnavailable(
            f"The NVR could not be reached while extracting {slug} "
            f"{from_iso}..{to_iso}: {exc}"
        ) from exc

    if resp.status_code == 200:
        # The recorder has ALREADY measured whether it actually held footage
        # across the whole requested window (server.py `_compute_coverage`) and
        # says so in these headers. Dropping them here is what left the exported
        # evidence certificate asserting "system operating properly" from a
        # literal.
        cov_raw = resp.headers.get("X-NVR-Coverage")
        coverage = 1.0 if cov_raw is None else _parse_coverage(cov_raw)
        return Clip(data=resp.content, coverage=coverage,
                    warning=resp.headers.get("X-NVR-Warning"))

    reason = _refusal_reason(resp)
    log.warning("nvr.clip.failed", slug=slug, status=resp.status_code, reason=reason)
    raise ClipUnavailable(
        f"The NVR could not extract {slug} {from_iso}..{to_iso}: {reason}"
    )


async def list_cameras() -> Optional[set[str]]:
    """Names with an ACTIVE recording worker, or None if the NVR is unreachable.

    The NVR's /cameras lists every camera with *indexed footage* — including
    ones whose worker died with an NVR restart (runtime cameras are in-memory).
    Only entries carrying an ``rtsp_url`` have a live worker, so filter on it:
    reconcile must re-add index-only cameras or recording silently stays off.
    """
    try:
        resp = await _get_client().get(f"{_base()}/cameras")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return {c["name"] for c in body.get("cameras", []) if c.get("rtsp_url")}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("nvr.sync.list.unreachable", error=str(exc))
        return None


# ── Reconciliation ────────────────────────────────────────────────────────────

async def reconcile(
    enabled: dict[str, tuple[list | None, int | None, int | None]],
    disabled_slugs: set[str],
) -> None:
    """Re-assert desired NVR state.

    • every enabled registry camera is recording (add is idempotent);
      ``enabled`` maps slug → (privacy masks, retention_days,
      groom_after_days) so a re-add after an NVR restart keeps the camera on
      the masked recording path and on its own retention/grooming policies
      (all of which live only in NVR memory)
    • registry cameras that are disabled/deleted are not recording

    Cameras the NVR knows about but the registry doesn't (manual cameras.yaml
    entries) are left alone — the registry only manages its own.

    Keys here are RECORDING NAMES, not slugs: a camera with an enabled sub track
    contributes both `<slug>` and `<slug>_sub`. Comparing slugs alone would read
    `<slug>_sub` as a stranger and leave it alone forever — including after the
    sub is switched off, which would keep recording a stream nobody wants.
    """
    current = await list_cameras()
    if current is None:
        return  # NVR down; next pass will retry

    for slug in sorted(set(enabled) - current):
        masks, retention, groom = enabled[slug]
        await add_camera(slug, retention_days=retention, masks=masks,
                         groom_after_days=groom)
    for slug in sorted(disabled_slugs & current):
        await remove_camera(slug)


async def reconcile_loop() -> None:
    """Background task: periodic registry→NVR reconciliation.

    Heals NVR restarts (runtime cameras are in-memory) and any hook call that
    failed while the NVR was briefly unreachable.
    """
    # Local import to avoid a circular import at module load time.
    from sqlalchemy import select

    from types import SimpleNamespace

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage, sub_recording_name
    from . import schedule as schedule_svc
    from . import tracks
    from .tracks import tracks_for

    log.info("nvr.sync.loop_started", interval=settings.nvr_sync_interval)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            Camera.slug, Camera.enabled, Camera.recording,
                            Camera.privacy_masks, Camera.recording_schedule,
                            Camera.retention_days, Camera.groom_after_days,
                            Camera.rtsp_url, Camera.sub_track,
                        ).where(Camera.stage == CameraStage.REGISTERED.value)
                    )
                ).all()
            # A camera records iff it is enabled AND its recording flag is on
            # AND its schedule (None = 24/7) is in-window right now. This loop
            # is the schedule enforcer: it starts/stops workers at window
            # edges, so schedule granularity = nvr_sync_interval (default 60s).
            # Desired state is per TRACK, and the two tracks do NOT share a
            # lifecycle.
            #
            # The sub keeps its own, much shorter retention. It exists so that
            # scrubbing recent footage is fast; the main is what evidence is
            # drawn from. Keeping both for the same period doubles the storage
            # cost of the feature well past the point it buys anything, and once
            # the sub ages out playback simply falls back to the main — the NVR
            # checks coverage per track, so an expired sub is a track with no
            # footage at that time.
            #
            # The sub is also NOT groomed. Grooming rewrites footage
            # keyframe-only, which plays as a slideshow — precisely destroying
            # the one thing a scrub track is for. `0` disables it.
            #
            # It DOES inherit the privacy masks, and that is not optional: a
            # mask exists to redact a region, and an unmasked sub of a masked
            # camera would publish exactly what the mask hides, at a URL the
            # same operators can reach. It costs a re-encode on the sub as well
            # (the recorder burns masks in), which is the price of the mask
            # meaning what it says.
            enabled: dict[str, tuple] = {}
            disabled: set[str] = set()
            for (slug, en, rec, masks, sched, retention, groom,
                 url, sub_track) in rows:
                on = en and rec and schedule_svc.is_active(sched)
                cam = SimpleNamespace(slug=slug, rtsp_url=url, sub_track=sub_track)
                sub_name = sub_recording_name(slug)
                names = [t.recording_name for t in tracks_for(cam)]
                if on:
                    for name in names:
                        if name == sub_name:
                            enabled[name] = (masks,
                                             tracks.sub_retention_days(cam, retention),
                                             0)
                        else:
                            # `or None`: 0 means "never groom" on the wire now,
                            # but on a MAIN camera it has always meant "no
                            # override" (routers/cameras.py stores it that way).
                            enabled[name] = (masks, retention, groom or None)
                    if sub_name not in names:
                        disabled.add(sub_name)
                else:
                    disabled.add(slug)
                    disabled.add(sub_recording_name(slug))
            await reconcile(enabled, disabled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("nvr.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.nvr_sync_interval)
