"""Heatmaps against a real PostgreSQL — the half a stub cannot reach.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, with the same two rails as
test_ai_events_db.py: DATABASE_URL is overridden for alembic (inside the api
container it names the LIVE database), and the scratch database must start
empty.

    HEATMAP_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \\
        python -m pytest services/camera-mgmt/tests/test_heatmaps_db.py

WHAT IT PROVES, none of which the logic tests can:
  * the session upsert: a retried upload updates one row, never writes a second
  * the unique (session, kind, captured_at) index: the same minute's picture
    cannot land twice, and a retry replaces its bytes
  * the overview's DISTINCT ON: one row per camera, its newest picture, and
    only cameras that actually have the activity configured
  * the runtime switch survives as a row and drives the state machine:
    OFF freezes an active session, ON resumes a frozen one, ON with "new"
    closes it, and a CLOSED session is never reopened by either
  * the switch never rewrites `cameras.analytics_config` — the schedule is not
    the switch, and this is the test that says so against real JSONB
  * snapshots are pruned to the rolling window while `final` pictures stay
  * expired pictures are hidden from reads before the sweep, deleted by it, and
    a closed session with no pictures left goes with them
  * a deleted camera takes its sessions and pictures with it (ON DELETE CASCADE)
  * an accumulation abandoned by a restart is closed as `interrupted` — by the
    next session id for that camera, or by the sweep if the camera never
    returns — while a FROZEN session stays resumable however long it is quiet
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

DB_URL = os.environ.get("HEATMAP_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set HEATMAP_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                                  # noqa: E402
    from alembic.config import Config                            # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JPEG = b"\xff\xd8\xff\xe0" + b"heatmap" * 100
OTHER_JPEG = b"\xff\xd8\xff\xe0" + b"newer!!" * 100


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
    return {"regions": {},
            "activities": [{"type": a, "regions": [], "params": {}} for a in activities]}


@pytest.fixture(scope="module")
def cams():
    tables = asyncio.run(_fetch("SELECT count(*) AS n FROM information_schema.tables "
                                "WHERE table_schema = 'public'"))[0]["n"]
    if tables:                                          # RAIL 2
        pytest.fail(f"HEATMAP_TEST_DB is not empty ({tables} tables) — refusing to run")
    command.upgrade(_cfg(), "head")

    out = {}
    for slug, name, cfg in (
        ("gate-a1b2", "Gate", _config("Heatmap_overlay")),
        ("yard-c3d4", "Yard", _config("Heatmap_overlay", "stray_parking")),
        ("lobby-e5f6", "Lobby", _config("stray_parking")),
    ):
        row = asyncio.run(_fetch(
            "INSERT INTO cameras (slug, name, rtsp_url, stage, analytics_config) "
            "VALUES ($1, $2, $3, 'registered', $4::jsonb) RETURNING id",
            slug, name, f"rtsp://10.0.0.1/{slug}", json.dumps(cfg)))
        out[slug] = row[0]["id"]
    return out


@pytest.fixture(autouse=True)
def cpu_is_the_engine(cams, monkeypatch):
    """Pin the engine, because the ingest now asks WHO is writing.

    These tests are about storage and lifecycle, not about the engine switch,
    and `heatmaps.DEFAULT_SOURCE` attributes an unsigned message to the CPU
    engine -- so they must run with CPU as the engine or every upload here would
    be refused `source_not_engine`. Left ambient they would pass or fail
    depending on whether the developer's .env happens to have DeepStream on,
    which is how this pinning came to be written. The gate itself is tested in
    TestTheEngineGate below and in test_detection_engine.py.

    Depends on `cams` so DATABASE_URL is already set before backend is imported.
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "deepstream_sync_enabled", False)


@pytest.fixture(autouse=True)
def clean(cams):
    asyncio.run(_fetch("DELETE FROM heatmap_artifacts"))
    asyncio.run(_fetch("DELETE FROM heatmap_sessions"))
    asyncio.run(_fetch("DELETE FROM heatmap_camera_state"))
    asyncio.run(_fetch("DELETE FROM heatmap_chunks"))
    asyncio.run(_fetch("DELETE FROM heatmap_backgrounds"))
    yield


def _store(meta, image=JPEG):
    from backend.services import heatmaps

    async def go(s):
        return await heatmaps.store(s, meta, image)

    return _session(go)


def _meta(slug="gate-a1b2", session_id=None, **kw):
    started = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc).timestamp()
    body = {"kind": "live", "camera": slug,
            "session_id": session_id or uuid.uuid4().hex,
            "session_started_at": started, "captured_at": started + 60,
            "status": "active", "width": 1280, "height": 720,
            "points": 42, "frames": 300, "peak": 17.5,
            "params": {"classes_for_heatmap": ["person"], "intensity": 1.0, "radius": 40}}
    body.update(kw)
    return body


class TestTheUpload:
    def test_one_upload_makes_one_session_and_one_picture(self):
        _store(_meta())
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))) == 1
        rows = asyncio.run(_fetch("SELECT * FROM heatmap_artifacts"))
        assert len(rows) == 1 and bytes(rows[0]["image"]) == JPEG
        assert rows[0]["width"] == 1280 and rows[0]["points"] == 42

    def test_a_retry_updates_rather_than_duplicating(self):
        sid = uuid.uuid4().hex
        _store(_meta(session_id=sid))
        _store(_meta(session_id=sid), image=OTHER_JPEG)      # the sink retried
        rows = asyncio.run(_fetch("SELECT * FROM heatmap_artifacts"))
        assert len(rows) == 1 and bytes(rows[0]["image"]) == OTHER_JPEG
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))) == 1

    def test_the_next_minute_replaces_the_picture_of_the_same_session(self):
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        _store(_meta(session_id=sid, captured_at=started + 60))
        _store(_meta(session_id=sid, captured_at=started + 120), image=OTHER_JPEG)
        rows = asyncio.run(_fetch("SELECT * FROM heatmap_artifacts"))
        assert len(rows) == 1 and bytes(rows[0]["image"]) == OTHER_JPEG
        assert rows[0]["captured_at"].timestamp() == pytest.approx(started + 120)

    def test_a_final_picture_sits_beside_the_last_snapshot(self):
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        _store(_meta(session_id=sid, captured_at=started + 60))
        _store(_meta(session_id=sid, kind="final", status="frozen",
                     session_ended_at=started + 90, captured_at=started + 90,
                     close_reason="operator"))
        kinds = [r["kind"] for r in asyncio.run(
            _fetch("SELECT kind FROM heatmap_artifacts ORDER BY captured_at"))]
        assert kinds == ["live", "final"]
        [session] = asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))
        assert session["status"] == "frozen" and session["close_reason"] == "operator"

    def test_a_closed_notice_carries_no_picture_and_still_closes_the_session(self):
        sid = uuid.uuid4().hex
        _store(_meta(session_id=sid))
        _store(_meta(session_id=sid, kind="closed", status="closed",
                     close_reason="new session"), image=None)
        [session] = asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))
        assert session["status"] == "closed" and session["close_reason"] == "new session"
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_artifacts"))) == 1

    def test_expiry_is_stamped_from_the_cameras_retention(self):
        _store(_meta())
        [row] = asyncio.run(_fetch("SELECT captured_at, expires_at FROM heatmap_artifacts"))
        assert (row["expires_at"] - row["captured_at"]).days == 30


class TestNoPictureArchive:
    """Pictures are not archived: the history of a heatmap is its 5-minute heat
    blocks, which any range is redrawn from. A session keeps one live picture,
    replaced on every upload, and the one it ended on."""

    def test_a_session_keeps_one_live_picture_however_long_it_runs(self):
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        for minute in range(1, 61):                      # an hour, past two half-hours
            _store(_meta(session_id=sid, captured_at=started + 60 * minute))
        rows = asyncio.run(_fetch("SELECT kind, captured_at FROM heatmap_artifacts"))
        assert [r["kind"] for r in rows] == ["live"]
        assert rows[0]["captured_at"].timestamp() == pytest.approx(started + 3600)

    def test_a_legacy_snapshot_upload_is_stored_as_live(self):
        """An analytics build from before this change still says `snapshot`."""
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        for minute in range(1, 4):
            _store(_meta(session_id=sid, kind="snapshot", captured_at=started + 60 * minute))
        kinds = [r["kind"] for r in asyncio.run(_fetch("SELECT kind FROM heatmap_artifacts"))]
        assert kinds == ["live"]

    def test_a_final_picture_sits_beside_the_live_one(self):
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        for minute in range(1, 9):
            _store(_meta(session_id=sid, captured_at=started + 60 * minute))
        _store(_meta(session_id=sid, kind="final", status="frozen",
                     captured_at=started + 8 * 60 + 30,
                     session_ended_at=started + 8 * 60 + 30, close_reason="operator"))
        kinds = sorted(r["kind"] for r in asyncio.run(_fetch("SELECT kind FROM heatmap_artifacts")))
        assert kinds == ["final", "live"]


class TestTheLiveKind:
    """`live` is one row per session, replaced in place -- see migration 047."""

    def test_repeated_live_updates_replace_one_row(self):
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        for minute in range(1, 6):
            _store(_meta(session_id=sid, kind="live", captured_at=started + 30 * minute))
        rows = asyncio.run(_fetch(
            "SELECT kind, captured_at FROM heatmap_artifacts WHERE kind = 'live'"))
        assert len(rows) == 1, "a live picture must not accumulate one row per update"
        assert rows[0]["captured_at"].timestamp() == pytest.approx(started + 30 * 5)


class TestWhatThePageReads:
    def test_one_row_per_configured_camera_with_its_newest_picture(self, cams):
        from backend.services import heatmaps
        sid = uuid.uuid4().hex
        started = _meta()["session_started_at"]
        _store(_meta(session_id=sid, captured_at=started + 60))
        _store(_meta(session_id=sid, captured_at=started + 120))
        _store(_meta(slug="yard-c3d4"))

        out = _session(lambda s: heatmaps.overview(s))
        by_slug = {c["slug"]: c for c in out}
        assert set(by_slug) == {"gate-a1b2", "yard-c3d4"}      # lobby has no heatmap
        gate = by_slug["gate-a1b2"]
        assert gate["latest_artifact"]["captured_at"].timestamp() == pytest.approx(started + 120)
        assert gate["session"]["id"] == str(uuid.UUID(sid))
        assert gate["runtime_enabled"] is True                  # no row means ON

    def test_an_expired_picture_is_not_served_even_before_the_sweep(self, cams):
        from backend.services import heatmaps
        _store(_meta())
        asyncio.run(_fetch("UPDATE heatmap_artifacts SET expires_at = now() - interval '1 day'"))
        [row] = asyncio.run(_fetch("SELECT id FROM heatmap_artifacts"))
        assert _session(lambda s: heatmaps.artifact(s, row["id"])) is None

    def test_sessions_come_back_newest_first_with_their_last_picture(self, cams):
        from backend.services import heatmaps
        started = _meta()["session_started_at"]
        first, second = uuid.uuid4().hex, uuid.uuid4().hex
        _store(_meta(session_id=first, session_started_at=started, captured_at=started + 60,
                     status="closed", session_ended_at=started + 120, close_reason="schedule ended"))
        _store(_meta(session_id=second, session_started_at=started + 3600,
                     captured_at=started + 3660))
        out = _session(lambda s: heatmaps.sessions_for(s, cams["gate-a1b2"]))
        assert [x["id"] for x in out] == [str(uuid.UUID(second)), str(uuid.UUID(first))]
        assert out[0]["latest_artifact"]["captured_at"].timestamp() == pytest.approx(started + 3660)


class TestTheRuntimeSwitch:
    """The switch as the API layer drives it — the push to analytics is
    best-effort and asserted in test_analytics_sync.py; here the DB half is
    what matters."""

    @pytest.fixture(autouse=True)
    def _no_push(self, monkeypatch):
        from backend.services import analytics_client

        async def pretend(slug, enabled, *, mode="resume"):
            return True

        monkeypatch.setattr(analytics_client, "push_heatmap", pretend)

    def _camera(self, slug="gate-a1b2"):
        from backend.models import Camera
        from sqlalchemy import select

        async def go(s):
            return (await s.execute(select(Camera).where(Camera.slug == slug))).scalars().first()

        return _session(go)

    def _set(self, enabled, mode="resume"):
        from backend.services import heatmaps
        camera = self._camera()
        return _session(lambda s: heatmaps.set_runtime(s, camera, enabled, mode=mode,
                                                       actor="tester"))

    def test_off_is_recorded_and_freezes_the_running_session(self):
        _store(_meta())
        self._set(False)
        [state] = asyncio.run(_fetch("SELECT * FROM heatmap_camera_state"))
        assert state["runtime_enabled"] is False and state["updated_by"] == "tester"
        [session] = asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))
        assert session["status"] == "frozen" and session["close_reason"] == "operator"

    def test_on_with_resume_reopens_the_frozen_session(self):
        _store(_meta())
        self._set(False)
        self._set(True, mode="resume")
        [session] = asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))
        assert session["status"] == "active" and session["ended_at"] is None
        [state] = asyncio.run(_fetch("SELECT * FROM heatmap_camera_state"))
        assert state["runtime_enabled"] is True

    def test_on_with_new_closes_it_instead(self):
        _store(_meta())
        self._set(False)
        self._set(True, mode="new")
        [session] = asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))
        assert session["status"] == "closed" and session["close_reason"] == "new session"

    def test_a_closed_session_is_never_reopened_by_the_switch(self):
        sid = uuid.uuid4().hex
        _store(_meta(session_id=sid, status="closed", close_reason="schedule ended",
                     session_ended_at=_meta()["session_started_at"] + 120))
        self._set(True, mode="resume")
        [session] = asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))
        assert session["status"] == "closed" and session["close_reason"] == "schedule ended"

    def test_the_switch_never_touches_the_cameras_ai_config(self):
        before = asyncio.run(_fetch(
            "SELECT analytics_config FROM cameras WHERE slug = 'gate-a1b2'"))[0]
        _store(_meta())
        self._set(False)
        after = asyncio.run(_fetch(
            "SELECT analytics_config FROM cameras WHERE slug = 'gate-a1b2'"))[0]
        assert after["analytics_config"] == before["analytics_config"]

    def test_the_reconcile_loop_is_told_about_every_configured_camera(self):
        from backend.services import heatmaps
        _store(_meta())
        self._set(False)
        wanted = _session(lambda s: heatmaps.desired_runtime(s))
        assert wanted == {"gate-a1b2": False, "yard-c3d4": True}    # lobby has no heatmap


class TestOrphanedSessions:
    """An analytics restart abandons the session it was holding: its matrix dies
    with the process and it mints a new id on the next frame. Nothing could
    close the row it left behind, so the page kept calling a dead accumulation
    "active" and the row could never be purged. Both halves of the fix here.
    """

    def _rows(self):
        return asyncio.run(_fetch(
            "SELECT s.id, c.slug, s.status, s.close_reason, s.started_at, s.ended_at"
            " FROM heatmap_sessions s JOIN cameras c ON c.id = s.camera_id"
            " ORDER BY s.started_at"))

    def _age(self, seconds, slug=None):
        """Backdate updated_at, the way a real silence would."""
        sql = ("UPDATE heatmap_sessions SET updated_at = now() - ($1 * interval '1 second')")
        if slug:
            sql += " WHERE camera_id = (SELECT id FROM cameras WHERE slug = $2)"
            return asyncio.run(_fetch(sql, float(seconds), slug))
        return asyncio.run(_fetch(sql, float(seconds)))

    # ── half one: a new session id closes what it supersedes ─────────────────
    def test_a_new_session_closes_the_one_the_restart_abandoned(self):
        started = _meta()["session_started_at"]
        old, new = uuid.uuid4().hex, uuid.uuid4().hex
        _store(_meta(session_id=old, session_started_at=started, captured_at=started + 60))
        _store(_meta(session_id=new, session_started_at=started + 3600,
                     captured_at=started + 3660))
        rows = self._rows()
        assert [r["status"] for r in rows] == ["closed", "active"]
        assert rows[0]["close_reason"] == "interrupted"
        # Closed at its last picture, not at "now" — it never gathered heat after that.
        assert rows[0]["ended_at"].timestamp() == pytest.approx(started + 60)

    def test_a_retry_of_the_running_session_closes_nothing(self):
        sid = uuid.uuid4().hex
        _store(_meta(session_id=sid))
        _store(_meta(session_id=sid), image=OTHER_JPEG)
        [row] = self._rows()
        assert row["status"] == "active" and row["close_reason"] is None

    def test_a_late_upload_of_a_stale_session_closes_itself_not_the_running_one(self):
        started = _meta()["session_started_at"]
        old, new = uuid.uuid4().hex, uuid.uuid4().hex
        _store(_meta(session_id=new, session_started_at=started + 3600,
                     captured_at=started + 3660))
        # The sink finally delivers the OLD session's snapshot, after the new one.
        _store(_meta(session_id=old, session_started_at=started, captured_at=started + 60))
        by_id = {str(r["id"]): r for r in self._rows()}
        assert by_id[str(uuid.UUID(old))]["status"] == "closed"
        assert by_id[str(uuid.UUID(new))]["status"] == "active"

    def test_a_frozen_session_is_closed_when_a_newer_one_appears(self):
        """The freeze was resumable only while analytics held its matrix."""
        started = _meta()["session_started_at"]
        frozen_id, fresh = uuid.uuid4().hex, uuid.uuid4().hex
        _store(_meta(session_id=frozen_id, kind="final", status="frozen",
                     session_ended_at=started + 90, captured_at=started + 90,
                     close_reason="operator"))
        _store(_meta(session_id=fresh, session_started_at=started + 3600,
                     captured_at=started + 3660))
        by_id = {str(r["id"]): r for r in self._rows()}
        old = by_id[str(uuid.UUID(frozen_id))]
        assert old["status"] == "closed"
        # It already had an ended_at from the freeze; that truth is kept.
        assert old["ended_at"].timestamp() == pytest.approx(started + 90)

    def test_one_cameras_restart_never_closes_anothers_session(self, cams):
        started = _meta()["session_started_at"]
        other = uuid.uuid4().hex
        _store(_meta(slug="yard-c3d4", session_id=other))
        _store(_meta(slug="gate-a1b2", session_id=uuid.uuid4().hex))
        _store(_meta(slug="gate-a1b2", session_id=uuid.uuid4().hex,
                     session_started_at=started + 3600, captured_at=started + 3660))
        by_slug = {}
        for r in self._rows():
            by_slug.setdefault(r["slug"], []).append(r["status"])
        assert by_slug["yard-c3d4"] == ["active"]
        assert sorted(by_slug["gate-a1b2"]) == ["active", "closed"]

    # ── half two: the sweep, for a camera that never comes back ───────────────
    def test_the_sweep_closes_an_active_session_gone_quiet(self):
        from backend.services import heatmaps
        started = _meta()["session_started_at"]
        _store(_meta(captured_at=started + 60))
        self._age(heatmaps.STALE_ACTIVE_S + 60)
        assert _session(lambda s: heatmaps.close_stale_sessions(s)) == 1
        [row] = self._rows()
        assert row["status"] == "closed" and row["close_reason"] == "interrupted"
        assert row["ended_at"].timestamp() == pytest.approx(started + 60)

    def test_the_sweep_leaves_a_session_that_is_still_uploading_alone(self):
        from backend.services import heatmaps
        _store(_meta())
        self._age(heatmaps.STALE_ACTIVE_S // 2)          # quiet, but not long enough
        assert _session(lambda s: heatmaps.close_stale_sessions(s)) == 0
        [row] = self._rows()
        assert row["status"] == "active"

    def test_the_sweep_never_closes_a_frozen_session(self):
        """An operator who switches a camera off overnight must still be offered
        Resume in the morning, and a frozen session is silent by design."""
        from backend.services import heatmaps
        started = _meta()["session_started_at"]
        _store(_meta(kind="final", status="frozen", close_reason="operator",
                     session_ended_at=started + 90, captured_at=started + 90))
        self._age(heatmaps.STALE_ACTIVE_S * 20)          # a whole night of silence
        assert _session(lambda s: heatmaps.close_stale_sessions(s)) == 0
        [row] = self._rows()
        assert row["status"] == "frozen" and row["close_reason"] == "operator"

    def test_the_retention_sweep_runs_the_close_as_part_of_its_pass(self):
        from backend.services import heatmaps
        _store(_meta())
        self._age(heatmaps.STALE_ACTIVE_S + 60)
        _session(lambda s: heatmaps.purge_expired(s))
        [row] = self._rows()
        assert row["status"] == "closed"

    def test_a_closed_orphan_is_purged_once_its_pictures_expire(self):
        from backend.services import heatmaps
        _store(_meta())
        self._age(heatmaps.STALE_ACTIVE_S + 60)
        _session(lambda s: heatmaps.close_stale_sessions(s))
        asyncio.run(_fetch("UPDATE heatmap_artifacts SET expires_at = now() - interval '1 day'"))
        _session(lambda s: heatmaps.purge_expired(s))
        assert self._rows() == []


class TestRetention:
    def test_the_sweep_deletes_what_is_past_its_deadline(self):
        from backend.services import heatmaps
        _store(_meta())
        _store(_meta(slug="yard-c3d4"))
        asyncio.run(_fetch("UPDATE heatmap_artifacts SET expires_at = now() - interval '1 day' "
                           "WHERE camera_id = (SELECT id FROM cameras WHERE slug='gate-a1b2')"))
        purged = _session(lambda s: heatmaps.purge_expired(s))
        assert purged == 1
        rows = asyncio.run(_fetch("SELECT camera_id FROM heatmap_artifacts"))
        assert len(rows) == 1

    def test_a_closed_session_goes_when_its_last_picture_does(self):
        from backend.services import heatmaps
        sid = uuid.uuid4().hex
        _store(_meta(session_id=sid, status="closed", close_reason="schedule ended",
                     session_ended_at=_meta()["session_started_at"] + 120))
        asyncio.run(_fetch("UPDATE heatmap_artifacts SET expires_at = now() - interval '1 day'"))
        _session(lambda s: heatmaps.purge_expired(s))
        assert asyncio.run(_fetch("SELECT * FROM heatmap_sessions")) == []

    def test_deleting_a_camera_takes_its_heatmaps_with_it(self, cams):
        _store(_meta(slug="yard-c3d4"))
        asyncio.run(_fetch("DELETE FROM cameras WHERE slug = 'yard-c3d4'"))
        assert asyncio.run(_fetch("SELECT * FROM heatmap_artifacts")) == []
        assert asyncio.run(_fetch("SELECT * FROM heatmap_sessions")) == []
        # Put it back for the rest of the module.
        asyncio.run(_fetch(
            "INSERT INTO cameras (id, slug, name, rtsp_url, stage, analytics_config) "
            "VALUES ($1, 'yard-c3d4', 'Yard', 'rtsp://10.0.0.1/yard', 'registered', $2::jsonb)",
            cams["yard-c3d4"], json.dumps(_config("Heatmap_overlay", "stray_parking"))))


class TestTheEngineGate:
    """One engine per camera, refused at the door (services/detection_engine.py).

    Both sides can render `Heatmap_overlay`, so a camera handed from one engine
    to the other would otherwise keep accepting pictures from the one that no
    longer owns it -- two interleaved accumulations of the same floor, each new
    session id convincing `_close_superseded` that the other had finished.
    """

    @staticmethod
    def _deepstream(monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "deepstream_sync_enabled", True)

    def test_the_engine_that_does_not_own_the_camera_is_refused(self, monkeypatch):
        from backend.services import heatmaps
        self._deepstream(monkeypatch)
        with pytest.raises(heatmaps.WrongEngine):
            _store(_meta())                 # unsigned = cpu, and cpu is not the engine

    def test_a_refused_upload_leaves_nothing_behind(self, monkeypatch):
        """WHY THE CHECK PRECEDES THE UPSERT. A session row written for a producer
        that may not own it is a session nothing owns, and it is exactly these
        rows that `_close_superseded` and the stale sweep then reason about."""
        from backend.services import heatmaps
        self._deepstream(monkeypatch)
        with pytest.raises(heatmaps.WrongEngine):
            _store(_meta())
        assert asyncio.run(_fetch("SELECT * FROM heatmap_sessions")) == []
        assert asyncio.run(_fetch("SELECT * FROM heatmap_artifacts")) == []

    def test_the_owning_engine_is_stored(self, monkeypatch):
        self._deepstream(monkeypatch)
        _store(_meta(source="deepstream"))
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))) == 1
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_artifacts"))) == 1

    def test_a_pipeline_that_forgets_to_name_itself_is_shut_out(self, monkeypatch):
        """The default fails CLOSED for everyone but the one caller it exists for."""
        from backend.services import heatmaps
        self._deepstream(monkeypatch)
        with pytest.raises(heatmaps.WrongEngine):
            _store(_meta(source=""))

    def test_an_unsigned_message_is_the_cpu_engine(self):
        """The CPU sink has never sent `source` and predates this gate: its
        messages must still land. This is the compatibility the default is for."""
        _store(_meta())                     # autouse fixture: cpu is the engine
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))) == 1

    def test_the_cpu_engine_is_refused_once_the_pipeline_owns_the_camera(self, monkeypatch):
        """The handover direction that matters: analytics still running after the
        operator switched the engine must not keep writing the same camera."""
        from backend.services import heatmaps
        _store(_meta())                     # cpu owns it, this lands
        self._deepstream(monkeypatch)
        with pytest.raises(heatmaps.WrongEngine):
            _store(_meta(source="cpu"))
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_sessions"))) == 1


class TestOneBackgroundPerCamera:
    """The frame every heatmap is drawn over is ONE row per camera, overwritten.
    Nothing about it goes to the object store, and it never piles up."""

    @pytest.fixture
    def store_chunk(self, monkeypatch):
        import io
        import numpy as np
        from backend.services import heatmap_chunks, object_store
        put = []
        monkeypatch.setattr(object_store, "is_configured", lambda: True)
        monkeypatch.setattr(object_store, "put",
                            lambda key, data, content_type="": put.append(key) or key)
        buf = io.BytesIO()
        np.savez_compressed(buf, heat=np.zeros((90, 160), np.float32))
        heat = buf.getvalue()
        t0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc).timestamp()

        def go(minute, background):
            meta = {"camera": "gate-a1b2", "period_start": t0 + 60 * minute,
                    "period_end": t0 + 60 * (minute + 5), "points": 1}
            return _session(lambda s: heatmap_chunks.store(s, meta, heat, background))
        go.put, go.t0 = put, t0
        return go

    def test_each_frame_replaces_the_last(self, store_chunk):
        store_chunk(0, JPEG)
        store_chunk(5, OTHER_JPEG)
        rows = asyncio.run(_fetch("SELECT image, captured_at FROM heatmap_backgrounds"))
        assert len(rows) == 1 and bytes(rows[0]["image"]) == OTHER_JPEG
        assert rows[0]["captured_at"].timestamp() == pytest.approx(store_chunk.t0 + 600)
        assert all(k.endswith(".npz") for k in store_chunk.put), "no frame in the object store"
        assert asyncio.run(_fetch(
            "SELECT id FROM heatmap_chunks WHERE background_key IS NOT NULL")) == []

    def test_a_late_retry_never_puts_an_older_frame_back(self, store_chunk):
        store_chunk(5, OTHER_JPEG)
        store_chunk(0, JPEG)                               # an older block, retried late
        [row] = asyncio.run(_fetch("SELECT image FROM heatmap_backgrounds"))
        assert bytes(row["image"]) == OTHER_JPEG

    def test_a_block_without_a_frame_keeps_the_one_there(self, store_chunk):
        store_chunk(0, JPEG)
        store_chunk(5, None)
        [row] = asyncio.run(_fetch("SELECT image FROM heatmap_backgrounds"))
        assert bytes(row["image"]) == JPEG

    def test_a_frame_nobody_refreshes_is_purged_with_retention(self, store_chunk, monkeypatch):
        from backend.services import heatmap_chunks, object_store
        monkeypatch.setattr(object_store, "delete_many", lambda keys: len(keys))
        store_chunk(0, JPEG)
        _session(heatmap_chunks.purge_expired)
        assert len(asyncio.run(_fetch("SELECT * FROM heatmap_backgrounds"))) == 1, \
            "a frame inside its retention stays"
        asyncio.run(_fetch("UPDATE heatmap_backgrounds SET expires_at = now() - interval '1 minute'"))
        _session(heatmap_chunks.purge_expired)
        assert asyncio.run(_fetch("SELECT * FROM heatmap_backgrounds")) == []



@pytest.fixture
def blocks(monkeypatch):
    """Store 5-minute heat blocks for gate-a1b2, `minute` minutes after t0,
    with the object store stubbed (objects named, never written)."""
    import io
    import numpy as np
    from backend.services import heatmap_chunks, object_store
    deleted = []
    monkeypatch.setattr(object_store, "is_configured", lambda: True)
    monkeypatch.setattr(object_store, "put", lambda key, data, content_type="": key)
    monkeypatch.setattr(object_store, "delete_many",
                        lambda keys: deleted.extend(keys) or len(keys))
    buf = io.BytesIO()
    np.savez_compressed(buf, heat=np.zeros((90, 160), np.float32))
    heat = buf.getvalue()
    t0 = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)

    def go(*minutes):
        for minute in minutes:
            start = t0 + timedelta(minutes=minute)
            meta = {"camera": "gate-a1b2", "period_start": start.timestamp(),
                    "period_end": (start + timedelta(minutes=5)).timestamp(), "points": 1}
            _session(lambda s: heatmap_chunks.store(s, meta, heat, None))
    go.t0, go.heat, go.deleted = t0, heat, deleted
    return go


def _set_caps(cams, **params):
    asyncio.run(_fetch(
        "UPDATE cameras SET analytics_config = $1::jsonb WHERE id = $2",
        json.dumps({"regions": {}, "activities": [
            {"type": "Heatmap_overlay", "regions": [], "params": params}]}),
        cams["gate-a1b2"]))


def _enforce(cams, now):
    from backend.services import heatmap_chunks

    async def go(s):
        cfg = (await s.execute(text_("SELECT analytics_config FROM cameras WHERE id = :i"),
                               {"i": cams["gate-a1b2"]})).scalar()
        return await heatmap_chunks.enforce_caps(s, cams["gate-a1b2"], cfg, now=now)
    from sqlalchemy import text as text_
    return _session(go)


def _starts():
    return [r["period_start"] for r in asyncio.run(_fetch(
        "SELECT period_start FROM heatmap_chunks ORDER BY period_start"))]


class TestStorageCaps:
    """The operator's caps on the Heatmap overlay activity: days, then MB."""

    @pytest.fixture(autouse=True)
    def reset_caps(self, cams):
        yield
        _set_caps(cams)

    def test_each_block_records_its_size(self, blocks):
        blocks(0)
        [row] = asyncio.run(_fetch("SELECT size_bytes, expires_at, period_end FROM heatmap_chunks"))
        assert row["size_bytes"] == len(blocks.heat)
        assert (row["expires_at"] - row["period_end"]).days == 30     # the default cap

    def test_blocks_older_than_the_days_cap_go_oldest_first(self, cams, blocks):
        _set_caps(cams, retention_days=2)
        blocks(0, 60 * 24, 60 * 48)                                  # day 0, 1, 2
        n = _enforce(cams, now=blocks.t0 + timedelta(days=3, minutes=1))
        assert n == 1 and [s - blocks.t0 for s in _starts()] == \
            [timedelta(days=1), timedelta(days=2)]
        assert len(blocks.deleted) == 1 and blocks.deleted[0].endswith(".npz")

    def test_raising_the_days_cap_keeps_what_the_old_cap_would_have_deleted(self, cams, blocks):
        _set_caps(cams, retention_days=1)
        blocks(0)                                                    # stamped with the 1-day cap
        _set_caps(cams, retention_days=10)
        assert _enforce(cams, now=blocks.t0 + timedelta(days=5)) == 0
        assert len(_starts()) == 1

    def test_over_the_storage_cap_the_oldest_go_until_it_fits(self, cams, blocks):
        _set_caps(cams, storage_mb=10)
        blocks(0, 5, 10, 15, 20)
        asyncio.run(_fetch("UPDATE heatmap_chunks SET size_bytes = $1", 3 * 1024 * 1024))
        assert _enforce(cams, now=blocks.t0 + timedelta(hours=1)) == 2   # 15 MB -> 9 MB
        assert [s - blocks.t0 for s in _starts()] == \
            [timedelta(minutes=m) for m in (10, 15, 20)]

    def test_under_both_caps_nothing_is_deleted(self, cams, blocks):
        blocks(0, 5, 10)
        assert _enforce(cams, now=blocks.t0 + timedelta(hours=1)) == 0
        assert len(_starts()) == 3

    def test_blocks_from_before_sizes_were_recorded_are_measured(self, cams, blocks, monkeypatch):
        from backend.services import object_store
        monkeypatch.setattr(object_store, "size", lambda key: 6 * 1024 * 1024)
        _set_caps(cams, storage_mb=10)
        blocks(0, 5)
        asyncio.run(_fetch("UPDATE heatmap_chunks SET size_bytes = NULL"))
        assert _enforce(cams, now=blocks.t0 + timedelta(hours=1)) == 1   # 12 MB -> 6 MB
        assert asyncio.run(_fetch(
            "SELECT size_bytes FROM heatmap_chunks"))[0]["size_bytes"] == 6 * 1024 * 1024


class TestAvailability:
    def test_lists_the_local_days_with_heat_and_the_caps(self, cams, blocks):
        from backend.services import heatmap_chunks
        _set_caps(cams, retention_days=14, storage_mb=200)
        blocks(0, 60 * 48)                                           # 20 Sep and 22 Sep

        async def go(s):
            from backend.models import Camera
            return await heatmap_chunks.availability(s, await s.get(Camera, cams["gate-a1b2"]))
        out = _session(go)
        local = lambda d: d.astimezone().date().isoformat()          # noqa: E731
        assert out["days"] == [local(blocks.t0), local(blocks.t0 + timedelta(days=2))]
        assert datetime.fromisoformat(out["first_at"]) == blocks.t0
        assert (out["blocks"], out["used_bytes"]) == (2, 2 * len(blocks.heat))
        assert (out["retention_days"], out["storage_mb"]) == (14, 200)
        _set_caps(cams)
