"""PTZ phase 4: a dataset of a PTZ camera samples only while the camera was AT the chosen presets.

  * the confirmed views of the window become intervals at those presets; moves and other presets are left out
  * the last seconds before a move are left out (a move is noticed a poll after it began); fly-bys are dropped
  * frames are spread evenly over just that time — midpoints, never on an edge — or scanned in detection mode
  * footage older than the view history falls back to the position log, 3 s after arrival, and says so
  * every frame's manifest entry names the preset it shows

No camera, NVR or Postgres: pure functions and an injected snapshot fetcher.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import dataset_build, ptz_dataset as pd  # noqa: E402

T0 = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
GATE, YARD = "11111111-0000-0000-0000-000000000001", "11111111-0000-0000-0000-000000000002"


def at(s):
    return T0 + timedelta(seconds=s)


# A patrol: Gate 0-60 s, moving, Yard 70-130, moving, Gate 140-200, a 1-s fly-by of Yard, … until the window ends.
VIEWS = [
    {"at": at(-30), "state": "preset", "preset_id": GATE, "preset_name": "Gate"},      # in force at the window start
    {"at": at(60), "state": "none"},
    {"at": at(70), "state": "preset", "preset_id": YARD, "preset_name": "Yard"},
    {"at": at(130), "state": "none"},
    {"at": at(140), "state": "preset", "preset_id": GATE, "preset_name": "Gate"},
    {"at": at(200), "state": "none"},
    {"at": at(205), "state": "preset", "preset_id": YARD, "preset_name": "Yard"},
    {"at": at(206), "state": "none"},
]


def test_only_the_time_at_the_chosen_presets_counts_and_the_seconds_before_a_move_are_left_out():
    spans = pd.intervals(VIEWS, T0, at(300), {GATE})
    assert [(s["start"], s["end"], s["preset_name"]) for s in spans] == [
        (T0, at(58), "Gate"), (at(140), at(198), "Gate")]               # clipped to the window; 2 s before each move
    both = pd.intervals(VIEWS, T0, at(300), {GATE, YARD})
    assert [s["preset_name"] for s in both] == ["Gate", "Yard", "Gate"]  # the 1-s fly-by of Yard is dropped
    assert pd.total_seconds(spans) == 116
    assert pd.per_preset(both) == [{"preset_id": GATE, "preset_name": "Gate", "seconds": 116.0, "visits": 2},
                                   {"preset_id": YARD, "preset_name": "Yard", "seconds": 58.0, "visits": 1}]
    assert pd.intervals(VIEWS, T0, at(300), {"nope"}) == []


def test_a_view_still_in_force_at_the_window_end_runs_to_the_end():
    spans = pd.intervals([{"at": at(10), "state": "preset", "preset_id": GATE, "preset_name": "Gate"}], T0, at(100), {GATE})
    assert [(s["start"], s["end"]) for s in spans] == [(at(10), at(100))]


def test_frames_are_spread_over_the_time_at_presets_never_on_an_edge():
    spans = pd.intervals(VIEWS, T0, at(300), {GATE})                     # 58 s + 58 s
    plan = pd.spread(spans, 4)
    assert [p["at"] for p in plan] == [at(14.5), at(43.5), at(140 + 14.5), at(140 + 43.5)]
    assert all(s["start"] < p["at"] < s["end"] for p in plan for s in spans if s["preset_id"] == p["preset_id"]
               and s["start"] <= p["at"] <= s["end"])
    assert {p["preset_name"] for p in plan} == {"Gate"}
    assert pd.spread([], 10) == [] and pd.spread(spans, 0) == []
    scan = pd.scan(spans, 20.0)
    assert [p["at"] for p in scan] == [at(10), at(30), at(50), at(150), at(170), at(190)]


def test_footage_older_than_the_view_history_uses_the_position_log_after_it_settled():
    pos = [{"at": at(0), "state": "at_preset", "preset_id": GATE, "preset_name": "Gate"},
           {"at": at(40), "state": "moving", "preset_id": None},
           {"at": at(45), "state": "at_preset", "preset_id": GATE, "preset_name": "Gate"}]
    spans = pd.intervals(pos, T0, at(100), {GATE}, at_state="at_preset", settle_s=pd.POSITION_SETTLE_S)
    assert [(s["start"], s["end"]) for s in spans] == [(at(3), at(38)), (at(48), at(100))]


def test_every_frame_of_a_preset_dataset_names_its_preset(tmp_path):
    spans = pd.intervals(VIEWS, T0, at(300), {GATE, YARD})
    plan = [{**p, "view": "picture"} for p in pd.spread(spans, 6)]
    asked = []

    async def fetch(slug, iso, quality):
        asked.append(iso)
        return b"JPEG"
    recipe = {"id": 7, "camera_slug": "ptz-a", "window_start": T0, "window_end": at(300), "target_frames": 6,
              "jpeg_quality": 85, "plan": plan}
    out = asyncio.run(dataset_build.collect_frames(recipe, str(tmp_path), fetch))
    assert out["collected"] == 6 and len(asked) == 6
    assert [f["preset_name"] for f in out["frames"]] == [p["preset_name"] for p in plan]
    assert all(f["view"] == "picture" and f["preset_id"] in (GATE, YARD) for f in out["frames"])
    assert out["frames"][0]["timestamp"] == dataset_build._iso_z(plan[0]["at"])


def test_a_plain_dataset_is_sampled_as_before(tmp_path):
    async def fetch(slug, iso, quality):
        return b"JPEG"
    recipe = {"id": 8, "camera_slug": "fixed", "window_start": T0, "window_end": at(100), "target_frames": 4,
              "jpeg_quality": 85}
    out = asyncio.run(dataset_build.collect_frames(recipe, str(tmp_path), fetch))
    assert out["collected"] == 4 and "preset_id" not in out["frames"][0]
