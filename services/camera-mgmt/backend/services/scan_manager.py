"""scan_manager.py — orchestrates a network scan as one background job.

Descended from the discovery microservice's scan_manager, adapted for the
unified backend:

  • Job state lives in REDIS, not a module-level singleton — this API runs
    multiple uvicorn workers, so the worker that receives POST /scan is rarely
    the one that answers the next GET /scan poll. A SET-NX lock prevents
    overlapping scans across workers; progress counters use HINCRBY.
  • Devices are rows in the unified `cameras` table (stage-based lifecycle),
    not a separate discovered_devices table. Rows already at stage='registered'
    are the dedupe set: scanning an IP that belongs to a registered camera
    touches nothing.

`probe_device()` is the reusable unit: ONVIF interrogate → encrypt creds →
ffprobe each candidate. It's called both by a scan (with a default credential)
and by the per-device credential endpoint. Each device is probed concurrently
(bounded) and persisted in its *own* DB session, since AsyncSession is not safe
to share across concurrent tasks.
"""
from __future__ import annotations

import asyncio
import ipaddress
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import select, update

from .. import redis_client
from ..config import settings
from ..crypto import encrypt
from ..db import AsyncSessionLocal
from ..models import Camera, CameraStage
from ..urlutil import inject_credentials, to_rtsps
from . import netscan, onvif_probe, rtsp_verify

log = structlog.get_logger(__name__)

_LOCK_KEY = "discovery:scan:lock"
_JOB_KEY = "discovery:scan:job"
# Safety valve: a crashed worker's lock must not block scans forever.
_LOCK_TTL_SEC = 3600
_INT_FIELDS = {"total", "scanned"}
_BOOL_FIELDS = {"with_credentials", "auto"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScanAlreadyRunning(Exception):
    pass


# ─── Job state (Redis) ────────────────────────────────────────────────────────

async def current_job() -> Optional[dict[str, Any]]:
    r = await redis_client.get_redis()
    raw = await r.hgetall(_JOB_KEY)
    if not raw:
        return None
    job: dict[str, Any] = {}
    for k, v in raw.items():
        if k in _INT_FIELDS:
            job[k] = int(v)
        elif k in _BOOL_FIELDS:
            job[k] = v == "1"
        else:
            job[k] = v or None
    return job


async def _job_set(**fields: Any) -> None:
    r = await redis_client.get_redis()
    mapping = {}
    for k, v in fields.items():
        if isinstance(v, bool):
            mapping[k] = "1" if v else "0"
        else:
            mapping[k] = "" if v is None else str(v)
    await r.hset(_JOB_KEY, mapping=mapping)


async def start_scan(
    cidr: Optional[str], username: Optional[str], password: Optional[str]
) -> dict[str, Any]:
    # No CIDR → zero-config: WS-Discovery + a sweep of the appliance's own
    # subnet, derived in netscan.discover (nothing to validate here).
    if cidr:
        ipaddress.ip_network(cidr, strict=False)  # raises ValueError on bad CIDR

    r = await redis_client.get_redis()
    acquired = await r.set(_LOCK_KEY, "1", ex=_LOCK_TTL_SEC, nx=True)
    if not acquired:
        raise ScanAlreadyRunning()

    await r.delete(_JOB_KEY)
    await _job_set(
        id=_utcnow().strftime("%Y%m%d%H%M%S"),
        cidr=cidr or "auto (local subnet)",
        auto=not cidr,
        status="running",
        phase="discovering",
        total=0,
        scanned=0,
        with_credentials=bool(username and password),
        error=None,
        started_at=_utcnow().isoformat(),
        finished_at=None,
    )
    asyncio.create_task(_run(cidr, username, password))
    return await current_job() or {}


# ─── Device probing (reusable) ────────────────────────────────────────────────

def _onvif_ports_for(camera: Camera) -> list[int]:
    ports = [p for p in (camera.open_ports or []) if p in settings.discovery_onvif_ports]
    if camera.onvif_port and camera.onvif_port in ports:
        ports.remove(camera.onvif_port)
        ports.insert(0, camera.onvif_port)
    # The TCP sweep can miss ONVIF ports entirely (filtered ports; Docker
    # Desktop's NAT swallows refusals — see VMS-27). An explicit credentialed
    # probe must still get a chance, or such devices are stuck at no_onvif
    # forever: fall back to trying every configured ONVIF port.
    return ports or list(settings.discovery_onvif_ports)


async def probe_device(db, camera: Camera, user: str, password: str) -> Camera:
    """ONVIF-probe a staging camera row with the given creds, verify streams, persist.

    Never call this on a registered row — it rewrites the lifecycle stage.
    """
    camera.stage = CameraStage.PROBING.value
    camera.last_scanned_at = _utcnow()
    await db.commit()

    ports = _onvif_ports_for(camera)
    res = await onvif_probe.probe(camera.ip, ports, user, password)

    if res["status"] == "ok":
        camera.vendor = res.get("vendor")
        camera.model = res.get("model")
        camera.firmware = res.get("firmware")
        camera.onvif_port = res.get("onvif_port") or camera.onvif_port
        camera.enc_username = encrypt(user)
        camera.enc_password = encrypt(password)

        candidates = res.get("profiles", [])
        any_ok = False
        for cand in candidates:
            full = inject_credentials(cand["url_raw"], user, password)
            ok, working = await rtsp_verify.resolve_rtsp(full)
            cand["verified"] = ok
            # A TLS-only camera answers nothing on plain RTSP but still hands
            # out rtsp:// URIs over ONVIF. Keep the scheme that actually
            # played, or every later use of this candidate — promote, verify,
            # relay — inherits a URL the device refuses.
            if ok and working != full:
                cand["url_raw"] = to_rtsps(cand["url_raw"]) or cand["url_raw"]
            any_ok = any_ok or ok
        camera.rtsp_candidates = candidates
        camera.stage = CameraStage.VERIFIED.value
        camera.discovery_error = None if any_ok else "ONVIF OK but no RTSP stream confirmed"
    elif res["status"] == "auth_failed":
        camera.stage = CameraStage.AUTH_FAILED.value
        camera.onvif_port = res.get("onvif_port") or camera.onvif_port
        camera.discovery_error = res.get("error")
    else:
        camera.stage = CameraStage.NO_ONVIF.value
        camera.discovery_error = res.get("error")

    await db.commit()
    await db.refresh(camera)
    return camera


async def _get_or_create_staging(db, ip: str, open_ports: list[int]) -> Optional[Camera]:
    """Return the staging row for `ip`, creating one if needed.

    Returns None when the IP already belongs to a REGISTERED camera — natural
    rescan dedupe; the registered row itself shows up in the device list.
    """
    rows = (
        (await db.execute(select(Camera).where(Camera.ip == ip))).scalars().all()
    )
    if any(c.stage == CameraStage.REGISTERED.value for c in rows):
        return None
    staging = next(
        (c for c in rows if c.stage != CameraStage.REGISTERED.value), None
    )
    if staging is None:
        # Staging rows must never be relayed/recorded/health-checked.
        staging = Camera(
            ip=ip,
            open_ports=open_ports,
            stage=CameraStage.DISCOVERED.value,
            enabled=False,
        )
        db.add(staging)
        await db.flush()
    else:
        staging.open_ports = open_ports
    return staging


# ─── Scan orchestration ───────────────────────────────────────────────────────

async def _known_camera_subnets() -> list[str]:
    """The /24 around every private camera IP the registry has ever seen
    (registered or staged from a previous scan). Feeds zero-config discovery:
    once any camera has been found — by an earlier auto scan, a manual range,
    or a manual IP add — future auto scans sweep its network without the
    operator configuring anything. This is also what makes auto scans work
    where interface derivation can't see the camera LAN (macOS Docker/OrbStack
    VMs on a virtual subnet)."""
    async with AsyncSessionLocal() as db:
        ips = (
            await db.execute(select(Camera.ip).where(Camera.ip.is_not(None)).distinct())
        ).scalars().all()
    nets: set[str] = set()
    for ip in ips:
        try:
            if not ipaddress.ip_address(ip).is_private:
                continue
            nets.add(str(ipaddress.ip_network(f"{ip}/24", strict=False)))
        except ValueError:
            continue
    return sorted(nets)


async def _run(cidr: Optional[str], username: Optional[str], password: Optional[str]) -> None:
    r = await redis_client.get_redis()
    try:
        extra = None
        if not cidr:
            try:
                extra = await _known_camera_subnets()
            except Exception:  # noqa: BLE001 — a DB hiccup must not kill the scan
                log.warning("scan.known_subnets_failed")
        candidates = await netscan.discover(cidr, extra_subnets=extra)
        await _job_set(total=len(candidates), phase="probing")

        sem = asyncio.Semaphore(settings.discovery_probe_concurrency)

        async def handle(cand: netscan.Candidate) -> None:
            async with sem:
                try:
                    async with AsyncSessionLocal() as db:
                        camera = await _get_or_create_staging(db, cand.ip, cand.open_ports)
                        if camera is None:  # already a registered camera
                            # Stamp it so the current-scan device view still shows
                            # it as an "in relay" dedupe hint — the list is scoped
                            # by last_scanned_at, and registered rows would
                            # otherwise carry a stale (pre-scan) timestamp.
                            await db.execute(
                                update(Camera)
                                .where(
                                    Camera.ip == cand.ip,
                                    Camera.stage == CameraStage.REGISTERED.value,
                                )
                                .values(last_scanned_at=_utcnow())
                            )
                            await db.commit()
                            return
                        camera.rtsp_port = (
                            settings.discovery_rtsp_port if cand.rtsp_open else None
                        )
                        if cand.onvif_ports and not camera.onvif_port:
                            camera.onvif_port = cand.onvif_ports[0]

                        if username and password and cand.onvif_ports:
                            await probe_device(db, camera, username, password)
                        else:
                            camera.stage = (
                                CameraStage.DISCOVERED.value
                                if cand.onvif_ports
                                else CameraStage.NO_ONVIF.value
                            )
                            camera.last_scanned_at = _utcnow()
                            await db.commit()
                except Exception as exc:  # noqa: BLE001 — one bad host shouldn't kill the scan
                    log.warning("scan.handle.error", ip=cand.ip, error=str(exc))
                finally:
                    await r.hincrby(_JOB_KEY, "scanned", 1)

        await asyncio.gather(*[handle(c) for c in candidates])

        await _job_set(phase="done", status="done", finished_at=_utcnow().isoformat())
        log.info("scan.done", cidr=cidr, candidates=len(candidates))
    except Exception as exc:  # noqa: BLE001
        await _job_set(status="error", error=str(exc), finished_at=_utcnow().isoformat())
        log.error("scan.failed", error=str(exc))
    finally:
        await r.delete(_LOCK_KEY)
