"""People Distribution: the occupancy ingest, the live reading, and a day of it.

    POST /api/analytics/people-distribution            ← the pipeline posts here
    GET  /api/analytics/people-distribution            ← live: every camera now
    GET  /api/analytics/people-distribution/{id}/day   ← history: one local day

JSON, not multipart: occupancy is a handful of integers. The heatmap ingest is
multipart because it carries a JPEG; nothing here carries an image, and nothing
here writes to the object store.

The ingest is for the pipeline only (`X-Internal-Key`), exactly like the heatmap
and event doors -- an operator cannot assert that four people were in a zone.
Reading needs `ai_analytics`, because where people stood is a record about
people.
"""
from __future__ import annotations

import uuid
from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Camera, CameraStage
from ..security import get_principal
from ..services import detection_engine, people_distribution as pd
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/analytics/people-distribution",
                   tags=["analytics-people-distribution"])

_can_view = [Depends(require_capability("ai_analytics"))]


@router.post("")
async def ingest_sample(request: Request, body: dict,
                        db: AsyncSession = Depends(get_db)) -> dict:
    """One occupancy sample from the analytics pipeline."""
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="People Distribution ingest is for the analytics pipeline "
                   "(X-Internal-Key) only",
        )
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    try:
        return await pd.store(db, body)
    except pd.WrongEngine as exc:
        # 409 BEFORE the 422: WrongEngine IS a Rejected, so catching Rejected
        # first would swallow it. Same shape the observation and heatmap doors
        # use, so a sink counts them together and neither retries.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"reason": "source_not_engine",
                                    "message": str(exc)}) from exc
    except pd.Rejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _configured(camera: Camera) -> bool:
    cfg = getattr(camera, "analytics_config", None) or {}
    return any(isinstance(a, dict) and str(a.get("type")) == pd.ACTIVITY
               for a in cfg.get("activities") or [])


async def _cameras(db: AsyncSession) -> list[Camera]:
    rows = (await db.execute(
        select(Camera).where(Camera.stage == CameraStage.REGISTERED.value)
        .order_by(Camera.name)
    )).scalars().all()
    return [c for c in rows if _configured(c)]


@router.get("", dependencies=_can_view)
async def live(db: AsyncSession = Depends(get_db)) -> dict:
    """Every camera with the activity configured, and its newest sample.

    A camera with no sample yet is STILL LISTED, with `sample: null`. Hiding it
    would make a camera that is configured but not yet reporting look like one
    that was never set up -- the two need different answers from an operator.
    """
    cameras = await _cameras(db)
    latest = await pd.latest_for(db, [c.id for c in cameras])
    out = []
    for cam in cameras:
        row = latest.get(cam.id)
        out.append({
            "camera_id": str(cam.id),
            "slug": cam.slug,
            "name": cam.name,
            "camera_enabled": bool(cam.enabled),
            "health": cam.health_status,
            "sample": pd.sample_out(row) if row is not None else None,
        })
    return {"cameras": out,
            "stale_after_seconds": pd.LIVE_STALE_SECONDS,
            "engine": detection_engine.engine()}


@router.get("/{camera_id}/day", dependencies=_can_view)
async def day(camera_id: uuid.UUID,
              day: date = Query(..., description="local calendar day"),
              db: AsyncSession = Depends(get_db)) -> dict:
    """One camera's occupancy through one local day, plus its peaks."""
    camera = (await db.execute(
        select(Camera).where(Camera.id == camera_id)
    )).scalars().first()
    if camera is None:
        raise HTTPException(status_code=404, detail="unknown camera")
    series = await pd.series_for(db, camera_id, day)
    return {
        "camera_id": str(camera.id),
        "slug": camera.slug,
        "name": camera.name,
        "day": day.isoformat(),
        # The history page reads THIS: fixed clock hours, each zone's count of
        # DISTINCT people. `series` is the raw 10-second occupancy behind it,
        # kept for anything that wants the fine grain.
        "hours": await pd.hours_for(db, camera_id, day),
        "series": series,
        "summary": pd.day_summary(series),
    }


@router.get("/{camera_id}/buckets", dependencies=_can_view)
async def buckets(camera_id: uuid.UUID,
                  day: date = Query(..., description="local calendar day"),
                  minutes: int = Query(60, description="bucket size: 15, 30, 60, 180, 360, 720 or 1440"),
                  db: AsyncSession = Depends(get_db)) -> dict:
    """One camera's day as fixed-size buckets of occupancy and dwell, for charts."""
    if minutes not in pd.BUCKET_MINUTES:
        raise HTTPException(status_code=422,
                            detail=f"minutes must be one of {list(pd.BUCKET_MINUTES)}")
    camera = (await db.execute(
        select(Camera).where(Camera.id == camera_id)
    )).scalars().first()
    if camera is None:
        raise HTTPException(status_code=404, detail="unknown camera")
    return {
        "camera_id": str(camera.id),
        "name": camera.name,
        "day": day.isoformat(),
        "minutes": minutes,
        "buckets": await pd.buckets_for(db, camera_id, day, minutes),
    }
