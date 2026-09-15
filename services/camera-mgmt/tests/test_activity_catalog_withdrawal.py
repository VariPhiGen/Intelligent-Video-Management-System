"""Migrations 033 and 034 withdraw activity types — what that must not break.

THESE TWO ARE NOT SCHEMA CHANGES. They delete catalog rows and then rewrite
stored `cameras.analytics_config`: activities whose type was withdrawn are
dropped, and regions no surviving activity references go with them. Data
surgery with a downgrade that claims to put everything back exactly as it was.
Nothing tested any of it, and the repository has no other migration coverage.

WHAT IS TESTED HERE, and why it is worth a hermetic test rather than a database:
the dangerous parts are pure. `_prune_regions` is a dict transform; the
withdrawn lists and the restore snapshots are module data; the rename's key
invariant is a constant. Each can go wrong silently, and each is cheap to pin.
The database behaviour — upgrade actually removing the rows, downgrade actually
restoring them — lives in test_activity_catalog_migration_db.py, which needs a
scratch database and skips without one.

The filenames start with digits, so these modules cannot be imported by name.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "migrations" / "versions"
ACTIVITIES_TS = (ROOT.parents[1] / "frontend-react" / "src" / "pages" / "config"
                 / "analytics" / "activities.ts")


def _load(filename: str):
    """Import a migration module by path, standing in for `alembic` if absent.

    A migration does `from alembic import op` at import time, so loading one
    would otherwise require alembic — and this file would ERROR AT COLLECTION,
    not skip, on any machine without the service's own dependencies. Nothing
    tested here calls `op`: `_prune_regions` is a dict transform and everything
    else is module data. So the import is satisfied with a placeholder that is
    removed again, rather than gating real tests behind an installation they
    never use. Where alembic IS installed, the real one is used untouched.
    """
    stubbed = False
    if "alembic" not in sys.modules:
        try:
            importlib.import_module("alembic")
        except ImportError:
            placeholder = types.ModuleType("alembic")
            placeholder.op = types.SimpleNamespace()
            sys.modules["alembic"] = placeholder
            stubbed = True
    try:
        path = VERSIONS / filename
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if stubbed:
            sys.modules.pop("alembic", None)


M033 = _load("033_withdraw_nine_activity_types.py")
M034 = _load("034_narrow_catalog_and_rename_entry_exit.py")


# ── The pruning rule both migrations depend on ──────────────────────────────
#
# A region left behind after its last activity is dropped still draws in the
# zone editor, which tells an operator that an area is being analysed when
# nothing analyses it. That is the failure this rule exists for, so it is
# tested on the function rather than inferred from the migration's outcome.

@pytest.mark.parametrize("prune", [M033._prune_regions, M034._prune_regions],
                         ids=["033", "034"])
class TestPruneRegions:
    def test_a_region_no_activity_references_is_dropped(self, prune):
        cfg = {"activities": [{"type": "kept", "regions": ["r1"]}],
               "regions": {"r1": {"name": "Zone 1"}, "r2": {"name": "Orphan"}}}
        assert prune(cfg) is True
        assert set(cfg["regions"]) == {"r1"}

    def test_a_region_still_referenced_survives(self, prune):
        cfg = {"activities": [{"type": "kept", "regions": ["r1"]}],
               "regions": {"r1": {"name": "Zone 1"}}}
        assert prune(cfg) is False
        assert set(cfg["regions"]) == {"r1"}

    def test_a_region_shared_by_two_activities_survives_losing_one(self, prune):
        """The subtle one: one activity is gone, another still points at it."""
        cfg = {"activities": [{"type": "kept", "regions": ["shared"]}],
               "regions": {"shared": {"name": "Gate"}}}
        assert prune(cfg) is False
        assert "shared" in cfg["regions"]

    def test_every_region_orphaned_leaves_an_empty_map_not_a_missing_key(self, prune):
        cfg = {"activities": [], "regions": {"r1": {}, "r2": {}}}
        assert prune(cfg) is True
        assert cfg["regions"] == {}

    def test_a_config_with_no_regions_is_left_alone(self, prune):
        cfg = {"activities": [{"type": "kept"}]}
        assert prune(cfg) is False

    def test_an_activity_with_no_regions_key_does_not_raise(self, prune):
        """Stored configs predate the regions field; a KeyError here would
        abort the whole migration on the first old camera it met."""
        cfg = {"activities": [{"type": "kept"}], "regions": {"r1": {}}}
        assert prune(cfg) is True
        assert cfg["regions"] == {}


# ── The withdrawn lists and their restore snapshots ─────────────────────────

class TestWithdrawnData:
    def test_the_withdrawn_keys_come_from_the_snapshot(self):
        """`_WITHDRAWN` is derived from `_WITHDRAWN_ROWS`, which is what makes a
        downgrade able to restore exactly what the upgrade removed. Two
        hand-maintained lists here would be the drift this repo keeps paying
        for."""
        for mod in (M033, M034):
            assert mod._WITHDRAWN == [row["key"] for row in mod._WITHDRAWN_ROWS]

    def test_each_migration_withdraws_what_its_docstring_claims(self):
        assert len(M033._WITHDRAWN) == 9
        assert len(M034._WITHDRAWN) == 7

    def test_no_type_is_withdrawn_twice(self):
        """034 is a second pass over the same catalog. An overlap would mean
        033's downgrade restoring a row 034 also restores — and, worse, that
        one of the two snapshots is of a row that no longer existed."""
        assert not set(M033._WITHDRAWN) & set(M034._WITHDRAWN)

    @pytest.mark.parametrize("mod", [M033, M034], ids=["033", "034"])
    def test_every_snapshot_row_carries_what_the_downgrade_inserts(self, mod):
        """downgrade() INSERTs key, label, color, sort_order and params_schema.
        A row missing one of them fails at restore time — the one moment
        nobody is watching."""
        for row in mod._WITHDRAWN_ROWS:
            missing = {"key", "label", "color", "sort_order", "params_schema"} - set(row)
            assert not missing, f"{row.get('key')} lacks {missing}"
            assert isinstance(row["params_schema"], list)
            json.dumps(row["params_schema"])      # must survive the round trip

    def test_the_restored_ppe_is_the_repaired_one(self):
        """033's docstring makes a specific promise: 016 gave `ppe` a schema of
        `frame_accuracy` alone, which made it a silent no-op, and 017 repaired
        it with `subcategory_mapping`. The snapshot must hold 017's version, or
        a downgrade quietly reinstates the broken detector."""
        ppe = next(r for r in M033._WITHDRAWN_ROWS if r["key"] == "ppe")
        assert {f["key"] for f in ppe["params_schema"]} >= {"subcategory_mapping"}


# ── The rename in 034 ───────────────────────────────────────────────────────

class TestLabelOnlyRename:
    def test_the_key_is_not_withdrawn(self):
        """Renaming a row the same migration deletes would be incoherent."""
        assert M034._RENAMED_KEY not in M034._WITHDRAWN

    def test_the_rename_actually_changes_something(self):
        assert M034._OLD_LABEL != M034._NEW_LABEL

    def test_the_key_is_not_part_of_the_rename(self):
        """THE INVARIANT STORED CONFIGS DEPEND ON. Camera configs reference
        types by key, and the admin editor makes a saved row's key read-only
        for this reason: changing it here would orphan every config using it
        while looking like a cosmetic edit. The key must appear in the new
        label's migration only as a WHERE clause, never as a new value."""
        assert M034._RENAMED_KEY not in M034._NEW_LABEL
        assert M034._RENAMED_KEY not in M034._OLD_LABEL


# ── The offline catalog the SPA falls back to ───────────────────────────────

@pytest.mark.skipif(not ACTIVITIES_TS.is_file(), reason="SPA sources not present")
class TestFrontendFallbackAgrees:
    """DEFAULT_ACTIVITY_CATALOG is what the AI Config tab offers when
    `/analytics/types` fails. It is a second copy of the catalog, maintained by
    hand, and its header admits the failure mode directly: it carried 014's
    placeholder keys long after 017 deleted them. That is this repository's
    recurring defect — one list written twice — so it is pinned rather than
    trusted.
    """

    @staticmethod
    def _entries():
        text = ACTIVITIES_TS.read_text(encoding="utf-8")
        # Anchor on the declaration, NOT the first mention: the header comment
        # names DEFAULT_ACTIVITY_CATALOG too, and matching that parses the
        # prose instead of the array and silently finds nothing.
        decl = text.split("export const DEFAULT_ACTIVITY_CATALOG", 1)[1]
        block = decl[decl.index("["):decl.index("];")]
        return dict(re.findall(r"key:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'", block))

    def test_the_fallback_offers_nothing_that_was_withdrawn(self):
        """An operator picking a withdrawn type from the offline fallback gets
        a 422 on save — the catalog is validated server-side — with nothing in
        the UI explaining why."""
        offered = set(self._entries())
        withdrawn = set(M033._WITHDRAWN) | set(M034._WITHDRAWN)
        assert not offered & withdrawn, f"fallback still offers {offered & withdrawn}"

    def test_the_fallback_is_not_empty(self):
        """Guards the test above from passing because the parse found nothing."""
        assert len(self._entries()) >= 5

    def test_the_renamed_type_carries_its_new_label(self):
        entries = self._entries()
        assert entries.get(M034._RENAMED_KEY) == M034._NEW_LABEL
