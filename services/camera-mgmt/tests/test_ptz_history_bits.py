"""PTZ history helpers: time spent where, CSV, and the patrol runner's arrival check."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.ptz_track import time_split, to_csv  # noqa: E402

T0 = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)


def _r(minutes, state, token=None, name=None):
    return {"at": T0 + timedelta(minutes=minutes), "state": state, "preset_token": token, "preset_name": name}


def test_time_split_counts_from_the_row_in_force_at_the_start():
    rows = [_r(-30, "at_preset", "1", "Gate"),        # in force when the window opens
            _r(30, "moving"), _r(31, "at_preset", "2", "Yard"), _r(45, "off_preset")]
    out = {s["key"]: s for s in time_split(rows, T0, T0 + timedelta(hours=1))}
    assert out["preset:1"]["seconds"] == 30 * 60 and out["preset:1"]["pct"] == 50.0
    assert out["preset:2"]["seconds"] == 14 * 60 and out["preset:2"]["label"] == "Yard"
    assert out["moving"]["seconds"] == 60 and out["off_preset"]["seconds"] == 15 * 60
    assert "no_data" not in out


def test_time_split_says_when_there_is_no_data():
    out = {s["key"]: s for s in time_split([_r(20, "at_preset", "1", "Gate")], T0, T0 + timedelta(hours=1))}
    assert out["no_data"]["seconds"] == 20 * 60 and out["preset:1"]["seconds"] == 40 * 60


def test_csv_has_a_header_and_blank_for_missing():
    text = to_csv([{"at": "2026-10-26T00:00:00+00:00", "state": "moving", "camera_name": "PT1"}])
    lines = text.strip().splitlines()
    assert lines[0].startswith("at,camera_name,state") and lines[1].startswith("2026-10-26T00:00:00+00:00,PT1,moving,")


def test_patrol_arrival_verdict():
    import pytest
    pytest.importorskip("backend.extensions.ptz_patrols")
    from backend.extensions.ptz_patrols.runner import arrival_verdict
    here = {"pan": 0.2, "tilt": 0.1, "zoom": 0.3, "moving_pan_tilt": "IDLE", "moving_zoom": "IDLE"}
    assert arrival_verdict(here, {"pan": 0.21, "tilt": 0.1, "zoom": 0.3}) == (True, "")
    assert arrival_verdict(here, {"pan": 0.5, "tilt": 0.1, "zoom": 0.3})[0] is False
    assert arrival_verdict({**here, "moving_pan_tilt": "MOVING"}, {"pan": 0.2})[1] == "still moving"
    assert arrival_verdict(None, {"pan": 0.2})[0] is None             # can't tell → no row, not "missed"
    assert arrival_verdict(here, None)[0] is None
