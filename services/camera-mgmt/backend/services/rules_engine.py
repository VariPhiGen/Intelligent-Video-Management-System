"""rules_engine.py — the impure half of the rule engine.

services/rules.py decides WHETHER a rule matches, with no I/O and no ambient
clock. This module does everything that touches the world: the cross-worker arm
state, the rule cache and its version counter, the cooldown, and writing the
alert row.

WHY VALKEY, AND WHY FAIL-CLOSED HERE.

The API runs `uvicorn --workers 4` (Dockerfile.backend, docker-compose.yml). A
rule disabled in one worker that keeps firing in the other three is not a
staleness nuisance for a feature that opens gates — it is a correctness bug. So
the arm state and a version counter live in Valkey and every worker reads them.

services/leader.py solves the neighbouring problem (run a loop in ONE worker)
and is deliberately FAIL-OPEN: if Valkey is unreachable its loops run in every
worker, because a cache outage silently stopping recording reconciliation is
worse than doing it four times. That reasoning does not transfer here. These
are per-event safety gates, not idempotent loops. A worker that cannot confirm
the arm state, the rule version or the cooldown must not actuate. It still
raises alerts — the detection happened, and suppressing evidence would corrupt
the false-positive denominator — but it moves nothing.

Keep the two rules apart. They look similar and mean the opposite.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import NamedTuple, Optional

import structlog

from ..redis_client import get_redis

log = structlog.get_logger(__name__)

VERSION_KEY = "rules:version"
ARM_KEY = "rules:arm"


class ArmState(NamedTuple):
    armed: bool
    by: Optional[str]
    at: Optional[str]
    reason: Optional[str]
    #: Valkey was unreachable, or the record was unreadable. The arm state
    #: could not be CONFIRMED, so no caller may actuate on the strength of it.
    degraded: bool


async def bump_version() -> int:
    """Invalidate every worker's rule cache. Call after ANY rule write."""
    r = await get_redis()
    return int(await r.incr(VERSION_KEY))


async def current_version() -> Optional[int]:
    """The cache generation, or None.

    None is not "version 0" — it means UNCONFIRMED. A caller holding cached
    rules must treat them as possibly stale and refuse to actuate on them.
    """
    try:
        r = await get_redis()
        raw = await r.get(VERSION_KEY)
        return int(raw) if raw is not None else 0
    except Exception as exc:                                    # noqa: BLE001
        log.warning("rules.version_unavailable", error=str(exc))
        return None


async def set_arm(armed: bool, *, by: str, reason: str) -> None:
    """Disarm or re-arm the whole engine, for every worker, without a restart.

    Bumps the version too, so a worker holding cached rules re-reads on its
    next batch rather than at some later invalidation.
    """
    r = await get_redis()
    await r.set(ARM_KEY, json.dumps({
        "armed": armed, "by": by, "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }))
    await r.incr(VERSION_KEY)
    # warning, not info: an automation system changing arm state is something
    # the next person on shift needs to find in the log without grepping.
    log.warning("rules.arm_changed", armed=armed, by=by, reason=reason)


async def arm_state() -> ArmState:
    """The engine's arm state. Unreadable == not armed for actuation."""
    try:
        r = await get_redis()
        raw = await r.get(ARM_KEY)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("rules.arm_unavailable", error=str(exc))
        return ArmState(False, None, None, None, True)
    if raw is None:
        # Never disarmed. Armed is the default, and this is a CONFIRMED read.
        return ArmState(True, None, None, None, False)
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("arm record is not an object")
    except (ValueError, TypeError) as exc:
        # Garbage in the key must not read as "armed". Unparseable is unknown.
        log.warning("rules.arm_corrupt", error=str(exc))
        return ArmState(False, None, None, None, True)
    return ArmState(bool(d.get("armed", True)), d.get("by"), d.get("at"),
                    d.get("reason"), False)


async def may_actuate() -> bool:
    """May this worker move something right now?

    False when disarmed, and false when the answer could not be confirmed. In
    P1 nothing can actuate at all — there is no bridge — but the gate lands now
    so the bridge has nowhere to forget it.
    """
    st = await arm_state()
    return st.armed and not st.degraded


# ── Firing ────────────────────────────────────────────────────────────────────

import asyncio                                                 # noqa: E402
import time as _time                                          # noqa: E402
import uuid                                                    # noqa: E402
from datetime import timedelta                                # noqa: E402
from typing import Any, Mapping, Sequence                      # noqa: E402

from sqlalchemy import delete, select                          # noqa: E402

from ..config import settings                                  # noqa: E402
from ..models import Alert, AlertRule                          # noqa: E402
from . import audit as audit_svc                               # noqa: E402
from . import rules                                            # noqa: E402
from .bridge import get_bridge                                 # noqa: E402

#: Per-worker fallback, used ONLY when Valkey is unreachable. Up to 4x looser
#: than the configured cooldown because each worker keeps its own — an
#: acceptable amount of extra evidence, and recorded on the alert so the
#: looseness is visible in the data rather than inferred.
_local_cooldown: dict = {}

#: Per-worker rule cache, keyed on the Valkey version counter.
_cache: dict = {"version": None, "rules": []}


async def check_cooldown(rule_id, dedupe_key: str, cooldown_s: int):
    """(allowed, degraded). A cooldown of 0 never suppresses.

    SET NX EX is the whole rate limiter: the first caller to claim the key wins
    and the key expires on its own. On outage this degrades to a per-worker
    dict rather than failing open, because a rate limiter that fails open is
    how a relay gets hammered.
    """
    if cooldown_s <= 0:
        return True, False
    key = f"rules:cd:{rule_id}:{dedupe_key}"
    try:
        r = await get_redis()
        got = await r.set(key, "1", nx=True, ex=cooldown_s)
        return bool(got), False
    except Exception as exc:                                    # noqa: BLE001
        log.warning("rules.cooldown_degraded", error=str(exc))
        now = _time.monotonic()
        k = (rule_id, dedupe_key)
        last = _local_cooldown.get(k)
        if last is not None and now - last < cooldown_s:
            return False, True
        _local_cooldown[k] = now
        return True, True


async def _load_rules(db):
    """(rules, unconfirmed). Reloads when the Valkey version moved.

    `unconfirmed` is True when the version could not be read: the cached rules
    may be stale — a rule disarmed in another worker may still be in this
    worker's copy — so the caller must not actuate on them.
    """
    version = await current_version()
    if version is None:
        if _cache["rules"]:
            return _cache["rules"], True
        rows = (await db.execute(select(AlertRule).where(AlertRule.enabled))).scalars().all()
        return list(rows), True
    if _cache["version"] != version:
        rows = (await db.execute(select(AlertRule).where(AlertRule.enabled))).scalars().all()
        _cache["version"] = version
        _cache["rules"] = list(rows)
    return _cache["rules"], False


def _dedupe_key(event: Mapping) -> str:
    """Prefer the producer's key; fall back to the natural composite.

    A NULL dedupe_key is common — not every activity mints one — and a cooldown
    that silently did nothing for those would let a busy tripwire through.
    """
    return event.get("dedupe_key") or \
        f"{event.get('camera_slug')}:{event.get('activity')}:{event.get('zone') or ''}"


def _why_not(decision, arm, unconfirmed: bool) -> Optional[str]:
    """Why this alert's actions will not run, or None if they will.

    FOUR DISTINCT REASONS, and collapsing any pair of them would make the
    evidence record lie:

      stale_skipped — the detection is older than the rule tolerates. It
                      happened, so the alert stands; a queue backlog must not
                      open a gate ten minutes late.
      log_only      — nobody has armed this rule yet.
      disarmed      — a person switched the engine off, and their name and
                      reason are in the arm state.
      unconfirmed   — Valkey could not be reached, so the arm state is UNKNOWN.
                      NOT the same as disarmed: reporting a machine failure as
                      a human act sends whoever reads the row looking for
                      somebody who did nothing. (Recording this as "disarmed"
                      was observed on the appliance 2026-09-18.)
    """
    if decision.reason == "stale":
        return "stale_skipped"
    if decision.reason == "log_only":
        return "log_only"
    if arm.degraded or unconfirmed:
        return "unconfirmed"
    if not arm.armed:
        return "disarmed"
    if not decision.actuate:
        return "log_only"
    return None


async def dispatch_actions(db, rule, alert, decision, arm, unconfirmed: bool,
                           cd_degraded: bool, request=None, event=None) -> dict:
    """Run this rule's actions, and return the `notified` map.

    `alerts.notified` is the JSONB migration 022 added so "was anyone actually
    told?" is answerable without correlating service logs. Since P2 it records
    what the bridge actually did, not a placeholder.

    NEVER RAISES. The caller is the ingest handler, and a driver written later
    must not be able to cost the rest of the batch its events.
    """
    out: dict = {}
    blocked = _why_not(decision, arm, unconfirmed)
    if blocked is not None:
        out["_actuation"] = blocked
    if cd_degraded:
        out["_cooldown"] = "degraded"

    for action in (rule.actions or []):
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        if kind == "peripheral":
            key = action.get("peripheral") or "?"
        elif kind == "webhook":
            key = action.get("url_ref") or "webhook"
        else:
            continue

        if blocked is not None:
            # Recorded, not attempted. The row says which gate stopped it.
            out[key] = blocked
            continue
        if kind != "peripheral":
            # Webhook actions are carried by the bridge's webhook driver only
            # when a binding names it; a bare url_ref has no transport in P2.
            out[key] = "no_transport"
            continue

        command = action.get("command") or "open"
        params = {k: v for k, v in action.items()
                  if k not in ("type", "peripheral", "command",
                               "for_seconds", "then", "until", "grace_s")}
        for_seconds, then = action.get("for_seconds"), action.get("then")
        until = action.get("until")
        try:
            if until == "zone_empty" and then and event is not None:
                # Held for as long as the zone is occupied; for_seconds is only
                # the safety cap. The occupancy loop (bridge/pending.py) has
                # usually switched it on already, from the per-frame count:
                # this confirmed event lands 1-2 s later and then only finds
                # the hold running ("held"). If the person has ALREADY left,
                # switching on now would chirp the siren, so it does not.
                from .bridge import pending as _pending
                camera = str(event.get("camera_slug") or "")
                zone = event.get("zone") or None
                reading = await _pending.read_presence(camera)
                if reading is not None and _pending.occupied(
                        reading, zone, _time.time()) == 0:
                    out[key] = "zone_already_empty"
                    continue
                grace = action.get("grace_s")
                result = await get_bridge().hold(
                    db, key, command, then=str(then), camera=camera, zone=zone,
                    cap_seconds=int(for_seconds or _pending.HOLD_CAP_S),
                    grace_s=int(_pending.HOLD_GRACE_S if grace is None else grace),
                    params=params, alert_id=alert.id, now=alert.created_at)
            elif for_seconds and then:
                # A pulse: the close is committed to pending_actions BEFORE
                # the open publishes, so a worker dying mid-alarm cannot
                # leave a siren sounding with no record of why.
                result = await get_bridge().pulse(
                    db, key, command, then=str(then), for_seconds=int(for_seconds),
                    params=params, alert_id=alert.id, now=alert.created_at)
            else:
                result = await get_bridge().actuate(db, key, command, params)
            detail, ok = result.detail, result.ok
        except Exception as exc:                                # noqa: BLE001
            log.warning("rules.dispatch_failed", peripheral=key, error=str(exc))
            detail, ok = "dispatch_failed", False
        out[key] = detail

        # Audited whether it worked or not: an attempt that did nothing still
        # has to leave a trace, or "the gate did not open" is unanswerable.
        # THE DPDP CHAIN STARTS HERE — alert_id is the hop into
        # alerts.event_ids and on to the camera's lawful basis.
        try:
            await audit_svc.record(
                request, None, "peripheral.actuated",
                target=f"peripheral/{key}",
                detail={"alert_id": str(alert.id), "rule": rule.name,
                        "command": command, "source": "rule", "detail": detail},
                outcome="success" if ok else "failure")
        except Exception as exc:                                # noqa: BLE001
            log.warning("rules.actuation_audit_failed", peripheral=key,
                        error=str(exc))
    return out


def _as_mapping(rule) -> dict:
    """The column subset services/rules.py reads. Listed explicitly rather than
    taken from __dict__ so a schema change cannot silently alter matching."""
    return {"enabled": rule.enabled, "mode": rule.mode, "activity": rule.activity,
            "scope_kind": rule.scope_kind, "scope_value": rule.scope_value,
            "min_confidence": rule.min_confidence, "conditions": rule.conditions,
            "max_event_age_s": rule.max_event_age_s, "active_hours": rule.active_hours}


async def evaluate(db, events: Sequence[Mapping], *, now: datetime) -> list:
    """Evaluate newly-inserted events and raise the alerts they earn.

    NEVER RAISES. This runs inside the ingest handler, so a bad rule must cost
    its own alert and not the rest of the batch's events. Returns the ids of
    the alerts it created.
    """
    if not settings.rules_enabled or not events:
        return []
    try:
        loaded, unconfirmed = await _load_rules(db)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("rules.load_failed", error=str(exc))
        return []

    arm = await arm_state()
    raised: list = []

    for event in events:
        for rule in loaded:
            try:
                decision = rules.should_fire(
                    _as_mapping(rule), event, now, site_tz=settings.site_timezone)
                if not decision.fires:
                    continue
                allowed, cd_degraded = await check_cooldown(
                    rule.id, _dedupe_key(event), rule.cooldown_s)
                if not allowed:
                    continue

                # The alert is built FIRST, ADDED TO THE SESSION, and only
                # then is dispatch told about it: the actuation audit and a
                # pulse's follow-up row both cite the alert id, and a pulse
                # COMMITS its row before the open publishes. With the alert
                # added afterwards that commit hit the FK, the session rolled
                # back, and the alert itself was lost — an actuation that
                # cost its own evidence. Seen on the appliance with the first
                # siren rule. Evidence goes in before anything moves.
                alert = Alert(
                    id=uuid.uuid4(), rule_id=rule.id, rule_name=rule.name,
                    event_ids=[event["id"]], camera_id=event["camera_id"],
                    severity=rule.severity, state="new",
                    triggered_at=event.get("started_at") or now,
                    notified={}, created_at=now,
                )
                db.add(alert)
                from .event_delivery.envelope import CameraRef, alert_envelope
                from .event_delivery.outbox import enqueue
                await enqueue(db, alert_envelope(alert, CameraRef(event['camera_id'],
                    event.get('camera_slug') or '', event.get('camera_name') or event.get('camera_slug') or ''),
                    accepted_at=now))
                alert.notified = await dispatch_actions(
                    db, rule, alert, decision, arm, unconfirmed, cd_degraded,
                    event=event)
                raised.append(alert.id)
            except Exception as exc:                            # noqa: BLE001
                log.warning("rules.evaluate_failed", rule=str(getattr(rule, "id", "?")),
                            error=str(exc))

    if raised:
        try:
            await db.commit()
        except Exception as exc:                                # noqa: BLE001
            log.warning("rules.alert_commit_failed", error=str(exc))
            await db.rollback()
            return []
    return raised


# ── Retention ────────────────────────────────────────────────────────────────

async def purge_expired_alerts(db, *, keep_days: int) -> int:
    """Sweep alerts that nobody resolved and nobody held.

    Migration 022 gave `alerts` no `expires_at`, unlike analytics_events, and
    that was right: alerts are a small working set that may sit under legal
    hold for years, so stamping a deadline at insert would be guessing. It is
    not a reason to keep them for ever.

    TWO EXEMPTIONS, BOTH LOAD-BEARING:

      legal_hold  evidence lock. Never swept, at any age. This is the one
                  retention rule in the product that is not negotiable.
      resolved    the record of what an operator actually DECIDED. Sweeping
                  these by age would delete the denominator that makes a
                  per-rule false-positive rate computable — the number the
                  resolution column exists to produce.

    So what ages out is precisely the noise: alerts that fired, nobody looked
    at, and nobody needed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    result = await db.execute(
        delete(Alert).where(
            Alert.triggered_at < cutoff,
            Alert.legal_hold.is_(False),
            Alert.resolved_at.is_(None),
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


async def alert_retention_loop() -> None:
    """Hourly sweep. Started under leader.run_as_leader — see main.py.

    FAIL-OPEN IS CORRECT HERE, and it is the opposite of the rule this module's
    header sets out for the arm state. That one is a per-event safety gate: a
    worker that cannot confirm it must not act. This is an idempotent loop, and
    a DELETE running in four workers deletes the same rows once. Keep the two
    rules apart; they look similar and mean the opposite.
    """
    from ..db import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await purge_expired_alerts(
                    db, keep_days=settings.alert_retention_days)
            if n:
                log.info("alerts.retention.swept", removed=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("alerts.retention.failed", error=str(exc))
        await asyncio.sleep(3600)
