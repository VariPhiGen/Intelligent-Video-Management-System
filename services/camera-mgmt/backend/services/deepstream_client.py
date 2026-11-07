"""deepstream_client.py — camera registry → DeepStream analytics pipeline, over HTTP.

The VMS tells the pipeline about a camera by POSTing the camera's full config
(the *envelope* ``services/deepstream.py::deepstream_config`` produces) to the
pipeline's control API on :8081. No shared directory, no bind mount, no path:
DeepStream validates the envelope, writes its own ``<sensor_id>.json`` and
applies it to the live pipeline in one call. The HTTP status is the answer.

    create / update / analytics edit  → sync_camera(camera)
          should_project(camera)?  yes → POST /camera/modify {envelope}
                                          404 not_registered → POST /camera/add
                                    no  → POST /camera/remove {"sensor_id"}
    delete                            → sync_removal(slug)
    startup + every N seconds         → sync_now("periodic")  (full sweep + reconcile)

``modify`` is tried first on purpose: re-applying an existing camera is by far
the common case (every zone edit, every reconcile pass) and the pipeline
answers ``unchanged`` for an identical push without touching anything. Its
404 is the "first time we see this camera" signal, and ``add`` attaches the
stream. Both are hot on the pipeline side; the response's
``restart_scheduled`` flag is the one case where it is not.

Ownership: every envelope carries ``_managed_by: vms-camera-mgmt``. The
pipeline refuses to overwrite or delete a config that lacks that marker
(``409 unowned_config``), which is how hand-authored cameras on the pipeline
stay out of reach of this loop. Such slugs are remembered and skipped until
the pipeline restarts (``boot_id`` changes).

Durability: a ``200`` means the config is fsync'd on the pipeline's disk *and*
applied; the pipeline reloads it on its own restart. Anything else — 4xx, 5xx,
timeout, connection refused — means assume nothing landed, and the periodic
``sync_now`` re-asserts it. That is why the loop is load-bearing: a push made
while the pipeline is down is simply lost until the next pass.

Best-effort throughout, like ``nvr_client``: a down pipeline must never fail a
registry operation.
"""
from __future__ import annotations

import asyncio
import time
from typing import NamedTuple, Optional

import httpx
import structlog

from ..config import settings
from ..models import Camera, CameraStage, DeepstreamSettings
from .deepstream import deepstream_config

log = structlog.get_logger(__name__)

# Stamped into every envelope; the pipeline uses it to tell our cameras from
# hand-authored ones and only lets us overwrite/delete what carries it.
_MARKER = "vms-camera-mgmt"


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

# Slugs the pipeline refused with 409 unowned_config (a hand-authored file of
# the same name), and when it last refused each one. Skipping them is only to
# stop the periodic sweep logging the same refusal every pass.
#
# THE SKIP EXPIRES, and that matters: the refusal tells the operator to run
# POST /camera/adopt on the pipeline, but a permanent skip meant adopting
# changed nothing until the api process restarted -- the fix was applied and the
# VMS kept ignoring the camera (observed 2026-09-25). A cooldown makes the
# remediation take effect on its own, at the cost of one retry every few
# minutes for a camera that really is hand-maintained.
_UNOWNED_RETRY_S = 300.0
_unowned: dict[str, float] = {}
_boot_id: Optional[str] = None


def _skip_unowned(slug: str) -> bool:
    """Whether to skip this slug without asking the pipeline again."""
    last = _unowned.get(slug)
    if last is None:
        return False
    if time.monotonic() - last < _UNOWNED_RETRY_S:
        return True
    # Cooldown expired: drop it so the next refusal logs again, and re-ask.
    _unowned.pop(slug, None)
    return False


# The administrator's address for the pipeline (Administration → DeepStream,
# table deepstream_settings), or None to use DEEPSTREAM_API_URL. Held here
# because _base() is sync; load_url() refreshes it from the database. Each api
# worker holds its own copy, which is why the admin routes reload it per
# request and the sync loop once per pass rather than trusting a save to have
# reached every process.
_url_override: Optional[str] = None


def _base() -> str:
    return (_url_override or settings.deepstream_api_url).rstrip("/")


def default_url() -> str:
    return settings.deepstream_api_url.rstrip("/")


async def load_url(db) -> str:
    """Refresh the pipeline address from the database; returns the one in use.

    A failed read keeps the last known address rather than snapping back to
    the environment default mid-run.
    """
    global _url_override
    try:
        row = await db.get(DeepstreamSettings, 1)
        _url_override = (row.api_url or None) if row else None
    except Exception as exc:  # noqa: BLE001 — never fail a caller over this
        log.warning("deepstream.settings.load_failed", error=str(exc))
    return _base()


def use_url(url: Optional[str]) -> None:
    """Apply a just-saved address in this process without waiting for a reload."""
    global _url_override
    _url_override = url or None


def _headers() -> dict[str, str]:
    # The pipeline enforces X-Internal-Key only once a key is configured on its
    # side (VMS.internal_api_key in its config_control_local.json, or
    # DEEPSTREAM_API_KEY). Sending it unconditionally is harmless when it is
    # not, and the same INTERNAL_API_KEY is what the pipeline is told to expect.
    key = (settings.internal_api_key or "").strip()
    return {"X-Internal-Key": key} if key else {}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT, headers=_headers())
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def should_project(camera: Camera) -> bool:
    """Whether this camera belongs on the pipeline.

    Registered, enabled, and with something to analyse: a region or activity
    drawn, or — DeepStream being the engine — Smart Search indexing to detect
    for (services/detection_engine.py). The ``GET /analytics/deepstream``
    endpoints use the same predicate, so pull and push always agree.
    """
    from .detection_engine import deepstream_wants

    return deepstream_wants(camera)


def _envelope(camera: Camera) -> dict:
    env = deepstream_config(camera)          # normalized coords; pipeline scales
    env["_managed_by"] = _MARKER
    return env


# ── Primitive operations ──────────────────────────────────────────────────────

async def apply_camera(camera: Camera) -> CallResult:
    """Push `camera`'s envelope: modify first, add on 404."""
    slug = camera.slug or ""
    if not slug:
        return CallResult(False)
    if _skip_unowned(slug):
        return CallResult(False)
    body = _envelope(camera)
    try:
        resp = await _get_client().post(f"{_base()}/camera/modify", json=body)
        if resp.status_code == 200:
            return _applied("modified", slug, resp)
        if resp.status_code == 409 and _error(resp) == "unowned_config":
            _note_unowned(slug, resp)
            return CallResult(False)
        if resp.status_code != 404:
            log.warning("deepstream.apply.modify_failed", slug=slug,
                        status=resp.status_code, detail=resp.text[:200])
            return CallResult(False)

        # Not registered yet — first time we have seen this camera, or the
        # pipeline restarted and this is the reconcile pass adding it back.
        resp = await _get_client().post(f"{_base()}/camera/add", json=body)
        if resp.status_code == 200:
            return _applied("added", slug, resp)
        if resp.status_code == 409 and _error(resp) == "unowned_config":
            _note_unowned(slug, resp)
            return CallResult(False)
        log.warning("deepstream.apply.add_failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        # Expected whenever the pipeline is restarting or down; the periodic
        # sweep re-pushes.
        log.warning("deepstream.apply.unreachable", slug=slug, error=str(exc))
    return CallResult(False)


async def remove_camera(slug: str) -> CallResult:
    """Detach `slug` from the pipeline and delete its config there.
    404 (already gone) counts as success."""
    if not slug:
        return CallResult(False)
    try:
        resp = await _get_client().post(f"{_base()}/camera/remove",
                                        json={"sensor_id": slug})
        if resp.status_code in (200, 404):
            restarting = _restart_scheduled(resp)
            # 404 = the pipeline never had it (e.g. a camera created without
            # zones inverting to a removal): nothing happened, so not worth
            # an info line every time.
            (log.debug if resp.status_code == 404 else log.info)(
                "deepstream.remove.ok", slug=slug,
                missing=resp.status_code == 404, restart=restarting)
            _unowned.pop(slug, None)
            return CallResult(True, restarting)
        if resp.status_code == 409 and _error(resp) == "unowned_config":
            # ALWAYS logged, unlike the apply path, which lets _note_unowned
            # de-duplicate so a 60 s sweep does not repeat itself. A removal is
            # not a sweep: it happens once, because an operator deleted the
            # camera, and the pipeline has just refused to let go of it. When
            # this was folded into the same de-duplicated warning, deleting a
            # camera whose slug had already been refused produced NO log line
            # at all -- the operator saw "delete_camera.ok" while the pipeline
            # kept running the camera and reloaded it on every restart
            # (observed 2026-09-25, cam3-cv88).
            log.warning("deepstream.remove.unowned", slug=slug,
                        detail=resp.text[:200],
                        note="the camera is deleted here but STILL RUNNING on "
                             "the pipeline, and its config will be reloaded on "
                             "the next pipeline restart",
                        action="POST /camera/adopt on the pipeline, then delete "
                               "again -- or remove the config file by hand")
            _note_unowned(slug, resp)
            return CallResult(False)
        log.warning("deepstream.remove.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("deepstream.remove.unreachable", slug=slug, error=str(exc))
    return CallResult(False)


def _applied(verb: str, slug: str, resp: httpx.Response) -> CallResult:
    restarting = _restart_scheduled(resp)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    action = data.get("action", verb)
    if action != "unchanged":
        log.info(f"deepstream.apply.{verb}", slug=slug, action=action,
                 pad_index=data.get("pad_index"), hot=data.get("hot"),
                 restart=restarting,
                 unsupported=data.get("unsupported_activities") or None)
    return CallResult(True, restarting)


def _error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", ""))
    except ValueError:
        return ""


def _note_unowned(slug: str, resp: httpx.Response) -> None:
    if slug not in _unowned:
        log.warning("deepstream.apply.unowned", slug=slug, detail=resp.text[:200],
                    action="POST /camera/adopt on the pipeline, or rename the camera",
                    retry_in_seconds=_UNOWNED_RETRY_S)
    _unowned[slug] = time.monotonic()


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


class LiveCamera(NamedTuple):
    url: str
    managed_by: Optional[str]


async def list_cameras() -> Optional[dict[str, LiveCamera]]:
    """sensor_id → (stream URL, owner) of the cameras live on the pipeline,
    or None if the pipeline is down.

    None and `{}` mean different things and must not be collapsed: an empty
    dict is a live pipeline with nothing loaded (reconcile should push
    everything), None is no answer at all (reconcile must do nothing).
    """
    global _boot_id
    try:
        resp = await _get_client().get(f"{_base()}/camera/list")
        resp.raise_for_status()
        data = resp.json()
        boot = data.get("boot_id")
        if boot and boot != _boot_id:
            if _boot_id is not None:
                log.info("deepstream.restarted", boot_id=boot)
            _boot_id = boot
            _unowned.clear()   # a restart may have changed what is on its disk
        return {
            c["sensor_id"]: LiveCamera(c.get("camera_url") or c.get("url") or "",
                                       c.get("managed_by"))
            for c in data.get("cameras", [])
            if c.get("sensor_id")
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        log.warning("deepstream.list.unreachable", error=str(exc))
        return None


# ── Request-path hooks ────────────────────────────────────────────────────────

async def sync_camera(camera: Camera, reason: str) -> None:
    """Push one camera's current state. Called by create / update / analytics
    edit. Never raises: the operator's save already succeeded."""
    if not settings.deepstream_sync_enabled:
        return
    try:
        if should_project(camera):
            await apply_camera(camera)
        elif camera.slug:
            # Disabled, or its last activity was cleared: disabling must
            # actually stop analysis, so this inverts to a removal.
            await remove_camera(camera.slug)
    except Exception as exc:  # noqa: BLE001 — best-effort hook
        log.warning("deepstream.sync.failed", reason=reason, error=str(exc))


async def sync_removal(slug: str, reason: str) -> None:
    """Called by delete, after the row is gone."""
    if not settings.deepstream_sync_enabled:
        return
    try:
        await remove_camera(slug)
    except Exception as exc:  # noqa: BLE001
        log.warning("deepstream.sync.failed", reason=reason, error=str(exc))


# ── Orchestration ─────────────────────────────────────────────────────────────

async def sync_now(reason: str) -> None:
    """Full sweep: push every camera that belongs on the pipeline, then
    reconcile what the pipeline reports against the registry.

    Used at startup and by the periodic loop. Never raises.
    """
    if not settings.deepstream_sync_enabled:
        return
    from sqlalchemy import select

    from ..db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await load_url(db)
            cameras = (await db.execute(
                select(Camera).where(Camera.stage == CameraStage.REGISTERED.value)
            )).scalars().all()
    except Exception as exc:  # noqa: BLE001
        log.warning("deepstream.sync.db_failed", reason=reason, error=str(exc))
        return

    desired = [c for c in cameras if should_project(c) and c.slug]
    for camera in desired:
        if (await apply_camera(camera)).restarting:
            # An apply can restart the pipeline when it changes the entry/exit
            # lines nvdsanalytics only reads at startup. Everything else is
            # reloaded from its disk on the way back up.
            log.info("deepstream.sync.deferred_to_restart", reason=reason)
            return

    await reconcile({c.slug: settings.local_rtsp_url(c.slug) for c in desired},
                    {c.slug for c in cameras if c.slug})


async def reconcile(desired: dict[str, str], registry_slugs: set[str]) -> None:
    """Repair drift between the registry and what the pipeline reports.

    * A desired camera the pipeline lacks is pushed by ``sync_now`` already;
      here we only report what is still missing after that.
    * A camera on the pipeline that carries OUR marker but has no registry row
      any more is retired: it was deleted while the pipeline was down (the
      remove was lost) and would otherwise be resurrected at every pipeline
      boot forever. Three guards: an empty registry is far likelier to be a
      failed query than an empty site, so it never triggers removals; a
      camera without our marker is hand-authored and untouchable; and the
      test is "has ANY registry row", not ``should_project`` — a disabled
      camera is removed by its own hook, and inferring from the predicate
      here would fight that hook.
    * A URL that differs (SERVER_IP / RTSP_PORT changed) is pushed as a
      modify; the pipeline hot-replaces the source.
    """
    current = await list_cameras()
    if current is None:
        return  # pipeline down; next pass retries

    missing = sorted(set(desired) - set(current))
    if missing:
        log.warning("deepstream.reconcile.missing", slugs=missing,
                    note="push refused or pipeline at capacity; see apply logs")

    if registry_slugs:
        for sensor_id, live in sorted(current.items()):
            if live.managed_by != _MARKER or sensor_id in registry_slugs:
                continue
            log.info("deepstream.reconcile.retire_orphan", slug=sensor_id)
            if (await remove_camera(sensor_id)).restarting:
                return

    for slug, url in sorted(desired.items()):
        live = current.get(slug)
        if live and live.url and live.url != url:
            log.warning("deepstream.reconcile.url_drift", slug=slug,
                        pipeline_url=live.url, expected_url=url,
                        note="next apply hot-replaces the source")


async def reconcile_loop() -> None:
    """Background task: re-asserts the registry against the pipeline every
    DEEPSTREAM_SYNC_INTERVAL seconds. This is what makes every hook in the
    request path safe to be best-effort, and what re-adds cameras after a
    pipeline restart or a push that was lost while it was down.
    """
    interval = settings.deepstream_sync_interval
    log.info("deepstream.sync.loop_started", interval=interval, api=_base())
    while True:
        try:
            await sync_now("periodic")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive anything
            log.warning("deepstream.sync.loop_error", error=str(exc))
        await asyncio.sleep(interval)


# ── Detector model management (Administration → DeepStream) ──────────────────
#
# Operator-driven, not part of the sync loop: an administrator uploads an ONNX +
# labels to the pipeline, lists what it holds, or restarts it to load one. These
# return the pipeline's own answer untouched so its error text reaches the
# screen; only "could not reach it at all" is turned into an exception.

class PipelineUnreachable(Exception):
    pass


# An ONNX can be hundreds of MB and the pipeline may parse it before answering.
_MODEL_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


async def _model_call(method: str, path: str, *, base: Optional[str] = None,
                      **kwargs) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=kwargs.pop("timeout", _TIMEOUT),
                                     headers=_headers()) as client:
            return await client.request(method, f"{(base or _base()).rstrip('/')}{path}", **kwargs)
    except httpx.HTTPError as exc:
        raise PipelineUnreachable(str(exc) or type(exc).__name__) from exc


async def probe(base: str) -> httpx.Response:
    """GET /health on `base` — used to test an address before saving it."""
    return await _model_call("GET", "/health", base=base,
                             timeout=httpx.Timeout(5.0, connect=3.0))


async def list_models() -> httpx.Response:
    return await _model_call("GET", "/model")


async def add_model(*, name: str, onnx: tuple[str, object], labels: tuple[str, object],
                    batch_size: int, network_size: int | None,
                    restart: bool) -> httpx.Response:
    data = {"name": name, "batch_size": str(batch_size),
            "restart": "true" if restart else "false"}
    if network_size is not None:     # omitted → the pipeline's own default
        data["network_size"] = str(network_size)
    return await _model_call(
        "POST", "/model/add",
        files={"onnx": (onnx[0], onnx[1], "application/octet-stream"),
               "labels": (labels[0], labels[1], "text/plain")},
        data=data, timeout=_MODEL_TIMEOUT)


async def restart_pipeline() -> httpx.Response:
    return await _model_call("POST", "/model/restart")
