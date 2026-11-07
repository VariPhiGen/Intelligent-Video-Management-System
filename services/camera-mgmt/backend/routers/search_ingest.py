"""search_ingest.py — DeepStream's door into Smart Search.

Mounted at /api/search behind require_authenticated (main.py).

    POST /observations                   one detection (crop + metadata) → the index
    POST /producers/{name}/heartbeat     producer stats → the index's /health

WHY A DOOR AT ALL. The index service has no auth and listens on loopback; CPU
analytics reaches it there directly. DeepStream is a separate pipeline that
may run on another box, and it must not be handed an unauthenticated write
path into a database of people's faces and number plates. So it posts here,
with the internal service key, and this module checks each observation
against what AI Config asks of that camera before forwarding it untouched.

ONLY WHILE DEEPSTREAM IS THE ENGINE (services/detection_engine.py). With CPU
analytics as the engine this door refuses everything: two producers indexing
one camera would put every person in the index twice.

The observation is forwarded in the index's own format (multipart: `meta`
JSON + `crop` image + optional `frame` JPEG — smartsearch api/server.py), so
the index validates, embeds and deduplicates it exactly as it does CPU
analytics' observations. Its answer (and status) is passed back verbatim.
"""
from __future__ import annotations

import json
import re

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Camera, CameraStage
from ..security import get_principal
from ..services import detection_engine

log = structlog.get_logger(__name__)
router = APIRouter(tags=["search-ingest"])

#: Upper bounds, so one request cannot hold a worker on an absurd upload. The
#: index keeps a frame only up to 4 MB itself; a crop is a fraction of that.
MAX_CROP_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_META_BYTES = 64 * 1024

_TIMEOUT = httpx.Timeout(20.0, connect=3.0)
_PRODUCER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


async def _require_service(request: Request) -> None:
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Detection ingest is for the pipeline (X-Internal-Key) only")


def _index_url() -> str:
    base = (settings.smartsearch_index_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "No Smart Search index is deployed on this VMS")
    return base


def _refuse(reason: str, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    # A reason the producer can log and count; 4xx = do not retry.
    return HTTPException(code, {"reason": reason})


@router.post("/observations")
async def ingest_observation(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """One DeepStream detection → Smart Search, if AI Config asks for it."""
    await _require_service(request)
    if not detection_engine.deepstream_mode():
        raise _refuse("deepstream_not_engine", status.HTTP_409_CONFLICT)
    base = _index_url()

    form = await request.form(max_files=2, max_fields=4)
    meta_raw = form.get("meta")
    crop = form.get("crop")
    frame = form.get("frame")
    if not isinstance(meta_raw, str) or len(meta_raw) > MAX_META_BYTES:
        raise _refuse("meta_missing_or_too_large")
    if crop is None or isinstance(crop, str):
        raise _refuse("crop_missing")
    try:
        meta = json.loads(meta_raw)
    except ValueError:
        raise _refuse("meta_not_json") from None
    if not isinstance(meta, dict):
        raise _refuse("meta_not_object")

    slug, domain = meta.get("camera"), meta.get("domain")
    if not isinstance(slug, str) or not isinstance(domain, str):
        raise _refuse("camera_and_domain_required")
    camera = (await db.execute(
        select(Camera).where(Camera.slug == slug,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()
    if camera is None:
        raise _refuse("unknown_camera")
    why = detection_engine.observation_refusal(camera, domain, bool(meta.get("plate")))
    if why:
        raise _refuse(why)

    crop_bytes = await crop.read()
    if not crop_bytes or len(crop_bytes) > MAX_CROP_BYTES:
        raise _refuse("crop_empty_or_too_large")
    files = {"crop": (crop.filename or "crop.png", crop_bytes,
                      crop.content_type or "application/octet-stream")}
    if frame is not None and not isinstance(frame, str):
        frame_bytes = await frame.read()
        if len(frame_bytes) > MAX_FRAME_BYTES:
            raise _refuse("frame_too_large")
        if frame_bytes:
            files["frame"] = ("frame.jpg", frame_bytes, "image/jpeg")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/observations", data={"meta": meta_raw}, files=files)
    except httpx.RequestError as exc:
        log.warning("search_ingest.index_unreachable", error=str(exc))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Smart Search index is unreachable") from None
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:300]}
    if r.status_code >= 400:
        # The index's own refusal (422 bad crop / embedding, 503 warming) —
        # passed through so the producer retries exactly what the index says.
        raise HTTPException(r.status_code, body.get("detail", body))
    return body


@router.post("/producers/{name}/heartbeat")
async def producer_heartbeat(name: str, request: Request) -> dict:
    """Producer stats → the index, where /health reports whether it is feeding."""
    await _require_service(request)
    if not _PRODUCER.match(name):
        raise _refuse("bad_producer_name")
    base = _index_url()
    try:
        stats = await request.json()
    except ValueError:
        stats = {}
    if not isinstance(stats, dict):
        stats = {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/producers/{name}/heartbeat", json=stats)
    except httpx.RequestError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Smart Search index is unreachable") from None
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()
