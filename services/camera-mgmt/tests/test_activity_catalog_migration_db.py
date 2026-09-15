"""What 033 and 034 do to a real database — the half a dict test cannot reach.

SKIPPED UNLESS YOU GIVE IT A SCRATCH DATABASE. Every other test in this suite
is hermetic and the conftest's `db` fixture is a stub, so nothing here can run
in the pull-request jobs. That is deliberate: a test needing Postgres that goes
red on a runner teaches everyone to ignore a red suite. Point it at a throwaway
database and it runs; leave it unset and it says why it skipped.

    MIGRATION_TEST_DB=postgresql+asyncpg://user:pw@host:5432/scratch \
        python -m pytest services/camera-mgmt/tests/test_activity_catalog_migration_db.py

Run it where alembic lives — the api image has it; a bare checkout may not.

TWO SAFETY RAILS, because the cost of getting the URL wrong is somebody's
appliance being migrated down:

  1. `migrations/env.py` takes its URL from DATABASE_URL when that is set, and
     inside the api container DATABASE_URL points at the LIVE database. Passing
     a scratch URL to alembic alone would therefore migrate the appliance. So
     DATABASE_URL is overridden with the scratch URL for the duration.
  2. The scratch database must be EMPTY. An empty schema is the one state that
     cannot belong to a running deployment.

WHAT IT PROVES, none of which the unit tests can:
  * upgrade removes exactly the sixteen withdrawn keys and keeps the eight
  * a camera config on a withdrawn type loses that activity, and the regions
    only that activity referenced go with it
  * a camera config on surviving types is left untouched
  * downgrade restores the withdrawn rows, with `ppe`'s repaired schema
  * the renamed row keeps its KEY and gets its old label back
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

DB_URL = os.environ.get("MIGRATION_TEST_DB")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set MIGRATION_TEST_DB to a scratch database URL")

if DB_URL:                                  # keep collection cheap when skipping
    pytest.importorskip(
        "alembic", reason="alembic is not installed here (it lives in the api image)")
    asyncpg = pytest.importorskip("asyncpg")
    from alembic import command                               # noqa: E402
    from alembic.config import Config                         # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

WITHDRAWN_TOTAL = 16                        # 9 in 033 + 7 in 034
SURVIVORS = {
    "entry_exit_WLE_logs", "stray_parking", "people_gathering", "no_person_area",
    "restricted_zone_entry", "car_detection", "idle_worker", "person_detection",
}
RENAMED_KEY = "entry_exit_WLE_logs"
NEW_LABEL = "Entry / exit"
OLD_LABEL = "Entry / exit + wrong lane"


def _alembic_url() -> str:
    return DB_URL if "+asyncpg" in DB_URL else DB_URL.replace("postgresql://",
                                                              "postgresql+asyncpg://")


def _asyncpg_dsn() -> str:
    return DB_URL.replace("+asyncpg", "")


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


def _keys() -> set[str]:
    return {r["key"] for r in _run(_query("SELECT key FROM analytics_activity_types"))}


def _label_of(key: str) -> str:
    rows = _run(_query("SELECT label FROM analytics_activity_types WHERE key = $1", key))
    return rows[0]["label"]


def _schema_of(key: str) -> list:
    rows = _run(_query("SELECT params_schema FROM analytics_activity_types WHERE key = $1",
                       key))
    raw = rows[0]["params_schema"]
    return json.loads(raw) if isinstance(raw, str) else raw


def _config_of(slug: str) -> dict:
    rows = _run(_query("SELECT analytics_config FROM cameras WHERE slug = $1", slug))
    raw = rows[0]["analytics_config"]
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.fixture(scope="module")
def migrated():
    """Migrate to 032, seed camera configs, then let 033 and 034 run over them."""
    # RAIL 2.
    tables = _run(_query("SELECT count(*) AS n FROM information_schema.tables "
                         "WHERE table_schema = 'public'"))[0]["n"]
    if tables:
        pytest.fail(f"MIGRATION_TEST_DB is not empty ({tables} tables) — refusing to run")

    command.upgrade(_cfg(), "032")

    assert "ppe" in _keys(), "fixture assumption: ppe is in the catalog at 032"

    withdrawn_cfg = {
        "activities": [
            {"id": "a1", "type": "ppe", "regions": ["r_gone"]},
            {"id": "a2", "type": "person_detection", "regions": ["r_kept"]},
        ],
        "regions": {"r_gone": {"name": "PPE zone"}, "r_kept": {"name": "Doorway"}},
    }
    survivor_cfg = {
        "activities": [{"id": "b1", "type": "car_detection", "regions": ["r_park"]}],
        "regions": {"r_park": {"name": "Car park"}},
    }
    for slug, cfg in (("cam-withdrawn", withdrawn_cfg), ("cam-survivor", survivor_cfg)):
        _run(_execute(
            "INSERT INTO cameras (slug, name, rtsp_url, analytics_config) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            slug, slug, f"rtsp://10.0.0.1/{slug}", json.dumps(cfg)))

    command.upgrade(_cfg(), "head")
    return True


class TestUpgrade:
    def test_the_catalog_is_narrowed_to_the_survivors(self, migrated):
        assert _keys() == SURVIVORS

    def test_an_activity_on_a_withdrawn_type_is_dropped(self, migrated):
        cfg = _config_of("cam-withdrawn")
        assert [a["type"] for a in cfg["activities"]] == ["person_detection"]

    def test_the_region_only_that_activity_used_goes_with_it(self, migrated):
        """A region left behind still draws in the zone editor, telling an
        operator an area is analysed when nothing analyses it."""
        cfg = _config_of("cam-withdrawn")
        assert set(cfg["regions"]) == {"r_kept"}

    def test_a_config_using_only_surviving_types_is_untouched(self, migrated):
        """The migration must not rewrite configs it has no business in."""
        cfg = _config_of("cam-survivor")
        assert [a["type"] for a in cfg["activities"]] == ["car_detection"]
        assert set(cfg["regions"]) == {"r_park"}

    def test_the_renamed_row_kept_its_key_and_took_the_new_label(self, migrated):
        assert _label_of(RENAMED_KEY) == NEW_LABEL


class TestDowngrade:
    def test_the_withdrawn_rows_come_back_with_their_schemas(self, migrated):
        command.downgrade(_cfg(), "032")
        try:
            keys = _keys()
            assert len(keys) == len(SURVIVORS) + WITHDRAWN_TOTAL
            assert {"ppe", "face_search", "Heatmap_overlay"} <= keys
            # 017's repair, not 016's silent no-op.
            assert {f["key"] for f in _schema_of("ppe")} >= {"subcategory_mapping"}
            assert _label_of(RENAMED_KEY) == OLD_LABEL
        finally:
            command.upgrade(_cfg(), "head")

    def test_camera_configs_are_not_restored_and_that_is_deliberate(self, migrated):
        """Not a gap — a documented decision. A config that lost an activity is
        indistinguishable on the way back from one that never had it, so
        re-inserting a guess would be worse than leaving it to the operator."""
        cfg = _config_of("cam-withdrawn")
        assert [a["type"] for a in cfg["activities"]] == ["person_detection"]
