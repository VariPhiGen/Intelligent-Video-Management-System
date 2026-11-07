"""PTZ phase 3 — a preset's zones follow its view when the preset changes, and outlive a delete.

  * re-saved a little off: the zones move with the view and keep running ("shifted", undoable)
  * re-saved far off, zoomed, or not lined up: rules paused until someone checks ("review")
  * re-stored in the camera's own web page: paused until the first picture of the new view lines them up
  * deleted: zones + rules + picture archived, copyable onto another preset (always for review)

No camera, Valkey or Postgres: pure functions, plain objects for rows, real frames from tests/fixtures/ptz.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import ptz_presets, ptz_view_changes as vc, ptz_views  # noqa: E402


FIX = ROOT / "tests" / "fixtures" / "ptz"
# Real frames from a customer site's PTZ camera; the public tree leaves them out.
needs_frames = pytest.mark.skipif(not FIX.is_dir(), reason="tests/fixtures/ptz is not in this tree")
ZONE = {"kind": "zone", "name": "Loading bay", "color": "#ff0000", "points": [[0.3, 0.3], [0.6, 0.3], [0.6, 0.6], [0.3, 0.6]]}
WIRE = {"kind": "tripwire", "name": "Gate line", "color": "#00ff00", "points": [[0.2, 0.8], [0.8, 0.8]], "direction": "both"}
CFG = {"regions": {"region_1": ZONE, "region_2": WIRE},
       "activities": [{"type": "restricted_zone_entry", "regions": ["region_1"], "params": {}}]}


def _jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def view(name: str, dx: float = 0.0, dy: float = 0.0) -> bytes:
    """A view of a real scene, the head turned so the scene sits (dx, dy) of the frame further right / down."""
    from PIL import Image
    im = Image.open(FIX / f"{name}.jpg").convert("RGB")
    w, h = im.size
    m = 0.15
    cw, ch = int(w * (1 - 2 * m)), int(h * (1 - 2 * m))
    x, y = int(w * m - dx * cw), int(h * m - dy * ch)
    return _jpeg(im.crop((x, y, x + cw, y + ch)))


def row(cfg=CFG, **kw):
    base = dict(id="p1", camera_id="c1", token="1", name="Gate", pan=0.1, tilt=0.2, zoom=0.0, analytics_config=cfg,
                analytics_review=None, previous_reference=None, reference=None, reference_at=None,
                reference_night=None, reference_night_at=None, thumbnail=None, thumbnail_at=None)
    return SimpleNamespace(**{**base, **kw})


# ── the plan (pure) ──────────────────────────────────────────────────────────

def test_a_resave_on_the_same_view_changes_nothing():
    assert vc.plan(CFG, (0.004, -0.003), False) == (CFG, None, "")


def test_a_small_slide_moves_the_zones_with_the_view_and_they_keep_running():
    cfg, state, note = vc.plan(CFG, (0.05, -0.02), False)
    assert state == "shifted" and "5% right" in note and "2% up" in note
    assert cfg["regions"]["region_1"]["points"][0] == [0.35, 0.28]
    assert cfg["regions"]["region_2"]["points"] == [[0.25, 0.78], [0.85, 0.78]]
    assert CFG["regions"]["region_1"]["points"][0] == [0.3, 0.3]          # the stored one untouched (undo)
    assert not vc.paused({"state": state})


def test_a_big_slide_a_zoom_or_an_unmatched_picture_pauses_for_review():
    cfg, state, _ = vc.plan(CFG, (0.15, 0.0), False)
    assert state == "review" and cfg["regions"]["region_1"]["points"][0] == [0.45, 0.3]   # moved, as a starting point
    assert vc.plan(CFG, (0.03, 0.0), True)[:2] == (CFG, "review")                         # zoom: left as they were
    assert vc.plan(CFG, None, False)[:2] == (CFG, "review")
    assert vc.paused({"state": "review"}) and vc.paused({"state": "awaiting_picture"})


def test_a_zone_pushed_off_the_picture_is_cut_to_it_or_sent_for_review():
    half_out = {"regions": {"r": {**ZONE, "points": [[0.8, 0.1], [0.95, 0.1], [0.95, 0.3], [0.8, 0.3]]}},
                "activities": CFG["activities"]}
    cfg, lost, trimmed = vc.shift_config(half_out, 0.07, 0.0)            # loses 0.02 of 0.15 width: cut to fit
    pts = cfg["regions"]["r"]["points"]
    assert not lost and trimmed == ["Loading bay"] and max(p[0] for p in pts) == 1.0 and len(pts) == 4
    _, lost, _ = vc.shift_config(half_out, 0.15, 0.0)                    # most of it off the picture
    assert lost == ["Loading bay"]
    _, lost, _ = vc.shift_config(CFG, 0.0, 0.25)                         # the tripwire's ends leave the picture
    assert lost == ["Gate line"]
    cfg, state, note = vc.plan(half_out, (0.07, 0.0), False)              # a small slide, but part of it is gone
    assert state == "review" and "outside the picture" in note


def test_clipping_keeps_the_part_inside_the_picture():
    square = [[-0.5, 0.2], [0.5, 0.2], [0.5, 0.6], [-0.5, 0.6]]
    assert vc._area(vc.clip_to_frame(square)) == pytest.approx(0.2)


def test_a_paused_preset_runs_nothing_and_a_shifted_one_runs():
    presets = [{"id": "p1", "token": "1", "name": "Gate", "analytics_config": CFG, "analytics_review": {"state": "review"}}]
    at = {"state": "preset", "preset_id": "p1", "preset_token": "1"}
    assert ptz_views.effective({}, presets, at) == (ptz_views.EMPTY, None)
    assert ptz_views.follows_presets(presets)              # still follows presets: never falls back to the whole camera
    presets[0]["analytics_review"] = {"state": "shifted"}
    assert ptz_views.effective({}, presets, at)[0] == CFG


def test_the_ui_is_told_whether_it_can_undo_and_whether_rules_are_paused():
    rec = vc.record("shifted", "zones moved 5% right with the view", "re-saved by ana", "ana", (0.05, 0), False, CFG)
    out = vc.review_out(rec)
    assert out["can_undo"] and not out["paused"] and "previous_config" not in out
    assert vc.review_out(vc.record("review", "", "", None, None, True, CFG))["paused"]
    assert vc.review_out(None) is None
    assert ptz_presets.out(SimpleNamespace(**{**vars(row()), "analytics_review": rec, "is_home": False, "sort_order": 1,
                                              "missing_on_camera": False, "created_by": None, "created_at": None})
                           )["review"]["state"] == "shifted"


def test_a_position_re_stored_outside_vms_is_told_from_noise():
    r = row()
    assert not vc.position_moved(r, 0.11, 0.21, 0.0)                    # GetStatus vs the list: a hair apart
    assert vc.position_moved(r, 0.2, 0.2, 0.0)
    assert vc.position_moved(r, -0.98, 0.2, 0.0) is True                # pan wraps: 0.1 → -0.98 is 0.92 away
    assert not vc.position_moved(row(pan=None, tilt=None, zoom=None), 0.5, 0.5, 0.5)


# ── on real pictures ─────────────────────────────────────────────────────────

@needs_frames
@pytest.mark.parametrize("name", ["cpplus_pan_before", "ipro_tilt_before"])
@pytest.mark.asyncio
async def test_a_preset_re_saved_a_little_off_moves_its_zones_by_what_the_pictures_show(name):
    r = row(reference=view(name), thumbnail=b"thumb")
    old = r.reference
    rec = await vc.apply_new_view(r, view(name, 0.05, 0.03), old=old, zoom_changed=False, reason="re-saved", by="ana")
    assert rec["state"] == "shifted" and not vc.paused(rec)
    x, y = r.analytics_config["regions"]["region_1"]["points"][0]
    assert x == pytest.approx(0.35, abs=0.01) and y == pytest.approx(0.33, abs=0.01)
    assert r.previous_reference == old and r.reference not in (None, old) and r.reference_night is None
    assert rec["previous_config"] == CFG


@needs_frames
@pytest.mark.asyncio
async def test_a_preset_re_saved_on_the_same_view_keeps_its_zones_and_takes_the_fresh_picture():
    r = row(reference=view("cpplus_pan_before"))
    old = r.reference
    assert await vc.apply_new_view(r, view("cpplus_pan_before"), old=old, zoom_changed=False, reason="", by=None) is None
    assert r.analytics_config == CFG and r.analytics_review is None and r.previous_reference is None
    assert r.reference is not None


@needs_frames
@pytest.mark.asyncio
async def test_a_preset_re_saved_far_off_is_paused_for_review():
    before = (FIX / "cpplus_pan_before.jpg").read_bytes()
    r = row(reference=before)
    rec = await vc.apply_new_view(r, (FIX / "cpplus_pan_after.jpg").read_bytes(), old=before, zoom_changed=False,
                                  reason="re-saved", by="ana")
    assert rec["state"] == "review" and vc.paused(r.analytics_review)
    assert rec["offset"][0] == pytest.approx(-0.24, abs=0.02)             # panned right: the scene slid left


@needs_frames
@pytest.mark.asyncio
async def test_re_stored_outside_vms_waits_for_a_picture_then_lines_up():
    r = row(reference=view("ipro_pan_before"), thumbnail=b"thumb")
    vc.mark_moved_outside(r)
    assert r.analytics_review["state"] == "awaiting_picture" and vc.paused(r.analytics_review)
    assert r.reference is None and r.thumbnail is None and r.previous_reference is not None   # arrivals can confirm
    handled, rec = await vc.after_first_picture(r, view("ipro_pan_before", -0.04, 0.0))
    assert handled and rec["state"] == "shifted" and r.reference is not None
    assert r.analytics_config["regions"]["region_1"]["points"][0][0] == pytest.approx(0.26, abs=0.01)
    assert await vc.after_first_picture(r, b"x") == (False, None)        # only once


@needs_frames
@pytest.mark.asyncio
async def test_re_stored_outside_vms_to_the_same_view_simply_runs_again():
    r = row(reference=view("ipro_pan_before"))
    vc.mark_moved_outside(r)
    handled, rec = await vc.after_first_picture(r, view("ipro_pan_before"))
    assert handled and rec is None and r.analytics_review is None


# ── delete → archive → copy ──────────────────────────────────────────────────

@needs_frames
@pytest.mark.asyncio
async def test_a_deleted_presets_zones_are_archived_and_copied_onto_another_for_review():
    assert vc.archive_row(row(cfg=None), "ana", "preset deleted") is None
    gone = row(reference=view("cpplus_tilt_after"))
    a = vc.archive_row(gone, "ana", "preset deleted")
    assert a.analytics_config == CFG and a.picture == gone.reference and a.preset_id == "p1"
    own = {"regions": {}, "activities": []}
    target = row(id="p2", token="1", name="Gate again", cfg=own, reference=view("cpplus_tilt_after", 0.03, 0.0))
    rec = await vc.copy_from_archive(target, a, "ana")
    assert rec["state"] == "review" and "Gate" in rec["note"] and rec["previous_config"] == own
    assert target.analytics_config["regions"]["region_1"]["points"][0][0] == pytest.approx(0.33, abs=0.01)
