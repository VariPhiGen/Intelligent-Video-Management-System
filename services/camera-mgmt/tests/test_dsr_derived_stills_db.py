"""Erasure has to reach the stills, not only the footage.

    ANNOTATIONS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_dsr_derived_stills_db.py

Before this, `erase_refs` fanned across NVR range-delete, Smart Search crops and
timeline presence — and left annotation JPEGs and Data Collection frames on
disk while the register recorded the request as FULFILLED. A fulfilment that
misses a copy is not a partial erasure, it is a false claim of one: the same
sentence the sub-track and search-index fixes are already justified by.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DB_URL = os.environ.get("ANNOTATIONS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set ANNOTATIONS_TEST_DB to a scratch database URL")

if DB_URL:
    pytest.importorskip("alembic", reason="alembic lives in the api image")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                               # noqa: E402
    from alembic.config import Config                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CAMERA = "gate-cam"


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace(
        "postgresql://", "postgresql+asyncpg://")


def _asyncpg_dsn() -> str:
    return _alembic_url().replace("+asyncpg", "")


def _cfg() -> "Config":
    os.environ["DATABASE_URL"] = _alembic_url()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _alembic_url())
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _query(sql, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(sql, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.execute(sql, *args)
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
def migrated():
    async def _count():
        conn = await asyncpg.connect(_asyncpg_dsn())
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'")
        finally:
            await conn.close()
    if _run(_count()):
        pytest.skip("ANNOTATIONS_TEST_DB is not empty; refusing to migrate it")
    command.upgrade(_cfg(), "048")
    yield


@pytest.fixture
def nvr(migrated, monkeypatch):
    from backend.services import annotation_store

    state = {"deleted": [], "ok": True}

    async def _delete(annotation_id):
        if not state["ok"]:
            return False
        state["deleted"].append(annotation_id)
        return True

    monkeypatch.setattr(annotation_store.nvr_client, "delete_annotation",
                        _delete)
    _run(_execute(
        "INSERT INTO label_classes (key, label, color, sort_order, active)"
        " VALUES ('forklift','Forklift','#ff8800',0,true)"))
    yield state
    _run(_execute("DELETE FROM annotations"))
    _run(_execute("DELETE FROM label_classes"))
    _run(_execute("DELETE FROM datasets"))


def _seed_annotation(captured, camera=CAMERA):
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO annotations (id, camera_slug, class_key, captured_at,"
        " bbox_x, bbox_y, bbox_w, bbox_h, frame_width, frame_height, image_path)"
        " VALUES ($1,$2,'forklift',$3,10,20,30,40,1920,1080,'/a.jpg')",
        aid, camera, captured))
    return aid


def _erase_annotations(frm, to, slug=CAMERA):
    from backend.services import annotation_store

    async def go(s):
        return await annotation_store.erase_range(s, slug, frm, to)
    return _session(go)


def _erase_frames(frm, to, slug=CAMERA):
    from backend.services import dataset_frames

    async def go(s):
        return await dataset_frames.erase_range(s, slug, frm, to)
    return _session(go)


def _seed_dataset(tmp_path, stamps, camera=CAMERA):
    """A dataset whose manifest names one file per stamp, all on disk."""
    frames_dir = tmp_path / f"ds_{uuid.uuid4().hex[:6]}"
    frames_dir.mkdir()
    manifest = []
    for i, stamp in enumerate(stamps, start=1):
        name = f"frame_{i:05d}_{int(stamp.timestamp() * 1000)}.jpg"
        (frames_dir / name).write_bytes(b"x" * 10)
        manifest.append({"filename": name, "timestamp": stamp.isoformat(),
                         "sha256": "a" * 64, "bytes": 10})
    rows = _run(_query(
        "INSERT INTO datasets (created_at, updated_at, name, camera_slug,"
        " window_start, window_end, target_frames, frames, frames_dir,"
        " frames_collected, bytes_total)"
        " VALUES (now(), now(), 'd', $1, now() - interval '10 days', now(),"
        "         $2, $3::jsonb, $4, $2, $5) RETURNING id",
        camera, len(stamps), json.dumps(manifest), str(frames_dir),
        10 * len(stamps)))
    return rows[0]["id"], frames_dir


def _now():
    return datetime.now(timezone.utc)


# ── Annotations ─────────────────────────────────────────────────────────────


def test_annotations_inside_the_window_are_erased(migrated, nvr):
    now = _now()
    inside = _seed_annotation(now)
    outside = _seed_annotation(now - timedelta(days=5))
    assert _erase_annotations(now - timedelta(hours=1),
                              now + timedelta(hours=1)) == 1
    remaining = [str(r["id"]) for r in _run(_query("SELECT id FROM annotations"))]
    assert remaining == [str(outside)]
    assert nvr["deleted"] == [inside.hex]


def test_another_cameras_annotations_are_untouched(migrated, nvr):
    now = _now()
    _seed_annotation(now, camera="yard-cam")
    assert _erase_annotations(now - timedelta(hours=1),
                              now + timedelta(hours=1)) == 0
    assert len(_run(_query("SELECT id FROM annotations"))) == 1


def test_a_file_the_nvr_cannot_delete_returns_none(migrated, nvr):
    """None is what stops fulfil_dsr marking the request fulfilled. Returning a
    count here would close the register on an erasure that did not happen."""
    now = _now()
    _seed_annotation(now)
    nvr["ok"] = False
    assert _erase_annotations(now - timedelta(hours=1),
                              now + timedelta(hours=1)) is None
    assert len(_run(_query("SELECT id FROM annotations"))) == 1, \
        "the row must survive so a retry finds the file again"


def test_nothing_in_the_window_is_a_clean_zero_not_a_failure(migrated, nvr):
    now = _now()
    assert _erase_annotations(now - timedelta(hours=1),
                              now + timedelta(hours=1)) == 0


# ── Dataset frames ──────────────────────────────────────────────────────────


def test_dataset_frames_inside_the_window_are_unlinked(migrated, nvr, tmp_path):
    now = _now()
    did, frames_dir = _seed_dataset(tmp_path, [now - timedelta(days=5), now])
    assert _erase_frames(now - timedelta(hours=1), now + timedelta(hours=1)) == 1
    survivors = sorted(p.name for p in frames_dir.iterdir())
    assert len(survivors) == 1
    rows = _run(_query("SELECT frames, frames_collected, bytes_total"
                       " FROM datasets WHERE id = $1", did))
    manifest = json.loads(rows[0]["frames"]) if isinstance(
        rows[0]["frames"], str) else rows[0]["frames"]
    assert [f["filename"] for f in manifest] == survivors
    assert rows[0]["frames_collected"] == 1
    assert rows[0]["bytes_total"] == 10


def test_a_dataset_emptied_by_erasure_is_kept(migrated, nvr, tmp_path):
    """The recipe is still valid and re-collectable. Deleting an operator's
    dataset row during somebody else's erasure is a surprise nobody asked for."""
    now = _now()
    did, frames_dir = _seed_dataset(tmp_path, [now])
    assert _erase_frames(now - timedelta(hours=1), now + timedelta(hours=1)) == 1
    rows = _run(_query("SELECT frames, frames_collected FROM datasets"
                       " WHERE id = $1", did))
    assert len(rows) == 1
    manifest = json.loads(rows[0]["frames"]) if isinstance(
        rows[0]["frames"], str) else rows[0]["frames"]
    assert manifest == []
    assert rows[0]["frames_collected"] == 0
    assert list(frames_dir.iterdir()) == []


def test_frames_of_another_camera_are_untouched(migrated, nvr, tmp_path):
    now = _now()
    _, frames_dir = _seed_dataset(tmp_path, [now], camera="yard-cam")
    assert _erase_frames(now - timedelta(hours=1),
                         now + timedelta(hours=1)) == 0
    assert len(list(frames_dir.iterdir())) == 1


def test_an_undateable_manifest_entry_is_treated_as_inside(migrated, nvr,
                                                           tmp_path):
    """Over-erasing working material is recoverable by re-collecting.
    Under-erasing is a false fulfilment, which is not."""
    now = _now()
    did, frames_dir = _seed_dataset(tmp_path, [now])
    _run(_execute(
        "UPDATE datasets SET frames = jsonb_set(frames, '{0,timestamp}',"
        " '\"not-a-date\"') WHERE id = $1", did))
    assert _erase_frames(now - timedelta(hours=1),
                         now + timedelta(hours=1)) == 1
    assert list(frames_dir.iterdir()) == []


def test_a_dataset_with_no_frames_yet_is_skipped(migrated, nvr):
    now = _now()
    _run(_execute(
        "INSERT INTO datasets (created_at, updated_at, name, camera_slug,"
        " window_start, window_end, target_frames)"
        " VALUES (now(), now(), 'draft', $1,"
        "         now() - interval '1 hour', now(), 10)", CAMERA))
    assert _erase_frames(now - timedelta(hours=1),
                         now + timedelta(hours=1)) == 0
