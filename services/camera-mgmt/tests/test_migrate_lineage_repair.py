"""backend.migrate's one-time repair for appliances on the pre-merge `field` migration numbering.

`field` and the ai line both shipped migrations 033/035/036/050/051/052 with different contents.
The repair must fire ONLY for the old field lineage (collision id + `datasets` + no `alert_rules`) — never for
an ai appliance (whose ids are the canonical ones) or a fresh database. Verified end-to-end against a
real postgres:15 on 2026-10-07: old-field 052 -> backup + stamp 032 -> upgrade to 056 with data intact; an
ai 052 -> 053..056 only, no repair; fresh -> 056; re-run = no-op; the backup restores to the 052 state.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.migrate import is_old_field_lineage  # noqa: E402


def test_old_field_lineage_is_detected_at_every_colliding_id():
    for v in ("033", "035", "036", "050", "051", "052"):
        assert is_old_field_lineage({"version": v, "datasets": True, "alert_rules": False}), v


def test_ai_line_and_fresh_databases_are_left_alone():
    # ai line before its own datasets (045): colliding id, no datasets table
    assert not is_old_field_lineage({"version": "033", "datasets": False, "alert_rules": False})
    # ai at 050-052: has datasets AND alert_rules
    assert not is_old_field_lineage({"version": "052", "datasets": True, "alert_rules": True})
    # already on the merged chain, or never migrated
    assert not is_old_field_lineage({"version": "056", "datasets": True, "alert_rules": True})
    assert not is_old_field_lineage({"version": None, "datasets": False, "alert_rules": False})
    # a non-colliding old id is fine as-is (shared history up to 032)
    assert not is_old_field_lineage({"version": "032", "datasets": False, "alert_rules": False})


# ---- `manual` branch (QA sink) numbering: its 053/054 are 058/059 on the merged chain (2026-10-15) ----------

from backend.migrate import is_manual_qa_lineage, repair_target  # noqa: E402


def test_manual_qa_lineage_is_detected_and_restamped_to_052():
    for v in ("053", "054", "055"):
        state = {"version": v, "datasets": True, "alert_rules": True, "qa_sink": True}
        assert is_manual_qa_lineage(state), v
        assert repair_target(state) == "052", v


def test_field_053_054_and_merged_chain_are_left_alone():
    # field's own 053 (ONVIF-HTTPS) / 054 (registration): no qa_sink_config yet
    for v in ("053", "054", "055"):
        assert repair_target({"version": v, "datasets": True, "alert_rules": True, "qa_sink": False}) is None
    # merged chain at/after the QA sink
    for v in ("058", "059"):
        assert repair_target({"version": v, "datasets": True, "alert_rules": True, "qa_sink": True}) is None


def test_old_field_lineage_still_wins_its_own_target():
    assert repair_target({"version": "052", "datasets": True, "alert_rules": False, "qa_sink": False}) == "032"
