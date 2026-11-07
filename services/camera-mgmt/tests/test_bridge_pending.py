"""The durable pulse: "open for 8 seconds" across four workers.

The worker that opens the barrier may be gone before the close is due. So the
follow-up is a committed ROW, written BEFORE the first half publishes — a crash
between the two must leave evidence, not silence.

DEFENCE IN DEPTH, NOT THE SAFETY MECHANISM. The PLC owns the authoritative
timeout (spec 11) and nothing here weakens that; what this buys is that a
deploy does not leave a gate open with no record of why.

Run: python -m pytest tests/test_bridge_pending.py -q
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.bridge import base, pending  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


class OrderRecordingSession:
    """Records the ORDER of add/commit, which is the property under test."""

    def __init__(self):
        self.events = []
        self.added = []

    def add(self, obj):
        self.added.append(obj)
        self.events.append("add")

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


# ── Ordering: the whole point of the table ───────────────────────────────────

@_aio
async def test_the_follow_up_row_is_committed_before_the_first_half_publishes():
    """THE ordering test. Publish first and die, and there is no record that a
    barrier was ever opened, let alone that it needs closing."""
    db = OrderRecordingSession()
    published = []

    async def publish(*a, **kw):
        published.append(("published", len(db.events)))
        return base.ActuationResult(True, "published")

    await pending.pulse(db, peripheral_id=1, slug="gate-3-barrier",
                        command="open", then="close", for_seconds=8,
                        params={}, alert_id=None, now=NOW, publish=publish)

    assert "commit" in db.events, "no follow-up row was committed at all"
    assert db.events.index("commit") < published[0][1], \
        "the command published before its follow-up was durable"


@_aio
async def test_the_follow_up_is_due_after_for_seconds():
    db = OrderRecordingSession()

    async def publish(*a, **kw):
        return base.ActuationResult(True, "published")

    await pending.pulse(db, peripheral_id=1, slug="g", command="open",
                        then="close", for_seconds=8, params={}, alert_id=None,
                        now=NOW, publish=publish)
    [row] = db.added
    assert row.command == "close"
    assert row.due_at == NOW + timedelta(seconds=8)
    assert row.state == "pending"


@_aio
async def test_the_follow_up_cites_the_alert_that_caused_it():
    """So an abandoned close can be traced back to the detection that opened
    the barrier, which is the whole custody chain in miniature."""
    db = OrderRecordingSession()
    alert_id = uuid.uuid4()

    async def publish(*a, **kw):
        return base.ActuationResult(True, "published")

    await pending.pulse(db, peripheral_id=1, slug="g", command="open",
                        then="close", for_seconds=8, params={},
                        alert_id=alert_id, now=NOW, publish=publish)
    assert db.added[0].alert_id == alert_id


@_aio
async def test_a_failed_open_withdraws_its_close():
    """Scheduling a close for a barrier that never opened would leave a row
    that retries, fails, and finally raises a critical alert for a non-event."""
    db = OrderRecordingSession()

    async def publish(*a, **kw):
        return base.ActuationResult(False, "offline")

    r = await pending.pulse(db, peripheral_id=1, slug="g", command="open",
                            then="close", for_seconds=8, params={},
                            alert_id=None, now=NOW, publish=publish)
    assert r.ok is False
    assert db.added[0].state == "abandoned"
    assert "open_failed" in db.added[0].last_error


# ── Executing a follow-up ────────────────────────────────────────────────────

def _due(attempts=0, command="close", alert_id=None):
    return pending.Due(id=uuid.uuid4(), peripheral_id=1, slug="gate-3-barrier",
                       command=command, params={}, attempts=attempts,
                       alert_id=alert_id)


@_aio
async def test_a_successful_follow_up_is_done():
    async def actuate(*a, **kw):
        return base.ActuationResult(True, "published")

    assert await pending.execute_one(None, _due(), actuate=actuate,
                                     raise_alert=lambda **kw: None) == "done"


@_aio
async def test_a_retryable_failure_stays_pending():
    async def actuate(*a, **kw):
        return base.ActuationResult(False, "offline")

    assert await pending.execute_one(None, _due(), actuate=actuate,
                                     raise_alert=lambda **kw: None) == "pending"


@_aio
async def test_exhausted_attempts_abandons_and_raises_a_critical_alert():
    """An un-closed barrier is exactly the condition an operator must be told
    about. Silently giving up is the worst available behaviour."""
    raised = []

    async def actuate(*a, **kw):
        return base.ActuationResult(False, "offline")

    outcome = await pending.execute_one(
        None, _due(attempts=pending.MAX_ATTEMPTS), actuate=actuate,
        raise_alert=lambda **kw: raised.append(kw))
    assert outcome == "abandoned"
    assert raised and raised[0]["severity"] == "critical"
    assert raised[0]["peripheral"] == "gate-3-barrier"
    assert raised[0]["command"] == "close"


@_aio
async def test_an_actuator_that_raises_is_a_retry_not_a_crash():
    """The sweep runs in a background loop. One exploding driver must not end
    it for every other pending row."""
    async def actuate(*a, **kw):
        raise RuntimeError("boom")

    assert await pending.execute_one(None, _due(), actuate=actuate,
                                     raise_alert=lambda **kw: None) == "pending"


@_aio
async def test_an_exploding_actuator_at_the_last_attempt_still_warns():
    """The abandon path must not be reachable only by a clean refusal — a
    driver that crashes every time is exactly as un-closed as one that
    refuses."""
    raised = []

    async def actuate(*a, **kw):
        raise RuntimeError("boom")

    outcome = await pending.execute_one(
        None, _due(attempts=pending.MAX_ATTEMPTS), actuate=actuate,
        raise_alert=lambda **kw: raised.append(kw))
    assert outcome == "abandoned"
    assert raised


# ── The follow-up is audited like every other actuation ─────────────────────

@_aio
async def test_a_follow_up_is_audited_with_its_alert_success_or_failure():
    """The siren stopping is as much a fact about the incident as it starting,
    and a close that FAILED is the one the operator will be asked about. Seen
    on the appliance: the rule's `on` was in the audit log and the `off` that
    followed 20 s later was not."""
    seen = []

    async def audit(row, ok, detail):
        seen.append((row.command, row.alert_id, ok, detail))

    async def good(*a, **kw):
        return base.ActuationResult(True, "published")

    async def bad(*a, **kw):
        return base.ActuationResult(False, "offline")

    alert_id = uuid.uuid4()
    await pending.execute_one(None, _due(alert_id=alert_id), actuate=good,
                              raise_alert=lambda **kw: None, audit=audit)
    await pending.execute_one(None, _due(alert_id=alert_id), actuate=bad,
                              raise_alert=lambda **kw: None, audit=audit)
    assert seen == [("close", alert_id, True, "published"),
                    ("close", alert_id, False, "offline")]


@_aio
async def test_an_audit_that_fails_does_not_lose_the_follow_up():
    async def audit(row, ok, detail):
        raise RuntimeError("audit table locked")

    async def good(*a, **kw):
        return base.ActuationResult(True, "published")

    assert await pending.execute_one(None, _due(), actuate=good,
                                     raise_alert=lambda **kw: None,
                                     audit=audit) == "done"
