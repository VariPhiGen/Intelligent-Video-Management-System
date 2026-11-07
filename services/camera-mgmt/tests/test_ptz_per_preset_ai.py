"""PTZ phase 2 — per-preset AI: each preset is a fixed-camera view.

  * once any preset has a rule, the camera runs the CONFIRMED preset's zones + rules, and nothing between presets
    (registered with an empty config, so the engine skips it without detecting — and the next stop is a swap)
  * an event is judged by the preset in force when it happened: that preset's activities and region names
  * each preset keeps day and night reference pictures, refreshed only from very sure matches
  * a scene that changed (a crane moved) still confirms the view

No camera, Valkey or Postgres: pure functions and fakes; real frames from tests/fixtures/ptz.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import ai_events, analytics_client, ptz_presets, ptz_track, ptz_views  # noqa: E402

pytestmark = pytest.mark.asyncio

FIX = ROOT / "tests" / "fixtures" / "ptz"
# Real frames from a customer site's PTZ camera; the public tree leaves them out.
needs_frames = pytest.mark.skipif(not FIX.is_dir(), reason="tests/fixtures/ptz is not in this tree")
T0 = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
GATE_CFG = {"regions": {"region_1": {"kind": "zone", "name": "Loading bay", "color": "#ff0000",
                                     "points": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]}},
            "activities": [{"type": "restricted_zone_entry", "regions": ["region_1"], "params": {}}]}
YARD_CFG = {"regions": {"region_1": {"kind": "zone", "name": "Scrap pile", "color": "#00ff00",
                                     "points": [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6]]}},
            "activities": [{"type": "people_gathering", "regions": ["region_1"], "params": {}}]}
CAM_CFG = {"regions": {}, "activities": [{"type": "people_gathering", "regions": [], "params": {}}]}
G, Y, N = "11111111-0000-0000-0000-000000000001", "11111111-0000-0000-0000-000000000002", \
    "11111111-0000-0000-0000-000000000003"
PRESETS = [{"id": G, "token": "1", "name": "Gate", "analytics_config": GATE_CFG},
           {"id": Y, "token": "2", "name": "Yard", "analytics_config": YARD_CFG},
           {"id": N, "token": "3", "name": "Road", "analytics_config": {"regions": {}, "activities": []}}]


def at(pid, token):
    return {"state": "preset", "preset_id": pid, "preset_token": token}


# ── which config runs ────────────────────────────────────────────────────────

def test_the_confirmed_presets_rules_run_and_nothing_between_presets():
    cfg, p = ptz_views.effective(CAM_CFG, PRESETS, at(G, "1"))
    assert cfg == GATE_CFG and p["name"] == "Gate"
    assert ptz_views.effective(CAM_CFG, PRESETS, at(Y, "2"))[0] == YARD_CFG
    assert ptz_views.effective(CAM_CFG, PRESETS, {"state": "none", "reason": "moving"}) == (ptz_views.EMPTY, None)
    assert ptz_views.effective(CAM_CFG, PRESETS, at(N, "3")) == (ptz_views.EMPTY, None)   # a preset with no rules
    assert ptz_views.effective(CAM_CFG, PRESETS, None) == (ptz_views.EMPTY, None)


def test_a_camera_with_no_preset_rules_keeps_its_own_config():
    no_rules = [{**p, "analytics_config": {"regions": {}, "activities": []}} for p in PRESETS]
    assert ptz_views.effective(CAM_CFG, no_rules, at(G, "1")) == (CAM_CFG, None)
    assert not ptz_views.follows_presets(no_rules) and ptz_views.follows_presets(PRESETS)


def test_between_presets_the_camera_stays_registered_with_nothing_to_run(monkeypatch):
    monkeypatch.setattr("backend.services.detection_engine.deepstream_mode", lambda: False)
    rows = [("ptz-a", True, False, [], dict(ptz_views.EMPTY)), ("fixed-b", True, False, [], dict(ptz_views.EMPTY))]
    desired, absent = analytics_client.desired_sets(rows, {"ptz-a"})
    assert desired["ptz-a"].analytics_config == ptz_views.EMPTY and "fixed-b" in absent
    monkeypatch.setattr("backend.services.detection_engine.deepstream_mode", lambda: True)
    _, absent = analytics_client.desired_sets(rows, {"ptz-a"})
    assert "ptz-a" in absent                                            # DeepStream is the engine: CPU stays out


def test_the_view_in_force_at_a_moment_is_found_in_the_log():
    log = [{"state": "none", "from": (T0 + timedelta(seconds=40)).isoformat()},
           {"state": "preset", "preset_id": G, "from": (T0 + timedelta(seconds=10)).isoformat()},
           {"state": "none", "from": T0.isoformat()}]
    assert ptz_views.view_at(log, T0 + timedelta(seconds=25))["preset_id"] == G
    assert ptz_views.view_at(log, T0 + timedelta(seconds=45))["state"] == "none"
    assert ptz_views.view_at(log, T0 - timedelta(seconds=5)) is None


# ── events are judged by their preset ────────────────────────────────────────

def _ev(activity, zone, when):
    return ai_events.EventIn(sensor_id="ptz-a", activity=activity, zone=zone, started_at=when, source="cpu")


def test_an_event_takes_its_presets_zone_names_and_is_tagged_with_the_preset():
    cam = SimpleNamespace(id=__import__("uuid").uuid4(), slug="ptz-a", analytics_config=CAM_CFG, retention_minutes=60)
    log = [{"state": "preset", "preset_id": Y, "preset_token": "2", "from": (T0 + timedelta(seconds=60)).isoformat()},
           {"state": "preset", "preset_id": G, "preset_token": "1", "from": T0.isoformat()}]

    def resolve(ev, camera):
        cfg, preset = ptz_views.effective(camera.analytics_config, PRESETS, ptz_views.view_at(log, ev.started_at))
        return SimpleNamespace(id=camera.id, slug=camera.slug, analytics_config=cfg, retention_minutes=60), preset

    rows, rejected = ai_events.prepare(
        [_ev("restricted_zone_entry", "region_1", T0 + timedelta(seconds=30)),     # at Gate
         _ev("people_gathering", "region_1", T0 + timedelta(seconds=90)),          # at Yard
         _ev("restricted_zone_entry", "region_1", T0 + timedelta(seconds=90))],    # Gate's rule, but at Yard
        {"ptz-a": cam}, now=T0 + timedelta(seconds=100), default_retention_minutes=60, resolve=resolve)
    assert [(r["zone"], r["preset_name"]) for r in rows] == [("Loading bay", "Gate"), ("Scrap pile", "Yard")]
    assert [r["reason"] for r in rejected] == ["activity_not_configured"]


# ── reference pictures ───────────────────────────────────────────────────────

def _jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


@needs_frames
def test_night_is_told_from_day_by_colour():
    from PIL import Image
    day = (FIX / "cpplus_pan_before.jpg").read_bytes()
    grey = _jpeg(Image.open(io.BytesIO(day)).convert("L").convert("RGB"))
    assert ptz_track.is_night(grey) and not ptz_track.is_night(_jpeg(Image.new("RGB", (64, 36), (40, 120, 200))))


def test_a_reference_is_stored_first_time_then_only_from_a_very_sure_match_once_a_day():
    sure = {"conf": 0.8, "shift": 0.004}
    assert ptz_track.reference_due({}, False, "position", {}, T0)                       # none yet: take one
    old = {"day": b"x", "day_at": T0 - timedelta(days=2)}
    assert ptz_track.reference_due(old, False, "picture", sure, T0)
    assert not ptz_track.reference_due(old, False, "position", sure, T0)                # not checked by picture
    assert not ptz_track.reference_due(old, False, "picture", {"conf": 0.8, "shift": 0.03}, T0)   # a bit off
    assert not ptz_track.reference_due(old, False, "picture", {"conf": 0.45, "shift": 0.0}, T0)   # unsure
    fresh = {"day": b"x", "day_at": T0 - timedelta(hours=3)}
    assert not ptz_track.reference_due(fresh, False, "picture", sure, T0)               # once a day at most
    assert ptz_track.reference_due(fresh, True, "picture", sure, T0)                    # night has none yet


async def _nosleep(_s):
    return None


def _same(jpeg):
    async def frame():
        return jpeg
    return frame


@needs_frames
async def test_a_crane_that_moved_does_not_stop_the_view_being_confirmed():
    """2026-10-28: "in one preset there was a crane and now it has moved?" — a big object gone from (or new in) a
    quarter of the picture: the rest of the scene still lines up where it was."""
    from PIL import Image, ImageDraw
    ref = (FIX / "cpplus_pan_before.jpg").read_bytes()
    im = Image.open(io.BytesIO(ref)).convert("RGB")
    w, h = im.size
    ImageDraw.Draw(im).rectangle([int(w * 0.55), int(h * 0.1), int(w * 0.95), int(h * 0.7)], fill=(150, 140, 120))
    info: dict = {}
    ok, how = await ptz_track.verify_arrival(_same(_jpeg(im)), ref, sleep=_nosleep, info=info)
    assert ok and how in ("picture", "position") and info.get("verdict") != "elsewhere"


@needs_frames
async def test_a_sub_stream_picture_is_compared_at_the_references_shape():
    """A 704×576 sub stream of a 16:9 camera is the same view squeezed — stretched back before comparing."""
    from PIL import Image
    ref = (FIX / "cpplus_pan_before.jpg").read_bytes()
    squeezed = _jpeg(Image.open(io.BytesIO(ref)).convert("RGB").resize((704, 576)))
    ok, how = await ptz_track.verify_arrival(_same(squeezed), lambda still: ref, sleep=_nosleep)
    assert (ok, how) == (True, "picture")


def test_the_preset_list_counts_what_each_view_watches():
    assert ptz_presets.ai_summary(GATE_CFG) == {"zones": 1, "tripwires": 0, "rules": 1}
    assert ptz_presets.ai_summary(None) == {"zones": 0, "tripwires": 0, "rules": 0}


@needs_frames
@pytest.mark.parametrize("cam", ["cpplus", "ipro"])
@pytest.mark.parametrize("axis", ["pan", "tilt"])
def test_the_same_view_matches_a_moved_one_never_does_and_a_changed_scene_is_never_called_elsewhere(cam, axis):
    """Real frames of two makes: before/after a calibration move (0.24–0.38 of the frame)."""
    from PIL import Image, ImageDraw
    before = (FIX / f"{cam}_{axis}_before.jpg").read_bytes()
    after = (FIX / f"{cam}_{axis}_after.jpg").read_bytes()
    assert ptz_track.view_match(before, before)[0] == "same"
    assert ptz_track.view_match(before, after)[0] != "same"
    assert ptz_track.view_match(after, before)[0] != "same"
    for frac in (0.1, 0.25, 0.4):                                   # a crane / a pile / a new structure
        im = Image.open(io.BytesIO(before)).convert("RGB")
        w, h = im.size
        side = frac ** 0.5
        ImageDraw.Draw(im).rectangle([int(w * (0.95 - side)), int(h * 0.05), int(w * 0.95), int(h * (0.05 + side))],
                                     fill=(150, 140, 120))
        verdict = ptz_track.view_match(before, _jpeg(im))[0]
        assert verdict != "elsewhere", (frac, verdict)
        if frac <= 0.1:
            assert verdict == "same"
