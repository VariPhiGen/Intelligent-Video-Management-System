"""Files whose index row never landed, and history that predates the taxonomy.

    ANNOTATIONS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_annotation_adoption_db.py

Adoption is what makes file-before-row safe. If it is not idempotent, a retried
pass produces duplicate training data — so that is the property pinned hardest
here.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
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
def nvr_holds(migrated, monkeypatch):
    """Stand in for the NVR's sidecar listing."""
    from backend.services import annotation_store

    sidecars: list = []

    async def _list(camera=None, since=None):
        return list(sidecars)
    monkeypatch.setattr(annotation_store.nvr_client, "list_annotations", _list)
    yield sidecars
    _run(_execute("DELETE FROM annotations"))
    _run(_execute("DELETE FROM label_classes"))


def _a_class(key="forklift"):
    _run(_execute(
        "INSERT INTO label_classes (key, label, color, sort_order, active)"
        " VALUES ($1, $2, '#ff8800', 0, true)", key, key.title()))


def test_a_file_with_no_row_is_adopted(migrated, nvr_holds):
    _a_class()
    meta = _sidecar()
    nvr_holds.append(meta)
    assert _adopt() == 1
    rows = _run(_query("SELECT * FROM annotations WHERE id = $1",
                       uuid.UUID(meta["id"])))
    assert len(rows) == 1
    assert rows[0]["class_key"] == "forklift"
    assert rows[0]["camera_slug"] == CAMERA
    assert rows[0]["bbox_w"] == 30.0
    assert rows[0]["expires_at"] is not None


def test_adoption_is_idempotent(migrated, nvr_holds):
    """A retried pass must not duplicate training data."""
    _a_class()
    nvr_holds.append(_sidecar())
    assert _adopt() == 1
    assert _adopt() == 0
    assert len(_run(_query("SELECT id FROM annotations"))) == 1


def test_an_already_indexed_file_is_left_alone(migrated, nvr_holds):
    _a_class()
    meta = _sidecar()
    nvr_holds.append(meta)
    _adopt()
    _run(_execute("UPDATE annotations SET created_by = 'human' WHERE id = $1",
                  uuid.UUID(meta["id"])))
    assert _adopt() == 0
    rows = _run(_query("SELECT created_by FROM annotations WHERE id = $1",
                       uuid.UUID(meta["id"])))
    assert rows[0]["created_by"] == "human", "adoption overwrote a live row"


def test_history_without_a_class_lands_on_an_inert_holder(migrated, nvr_holds):
    """Annotations drawn before the taxonomy existed have no class. Discarding
    them loses frames nobody agreed to lose; guessing a class is worse."""
    from backend.services import annotation_store
    nvr_holds.append(_sidecar(drop_class=True))
    assert _adopt() == 1
    rows = _run(_query("SELECT class_key FROM annotations"))
    assert rows[0]["class_key"] == annotation_store.UNCLASSIFIED_KEY
    cls = _run(_query("SELECT active FROM label_classes WHERE key = $1",
                      annotation_store.UNCLASSIFIED_KEY))
    assert cls[0]["active"] is False, "the holder must not be selectable"


def test_the_holder_is_created_once_not_per_file(migrated, nvr_holds):
    nvr_holds.extend([_sidecar(drop_class=True), _sidecar(drop_class=True)])
    assert _adopt() == 2
    rows = _run(_query("SELECT key FROM label_classes"))
    assert len(rows) == 1


def test_a_class_that_no_longer_exists_parks_rather_than_loses_the_frame(
        migrated, nvr_holds):
    from backend.services import annotation_store
    nvr_holds.append(_sidecar(class_key="deleted_somehow"))
    assert _adopt() == 1
    rows = _run(_query("SELECT class_key FROM annotations"))
    assert rows[0]["class_key"] == annotation_store.UNCLASSIFIED_KEY


def test_an_unreachable_nvr_adopts_nothing_and_does_not_raise(
        migrated, monkeypatch):
    """None and [] mean different things: 'could not look' must not be read as
    'nothing to adopt'."""
    from backend.services import annotation_store

    async def _down(camera=None, since=None):
        return None
    monkeypatch.setattr(annotation_store.nvr_client, "list_annotations", _down)
    assert _adopt() == 0


def test_a_sidecar_without_an_id_is_skipped_not_fatal(migrated, nvr_holds):
    _a_class()
    broken = _sidecar()
    del broken["id"]
    good = _sidecar()
    nvr_holds.extend([broken, good])
    assert _adopt() == 1
    rows = _run(_query("SELECT id FROM annotations"))
    assert [str(r["id"]) for r in rows] == [str(uuid.UUID(good["id"]))]
