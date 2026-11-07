"""What 040 does to a real database.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, with the same two rails as
test_alert_rules_db.py: DATABASE_URL is overridden for alembic (inside the api
container it names the LIVE database), and the scratch database must start
EMPTY — a second run on an already-migrated one skips silently until you
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;`.

    RULES_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_bridge_db.py

WHAT IT PROVES, none of which a unit test can:
  * one binding per peripheral, enforced by the primary key
  * deleting a peripheral takes its binding AND its pending actions with it
  * the driver and pending-state vocabularies are enforced by the database
  * the equality form of the topic loop is refused by a CHECK
  * a pending action is claimable exactly once, which is what stops four
    workers all closing the same barrier
  * the bridge really is the writer of peripherals.last_state
  * unbinding takes the binding and the wall's state, refuses while a
    follow-up is owed, and leaves the peripheral and its history alone
  * downgrade is clean, so 040 is reversible
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

DB_URL = os.environ.get("RULES_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set RULES_TEST_DB to a scratch database URL")

if DB_URL:                                  # keep collection cheap when skipping
    pytest.importorskip("alembic", reason="alembic lives in the api image")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                               # noqa: E402
    from alembic.config import Config                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace(
        "postgresql://", "postgresql+asyncpg://")


def _dsn() -> str:
    return _alembic_url().replace("+asyncpg", "")


def _cfg():
    os.environ["DATABASE_URL"] = _alembic_url()               # RAIL 1
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _alembic_url())
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _query(sql, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(sql, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


def _session(fn):
    """Run `fn(session)` on a fresh engine — an asyncpg pool belongs to one loop."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def go():
        engine = create_async_engine(_alembic_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture(scope="module")
def migrated():
    tables = _run(_query("SELECT count(*) AS n FROM information_schema.tables "
                         "WHERE table_schema = 'public'"))
    if tables[0]["n"]:
        pytest.skip("RULES_TEST_DB is not empty; refusing to migrate it")
    command.upgrade(_cfg(), "040")
    yield


@pytest.fixture(autouse=True)
def clean(migrated):
    yield
    _run(_execute("DELETE FROM pending_actions"))
    _run(_execute("DELETE FROM peripheral_bindings"))
    _run(_execute("DELETE FROM peripherals"))


def _a_peripheral(slug="gate-3-barrier") -> int:
    rows = _run(_query(
        "INSERT INTO peripherals (slug, name, category, created_at, updated_at) "
        "VALUES ($1, $2, 'Access control', now(), now()) RETURNING id",
        slug, slug.replace("-", " ").title()))
    return rows[0]["id"]


def _a_binding(pid, **cols):
    base = {"driver": "mqtt", "address": "site/gate3/set"}
    base.update(cols)
    keys = ", ".join(base)
    holes = ", ".join(f"${i + 2}" for i in range(len(base)))
    _run(_execute(
        f"INSERT INTO peripheral_bindings "
        f"(peripheral_id, created_at, updated_at, {keys}) "
        f"VALUES ($1, now(), now(), {holes})", pid, *base.values()))


# ── The binding table ────────────────────────────────────────────────────────

def test_a_peripheral_has_at_most_one_binding(migrated):
    """One device, one address. Two bindings would mean two answers to "where
    does this live", and the second would be silently unused."""
    pid = _a_peripheral()
    _a_binding(pid)
    with pytest.raises(asyncpg.UniqueViolationError):
        _a_binding(pid, address="site/other/set")


def test_deleting_a_peripheral_takes_its_binding(migrated):
    pid = _a_peripheral()
    _a_binding(pid)
    _run(_execute("DELETE FROM peripherals WHERE id = $1", pid))
    assert not _run(_query(
        "SELECT 1 FROM peripheral_bindings WHERE peripheral_id = $1", pid))


def test_the_driver_is_constrained_to_known_values(migrated):
    pid = _a_peripheral()
    with pytest.raises(asyncpg.CheckViolationError):
        _a_binding(pid, driver="carrier_pigeon")


def test_a_state_topic_equal_to_the_command_topic_is_refused_by_the_database(
        migrated):
    """The wildcard forms need validate_topics; this one is pure equality and
    the database can hold it, so it does — a binding written by anything other
    than the API still cannot create a message loop."""
    pid = _a_peripheral()
    with pytest.raises(asyncpg.CheckViolationError):
        _a_binding(pid, address="site/gate3/set", state_topic="site/gate3/set")


def test_a_write_only_device_may_have_no_state_topic(migrated):
    """A relay that reports nothing back is a real device and still worth
    driving."""
    pid = _a_peripheral()
    _a_binding(pid, state_topic=None)
    [row] = _run(_query("SELECT state_topic, verified_at FROM peripheral_bindings "
                        "WHERE peripheral_id = $1", pid))
    assert row["state_topic"] is None
    # And it starts unverified, so it cannot actuate until the bridge confirms.
    assert row["verified_at"] is None


# ── Pending actions ──────────────────────────────────────────────────────────

def test_deleting_a_peripheral_takes_its_pending_actions(migrated):
    """Otherwise a deleted barrier keeps a "close me" row that can never
    resolve, and the sweep retries it forever."""
    pid = _a_peripheral()
    _a_binding(pid)
    _run(_execute(
        "INSERT INTO pending_actions (id, peripheral_id, command, due_at) "
        "VALUES ($1, $2, 'close', now())", uuid.uuid4(), pid))
    _run(_execute("DELETE FROM peripherals WHERE id = $1", pid))
    assert not _run(_query(
        "SELECT 1 FROM pending_actions WHERE peripheral_id = $1", pid))


def test_the_pending_state_is_constrained(migrated):
    pid = _a_peripheral()
    with pytest.raises(asyncpg.CheckViolationError):
        _run(_execute(
            "INSERT INTO pending_actions (id, peripheral_id, command, due_at, state) "
            "VALUES ($1, $2, 'close', now(), 'probably_fine')", uuid.uuid4(), pid))


def test_a_pending_action_is_claimable_exactly_once(migrated):
    """THE test that makes four workers safe. The claim is an UPDATE guarded on
    the current state, so the loser of a race updates zero rows and does
    nothing — which is why the sweep needs no lock of its own."""
    pid = _a_peripheral()
    _a_binding(pid)
    aid = uuid.uuid4()
    _run(_execute("INSERT INTO pending_actions (id, peripheral_id, command, due_at) "
                  "VALUES ($1, $2, 'close', now() - interval '1 second')", aid, pid))

    claim = ("UPDATE pending_actions SET state = 'claimed', "
             "attempts = attempts + 1 "
             "WHERE id = $1 AND state = 'pending' RETURNING id")
    first = _run(_query(claim, aid))
    second = _run(_query(claim, aid))
    assert len(first) == 1
    assert second == []


def test_the_due_index_is_partial_on_pending(migrated):
    """The sweep's only query. A full index would grow with every completed
    pulse the table has ever held."""
    [row] = _run(_query(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_pending_due'"))
    # Postgres normalises the predicate — `((state)::text = 'pending'::text)` —
    # so this asserts that a predicate exists and names the state, not its
    # exact spelling, which is the server's business and not the contract.
    indexdef = row["indexdef"]
    assert " WHERE " in indexdef
    assert "'pending'" in indexdef


# ── Reversibility ────────────────────────────────────────────────────────────

def test_downgrade_is_clean(migrated):
    command.downgrade(_cfg(), "039")
    for table in ("peripheral_bindings", "pending_actions"):
        assert not _run(_query(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=$1", table))
    command.upgrade(_cfg(), "040")


# ── The bridge is the writer of last_state ───────────────────────────────────

def test_the_bridge_writes_last_state_and_last_seen(migrated):
    """The end-to-end proof: a state message becomes the value the device wall
    renders. The API cannot write these columns; this is what can."""
    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid, state_topic="site/gate3/state")
    _session(lambda s: bridge._write_state(s, pid, "closed", NOW))
    [row] = _run(_query(
        "SELECT last_state, last_seen FROM peripherals WHERE id = $1", pid))
    assert row["last_state"] == "closed"
    assert row["last_seen"] is not None


def test_a_state_topic_resolves_to_its_peripheral(migrated):
    """The inbound half of the boundary rule, against real rows."""
    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid, state_topic="site/gate3/state")
    found = _session(
        lambda s: bridge._peripheral_id_for_topic(s, "site/gate3/state"))
    assert found == pid
    assert _session(
        lambda s: bridge._peripheral_id_for_topic(s, "site/someone-else/state")) is None


def test_apply_state_end_to_end_writes_only_for_known_topics(migrated):
    """apply_state is what the subscriber calls for every message the broker
    carries — including the customer's whole smart home."""
    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid, state_topic="site/gate3/state")

    _session(lambda s: bridge.apply_state(s, "site/gate3/state", "open", NOW))
    _session(lambda s: bridge.apply_state(s, "site/lounge/lamp", "on", NOW))

    [row] = _run(_query("SELECT last_state FROM peripherals WHERE id = $1", pid))
    assert row["last_state"] == "open"
    # And nothing was invented for the topic that belongs to no VMS device.
    assert _run(_query("SELECT count(*) AS n FROM peripherals"))[0]["n"] == 1


def test_a_gone_quiet_device_is_returned_to_unknown(migrated):
    """THE dishonesty this feature exists to avoid: the wall showing "closed"
    for a device that stopped reporting an hour ago."""
    from datetime import timedelta

    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid, state_topic="site/gate3/state")
    _run(_execute("UPDATE peripherals SET last_state = 'closed', "
                  "last_seen = now() - interval '2 hours' WHERE id = $1", pid))

    cleared = _session(lambda s: bridge._clear_stale_states(
        s, now=datetime.now(timezone.utc)))
    assert cleared == 1
    [row] = _run(_query("SELECT last_state FROM peripherals WHERE id = $1", pid))
    assert row["last_state"] is None


def test_a_fresh_reading_survives_the_sweep(migrated):
    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid, state_topic="site/gate3/state")
    _run(_execute("UPDATE peripherals SET last_state = 'closed', last_seen = now() "
                  "WHERE id = $1", pid))
    assert _session(lambda s: bridge._clear_stale_states(
        s, now=datetime.now(timezone.utc))) == 0


# ── Unbinding ────────────────────────────────────────────────────────────────

def test_unbinding_removes_the_row_and_returns_the_wall_to_unknown(migrated):
    """The peripheral, its pins and its `last_seen` survive; the binding and
    the `last_state` do not — nothing can report for this device now."""
    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid, state_topic="site/gate3/state")
    _run(_execute("UPDATE peripherals SET last_state = 'closed', last_seen = now() "
                  "WHERE id = $1", pid))

    assert _session(lambda s: bridge.unbind(s, pid)) is True
    assert not _run(_query(
        "SELECT 1 FROM peripheral_bindings WHERE peripheral_id = $1", pid))
    [row] = _run(_query(
        "SELECT last_state, last_seen FROM peripherals WHERE id = $1", pid))
    assert row["last_state"] is None
    assert row["last_seen"] is not None


def test_unbinding_a_device_with_no_binding_says_so(migrated):
    from backend.services import bridge
    pid = _a_peripheral()
    assert _session(lambda s: bridge.unbind(s, pid)) is False


def test_unbinding_is_refused_while_a_follow_up_is_owed(migrated):
    """An `off` still to come needs the address to be delivered. The row and
    the follow-up both stay; a done follow-up does not count."""
    from backend.services import bridge
    pid = _a_peripheral()
    _a_binding(pid)
    _run(_execute(
        "INSERT INTO pending_actions (id, peripheral_id, command, due_at, state) "
        "VALUES ($1, $2, 'off', now() + interval '5 seconds', 'pending')",
        uuid.uuid4(), pid))
    with pytest.raises(bridge.FollowUpOwed) as exc:
        _session(lambda s: bridge.unbind(s, pid))
    assert exc.value.owed == 1
    assert _run(_query(
        "SELECT 1 FROM peripheral_bindings WHERE peripheral_id = $1", pid))

    _run(_execute("UPDATE pending_actions SET state = 'done' WHERE peripheral_id = $1",
                  pid))
    assert _session(lambda s: bridge.unbind(s, pid)) is True


# ── The durable pulse, against real rows ─────────────────────────────────────

def test_two_concurrent_sweeps_execute_a_row_once(migrated):
    """Proven at the SQL level above; this proves the SWEEP uses it. Four
    workers must not each close the same barrier."""
    from backend.services.bridge import pending
    pid = _a_peripheral()
    _a_binding(pid)
    _run(_execute("INSERT INTO pending_actions (id, peripheral_id, command, due_at) "
                  "VALUES ($1, $2, 'close', now() - interval '1 second')",
                  uuid.uuid4(), pid))
    first = _session(lambda s: pending.claim_due(s, limit=10))
    second = _session(lambda s: pending.claim_due(s, limit=10))
    assert len(first) == 1
    assert second == []


def test_a_claim_carries_the_slug_the_bridge_needs(migrated):
    """The bridge actuates by SLUG — the boundary rule resolves through the
    registry — so a claim that returned only peripheral_id would need a second
    query per row inside the sweep."""
    from backend.services.bridge import pending
    pid = _a_peripheral(slug="gate-7-barrier")
    _a_binding(pid)
    _run(_execute("INSERT INTO pending_actions (id, peripheral_id, command, due_at) "
                  "VALUES ($1, $2, 'close', now() - interval '1 second')",
                  uuid.uuid4(), pid))
    [row] = _session(lambda s: pending.claim_due(s, limit=10))
    assert row.slug == "gate-7-barrier"
    assert row.command == "close"
    assert row.attempts == 1


def test_a_future_follow_up_is_not_claimed_yet(migrated):
    """The barrier is supposed to be open right now."""
    from backend.services.bridge import pending
    pid = _a_peripheral()
    _a_binding(pid)
    _run(_execute("INSERT INTO pending_actions (id, peripheral_id, command, due_at) "
                  "VALUES ($1, $2, 'close', now() + interval '1 hour')",
                  uuid.uuid4(), pid))
    assert _session(lambda s: pending.claim_due(s, limit=10)) == []


def test_a_sweep_marks_a_successful_follow_up_done(migrated):
    """The end-to-end path: claimed, executed, and no longer due."""
    from backend.services.bridge import base, pending
    pid = _a_peripheral()
    _a_binding(pid)
    aid = uuid.uuid4()
    _run(_execute("INSERT INTO pending_actions (id, peripheral_id, command, due_at) "
                  "VALUES ($1, $2, 'close', now() - interval '1 second')", aid, pid))

    async def _ok(slug, command, params):
        return base.ActuationResult(True, "published")

    executed = _session(lambda s: pending.sweep(
        s, actuate=_ok, raise_alert=lambda **kw: None))
    assert executed == 1
    [row] = _run(_query("SELECT state FROM pending_actions WHERE id = $1", aid))
    assert row["state"] == "done"
    # And it is not picked up again.
    assert _session(lambda s: pending.claim_due(s, limit=10)) == []
