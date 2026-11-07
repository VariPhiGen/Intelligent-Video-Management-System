"""Device state coming back in, and the one-writer rule.

`peripherals.last_state` is the column the device wall renders. It has exactly
ONE writer — the leader-elected subscriber — because four workers racing on it
would be a write storm on the one table whose honesty this whole design rests
on.

The API still refuses last_state (PeripheralIn/PeripheralPatch exclude it, as
migration 029 intended). The bridge does not relax that; it becomes the only
thing that can set it. That is what makes the wall honest by construction
rather than by convention.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_state.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import bridge  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


# ── The election, asserted structurally ──────────────────────────────────────

def test_the_state_loop_is_registered_under_leader_election():
    """Four subscribers would be four writers. The election is what makes the
    single-writer claim true rather than aspirational, and it is one line in
    main.py that nobody is forced to look at — so it is asserted here."""
    import inspect

    from backend import main
    src = inspect.getsource(main)
    assert 'run_as_leader("bridge_state"' in src


def test_the_verify_loop_is_registered_under_leader_election():
    import inspect

    from backend import main
    src = inspect.getsource(main)
    assert 'run_as_leader("bridge_verify"' in src


def test_the_publisher_loop_runs_in_every_worker_not_under_election():
    """The mirror image of the two tests above, and the one the appliance
    taught. Only the elected worker held a client, so three requests in four
    were refused with `offline` while the broker showed a live subscriber.
    Outbound is per worker: a task in main.py, and NOT inside run_as_leader."""
    import inspect
    import re

    from backend import main
    src = inspect.getsource(main)
    assert "bridge.publisher_loop()" in src
    assert not re.search(r'run_as_leader\(\s*"[^"]*",\s*bridge\.publisher_loop', src)


@_aio
async def test_the_publisher_holds_a_publish_only_session(monkeypatch):
    """publisher_loop opens the shared driver WITHOUT subscriptions — the
    subscription is the elected worker's and lives on a separate session —
    and when the session ends it comes back rather than giving up."""
    import asyncio

    calls = []

    class FakeDriver:
        async def health(self):
            return "offline"

        async def start(self, *, subscriptions):
            calls.append(subscriptions)
            if len(calls) == 1:
                raise ConnectionError("broker gone")   # first attempt fails
            # The second one "holds". NOT asyncio.sleep: that is patched to
            # return at once below, and a start() that returns at once makes
            # publisher_loop a hot loop that never yields to this test.
            await asyncio.Event().wait()

    class FakeBridge:
        _drivers = {"mqtt": FakeDriver()}

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(bridge, "get_bridge", lambda: FakeBridge())
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bridge.asyncio, "sleep", fake_sleep)
    task = asyncio.get_running_loop().create_task(bridge.publisher_loop())
    for _ in range(50):
        await real_sleep(0)
        if len(calls) >= 2:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls[:2] == [False, False]   # publish-only, and it retried
    assert slept and slept[0] > 0         # with a backoff, not a hot loop


def test_the_api_still_refuses_last_state():
    """029's principle survives P2: a hand-typed status must never appear next
    to a live camera wall. The bridge is the only writer, and these four
    exclusions are what keep that true."""
    from backend.models import PeripheralIn, PeripheralPatch
    assert "last_state" not in PeripheralIn.model_fields
    assert "last_seen" not in PeripheralIn.model_fields
    assert "last_state" not in PeripheralPatch.model_fields
    assert "last_seen" not in PeripheralPatch.model_fields


# ── Staleness ────────────────────────────────────────────────────────────────

def test_a_stale_reading_is_stale_and_a_fresh_one_is_not():
    """A value from half an hour ago shown beside live video reads as "the gate
    is closed right now", and it is not. After the threshold the wall goes back
    to unknown, which is the truth."""
    assert bridge.is_stale(NOW - timedelta(minutes=30), now=NOW) is True
    assert bridge.is_stale(NOW - timedelta(seconds=30), now=NOW) is False


def test_never_having_reported_is_stale():
    assert bridge.is_stale(None, now=NOW) is True


def test_a_naive_timestamp_is_treated_as_utc_rather_than_crashing():
    """asyncpg hands back aware datetimes, but a caller constructing one by
    hand may not. Comparing aware to naive raises TypeError, and a status
    column must not be able to take down the poll that refreshes it."""
    naive = (NOW - timedelta(seconds=30)).replace(tzinfo=None)
    assert bridge.is_stale(naive, now=NOW) is False


def test_a_reading_from_the_future_is_not_stale():
    """Clock skew runs both ways, and a device slightly ahead is not history."""
    assert bridge.is_stale(NOW + timedelta(seconds=30), now=NOW) is False


# ── Inbound routing ──────────────────────────────────────────────────────────

@_aio
async def test_state_for_an_unknown_topic_is_dropped(monkeypatch):
    """The broker carries the customer's whole smart home. Only devices in the
    VMS inventory get rows — the boundary rule applies inbound too."""
    applied = []

    async def _write(db, pid, state, at):
        applied.append(pid)

    async def _lookup(db, topic):
        return None

    monkeypatch.setattr(bridge, "_write_state", _write)
    monkeypatch.setattr(bridge, "_peripheral_id_for_topic", _lookup)
    await bridge.apply_state(None, "site/somebody-else/state", "on", NOW)
    assert applied == []


@_aio
async def test_state_for_a_known_topic_is_written(monkeypatch):
    applied = []

    async def _write(db, pid, state, at):
        applied.append((pid, state, at))

    async def _lookup(db, topic):
        return 7

    monkeypatch.setattr(bridge, "_write_state", _write)
    monkeypatch.setattr(bridge, "_peripheral_id_for_topic", _lookup)
    await bridge.apply_state(None, "site/gate3/state", "closed", NOW)
    assert applied == [(7, "closed", NOW)]


@_aio
async def test_an_empty_payload_is_dropped_rather_than_stored(monkeypatch):
    """MQTT publishes an empty payload to CLEAR a retained topic. Storing it
    would put an empty string on the wall where a state belongs."""
    applied = []

    async def _write(db, pid, state, at):
        applied.append(state)

    async def _lookup(db, topic):
        return 7

    monkeypatch.setattr(bridge, "_write_state", _write)
    monkeypatch.setattr(bridge, "_peripheral_id_for_topic", _lookup)
    await bridge.apply_state(None, "site/gate3/state", "", NOW)
    await bridge.apply_state(None, "site/gate3/state", "   ", NOW)
    assert applied == []


@_aio
async def test_an_over_long_payload_is_truncated_to_the_column(monkeypatch):
    """`last_state` is String(40). A device publishing a JSON blob must not
    make the write fail — the wall showing the first 40 characters is better
    than a subscriber that dies on one chatty device."""
    applied = []

    async def _write(db, pid, state, at):
        applied.append(state)

    async def _lookup(db, topic):
        return 7

    monkeypatch.setattr(bridge, "_write_state", _write)
    monkeypatch.setattr(bridge, "_peripheral_id_for_topic", _lookup)
    await bridge.apply_state(None, "site/gate3/state", "x" * 200, NOW)
    assert len(applied[0]) <= 40


@_aio
async def test_a_write_failure_does_not_kill_the_subscriber(monkeypatch):
    """One bad message must not end the subscription for every device. The
    subscriber is elected — if it dies, nothing writes state until the lock
    expires."""
    async def _write(db, pid, state, at):
        raise RuntimeError("db gone")

    async def _lookup(db, topic):
        return 7

    monkeypatch.setattr(bridge, "_write_state", _write)
    monkeypatch.setattr(bridge, "_peripheral_id_for_topic", _lookup)
    await bridge.apply_state(None, "site/gate3/state", "closed", NOW)   # no raise


# ── The subscription must follow the bindings, not the boot ──────────────────

def test_the_state_loop_reconciles_topics_while_the_session_is_live():
    """The regression this file exists to prevent recurring.

    `state_loop` used to read the binding topics ONCE, before `start()`, and
    the session then held that set until leadership moved or the process
    restarted. An operator who bound a peripheral afterwards got a device that
    actuated correctly and reported `last_state` NULL forever — the wall said
    "unknown", which reads as a broken bridge rather than as a subscriber that
    had never heard of the binding. Verified against a live Mosquitto: the
    retained state arrived only after an API restart.

    Asserted structurally, because the alternative is an integration test with
    a broker and this is one call that is easy to delete by accident.
    """
    import inspect

    from backend.services import bridge
    src = inspect.getsource(bridge.state_loop)
    assert "sync_topics" in src, \
        "state_loop no longer re-asserts its topics; a binding added after " \
        "boot will never report state"


def test_the_topic_reconcile_has_a_bounded_interval():
    """A reconcile an operator has to wait minutes for is one they will report
    as a bug. The interval is a setting so a site can tune it, but it must be
    short enough that binding a device and watching the wall feels immediate."""
    from backend.config import settings
    assert settings.bridge_topic_sync_s <= 30
