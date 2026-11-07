"""timeline.py — person / vehicle / motion presence for the playback timeline.

Mounted at /api/playback/timeline behind require_authenticated (main.py).

    POST /presence   analytics / motion report presence segments (X-Internal-Key only)
    GET  ""          one camera's merged presence bars for a window
    GET  /next       the nearest marker (presence or event) before / after a moment

Reads need `ai_analytics`, like the events they are drawn beside: presence is a
record of people at a place and a time, so the gate is enforced by the API and
not only by the SPA hiding the strip.

The logic lives in services/timeline.py; this module is HTTP only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Camera, CameraStage
from ..security import get_principal
from ..services import timeline
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/playback/timeline", tags=["playback-timeline"])

_can_view = [Depends(require_capability("ai_analytics"))]


@router.post("/presence")
async def ingest_presence(
    body: timeline.IngestBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Analytics (tracked objects) and motion (runs of motion) report presence.

    Not for browser users. Per-segment rejections come back with a reason, so
    one camera reconfigured mid-flight never costs the rest of the batch.
    """
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Presence ingest is for the analytics and motion services "
                   "(X-Internal-Key) only",
        )
    slugs = {s.sensor_id for s in body.segments}
    cameras = (await db.execute(
        select(Camera).where(Camera.slug.in_(slugs),
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().all()
    rows, rejected = timeline.prepare(body.segments, {c.slug: c for c in cameras},
                                      now=datetime.now(timezone.utc))
    counts = await timeline.record(db, rows)
    if rejected:
        log.info("timeline.ingest.rejected", count=len(rejected),
                 reasons=sorted({r["reason"] for r in rejected}))
    return {"received": len(body.segments), **counts, "rejected": rejected}


def _kinds(raw: Optional[str], *, allow_empty: bool) -> list[str]:
    wanted = [k.strip() for k in (raw if raw is not None else ",".join(timeline.KINDS)).split(",")
              if k.strip()]
    bad = [k for k in wanted if k not in timeline.KINDS]
    if bad or (not wanted and not allow_empty):
        raise HTTPException(400, f"Unknown kind(s): {', '.join(bad) or '(none)'}")
    return list(dict.fromkeys(wanted))


async def _camera(db: AsyncSession, slug: str) -> Camera:
    cam = (await db.execute(
        select(Camera).where(Camera.slug == slug,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()
    if cam is None:
        raise HTTPException(404, f"Camera '{slug}' not found")
    return cam


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise HTTPException(400, f"'{name}' needs a timezone")
    return value


@router.get("", dependencies=_can_view)
async def get_presence(
    camera: str = Query(max_length=255, description="Camera slug"),
    frm: datetime = Query(alias="from"),
    to: datetime = Query(),
    kinds: Optional[str] = Query(default=None, max_length=64,
                                 description="Comma-separated: person,vehicle,motion (default all)"),
    px: int = Query(default=1200, ge=100, le=8000,
                    description="Timeline width in pixels — sets the merge resolution"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Merged presence bars, `[start, end, segments]` in epoch seconds, per kind."""
    frm, to = _aware(frm, "from"), _aware(to, "to")
    if frm >= to:
        raise HTTPException(400, "'from' must be before 'to'")
    if to - frm > timeline.MAX_WINDOW:
        raise HTTPException(400, "Window is longer than the timeline can show")
    wanted = _kinds(kinds, allow_empty=False)
    cam = await _camera(db, camera)

    data = await timeline.presence(db, cam.id, frm, to, wanted, px)
    return {"camera": cam.slug, "from": frm.timestamp(), "to": to.timestamp(),
            # Which kinds AI Config has this camera producing NOW, so the UI can
            # tell "indexed and quiet" from "not indexed" without a second call.
            "indexed": sorted(timeline.indexed_kinds(cam)), **data}


@router.get("/next", dependencies=_can_view)
async def get_adjacent(
    camera: str = Query(max_length=255, description="Camera slug"),
    at: datetime = Query(description="Where the playhead is (timezone required)"),
    direction: str = Query(default="next", alias="dir", pattern="^(next|prev)$"),
    kinds: Optional[str] = Query(default=None, max_length=64,
                                 description="Presence kinds to consider; empty = none"),
    events: bool = Query(default=True, description="Consider AI events too"),
    hide: Optional[str] = Query(default=None, max_length=2000,
                                description="Comma-separated activity keys to skip"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The nearest marker past `at` in `dir`, across all footage still kept.

    `{"marker": null}` when there is none — an answer, not an error: the
    operator is at the last (or first) thing that happened.
    """
    at = _aware(at, "at")
    wanted = _kinds(kinds, allow_empty=True)
    hidden = [h.strip() for h in (hide or "").split(",") if h.strip()]
    cam = await _camera(db, camera)
    found = await timeline.adjacent(db, cam.id, at, direction, wanted, events, hidden)
    return {"camera": cam.slug, "marker": found}
