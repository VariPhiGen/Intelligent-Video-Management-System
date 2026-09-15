"""
relay.py — MediaMTX REST API client

All communication with MediaMTX is async HTTP via httpx.  We never call
subprocess.run() or spawn persistent FFmpeg processes here.

TODO: VERIFY — All endpoint paths assume MediaMTX v1.9.x which uses /v3/
  prefix.  Run `curl http://localhost:9997/v3/config/global/get` to verify the
  version.  Earlier versions (v1.x ≤ 1.4) used /v1/, later versions may change.

Concurrency note:
  Multiple uvicorn worker processes may call add_path / remove_path for the
  same slug concurrently (e.g., during startup re-registration).  MediaMTX
  itself serialises config mutations internally, so an add of an existing path
  is benign — treat it as success.  It reports that as 400 + "path already
  exists" rather than a 409, and uses the same 400 for genuine config
  rejections, so the test is on the response body (see _is_already_exists).
  Callers that need strict once-and-only-once semantics must hold a PostgreSQL
  advisory lock before calling these functions (see cameras.py for the lock
  pattern).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx
import structlog

from ..config import settings
from ..models import owning_slug
from ..urlutil import redact_credentials
from .. import redis_client
from . import tlsutil

log = structlog.get_logger(__name__)

_BASE = settings.mediamtx_api_url.rstrip("/")

# ── Shared async client (one per process, created lazily) ─────────────────────
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_BASE,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ─────────────────────────────────────────────────────────────────────────────
# Path lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def _restamp_command(slug: str, rtsp_url: str) -> str:
    """FFmpeg hop that strips a broken source clock, for `runOnInit`.

    `-c copy` throughout: this re-writes timestamps, never pixels, so it costs
    ~0.5 % of one core — the same as one of the NVR's existing per-camera
    recording processes — and never touches the GPU.

    `-use_wallclock_as_timestamps 1` is the load-bearing flag. It replaces the
    source's timestamps at the demuxer with the local clock, which is what
    actually absorbs the wrap; with `-c copy` alone FFmpeg carries the source
    DTS through and its only correction is a +1-tick nudge, useless against a
    jump of -4295 s. `+genpts` and `make_zero` keep the republished stream
    monotonic from zero.

    Deliberately no `-bsf:v hevc_metadata`: the detector keys on a timestamp
    symptom, not a codec, so it can flag an H.264 camera — and that bitstream
    filter aborts on non-HEVC input. It only rewrote parameter-set metadata,
    which has no bearing on timestamps, so dropping it does not change the
    behaviour this was verified for.

    $MTX_PATH is expanded by MediaMTX to the path name, so one command string
    is correct for every slug.
    """
    target = f"rtsp://{settings.relay_internal_host}:{settings.rtsp_port}/$MTX_PATH"
    return (
        "ffmpeg -nostdin -hide_banner -loglevel warning "
        "-rtsp_transport tcp -use_wallclock_as_timestamps 1 "
        f"-i '{rtsp_url}' "
        "-c copy -fflags +genpts -avoid_negative_ts make_zero "
        "-max_interleave_delta 0 "
        f"-f rtsp -rtsp_transport tcp '{target}'"
    )


async def _path_config(slug: str, rtsp_url: str, on_demand: bool = False) -> dict[str, Any]:
    # A camera whose recorder emits a backward timestamp jump cannot be pulled
    # by MediaMTX directly — its DTS extractor gives up and kills the HLS muxer
    # (see redis_client.set_restamp for the mechanism). Such a camera is fed by
    # an FFmpeg hop instead: MediaMTX stops pulling and becomes the publish
    # target, so the camera is still opened exactly once, just by FFmpeg.
    #
    # Gated per-camera on purpose. The hop costs ~300 ms of added latency, and
    # a healthy camera must not pay it.
    #
    # owning_slug, NOT `slug`: this function is called once per TRACK, so for a
    # sub track `slug` is `<camera>_sub` while health.py writes the flag under
    # the bare camera slug. Reading it by the track name found a key nothing
    # writes, so a flagged camera got the hop on its main stream and left its
    # sub pulling directly from the same broken clock — dying on the same
    # 71 m 35 s cycle, with nothing to notice because the health monitor is
    # deliberately one-row-per-camera. Both tracks come off one recorder and
    # therefore one clock, so they move together.
    if await redis_client.get_restamp(owning_slug(slug)):
        return {
            "source": "publisher",
            "runOnInit": _restamp_command(slug, rtsp_url),
            "runOnInitRestart": True,
            "maxReaders": 0,
            "record": False,
            "overridePublisher": True,
        }

    # Pin the camera's TLS certificate for rtsps:// sources. These are
    # self-signed and routinely carry the wrong CN/SAN, so MediaMTX — which has
    # no skip-verify switch for RTSP sources — rejects them outright without a
    # fingerprint. Always send the key, empty for plain rtsp://: patch_path
    # merges into the live config, so an omitted field would leave a stale pin
    # behind after a URL is edited back to plaintext. See services/tlsutil.py.
    fingerprint = await tlsutil.source_fingerprint(rtsp_url) or ""
    return {
        "source": rtsp_url,
        "sourceFingerprint": fingerprint,
        # Pull the camera over RTSP-interleaved TCP, not the MediaMTX default
        # ("automatic" = UDP). UDP drops packets on high-bitrate sources (big
        # I-frames burst past the socket buffer), corrupting I-frames and
        # pixelating the whole GOP. TCP retransmits, so the relayed bitstream
        # stays intact. See health.py reconnect path — both leave this in place.
        "rtspTransport": "tcp",
        "sourceOnDemand": on_demand,
        "sourceOnDemandStartTimeout": "10s",
        "sourceOnDemandCloseAfter": "10s",
        "maxReaders": 0,
        "record": False,
        "overridePublisher": True,
    }


def _is_already_exists(resp: httpx.Response) -> bool:
    """True when a path-add failed only because the path is already there.

    MediaMTX v1.9.1 answers a duplicate add with 400 + {"error":"path already
    exists"}, NOT the 409 this module once assumed — so the old status-only
    check never fired and every idempotent re-registration was logged as an
    error and re-raised (a burst of relay.bulk_register.failed on each startup,
    hiding real failures behind it).

    Match the body, not the bare status: MediaMTX also returns 400 for genuine
    config rejections — {"error":"invalid source: 'not-a-url'"}, an unusable
    sourceFingerprint — and reading those as success would be strictly worse
    than the noise. 409 stays in the tuple so the check survives a future
    MediaMTX switching to the more apt status.
    """
    return resp.status_code in (400, 409) and "already exists" in resp.text


async def add_path(slug: str, rtsp_url: str, on_demand: bool = False) -> bool:
    """
    Register a new path in MediaMTX.  Returns True on success.
    An "already exists" rejection is treated as success (see _is_already_exists).
    Raises httpx.HTTPStatusError for other 4xx/5xx responses.
    """
    client = _get_client()
    try:
        resp = await client.post(
            f"/v3/config/paths/add/{slug}",
            json=await _path_config(slug, rtsp_url, on_demand),
        )
        if _is_already_exists(resp):
            log.debug("relay.add_path.already_exists", slug=slug)
            return True
        resp.raise_for_status()
        # Redacted: this URL carries the camera's password, and a plaintext
        # copy in the logs would undo encrypting it at rest.
        log.info("relay.add_path.ok", slug=slug, source=redact_credentials(rtsp_url))
        return True
    except httpx.HTTPStatusError as exc:
        # The body too — MediaMTX echoes the offending path config, source
        # URL included, in its validation errors.
        log.error("relay.add_path.error", slug=slug, status=exc.response.status_code,
                  body=redact_credentials(exc.response.text))
        raise
    except httpx.RequestError as exc:
        log.error("relay.add_path.request_error", slug=slug, error=str(exc))
        raise


async def patch_path(slug: str, rtsp_url: str, on_demand: bool = False) -> bool:
    """Update an existing path config (e.g., after an rtsp_url change)."""
    client = _get_client()
    resp = await client.patch(
        f"/v3/config/paths/patch/{slug}",
        json=await _path_config(slug, rtsp_url, on_demand),
    )
    resp.raise_for_status()
    log.info("relay.patch_path.ok", slug=slug, source=redact_credentials(rtsp_url))
    return True


async def ensure_path(slug: str, rtsp_url: str, on_demand: bool = False) -> bool:
    """Make the path exist AND point at `rtsp_url` — add it, or repoint it.

    `add_path` treats "already exists" as success without touching the existing
    config, which is right for an idempotent create and wrong for anything that
    can change a source URL. A sub track's URL is composed from the main's
    credentials at use time, so a password rotation or a re-probe that finds a
    better profile changes it — and every caller was using `add_path`, so the
    relay went on pulling the OLD profile, or after a credential change nothing
    at all. Silent either way: the path exists and MediaMTX reports it fine.
    """
    await add_path(slug, rtsp_url, on_demand)
    # Unconditional: `add_path` cannot tell us whether it created or found the
    # path (it collapses both onto True), and patching a path we just created
    # with the same config is a no-op. Cheaper than asking.
    return await patch_path(slug, rtsp_url, on_demand)


async def remove_path(slug: str) -> bool:
    """
    Remove a path from MediaMTX.  Returns True on success.
    A 404 (path not found) is treated as success.
    """
    client = _get_client()
    try:
        # MediaMTX v3 route is config/paths/DELETE — "remove" does not exist,
        # and the resulting 404 was treated as "already gone", silently
        # no-opping every programmatic removal (delete/disable/reconnect/
        # orphan sweep) while manual curls to /delete/ worked.
        resp = await client.delete(f"/v3/config/paths/delete/{slug}")
        if resp.status_code == 404:
            log.debug("relay.remove_path.not_found", slug=slug)
            return True
        resp.raise_for_status()
        log.info("relay.remove_path.ok", slug=slug)
        return True
    except httpx.HTTPStatusError as exc:
        log.error("relay.remove_path.error", slug=slug, status=exc.response.status_code)
        raise


async def force_reconnect(slug: str, rtsp_url: str) -> None:
    """
    Force-cycle a path to restart the source connection.
    Used by the health monitor after the auto-reconnect backoff expires.
    """
    log.info("relay.force_reconnect", slug=slug)
    await remove_path(slug)
    await asyncio.sleep(0.5)
    await add_path(slug, rtsp_url)


async def set_restamp(slug: str, rtsp_url: str, enabled: bool) -> None:
    """Turn the FFmpeg re-stamp hop on or off for one camera, and re-register.

    Delete-then-add rather than PATCH: the two path shapes are structurally
    different — one carries `source: <url>` + `sourceFingerprint`, the other
    `source: publisher` + `runOnInit` — and PATCH merges, so a patched path
    would keep the stale half of whichever shape it came from. Recreating is
    also what stops (or starts) the `runOnInit` process, which MediaMTX only
    reconsiders at path creation.
    """
    await redis_client.set_restamp(slug, enabled)
    log.info("relay.set_restamp", slug=slug, enabled=enabled)
    await remove_path(slug)
    await asyncio.sleep(0.5)
    await add_path(slug, rtsp_url)


# ─────────────────────────────────────────────────────────────────────────────
# Path status
# ─────────────────────────────────────────────────────────────────────────────

def ready_since_of(path: dict[str, Any]) -> Optional[str]:
    """MediaMTX's `readyTime` for one path entry, or None if it is not ready.

    THE SINGLE DEFINITION, because two surfaces need it and they must not
    disagree. `GET /cameras` enriches a whole page from `list_active_paths()`
    and `GET /cameras/{id}` enriches one camera from `get_path_status()`; both
    answer the same field on the same model, so the normalisation cannot be
    written out twice. It was, once — the detail endpoint simply omitted it and
    reported every streaming camera as having no ready time.

    `readyTime` carries nanosecond precision that not every Date parser
    accepts, so it is truncated to whole seconds and stamped `Z`. `ready` is
    checked as well as `readyTime`: a path that has gone down keeps its last
    `readyTime`, and reporting that as "up since" would be a lie with a
    plausible value in it.
    """
    if not (path.get("ready") and path.get("readyTime")):
        return None
    return str(path["readyTime"]).split(".")[0] + "Z"


async def get_path_status(slug: str) -> dict[str, Any]:
    """
    Query the live (runtime) path state from MediaMTX.

    Returns a dict with keys:
      connected    bool   — True if MediaMTX has an active source for this path
      source_type  str    — e.g. "rtspSession", "rtmpConn", or None
      tracks       list   — list of track dicts [{id, type}, …]
      readers      list   — list of active reader dicts
      ready_time   str    — ISO-8601 Z second precision, or None (see
                            :func:`ready_since_of`)

    A 404 means the path is not registered at all (needs re-registration).
    TODO: VERIFY — /v3/paths/get/{name} endpoint exists in v1.9.x;
      earlier versions may use /v1/paths/{name}.
    """
    client = _get_client()
    try:
        resp = await client.get(f"/v3/paths/get/{slug}")
        if resp.status_code == 404:
            return {"connected": False, "source_type": None, "tracks": [],
                    "readers": [], "registered": False, "ready_time": None}
        resp.raise_for_status()
        data = resp.json()
        source = data.get("source")
        # Use the 'ready' field — source is non-null even for offline paths
        # (it just describes the configured source type, not live state).
        connected = data.get("ready", False)
        return {
            "connected": connected,
            "registered": True,
            "source_type": source.get("type") if source else None,
            "tracks": data.get("tracks", []),
            "readers": data.get("readers", []),
            "ready_time": ready_since_of(data),
        }
    except httpx.RequestError as exc:
        log.warning("relay.get_path_status.unreachable", slug=slug, error=str(exc))
        return {"connected": False, "source_type": None, "tracks": [],
                "readers": [], "registered": False, "ready_time": None,
                "error": str(exc)}


async def list_active_paths() -> list[dict[str, Any]]:
    """
    Return all paths currently known to MediaMTX, across every page.

    MediaMTX caps /v3/paths/list at 100 items per page, so a single request
    silently truncates once more than 100 paths exist.  We request a large page
    size and follow pageCount to be safe.
    """
    client = _get_client()
    items: list[dict[str, Any]] = []
    try:
        page = 0
        while True:
            resp = await client.get(
                "/v3/paths/list",
                params={"page": page, "itemsPerPage": 1000},
            )
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("items", []))
            if page + 1 >= (data.get("pageCount", 1) or 1):
                break
            page += 1
        return items
    except Exception as exc:
        log.warning("relay.list_active_paths.error", error=str(exc))
        return items


async def is_reachable() -> bool:
    """Quick reachability check for system health endpoint."""
    client = _get_client()
    try:
        resp = await client.get("/v3/config/global/get", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Bulk registration (called on startup)
# ─────────────────────────────────────────────────────────────────────────────

async def register_cameras_bulk(cameras: list[dict[str, Any]]) -> None:
    """
    Re-register all enabled cameras into MediaMTX after a restart.
    Fires all add_path calls concurrently (bounded semaphore to avoid
    overwhelming the MediaMTX API with thousands of simultaneous requests).
    """
    sem = asyncio.Semaphore(20)

    from types import SimpleNamespace

    from .tracks import relay_tracks_for

    async def _add(name: str, url: str, on_demand: bool = False) -> None:
        async with sem:
            try:
                await add_path(name, url, on_demand)
            except Exception as exc:
                log.error("relay.bulk_register.failed", slug=name, error=str(exc))

    # One path per TRACK, not per camera: a camera with a sub track needs both
    # relayed or the NVR has nothing to record the sub from. relay_tracks_for()
    # is the single definition for the RELAY — see services/tracks.py — and it
    # also carries a resolved-but-unrecorded sub on demand, so the live view can
    # fall back to it for a camera the browser cannot decode.
    jobs = []
    for c in cameras:
        cam = SimpleNamespace(slug=c["slug"], rtsp_url=c["rtsp_url"],
                              sub_track=c.get("sub_track"))
        for t in relay_tracks_for(cam):
            jobs.append(_add(t.recording_name, t.url, t.on_demand))

    await asyncio.gather(*jobs, return_exceptions=False)
    log.info("relay.bulk_register.done", cameras=len(cameras), paths=len(jobs))


# ─────────────────────────────────────────────────────────────────────────────
# Periodic reconcile (heals MediaMTX restarts)
# ─────────────────────────────────────────────────────────────────────────────

async def reconcile_loop() -> None:
    """Background task: periodically re-assert every enabled camera's path.

    MediaMTX keeps its rtspSource paths in memory, so a MediaMTX restart drops
    them ALL at once and the streams go dark. The API otherwise pushes paths
    only at its own startup (see main.py step 3), so a MediaMTX that restarts
    under a long-lived API is never healed — every camera stays offline until
    the API is restarted by hand. This loop is the missing counterpart to
    nvr_client/motion_client.reconcile_loop for the relay.

    Scope is deliberately PRESENCE, not liveness:
      • desired path missing from MediaMTX  → add_path        (heals the restart)
      • disabled camera's path still present → remove_path     (converges state)
      • present but source disconnected      → left untouched  (the health
        monitor owns reconnect, with per-camera exponential backoff)

    add_path is idempotent (an existing path returns success), so this never
    fights the health monitor. Best-effort throughout: a down MediaMTX or one
    bad path must not stall the sweep — the next tick retries.
    """
    # Local imports to avoid a circular import at module load time (mirrors
    # nvr_client/motion_client).
    from sqlalchemy import select

    from types import SimpleNamespace

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage, sub_recording_name
    from .tracks import relay_tracks_for

    log.info("relay.sync.loop_started", interval=settings.mediamtx_sync_interval)
    while True:
        try:
            # Nothing to reconcile against a MediaMTX that is down (e.g. stopped
            # for maintenance) — skip the pass rather than flap add_path at it.
            if await is_reachable():
                async with AsyncSessionLocal() as db:
                    rows = (
                        await db.execute(
                            select(Camera.slug, Camera.rtsp_url, Camera.enabled,
                                   Camera.sub_track)
                            .where(Camera.stage == CameraStage.REGISTERED.value)
                        )
                    ).all()
                # Desired state is per TRACK. A camera contributes its main path
                # and, when its sub track is enabled, a `<slug>_sub` path too.
                # Anything a camera could have but should not — a disabled
                # camera, or one whose sub was switched off — converges away
                # below, which is what makes turning the sub off actually stop it.
                desired: dict[str, tuple[str, bool]] = {}
                absent: set[str] = set()
                for slug, url, enabled, sub_track in rows:
                    cam = SimpleNamespace(slug=slug, rtsp_url=url, sub_track=sub_track)
                    # relay_tracks_for, NOT tracks_for: the relay also carries a
                    # sub that exists but is not recorded, on demand, so the
                    # live view has something to fall back to on a camera whose
                    # codec the browser cannot decode. Recording stays opt-in.
                    names = {t.recording_name: (t.url, t.on_demand)
                             for t in relay_tracks_for(cam)}
                    if enabled:
                        desired.update(names)
                        # A sub that exists but is switched off must be removed,
                        # not merely left un-added.
                        if sub_recording_name(slug) not in names:
                            absent.add(sub_recording_name(slug))
                    else:
                        absent.add(slug)
                        absent.add(sub_recording_name(slug))

                current = {p["name"] for p in await list_active_paths()}

                missing = sorted(s for s in desired if s not in current)
                for slug in missing:
                    try:
                        await add_path(slug, desired[slug][0], desired[slug][1])
                    except Exception as exc:  # noqa: BLE001 — one path can't stall the sweep
                        log.warning("relay.sync.add_failed", slug=slug, error=str(exc))

                for slug in sorted(absent & current):
                    try:
                        await remove_path(slug)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("relay.sync.remove_failed", slug=slug, error=str(exc))

                if missing:
                    log.info("relay.sync.paths_re_added", count=len(missing))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("relay.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.mediamtx_sync_interval)
