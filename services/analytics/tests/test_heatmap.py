"""The heat accumulator: what it adds, where, and what it renders.

The pixel arithmetic of the supplied DeepStream probe, pinned here on the
implementation that replaced its per-pixel Python loops:

  * heat lands at the BOTTOM CENTRE of a box, in a disc of `radius`;
  * `intensity` is added per DETECTION per frame, so standing still keeps
    adding — that is the dwell signal, and it is why nothing de-duplicates;
  * nothing decays, so a session's matrix only grows;
  * the picture is blur → clip at 255 (not normalised) → JET → blend.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys
import time

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.heatmap import (BLUR_PX, CEILING, HeatAccumulator, Snapshot,  # noqa: E402
                               disc)

W, H = 1920, 1080


def acc(**kw) -> HeatAccumulator:
    return HeatAccumulator(kw.pop("width", W), kw.pop("height", H), **kw)


def frame(value: int = 60) -> np.ndarray:
    return np.full((H, W, 3), value, np.uint8)


def spread(img: np.ndarray) -> float:
    """How much the picture varies ACROSS the frame, per colour channel."""
    return float(img.reshape(-1, img.shape[-1]).std(axis=0).max())


class TestTheStamp:
    def test_it_is_a_circle_of_the_asked_radius(self):
        s = disc(4, 1.0)
        assert s.shape == (9, 9)
        assert s[4, 4] == 1.0 and s[4, 0] == 1.0          # centre and the rim
        assert s[0, 0] == 0.0                              # the corner is outside
        assert s.max() == 1.0

    def test_intensity_is_the_value_inside(self):
        assert disc(3, 2.5).max() == pytest.approx(2.5)

    def test_a_tiny_radius_still_has_a_pixel(self):
        assert disc(0.1, 1.0).sum() > 0


class TestWhereHeatLands:
    def test_it_lands_at_the_point_it_was_given(self):
        a = acc(radius_px=40, intensity=1.0)
        a.add(960, 540)
        ys, xs = np.nonzero(a.matrix)                      # the disc is flat: use its centre
        assert xs.mean() == pytest.approx(960 / a.scale, abs=1)
        assert ys.mean() == pytest.approx(540 / a.scale, abs=1)

    def test_nothing_lands_anywhere_else(self):
        a = acc(radius_px=20, intensity=1.0)
        a.add(300, 300)
        assert a.matrix[0, 0] == 0.0
        assert a.matrix[-1, -1] == 0.0

    def test_a_point_at_the_edge_keeps_the_half_that_is_on_the_frame(self):
        a = acc(radius_px=40, intensity=1.0)
        a.add(0, 540)
        assert a.points == 1 and a.matrix.max() == pytest.approx(1.0)

    def test_a_point_off_the_frame_adds_nothing(self):
        a = acc(radius_px=10, intensity=1.0)
        a.add(-500, -500)
        assert a.matrix.max() == 0.0 and a.points == 0

    def test_the_radius_is_in_frame_pixels(self):
        small, big = acc(radius_px=20, intensity=1.0), acc(radius_px=80, intensity=1.0)
        small.add(960, 540)
        big.add(960, 540)
        assert big.matrix.sum() > small.matrix.sum() * 8


class TestAccumulation:
    def test_every_frame_adds_again_so_standing_still_is_dwell(self):
        a = acc(radius_px=30, intensity=1.0)
        for _ in range(10):
            a.observe([(500, 700)])
        assert a.peak == pytest.approx(10.0)
        assert a.points == 10 and a.frames == 10

    def test_two_detections_in_one_frame_both_count(self):
        a = acc(radius_px=10, intensity=1.0)
        added = a.observe([(200, 300), (900, 800)])
        assert added == 2 and a.points == 2

    def test_overlapping_detections_add_up(self):
        a = acc(radius_px=40, intensity=1.0)
        a.observe([(500, 500), (505, 505)])
        assert a.peak == pytest.approx(2.0)

    def test_nothing_decays(self):
        a = acc(radius_px=20, intensity=1.0)
        a.observe([(400, 400)])
        peak = a.peak
        for _ in range(50):
            a.observe([])                                  # 50 empty frames
        assert a.peak == pytest.approx(peak)
        assert a.frames == 51

    def test_intensity_scales_what_one_frame_adds(self):
        one, four = acc(radius_px=20, intensity=1.0), acc(radius_px=20, intensity=4.0)
        one.observe([(600, 600)])
        four.observe([(600, 600)])
        assert four.peak == pytest.approx(one.peak * 4)

    def test_clearing_starts_the_next_session_empty(self):
        a = acc(radius_px=20, intensity=1.0)
        a.observe([(600, 600)])
        a.clear()
        assert a.peak == 0.0 and a.points == 0 and a.frames == 0


class TestWhenTheAccumulatorMustBeReplaced:
    def test_the_same_settings_and_frame_size_keep_it(self):
        a = acc(radius_px=40, intensity=1.0)
        assert a.matches(W, H, 40, 1.0)

    def test_a_different_frame_size_does_not(self):
        a = acc(radius_px=40, intensity=1.0)
        assert not a.matches(1280, 720, 40, 1.0)

    def test_changed_settings_do_not(self):
        a = acc(radius_px=40, intensity=1.0)
        assert not a.matches(W, H, 50, 1.0)
        assert not a.matches(W, H, 40, 2.0)


class TestTheRenderedOverlay:
    def test_it_is_the_frame_size_down_to_the_output_width(self):
        a = acc(radius_px=40, intensity=1.0)
        out = a.render(frame(), output_width=1280)
        assert out.shape == (720, 1280, 3)

    def test_a_smaller_frame_is_not_upscaled(self):
        a = HeatAccumulator(640, 360, radius_px=20)
        out = a.render(np.full((360, 640, 3), 60, np.uint8), output_width=1280)
        assert out.shape == (360, 640, 3)

    def test_heat_shows_where_people_were(self):
        a = acc(radius_px=60, intensity=40.0)
        for _ in range(6):
            a.observe([(400, 800)])
        out = a.render(frame(), output_width=1280)
        hot = out[int(800 * 720 / H), int(400 * 1280 / W)]
        cold = out[50, 1200]
        assert int(hot[2]) > int(cold[2]) + 40             # red channel, JET's hot end

    def test_an_empty_session_renders_the_frame_under_an_even_wash(self):
        a = acc(radius_px=40, intensity=1.0)
        out = a.render(frame(), output_width=1280)
        # JET colours zero heat dark blue, so the wash is even, not absent.
        assert spread(out) < 2 and out[..., 0].mean() > out[..., 2].mean()

    def test_clipping_not_normalising_so_two_busy_sessions_look_alike(self):
        busy, busier = acc(radius_px=40, intensity=10.0), acc(radius_px=40, intensity=10.0)
        for _ in range(60):
            busy.observe([(900, 500)])
        for _ in range(600):
            busier.observe([(900, 500)])
        a = busy.heat_image(640, 360)
        b = busier.heat_image(640, 360)
        assert a[180, 300].tolist() == b[180, 300].tolist()  # both saturated

    def test_the_blend_keeps_the_frame_visible(self):
        a = acc(radius_px=10, intensity=1.0)
        dark, light = a.render(frame(0), output_width=640), a.render(frame(255), output_width=640)
        assert light.mean() > dark.mean() + 50


class TestSnapshots:
    def test_it_encodes_a_jpeg_that_decodes_to_the_overlay(self):
        a = acc(radius_px=40, intensity=5.0)
        a.observe([(600, 700)])
        snap = a.snapshot(frame(), output_width=1280)
        assert isinstance(snap, Snapshot)
        decoded = cv2.imdecode(np.frombuffer(snap.jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert decoded.shape == (snap.height, snap.width, 3) == (720, 1280, 3)
        assert snap.points == 1 and snap.peak == pytest.approx(5.0)

    def test_it_carries_the_time_it_was_taken(self):
        a = acc(radius_px=20, intensity=1.0)
        snap = a.snapshot(frame(), captured_at=1789800000.5)
        assert snap.captured_at == 1789800000.5

    def test_no_frame_means_no_snapshot(self):
        a = acc(radius_px=20, intensity=1.0)
        assert a.snapshot(None) is None
        assert a.snapshot(np.zeros((0, 0, 3), np.uint8)) is None

    def test_the_artifact_is_only_the_overlay_no_text_burnt_in(self):
        # Presentation metadata belongs to the artifact/UI layer, so a snapshot
        # of an empty session over a flat frame stays flat.
        a = acc(radius_px=20, intensity=1.0)
        snap = a.snapshot(frame(120), output_width=640)
        decoded = cv2.imdecode(np.frombuffer(snap.jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert spread(decoded) < 3


class TestCostShape:
    def test_the_matrix_is_a_sixteenth_of_the_frame(self):
        a = acc(radius_px=40, intensity=1.0)
        assert a.shape == (H // 4, W // 4)
        assert a.matrix.nbytes < 600_000

    def test_the_default_blur_and_ceiling_are_the_supplied_ones(self):
        assert BLUR_PX == 31 and CEILING == 255.0


class TestWhatItCosts:
    """The design's whole claim is that the per-frame half is nearly free and
    the expensive half happens once a minute. Budgets here are deliberately
    loose — measured on the analytics image they are ~0.012 ms and ~11 ms — so
    these fail on a change of shape (a per-frame blur, a full-resolution
    matrix), not on a busy machine.
    """

    POINTS = [(300.0, 400.0), (900.0, 500.0), (1500.0, 800.0), (1000.0, 1000.0)]

    def _per_frame_ms(self, a, frames=300):
        a.observe(self.POINTS)                     # the stamp is cached on first use
        t = time.perf_counter()
        for _ in range(frames):
            a.observe(self.POINTS)
        return (time.perf_counter() - t) * 1000.0 / frames

    def _render_ms(self, a, img, times=5):
        a.snapshot(img)
        t = time.perf_counter()
        for _ in range(times):
            a.snapshot(img)
        return (time.perf_counter() - t) * 1000.0 / times

    def test_a_frame_of_detections_costs_a_fraction_of_a_millisecond(self):
        assert self._per_frame_ms(acc(radius_px=40, intensity=1.0)) < 1.0

    def test_the_picture_is_the_expensive_part_and_it_happens_once_a_minute(self):
        a = acc(radius_px=40, intensity=1.0)
        per_frame = self._per_frame_ms(a)
        render = self._render_ms(a, frame())
        assert render > 20 * per_frame              # which is why it is not per frame
        assert render < 250.0
        # A minute at 5 FPS: 300 accumulations and ONE render.
        assert per_frame * 300 + render < 1_000.0   # under a second per camera-minute

    def test_it_does_not_grow_a_new_array_per_frame(self):
        a = acc(radius_px=40, intensity=1.0)
        before = a.matrix.__array_interface__["data"][0]
        for _ in range(50):
            a.observe(self.POINTS)
        assert a.matrix.__array_interface__["data"][0] == before
        assert a.matrix.nbytes < 600_000           # still the one small matrix
