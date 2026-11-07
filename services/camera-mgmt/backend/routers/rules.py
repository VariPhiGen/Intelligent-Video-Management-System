"""rules.py — automation rules: CRUD, arming, and the kill switch.

TWO AUTHORITIES, DELIBERATELY SEPARATED, and the separation is the whole point
of this file. Editing a rule changes what it would match; ARMING it is what
lets it act on a physical device. So `mode` is not a patchable field — it moves
only through /arm and /disarm, which audit as `rule.armed` / `rule.disarmed`
rather than as `rule.updated {changed: ["mode"]}`. An auditor scanning the log
for authority changes should see "this rule became able to open a gate", not a
field diff they would read straight past.

THE GLOBAL KILL SWITCH (/disarm, /rearm) exists because RULES_ENABLED is an env
var, and a rule flapping a barrier at 02:00 needs one button rather than a
redeploy. It requires a reason, and the reason is shown to every operator in
the SPA banner: a disarm nobody can explain is an unexplained outage for
whoever comes on shift next.

DISARMING STOPS ACTUATION, NEVER EVIDENCE. Alerts keep being raised while the
engine is disarmed — the detections still happened, and suppressing them would
corrupt the false-positive denominator the resolution column exists to compute.

P1 MOVES NOTHING PHYSICAL. There is no bridge, so an armed rule with a
peripheral action records `no_bridge` and calls no driver. Arming is still a
real authority worth gating and auditing now, because the day the bridge lands
these rows are already live.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db import get_db
from ..models import (
    AlertRule, AlertRuleIn, AlertRuleOut, AlertRulePatch, check_scope_pairing,
)
from ..security import Principal
from ..services import audit as audit_svc
from ..services import rules_engine
from ..services.policy import require_capability

router = APIRouter(prefix="/rules", tags=["rules"])

_MANAGE = require_capability("rule_manage")


class ArmBody(BaseModel):
    """Required, not optional. See the module docstring: a disarm with no
    stated reason is an unexplained outage for the next shift."""

    reason: str = Field(min_length=1, max_length=280)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("a reason is required — it is shown to every operator")
        return v


def _out(row: AlertRule) -> AlertRuleOut:
    return AlertRuleOut.model_validate(row)


async def _get_or_404(db, rule_id: uuid.UUID) -> AlertRule:
    row = await db.get(AlertRule, rule_id)
    if row is None:
        raise HTTPException(404, f"Rule '{rule_id}' not found")
    return row


async def _reject_duplicate_name(db, name: str,
                                 exclude: uuid.UUID | None = None) -> None:
    """Case-insensitive pre-check for a friendly message. The unique index
    ix_alert_rules_name_lower is the real guard — this is the cameras.py
    pattern, and the IntegrityError handler below is what makes it safe under
    a race."""
    q = select(AlertRule).where(func.lower(AlertRule.name) == name.strip().lower())
    if exclude is not None:
        q = q.where(AlertRule.id != exclude)
    dup = (await db.execute(q)).scalars().first()
    if dup is not None:
        raise HTTPException(409, detail=f"A rule named '{dup.name}' already exists")


@router.get("")
async def list_rules(db=Depends(get_db)) -> list[AlertRuleOut]:
    rows = (await db.execute(
        select(AlertRule).order_by(AlertRule.name.asc())
    )).scalars().all()
    return [_out(r) for r in rows]


@router.post("", status_code=201)
async def create_rule(body: AlertRuleIn, request: Request,
                      principal: Principal = Depends(_MANAGE),
                      db=Depends(get_db)) -> AlertRuleOut:
    await _reject_duplicate_name(db, body.name)
    now = datetime.now(timezone.utc)
    # mode is NOT read from the body — AlertRuleIn does not carry it. A new
    # rule is always log_only, whatever its actions say.
    row = AlertRule(id=uuid.uuid4(), mode="log_only", created_at=now, updated_at=now,
                    created_by=principal.subject, **body.model_dump())
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail=f"A rule named '{body.name}' already exists")
    await db.refresh(row)
    await rules_engine.bump_version()
    await audit_svc.record(request, principal, "rule.created",
                           target=f"rule/{row.id}",
                           detail={"name": row.name, "activity": row.activity,
                                   "scope": f"{row.scope_kind}:{row.scope_value or '*'}"})
    return _out(row)


@router.patch("/{rule_id}")
async def update_rule(rule_id: uuid.UUID, body: AlertRulePatch, request: Request,
                      principal: Principal = Depends(_MANAGE),
                      db=Depends(get_db)) -> AlertRuleOut:
    row = await _get_or_404(db, rule_id)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return _out(row)
    if fields.get("name"):
        await _reject_duplicate_name(db, fields["name"], exclude=row.id)
    # The scope pairing is a property of the RESULT, not of the patch: changing
    # scope_kind alone can strand a rule that matches nothing, and the database
    # CHECK would reject it as a 500 rather than a message.
    _merged_kind = fields.get("scope_kind", row.scope_kind)
    _merged_value = fields.get("scope_value", row.scope_value)
    try:
        check_scope_pairing(_merged_kind, _merged_value)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))

    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail="A rule with that name already exists")
    await db.refresh(row)
    await rules_engine.bump_version()
    await audit_svc.record(request, principal, "rule.updated", target=f"rule/{row.id}",
                           detail={"changed": sorted(fields.keys())})
    return _out(row)


@router.post("/{rule_id}/arm")
async def arm_rule(rule_id: uuid.UUID, request: Request,
                   principal: Principal = Depends(_MANAGE),
                   db=Depends(get_db)) -> AlertRuleOut:
    """Promote log_only -> armed: its own endpoint and its own audit action,
    because this is the moment a rule becomes able to move something.

    The audit detail carries the actions as they stood at the moment of arming.
    "This rule was armed" is half the fact; WHICH peripheral it can now reach is
    the other half, and it is the half that cannot be reconstructed once the
    rule is edited.
    """
    row = await _get_or_404(db, rule_id)
    row.mode = "armed"
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    await rules_engine.bump_version()
    await audit_svc.record(request, principal, "rule.armed", target=f"rule/{row.id}",
                           detail={"name": row.name, "actions": row.actions})
    return _out(row)


@router.post("/{rule_id}/disarm")
async def disarm_rule(rule_id: uuid.UUID, request: Request,
                      principal: Principal = Depends(_MANAGE),
                      db=Depends(get_db)) -> AlertRuleOut:
    """Back to log_only. The rule keeps firing and keeps raising alerts; it
    simply stops being able to act."""
    row = await _get_or_404(db, rule_id)
    row.mode = "log_only"
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    await rules_engine.bump_version()
    await audit_svc.record(request, principal, "rule.disarmed", target=f"rule/{row.id}",
                           detail={"name": row.name})
    return _out(row)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: uuid.UUID, request: Request,
                      principal: Principal = Depends(_MANAGE),
                      db=Depends(get_db)) -> dict:
    """Alerts raised by this rule SURVIVE it — the FK is ON DELETE SET NULL and
    alerts.rule_name was denormalised in 022 for exactly this. Deleting a rule
    must never delete evidence."""
    row = await _get_or_404(db, rule_id)
    name = row.name
    await db.delete(row)
    await db.commit()
    await rules_engine.bump_version()
    await audit_svc.record(request, principal, "rule.deleted", target=f"rule/{rule_id}",
                           detail={"name": name})
    return {"deleted": str(rule_id)}


# ── The engine-wide kill switch ──────────────────────────────────────────────

@router.post("/disarm")
async def disarm_engine(body: ArmBody, request: Request,
                        principal: Principal = Depends(_MANAGE)) -> dict:
    """Stop every rule acting, in every worker, within one ingest batch.

    Alerts keep being raised — they are evidence, and this switch is about what
    the appliance DOES, not about what it saw.
    """
    await rules_engine.set_arm(False, by=principal.subject, reason=body.reason)
    await audit_svc.record(request, principal, "rules.disarmed",
                           detail={"reason": body.reason})
    return (await rules_engine.arm_state())._asdict()


@router.post("/rearm")
async def rearm_engine(body: ArmBody, request: Request,
                       principal: Principal = Depends(_MANAGE)) -> dict:
    await rules_engine.set_arm(True, by=principal.subject, reason=body.reason)
    await audit_svc.record(request, principal, "rules.rearmed",
                           detail={"reason": body.reason})
    return (await rules_engine.arm_state())._asdict()


@router.get("/arm-state")
async def read_arm_state() -> dict:
    """Readable by any authenticated principal, deliberately. The SPA shows a
    disarmed banner to everyone who can see the page, and an operator who
    cannot tell that automation is off is worse off than one who can.

    `degraded` is its own field and the UI must not collapse it into `armed`:
    "disarmed by Alice" and "we could not reach Valkey to find out" are
    different facts that call for different actions.
    """
    return (await rules_engine.arm_state())._asdict()
