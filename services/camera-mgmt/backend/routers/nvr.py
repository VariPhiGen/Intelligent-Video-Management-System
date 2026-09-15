"""nvr.py — authenticated reverse-proxy to the NVR recording service.

The NVR (services/nvr) has no auth of its own and binds to 127.0.0.1. Users
reach it exclusively through this proxy, so the product keeps one login:

    /api/nvr/<path>  →  <NVR_API_URL>/<path>

RBAC: reads (camera list, coverage, clips, snapshots) are available to any
authenticated principal; mutating calls (add/remove camera — normally done by
the registry sync, not humans) require the ``admin`` role.

Clip extraction can transcode HEVC→H.264 and take tens of seconds, and the
responses are binary (video/mp4, image/jpeg) — hence the long timeout and the
pass-through of content headers.
"""
from __future__ import annotations

import json
import re

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status

from ..config import settings
from ..models import sub_recording_name
from ..security import get_principal
from ..services import audit as audit_svc
from ..services import policy as policy_svc

log = structlog.get_logger(__name__)
router = APIRouter(tags=["nvr"])

# Path heads worth an audit row, and the action name each records. Footage
# extraction is an evidence export; the report path is a generated report.
_AUDIT_ACTION = {"clip": "evidence.export_requested", "report": "report.generated"}

# Capability required per read path (Roles & permissions matrix). Health and
# storage stay open to any authenticated principal — they're monitoring, not
# footage access.
def _read_capability(path: str) -> str | None:
    head = path.split("/", 1)[0].split("?", 1)[0]
    if head == "report":
        return "export_reports"
    if head in ("clip", "snapshot", "coverage", "cameras", "hls"):
        return "playback_search"
    return None

# Clip extraction (ffmpeg seek + optional transcode) is the slow path. HLS
# segments come through the same proxy; a cached one is a file read, an
# uncached transcode queues behind the NVR's extraction semaphore.
_TIMEOUT = httpx.Timeout(300.0, connect=5.0)
_DROP_REQ = {"host", "content-length", "connection", "cookie", "authorization"}
# Content headers the browser needs for playback/downloads, plus the NVR's
# clip-coverage marker (the playback UI shows a gap warning when < 1).
# `x-nvr-hls-mode` / `x-nvr-source-codec` tell the playback UI which of the
# three HLS delivery paths a camera landed on — without them a silent fall to
# the per-segment transcode path is invisible.
_KEEP_RESP = {"content-type", "content-disposition", "content-length",
              "x-nvr-coverage", "x-nvr-hls-mode", "x-nvr-source-codec"}


_PURGE_PATH = re.compile(r"^cameras/([^/?]+)/recordings$")


def _purge_camera(path: str) -> str | None:
    """The camera name for a whole-camera purge, or None for anything else.

    Deliberately exact: `cameras/{name}/recordings/range` is scoped erasure and
    must NOT be swept into the all-tracks fan-out — it carries a time window and
    is audited on its own terms further down.
    """
    m = _PURGE_PATH.match(path.split("?", 1)[0].strip("/"))
    return m.group(1) if m else None


async def _purge_all_tracks(camera: str, request: Request, principal) -> Response:
    """DELETE the recordings of `camera` and of its `<slug>_sub` track, merging
    the two results into one answer.

    404 means "this recording name has no footage", which for the sub is the
    normal case — most cameras never recorded one — so it counts as success
    with zero bytes. The MAIN's 404 does not end the request: a camera whose
    main footage already aged out (or was range-erased) can still hold sub
    segments, and those are exactly the bytes the row told the operator about.
    Only when BOTH tracks are empty is the main's 404 passed through, because
    "nothing to purge" is then the whole truth and the SPA already reads it
    that way.

    A sub-track failure is a FAILURE, not a footnote. This response feeds the
    row's "freed" toast, which is a claim about deleted footage; reporting ok
    while a copy of that footage remains is the difference between deleted and
    hidden (the DPDP erasure concern). The main's purge is still audited —
    it happened — but the request errors so the operator retries instead of
    trusting a half-truth.
    """
    base = settings.nvr_api_url.rstrip("/")
    sub = sub_recording_name(camera)
    results: dict[str, dict] = {}
    main_404: Response | None = None
    sub_failure: str | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for name in (camera, sub):
            try:
                r = await client.delete(f"{base}/cameras/{name}/recordings")
            except httpx.RequestError as exc:
                log.warning("nvr.purge.unreachable", camera=name, error=str(exc))
                if name == camera:
                    return Response(
                        content=b'{"detail":"NVR service is unreachable"}',
                        status_code=502, media_type="application/json")
                sub_failure = "unreachable"
                continue
            if name == camera and r.status_code >= 400 and r.status_code != 404:
                # Let the caller see the NVR's own refusal verbatim.
                return Response(content=r.content, status_code=r.status_code,
                                media_type="application/json")
            if name == camera and r.status_code == 404:
                main_404 = Response(content=r.content, status_code=404,
                                    media_type="application/json")
            if r.status_code == 200:
                results[name] = r.json()
            elif r.status_code != 404:
                log.warning("nvr.purge.track_failed", camera=name,
                            status=r.status_code, detail=r.text[:200])
                if name == sub:
                    sub_failure = f"HTTP {r.status_code}"

    merged = {
        "status": "partial" if sub_failure else "ok",
        "camera": camera,
        "segments_deleted": sum(v.get("segments_deleted", 0) for v in results.values()),
        "bytes_freed": sum(v.get("bytes_freed", 0) for v in results.values()),
        # Per track, so "the sub held nothing" and "the sub was never asked"
        # stay apart in the audit record.
        "tracks_purged": {n: results.get(n, {}).get("segments_deleted", 0)
                          for n in (camera, sub)},
    }
    if main_404 is not None and not results and not sub_failure:
        # Neither track held anything and nothing failed: "no recordings" is
        # the whole truth, and the SPA already reads the NVR's own 404 as the
        # desired state. BEFORE the audit, restoring the old invariant that an
        # "evidence.purged" row implies a purge actually executed — a
        # double-click on an empty camera must not write records an auditor
        # later reads as "evidence existed and was destroyed".
        return main_404
    # Audited when something was deleted OR a real attempt partially failed —
    # with the failure named, so the record never reads as "both tracks clean"
    # when one was not even reachable.
    await audit_svc.record(
        request, principal, "evidence.purged", target=camera,
        detail={"segments_deleted": merged["segments_deleted"],
                "bytes_freed": merged["bytes_freed"],
                "tracks_purged": merged["tracks_purged"],
                **({"sub_track_failed": sub_failure} if sub_failure else {})},
    )
    if sub_failure:
        # An error, not a 200 with a sad field: the SPA's purge loop counts any
        # 2xx as fully freed, and this response is the basis of a deletion
        # claim. The detail states only what actually happened — with the main
        # at 404 ("nothing there"), claiming it was purged would assert an
        # action that never ran.
        main_phrase = ("Purged the main track" if camera in results
                       else "The main track had no recordings")
        merged["detail"] = (
            f"{main_phrase}, but the low-res sub track failed "
            f"({sub_failure}) — its footage may remain. Retry the purge.")
        return Response(content=json.dumps(merged).encode(), status_code=502,
                        media_type="application/json")
    return Response(content=json.dumps(merged).encode(), status_code=200,
                    media_type="application/json")


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    principal = await get_principal(request)
    if request.method != "GET":
        # Annotation saves are an OPERATOR action (draw a box on a paused
        # playback frame), not fleet administration — gate them on the same
        # capability as the playback page they are drawn from, exactly the
        # shape of motion.py's reset carve-out. Everything else mutating stays
        # admin-only: those routes are the registry sync's, not a human's.
        if request.method == "POST" and path.split("?", 1)[0].strip("/") == "annotations":
            await policy_svc.require(principal, "playback_search")
        elif not principal.has_any(frozenset({"admin"})):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires role: admin",
            )
    cap = _read_capability(path) if request.method == "GET" else None
    if cap:
        await policy_svc.require(principal, cap)

    # Purging a camera's footage has to reach EVERY recording track of it.
    # A camera with a sub track holds a second, low-resolution copy under
    # `<slug>_sub`, and the Storage tab folds those bytes into the parent's row
    # — so the operator selects one row, is told how much it holds including
    # the sub, and the purge deleted only the main. The freed figure was a
    # claim about bytes that were still on disk, and since the per-`_sub` row
    # was folded away there was no longer any way to purge them at all.
    #
    # Handled here rather than in the SPA so every caller of this proxy gets it,
    # and so the merged byte total is what gets reported back.
    if request.method == "DELETE":
        purge_target = _purge_camera(path)
        if purge_target:
            return await _purge_all_tracks(purge_target, request, principal)

    url = f"{settings.nvr_api_url.rstrip('/')}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
    except httpx.RequestError as exc:
        log.warning("nvr.proxy.unreachable", path=path, error=str(exc))
        return Response(
            content=b'{"detail":"NVR service is unreachable"}',
            status_code=502,
            media_type="application/json",
        )

    # Audit successful evidence exports / report generation (not browsing reads).
    head = path.split("/", 1)[0].split("?", 1)[0]
    action = _AUDIT_ACTION.get(head)
    if action and request.method == "GET" and upstream.status_code < 400:
        await audit_svc.record(
            request, principal, action, target=path,
            detail={k: v for k, v in request.query_params.items()} or None,
        )
    # Annotation saves are an evidence-adjacent act (an operator asserting
    # "this person/object, at this instant") — record who drew what where. The
    # payload itself is multipart, which this pass-through never parses, so the
    # SPA duplicates camera/timestamp as query params purely for this row; the
    # NVR reads its Form fields and ignores them.
    if (request.method == "POST"
            and path.split("?", 1)[0].strip("/") == "annotations"
            and upstream.status_code < 400):
        await audit_svc.record(
            request, principal, "annotation.created",
            target=request.query_params.get("camera") or "annotations",
            detail={k: v for k, v in request.query_params.items()} or None,
        )
    # Scoped erasure through the raw proxy must not be a silent path. A caller
    # that erases footage this way bypasses whatever richer record the
    # originating workflow would have written, so log it here regardless.
    if (request.method == "DELETE" and path.endswith("/recordings/range")
            and upstream.status_code < 400):
        await audit_svc.record(
            request, principal, "evidence.erased", target=path,
            detail={k: v for k, v in request.query_params.items()} or None,
        )

    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() in _KEEP_RESP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )
