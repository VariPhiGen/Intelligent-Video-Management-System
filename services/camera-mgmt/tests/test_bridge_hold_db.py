"""'Until the person leaves the zone' against a real PostgreSQL (migration 080).

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE:

    HOLD_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \\
        python -m pytest tests/test_bridge_hold_db.py -q

It refuses a database that already has tables, and migrates it to head.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

DB_URL = os.environ.get("HOLD_TEST_DB")
pytestmark = pytest.mark.skipif(not DB_URL, reason="set HOLD_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                                  # noqa: E402
    from alembic.config import Config                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace("postgresql://", "postgresql+asyncpg://")


def _cfg():
    os.environ["DATABASE_URL"] = _url()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _url())
    return cfg


async def _q(sql, *args):
    conn = await asyncpg.connect(_url().replace("+asyncpg", ""))
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def _session(fn):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    async def go():
        engine = create_async_engine(_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                return await fn(s)
        finally:
            await engine.dispose()
    return asyncio.run(go())


@pytest.fixture(scope="module")
def migrated():
    if asyncio.run(_q("SELECT count(*) n FROM information_schema.tables "
                      "WHERE table_schema='public'"))[0]["n"]:
        pytest.skip("HOLD_TEST_DB is not empty; refusing to migrate it")
    command.upgrade(_cfg(), "head")
    yield


@pytest.fixture
def siren(migrated):
    rows = asyncio.run(_q(
        "INSERT INTO peripherals (slug, name, category, created_at, updated_at) "
        "VALUES ('relay-1-ch8', 'Relay 1 CH8', 'Audio · Sensors', now(), now()) RETURNING id"))
    yield rows[0]["id"]
    asyncio.run(_q("DELETE FROM pending_actions"))
    asyncio.run(_q("DELETE FROM peripherals"))


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _published():
    sent = []

    async def publish(slug, cmd, params):
        sent.append((slug, cmd))
        return SimpleNamespace(ok=True, detail="called")
    return sent, publish


def _hold(pid, publish, *, now=NOW, zone="Zone 1"):
    from backend.services.bridge import pending
    return _session(lambda db: pending.hold(
        db, peripheral_id=pid, slug="relay-1-ch8", command="on", then="off",
        cap_seconds=600, grace_s=0, camera="realcam-al5d", zone=zone, params={},
        alert_id=None, now=now, publish=publish))


def _rows():
    return asyncio.run(_q("SELECT state, command, due_at, hold_camera, hold_zone, "
                          "hold_grace_s, empty_since FROM pending_actions"))


def test_a_hold_commits_its_off_with_a_cap_and_switches_on(siren):
    sent, publish = _published()
    assert _hold(siren, publish).ok
    [r] = _rows()
    assert sent == [("relay-1-ch8", "on")]
    assert (r["state"], r["command"], r["hold_camera"], r["hold_zone"], r["hold_grace_s"]) == \
        ("pending", "off", "realcam-al5d", "Zone 1", 0)
    assert r["due_at"] == NOW + timedelta(seconds=600)


def test_a_running_hold_is_refreshed_not_resent(siren):
    """One hold per device. The occupancy loop and the late event both ask for
    it; the second must not send ON again (an operator's manual OFF would be
    undone) nor add a second OFF that cuts the hold short."""
    sent, publish = _published()
    _hold(siren, publish)
    r = _hold(siren, publish, now=NOW + timedelta(seconds=30), zone="Zone 2")
    rows = _rows()
    assert r.detail == "held" and sent == [("relay-1-ch8", "on")]
    assert len(rows) == 1 and rows[0]["due_at"] == NOW + timedelta(seconds=630)
    assert rows[0]["hold_zone"] == "Zone 1", "the running hold keeps watching its zone"


def test_a_hold_that_never_switched_on_is_withdrawn(siren):
    async def fail(slug, cmd, params):
        return SimpleNamespace(ok=False, detail="call_rejected:modbus-tcp/timeout")
    from backend.services.bridge import pending  # noqa: F401
    _hold(siren, fail)
    assert _rows()[0]["state"] == "abandoned"


def _hold_rows():
    from sqlalchemy import select
    from backend.models import PendingAction

    async def go(db):
        rows = (await db.execute(select(PendingAction).where(
            PendingAction.state == "pending", PendingAction.hold_camera.isnot(None)))).scalars().all()
        for r in rows:
            r.slug = "relay-1-ch8"
        return rows
    return go


def _end_and_sweep(now, count):
    """The occupancy loop's ending half against the real tables: decide on a
    reading, commit, then the real sweep with a fake actuate."""
    from backend.services.bridge import pending
    sent = []
    reading = {"ts": now.timestamp(), "zones": {"region_1": {"name": "Zone 1", "count": count}}}

    async def decide_and_commit(db):
        holds = await _hold_rows()(db)
        _, end = pending.decide([], holds, {"realcam-al5d": reading}, now=now, busy=set())
        await db.commit()
        return len(end)

    async def actuate(slug, cmd, params):
        sent.append((slug, cmd))
        return SimpleNamespace(ok=True, detail="called")

    ended = _session(decide_and_commit)
    swept = _session(lambda db: pending.sweep(db, actuate=actuate, raise_alert=lambda **k: None))
    return ended, swept, sent


def test_the_off_is_sent_the_moment_the_zone_reads_empty(siren):
    _, publish = _published()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=5)
    _hold(siren, publish, now=t0)
    assert _end_and_sweep(t0 + timedelta(seconds=1), count=0) == (1, 1, [("relay-1-ch8", "off")])
    assert _rows()[0]["state"] == "done"


def test_an_occupied_zone_sends_nothing(siren):
    _, publish = _published()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=5)
    _hold(siren, publish, now=t0)
    assert _end_and_sweep(t0 + timedelta(seconds=1), count=1) == (0, 0, [])
    assert _rows()[0]["state"] == "pending"
