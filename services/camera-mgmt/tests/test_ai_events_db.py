"""AI activity events against a real PostgreSQL — the half a stub cannot reach.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, for the same reasons and with the
same two rails as test_activity_catalog_migration_db.py: DATABASE_URL is
overridden for alembic (inside the api container it names the LIVE database),
and the scratch database must start empty.

    EVENTS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \\
        python -m pytest services/camera-mgmt/tests/test_ai_events_db.py

WHAT IT PROVES, none of which the logic tests can:
  * the upsert: a retry is a duplicate, a close updates only an open event
  * newest-first ordering across cameras, with the id tie-break
  * camera / activity / from / to filters, and keyset pages that neither
    overlap nor skip
  * the overview: at most ten in the ticker, one card per configured camera
    (quiet cameras included), each card's slice newest first
  * expired rows are hidden before the sweep and deleted by it
  * migration 035's index exists
  * the Entry / Exit graph's count: each crossing in the bucket that holds it,
    Entry and Exit apart, one tripwire alone, directionless touches and other
    activities and cameras never counted, buckets on the viewer's clock
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DB_URL = os.environ.get("EVENTS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set EVENTS_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                                  # noqa: E402
    from alembic.config import Config                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace("postgresql://",
                                                              "postgresql+asyncpg://")


def _dsn() -> str:
    return _alembic_url().replace("+asyncpg", "")


def _cfg():
    os.environ["DATABASE_URL"] = _alembic_url()          # RAIL 1
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _alembic_url())
    return cfg


async def _fetch(sql, *args):
    conn = await asyncpg.connect(_dsn())
    try:
        return await conn.fetch(sql, *args)
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


def _config(*activities):
    regions = {"region_1": {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
                            "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]], "direction": None}}
    return {"regions": regions,
            "activities": [{"type": a, "regions": ["region_1"], "params": {}} for a in activities]}


@pytest.fixture(scope="module")
def cams():
    tables = asyncio.run(_fetch("SELECT count(*) AS n FROM information_schema.tables "
                                "WHERE table_schema = 'public'"))[0]["n"]
    if tables:                                          # RAIL 2
        pytest.fail(f"EVENTS_TEST_DB is not empty ({tables} tables) — refusing to run")
    command.upgrade(_cfg(), "head")

    out = {}
    for slug, name, cfg in (
        ("gate-a1b2", "Gate", _config("restricted_zone_entry", "no_person_area")),
        ("yard-c3d4", "Yard", _config("stray_parking")),
        ("lobby-e5f6", "Lobby", {}),
    ):
        row = asyncio.run(_fetch(
            "INSERT INTO cameras (slug, name, rtsp_url, stage, analytics_config) "
            "VALUES ($1, $2, $3, 'registered', $4::jsonb) RETURNING id",
            slug, name, f"rtsp://10.0.0.1/{slug}", json.dumps(cfg)))
        out[slug] = row[0]["id"]
    return out


@pytest.fixture(autouse=True)
def clean(cams):
    asyncio.run(_fetch("DELETE FROM analytics_events"))
    yield


def _ingest(events, now=None):
    from backend.models import Camera
    from backend.services import ai_events as ev
    from sqlalchemy import select

    async def go(s):
        cams = (await s.execute(select(Camera))).scalars().all()
        rows, rejected = ev.prepare([ev.EventIn(**e) for e in events],
                                    {c.slug: c for c in cams},
                                    now=now or datetime.now(timezone.utc),
                                    default_retention_days=30)
        counts = await ev.record(s, rows) if rows else {}
        return counts, rejected

    return _session(go)


def _ago(**kw):
    return datetime.now(timezone.utc).replace(microsecond=0) - timedelta(**kw)


def _ev(slug="gate-a1b2", activity="restricted_zone_entry", **kw):
    return {"sensor_id": slug, "activity": activity, "started_at": _ago(minutes=1), **kw}


def _list(**filters):
    from backend.services import ai_events as ev

    async def go(s):
        return await ev.list_events(s, ev.EventFilter(**filters), await ev.load_catalog(s))

    return _session(go)


class TestUpsert:
    def test_a_retry_is_a_duplicate_not_a_second_event(self, cams):
        e = _ev(id=str(uuid.uuid4()))
        assert _ingest([e])[0] == {"inserted": 1, "closed": 0, "duplicates": 0}
        assert _ingest([e])[0] == {"inserted": 0, "closed": 0, "duplicates": 1}
        assert len(_list()[0]) == 1

    def test_posting_the_end_closes_an_open_event_once(self, cams):
        ident, start = str(uuid.uuid4()), _ago(minutes=5)
        _ingest([_ev(id=ident, started_at=start)])
        assert _ingest([_ev(id=ident, started_at=start, ended_at=start + timedelta(seconds=40))])[0] \
            == {"inserted": 0, "closed": 1, "duplicates": 0}
        # A second, different end does not move it: stored events are immutable.
        assert _ingest([_ev(id=ident, started_at=start, ended_at=start + timedelta(seconds=90))])[0] \
            == {"inserted": 0, "closed": 0, "duplicates": 1}
        [only] = _list()[0]
        assert only["duration_s"] == 40.0

    def test_unconfigured_activities_never_reach_the_table(self, cams):
        counts, rejected = _ingest([_ev(activity="person_detection"),
                                    _ev(slug="lobby-e5f6", activity="restricted_zone_entry")])
        assert counts == {} and [r["reason"] for r in rejected] == ["activity_not_configured"] * 2
        assert _list()[0] == []


class TestOrderingAndFilters:
    def _seed(self):
        _ingest([
            _ev(started_at=_ago(minutes=30)),
            _ev(activity="no_person_area", started_at=_ago(minutes=10)),
            _ev(slug="yard-c3d4", activity="stray_parking", started_at=_ago(minutes=20)),
            _ev(started_at=_ago(minutes=2)),
            _ev(slug="yard-c3d4", activity="stray_parking", started_at=_ago(hours=5)),
        ])

    def test_newest_first_across_every_camera(self, cams):
        self._seed()
        events, _ = _list()
        starts = [e["started_at"] for e in events]
        assert len(events) == 5 and starts == sorted(starts, reverse=True)

    def test_simultaneous_events_order_by_id(self, cams):
        t = _ago(minutes=3)
        _ingest([_ev(started_at=t, track_id=n) for n in range(4)])
        ids = [e["id"] for e in _list()[0]]
        assert ids == sorted(ids, reverse=True)

    def test_camera_filter(self, cams):
        self._seed()
        events, _ = _list(camera_ids=[cams["yard-c3d4"]])
        assert {e["camera"]["slug"] for e in events} == {"yard-c3d4"} and len(events) == 2

    def test_activity_filter(self, cams):
        self._seed()
        events, _ = _list(camera_ids=[cams["gate-a1b2"]], activity="no_person_area")
        assert [e["activity"]["key"] for e in events] == ["no_person_area"]

    def test_date_range_is_inclusive_on_start(self, cams):
        edge = _ago(minutes=20)
        _ingest([_ev(started_at=edge), _ev(started_at=_ago(minutes=40)), _ev(started_at=_ago(minutes=5))])
        events, _ = _list(frm=edge, to=_ago(minutes=5))
        assert len(events) == 2
        assert all(datetime.fromisoformat(e["started_at"].replace("Z", "+00:00")) >= edge
                   for e in events)

    def test_pages_neither_overlap_nor_skip(self, cams):
        _ingest([_ev(started_at=_ago(minutes=n % 3), track_id=n) for n in range(23)])
        seen, before = [], None
        from backend.services import ai_events as ev
        while True:
            page, nxt = _list(limit=5, before=ev.Cursor.decode(before) if before else None)
            seen += [e["id"] for e in page]
            if not nxt:
                break
            before = nxt
        assert len(seen) == 23 and len(set(seen)) == 23


class TestOverview:
    def _overview(self, per_camera=5):
        from backend.services import ai_events as ev

        async def go(s):
            return await ev.overview(s, per_camera=per_camera)

        return _session(go)

    def test_the_ticker_is_the_ten_newest_across_cameras(self, cams):
        # Gate events 1..12 minutes old; the yard event, 30 s old, is the newest.
        _ingest([_ev(started_at=_ago(minutes=n), track_id=n) for n in range(1, 13)]
                + [_ev(slug="yard-c3d4", activity="stray_parking", started_at=_ago(seconds=30))])
        out = self._overview()
        latest = out["latest"]
        assert len(latest) == 10
        assert latest[0]["camera"]["slug"] == "yard-c3d4"
        starts = [e["started_at"] for e in latest]
        assert starts == sorted(starts, reverse=True)

    def test_one_card_per_configured_camera_quiet_ones_included(self, cams):
        _ingest([_ev(started_at=_ago(minutes=n), track_id=n) for n in range(8)])
        cards = {c["camera"]["slug"]: c for c in self._overview(per_camera=5)["cameras"]}
        assert set(cards) == {"gate-a1b2", "yard-c3d4"}          # lobby has nothing configured
        assert [a["key"] for a in cards["gate-a1b2"]["activities"]] == [
            "restricted_zone_entry", "no_person_area"]
        assert len(cards["gate-a1b2"]["events"]) == 5
        starts = [e["started_at"] for e in cards["gate-a1b2"]["events"]]
        assert starts == sorted(starts, reverse=True)
        assert cards["yard-c3d4"]["events"] == [] and cards["yard-c3d4"]["last_event_at"] is None


class TestRetention:
    def test_expired_rows_are_hidden_then_swept(self, cams):
        from backend.services import ai_events as ev

        _ingest([_ev(), _ev(started_at=_ago(minutes=3))])
        asyncio.run(_fetch("UPDATE analytics_events SET expires_at = now() - interval '1 second' "
                           "WHERE started_at < now() - interval '2 minutes'"))
        assert len(_list()[0]) == 1

        async def sweep(s):
            return await ev.purge_expired(s)

        assert _session(sweep) == 1
        assert asyncio.run(_fetch("SELECT count(*) AS n FROM analytics_events"))[0]["n"] == 1


class TestEntryExitCounts:
    """Rows are written directly: the count reads what is stored, whoever stored it."""

    def _store(self, cam, at, direction, activity="entry_exit_WLE_logs", tripwire="region_2",
               expires=timedelta(days=1)):
        attrs = {"tripwire_id": tripwire, **({"direction": direction} if direction else {})}
        asyncio.run(_fetch(
            "INSERT INTO analytics_events (id, camera_id, activity, zone, started_at, attributes, "
            "source, expires_at) VALUES ($1, $2, $3, 'Door line', $4, $5::jsonb, 'cpu', now() + $6)",
            uuid.uuid4(), cam, activity, at, json.dumps(attrs), expires))

    def _series(self, cam, minutes=60, tripwire=None, offset=0):
        from backend.services import entry_exit as ee

        win = ee.window(minutes, datetime.now(timezone.utc), offset)

        async def go(s):
            return await ee.counts(s, cam, win, tripwire)

        return ee.series(_session(go), win), win

    @staticmethod
    def _index(win, at):
        return (int(at.timestamp()) + win.offset_s) // win.bucket_s - win.first_slot

    def test_each_crossing_is_counted_once_in_its_bucket_entry_and_exit_apart(self, cams):
        door, yard = cams["gate-a1b2"], cams["yard-c3d4"]
        entries = [_ago(minutes=m) for m in (2, 2, 17, 44)]
        exits = [_ago(minutes=m) for m in (3, 30)]
        for at in entries:
            self._store(door, at, "entry")
        for at in exits:
            self._store(door, at, "exit")
        self._store(door, _ago(hours=2), "entry")                                  # before the window
        self._store(door, _ago(minutes=5), None)                                   # an old directionless touch
        self._store(door, _ago(minutes=5), "entry", activity="restricted_zone_entry")
        self._store(yard, _ago(minutes=5), "exit")                                 # another camera

        out, win = self._series(door)
        assert out["totals"] == {"entry": 4, "exit": 2}
        assert sum(b["entry"] for b in out["buckets"]) == 4 and sum(b["exit"] for b in out["buckets"]) == 2
        expected = [[0, 0] for _ in out["buckets"]]
        for at in entries:
            expected[self._index(win, at)][0] += 1
        for at in exits:
            expected[self._index(win, at)][1] += 1
        assert [[b["entry"], b["exit"]] for b in out["buckets"]] == expected

    def test_one_tripwire_counts_only_its_own_crossings(self, cams):
        door = cams["gate-a1b2"]
        self._store(door, _ago(minutes=4), "entry", tripwire="region_2")
        self._store(door, _ago(minutes=4), "entry", tripwire="region_3")
        self._store(door, _ago(minutes=6), "exit", tripwire="region_3")
        assert self._series(door, tripwire="region_3")[0]["totals"] == {"entry": 1, "exit": 1}
        assert self._series(door)[0]["totals"] == {"entry": 2, "exit": 1}

    def test_expired_records_are_not_counted(self, cams):
        door = cams["gate-a1b2"]
        self._store(door, _ago(minutes=4), "entry")
        self._store(door, _ago(minutes=4), "entry", expires=timedelta(seconds=-1))
        assert self._series(door, minutes=5)[0]["totals"] == {"entry": 1, "exit": 0}

    def test_buckets_follow_the_viewers_clock(self, cams):
        door = cams["gate-a1b2"]
        times = [_ago(minutes=m) for m in (10, 70, 130, 600)]
        for at in times:
            self._store(door, at, "exit")
        out, win = self._series(door, minutes=1440, offset=330)
        assert (int(win.start.timestamp()) + 330 * 60) % 3600 == 0
        assert out["totals"] == {"entry": 0, "exit": 4}
        for at in times:
            assert out["buckets"][self._index(win, at)]["exit"] >= 1


def test_the_fleet_time_index_exists(cams):
    rows = asyncio.run(_fetch("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_events_started_at'"))
    assert rows and "started_at DESC, id DESC" in rows[0]["indexdef"]
