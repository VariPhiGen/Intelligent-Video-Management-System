"""discovery.py — native camera-discovery endpoints (formerly a microservice proxy).

The discovery microservice has been folded into this backend: devices are rows
in the unified `cameras` table at a pre-registration stage, and "promote to
relay" is an in-process UPDATE on the same row (set name/slug/rtsp_url, stage →
registered) instead of an HTTP hop through /api/cameras.

Paths and JSON shapes are wire-compatible with the old proxied service, so the
SPA's discovery page is unchanged. The router is mounted in main.py under
/api/discovery gated on the 'camera_manage' capability (grantable under
Administration → Roles & permissions), so onboarding is delegable, not
admin-only.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..crypto import decrypt, encrypt
from ..db import get_db
from ..models import (
    AddBody,
    Camera,
    CameraStage,
    CredentialBody,
    DeviceOut,
    ManualAddBody,
    ScanBody,
    generate_slug,
    validate_lawful_basis,
)
from ..services import audit as audit_svc
from ..services import health as health_svc
from ..services import nvr_client, relay, rtsp_verify, scan_manager, substream
from ..security import get_principal
from ..urlutil import host_of, inject_credentials, split_credentials, to_rtsps
from .cameras import _ensure_unique_slug

log = structlog.get_logger(__name__)
router = APIRouter(tags=["discovery"])

_REGISTERED = CameraStage.REGISTERED.value


# ─── Config / scan ────────────────────────────────────────────────────────────

@router.get("/config")
async def config() -> dict:
    """Non-secret scan parameters for the SPA. (default_cidr is gone on
    purpose: prefilling a guessed range scoped scans to the wrong subnet —
    an empty range now runs the self-learning auto scan instead.)"""
    return {
        "onvif_ports": settings.discovery_onvif_ports,
        "rtsp_port": settings.discovery_rtsp_port,
    }


@router.post("/scan")
async def start_scan(body: ScanBody) -> dict:
    # A CIDR (or a bare IP as /32) runs a sweep; omitting it runs zero-config
    # WS-Discovery only — no IP range required.
    cidr = (body.cidr or "").strip() or None
    try:
        return await scan_manager.start_scan(cidr, body.username, body.password)
    except scan_manager.ScanAlreadyRunning:
        raise HTTPException(status_code=409, detail="A scan is already running")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid CIDR: {exc}")


@router.get("/scan")
async def scan_status() -> dict:
    job = await scan_manager.current_job()
    return job if job else {"status": "idle"}


# ─── Device list / helpers ────────────────────────────────────────────────────

def _out(c: Camera) -> DeviceOut:
    return DeviceOut.from_camera(c, decrypt(c.enc_username))


async def _get_or_404(device_id: uuid.UUID, db: AsyncSession) -> Camera:
    row = await db.execute(select(Camera).where(Camera.id == device_id))
    camera = row.scalar_one_or_none()
    if camera is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return camera


def _staging_or_409(camera: Camera, action: str) -> None:
    if camera.stage == _REGISTERED:
        raise HTTPException(
            status_code=409,
            detail=f"Camera is registered — {action} it from the Cameras page instead",
        )


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> list[DeviceOut]:
    # Scope to the CURRENT scan. Staging rows persist in the cameras table across
    # scans (nothing prunes them), so an unscoped list would resurface every
    # device any past scan ever found — the "why do I always see the same N
    # cameras" bug. A scan bumps last_scanned_at on every device it surfaces
    # (see scan_manager), so filtering to rows touched at/after the current job
    # started shows only what this scan found. No live job → nothing to show yet
    # (a manual RTSP add is registered directly and surfaced via its own POST).
    job = await scan_manager.current_job()
    started_at = job.get("started_at") if job else None
    if not started_at:
        return []
    since = datetime.fromisoformat(started_at)
    stmt = (
        select(Camera)
        .where(Camera.ip.is_not(None), Camera.last_scanned_at >= since)
        .order_by(Camera.ip)
    )
    if status:
        stage = _REGISTERED if status == "added" else status
        stmt = stmt.where(Camera.stage == stage)
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(c) for c in rows]


# ─── Per-device actions ───────────────────────────────────────────────────────

@router.put("/devices/{device_id}/credentials", response_model=DeviceOut)
async def set_credentials(
    device_id: uuid.UUID,
    body: CredentialBody,
    db: AsyncSession = Depends(get_db),
) -> DeviceOut:
    """Store new credentials and immediately re-probe the device with them."""
    camera = await _get_or_404(device_id, db)
    _staging_or_409(camera, "manage")
    camera = await scan_manager.probe_device(db, camera, body.username, body.password)
    return _out(camera)


@router.post("/devices/{device_id}/verify", response_model=DeviceOut)
async def verify(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> DeviceOut:
    """Re-run ffprobe on the stored RTSP candidates using stored credentials."""
    camera = await _get_or_404(device_id, db)
    _staging_or_409(camera, "verify")
    user = decrypt(camera.enc_username)
    password = decrypt(camera.enc_password)
    if not user or not camera.rtsp_candidates:
        raise HTTPException(status_code=409, detail="No credentials or RTSP candidates to verify")

    candidates = list(camera.rtsp_candidates)
    for cand in candidates:
        full = inject_credentials(cand["url_raw"], user, password)
        ok, working = await rtsp_verify.resolve_rtsp(full)
        cand["verified"] = ok
        # Persist a scheme correction (rtsp → rtsps) so re-verifying a TLS-only
        # camera repairs the stored URL rather than just reporting it playable.
        if ok and working != full:
            cand["url_raw"] = to_rtsps(cand["url_raw"]) or cand["url_raw"]
    camera.rtsp_candidates = candidates
    await db.commit()
    await db.refresh(camera)
    return _out(camera)


async def _register(
    camera: Camera,
    db: AsyncSession,
    *,
    request: Request | None = None,
    name: str,
    rtsp_url: str,
    enabled: bool,
    recording: bool = True,
    zone: str | None = None,
    metadata: dict | None = None,
    lawful_basis: str | None = None,
    purpose: str | None = None,
    notice_posted: bool = False,
) -> bool:
    """Promote a staging row to a registered camera in place.

    Returns False when the rtsp_url already belongs to another camera (the row
    is left at its current stage with an explanatory error). On success the
    relay path, NVR recording (only when `recording`), and a fast health check
    are kicked off exactly like POST /api/cameras does.
    """
    # DPDP: registration requires a valid lawful basis + purpose (422 otherwise).
    lawful_basis, purpose = validate_lawful_basis(lawful_basis, purpose)
    slug = generate_slug(name)
    try:
        await _ensure_unique_slug(slug, db)
        camera.name = name
        camera.slug = slug
        camera.rtsp_url = rtsp_url
        camera.enabled = enabled
        camera.recording = recording
        extra = {k: v for k, v in {**(metadata or {}), "zone": zone}.items() if v}
        if extra:
            camera.camera_metadata = {**(camera.camera_metadata or {}), **extra}
        camera.lawful_basis = lawful_basis
        camera.purpose = purpose
        camera.notice_posted = notice_posted
        camera.stage = _REGISTERED
        camera.discovery_error = None
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "ix_cameras_rtsp_url" not in str(exc.orig):
            raise
        camera.discovery_error = "Already registered in the relay"
        await db.commit()
        await db.refresh(camera)
        return False

    if enabled:
        try:
            await relay.add_path(slug, rtsp_url)
        except Exception as exc:  # noqa: BLE001 — health monitor re-registers on next poll
            log.warning("discovery.register.relay_failed", slug=slug, error=str(exc))
        if settings.nvr_sync_enabled and camera.recording:
            await nvr_client.add_camera(slug, masks=camera.privacy_masks)
        asyncio.create_task(health_svc.check_camera_soon(camera.id))
    # Find out what second stream this camera offers, in the background —
    # probing takes seconds and must not delay registration. Stores only; it
    # never starts recording on its own.
    asyncio.create_task(substream.resolve_in_background(camera.id))

    await db.refresh(camera)
    log.info("discovery.register.ok", ip=camera.ip, slug=slug)
    # AUDITED HERE, not in the two endpoints above, because this is the single
    # funnel both of them promote through — the ONVIF assign path and the
    # manual rtsp:// add. Registering a camera is the act that STARTS
    # collecting personal data, so on a DPDP appliance it has to leave the same
    # trace that POST /api/cameras leaves; it did not, and the wizard uses this
    # path, so in practice no camera add was ever audited.
    await audit_svc.record(
        request, await get_principal(request) if request else None,
        "camera.created", target=slug,
        detail={"name": name, "via": "discovery", "ip": camera.ip},
    )
    return True


@router.post("/devices/{device_id}/add", response_model=DeviceOut)
async def add_to_relay(
    device_id: uuid.UUID,
    body: AddBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DeviceOut:
    """Promote a verified device into the relay (in-process registration)."""
    camera = await _get_or_404(device_id, db)
    _staging_or_409(camera, "re-add")
    user = decrypt(camera.enc_username)
    password = decrypt(camera.enc_password)

    if not camera.rtsp_candidates:
        raise HTTPException(status_code=409, detail="Device has no RTSP URL — set credentials first")
    if body.profile_index < 0 or body.profile_index >= len(camera.rtsp_candidates):
        raise HTTPException(status_code=422, detail="profile_index out of range")

    candidates = list(camera.rtsp_candidates)
    cand = candidates[body.profile_index]
    full_url = inject_credentials(cand["url_raw"], user, password)
    name = body.name or _default_name(camera)

    # Confirm the stream at promotion time rather than trusting a scan result
    # that may predate a credential or firmware change. Two things ride on it:
    # the scheme can still be corrected here (a TLS-only camera advertises
    # rtsp:// over ONVIF), and a URL that will not play must not be enabled —
    # the health monitor would force-reconnect that path forever, which reads
    # in the UI as a camera that flaps instead of one that never connected.
    playable: Optional[bool] = None
    if body.verify:
        playable, full_url = await rtsp_verify.resolve_rtsp(full_url)
        if playable and full_url.startswith("rtsps://"):
            cand["url_raw"] = to_rtsps(cand["url_raw"]) or cand["url_raw"]
        cand["verified"] = playable
        camera.rtsp_candidates = candidates
        await db.commit()

    registered = await _register(
        camera, db, request=request, name=name, rtsp_url=full_url,
        enabled=body.enabled and playable is not False,
        recording=body.recording, zone=body.zone,
        metadata=body.metadata,
        lawful_basis=body.lawful_basis, purpose=body.purpose,
        notice_posted=body.notice_posted,
    )
    if registered and playable is False:
        camera.discovery_error = (
            "Added but left disabled — no playable stream at this URL. "
            "Fix the credentials or profile, then enable it."
        )
        await db.commit()
        await db.refresh(camera)
    return _out(camera)


@router.post("/devices/manual", response_model=DeviceOut)
async def add_manual(
    body: ManualAddBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DeviceOut:
    """Add a camera by raw RTSP URL — for non-ONVIF or undiscovered devices.

    Bypasses ONVIF entirely: stores the credential-less URL as a single profile,
    optionally ffprobe-verifies it, then registers it in place. Creds (inline or
    supplied) are encrypted at rest; only the clean URL lives in rtsp_candidates.
    """
    clean_url, embedded_user, embedded_pass = split_credentials(body.rtsp_url)
    host = host_of(body.rtsp_url)
    if not host:
        raise HTTPException(status_code=422, detail="Could not parse a host from the RTSP URL")

    user = body.username or embedded_user
    password = body.password or embedded_pass
    full_url = inject_credentials(clean_url, user, password)

    # resolve_rtsp, not verify_rtsp: a hand-typed rtsp:// URL for a TLS-only
    # camera gets its scheme corrected here too, so a manual add of such a
    # device lands playable instead of registering a URL that never connects.
    verified: Optional[bool] = None
    if body.verify:
        verified, full_url = await rtsp_verify.resolve_rtsp(full_url)
        if verified and full_url.startswith("rtsps://"):
            clean_url = to_rtsps(clean_url) or clean_url

    # Reuse an existing staging row for this host, else start a fresh one. A
    # registered camera on the same host is fine — multi-channel devices put
    # several cameras on one IP, so a new row is created alongside it.
    rows = (await db.execute(select(Camera).where(Camera.ip == host))).scalars().all()
    camera = next((c for c in rows if c.stage != _REGISTERED), None)
    if camera is None:
        camera = Camera(
            ip=host,
            open_ports=[],
            stage=CameraStage.DISCOVERED.value,
            enabled=False,
        )
        db.add(camera)
        await db.flush()

    camera.vendor = camera.vendor or "Manual"
    camera.rtsp_port = urlparse(clean_url).port or camera.rtsp_port
    camera.rtsp_candidates = [
        {"profile": "manual", "token": "manual", "url_raw": clean_url, "verified": verified}
    ]
    if user:
        camera.enc_username = encrypt(user)
    if password:
        camera.enc_password = encrypt(password)
    camera.last_scanned_at = datetime.now(timezone.utc)
    # Persist the staging data on its own: it must survive even if
    # registration below fails on a duplicate rtsp_url (rollback-safe retry).
    await db.commit()

    name = body.name or _default_name(camera)
    registered = await _register(
        camera, db, request=request, name=name, rtsp_url=full_url,
        # Same rule as add_to_relay: an unplayable URL is registered but left
        # disabled, so it can't enter the health monitor's endless reconnect
        # cycle. verified is None when the caller opted out of verification —
        # nothing was proven either way, so honour their enabled flag.
        enabled=body.enabled and verified is not False,
        recording=body.recording, zone=body.zone,
        metadata=body.metadata,
        lawful_basis=body.lawful_basis, purpose=body.purpose,
        notice_posted=body.notice_posted,
    )
    if registered and verified is False:
        camera.discovery_error = (
            "Added but left disabled — ffprobe found no stream at this URL. "
            "Fix the URL or credentials, then enable it."
        )
        await db.commit()
        await db.refresh(camera)
    if not registered:
        # Keep the row so the user can fix and retry (mirrors old behaviour).
        camera.stage = (
            CameraStage.VERIFIED.value if verified else CameraStage.DISCOVERED.value
        )
        await db.commit()
        await db.refresh(camera)
    return _out(camera)


@router.post("/devices/{device_id}/ignore", response_model=DeviceOut)
async def ignore(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> DeviceOut:
    camera = await _get_or_404(device_id, db)
    _staging_or_409(camera, "remove")
    camera.stage = CameraStage.IGNORED.value
    await db.commit()
    await db.refresh(camera)
    return _out(camera)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(device_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Response:
    camera = await _get_or_404(device_id, db)
    _staging_or_409(camera, "delete")
    await db.delete(camera)
    await db.commit()
    return Response(status_code=204)


def _default_name(c: Camera) -> str:
    parts = [p for p in (c.vendor, c.model) if p]
    base = " ".join(parts) if parts else "Camera"
    return f"{base} {c.ip}"[:255]
