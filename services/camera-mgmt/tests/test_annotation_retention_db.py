"""Expiry — the hole this feature would otherwise have made large.

    ANNOTATIONS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_annotation_retention_db.py

Annotations were immortal before this: the NVR's retention.py sweeps segments,
clips and the HLS cache and never touched the annotations directory. A still
derived from footage outliving its source is the exposure; a dataset-attached
one is the exemption, because it has a named owner and a deletable parent.

THE SWEEP LIVES HERE, NOT IN THE NVR. `expires_at` is a column in this
database; the NVR has only its own SQLite index and could not read the deadline
it would be enforcing.
"""
from __future__ import annotations

import asyncio
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
EPOCH = 1790000000.0


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


def _adopt():
    from backend.services import annotation_store

    async def go(s):
        return await annotation_store.adopt_orphans(s)
    return _session(go)


def _sidecar(aid=None, class_key="forklift", camera=CAMERA, drop_class=False):
    aid = aid or uuid.uuid4().hex
    meta = {
        "id": aid, "camera": camera, "class_key": class_key,
        "timestamp": "2026-09-25T10:00:00+00:00", "timestamp_epoch": EPOCH,
        "bbox": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0},
        "frame": {"width": 1920, "height": 1080},
        "image_path": f"/data/nvr/annotations/{camera}/{int(EPOCH)}_{aid}.jpg",
    }
    if drop_class:
        del meta["class_key"]
    return meta


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
    """Records what the sweep asked the NVR to delete, and can refuse."""
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


def _purge():
    from backend.services import annotation_store

    async def go(s):
        return await annotation_store.purge_expired(s)
    return _session(go)


def _seed(expires_at, dataset_id=None):
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO annotations (id, camera_slug, class_key, captured_at,"
        " bbox_x, bbox_y, bbox_w, bbox_h, frame_width, frame_height,"
        " image_path, dataset_id, expires_at)"
        " VALUES ($1,$2,'forklift', now(),10,20,30,40,1920,1080,'/a.jpg',$3,$4)",
        aid, CAMERA, dataset_id, expires_at))
    return aid


def _a_dataset():
    rows = _run(_query(
        "INSERT INTO datasets (created_at, updated_at, name, camera_slug,"
        " window_start, window_end, target_frames)"
        " VALUES (now(), now(), 'd', $1, now() - interval '1 hour', now(), 10)"
        " RETURNING id", CAMERA))
    return rows[0]["id"]


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _ahead(**kw):
    return datetime.now(timezone.utc) + timedelta(**kw)


def test_an_expired_annotation_is_swept(migrated, nvr):
    aid = _seed(expires_at=_ago(minutes=1))
    assert _purge() == 1
    assert nvr["deleted"] == [aid.hex]
    assert _run(_query("SELECT id FROM annotations")) == []


def test_an_unexpired_annotation_survives(migrated, nvr):
    _seed(expires_at=_ahead(days=7))
    assert _purge() == 0
    assert len(_run(_query("SELECT id FROM annotations"))) == 1
    assert nvr["deleted"] == []


def test_a_dataset_attached_annotation_never_expires(migrated, nvr):
    """Null expiry means it has a named owner and a deletable parent, so its
    removal is the dataset's business rather than this sweep's."""
    did = _a_dataset()
    _seed(expires_at=None, dataset_id=did)
    assert _purge() == 0
    assert len(_run(_query("SELECT id FROM annotations"))) == 1


def test_the_file_goes_before_the_row(migrated, nvr):
    """If the row went first, the surviving file would be handed straight back
    to the adoption pass, which would re-index what the sweep just removed."""
    aid = _seed(expires_at=_ago(minutes=1))
    nvr["ok"] = False
    assert _purge() == 0
    assert len(_run(_query("SELECT id FROM annotations"))) == 1, \
        "the row must survive a file the NVR could not delete"
    nvr["ok"] = True
    assert _purge() == 1
    assert nvr["deleted"] == [aid.hex]


def test_one_stuck_file_does_not_block_the_rest(migrated, nvr, monkeypatch):
    from backend.services import annotation_store
    stuck = _seed(expires_at=_ago(minutes=1))
    fine = _seed(expires_at=_ago(minutes=1))

    async def _selective(annotation_id):
        return annotation_id != stuck.hex
    monkeypatch.setattr(annotation_store.nvr_client, "delete_annotation",
                        _selective)

    assert _purge() == 1
    remaining = [str(r["id"]) for r in _run(_query("SELECT id FROM annotations"))]
    assert remaining == [str(stuck)]
    assert str(fine) not in remaining


def test_an_empty_pile_is_a_no_op(migrated, nvr):
    assert _purge() == 0


# ── The posture report counts what this appliance derived from footage ──────


def _posture_stills():
    from backend.extensions.compliance import posture

    async def go(s):
        return await posture.derived_stills(s)
    return _session(go)


def test_the_posture_report_counts_annotations(migrated, nvr):
    _seed(expires_at=_ahead(days=7))
    _seed(expires_at=_ahead(days=7))
    assert _posture_stills()["annotations"] == 2


def test_it_separates_the_ones_with_no_expiry(migrated, nvr):
    """A still with no expiry is legitimate when it belongs to a dataset. The
    report states the number; it does not decide whether that is acceptable."""
    did = _a_dataset()
    _seed(expires_at=_ahead(days=7))
    _seed(expires_at=None, dataset_id=did)
    out = _posture_stills()
    assert out["annotations"] == 2
    assert out["annotations_without_expiry"] == 1


def test_it_counts_dataset_frames_too(migrated, nvr):
    did = _a_dataset()
    _run(_execute(
        "UPDATE datasets SET frames_collected = 200 WHERE id = $1", did))
    assert _posture_stills()["dataset_frames"] == 200


def test_zero_is_reported_not_omitted(migrated, nvr):
    assert _posture_stills() == {"annotations": 0,
                                 "annotations_without_expiry": 0,
                                 "dataset_frames": 0}
