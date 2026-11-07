"""Playback presence against a real PostgreSQL — the merge query and the CHECKs.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, with the same two rails as
test_ai_events_db.py: DATABASE_URL is overridden for alembic, and the scratch
database must start empty.

    PRESENCE_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \\
        python -m pytest services/camera-mgmt/tests/test_timeline_presence_db.py

WHAT IT PROVES, none of which the logic tests can:
  * a retried segment is a duplicate, not a second row
  * segments closer than the merge gap become one bar, counting what it absorbed;
    farther apart they stay two; kinds never merge into each other
  * a segment that began before the window but reaches into it is found (the
    MAX_SEGMENT lower bound), one that ended before it is not
  * expired rows are hidden before the sweep and deleted by it
  * a range erasure deletes exactly what overlaps the range
  * the table refuses an over-long segment itself (migration 043's CHECK)
  * motion is a kind of its own (migration 044)
  * next / previous: island starts only (a long stay's later segments are not
    "the next person"), compared on the seek point so a jump moves on,
    events and presence together, hidden activities skipped
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DB_URL = os.environ.get("PRESENCE_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set PRESENCE_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                                  # noqa: E402
    from alembic.config import Config                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

T0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


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
def cam():
    tables = asyncio.run(_fetch("SELECT count(*) AS n FROM information_schema.tables "
                                "WHERE table_schema = 'public'"))[0]["n"]
    if tables:                                          # RAIL 2
        pytest.fail(f"PRESENCE_TEST_DB is not empty ({tables} tables) — refusing to run")
    command.upgrade(_cfg(), "head")
    row = asyncio.run(_fetch(
        "INSERT INTO cameras (slug, name, rtsp_url, stage, search_indexing, search_domains, "
        "motion_detection, analytics_config) "
        "VALUES ('gate-a1b2', 'Gate', 'rtsp://10.0.0.1/gate', 'registered', true, "
        "ARRAY['person','vehicles'], true, "
        "'{\"regions\": {}, \"activities\": [{\"type\": \"loitering\", \"regions\": [], "
        "\"params\": {}}, {\"type\": \"intrusion\", \"regions\": [], \"params\": {}}]}'::jsonb) "
        "RETURNING id"))
    return row[0]["id"]


@pytest.fixture(autouse=True)
def clean(cam):
    asyncio.run(_fetch("DELETE FROM timeline_presence"))
    asyncio.run(_fetch("DELETE FROM analytics_events"))
    yield


def _store(*segs, now=None):
    """segs: (kind, start offset s, end offset s[, track])"""
    from sqlalchemy import select

    from backend.models import Camera
    from backend.services import timeline as tl

    async def go(s):
        cams = (await s.execute(select(Camera))).scalars().all()
        items = [tl.PresenceIn(sensor_id="gate-a1b2", kind=k,
                               started_at=T0 + timedelta(seconds=a),
                               ended_at=T0 + timedelta(seconds=b),
                               track_id=(rest[0] if rest else f"t{a}"),
                               # Each kind from its real producer: motion from the
                               # motion service, the rest from the engine (CPU).
                               source="motion" if k == "motion" else "cpu")
                 for k, a, b, *rest in segs]
        rows, rejected = tl.prepare(items, {c.slug: c for c in cams},
                                    now=now or T0 + timedelta(minutes=30))
        assert rejected == []
        return await tl.record(s, rows)

    return _session(go)


def _read(frm_s, to_s, px=1200, kinds=("person", "vehicle")):
    from backend.services import timeline as tl

    async def go(s):
        cid = (await _fetch("SELECT id FROM cameras"))[0]["id"]
        return await tl.presence(s, cid, T0 + timedelta(seconds=frm_s),
                                 T0 + timedelta(seconds=to_s), list(kinds), px)

    return _session(go)


def _bars(data, kind):
    return [(round(s - T0.timestamp()), round(e - T0.timestamp()), n)
            for s, e, n in data["kinds"][kind]]


def test_a_retry_is_a_duplicate():
    assert _store(("person", 0, 10, "a")) == {"inserted": 1, "duplicates": 0}
    assert _store(("person", 0, 10, "a")) == {"inserted": 0, "duplicates": 1}


def test_close_segments_merge_and_far_ones_do_not():
    # 10-minute window at 600 px: 1 s/px, so the gap is the 3 s floor.
    _store(("person", 0, 10), ("person", 12, 20), ("person", 60, 70), ("vehicle", 5, 15))
    data = _read(0, 600, px=600)
    assert _bars(data, "person") == [(0, 20, 2), (60, 70, 1)]
    assert _bars(data, "vehicle") == [(5, 15, 1)], "kinds never merge into each other"


def test_zooming_out_merges_more():
    _store(("person", 0, 10), ("person", 60, 70))
    # One day at 1200 px: 72 s/px, gap 144 s — the two become one bar.
    data = _read(-43200, 43200, px=1200)
    assert _bars(data, "person") == [(0, 70, 2)]


def test_a_segment_reaching_into_the_window_is_found():
    _store(("person", -300, 30), ("person", -400, -350))
    assert _bars(_read(0, 600), "person") == [(-300, 30, 1)]


def test_expired_rows_are_hidden_then_swept():
    from backend.services import timeline as tl

    _store(("person", 0, 10))
    asyncio.run(_fetch("UPDATE timeline_presence SET expires_at = now() - interval '1 minute'"))
    assert _bars(_read(0, 600), "person") == []
    assert _session(tl.purge_expired) == 1


def test_range_erasure_deletes_only_the_overlap():
    from backend.services import timeline as tl

    _store(("person", 0, 10), ("person", 100, 110), ("vehicle", 200, 210))

    async def go(s):
        cid = (await _fetch("SELECT id FROM cameras"))[0]["id"]
        return await tl.erase(s, cid, T0 + timedelta(seconds=5), T0 + timedelta(seconds=150))

    assert _session(go) == 2
    assert _bars(_read(0, 600), "vehicle") == [(200, 210, 1)]


def test_the_table_refuses_an_over_long_segment():
    with pytest.raises(Exception, match="ck_timeline_presence_bounded"):
        asyncio.run(_fetch(
            "INSERT INTO timeline_presence (id, camera_id, kind, started_at, ended_at, expires_at) "
            "SELECT gen_random_uuid(), id, 'person', now() - interval '11 minutes', now(), "
            "now() + interval '1 day' FROM cameras"))


def test_motion_is_a_kind_of_its_own():
    _store(("motion", 0, 30), ("person", 10, 20))
    data = _read(0, 600, kinds=("person", "vehicle", "motion"))
    assert _bars(data, "motion") == [(0, 30, 1)]
    assert _bars(data, "person") == [(10, 20, 1)]


def _event(activity, offset_s):
    asyncio.run(_fetch(
        "INSERT INTO analytics_events (id, camera_id, activity, started_at, source, expires_at) "
        "SELECT gen_random_uuid(), id, $1, $2, 'cpu', now() + interval '1 day' FROM cameras",
        activity, T0 + timedelta(seconds=offset_s)))


def _adjacent(at_s, direction="next", kinds=("person", "vehicle", "motion"), events=True,
              hidden=()):
    from backend.services import timeline as tl

    async def go(s):
        cid = (await _fetch("SELECT id FROM cameras"))[0]["id"]
        return await tl.adjacent(s, cid, T0 + timedelta(seconds=at_s), direction,
                                 list(kinds), events, list(hidden))

    m = _session(go)
    return None if m is None else (m["row"], round(m["start"] - T0.timestamp()))


def test_next_skips_the_later_segments_of_one_long_stay():
    # One person for 4 minutes, cut into two segments; a second person later.
    _store(("person", 0, 120, "a"), ("person", 120, 240, "a"), ("person", 400, 410, "b"))
    assert _adjacent(-60) == ("person", 0)
    assert _adjacent(-3) == ("person", 400), "from the first bar's seek point, move on"


def test_prev_goes_back_to_the_start_of_the_bar_before():
    _store(("person", 0, 120, "a"), ("person", 120, 240, "a"), ("person", 400, 410, "b"))
    assert _adjacent(397, "prev") == ("person", 0)
    assert _adjacent(-10, "prev") is None


def test_events_and_presence_are_walked_together():
    _store(("vehicle", 100, 110))
    _event("loitering", 50)
    _event("intrusion", 200)
    assert _adjacent(0) == ("event", 50)
    assert _adjacent(45) == ("vehicle", 100)
    assert _adjacent(99) == ("event", 200)


def test_hidden_activities_and_unasked_kinds_are_skipped():
    _store(("vehicle", 100, 110))
    _event("loitering", 50)
    assert _adjacent(0, hidden=("loitering",)) == ("vehicle", 100)
    assert _adjacent(0, kinds=(), hidden=("loitering",)) is None
    assert _adjacent(0, kinds=("person",), events=False) is None
