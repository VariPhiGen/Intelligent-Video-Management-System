"""pending.py — the follow-up half of a pulse, made durable.

"Open the barrier for 8 seconds" is two commands, and the API runs four uvicorn
workers: the one that opened it may be gone before the close is due. So the
follow-up is a committed row, written BEFORE the first half publishes.

DEFENCE IN DEPTH, NOT THE SAFETY MECHANISM. The PLC owns the authoritative
timeout that closes the barrier (spec 11). Nothing here weakens that, and no
deployment should depend on this instead of it. What this buys is that a worker
restart during a deploy does not leave a gate open with no record of why.

The sweep needs no lock. Claiming is `UPDATE ... WHERE state='pending'
RETURNING`, so the loser of a race updates zero rows and does nothing.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple, Optional

import structlog
from sqlalchemy import text

from ...models import PendingAction
from ...config import settings
from .base import ActuationResult

log = structlog.get_logger(__name__)

#: After this many failures the follow-up is abandoned AND an operator is told.
#: Silently giving up on an un-closed barrier is the worst available behaviour.
MAX_ATTEMPTS = 5

#: How often the sweep looks. Short, because the thing it is late for is a
#: barrier that is still open — and, for a hold, a siren still sounding after
#: the zone emptied.
SWEEP_INTERVAL_S = 1

#: "Hold until the zone is empty" (migration 080) — the occupancy loop.
#: It reads the PER-FRAME zone count, which needs no tracker confirmation: the
#: confirmed analytics event arrives ~1-2 s after a person steps in, which is
#: too late to switch a siren on. So the loop switches ON from the count and
#: OFF the moment it reads zero; the event, when it lands, only writes the
#: alert and finds the hold already running.
OCCUPANCY_POLL_S = 0.25
#: Seconds a zone must read empty before the OFF. 0 = at once.
HOLD_GRACE_S = 0
HOLD_CAP_S = 600
#: No fresh frame for this long and the zone counts as empty: a camera or
#: analytics outage must END a hold, never freeze a siren on.
PRESENCE_STALE_S = 3.0
#: GUARD RAILS so no call can stick the loop or a device:
#: any one device call is abandoned after this long ...
CALL_TIMEOUT_S = 3.0
#: ... a presence read after this long ...
PRESENCE_TIMEOUT_S = 1.0
#: ... and a device whose ON failed is not re-tried on every tick.
RETRY_AFTER_FAIL_S = 5.0


class Due(NamedTuple):
    id: uuid.UUID
    peripheral_id: int
    slug: str
    command: str
    params: dict
    attempts: int
    alert_id: Optional[uuid.UUID]


async def pulse(db, *, peripheral_id: int, slug: str, command: str, then: str,
                for_seconds: int, params: dict, alert_id, now: datetime,
                publish: Callable) -> ActuationResult:
    """Open-then-close, with the close made durable FIRST.

    The ordering is the whole point and is pinned by a test: publish first and
    die, and there is no record that a barrier was ever opened.
    """
    row = PendingAction(
        id=uuid.uuid4(), alert_id=alert_id, peripheral_id=peripheral_id,
        command=then, params=params or {},
        due_at=now + timedelta(seconds=for_seconds), state="pending",
        created_at=now,
    )
    db.add(row)
    await db.commit()

    result = await publish(slug, command, params)
    if not result.ok:
        # Never opened, so nothing to close. Withdraw the follow-up rather than
        # leave a row that will retry, fail, and finally raise a critical alert
        # for a barrier that never moved.
        row.state = "abandoned"
        row.last_error = f"open_failed:{result.detail}"[:200]
        await db.commit()
    return result


async def hold(db, *, peripheral_id: int, slug: str, command: str, then: str,
               cap_seconds: int, grace_s: int, camera: str, zone: Optional[str],
               params: dict, alert_id, now: datetime, publish: Callable) -> ActuationResult:
    """ON now; OFF once `zone` on `camera` reads empty (after `grace_s`).

    The same ordering as pulse(): the OFF row is committed before the ON is
    sent, with `due_at` as a hard cap. ONE HOLD PER DEVICE: if one is already
    running, this only refreshes its cap and sends nothing — the device is on,
    and re-sending would undo an operator's manual OFF on every tick.
    """
    from sqlalchemy import select

    row = (await db.execute(
        select(PendingAction).where(
            PendingAction.peripheral_id == peripheral_id,
            PendingAction.state == "pending",
            PendingAction.hold_camera.isnot(None))
        .order_by(PendingAction.created_at.desc()).limit(1)
    )).scalars().first()
    due = now + timedelta(seconds=cap_seconds)
    if row is not None:
        row.due_at, row.empty_since = due, None
        await db.commit()
        return ActuationResult(True, "held")
    row = PendingAction(
        id=uuid.uuid4(), alert_id=alert_id, peripheral_id=peripheral_id,
        command=then, params=params or {}, due_at=due, state="pending",
        created_at=now, hold_camera=camera, hold_zone=zone,
        hold_grace_s=grace_s, empty_since=None)
    db.add(row)
    await db.commit()

    result = await publish(slug, command, params)
    if not result.ok:
        # Never switched on, so nothing to switch off (see pulse()).
        row.state = "abandoned"
        row.last_error = f"open_failed:{result.detail}"[:200]
        await db.commit()
    return result


def occupied(presence: Optional[dict], zones: Optional[str], now_ts: float) -> int:
    """People in `zones` per a presence reading; 0 when it is missing or stale.

    `zones` is one zone or a comma list, each matched by id or by name; None
    or empty = every drawn zone. The whole-frame count is never a zone:
    someone walking past outside the zone must not hold a siren about it."""
    if not presence or now_ts - float(presence.get("ts") or 0) > PRESENCE_STALE_S:
        return 0
    wanted = {z.strip() for z in (zones or "").split(",") if z.strip()}
    total = 0
    for zid, z in (presence.get("zones") or {}).items():
        if zid == "__frame__":
            continue
        if not wanted or zid in wanted or z.get("name") in wanted:
            total += int(z.get("count") or 0)
    return total


def zones_for(analytics_config: Optional[dict], activity: str) -> Optional[str]:
    """The zones a camera assigns to `activity`, as a comma list of region ids.
    None = the activity has none of its own, so every drawn zone counts."""
    ids: list[str] = []
    for act in (analytics_config or {}).get("activities") or []:
        if isinstance(act, dict) and act.get("type") == activity:
            ids.extend(str(r) for r in act.get("regions") or [])
    return ",".join(dict.fromkeys(ids)) or None


class Target(NamedTuple):
    """One device an armed hold rule wants on while its zone is occupied."""
    rule_name: str
    peripheral: str
    command: str
    then: str
    cap_seconds: int
    grace_s: int
    camera: str
    zones: Optional[str]


def hold_targets(rules, configs: dict, now: datetime, *, site_tz: str) -> list:
    """Every (rule, device) the occupancy loop is responsible for right now:
    enabled, ARMED, camera-scoped rules, inside their active hours, whose
    action holds `until` the zone is empty."""
    from ..rules import within_active_hours

    out = []
    for rule in rules:
        if not rule.enabled or rule.mode != "armed" or rule.scope_kind != "camera":
            continue
        if rule.scope_value not in configs:
            continue
        if not within_active_hours(rule.active_hours, now, site_tz=site_tz):
            continue
        for a in rule.actions or []:
            if (isinstance(a, dict) and a.get("type") == "peripheral"
                    and a.get("until") == "zone_empty" and a.get("then")):
                grace = a.get("grace_s")
                out.append(Target(
                    rule.name, str(a.get("peripheral")), str(a.get("command") or "on"),
                    str(a["then"]), int(a.get("for_seconds") or HOLD_CAP_S),
                    int(HOLD_GRACE_S if grace is None else grace), rule.scope_value,
                    zones_for(configs[rule.scope_value], rule.activity)))
    return out


def decide(targets: list, holds: list, readings: dict, *, now: datetime,
           busy: set) -> tuple[list, list]:
    """(targets to switch ON, holds to end now). Pure, so it is testable.

    `holds` are pending hold rows carrying `.slug`; `busy` is every device a
    call is already in flight for, or that is waiting out a failure."""
    ts = now.timestamp()
    held = {h.slug for h in holds}
    start: list = []
    for t in targets:
        if t.peripheral in held or t.peripheral in busy:
            continue
        if occupied(readings.get(t.camera), t.zones, ts) > 0:
            start.append(t)
            held.add(t.peripheral)          # two rules, one siren: one ON
    end: list = []
    for h in holds:
        if occupied(readings.get(h.hold_camera), h.hold_zone, ts) > 0:
            h.empty_since = None
            continue
        if h.empty_since is None:
            h.empty_since = now
        if (now - h.empty_since).total_seconds() >= (h.hold_grace_s or 0) and h.due_at > now:
            h.due_at = now                  # only ever pulled FORWARD
            end.append(h)
    return start, end


async def read_presence(camera: str) -> Optional[dict]:
    """Live zone occupancy from analytics (GET /cameras/{slug}/presence).
    None on any failure or timeout, which every caller treats as empty."""
    from .. import analytics_client
    if not analytics_client.is_configured():
        return None
    try:
        resp = await asyncio.wait_for(analytics_client._get_client().get(
            f"{analytics_client._base()}/cameras/{camera}/presence"), PRESENCE_TIMEOUT_S)
    except Exception as exc:                                    # noqa: BLE001
        log.info("bridge.hold.presence_unavailable", camera=camera, error=str(exc))
        return None
    return resp.json() if resp.status_code == 200 else None


class _LoopState:
    def __init__(self) -> None:
        self.in_flight: set[str] = set()
        self.retry_after: dict[str, float] = {}
        self.sweeping = False

    def busy(self, now_mono: float) -> set:
        waiting = {k for k, t in self.retry_after.items() if t > now_mono}
        return self.in_flight | waiting


async def _start_hold(t: Target, state: _LoopState) -> None:
    """Switch one device ON for a hold, in its own session and its own time
    budget: a dead device must not stall the loop or any other device."""
    import time
    from ...db import AsyncSessionLocal
    from .. import audit as audit_svc
    from . import get_bridge

    try:
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(get_bridge().hold(
                db, t.peripheral, t.command, then=t.then, camera=t.camera,
                zone=t.zones, cap_seconds=t.cap_seconds, grace_s=t.grace_s),
                CALL_TIMEOUT_S)
        ok, detail = result.ok, result.detail
    except asyncio.TimeoutError:
        ok, detail = False, "timeout"
    except Exception as exc:                                    # noqa: BLE001
        log.warning("bridge.hold.start_failed", peripheral=t.peripheral, error=str(exc))
        ok, detail = False, "start_failed"
    if not ok:
        state.retry_after[t.peripheral] = time.monotonic() + RETRY_AFTER_FAIL_S
    else:
        state.retry_after.pop(t.peripheral, None)
    state.in_flight.discard(t.peripheral)
    if detail == "held":
        return                               # already on: nothing was sent
    try:
        await audit_svc.record(
            None, None, "peripheral.actuated", target=f"peripheral/{t.peripheral}",
            detail={"rule": t.rule_name, "command": t.command, "source": "occupancy",
                    "camera": t.camera, "detail": detail},
            outcome="success" if ok else "failure")
    except Exception as exc:                                    # noqa: BLE001
        log.warning("bridge.hold.audit_failed", error=str(exc))


async def _sweep_now(state: _LoopState) -> None:
    """Send the OFFs just made due, now rather than at the next sweep tick."""
    from ...db import AsyncSessionLocal
    from . import get_bridge

    try:
        async with AsyncSessionLocal() as db:
            async def _actuate(slug, command, params):
                return await asyncio.wait_for(
                    get_bridge().actuate(db, slug, command, params), CALL_TIMEOUT_S)
            await asyncio.wait_for(sweep(db, actuate=_actuate, raise_alert=_raise_unclosed,
                                         audit=_audit_follow_up), 4 * CALL_TIMEOUT_S)
    except Exception as exc:                                    # noqa: BLE001
        # The row is still due; the regular 1 s sweep picks it up.
        log.warning("bridge.hold.sweep_now_failed", error=str(exc))
    finally:
        state.sweeping = False


async def occupancy_tick(db, state: _LoopState, *, now: datetime,
                         read: Callable = None) -> tuple[int, int]:
    """One pass: start the holds whose zone is occupied, end the ones whose
    zone is empty. Returns (started, ended). Device calls run as tasks."""
    import time
    from sqlalchemy import select
    from ...models import Camera, Peripheral
    from .. import rules_engine

    read = read or read_presence
    rules, unconfirmed = await rules_engine._load_rules(db)
    # Starting a hold MOVES something, so it passes the same gate as any rule;
    # ENDING one only switches off, so it never waits on the gate.
    targets: list = []
    if not unconfirmed and await rules_engine.may_actuate():
        cams = {r.scope_value for r in rules if r.scope_kind == "camera"}
        configs = dict((await db.execute(
            select(Camera.slug, Camera.analytics_config).where(Camera.slug.in_(cams))
        )).all()) if cams else {}
        targets = hold_targets(rules, configs, now, site_tz=settings.site_timezone)
    rows = (await db.execute(
        select(PendingAction, Peripheral.slug)
        .join(Peripheral, Peripheral.id == PendingAction.peripheral_id)
        .where(PendingAction.state == "pending", PendingAction.hold_camera.isnot(None))
    )).all()
    holds = []
    for row, slug in rows:
        row.slug = slug
        holds.append(row)
    cams = {t.camera for t in targets} | {h.hold_camera for h in holds}
    if not cams:
        return 0, 0
    readings = dict(zip(cams, await asyncio.gather(*(read(c) for c in cams))))
    start, end = decide(targets, holds, readings, now=now,
                        busy=state.busy(time.monotonic()))
    await db.commit()
    for t in start:
        state.in_flight.add(t.peripheral)
        asyncio.create_task(_start_hold(t, state), name=f"hold-on-{t.peripheral}")
    if end and not state.sweeping:
        state.sweeping = True
        asyncio.create_task(_sweep_now(state), name="hold-off")
    return len(start), len(end)


async def hold_loop() -> None:
    """Runs under leader.run_as_leader — see main.py. One pass every
    OCCUPANCY_POLL_S; a failed pass is logged and the next one runs."""
    from ...db import AsyncSessionLocal

    state = _LoopState()
    while True:
        try:
            async with AsyncSessionLocal() as db:
                started, ended = await occupancy_tick(db, state, now=datetime.now(timezone.utc))
            if started or ended:
                log.info("bridge.hold.occupancy", started=started, ended=ended)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.hold.loop_failed", error=str(exc))
        await asyncio.sleep(OCCUPANCY_POLL_S)


async def claim_due(db, *, limit: int = 50) -> list:
    """Claim due rows for THIS worker, and return them as `Due` tuples.

    No lock needed: the guard is in the UPDATE, so a concurrent sweep updates
    zero rows and does nothing. `FOR UPDATE SKIP LOCKED` keeps two sweeps from
    blocking on each other rather than merely from duplicating work.
    """
    rows = (await db.execute(text(
        "UPDATE pending_actions p SET state='claimed', attempts = attempts + 1 "
        "FROM peripherals per "
        "WHERE p.peripheral_id = per.id AND p.id IN ("
        "  SELECT id FROM pending_actions "
        "  WHERE state='pending' AND due_at <= now() "
        "  ORDER BY due_at LIMIT :lim FOR UPDATE SKIP LOCKED) "
        "RETURNING p.id, p.peripheral_id, per.slug, p.command, p.params, "
        "          p.attempts, p.alert_id"
    ), {"lim": limit})).all()
    await db.commit()
    return [Due(id=r[0], peripheral_id=r[1], slug=r[2], command=r[3],
                params=r[4] or {}, attempts=r[5], alert_id=r[6]) for r in rows]


async def execute_one(db, row: Due, *, actuate: Callable,
                      raise_alert: Callable, audit: Optional[Callable] = None) -> str:
    """Run one follow-up. Returns its new state.

    An exhausted follow-up does not fail quietly: an un-closed barrier is
    exactly the condition an operator must be told about, and the alert is
    raised on the crash path as well as the refusal path — a driver that
    explodes every time leaves the gate just as open as one that refuses.

    AUDITED LIKE EVERY OTHER ACTUATION, success or failure, citing the alert
    that opened the pulse. The siren stopping at :25 is as much a fact about
    the incident as it starting at :03, and a close that failed is the one
    the operator will be asked about.
    """
    try:
        result = await actuate(row.slug, row.command, row.params)
        ok, detail = result.ok, result.detail
    except Exception as exc:                                    # noqa: BLE001
        log.warning("bridge.pending.actuate_raised", peripheral=row.slug,
                    error=str(exc))
        ok, detail = False, "driver_error"
    if audit is not None:
        try:
            await audit(row, ok, detail)
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.pending.audit_failed", peripheral=row.slug,
                        error=str(exc))
    if ok:
        return "done"
    if row.attempts >= MAX_ATTEMPTS:
        log.error("bridge.pending.abandoned", peripheral=row.slug,
                  command=row.command, detail=detail)
        raise_alert(severity="critical", peripheral=row.slug,
                    command=row.command, detail=detail, alert_id=row.alert_id)
        return "abandoned"
    return "pending"


async def sweep(db, *, actuate: Callable, raise_alert: Callable,
                audit: Optional[Callable] = None) -> int:
    """One pass. Returns how many follow-ups were executed."""
    due = await claim_due(db)
    for row in due:
        state = await execute_one(db, row, actuate=actuate,
                                  raise_alert=raise_alert, audit=audit)
        await db.execute(text(
            "UPDATE pending_actions SET state = :s, last_error = :e "
            "WHERE id = :i"),
            {"s": state, "e": None if state == "done" else "retry", "i": row.id})
    if due:
        await db.commit()
    return len(due)


def _raise_unclosed(**kw) -> None:
    # P2 logs it; wiring this to a real Alert row needs a camera_id the
    # pending row does not carry, and inventing one would put a fabricated
    # camera on an operator's screen. The log line is the honest half.
    log.error("bridge.pending.unclosed", **kw)


async def _audit_follow_up(row, ok, detail) -> None:
    # Same action and shape as the rule's open, so the two halves of a pulse
    # (or a hold) sit next to each other in the log.
    from .. import audit as audit_svc
    await audit_svc.record(
        None, None, "peripheral.actuated", target=f"peripheral/{row.slug}",
        detail={"alert_id": str(row.alert_id) if row.alert_id else None,
                "command": row.command, "source": "follow_up",
                "attempt": row.attempts, "detail": detail},
        outcome="success" if ok else "failure")


async def sweep_loop() -> None:
    """Runs under leader.run_as_leader — see main.py.

    FAIL-OPEN IS CORRECT HERE: this is a loop, not a gate, and `close` is
    idempotent, so several workers closing the same barrier is wasteful rather
    than wrong. The opposite of `rules_engine.may_actuate`, which is a gate and
    fails closed. The two rules look alike and mean the opposite.
    """
    from ...db import AsyncSessionLocal
    from . import get_bridge

    while True:
        try:
            async with AsyncSessionLocal() as db:
                async def _actuate(slug, command, params):
                    return await asyncio.wait_for(
                        get_bridge().actuate(db, slug, command, params), CALL_TIMEOUT_S)

                n = await sweep(db, actuate=_actuate, raise_alert=_raise_unclosed,
                                audit=_audit_follow_up)
            if n:
                log.info("bridge.pending.swept", executed=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.pending.sweep_failed", error=str(exc))
        await asyncio.sleep(SWEEP_INTERVAL_S)
