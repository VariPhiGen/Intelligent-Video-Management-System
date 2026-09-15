"""
main.py — FastAPI application entrypoint

Lifespan:
  1. Connect to Redis and verify PostgreSQL connectivity
  2. Re-register all enabled cameras from PostgreSQL into MediaMTX
     (MediaMTX runtime config is in-memory and lost on restart)
  3. Start the background health monitor task
  4. Yield (application runs)
  5. On shutdown: cancel monitor, close connections
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

import backend.redis_client as redis_client
from backend import extensions
from backend.config import settings
from backend.db import close_db, engine
from backend.models import (
    SUB_TRACK_SUFFIX,
    Camera,
    CameraStage,
    SystemHealthResponse,
)
from backend.routers.audit import router as audit_router
from backend.routers.auth import router as auth_router
from backend.routers.cameras import router as cameras_router
from backend.routers.discovery import router as discovery_router
from backend.routers.events import router as events_router
from backend.routers.hls import router as hls_router
from backend.routers.motion import router as motion_router
from backend.routers.nvr import router as nvr_router
from backend.routers.search import router as search_router
from backend.routers.peripherals import router as peripherals_router
from backend.routers.sitemaps import router as sitemaps_router
from backend.security import require_authenticated
from backend.services.policy import require_capability
from backend.services import deepstream_client
from backend.services import health as health_svc
from backend.services import ai_events
from backend.services import analytics_client
from backend.services import frames_client
from backend.services import motion_client
from backend.services import smartsearch_sync
from backend.services import leader
from backend.services import nvr_client
from backend.services import relay

# ── Logging setup ─────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(message)s",
)

log = structlog.get_logger(__name__)

# ── Background task references ────────────────────────────────────────────────
_monitor_task: asyncio.Task | None = None
_relay_sync_task: asyncio.Task | None = None
_nvr_sync_task: asyncio.Task | None = None
_motion_sync_task: asyncio.Task | None = None
_frames_sync_task: asyncio.Task | None = None
_analytics_sync_task: asyncio.Task | None = None
_events_retention_task: asyncio.Task | None = None
_smartsearch_sync_task: asyncio.Task | None = None
_gpu_task: asyncio.Task | None = None
_deepstream_projection_task: asyncio.Task | None = None
_audit_kc_task: asyncio.Task | None = None


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _monitor_task

    log.info("startup.begin", server_ip=settings.server_ip, rtsp_port=settings.rtsp_port)

    # 0. Refuse to boot on shipped/insecure secret defaults.
    #    dev_auth=True is an explicit dev posture (it already makes every caller a
    #    full admin), so the guard applies only when dev_auth is off — i.e. the
    #    operator has declared production intent. Fail-fast, before any I/O, so a
    #    box where a guessed X-Internal-Key is full admin, camera creds are
    #    encrypted under a public key, or Keycloak admin is "admin" cannot start.
    #    Names only — the secret values are never logged.
    if not settings.dev_auth:
        insecure = settings.insecure_default_secrets()
        if insecure:
            log.error("startup.insecure_secret_defaults", settings=insecure)
            raise RuntimeError(
                "Refusing to start: these secrets are still at an insecure shipped "
                "default (" + ", ".join(insecure) + "). Set them to real values "
                "(run scripts/gen-secrets.sh and set DISCOVERY_SECRET_KEY manually), "
                "or set DEV_AUTH=true for a local dev box."
            )

    # 1. Verify PostgreSQL connectivity
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        log.info("startup.postgres_ok")
    except Exception as exc:
        log.error("startup.postgres_unreachable", error=str(exc))
        raise RuntimeError("Cannot connect to PostgreSQL — refusing to start") from exc

    # 2. Verify Redis connectivity
    try:
        r = await redis_client.get_redis()
        await r.ping()
        log.info("startup.redis_ok")
    except Exception as exc:
        log.error("startup.redis_unreachable", error=str(exc))
        raise RuntimeError("Cannot connect to Redis — refusing to start") from exc

    # 3. Re-register cameras from PostgreSQL into MediaMTX.
    # Seed the re-stamp flags first: _path_config reads them to choose each
    # camera's path shape, so doing this after the bulk register would rebuild
    # every affected camera as a direct pull and leave it unwatchable until the
    # detector earned the flag back.
    try:
        await health_svc.restore_restamp_flags()
    except Exception as exc:
        log.warning("startup.restamp_restore_failed", error=str(exc))

    try:
        from backend.db import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Camera).where(
                    Camera.enabled.is_(True),
                    Camera.stage == CameraStage.REGISTERED.value,
                )
            )
            cameras = result.scalars().all()

        if cameras:
            await relay.register_cameras_bulk([
                {"slug": c.slug, "rtsp_url": c.rtsp_url, "sub_track": c.sub_track}
                for c in cameras
            ])
            log.info("startup.cameras_registered", count=len(cameras))
        else:
            log.info("startup.no_cameras_to_register")
    except Exception as exc:
        log.warning("startup.camera_registration_failed", error=str(exc))
        # Non-fatal: health monitor will retry on next poll

    # 4. Reset stale statuses, then start the background health monitor.
    # The unknown-mark writes uptime-history events so the span since the last
    # shutdown is recorded as "no data" instead of the last pre-restart state.
    # Crash-aware: after an ungraceful death (power cut / OOM / GPU hang) the
    # events are backdated to the last monitor heartbeat, so the dead window
    # doesn't count as camera uptime. With crash_gap_marks_down (default on),
    # cameras that were live over that gap are recorded as Down rather than
    # No data — see startup_mark_unknown / mark_all_unknown.
    await health_svc.startup_mark_unknown()
    _monitor_task = asyncio.create_task(leader.run_as_leader("health_monitor", health_svc.monitor_loop), name="health_monitor")
    log.info("startup.health_monitor_started")

    # 4b. Registry→MediaMTX path sync (periodic reconcile; heals MediaMTX
    # restarts, which drop every in-memory path at once — startup registration
    # above only fires once, so a long-lived API needs this to re-add them).
    global _relay_sync_task
    if settings.mediamtx_sync_enabled:
        _relay_sync_task = asyncio.create_task(
            leader.run_as_leader("relay_sync", relay.reconcile_loop), name="relay_sync"
        )
        log.info("startup.relay_sync_started")

    # 5. Registry→NVR recording sync (periodic reconcile; heals NVR restarts)
    global _nvr_sync_task
    if settings.nvr_sync_enabled:
        _nvr_sync_task = asyncio.create_task(
            leader.run_as_leader("nvr_sync", nvr_client.reconcile_loop), name="nvr_sync"
        )
        log.info("startup.nvr_sync_started")

    # 6. Registry→motion-service sync (same pattern; heals motion restarts)
    global _motion_sync_task
    if settings.motion_sync_enabled:
        _motion_sync_task = asyncio.create_task(
            leader.run_as_leader("motion_sync", motion_client.reconcile_loop), name="motion_sync"
        )
        log.info("startup.motion_sync_started")

    # 6a. Registry→frame-broker sync. Skipped unless a broker URL is set,
    # which only docker-compose.broker.yml does. Without this the broker sits
    # at zero cameras and both its consumers wait on notices that never come.
    global _frames_sync_task
    if settings.frames_sync_enabled and frames_client.is_configured():
        _frames_sync_task = asyncio.create_task(
            leader.run_as_leader("frames_sync", frames_client.reconcile_loop), name="frames_sync"
        )
        log.info("startup.frames_sync_started", broker=settings.frames_api_url)

    # 6a2. Registry→analytics sync. Skipped unless an analytics URL is set,
    # which only the analytics profile's overlay does. Same desired set as
    # Smart Search's own: detection exists to feed the index, so a camera the
    # index does not want must not cost a detector pass.
    global _analytics_sync_task
    if settings.analytics_sync_enabled and analytics_client.is_configured():
        _analytics_sync_task = asyncio.create_task(
            leader.run_as_leader("analytics_sync", analytics_client.reconcile_loop),
            name="analytics_sync")
        log.info("startup.analytics_sync_started", analytics=settings.analytics_api_url)

    # 6b. Registry→Smart Search index sync. Skipped entirely when no index URL
    # is set, which is the default: most deployments feed no index.
    global _smartsearch_sync_task
    if settings.smartsearch_sync_enabled and smartsearch_sync.is_configured():
        _smartsearch_sync_task = asyncio.create_task(
            leader.run_as_leader("smartsearch_sync", smartsearch_sync.reconcile_loop), name="smartsearch_sync"
        )
        log.info("startup.smartsearch_sync_started", index=settings.smartsearch_index_url)

    # 6c. AI activity events: drop rows past their own expires_at (stamped from
    # the camera's footage retention at insert), so no event outlives the
    # footage its Open Playback link points at.
    global _events_retention_task
    _events_retention_task = asyncio.create_task(
        leader.run_as_leader("events_retention", ai_events.retention_loop),
        name="events_retention")
    log.info("startup.events_retention_started")

    # 7. GPU metric sampler (topbar gauge). Off the request path by design —
    # see _gpu_sampler. Sampled once here so /health is populated immediately
    # rather than reporting "no GPU" for the first interval after a restart.
    global _gpu_task
    _gpu_state.update(await asyncio.to_thread(_sample_gpu))
    _gpu_task = asyncio.create_task(_gpu_sampler(), name="gpu_sampler")
    log.info("startup.gpu_sampler_started",
             present=_gpu_state["present"], count=_gpu_state["count"])

    # 8. Registry→DeepStream sync: project every camera's config to the shared
    # directory, then push the changes into the running pipeline. The sweep
    # runs inline here rather than only as the loop's first tick because the
    # pipeline may be booting alongside us and reads that directory exactly
    # once, at its own startup — the files need to be right before then.
    global _deepstream_projection_task
    if settings.deepstream_projection_enabled:
        await deepstream_client.sync_now("startup")
        _deepstream_projection_task = asyncio.create_task(
            leader.run_as_leader("deepstream_sync", deepstream_client.reconcile_loop), name="deepstream_sync"
        )
        log.info("startup.deepstream_sync_started",
                 dir=settings.deepstream_config_dir, api=settings.deepstream_api_url)

    # 9. Audit: poll Keycloak for auth events (failed logins, lockouts, logouts)
    # that never reach our handlers, and enable event storage on the realm.
    global _audit_kc_task
    if settings.audit_kc_events_enabled:
        from backend.services import audit as audit_svc
        _audit_kc_task = asyncio.create_task(
            leader.run_as_leader("audit_kc_events", audit_svc.keycloak_events_loop), name="audit_kc_events"
        )
        log.info("startup.audit_kc_events_started")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    log.info("shutdown.begin")

    for task in (_monitor_task, _relay_sync_task, _nvr_sync_task,
                 _motion_sync_task, _gpu_task,
                 _deepstream_projection_task, _audit_kc_task,
                 _smartsearch_sync_task, _frames_sync_task,
                 _analytics_sync_task, _events_retention_task):
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    # Close the uptime history cleanly: cameras are unmonitored from here on.
    # Runs AFTER the monitor is cancelled so no straggling poll overwrites it.
    await health_svc.mark_all_unknown("shutdown")

    await relay.close_client()
    await nvr_client.close_client()
    await motion_client.close_client()
    await frames_client.close_client()
    await analytics_client.close_client()
    await smartsearch_sync.close_client()
    await deepstream_client.close_client()
    from backend.services import keycloak_admin
    await keycloak_admin.close_client()
    await redis_client.close_redis()
    await close_db()

    log.info("shutdown.complete")


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="RTSP Relay Server",
    description="Manage and relay RTSP camera streams over LAN/WAN",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# The SPA is served same-origin by this app, so no CORS is needed by default.
# CORS_ORIGINS may list explicit cross-origin callers (comma-separated). Wildcard
# origins are intentionally not used in production.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ── Routers ───────────────────────────────────────────────────────────────────

# /auth/config is public; /me guards itself via its dependency.
app.include_router(auth_router, prefix="/api")
# Every camera endpoint requires an authenticated principal; mutating routes
# additionally require the 'admin' role (declared per-route in routers/cameras.py).
app.include_router(
    cameras_router, prefix="/api", dependencies=[Depends(require_authenticated)]
)
# Camera discovery (ONVIF scan / probe / promote) — native since the discovery
# microservice was folded into this backend. Onboarding cameras is admin-only.
app.include_router(
    discovery_router, prefix="/api/discovery", dependencies=[Depends(require_capability("camera_manage"))]
)
# Single-login gateway to the NVR (recording) service. Auth enforced inside the
# proxy per-method: GET for any authenticated user, mutations admin-only.
app.include_router(nvr_router, prefix="/api/nvr")
# Same-pattern gateway to the motion detection service.
app.include_router(motion_router, prefix="/api/motion")
# Smart Search. Same single-login gateway pattern, but this one also scopes the
# shared CLIP index to cameras this VMS records, enforces the `smart_search`
# capability, and audits every query — see routers/search.py.
app.include_router(
    search_router, prefix="/api/search", dependencies=[Depends(require_authenticated)]
)
# User and role administration is not mounted here: it is the identity
# extension (backend/extensions/identity), loaded below with the others.
# Enforcement stays core — see services/policy.py.
# Audit trail (append-only, hash-chained). Reads are admin/dpo-gated inside the
# router; /audit/ingest accepts events from trusted services via the internal key.
app.include_router(
    audit_router, prefix="/api", dependencies=[Depends(require_authenticated)]
)
# Sitemaps (Map tab floor plans). Reads for any authenticated principal;
# mutations self-gate on camera_manage inside the router.
app.include_router(
    sitemaps_router, prefix="/api", dependencies=[Depends(require_authenticated)]
)
# Peripherals inventory. Reads for any authenticated principal (the page is
# gated by the ui-only `peripherals` capability); writes self-gate on the
# grantable peripheral_manage capability inside the router.
app.include_router(
    peripherals_router, prefix="/api", dependencies=[Depends(require_authenticated)]
)
# AI activity events. POST is the pipeline's ingest and self-gates on the
# internal key; reads self-gate on the ai_analytics capability — see
# routers/events.py.
app.include_router(
    events_router, prefix="/api", dependencies=[Depends(require_authenticated)]
)
# Live-preview HLS relay → MediaMTX. Unauthenticated and streamed, mirroring the
# Caddy /hls/* handler, so a single-port front door (cloudflared) can serve the
# live preview without an extra hop. Must be registered before the "/" static
# mount below so /hls/* is matched here, not treated as an SPA asset.
app.include_router(hls_router, prefix="/hls")

# ── Optional extensions ───────────────────────────────────────────────────────
# Feature packages that are not part of the open core mount themselves here, if
# they are present in the image at all. Nothing above names one; this call is the
# single reference, and it names nothing either — see backend/extensions.
#
# Placed after every core router (so an extension cannot shadow a core route by
# accident) and before the "/" static mount (so its API routes are matched
# rather than served as SPA assets).
app.state.extensions = extensions.load(app)


# ── System health endpoint ────────────────────────────────────────────────────

@app.get("/health", response_model=SystemHealthResponse, tags=["system"])
async def system_health() -> SystemHealthResponse:
    from backend.db import AsyncSessionLocal
    from sqlalchemy import func

    # PostgreSQL
    pg_ok = False
    total_cameras = 0
    enabled_cameras = 0
    _registered = Camera.stage == CameraStage.REGISTERED.value
    try:
        async with AsyncSessionLocal() as db:
            total_row = await db.execute(
                select(func.count(Camera.id)).where(_registered)
            )
            enabled_row = await db.execute(
                select(func.count(Camera.id)).where(_registered, Camera.enabled.is_(True))
            )
            total_cameras = total_row.scalar() or 0
            enabled_cameras = enabled_row.scalar() or 0
        pg_ok = True
    except Exception:
        pass

    # Redis
    redis_ok = False
    try:
        r = await redis_client.get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass

    # MediaMTX
    mtx_ok = await relay.is_reachable()

    # Stream status counts from MediaMTX active paths.
    # Use the 'ready' field — 'source' is non-null even for offline paths
    # (it describes the configured source type, not live connectivity).
    #
    # Count CAMERAS, not paths. A camera with a sub track owns two relay paths
    # (`<slug>` and `<slug>_sub`), so counting paths made the header read
    # "STREAMS 9/8" and "-1 not streaming" the moment the first sub was enabled
    # — more streams than cameras, and a negative shortfall. The sub is an
    # implementation detail of one camera's recording, not a second stream an
    # operator is watching.
    connected = disconnected = unknown = 0
    if mtx_ok:
        paths = await relay.list_active_paths()
        for p in paths:
            if str(p.get("name") or "").endswith(SUB_TRACK_SUFFIX):
                continue
            if p.get("ready"):
                connected += 1
            else:
                disconnected += 1
    else:
        unknown = enabled_cameras

    overall = "ok" if (pg_ok and redis_ok and mtx_ok) else "degraded"

    return SystemHealthResponse(
        status=overall,
        total_cameras=total_cameras,
        enabled_cameras=enabled_cameras,
        connected_streams=connected,
        disconnected_streams=disconnected,
        unknown_streams=unknown,
        mediamtx_reachable=mtx_ok,
        postgres_reachable=pg_ok,
        redis_reachable=redis_ok,
        host=_host_metrics(),
    )


def _host_metrics() -> dict | None:
    """Appliance CPU/RAM for the System health node card — stdlib only.

    CPU is 1-min load average normalized by core count (no sampling window);
    RAM comes from /proc/meminfo (containers are Linux; absent elsewhere).
    """
    import socket

    try:
        cores = os.cpu_count() or 1
        load1 = os.getloadavg()[0]
        cpu_pct = min(100.0, round(load1 / cores * 100, 1))
        mem_total = mem_avail = None
        try:
            with open("/proc/meminfo") as fh:
                info = {l.split(":")[0]: int(l.split()[1]) for l in fh if ":" in l}
            mem_total = info.get("MemTotal")
            mem_avail = info.get("MemAvailable")
        except OSError:
            pass
        mem_pct = (
            round((1 - mem_avail / mem_total) * 100, 1)
            if mem_total and mem_avail is not None else None
        )
        return {
            "hostname": socket.gethostname(),
            "cores": cores,
            "load1": round(load1, 2),
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "mem_total_gb": round(mem_total / 1024 / 1024, 1) if mem_total else None,
            # Snapshot only — never sample the GPU from inside a request.
            "gpu": dict(_gpu_state),
            "gpu_pct": _gpu_state["pct"],   # flat alias, kept for older clients
        }
    except Exception:  # noqa: BLE001 — metrics must never break /health
        return None


# ── GPU sampling ──────────────────────────────────────────────────────────────
# nvidia-smi is a fork per sample, so it runs on a background timer and /health
# only ever reads the snapshot it leaves behind. Sampling inline (as this used
# to) stalls the event loop for the length of the subprocess — and every open
# browser tab polls /health every 10s, so one wedged driver would freeze the
# whole API for the full timeout on each cache miss. It went unnoticed only
# because the binary is absent in the container, which fails instantly.

GPU_SAMPLE_INTERVAL = 10.0

# present=False is a CPU-only appliance (hide the gauge); present=True with
# pct=None is a real fault — a GPU is there and we cannot read it (alert).
_gpu_state: dict[str, Any] = {"present": False, "count": 0, "pct": None, "devices": []}

# Per-device fields the System health card reads. VRAM matters more than the
# utilization gauge here: decode/inference dies on an out-of-memory long before
# the SM saturates, and NVDEC (utilization.decoder) is the real ceiling on how
# many camera streams this appliance can take.
_GPU_QUERY = (
    "index,name,utilization.gpu,memory.used,memory.total,"
    "utilization.decoder,utilization.encoder,temperature.gpu,power.draw,power.limit"
)


def _gpu_num(raw: str) -> float | None:
    """nvidia-smi prints '[N/A]' / '[Not Supported]' for fields a card lacks."""
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _sample_gpu() -> dict[str, Any]:
    """One nvidia-smi read. BLOCKING — only call via asyncio.to_thread."""
    import shutil
    import subprocess

    if shutil.which("nvidia-smi") is None:
        # No NVIDIA driver here at all: either a CPU-only box, or the container
        # was started without the GPU overlay (see docker-compose.gpu.yml).
        return {"present": False, "count": 0, "pct": None, "devices": []}
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        devices: list[dict[str, Any]] = []
        for line in out.stdout.splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) < 10 or not f[0].isdigit():
                continue
            mem_used, mem_total = _gpu_num(f[3]), _gpu_num(f[4])
            devices.append({
                "index": int(f[0]),
                "name": f[1],
                "pct": _gpu_num(f[2]),
                "mem_used_mb": mem_used,
                "mem_total_mb": mem_total,
                "mem_pct": round(mem_used / mem_total * 100, 1)
                           if mem_used is not None and mem_total else None,
                "decoder_pct": _gpu_num(f[5]),
                "encoder_pct": _gpu_num(f[6]),
                "temp_c": _gpu_num(f[7]),
                "power_w": _gpu_num(f[8]),
                "power_limit_w": _gpu_num(f[9]),
            })
        if out.returncode != 0 or not devices:
            return {"present": True, "count": 0, "pct": None, "devices": []}
        # Busiest GPU on multi-GPU boxes — a topbar gauge wants the worst case.
        utils = [d["pct"] for d in devices if d["pct"] is not None]
        return {
            "present": True,
            "count": len(devices),
            "pct": max(utils) if utils else None,
            "devices": devices,
        }
    except Exception:  # noqa: BLE001 — driver fault / timeout, but a GPU exists
        return {"present": True, "count": 0, "pct": None, "devices": []}


async def _gpu_sampler() -> None:
    """Refresh _gpu_state forever; never lets a sampling error kill the loop."""
    while True:
        try:
            _gpu_state.update(await asyncio.to_thread(_sample_gpu))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("gpu.sample_failed", error=str(exc))
        await asyncio.sleep(GPU_SAMPLE_INTERVAL)


# ── Static frontend ───────────────────────────────────────────────────────────
# Serve the product-wide UI straight from FastAPI so no separate nginx container
# is needed. Mounted LAST so /api/*, /health, /docs, /redoc keep priority;
# html=True serves index.html at "/" and as the fallback for client-side routes.
#
# The React app (frontend-react/, built by the Docker node stage into
# frontend_dist/) is the only UI. The legacy single-file SPA that was mounted at
# /legacy was removed on 2026-08-27 — the two things it still owned, the ONVIF
# device settings forms and CSV bulk import, had been ported to React
# (pages/config/device/, pages/wizard/CsvImport.tsx).
#
# There is deliberately no fallback UI now. A checkout without `npm run build`
# serves no SPA at all, which fails visibly rather than silently serving a
# second, unmaintained interface that nobody was keeping current.
_here = Path(__file__).resolve()


class _SpaStaticFiles(StaticFiles):
    """StaticFiles with the cache semantics a fingerprinted SPA build needs.

    Vite content-hashes everything under `assets/` (`index-B684wNcl.js`), so
    those files can be cached forever — the filename changes whenever the bytes
    do. `index.html` is the one file that is NOT fingerprinted: it is the
    pointer to the current bundle, so a cached copy pins the browser to the
    previous deploy's asset names even though the new ones are already on disk.

    Starlette sends only `etag` and `last-modified`, which leaves freshness to
    the browser's heuristics — and browsers routinely reuse an HTML document for
    minutes without asking. On 2026-08-20 a redeploy therefore looked like it
    had not landed until a hard refresh. `no-cache` does not mean "do not
    store"; it means "revalidate before use", which the existing ETag answers
    with a 304, so the cost is one conditional request per navigation.

    `vendor/` is deliberately left alone: those files are copied verbatim rather
    than hashed, so they keep plain revalidation.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):  # type: ignore[override]
        response = super().file_response(full_path, stat_result, scope, status_code)
        posix = Path(full_path).as_posix()
        if posix.endswith(".html"):
            response.headers["cache-control"] = "no-cache"
        elif "/assets/" in posix:
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


def _first_dir(candidates: list[Path]) -> Path | None:
    return next((p for p in candidates if p.is_dir()), None)


_react_dir = _first_dir([
    _here.parents[1] / "frontend_dist",                       # container: /app/frontend_dist
    *([_here.parents[3] / "frontend-react" / "dist"] if len(_here.parents) > 3 else []),
])

if _react_dir:
    app.mount("/", _SpaStaticFiles(directory=str(_react_dir), html=True), name="ui")
    log.info("startup.frontend_mounted", path=str(_react_dir))
else:
    log.warning("startup.frontend_dir_missing")
