"""motion_client.py — camera-registry → motion-service sync.

Mirrors nvr_client.py: the motion service (services/motion) analyses whatever
cameras it is told about at runtime, but that state is in-memory only — a
restart loses every camera. So this module provides both:

  • event hooks   — called from camera update/delete endpoints
  • reconcile_loop — periodically re-asserts the desired set (cameras with
    motion_detection on are analysed; everything else is absent)

Cameras are analysed FROM THE RELAY (rtsp://<host>:<rtsp_port>/<slug>) — the
same single-pull rule as recording, so motion detection adds zero load on the
physical camera and the service never sees camera credentials.

All calls are best-effort: a down motion service must never fail a registry
operation; the reconcile loop heals any missed sync on its next pass.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)

_client: Optional[httpx.AsyncClient] = None


def _base() -> str:
    return settings.motion_api_url.rstrip("/")


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

def _stream_url(slug: str) -> str:
    # Same relay host the NVR records from (loopback on Linux, the mediamtx
    # service name on the bridge overlay) — one setting, one clock, one relay.
    return f"rtsp://{settings.nvr_record_host}:{settings.rtsp_port}/{slug}"


async def add_camera(slug: str, sensitivity: str | None = None) -> bool:
    """Start analysing ``slug`` from its relay URL. 409 (already exists) is OK."""
    params: dict[str, Any] = {"rtsp_url": _stream_url(slug)}
    if sensitivity:
        params["sensitivity"] = sensitivity
    try:
        resp = await _get_client().post(f"{_base()}/cameras/{slug}", params=params)
        if resp.status_code in (200, 409):
            log.info("motion.sync.add.ok", slug=slug, existed=resp.status_code == 409)
            return True
        log.warning("motion.sync.add.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("motion.sync.add.unreachable", slug=slug, error=str(exc))
    return False


async def remove_camera(slug: str) -> bool:
    """Stop analysing ``slug``. 404 (already gone) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}")
        if resp.status_code in (200, 404):
            log.info("motion.sync.remove.ok", slug=slug, missing=resp.status_code == 404)
            return True
        log.warning("motion.sync.remove.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("motion.sync.remove.unreachable", slug=slug, error=str(exc))
    return False


async def restart_camera(slug: str, sensitivity: str | None = None) -> bool:
    """Re-add with new settings (sensitivity changes need a worker restart)."""
    await remove_camera(slug)
    return await add_camera(slug, sensitivity)


async def list_cameras() -> Optional[set[str]]:
    """Slugs currently being analysed, or None if the service is unreachable."""
    try:
        resp = await _get_client().get(f"{_base()}/cameras")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return {c["name"] for c in body.get("cameras", [])}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("motion.sync.list.unreachable", error=str(exc))
        return None


# ── Reconciliation ────────────────────────────────────────────────────────────

async def reconcile(desired: dict[str, str | None], absent: set[str]) -> None:
    """Re-assert desired motion-service state.

    ``desired`` maps slug → sensitivity for every camera that should be
    analysed; ``absent`` lists registry cameras that must not be.
    Unknown-to-the-registry names are left alone.
    """
    current = await list_cameras()
    if current is None:
        return  # service down; next pass retries

    for slug in sorted(set(desired) - current):
        await add_camera(slug, desired[slug])
    for slug in sorted(absent & current):
        await remove_camera(slug)


async def reconcile_loop() -> None:
    """Background task: periodic registry→motion reconciliation. Heals motion
    service restarts and hook calls that failed while it was unreachable."""
    # Local import to avoid a circular import at module load time.
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage

    log.info("motion.sync.loop_started", interval=settings.nvr_sync_interval)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            Camera.slug, Camera.enabled,
                            Camera.motion_detection, Camera.motion_sensitivity,
                        ).where(Camera.stage == CameraStage.REGISTERED.value)
                    )
                ).all()
            # A camera is analysed iff it is enabled AND opted into motion.
            desired = {slug: sens for slug, en, mo, sens in rows if en and mo}
            absent = {slug for slug, en, mo, _ in rows if not (en and mo)}
            await reconcile(desired, absent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("motion.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.nvr_sync_interval)
