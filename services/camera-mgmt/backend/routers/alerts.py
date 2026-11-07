"""alerts.py — the trigger log: what fired, who looked at it, what it turned out to be.

`resolution` is the column that makes per-rule false-positive reporting
possible at all, and it cannot be reconstructed after the fact — so it is a
constrained Literal from the first row rather than a free string that would
accumulate four spellings of "false alarm" before anyone tried to GROUP BY it.

TWO AUTHORITIES. Reads need `alerts_view`; closing the loop needs `alerts_ack`.
They are separate because somebody watching a wall should see what fired
without being able to mark it handled — an alert silently acknowledged by a
passer-by is an alert nobody is coming to.

THE EVIDENCE HOLD IS NOT A CONVENIENCE FLAG. While `legal_hold` is true the
retention sweep will not touch the alert at any age, which is the one retention
rule in this product that is not negotiable. It sits behind `alerts_ack` rather
than something stronger because the person who triages an incident is the
person who knows, at that moment, that its record has to be kept.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_db
from ..models import Alert, AlertOut, Camera
from ..security import Principal
from ..services import audit as audit_svc
from ..services.policy import require_capability

router = APIRouter(prefix="/alerts", tags=["alerts"])

_CAN_VIEW = [Depends(require_capability("alerts_view"))]
_ACK = require_capability("alerts_ack")


class ResolveBody(BaseModel):
    """Constrained from the first row: an unknown verdict is a 422, never a
    stored string. See the module docstring."""

    model_config = {"extra": "forbid"}

    resolution: Literal["true_positive", "false_positive", "duplicate", "no_action"]
    note: Optional[str] = Field(default=None, max_length=2000)


class HoldBody(BaseModel):
    model_config = {"extra": "forbid"}

    legal_hold: bool


def _out(row: Alert) -> AlertOut:
    return AlertOut.model_validate(row)


async def _get_or_404(db, alert_id: uuid.UUID) -> Alert:
    row = await db.get(Alert, alert_id)
    if row is None:
        raise HTTPException(404, f"Alert '{alert_id}' not found")
    return row


@router.get("", dependencies=_CAN_VIEW)
async def list_alerts(
    state: Optional[str] = Query(default=None),
    camera: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db=Depends(get_db),
) -> dict:
    q = select(Alert).order_by(Alert.triggered_at.desc()).limit(limit)
    if state:
        q = q.where(Alert.state == state)
    if since:
        q = q.where(Alert.triggered_at >= since)
    if camera:
        cam = (await db.execute(
            select(Camera).where(Camera.slug == camera)
        )).scalars().first()
        if cam is None:
            # NARROW TO NOTHING, never widen. Dropping an unresolvable filter
            # and returning the unfiltered set is how one camera's alerts leak
            # into a view somebody scoped on purpose.
            return {"total": 0, "alerts": []}
        q = q.where(Alert.camera_id == cam.id)
    rows = (await db.execute(q)).scalars().all()
    return {"total": len(rows), "alerts": [_out(r).model_dump() for r in rows]}


@router.post("/{alert_id}/ack")
async def acknowledge(alert_id: uuid.UUID, request: Request,
                      principal: Principal = Depends(_ACK),
                      db=Depends(get_db)) -> AlertOut:
    """Who acknowledged it and when are the two facts an operator handover
    needs, and neither can be reconstructed later."""
    row = await _get_or_404(db, alert_id)
    row.state = "acknowledged"
    row.acknowledged_at = datetime.now(timezone.utc)
    row.acknowledged_by = principal.subject
    await db.commit()
    await db.refresh(row)
    await audit_svc.record(request, principal, "alert.acknowledged",
                           target=f"alert/{alert_id}", detail={"rule": row.rule_name})
    return _out(row)


@router.post("/{alert_id}/resolve")
async def resolve(alert_id: uuid.UUID, body: ResolveBody, request: Request,
                  principal: Principal = Depends(_ACK),
                  db=Depends(get_db)) -> AlertOut:
    row = await _get_or_404(db, alert_id)
    row.state = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    row.resolved_by = principal.subject
    row.resolution = body.resolution
    row.resolution_note = body.note
    await db.commit()
    await db.refresh(row)
    await audit_svc.record(request, principal, "alert.resolved",
                           target=f"alert/{alert_id}",
                           detail={"rule": row.rule_name, "resolution": body.resolution})
    return _out(row)


@router.post("/{alert_id}/hold")
async def set_hold(alert_id: uuid.UUID, body: HoldBody, request: Request,
                   principal: Principal = Depends(_ACK),
                   db=Depends(get_db)) -> AlertOut:
    """Evidence lock. While true the retention sweep will not touch this alert
    at any age — see rules_engine.purge_expired_alerts."""
    row = await _get_or_404(db, alert_id)
    row.legal_hold = body.legal_hold
    await db.commit()
    await db.refresh(row)
    await audit_svc.record(request, principal, "alert.hold_set",
                           target=f"alert/{alert_id}",
                           detail={"legal_hold": body.legal_hold,
                                   "rule": row.rule_name})
    return _out(row)
