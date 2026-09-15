"""frames_client.py — camera-registry → frame-broker sync.

THE PIECE WITHOUT WHICH BROKER MODE IS AN EMPTY PIPE. The broker
(services/frames) decodes each camera once, writes the frame to shared memory
and publishes a motion notice; the Motion service and Smart Search consume
that instead of opening RTSP sessions of their own. But like the motion
service, its camera set is in-memory only — it starts with none and a restart
loses them. Nothing else pushes cameras to it, so without this loop both
consumers subscribe successfully and then wait forever for notices that are
never published.

WHOSE CAMERAS. The broker serves both consumers, so the desired set is their
UNION: a camera is decoded if the Motion service wants it OR Smart Search
does. A camera neither wants is not decoded — that is the whole saving, and
decoding it "just in case" would reintroduce the cost this replaces.

THE UNION IS NOT A COUPLING. Neither consumer learns anything about the
other's camera set: each still filters notices down to what it registered
(motion ignores unknown cameras outright, Smart Search only reads frames for
slugs it wants). The broker simply serves the superset.

Best-effort throughout, like nvr_client and motion_client: a down broker must
never fail a registry operation, and the next pass heals whatever was missed.
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


def is_configured() -> bool:
    """False when no broker is deployed — which is the default. Broker mode is
    opt-in (docker-compose.broker.yml), and a VMS without it must not spend a
    reconcile loop talking to a URL nothing is listening on."""
    return bool(settings.frames_api_url.strip())


def _base() -> str:
    return settings.frames_api_url.rstrip("/")


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
    # The relay, exactly as motion_client and nvr_client use it. The broker
    # replaces per-consumer decodes; it must not become a second puller on the
    # physical camera, and it never sees camera credentials.
    return f"rtsp://{settings.nvr_record_host}:{settings.rtsp_port}/{slug}"


async def add_camera(slug: str) -> bool:
    """Start decoding ``slug`` from its relay URL. 409 (already there) is OK."""
    params: dict[str, Any] = {"rtsp_url": _stream_url(slug)}
    try:
        resp = await _get_client().post(f"{_base()}/cameras/{slug}", params=params)
        if resp.status_code in (200, 409):
            log.info("frames.sync.add.ok", slug=slug, existed=resp.status_code == 409)
            return True
        log.warning("frames.sync.add.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("frames.sync.add.unreachable", slug=slug, error=str(exc))
    return False


async def remove_camera(slug: str) -> bool:
    """Stop decoding ``slug`` and free its ring. 404 (already gone) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}")
        if resp.status_code in (200, 404):
            log.info("frames.sync.remove.ok", slug=slug,
                     missing=resp.status_code == 404)
            return True
        log.warning("frames.sync.remove.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("frames.sync.remove.unreachable", slug=slug, error=str(exc))
    return False


async def list_cameras() -> Optional[set[str]]:
    """Slugs the broker is decoding, or None if it is unreachable."""
    try:
        resp = await _get_client().get(f"{_base()}/cameras")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        # "camera", not "name": the broker keys its rows differently from the
        # motion service, and copying motion_client's reader here raised
        # KeyError on every pass that found a camera. It went unnoticed at
        # first because the very first pass reads an EMPTY broker, iterates
        # nothing, and adds every camera successfully — the loop only breaks
        # once it has something to reconcile against.
        rows = body.get("cameras", [])
        out: set[str] = set()
        for c in rows:
            slug = c.get("camera")
            if slug is None:
                log.warning("frames.sync.list.unexpected_shape",
                            keys=sorted(c)[:8])
                return None          # reconcile against a half-read list is
                                     # worse than waiting for the next pass
            out.add(slug)
        return out
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("frames.sync.list.unreachable", error=str(exc))
        return None


# ── Reconciliation ────────────────────────────────────────────────────────────

async def reconcile(desired: set[str], absent: set[str]) -> None:
    """Re-assert the desired broker state. Names the registry does not know
    about are left alone."""
    current = await list_cameras()
    if current is None:
        return  # broker down; next pass retries

    for slug in sorted(desired - current):
        await add_camera(slug)
    for slug in sorted(absent & current):
        await remove_camera(slug)


def desired_sets(rows) -> tuple[set[str], set[str]]:
    """(decode, do-not-decode) for registry rows.

    A pure function on purpose: this is the rule that decides which cameras
    cost a decode, and it is the one thing here worth pinning in a test.
    Rows are (slug, enabled, motion_detection, search_indexing, search_domains,
    analytics_config).
    """
    from .analytics_client import wants as analytics_wants

    def wanted(en, motion, idx, dom, cfg) -> bool:
        # Exactly the consumers' own conditions, OR-ed — the motion service's,
        # and analytics' (Smart Search indexing OR configured AI activities),
        # imported rather than restated so a change to either rule is a change
        # here too instead of silently drifting.
        if not en:
            return False
        return bool(motion) or analytics_wants(en, idx, dom, cfg)

    desired, absent = set(), set()
    for slug, en, mo, idx, dom, cfg in rows:
        (desired if wanted(en, mo, idx, dom, cfg) else absent).add(slug)
    return desired, absent


async def reconcile_loop() -> None:
    """Background task: periodic registry→broker reconciliation."""
    # Local import to avoid a circular import at module load time.
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage

    log.info("frames.sync.loop_started", interval=settings.nvr_sync_interval,
             broker=settings.frames_api_url)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            Camera.slug, Camera.enabled,
                            Camera.motion_detection,
                            Camera.search_indexing, Camera.search_domains,
                            Camera.analytics_config,
                        ).where(Camera.stage == CameraStage.REGISTERED.value)
                    )
                ).all()

            desired, absent = desired_sets(rows)
            await reconcile(desired, absent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("frames.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.nvr_sync_interval)
