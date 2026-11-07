"""What 047 does to a real database.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE, with the same two rails as
test_alert_rules_db.py: DATABASE_URL is overridden for alembic (inside the api
container it names the LIVE database), and the scratch database must start
empty.

    ANNOTATIONS_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_annotations_db.py

WHAT IT PROVES, none of which a unit test can — the conftest `db` is a
StubSession whose reads come back empty, so every assertion below about what
the DATABASE enforces has to live here:

  * a class cannot be deleted while an annotation names it — the FK is what
    makes "retire, do not delete" a rule rather than a convention
  * retiring a class leaves its annotations resolvable
  * deleting a dataset NULLs annotations.dataset_id and keeps the annotation
  * a zero-area box is refused by the CHECK, not only by pydantic
  * downgrade drops both tables cleanly, so 047 is reversible
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

DB_URL = os.environ.get("ANNOTATIONS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set ANNOTATIONS_TEST_DB to a scratch database URL")

if DB_URL:                                  # keep collection cheap when skipping
    pytest.importorskip(
        "alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                               # noqa: E402
    from alembic.config import Config                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace(
        "postgresql://", "postgresql+asyncpg://")


def _asyncpg_dsn() -> str:
    return _alembic_url().replace("+asyncpg", "")


def _cfg() -> "Config":
    # RAIL 1. env.py prefers DATABASE_URL over anything passed here, and in the
    # api container that variable is the live database.
    os.environ["DATABASE_URL"] = _alembic_url()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _alembic_url())
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _query(sql: str, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _execute(sql: str, *args):
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def migrated():
    # RAIL 2: the scratch database must be empty.
    tables = _run(_query(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema = 'public'"))
    if tables[0]["n"]:
        pytest.skip("ANNOTATIONS_TEST_DB is not empty; refusing to migrate it")
    command.upgrade(_cfg(), "047")
    yield


@pytest.fixture(autouse=True)
def clean(migrated):
    yield
    _run(_execute("DELETE FROM annotations"))
    _run(_execute("DELETE FROM label_classes"))
    _run(_execute("DELETE FROM datasets"))


def _seed_class(key: str = "forklift", active: bool = True) -> None:
    _run(_execute(
        "INSERT INTO label_classes (key, label, color, sort_order, active) "
        "VALUES ($1, $2, '#ff8800', 0, $3)", key, key.title(), active))


def _seed_annotation(class_key: str = "forklift",
                     dataset_id: int | None = None,
                     bbox_w: float = 30.0, bbox_h: float = 40.0) -> uuid.UUID:
    """Raw SQL, so the DATABASE's constraints are what answer — not pydantic's."""
    aid = uuid.uuid4()
    _run(_execute(
        "INSERT INTO annotations (id, camera_slug, class_key, captured_at,"
        " bbox_x, bbox_y, bbox_w, bbox_h, frame_width, frame_height,"
        " image_path, dataset_id)"
        " VALUES ($1,'gate-cam',$2, now(), 10, 20, $3, $4, 1920, 1080,"
        "         '/data/nvr/annotations/gate-cam/1_x.jpg', $5)",
        aid, class_key, bbox_w, bbox_h, dataset_id))
    return aid


def _a_dataset() -> int:
    rows = _run(_query(
        "INSERT INTO datasets (created_at, updated_at, name, camera_slug,"
        " window_start, window_end, target_frames)"
        " VALUES (now(), now(), 'd', 'gate-cam',"
        "         now() - interval '1 hour', now(), 10)"
        " RETURNING id"))
    return rows[0]["id"]


def test_a_class_in_use_cannot_be_deleted(migrated):
    """The FK is what turns 'retire, do not delete' into a rule. Without it an
    administrator tidying the taxonomy would orphan every annotation."""
    _seed_class("forklift")
    _seed_annotation("forklift")
    with pytest.raises(Exception) as exc:
        _run(_execute("DELETE FROM label_classes WHERE key = 'forklift'"))
    assert "foreign key" in str(exc.value).lower()


def test_an_unused_class_can_be_deleted(migrated):
    _seed_class("unused")
    _run(_execute("DELETE FROM label_classes WHERE key = 'unused'"))
    assert _run(_query("SELECT key FROM label_classes")) == []


def test_retiring_a_class_keeps_its_annotations_resolvable(migrated):
    _seed_class("pallet")
    aid = _seed_annotation("pallet")
    _run(_execute("UPDATE label_classes SET active = false WHERE key = 'pallet'"))
    rows = _run(_query(
        "SELECT a.class_key, c.active FROM annotations a"
        " JOIN label_classes c ON c.key = a.class_key WHERE a.id = $1", aid))
    assert rows[0]["class_key"] == "pallet"
    assert rows[0]["active"] is False


def test_deleting_a_dataset_keeps_the_annotation(migrated):
    """The dataset is working material and deletable on purpose (045). The
    annotation is the labelled fact and outlives its container."""
    _seed_class("crate")
    did = _a_dataset()
    aid = _seed_annotation("crate", dataset_id=did)
    _run(_execute("DELETE FROM datasets WHERE id = $1", did))
    rows = _run(_query(
        "SELECT dataset_id FROM annotations WHERE id = $1", aid))
    assert len(rows) == 1 and rows[0]["dataset_id"] is None


def test_a_zero_area_box_is_refused_by_the_database(migrated):
    _seed_class("forklift")
    with pytest.raises(Exception) as exc:
        _seed_annotation("forklift", bbox_w=0.0)
    assert "ck_annotations_bbox" in str(exc.value)


def test_expires_at_is_nullable_for_dataset_attached_rows(migrated):
    _seed_class("crate")
    did = _a_dataset()
    aid = _seed_annotation("crate", dataset_id=did)
    rows = _run(_query("SELECT expires_at FROM annotations WHERE id = $1", aid))
    assert rows[0]["expires_at"] is None


def test_048_adds_kept_reason_and_is_reversible(migrated):
    """The column 048 adds, and that it comes off cleanly."""
    command.upgrade(_cfg(), "048")
    cols = _run(_query(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'annotations' AND column_name = 'kept_reason'"))
    assert len(cols) == 1 and cols[0]["is_nullable"] == "YES"
    command.downgrade(_cfg(), "047")
    cols = _run(_query(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'annotations' AND column_name = 'kept_reason'"))
    assert cols == []


def test_downgrade_drops_both_tables(migrated):
    command.downgrade(_cfg(), "046")
    tables = _run(_query(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'public'"
        "   AND table_name IN ('annotations', 'label_classes')"))
    assert tables == []
    command.upgrade(_cfg(), "047")          # leave the module as we found it
