"""Where a PTZ camera points (services/ptz_track): classification, what counts as a change, who moved it."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.ptz_track import attribute, changed, classify, decide  # noqa: E402

GATE = {"token": "1", "name": "Gate", "pan": 0.20, "tilt": 0.10, "zoom": 0.30}
YARD = {"token": "2", "name": "Yard", "pan": -0.50, "tilt": 0.00, "zoom": 0.00}
T0 = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


def st(pan, tilt, zoom, moving=False):
    m = "MOVING" if moving else "IDLE"
    return {"pan": pan, "tilt": tilt, "zoom": zoom, "moving_pan_tilt": m, "moving_zoom": "IDLE"}


def test_classify():
    assert classify(st(0.205, 0.10, 0.30), [GATE, YARD]) == ("at_preset", GATE)
    assert classify(st(0.30, 0.10, 0.30), [GATE, YARD])[0] == "off_preset"
    assert classify(st(0.2, 0.1, 0.3, moving=True), [GATE])[0] == "moving"
    assert classify(None, [GATE])[0] == "unknown"
    assert classify({"pan": None, "tilt": None, "zoom": None}, [GATE])[0] == "unknown"
    # pan wraps: -0.995 and +0.995 are 0.01 apart on a 360° head
    wrap = {"token": "3", "name": "Back", "pan": 0.995, "tilt": 0.0, "zoom": 0.0}
    assert classify(st(-0.995, 0.0, 0.0), [wrap])[1] == wrap
    # two presets close together: the nearer wins
    near = {**GATE, "token": "9", "name": "Gate close", "pan": 0.212}
    assert classify(st(0.211, 0.10, 0.30), [GATE, near])[1] == near


def test_changes_are_state_preset_or_a_new_off_preset_spot():
    prev = {"state": "at_preset", "preset_token": "1", "pan": 0.2, "tilt": 0.1, "zoom": 0.3}
    assert not changed(prev, "at_preset", GATE, st(0.2, 0.1, 0.3))
    assert changed(prev, "at_preset", YARD, st(-0.5, 0, 0))
    assert changed(prev, "moving", None, st(0.1, 0.1, 0.3))
    off = {"state": "off_preset", "pan": 0.4, "tilt": 0.1, "zoom": 0.3}
    assert not changed(off, "off_preset", None, st(0.41, 0.1, 0.3))   # jitter
    assert changed(off, "off_preset", None, st(0.6, 0.1, 0.3))        # parked somewhere else


def test_cause_is_the_fresh_hint_else_the_finished_move_else_the_camera():
    now = T0.timestamp()
    assert attribute({"cause": "operator", "actor": "ramesh", "at": now - 5}, None, now) == ("operator", "ramesh")
    assert attribute({"cause": "operator", "actor": "ramesh", "at": now - 600}, None, now) == ("camera", None)
    moving = {"state": "moving", "cause": "patrol", "actor": None}
    assert attribute(None, moving, now) == ("patrol", None)
    assert attribute(None, {"state": "at_preset"}, now) == ("camera", None)


def test_a_night_on_one_preset_is_one_row():
    cur, row = decide(None, st(0.2, 0.1, 0.3), [GATE], None, T0)
    assert row["state"] == "at_preset" and row["preset_name"] == "Gate" and row["cause"] is None   # first sight
    rows = 0
    for i in range(1, 100):
        cur, row = decide(cur, st(0.2, 0.1, 0.3), [GATE], None, T0 + timedelta(seconds=30 * i))
        rows += row is not None
    assert rows == 0 and cur["since"] == T0.isoformat()


def test_an_operator_move_is_recorded_with_who():
    cur, _ = decide(None, st(0.2, 0.1, 0.3), [GATE, YARD], None, T0)
    hint = {"cause": "operator", "actor": "ramesh", "at": (T0 + timedelta(seconds=10)).timestamp()}
    cur, row = decide(cur, st(0.0, 0.1, 0.3, moving=True), [GATE, YARD], hint, T0 + timedelta(seconds=12))
    assert row["state"] == "moving" and row["actor"] == "ramesh"
    cur, row = decide(cur, st(0.6, 0.3, 0.8), [GATE, YARD], None, T0 + timedelta(seconds=200))
    assert row["state"] == "off_preset" and (row["cause"], row["actor"]) == ("operator", "ramesh")


def test_a_camera_at_its_recorded_home_reads_home_not_off_preset():
    """2026-10-27: "Even if I am at home position it says Off preset" — home is classified like a preset, and a
    real preset at the same spot still wins the tie (listed first)."""
    from backend.services.ptz_track import HOME_TOKEN, classify
    home = {"token": HOME_TOKEN, "name": "Home", "pan": 0.1, "tilt": -0.2, "zoom": 0.0}
    status = {"pan": 0.1005, "tilt": -0.2, "zoom": 0.0, "moving_pan_tilt": "IDLE", "moving_zoom": "IDLE"}
    assert classify(status, [home]) == ("at_preset", home)
    gate = {"token": "3", "name": "Gate", "pan": 0.1, "tilt": -0.2, "zoom": 0.0}
    assert classify(status, [gate, home])[1] is gate


def test_history_retention_defaults_and_limits():
    """Operator moves 90 days, patrol steps 14 (patrol rows are most of the volume: ~6/min per patrolling camera);
    an admin's values are clamped — never 0 days of operator history, never a year of patrol laps."""
    from backend.services.ptz_track import RETENTION_DEFAULTS, clean_retention
    assert clean_retention(None) == RETENTION_DEFAULTS == {"operator_days": 90, "patrol_days": 14}
    assert clean_retention({"operator_days": 30, "patrol_days": 7}) == {"operator_days": 30, "patrol_days": 7}
    assert clean_retention({"operator_days": 0, "patrol_days": 9999}) == {"operator_days": 7, "patrol_days": 90}
    assert clean_retention({"operator_days": "x", "patrol_days": True}) == RETENTION_DEFAULTS
