"""events.py — AI activity events: pipeline ingest and the Events tab's reads.

Mounted at /api/analytics/events behind require_authenticated (main.py).

    POST /                  the pipeline reports events (X-Internal-Key only)
    GET  /overview          live ticker (latest 10) + one card per configured camera
    GET  /                  one camera's events, filtered and paginated
    GET  /entry-exit        one camera's Entry / Exit crossings, counted per time bucket

Reads need the `ai_analytics` capability. Events are records about people at
places and times, so this gate is enforced here, by the API, and not only by the
SPA hiding the tab.

The logic lives in services/ai_events.py; this module is HTTP only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Camera, CameraStage
from ..security import get_principal
from ..services import ai_events, entry_exit
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/analytics/events", tags=["analytics-events"])

_can_view = [Depends(require_capability("ai_analytics"))]


@router.post("")
async def ingest_events(
    body: ai_events.IngestBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The pipeline reports what its activities decided happened.

    Not for browser users: an operator cannot assert that an activity fired.
    Per-event rejections come back in the body with a reason, so one bad event
    never costs the rest of its batch.
    """
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Event ingest is for the analytics pipeline (X-Internal-Key) only",
        )

    slugs = {e.sensor_id for e in body.events}
    cameras = (await db.execute(
        select(Camera).where(Camera.slug.in_(slugs),
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().all()

    rows, rejected = ai_events.prepare(
        body.events, {c.slug: c for c in cameras},
        now=datetime.now(timezone.utc),
        default_retention_days=settings.nvr_default_retention_days,
    )
    counts = await ai_events.record(db, rows) if rows else \
        {"inserted": 0, "closed": 0, "duplicates": 0}
    if rejected:
        log.info("ai_events.ingest.rejected", count=len(rejected),
                 reasons=sorted({r["reason"] for r in rejected}))
    return {"received": len(body.events), **counts, "rejected": rejected}


@router.get("/overview", dependencies=_can_view)
async def events_overview(
    per_camera: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The Events tab's main view: the live ticker and the camera cards."""
    return await ai_events.overview(db, per_camera=per_camera)


@router.get("/entry-exit", dependencies=_can_view)
async def entry_exit_analytics(
    camera: str = Query(..., max_length=255, description="Camera slug"),
    minutes: int = Query(default=entry_exit.DEFAULT_MINUTES,
                         description="Range: 5, 15, 30, 60, 120, 360, 720 or 1440 minutes"),
    tripwire: Optional[str] = Query(default=None, max_length=64,
                                    description="One tripwire's region id; omit for all"),
    tz_offset: int = Query(default=0, ge=-entry_exit.MAX_OFFSET_MINUTES,
                           le=entry_exit.MAX_OFFSET_MINUTES,
                           description="Minutes east of UTC; buckets start on round times of that clock"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The Events page's Entry / Exit graph: one camera's crossings per time bucket.

    Counted from the stored Entry and Exit records; the individual records stay
    in analytics_events. Totals are the sum of the buckets.
    """
    if minutes not in entry_exit.RANGES:
        raise HTTPException(422, "'minutes' must be one of "
                                 + ", ".join(str(m) for m in entry_exit.RANGES))
    cam = (await db.execute(
        select(Camera).where(Camera.slug == camera,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()
    if cam is None:
        raise HTTPException(404, f"Camera '{camera}' not found")
    wires = entry_exit.tripwires(cam)
    if tripwire is not None and tripwire not in {w["id"] for w in wires}:
        raise HTTPException(404, f"'{tripwire}' is not a tripwire Entry / Exit watches on this camera")

    win = entry_exit.window(minutes, datetime.now(timezone.utc), tz_offset)
    rows = await entry_exit.counts(db, cam.id, win, tripwire)
    return {
        "camera": {"id": str(cam.id), "slug": cam.slug, "name": cam.name or cam.slug},
        "configured": entry_exit.ACTIVITY in ai_events.configured_activities(cam),
        "tripwires": wires,
        "tripwire": tripwire,
        **entry_exit.series(rows, win),
    }


def _aware(value: Optional[datetime], name: str) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        raise HTTPException(400, f"'{name}' needs a timezone (e.g. 2026-09-13T14:00:00Z)")
    return value


@router.get("", dependencies=_can_view)
async def list_events(
    camera: Optional[str] = Query(default=None, max_length=255,
                                  description="Camera slug; omit for every camera"),
    activity: Optional[str] = Query(default=None, max_length=64),
    frm: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    before: Optional[str] = Query(default=None, max_length=200,
                                  description="`next_before` from the previous page"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Events newest first, narrowed by camera, activity and a start-time range."""
    frm, to = _aware(frm, "from"), _aware(to, "to")
    if frm is not None and to is not None and frm > to:
        raise HTTPException(400, "'from' is after 'to'")
    try:
        cursor = ai_events.Cursor.decode(before) if before else None
    except ValueError:
        raise HTTPException(400, "Invalid 'before' cursor") from None

    camera_ids = None
    if camera:
        cam = (await db.execute(
            select(Camera).where(Camera.slug == camera,
                                 Camera.stage == CameraStage.REGISTERED.value)
        )).scalars().first()
        if cam is None:
            raise HTTPException(404, f"Camera '{camera}' not found")
        camera_ids = [cam.id]

    catalog = await ai_events.load_catalog(db)
    events, next_before = await ai_events.list_events(
        db,
        ai_events.EventFilter(camera_ids=camera_ids, activity=activity or None,
                              frm=frm, to=to, before=cursor, limit=limit),
        catalog,
    )
    return {"events": events, "next_before": next_before}
