"""deepstream_client.py — camera registry → DeepStream analytics pipeline.

Companion to ``deepstream_projection``, which writes each camera's config to
a directory both containers mount. This module makes those writes *take
effect*, and is the half that can tell you whether they did.

The split matters, because the two halves fail differently:

  • the **file** is durable. The pipeline's own startup loader scans the
    directory, so a config written while the pipeline was down is picked up
    when it comes back — with no involvement from this API, which may itself
    be down at that moment.
  • the **push** is immediate and answerable. ``POST /camera/modify`` hands
    the pipeline a path, it re-reads that file and rebuilds the camera's
    state in place, and the HTTP status says whether it worked. A file alone
    can never tell us the pipeline choked on a zone.

Neither alone is sufficient: push-only loses every camera on a pipeline
restart (the pipeline's add/modify endpoints never persist what they are
given — they only ever *read* config files), and write-only leaves the
operator staring at a saved config with no idea whether it is live.

Same trust posture as ``nvr_client``/``motion_client``: the pipeline's API
has no auth of its own, both processes run on the same host, and every call
here is best-effort — a down pipeline must never fail a registry operation.
``reconcile_loop`` heals whatever the hooks missed.

**Removal is expensive, uniquely.** The pipeline's ``/camera/remove``
deletes the camera, then restarts the whole container (it exits and lets
Docker's restart policy bring it back) so nvdsanalytics reloads its
sections. That briefly stops analysis on *every* camera, not just the one
removed. So removals are issued only from the explicit delete path, never
inferred by the reconcile loop — see ``reconcile``.
"""
from __future__ import annotations

import asyncio
from typing import NamedTuple, Optional

import httpx
import structlog

from ..config import settings
from . import deepstream_projection as projection

log = structlog.get_logger(__name__)


class CallResult(NamedTuple):
    """Outcome of one pipeline call.

    `restarting` is separate from `ok` because it is not a failure — the call
    succeeded — but it does mean the process is going away in a moment, so
    nothing else should be pushed at it this cycle.
    """
    ok: bool
    restarting: bool = False

# Adding a stream makes the pipeline open an RTSP connection before it
# answers, which is far slower than the NVR's bookkeeping calls — hence a
# longer read budget than nvr_client's flat 10s. Connect stays short: it is
# loopback, so a slow connect means the process is gone, not busy.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_client: Optional[httpx.AsyncClient] = None


def _base() -> str:
    return settings.deepstream_api_url.rstrip("/")


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


# ── Primitive operations ──────────────────────────────────────────────────────

async def apply_camera(slug: str) -> CallResult:
    """Make the pipeline read `slug`'s config file and apply it.

    Tries ``/camera/modify`` first — it rebuilds the camera's state in place
    without touching the RTSP stream, so re-applying a zone change does not
    interrupt analysis or reset tracker IDs. Its 404 ("not registered") is the
    signal to fall back to ``/camera/add``, which attaches the stream too.

    Ordering this way round rather than add-then-modify is deliberate:
    re-applying an existing camera is the common case by a wide margin (every
    zone edit, every reconcile pass), and it is the case that must stay
    non-disruptive.
    """
    path = projection.pipeline_path(slug)
    try:
        resp = await _get_client().post(f"{_base()}/camera/modify",
                                        json={"config_path": path})
        if resp.status_code == 200:
            restarting = _restart_scheduled(resp)
            log.info("deepstream.apply.modified", slug=slug, restart=restarting)
            return CallResult(True, restarting)
        if resp.status_code != 404:
            log.warning("deepstream.apply.modify_failed", slug=slug,
                        status=resp.status_code, detail=resp.text[:200])
            return CallResult(False)

        # Not registered yet — first time we have seen this camera, or the
        # pipeline restarted and lost it.
        resp = await _get_client().post(f"{_base()}/camera/add",
                                        json={"config_path": path})
        if resp.status_code == 200:
            restarting = _restart_scheduled(resp)
            log.info("deepstream.apply.added", slug=slug, restart=restarting)
            return CallResult(True, restarting)
        log.warning("deepstream.apply.add_failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        # Expected whenever the pipeline is restarting. The file on disk is
        # already correct, and its startup loader reads that file — so this
        # camera comes back on its own without us retrying.
        log.warning("deepstream.apply.unreachable", slug=slug, error=str(exc))
    return CallResult(False)


async def remove_camera(slug: str) -> CallResult:
    """Detach `slug` from the pipeline. 404 (already gone) counts as success.

    Restarts the pipeline container as a side effect (see module docstring),
    so call this only when a camera genuinely goes away.
    """
    try:
        resp = await _get_client().post(f"{_base()}/camera/remove",
                                        json={"sensor_id": slug})
        if resp.status_code in (200, 404):
            # A 404 means the pipeline never had it — nothing was torn down,
            # so nothing restarts, and the caller can keep pushing.
            restarting = _restart_scheduled(resp)
            log.info("deepstream.remove.ok", slug=slug,
                     missing=resp.status_code == 404, restart=restarting)
            return CallResult(True, restarting)
        log.warning("deepstream.remove.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("deepstream.remove.unreachable", slug=slug, error=str(exc))
    return CallResult(False)


def _restart_scheduled(resp: httpx.Response) -> bool:
    """Whether the pipeline told us it is about to restart.

    It answers 200 and *then* exits a few seconds later, so a truthy value
    here means every subsequent call this cycle will fail against a dying
    process. Callers stop pushing rather than logging a burst of misleading
    connection errors.
    """
    try:
        return bool(resp.json().get("restart_scheduled"))
    except ValueError:
        return False


async def list_cameras() -> Optional[dict[str, str]]:
    """Registered sensor_id → stream URL, or None if the pipeline is down.

    None and `{}` mean different things and must not be collapsed: an empty
    dict is a live pipeline with nothing loaded (reconcile should push
    everything), None is no answer at all (reconcile must do nothing).
    """
    try:
        resp = await _get_client().get(f"{_base()}/camera/list")
        resp.raise_for_status()
        return {
            c["sensor_id"]: c.get("camera_url") or ""
            for c in resp.json().get("cameras", [])
            if c.get("sensor_id")
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        log.warning("deepstream.list.unreachable", error=str(exc))
        return None


# ── Orchestration ─────────────────────────────────────────────────────────────

async def sync_now(reason: str) -> None:
    """Project the registry to disk, then push whatever changed.

    This is what the camera endpoints call. Never raises: the operator's save
    already succeeded, and a pipeline that is down or restarting is a
    condition to report, not an error to hand back to them.
    """
    if not settings.deepstream_projection_enabled:
        return

    res = await projection.project_now(reason)
    if res.locked_out:
        return  # another worker is projecting the same state and will push it

    # Removals first: each one may restart the pipeline, and once it does
    # there is nothing left to push at — the restarted process reloads every
    # remaining camera from the directory we just wrote, and the removed ones
    # stay gone because their files are already deleted.
    for slug in res.removed_slugs:
        if (await remove_camera(slug)).restarting:
            log.info("deepstream.sync.deferred_to_restart", reason=reason,
                     pending=len(res.written_slugs))
            return

    for slug in res.written_slugs:
        if (await apply_camera(slug)).restarting:
            # An add can restart too, when it changes the entry/exit lines
            # nvdsanalytics reads from a file only re-parsed at startup.
            log.info("deepstream.sync.deferred_to_restart", reason=reason)
            return


async def reconcile(desired: dict[str, str], registry_slugs: set[str]) -> None:
    """Re-assert desired pipeline state.

    Adds back every camera the pipeline is missing — the case that actually
    matters, because the pipeline forgets everything on restart and restarts
    itself routinely (any entry/exit line change does it).

    Deliberately does **not** remove. A removal restarts the whole container,
    stopping analysis on every camera, and inferring one from a background
    loop means a single bad read of either side takes the appliance down on a
    timer. Removals come only from the explicit delete path, where a human
    asked for it; a camera the loop believes is stale still has no config file
    on disk, so it is gone for good at the pipeline's next restart anyway.

    Cameras the pipeline knows about but the registry has never heard of
    (hand-authored configs that predate this integration) are left strictly
    alone, matching how nvr_client treats manual cameras.yaml entries.
    """
    current = await list_cameras()
    if current is None:
        return  # pipeline down; next pass retries

    for slug in sorted(set(desired) - set(current)):
        await apply_camera(slug)

    # A camera whose relay URL moved (SERVER_IP / RTSP_PORT changed) needs a
    # remove+add — modify updates our bookkeeping but never re-attaches the
    # stream. Reported rather than performed, for the same reason as above:
    # this is a deployment-level change, and the fix costs a full restart.
    for slug, url in sorted(desired.items()):
        live = current.get(slug)
        if live and live != url:
            log.warning("deepstream.reconcile.url_drift", slug=slug,
                        pipeline_url=live, expected_url=url,
                        action="restart the pipeline to pick up the new URL")

    stale = sorted((set(current) & registry_slugs) - set(desired))
    if stale:
        log.info("deepstream.reconcile.stale_present", slugs=stale,
                 note="config removed from disk; clears at the next pipeline restart")


async def reconcile_loop() -> None:
    """Background task: repairs drift in both halves.

    Sweeps the config directory (healing a failed write, a hand-edit, or a
    change made while this process was down), then re-asserts the pipeline's
    in-memory state against it. This is what makes every hook in the request
    path safe to be best-effort.
    """
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage

    interval = settings.deepstream_projection_interval
    log.info("deepstream.sync.loop_started", interval=interval,
             dir=settings.deepstream_config_dir, api=_base())
    while True:
        try:
            await sync_now("periodic")

            async with AsyncSessionLocal() as db:
                cameras = (await db.execute(
                    select(Camera).where(
                        Camera.stage == CameraStage.REGISTERED.value
                    )
                )).scalars().all()
            desired = {
                c.slug: settings.local_rtsp_url(c.slug)
                for c in cameras if projection.should_project(c)
            }
            registry_slugs = {c.slug for c in cameras if c.slug}
            await reconcile(desired, registry_slugs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive anything
            log.warning("deepstream.sync.loop_error", error=str(exc))
        await asyncio.sleep(interval)
