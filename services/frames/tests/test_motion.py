"""The canonical motion implementation.

WHAT THESE PROTECT. This module replaced two implementations, and the whole
value of that is that a tuning change now reaches both consumers. The risk is
the mirror image: a change made for one consumer silently altering the other.
So the tests come in two halves — the scalar the Motion service thresholds
against, and the regions SmartSearch crops from — plus the property that ties
them together, which is that they describe the same frame.

Region parity against the gate this replaced is NOT tested here; it is proved
against the real shipped gate on real frames by scripts/parity.py, which is
the Phase 1 exit criterion. Duplicating it with a reimplementation would be
testing a copy against a copy.

Run: python3 -m pytest tests -q   (from services/frames)
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.motion import (FULL_FRAME, NONE, REGIONS,           # noqa: E402
                           CanonicalMotion, MotionParams, merge_boxes)

W, H = 640, 360


def blank(value: int = 30) -> np.ndarray:
    return np.full((H, W, 3), value, np.uint8)


def with_box(x: int, y: int, w: int = 60, h: int = 120,
             value: int = 30, fill: int = 230) -> np.ndarray:
    f = blank(value)
    cv2.rectangle(f, (x, y), (x + w, y + h), (fill, fill, fill), -1)
    return f


def settled(m: CanonicalMotion, frame: np.ndarray):
    """Feed a frame twice so the baseline exists and the second call is a real
    difference — the first frame after connect is always FULL_FRAME."""
    m.analyse(frame)
    return m.analyse(frame)


# ── the first frame ──────────────────────────────────────────────────────────
def test_the_first_frame_reports_full_frame_not_silence():
    """A camera that comes up with someone already in shot must not stay
    invisible until they happen to move."""
    m = CanonicalMotion()
    r = m.analyse(blank())
    assert r.verdict == FULL_FRAME
    assert r.regions == []


def test_the_first_frame_says_its_baseline_is_not_valid():
    """Consumers must be able to tell 'nothing moved' from 'we cannot know
    yet'. Without this flag a reconnect looks like a quiet scene."""
    m = CanonicalMotion()
    assert m.analyse(blank()).baseline_valid is False
    assert m.analyse(blank()).baseline_valid is True


def test_reset_restores_the_unknown_state():
    m = CanonicalMotion()
    m.analyse(blank()); m.analyse(blank())
    m.reset()
    assert m.analyse(blank()).baseline_valid is False


# ── the region half ──────────────────────────────────────────────────────────
def test_a_still_scene_produces_no_regions():
    m = CanonicalMotion()
    r = settled(m, blank())
    assert r.verdict == NONE
    assert r.regions is None


def test_a_moving_object_produces_a_region_containing_it():
    m = CanonicalMotion()
    m.analyse(with_box(100, 100))
    r = m.analyse(with_box(260, 100))
    assert r.verdict == REGIONS
    assert r.regions
    # The object's new position must be inside one of the boxes, or the crop
    # handed to the detector would not contain what triggered it.
    assert any(x1 <= 290 <= x2 and y1 <= 160 <= y2
               for (x1, y1, x2, y2) in r.regions)


def test_regions_are_in_FULL_frame_coordinates():
    """Analysis happens at 320 wide; the detector crops the original. Getting
    this wrong does not crash — it stores a plausible box in the wrong place."""
    m = CanonicalMotion()
    m.analyse(with_box(100, 100))
    r = m.analyse(with_box(300, 150))
    assert r.regions
    for (x1, y1, x2, y2) in r.regions:
        assert 0 <= x1 < x2 <= W and 0 <= y1 < y2 <= H
        assert x2 > 320 or y2 > 180 or x1 > 0    # not left in analysis space


def test_a_scene_change_asks_for_the_whole_frame():
    """Lights, IR cut, auto-exposure, camera moved: everything 'moved', so
    regions are meaningless."""
    m = CanonicalMotion()
    m.analyse(blank(20))
    r = m.analyse(blank(220))
    assert r.verdict == FULL_FRAME
    assert "scene change" in r.reason


def test_too_many_regions_fall_back_to_the_whole_frame():
    """One inference beats N — the detector resizes any input to its own
    resolution, so three crops cost three inferences."""
    m = CanonicalMotion(MotionParams(max_regions=2))
    m.analyse(blank())
    f = blank()
    for i in range(6):
        cv2.rectangle(f, (40 + i * 95, 60), (75 + i * 95, 150), (240, 240, 240), -1)
    r = m.analyse(f)
    assert r.verdict == FULL_FRAME


def test_despeckle_removes_isolated_sensor_pixels():
    """Speckle that survives the threshold becomes real regions once dilated,
    and a frame full of them trips max_regions into a full-frame pass — the
    exact inference the region path exists to avoid."""
    rng = np.random.default_rng(3)
    base = blank()
    speckled = base.copy()
    ys, xs = rng.integers(0, H, 500), rng.integers(0, W, 500)
    speckled[ys, xs] = 255

    on = CanonicalMotion(MotionParams(despeckle_iterations=1))
    off = CanonicalMotion(MotionParams(despeckle_iterations=0))
    on.analyse(base); off.analyse(base)
    r_on, r_off = on.analyse(speckled), off.analyse(speckled)
    n_on = 0 if r_on.regions is None else len(r_on.regions)
    n_off = 0 if r_off.regions is None else len(r_off.regions)
    assert n_on < n_off or r_on.verdict == NONE


def test_despeckle_keeps_a_small_distant_object():
    """The half that matters. Small-object recall is the thing this may never
    regress — a distant figure is exactly what a whole-frame threshold loses."""
    m = CanonicalMotion(MotionParams(despeckle_iterations=1))
    m.analyse(blank())
    r = m.analyse(with_box(300, 150, w=14, h=30))
    assert r.verdict == REGIONS, "a small distant object was despeckled away"


# ── the scalar half ──────────────────────────────────────────────────────────
def test_the_scalar_is_zero_on_a_still_scene():
    m = CanonicalMotion()
    assert settled(m, blank()).fraction == pytest.approx(0.0, abs=1e-6)


def test_the_scalar_rises_with_the_amount_that_moved():
    small = CanonicalMotion(); big = CanonicalMotion()
    small.analyse(blank()); big.analyse(blank())
    r_small = small.analyse(with_box(100, 100, w=40, h=60))
    r_big = big.analyse(with_box(100, 60, w=400, h=260))
    assert r_big.fraction > r_small.fraction > 0


def test_the_noise_floor_suppresses_sub_threshold_change():
    """The floor earns its place on the scalar path even though it would be a
    no-op against the binary threshold used for regions."""
    base = blank(100)
    nudged = blank(104)                      # +4, under a floor of 10
    loose = CanonicalMotion(MotionParams(noise_floor=0))
    tight = CanonicalMotion(MotionParams(noise_floor=10))
    loose.analyse(base); tight.analyse(base)
    assert loose.analyse(nudged).fraction > 0
    assert tight.analyse(nudged).fraction == pytest.approx(0.0, abs=1e-6)


# ── the property that justifies merging the two ──────────────────────────────
def test_both_outputs_describe_the_same_frame():
    """The entire reason this module exists. A frame with real movement must
    not report 'nothing moved' to one consumer and regions to the other."""
    m = CanonicalMotion()
    m.analyse(with_box(80, 80))
    r = m.analyse(with_box(320, 140))
    assert r.verdict == REGIONS
    assert r.fraction > 0, "regions were emitted while the scalar said nothing"


def test_one_pass_serves_both_and_counters_reconcile():
    m = CanonicalMotion()
    frames = [blank(), with_box(60, 60), with_box(220, 60), blank(), blank()]
    for f in frames:
        m.analyse(f)
    s = m.snapshot()
    assert s["frames_seen"] == len(frames)
    assert (s["frames_none"] + s["frames_full_frame"]
            + s["frames_regions"]) == len(frames)


# ── helpers ──────────────────────────────────────────────────────────────────
def test_overlapping_boxes_merge_so_one_object_is_not_cropped_twice():
    merged = merge_boxes([(0, 0, 50, 50), (40, 40, 90, 90), (200, 200, 240, 240)])
    assert len(merged) == 2
    assert (0, 0, 90, 90) in merged


def test_disjoint_boxes_are_left_alone():
    boxes = [(0, 0, 20, 20), (100, 100, 130, 130)]
    assert sorted(merge_boxes(list(boxes))) == sorted(boxes)


def test_the_result_serialises_for_the_notice():
    """It crosses a process boundary as JSON, so it must survive the trip."""
    import json
    m = CanonicalMotion()
    m.analyse(with_box(60, 60))
    d = m.analyse(with_box(240, 60)).to_dict()
    round_tripped = json.loads(json.dumps(d))
    assert set(round_tripped) == {"fraction", "regions", "verdict", "reason",
                                  "baseline_valid"}
