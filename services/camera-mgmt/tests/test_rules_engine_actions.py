"""Rule actions reaching the bridge — and the five ways they must not.

P1 recorded `no_bridge` for every peripheral action because that was the truth.
P2 makes it a real dispatch, and these pin that every P1 safety property
survived the change: a log_only rule still moves nothing, a stale event still
moves nothing, a disarmed engine still moves nothing, a Valkey outage still
moves nothing — and the alert is still written in every one of those cases,
because evidence and actuation were never the same decision.

Run (from services/camera-mgmt): python -m pytest tests/test_rules_engine_actions.py -q
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import rules_engine  # noqa: E402
from backend.services.bridge import base  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
GATE = {"type": "peripheral", "peripheral": "gate-3-barrier", "command": "open"}


class SpyBridge:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or base.ActuationResult(True, "published")

    async def actuate(self, db, slug, command, params):
        self.calls.append((slug, command, params))
        return self.result

    async def pulse(self, db, slug, command, *, then, for_seconds, params,
                    alert_id, now):
        self.calls.append(("pulse", slug, command, then, for_seconds, params, alert_id))
        # What pending.pulse will do: commit a row citing alert_id. The alert
        # must already be in the session or that commit violates the FK.
        self.alert_in_session_at_pulse = any(
            getattr(o, "id", None) == alert_id for o in db.added)
        return self.result


from conftest import StubSession


class FakeSession(StubSession):
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None

    async def rollback(self):
        return None


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("valkey down")
        return self.store.get(key)

    async def set(self, key, value, **kw):
        if self.fail:
            raise ConnectionError("valkey down")
        if kw.get("nx") and key in self.store:
            return None
        self.store[key] = value
        return True

    async def incr(self, key):
        if self.fail:
            raise ConnectionError("valkey down")
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]


def rule(**kw):
    base_ = {"id": uuid.uuid4(), "name": "gate on intrusion", "enabled": True,
             "mode": "armed", "activity": "unauthorised_access",
             "scope_kind": "any", "scope_value": None, "min_confidence": None,
             "conditions": {}, "cooldown_s": 0, "max_event_age_s": 30,
             "severity": "warning", "actions": [dict(GATE)], "active_hours": None}
    base_.update(kw)
    return SimpleNamespace(**base_)


def event(**kw):
    base_ = {"id": uuid.uuid4(), "camera_id": uuid.uuid4(),
             "camera_slug": "cam2-o8iu", "activity": "unauthorised_access",
             "zone": "Zone 1", "confidence": 0.9, "object_class": "person",
             "attributes": {}, "started_at": NOW, "dedupe_key": None}
    base_.update(kw)
    return base_


@pytest.fixture
def valkey(monkeypatch):
    r = FakeRedis()

    async def _get():
        return r

    monkeypatch.setattr(rules_engine, "get_redis", _get)
    rules_engine._local_cooldown.clear()
    rules_engine._cache["version"] = None
    rules_engine._cache["rules"] = []
    return r


@pytest.fixture
def seeded(monkeypatch):
    """Put rules in the per-worker cache, so evaluate needs no database."""
    def _seed(*rules_):
        async def _load(db):
            return list(rules_), False
        monkeypatch.setattr(rules_engine, "_load_rules", _load)
    return _seed


@pytest.fixture
def quiet_audit(monkeypatch):
    seen = []

    async def record(request, principal, action, *, target=None, detail=None,
                     outcome="success"):
        seen.append({"action": action, "target": target, "detail": detail or {},
                     "outcome": outcome})

    monkeypatch.setattr(rules_engine.audit_svc, "record", record)
    return seen


# ── The happy path ───────────────────────────────────────────────────────────

@_aio
async def test_an_armed_rule_actuates(valkey, seeded, quiet_audit, monkeypatch):
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    seeded(rule(mode="armed"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert spy.calls == [("gate-3-barrier", "open", {})]
    assert db.added[0].notified["gate-3-barrier"] == "published"


@_aio
async def test_a_pulse_action_goes_through_pulse_not_actuate(valkey, seeded,
                                                             quiet_audit, monkeypatch):
    """`for_seconds` + `then` is a pulse, and a pulse's close must be durable
    before its open publishes. The engine used to strip both keys and call
    actuate(): a siren rule on the appliance fired `on` with no `off` behind
    it, and pending.pulse had no caller anywhere."""
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    siren = {"type": "peripheral", "peripheral": "site-siren", "command": "on",
             "for_seconds": 20, "then": "off"}
    seeded(rule(mode="armed", actions=[siren]))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    alert = db.added[0]
    assert spy.calls == [("pulse", "site-siren", "on", "off", 20, {}, alert.id)]
    assert alert.notified["site-siren"] == "published"
    # The ordering the appliance taught: pending.pulse commits a row that
    # cites the alert, so the alert has to be in the session first, or the
    # commit fails on the FK, the session rolls back and the ALERT is lost.
    assert spy.alert_in_session_at_pulse, "alert added after dispatch; pulse commit would hit the FK"


@_aio
async def test_no_bridge_is_retired_from_the_vocabulary(valkey, seeded,
                                                        quiet_audit, monkeypatch):
    """P1's placeholder. Its continued presence would mean the dispatch is not
    wired, and the row would say so for ever."""
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: SpyBridge())
    seeded(rule(mode="armed"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert "no_bridge" not in str(db.added[0].notified)


# ── The four gates, each of which must still hold ────────────────────────────

@_aio
async def test_a_log_only_rule_still_actuates_nothing(valkey, seeded, quiet_audit,
                                                      monkeypatch):
    """The P1 guarantee, and what makes a new rule safe to write at all."""
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    seeded(rule(mode="log_only"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert spy.calls == []
    assert db.added[0].notified["gate-3-barrier"] == "log_only"


@_aio
async def test_a_stale_event_still_actuates_nothing(valkey, seeded, quiet_audit,
                                                    monkeypatch):
    """A queue backlog must not open a gate ten minutes late — but the
    detection happened, so the alert is still evidence and is still raised."""
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    seeded(rule(mode="armed", max_event_age_s=30))
    db = FakeSession()
    ids = await rules_engine.evaluate(
        db, [event(started_at=NOW - timedelta(minutes=10))], now=NOW)
    assert spy.calls == []
    assert len(ids) == 1
    assert db.added[0].notified["_actuation"] == "stale_skipped"


@_aio
async def test_a_disarmed_engine_still_actuates_nothing(valkey, seeded,
                                                        quiet_audit, monkeypatch):
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    await rules_engine.set_arm(False, by="alice", reason="flapping")
    seeded(rule(mode="armed"))
    db = FakeSession()
    ids = await rules_engine.evaluate(db, [event()], now=NOW)
    assert spy.calls == []
    assert len(ids) == 1, "disarm stops actuation, never evidence"
    assert db.added[0].notified["_actuation"] == "disarmed"


@_aio
async def test_a_valkey_outage_still_actuates_nothing(valkey, seeded, quiet_audit,
                                                      monkeypatch):
    """FAIL CLOSED. A worker that cannot confirm the arm state must not move a
    barrier on the strength of a cached guess."""
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    seeded(rule(mode="armed"))
    db = FakeSession()
    valkey.fail = True
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert spy.calls == []


@_aio
async def test_an_unconfirmable_arm_state_is_not_reported_as_disarmed(
        valkey, seeded, quiet_audit, monkeypatch):
    """"Alice switched it off" and "we could not reach Valkey to find out" are
    different facts calling for different actions. The alert is the evidence
    record and must not attribute a machine failure to a human act — the same
    distinction the UI banner is careful to keep."""
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: SpyBridge())
    seeded(rule(mode="armed"))
    db = FakeSession()
    valkey.fail = True
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert db.added[0].notified["_actuation"] == "unconfirmed"


# ── Failure handling ─────────────────────────────────────────────────────────

@_aio
async def test_a_refused_actuation_is_recorded_with_its_reason(
        valkey, seeded, quiet_audit, monkeypatch):
    """"Was anyone actually told?" has to be answerable from the row alone."""
    spy = SpyBridge(base.ActuationResult(False, "binding_unverified"))
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    seeded(rule(mode="armed"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert db.added[0].notified["gate-3-barrier"] == "binding_unverified"


@_aio
async def test_a_bridge_that_raises_does_not_take_the_batch_with_it(
        valkey, seeded, quiet_audit, monkeypatch):
    """Still inside the ingest handler. Still must not 500 it."""
    class Exploding:
        async def actuate(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(rules_engine, "get_bridge", lambda: Exploding())
    seeded(rule(mode="armed"))
    db = FakeSession()
    ids = await rules_engine.evaluate(db, [event()], now=NOW)
    assert len(ids) == 1
    assert db.added[0].notified["gate-3-barrier"] == "dispatch_failed"


@_aio
async def test_a_rule_with_no_actions_still_raises_its_alert(valkey, seeded,
                                                             quiet_audit,
                                                             monkeypatch):
    """"Tell me, do nothing" is a legitimate and common rule."""
    spy = SpyBridge()
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: spy)
    seeded(rule(mode="armed", actions=[]))
    db = FakeSession()
    assert len(await rules_engine.evaluate(db, [event()], now=NOW)) == 1
    assert spy.calls == []


# ── The audit chain ──────────────────────────────────────────────────────────

@_aio
async def test_actuation_is_audited_with_the_citing_alert(valkey, seeded,
                                                          quiet_audit, monkeypatch):
    """THE DPDP chain. An auditor walks audit_log -> alerts.event_ids ->
    analytics_events -> the camera's lawful basis without leaving this
    database, and the first hop is this row."""
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: SpyBridge())
    seeded(rule(mode="armed"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    [entry] = [a for a in quiet_audit if a["action"] == "peripheral.actuated"]
    assert entry["detail"]["alert_id"] == str(db.added[0].id)
    assert entry["detail"]["command"] == "open"
    assert entry["detail"]["source"] == "rule"
    assert entry["outcome"] == "success"


@_aio
async def test_a_refused_actuation_is_audited_as_a_failure(valkey, seeded,
                                                           quiet_audit, monkeypatch):
    """An attempt that did nothing still has to leave a trace, or "the gate did
    not open" is unanswerable afterwards."""
    monkeypatch.setattr(rules_engine, "get_bridge",
                        lambda: SpyBridge(base.ActuationResult(False, "offline")))
    seeded(rule(mode="armed"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    [entry] = [a for a in quiet_audit if a["action"] == "peripheral.actuated"]
    assert entry["outcome"] == "failure"
    assert entry["detail"]["detail"] == "offline"


@_aio
async def test_nothing_is_audited_when_nothing_was_attempted(valkey, seeded,
                                                             quiet_audit,
                                                             monkeypatch):
    """A log_only rule did not try to actuate. An audit row saying it did would
    be the trail lying in the safest-looking direction."""
    monkeypatch.setattr(rules_engine, "get_bridge", lambda: SpyBridge())
    seeded(rule(mode="log_only"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    assert [a for a in quiet_audit if a["action"] == "peripheral.actuated"] == []
