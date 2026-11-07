"""The rule engine firing: cooldown collapse, alert rows, and what a Valkey
outage does to each half.

The ASYMMETRY under outage is the subtle part and has its own test. Suppressing
alerts would lose evidence; raising every one would flood the table; letting
actions through unchecked would hammer a relay. So alerts fall back to a
per-worker cooldown and actuation is refused outright.

What lands in a real database is test_alert_rules_db.py's job. These are the
decisions, against a session that only records what it was asked to store.

Run (from services/camera-mgmt): python -m pytest tests/test_rules_engine_fire.py -q
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import StubRedis  # noqa: E402

from backend.services import rules_engine  # noqa: E402

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
CAM = uuid.uuid4()


class Flaky(StubRedis):
    fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("valkey is down")

    async def get(self, key):
        self._check()
        return await super().get(key)

    async def set(self, key, value, **kw):
        self._check()
        return await super().set(key, value, **kw)

    async def incr(self, key):
        self._check()
        return await super().incr(key)


class FakeRule:
    """The columns services/rules.py and the engine read. A real AlertRule row
    minus SQLAlchemy, so these tests need no database."""

    def __init__(self, **kw):
        self.id = kw.get("id") or uuid.uuid4()
        self.name = kw.get("name", "gate on intrusion")
        self.enabled = kw.get("enabled", True)
        self.mode = kw.get("mode", "armed")
        self.activity = kw.get("activity", "unauthorised_access")
        self.scope_kind = kw.get("scope_kind", "any")
        self.scope_value = kw.get("scope_value")
        self.min_confidence = kw.get("min_confidence")
        self.conditions = kw.get("conditions", {})
        self.cooldown_s = kw.get("cooldown_s", 60)
        self.max_event_age_s = kw.get("max_event_age_s", 30)
        self.severity = kw.get("severity", "warning")
        self.actions = kw.get("actions", [
            {"type": "peripheral", "peripheral": "gate-3-barrier",
             "command": "open", "for_seconds": 8, "then": "close"}])
        self.active_hours = kw.get("active_hours")


class FakeSession:
    """Records what the engine asked to store, and answers reads as an EMPTY
    database.

    The rule cache is seeded directly, so `execute` is not reached for rules.
    It IS reached since P2: the bridge resolves every actuation target through
    the peripherals table, and a session that raised there would turn every
    outcome into `dispatch_failed` — an artefact of the fake rather than the
    behaviour under test. Empty is the honest simulation: no such peripheral,
    so the boundary rule refuses it.
    """

    def __init__(self):
        self.added = []
        self.commits = 0

    async def execute(self, *a, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: None, all=lambda: []),
            first=lambda: None, all=lambda: [])

    async def get(self, model, pk, **kwargs):
        return None  # No integrations configured in this rule-engine fixture.

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture
def valkey(monkeypatch):
    r = Flaky()

    async def _get():
        return r

    monkeypatch.setattr(rules_engine, "get_redis", _get)
    rules_engine._local_cooldown.clear()
    rules_engine._cache["version"] = None
    rules_engine._cache["rules"] = []
    return r


@pytest.fixture
def seeded(monkeypatch):
    """Put rules straight into the engine's cache, bypassing the database."""
    def _seed(*rules):
        async def _load(db):
            return list(rules), False
        monkeypatch.setattr(rules_engine, "_load_rules", _load)
    return _seed


def event(**kw):
    base = {"id": uuid.uuid4(), "camera_id": CAM, "camera_slug": "cam2-o8iu",
            "activity": "unauthorised_access", "zone": "Zone 1", "confidence": 0.9,
            "object_class": "person", "track_id": "7", "attributes": {},
            "started_at": NOW, "dedupe_key": None}
    base.update(kw)
    return base


# ── Cooldown ──────────────────────────────────────────────────────────────────

async def test_cooldown_collapses_a_burst_to_one(valkey):
    rid = uuid.uuid4()
    allowed, _ = await rules_engine.check_cooldown(rid, "cam3:intrusion", 60)
    assert allowed is True
    for _ in range(50):
        again, _ = await rules_engine.check_cooldown(rid, "cam3:intrusion", 60)
        assert again is False


async def test_cooldown_is_per_rule_and_per_dedupe_key(valkey):
    a, b = uuid.uuid4(), uuid.uuid4()
    assert (await rules_engine.check_cooldown(a, "k1", 60))[0] is True
    assert (await rules_engine.check_cooldown(b, "k1", 60))[0] is True     # other rule
    assert (await rules_engine.check_cooldown(a, "k2", 60))[0] is True     # other key
    assert (await rules_engine.check_cooldown(a, "k1", 60))[0] is False    # same pair


async def test_zero_cooldown_never_suppresses(valkey):
    rid = uuid.uuid4()
    assert (await rules_engine.check_cooldown(rid, "k", 0))[0] is True
    assert (await rules_engine.check_cooldown(rid, "k", 0))[0] is True


async def test_valkey_outage_still_collapses_via_the_local_fallback(valkey):
    """Alerts must keep flowing — they are evidence — but a busy tripwire must
    not write thousands of rows because the cache went away. The fallback is
    per-worker, so it is up to 4x looser than configured, and says so."""
    valkey.fail = True
    rid = uuid.uuid4()
    allowed, degraded = await rules_engine.check_cooldown(rid, "k", 60)
    assert allowed is True and degraded is True
    again, degraded2 = await rules_engine.check_cooldown(rid, "k", 60)
    assert again is False and degraded2 is True


# ── Firing ────────────────────────────────────────────────────────────────────

async def test_a_matching_event_raises_one_alert(valkey, seeded):
    rule = FakeRule()
    seeded(rule)
    db = FakeSession()
    ids = await rules_engine.evaluate(db, [event()], now=NOW)
    assert len(ids) == 1
    [alert] = db.added
    assert alert.rule_id == rule.id
    assert alert.rule_name == rule.name
    assert alert.camera_id == CAM
    assert alert.state == "new"
    assert alert.severity == "warning"


async def test_the_alert_cites_the_event_that_caused_it(valkey, seeded):
    """THE custody chain. audit_log -> alerts.event_ids -> analytics_events ->
    the camera's lawful basis has to resolve without leaving this database."""
    seeded(FakeRule())
    db = FakeSession()
    ev = event()
    await rules_engine.evaluate(db, [ev], now=NOW)
    [alert] = db.added
    assert alert.event_ids == [ev["id"]]


async def test_a_non_matching_event_raises_nothing(valkey, seeded):
    seeded(FakeRule())
    db = FakeSession()
    assert await rules_engine.evaluate(db, [event(activity="something_else")],
                                       now=NOW) == []
    assert db.added == []


async def test_two_rules_on_one_event_raise_two_alerts(valkey, seeded):
    """Two rules watching the same activity each get their own alert: they have
    different actions, different cooldowns and different resolutions."""
    seeded(FakeRule(name="a"), FakeRule(name="b"))
    db = FakeSession()
    assert len(await rules_engine.evaluate(db, [event()], now=NOW)) == 2


async def test_a_burst_of_events_collapses_to_one_alert(valkey, seeded):
    """The cooldown is what stops a tripwire writing thousands of rows an hour.
    Without a dedupe_key it must still collapse on the natural composite."""
    seeded(FakeRule(cooldown_s=60))
    db = FakeSession()
    ids = await rules_engine.evaluate(db, [event() for _ in range(20)], now=NOW)
    assert len(ids) == 1


async def test_events_on_different_cameras_do_not_share_a_cooldown(valkey, seeded):
    seeded(FakeRule(cooldown_s=60))
    db = FakeSession()
    ids = await rules_engine.evaluate(
        db, [event(camera_slug="cam-a"), event(camera_slug="cam-b")], now=NOW)
    assert len(ids) == 2


# ── What P1 cannot do ─────────────────────────────────────────────────────────

async def test_an_armed_rule_records_what_the_bridge_actually_did(valkey, seeded,
                                                                  monkeypatch):
    """RETIRED IN P2, AND THE CHANGE IS THE POINT. This test used to assert
    `no_bridge` "because none exists" — the honest answer while there was no
    outbound port at all. There is one now, so the recorded outcome is the
    bridge's own, and the old constant appearing again would mean the dispatch
    had been unwired.

    With no broker configured — the default deployment — that outcome is
    `driver_not_configured`: still not a claim that the barrier moved, which
    was the property the original test was protecting.
    """
    seeded(FakeRule(mode="armed"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    [alert] = db.added
    assert alert.notified["gate-3-barrier"] != "no_bridge"
    assert alert.notified["gate-3-barrier"] in (
        "driver_not_configured", "unknown_peripheral")


async def test_a_log_only_rule_records_log_only(valkey, seeded):
    seeded(FakeRule(mode="log_only"))
    db = FakeSession()
    await rules_engine.evaluate(db, [event()], now=NOW)
    [alert] = db.added
    assert alert.notified["gate-3-barrier"] == "log_only"
    assert alert.notified["_actuation"] == "log_only"


async def test_a_stale_event_raises_an_alert_marked_stale(valkey, seeded):
    seeded(FakeRule(max_event_age_s=30))
    db = FakeSession()
    await rules_engine.evaluate(db, [event(started_at=NOW - timedelta(minutes=10))],
                                now=NOW)
    [alert] = db.added
    assert alert.notified["_actuation"] == "stale_skipped"


async def test_disarmed_engine_still_raises_alerts(valkey, seeded):
    """Disarm stops actuation, never evidence."""
    await rules_engine.set_arm(False, by="alice", reason="flapping")
    seeded(FakeRule())
    db = FakeSession()
    ids = await rules_engine.evaluate(db, [event()], now=NOW)
    assert len(ids) == 1
    assert db.added[0].notified["_actuation"] == "disarmed"


async def test_valkey_outage_raises_the_alert_and_marks_the_cooldown_degraded(
        valkey, seeded):
    seeded(FakeRule())
    db = FakeSession()
    valkey.fail = True
    ids = await rules_engine.evaluate(db, [event()], now=NOW)
    assert len(ids) == 1
    assert db.added[0].notified["_cooldown"] == "degraded"


# ── It must never break ingest ────────────────────────────────────────────────

async def test_a_rule_that_explodes_does_not_take_the_batch_with_it(valkey, seeded,
                                                                    monkeypatch):
    """The engine runs inside the ingest handler. A bad rule must cost its own
    alert, not the rest of the batch's events."""
    bad, good = FakeRule(name="bad"), FakeRule(name="good")
    bad.conditions = property(lambda self: 1 / 0)      # explodes when read
    seeded(bad, good)
    db = FakeSession()
    ids = await rules_engine.evaluate(db, [event()], now=NOW)
    assert len(ids) == 1
    assert db.added[0].rule_name == "good"


async def test_no_events_is_not_an_error(valkey, seeded):
    seeded(FakeRule())
    assert await rules_engine.evaluate(FakeSession(), [], now=NOW) == []
