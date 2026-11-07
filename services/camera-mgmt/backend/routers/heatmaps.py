"""heatmaps.py — heat overlays: analytics uploads them, the Heatmap page reads them.

Mounted at /api/analytics/heatmaps behind require_authenticated (main.py).

    POST /                          analytics uploads an overlay (X-Internal-Key only)
    GET  /                          one card per camera: state, period, latest picture
    GET  /{camera_id}/sessions      that camera's recent sessions
    GET  /{camera_id}/availability  days with stored heat, first/last, storage caps
    GET  /artifacts/{id}/image      the overlay itself, as JPEG
    POST /{camera_id}/runtime       the operator's ON/OFF switch

Reads need `ai_analytics`, the same gate the Events tab uses: a heatmap is a
record of where people were. Changing the runtime switch is a configuration
change on a camera, so it needs `camera_manage` — but it is NOT a schedule
change, and this module never writes `cameras.analytics_config`.

The logic lives in services/heatmaps.py; this module is HTTP only.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Optional

import structlog
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Camera, CameraStage
from ..security import Principal, get_principal
from ..services import audit as audit_svc
from ..services import heatmap_chunks, heatmaps, object_store
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/analytics/heatmaps", tags=["analytics-heatmaps"])

_can_view = [Depends(require_capability("ai_analytics"))]
_MANAGE = require_capability("camera_manage")

#: An overlay of a 1280x720 frame is ~200-400 KB; this is a sanity bound on the
#: ingest, not a target.
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class RuntimeBody(BaseModel):
    """The operator's switch. `mode` applies when switching ON and a frozen
    session is waiting: resume it, or close it and start a new one."""

    enabled: bool
    mode: str = Field(default="resume", pattern="^(resume|new)$")
    reason: Optional[str] = Field(default=None, max_length=200)


@router.post("")
async def ingest_heatmap(
    request: Request,
    meta: str = Form(...),
    image: Optional[UploadFile] = File(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Analytics uploads one overlay, or reports a session change.

    Not for browser users: an operator cannot assert that a heatmap happened.
    Multipart rather than JSON because the picture is bytes and base64 in a
    JSON body would cost a third more for nothing.
    """
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Heatmap ingest is for the analytics pipeline (X-Internal-Key) only",
        )
    try:
        parsed = json.loads(meta)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="meta must be JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="meta must be a JSON object")

    payload = await image.read() if image is not None else None
    if payload is not None and len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="heatmap image too large")
    try:
        return await heatmaps.store(db, parsed, payload)
    except heatmaps.WrongEngine as exc:
        # 409 BEFORE the 422 below, and the order of these two clauses is the
        # whole point: WrongEngine IS a Rejected, so catching Rejected first
        # would swallow it. The producer is not wrong about this message -- this
        # appliance simply has another engine -- and 409 is what the observation
        # door already answers for `deepstream_not_engine`, so a sink counts the
        # two together and neither retries.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"reason": "source_not_engine",
                                    "message": str(exc)}) from exc
    except heatmaps.Rejected as exc:
        # 422, not 5xx: the sink retries 5xx for ever, and nothing about this
        # upload will get better by repeating it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", dependencies=_can_view)
async def list_heatmaps(db: AsyncSession = Depends(get_db)) -> dict:
    """One entry per camera with the heatmap configured, newest picture first."""
    cameras = await heatmaps.overview(db)
    return {"cameras": cameras, "snapshot_interval_s": 60}


@router.get("/artifacts/{artifact_id}/image", dependencies=_can_view)
async def heatmap_image(artifact_id: int, request: Request,
                        db: AsyncSession = Depends(get_db)) -> Response:
    """The overlay itself. Cached by ETag: a card polls every minute and the
    picture only changes when a new one is rendered."""
    row = await heatmaps.artifact(db, artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such heatmap image")

    # TWO PLACES THE BYTES CAN BE, and the row says which. An archived picture
    # written since the object store arrived carries `object_key` and no bytes;
    # a `live` picture, and every picture written before the store existed,
    # carries the bytes inline. Reading both means nothing had to be migrated
    # and the old path keeps working while the new one proves itself.
    payload = row.image
    if payload is None and row.object_key:
        try:
            payload = object_store.get(row.object_key)
        except object_store.ObjectStoreError as exc:
            # The store is down, not the picture missing. 503 so a browser
            # retries rather than caching a 404 for an image that exists.
            log.warning("heatmaps.image.store_unreachable",
                        artifact=artifact_id, key=row.object_key, error=str(exc))
            raise HTTPException(status_code=503,
                                detail="Heatmap object store is unreachable") from None
        if payload is None:
            # Retention deletes objects and rows in two steps; a reader can
            # arrive between them. 404 is the truth.
            log.info("heatmaps.image.object_missing", artifact=artifact_id,
                     key=row.object_key)
    if not payload:
        raise HTTPException(status_code=404, detail="No such heatmap image")

    etag = f'"{hashlib.sha256(payload).hexdigest()[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return Response(content=payload, media_type=row.content_type or "image/jpeg",
                    headers={"ETag": etag, "Cache-Control": "private, max-age=3600"})


@router.get("/{camera_id}/sessions", dependencies=_can_view)
async def camera_sessions(camera_id: uuid.UUID, limit: int = Query(default=20, ge=1, le=100),
                          day: Optional[date] = Query(
                              default=None,
                              description="Local calendar day (YYYY-MM-DD). Omit for the "
                                          "most recent sessions."),
                          db: AsyncSession = Depends(get_db)) -> dict:
    """That camera's accumulation sessions, newest first.

    With `day`, the historical view: the sessions that STARTED on that local
    calendar day. Without it, the recent list the overview card uses.
    """
    camera = await _camera_or_404(db, camera_id)
    return {"camera_id": str(camera.id), "slug": camera.slug,
            "day": day.isoformat() if day else None,
            "sessions": await heatmaps.sessions_for(db, camera.id, limit=limit, day=day)}


@router.get("/sessions/{session_id}/artifacts", dependencies=_can_view)
async def session_artifacts(session_id: uuid.UUID,
                            db: AsyncSession = Depends(get_db)) -> dict:
    """Every picture of one session, oldest first — the historical timeline.

    This is what draws 09:00, 09:30, 10:00 … with the session's final picture
    beside them. Deliberately separate from `/{camera_id}/sessions`, which
    returns ONE artifact per session for the overview card; a timeline needs
    them all and the two must not be conflated.
    """
    session = await heatmaps.session_or_none(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such heatmap session")
    artifacts = await heatmaps.artifacts_for_session(db, session_id)
    return {
        "session": heatmaps.session_out(session),
        # Split out so the page does not have to filter: the big picture at the
        # top, the strip underneath.
        "final": next((a for a in artifacts if a["kind"] == heatmaps.FINAL), None),
        "snapshots": [a for a in artifacts if a["kind"] == heatmaps.SNAPSHOT],
        "live": next((a for a in artifacts if a["kind"] == heatmaps.LIVE), None),
    }


@router.post("/{camera_id}/runtime")
async def set_runtime(camera_id: uuid.UUID, body: RuntimeBody, request: Request,
                      principal: Principal = Depends(_MANAGE),
                      db: AsyncSession = Depends(get_db)) -> dict:
    """Turn this camera's heatmap on or off.

    Switching OFF freezes the session and keeps its last picture; switching ON
    either resumes that session or starts a new one. The configured AI Config
    schedule is not touched either way.
    """
    camera = await _camera_or_404(db, camera_id)
    if not heatmaps.configured(camera.analytics_config):
        raise HTTPException(status_code=409,
                            detail="Heatmap overlay is not configured on this camera")
    result = await heatmaps.set_runtime(db, camera, body.enabled, mode=body.mode,
                                        actor=principal.subject)
    await audit_svc.record(
        request, principal,
        "heatmap.enabled" if body.enabled else "heatmap.disabled",
        target=f"camera/{camera.id}",
        detail={"slug": camera.slug, "mode": body.mode, "reason": body.reason},
    )
    return result


async def _camera_or_404(db: AsyncSession, camera_id: uuid.UUID) -> Camera:
    camera = (await db.execute(
        select(Camera).where(Camera.id == camera_id,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


# ── 5-minute heat blocks and the heatmap of any range ────────────────────────

#: A 5-minute block at 1/4 scale is a few hundred KB uncompressed; this is slack.
MAX_CHUNK_BYTES = 32 * 1024 * 1024


@router.post("/chunks")
async def ingest_chunk(
    request: Request,
    meta: str = Form(...),
    heat: UploadFile = File(...),
    background: Optional[UploadFile] = File(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Analytics uploads 5 minutes of heat (.npz) and, now and then, a camera frame."""
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Heat blocks are for the analytics pipeline (X-Internal-Key) only")
    try:
        parsed = json.loads(meta)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="meta must be JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="meta must be a JSON object")
    data = await heat.read()
    bg = await background.read() if background is not None else None
    if len(data) > MAX_CHUNK_BYTES or (bg and len(bg) > MAX_IMAGE_BYTES):
        raise HTTPException(status_code=413, detail="heat block too large")
    try:
        return await heatmap_chunks.store(db, parsed, data, bg)
    except heatmap_chunks.WrongEngine as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"reason": "source_not_engine", "message": str(exc)}) from exc
    except heatmap_chunks.Rejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _instant(value: str, name: str) -> datetime:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be an ISO date-time") from exc
    # A bare local time ("2026-10-13T09:00") means the appliance's clock.
    return at.astimezone() if at.tzinfo is None else at


async def _camera(db: AsyncSession, camera_id: uuid.UUID) -> Camera:
    cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalars().first()
    if cam is None:
        raise HTTPException(status_code=404, detail="unknown camera")
    return cam


@router.get("/{camera_id}/availability", dependencies=_can_view)
async def availability(camera_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Which local days have stored heat, from when, and the camera's storage
    caps -- what the page needs to grey out days and say what is kept."""
    return await heatmap_chunks.availability(db, await _camera(db, camera_id))


@router.get("/{camera_id}/range", dependencies=_can_view)
async def range_summary(camera_id: uuid.UUID,
                        start: str = Query(..., description="ISO date-time"),
                        end: str = Query(..., description="ISO date-time"),
                        db: AsyncSession = Depends(get_db)) -> dict:
    """What the heatmap of [start, end) is made of: blocks, heat points, the
    period they actually cover, and when the background frame was taken."""
    cam = await _camera(db, camera_id)
    try:
        return await heatmap_chunks.summary(db, cam, _instant(start, "start"), _instant(end, "end"))
    except heatmap_chunks.Rejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{camera_id}/range/image", dependencies=_can_view)
async def range_image(camera_id: uuid.UUID,
                      start: str = Query(..., description="ISO date-time"),
                      end: str = Query(..., description="ISO date-time"),
                      db: AsyncSession = Depends(get_db)) -> Response:
    """The heat of every 5-minute block in [start, end) added up, drawn over the
    most recent camera frame in that range. Colours are relative: the hottest
    spot in the range is red."""
    cam = await _camera(db, camera_id)
    try:
        jpeg = await heatmap_chunks.render(db, cam, _instant(start, "start"), _instant(end, "end"))
    except heatmap_chunks.Rejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if jpeg is None:
        raise HTTPException(status_code=404, detail="no heat or camera frame stored for this range")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "private, no-store"})
