"""Time-lapse against a real PostgreSQL (and a real ffmpeg) — the half a stub cannot reach.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, with the same two rails as
test_heatmaps_db.py: DATABASE_URL is overridden for alembic, and the scratch
database must start empty.

    TIMELAPSE_TEST_DB=postgresql+asyncpg://postgres:pw@localhost:55432/scratch \\
        python -m pytest services/camera-mgmt/tests/test_timelapse_db.py

WHAT IT PROVES:
  * migration 057 leaves every existing camera OFF
  * a retried insert of the same still is one row (the unique capture index)
  * the capture tick takes a still only from an enabled camera inside its schedule
  * days / frames / totals read back what was stored, by LOCAL day
  * expiry and the per-camera size cap delete the oldest stills first
  * DSR erasure takes the stills in the window AND any video overlapping it
  * a render queues, runs real ffmpeg, and lands as a playable MP4; too few
    stills is refused before it is queued; the nightly render is made once
  * deleting the camera takes its stills and videos with it (ON DELETE CASCADE)
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DB_URL = os.environ.get("TIMELAPSE_TEST_DB")

pytestmark = pytest.mark.skipif(not DB_URL, reason="set TIMELAPSE_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    pytest.importorskip("PIL")
    from alembic import command                                  # noqa: E402
    from alembic.config import Config                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace("postgresql://", "postgresql+asyncpg://")


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


def _run(fn, monkeypatch):
    """Run `fn()` with every AsyncSessionLocal the service opens bound to a fresh
    engine on THIS loop (an asyncpg pool belongs to one loop)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import backend.db as dbmod

    async def go():
        engine = create_async_engine(_alembic_url())
        monkeypatch.setattr(dbmod, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
        try:
            return await fn()
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _jpeg(colour=(180, 180, 180), size=(640, 360)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture(scope="module")
def cams():
    tables = asyncio.run(_fetch("SELECT count(*) AS n FROM information_schema.tables "
                                "WHERE table_schema = 'public'"))[0]["n"]
    if tables:                                          # RAIL 2
        pytest.fail(f"TIMELAPSE_TEST_DB is not empty ({tables} tables) — refusing to run")
    command.upgrade(_cfg(), "head")
    out = {}
    for slug in ("gate-a1b2", "yard-c3d4"):
        row = asyncio.run(_fetch(
            "INSERT INTO cameras (slug, name, rtsp_url, stage) VALUES ($1, $2, $3, 'registered') RETURNING id",
            slug, slug.split("-")[0].title(), f"rtsp://10.0.0.1/{slug}"))
        out[slug] = row[0]["id"]
    return out


@pytest.fixture(autouse=True)
def clean(cams, monkeypatch):
    from backend.services import object_store, timelapse as tl
    monkeypatch.setattr(object_store, "is_configured", lambda: False)     # bytes in the row
    asyncio.run(_fetch("DELETE FROM timelapse_frames"))
    asyncio.run(_fetch("DELETE FROM timelapse_renders"))
    asyncio.run(_fetch("UPDATE cameras SET timelapse_enabled = false, timelapse_config = '{}'::jsonb"))
    for d in (tl._last_attempt, tl._retry_after, tl._prev_thumb):
        d.clear()
    tl._inflight.clear()
    yield


def _processed(colour=(180, 180, 180)):
    from backend.services import timelapse as tl
    return tl.process(_jpeg(colour), masks=[], resolution="original", quality=80)


def _store(cam_id, at, monkeypatch, retention_days=90, colour=(180, 180, 180)):
    from backend.services import timelapse as tl
    return _run(lambda: tl.store_frame(cam_id, "gate-a1b2", at, _processed(colour), retention_days), monkeypatch)


T0 = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)


def test_migration_leaves_every_camera_off(cams):
    rows = asyncio.run(_fetch("SELECT timelapse_enabled, timelapse_config FROM cameras"))
    assert rows and all(r["timelapse_enabled"] is False for r in rows)


def test_a_retried_still_is_one_row_and_reads_back_by_local_day(cams, monkeypatch):
    from backend.services import timelapse as tl
    cam = cams["gate-a1b2"]
    _store(cam, T0, monkeypatch)
    _store(cam, T0, monkeypatch)                     # the retry
    _store(cam, T0 + timedelta(minutes=5), monkeypatch)
    assert asyncio.run(_fetch("SELECT count(*) AS n FROM timelapse_frames"))[0]["n"] == 2

    async def reads():
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as s:
            return await tl.days(s, cam), await tl.frames_of_day(s, cam, T0.astimezone().date()), await tl.totals(s, cam)

    days, frames, totals = _run(reads, monkeypatch)
    assert days[0]["day"] == T0.astimezone().date().isoformat() and days[0]["frames"] == 2
    assert [f["captured_at"] for f in frames] == sorted(f["captured_at"] for f in frames)
    assert totals["frames"] == 2 and totals["bytes"] > 0


def test_the_tick_captures_only_enabled_cameras_inside_their_schedule(cams, monkeypatch):
    from backend.services import snapshot, timelapse as tl
    calls = []

    async def snap(slug, wait_s=None):
        calls.append(slug)
        return _jpeg()

    async def status(*a, **k):
        return None

    monkeypatch.setattr(snapshot, "shared_snapshot", snap)
    monkeypatch.setattr(tl, "_status", status)
    asyncio.run(_fetch("UPDATE cameras SET timelapse_enabled = true, timelapse_config = $1::jsonb WHERE slug = 'gate-a1b2'",
                       '{"schedule": null, "interval_s": 60}'))
    # yard is enabled too, but its schedule is a window that is never "now"
    asyncio.run(_fetch("UPDATE cameras SET timelapse_enabled = true, timelapse_config = $1::jsonb WHERE slug = 'yard-c3d4'",
                       '{"schedule": {"mode": "monthly", "rules": [{"days": [31], "start": "00:00", "end": "00:01"}]}}'))

    async def tick():
        tasks = await tl.capture_tick(now=datetime(2026, 2, 3, 10, 0, tzinfo=timezone.utc))
        return await asyncio.gather(*tasks)

    out = _run(tick, monkeypatch)
    assert out == ["captured"] and calls == ["gate-a1b2"]
    # the next tick inside the interval takes nothing
    out = _run(lambda: tl.capture_tick(now=datetime(2026, 2, 3, 10, 0, 30, tzinfo=timezone.utc)), monkeypatch)
    assert out == []


def test_expiry_and_the_size_cap_take_the_oldest_first(cams, monkeypatch):
    from backend.services import timelapse as tl
    cam = cams["gate-a1b2"]
    _store(cam, T0 - timedelta(days=40), monkeypatch, retention_days=30)        # already expired
    for i in range(6):
        _store(cam, T0 + timedelta(minutes=i), monkeypatch, colour=(i * 40, 90, 200 - i * 30))

    async def sweep():
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as s:
            return await tl.purge_expired(s)

    assert _run(sweep, monkeypatch) == 1
    each = asyncio.run(_fetch("SELECT max(bytes) AS b FROM timelapse_frames"))[0]["b"]
    # a cap of 50 MB is the floor the schema allows; fake the arithmetic by
    # setting the cap below what is stored via a tiny-frame budget
    monkeypatch.setattr(tl, "effective_config", lambda raw: tl.TimelapseConfig(max_storage_mb=50))
    asyncio.run(_fetch("UPDATE timelapse_frames SET bytes = $1", 15 * 1024 * 1024))   # 6 × 15 MB = 90 MB
    async def caps():
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as s:
            return await tl.enforce_caps(s)
    assert _run(caps, monkeypatch) == 3                                           # back under 50 MB
    left = asyncio.run(_fetch("SELECT captured_at FROM timelapse_frames ORDER BY captured_at"))
    assert [r["captured_at"] for r in left] == [T0 + timedelta(minutes=i) for i in (3, 4, 5)]
    assert each > 0


def test_dsr_erasure_takes_the_window_and_any_video_across_it(cams, monkeypatch):
    from backend.services import timelapse as tl
    cam = cams["gate-a1b2"]
    for i in range(5):
        _store(cam, T0 + timedelta(hours=i), monkeypatch)
    asyncio.run(_fetch(
        "INSERT INTO timelapse_renders (id, camera_id, range_start, range_end, status, expires_at) "
        "VALUES ($1, $2, $3, $4, 'done', now() + interval '1 day'), ($5, $2, $6, $7, 'done', now() + interval '1 day')",
        uuid.uuid4(), cam, T0, T0 + timedelta(hours=4), uuid.uuid4(), T0 + timedelta(days=5), T0 + timedelta(days=6)))

    async def erase(ref):
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as s:
            return await tl.erase_range(s, ref, T0 + timedelta(minutes=30), T0 + timedelta(hours=2, minutes=30))

    assert _run(lambda: erase("gate-a1b2"), monkeypatch) == 3                     # 2 stills + 1 overlapping video
    assert asyncio.run(_fetch("SELECT count(*) AS n FROM timelapse_frames"))[0]["n"] == 3
    assert asyncio.run(_fetch("SELECT count(*) AS n FROM timelapse_renders"))[0]["n"] == 1
    assert _run(lambda: erase(str(uuid.uuid4())), monkeypatch) == 0              # no such camera: nothing


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed here")
def test_a_render_runs_real_ffmpeg_and_lands_as_an_mp4(cams, monkeypatch):
    from backend.models import Camera
    from backend.services import timelapse as tl
    cam_id = cams["gate-a1b2"]
    for i in range(30):
        _store(cam_id, T0 + timedelta(minutes=5 * i), monkeypatch, colour=(i * 8, 100, 255 - i * 8))

    async def queue_and_run():
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as s:
            cam = await s.get(Camera, cam_id)
            with pytest.raises(tl.Rejected):
                await tl.enqueue_render(s, cam, start=T0 - timedelta(days=3), end=T0 - timedelta(days=2),
                                        fps=24, overlay=True, deflicker=False)
            row = await tl.enqueue_render(s, cam, start=T0, end=T0 + timedelta(hours=3), fps=24,
                                          overlay=True, deflicker=True, requested_by="alice")
            rid = row.id
        status = await tl.run_render(rid)
        async with dbmod.AsyncSessionLocal() as s:
            from backend.models import TimelapseRender
            r = await s.get(TimelapseRender, rid)
            return status, r, await tl.video_bytes(r)

    status, r, video = _run(queue_and_run, monkeypatch)
    assert status == "done", r.error
    assert r.frames == 30 and r.encoder == "libx264" and r.progress == 1.0
    assert video[4:8] == b"ftyp" and r.bytes == len(video)


def test_the_nightly_render_is_queued_once(cams, monkeypatch):
    from backend.services import timelapse as tl
    cam = cams["gate-a1b2"]
    asyncio.run(_fetch("UPDATE cameras SET timelapse_enabled = true, timelapse_config = $1::jsonb WHERE slug = 'gate-a1b2'",
                       '{"auto_render_daily": true}'))
    now = datetime.now(timezone.utc).astimezone().replace(hour=6, minute=0, second=0, microsecond=0)
    y = now - timedelta(days=1)
    for h in (9, 12, 15):
        _store(cam, y.replace(hour=h).astimezone(timezone.utc), monkeypatch)
    assert _run(lambda: tl.enqueue_daily(now=now), monkeypatch) == 1
    assert _run(lambda: tl.enqueue_daily(now=now), monkeypatch) == 0
    row = asyncio.run(_fetch("SELECT kind, frames, requested_by FROM timelapse_renders"))[0]
    assert (row["kind"], row["frames"], row["requested_by"]) == ("daily", 3, "schedule")


def test_deleting_the_camera_takes_its_stills_and_videos(cams, monkeypatch):
    cam = asyncio.run(_fetch("INSERT INTO cameras (slug, name, rtsp_url, stage) "
                             "VALUES ('tmp-0000', 'Tmp', 'rtsp://x/tmp', 'registered') RETURNING id"))[0]["id"]
    from backend.services import timelapse as tl
    _run(lambda: tl.store_frame(cam, "tmp-0000", T0, _processed(), 90), monkeypatch)
    asyncio.run(_fetch("INSERT INTO timelapse_renders (id, camera_id, range_start, range_end, expires_at) "
                       "VALUES ($1, $2, $3, $3, now())", uuid.uuid4(), cam, T0))
    asyncio.run(_fetch("DELETE FROM cameras WHERE id = $1", cam))
    assert asyncio.run(_fetch("SELECT count(*) AS n FROM timelapse_frames WHERE camera_id = $1", cam))[0]["n"] == 0
    assert asyncio.run(_fetch("SELECT count(*) AS n FROM timelapse_renders WHERE camera_id = $1", cam))[0]["n"] == 0


def test_daily_picks_the_still_nearest_noon_per_day_and_honours_since(cams, monkeypatch):
    from backend.services import timelapse as tl
    monkeypatch.setattr(tl, "_local", lambda dt: dt.astimezone(timezone.utc))   # appliance clock = UTC here
    cam = cams["yard-c3d4"]
    d1 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for h in (8, 11, 13, 17):                                    # 11:00 and 13:00 tie at 1 h; 11:00 is first
        _store(cam, d1.replace(hour=h), monkeypatch)
    _store(cam, d1.replace(day=2, hour=12, minute=20), monkeypatch)
    _store(cam, d1.replace(day=2, hour=9), monkeypatch)

    async def read(since=None):
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as s:
            return await tl.daily(s, cam, since=since)

    out = _run(read, monkeypatch)
    assert [(d["day"], d["captured_at"][11:16]) for d in out] == [("2026-09-01", "11:00"), ("2026-09-02", "12:20")]
    out = _run(lambda: read(since=d1.replace(day=2).date()), monkeypatch)
    assert [d["day"] for d in out] == ["2026-09-02"]


def test_migration_061_keeps_only_what_a_camera_changed(cams):
    """Configs written out in full before 061 shrink to the keys that differ from the built-in defaults — so a
    camera that never changed anything starts following the Defaults page, and one on 1 minute keeps its minute."""
    import json
    from backend.services import timelapse as tl
    full = tl.TimelapseConfig().model_dump()
    command.downgrade(_cfg(), "060")
    try:
        asyncio.run(_fetch("UPDATE cameras SET timelapse_config = $1::jsonb WHERE id = $2",
                           json.dumps(full), cams["gate-a1b2"]))
        asyncio.run(_fetch("UPDATE cameras SET timelapse_config = $1::jsonb WHERE id = $2",
                           json.dumps({**full, "interval_s": 60}), cams["yard-c3d4"]))
    finally:
        command.upgrade(_cfg(), "head")
    rows = {r["id"]: json.loads(r["timelapse_config"]) if isinstance(r["timelapse_config"], str) else r["timelapse_config"]
            for r in asyncio.run(_fetch("SELECT id, timelapse_config FROM cameras"))}
    assert rows[cams["gate-a1b2"]] == {} and rows[cams["yard-c3d4"]] == {"interval_s": 60}


def test_the_disk_guard_pauses_every_camera_and_resume_needs_room(cams, monkeypatch):
    from collections import namedtuple
    from backend.services import timelapse as tl
    du = namedtuple("du", "total used free")
    asyncio.run(_fetch("UPDATE cameras SET timelapse_enabled = true"))
    asyncio.run(_fetch("DELETE FROM timelapse_settings"))
    tl._site.update(defaults={}, guard={}, paused=None, at=-1e9)
    monkeypatch.setattr(tl.shutil, "disk_usage", lambda p: du(100 * 1024 ** 3, 95 * 1024 ** 3, 5 * 1024 ** 3))

    async def tick():
        import backend.db as dbmod
        async with dbmod.AsyncSessionLocal() as db:
            return await tl.guard_tick(db)

    pause = _run(tick, monkeypatch)
    assert pause and "95% full" in pause["reason"] and len(pause["camera_ids"]) == 2
    assert not any(r["timelapse_enabled"] for r in asyncio.run(_fetch("SELECT timelapse_enabled FROM cameras")))
    stored = asyncio.run(_fetch("SELECT paused FROM timelapse_settings WHERE id = 1"))[0]["paused"]
    assert "camera_ids" in str(stored)
    # Never back on by itself, even with room again.
    monkeypatch.setattr(tl.shutil, "disk_usage", lambda p: du(100 * 1024 ** 3, 10 * 1024 ** 3, 90 * 1024 ** 3))
    assert _run(tick, monkeypatch) is None
    assert not any(r["timelapse_enabled"] for r in asyncio.run(_fetch("SELECT timelapse_enabled FROM cameras")))
