"""audit.py — read/verify/export the audit trail, plus an internal ingest seam.

The log itself is written by services/audit.py from the handlers that perform the
actions. This router only exposes it: the Administration → Audit log tab lists and
filters entries (with a per-row integrity check), verifies the whole chain,
exports a forensic CSV/JSON ("CERT-In export"), and accepts events from trusted
sibling services that live on a different origin (e.g. Smart Search face queries)
via ``POST /ingest``.

Access: reading and exporting require ``admin`` or ``dpo`` (the compliance
officer). Ingest requires a trusted service principal (the X-Internal-Key), never
a browser user.

A second router may mount additional routes on this same ``/audit`` prefix — the
DPDP posture report does, from an optional extension. That report used to live in
this file; it is a compliance product rather than part of the audit trail, and
the audit trail is not gated. Nothing here depends on it, and its absence is a
404 on one path.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import Text, func, or_, select

from ..db import get_db
from ..models import AuditLog
from ..security import Principal, get_principal, require_role
from ..services import audit as audit_svc

router = APIRouter(prefix="/audit", tags=["audit"])

_READ_ROLES = require_role("admin", "dpo")


def _apply_filters(stmt, q, action, actor, since, until):
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                AuditLog.actor.ilike(like),
                AuditLog.action.ilike(like),
                AuditLog.target.ilike(like),
                func.cast(AuditLog.detail, Text).ilike(like),
            )
        )
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if since:
        stmt = stmt.where(AuditLog.ts >= since)
    if until:
        stmt = stmt.where(AuditLog.ts <= until)
    return stmt


def _serialize(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "ts": row.ts.isoformat(),
        "actor_type": row.actor_type,
        "actor": row.actor,
        "action": row.action,
        "target": row.target,
        "detail": row.detail or {},
        "source_ip": row.source_ip,
        "outcome": row.outcome,
        "integrity": "verified" if audit_svc.is_intact(row) else "mismatch",
    }


@router.get("")
async def list_audit(
    request: Request,
    principal: Principal = Depends(_READ_ROLES),
    db=Depends(get_db),
    q: Optional[str] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    base = _apply_filters(select(AuditLog), q, action, actor, since, until)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(AuditLog.id.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {
        "entries": [_serialize(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/verify")
async def verify_audit(
    principal: Principal = Depends(_READ_ROLES),
    db=Depends(get_db),
) -> dict:
    return await audit_svc.verify_chain(db)


@router.get("/export")
async def export_audit(
    principal: Principal = Depends(_READ_ROLES),
    db=Depends(get_db),
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    q: Optional[str] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Response:
    stmt = _apply_filters(select(AuditLog), q, action, actor, since, until)
    rows = (await db.execute(stmt.order_by(AuditLog.id.asc()))).scalars().all()
    stamp = rows[-1].ts.strftime("%Y%m%d") if rows else "empty"

    if format == "json":
        body = json.dumps(
            [
                {**_serialize(r), "prev_hash": r.prev_hash, "entry_hash": r.entry_hash}
                for r in rows
            ],
            separators=(",", ":"),
            default=str,
        )
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="audit-log-{stamp}.json"'},
        )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["id", "timestamp", "actor_type", "actor", "action", "target",
         "detail", "source_ip", "outcome", "integrity", "prev_hash", "entry_hash"]
    )
    for r in rows:
        s = _serialize(r)
        w.writerow(
            [r.id, s["ts"], r.actor_type, r.actor, r.action, r.target,
             json.dumps(r.detail or {}, separators=(",", ":")),
             r.source_ip, r.outcome, s["integrity"], r.prev_hash, r.entry_hash]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit-log-{stamp}.csv"'},
    )




class IngestBody(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    actor_type: str = Field(default="service", max_length=16)
    actor: Optional[str] = Field(default=None, max_length=255)
    target: Optional[str] = Field(default=None, max_length=255)
    detail: dict = Field(default_factory=dict)
    source_ip: Optional[str] = Field(default=None, max_length=64)
    outcome: str = Field(default="success", max_length=16)


@router.post("/ingest", status_code=202)
async def ingest_audit(body: IngestBody, request: Request) -> dict:
    """Trusted sibling services (different origin, e.g. Smart Search) post events
    here with the internal key. Not for browser users — a signed-in user's
    actions are captured at their own handlers, not self-reported."""
    principal = await get_principal(request)
    if principal.kind != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audit ingest is for trusted services (X-Internal-Key) only",
        )
    await audit_svc.log_event(
        action=body.action,
        actor_type=body.actor_type,
        actor=body.actor,
        target=body.target,
        detail=body.detail,
        source_ip=body.source_ip or audit_svc.client_ip(request),
        outcome=body.outcome,
    )
    return {"status": "accepted"}
