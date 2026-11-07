"""Click-to-centre calibration (services/ptz_calibrate) against a SIMULATED PTZ head.

The simulated camera looks at a textured 'world' panorama: pan/tilt pick where the view is centred, zoom narrows
the field of view (1x .. 20x, geometric — like a real zoom lens), and each frame is that crop as a JPEG. So the
calibration measures it exactly the way it measures a real camera, and the tests can check the thing the
operator cares about: after a click, is the clicked spot in the middle of the picture?
"""
from __future__ import annotations

import asyncio
import io
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageFilter  # noqa: E402

from backend.services import ptz_calibrate as cal  # noqa: E402

WORLD_W, WORLD_H = 4000, 1200      # the whole pan circle (-1..1) × the tilt range (-1..1)
WIDE_FOV_W = 0.25                  # at zoom 0 the frame shows a quarter of the pan range ...
ASPECT = 16 / 9
MAX_MAG = 20.0


def _world() -> Image.Image:
    noise = Image.effect_noise((WORLD_W // 8, WORLD_H // 8), 90).resize((WORLD_W, WORLD_H), Image.BICUBIC)
    return noise.filter(ImageFilter.GaussianBlur(1.5)).convert("L")


WORLD = _world()


class SimCam:
    """pan/tilt/zoom in ONVIF generic units; `inverted` = a ceiling-mounted head (picture moves the other way)."""

    def __init__(self, pan=0.1, tilt=0.0, zoom=0.0, inverted=False, absolute=True):
        self.pan, self.tilt, self.zoom, self.inverted, self.absolute = pan, tilt, zoom, inverted, absolute
        self.moves = 0

    def mag(self, z=None):
        z = self.zoom if z is None else z
        return MAX_MAG ** z

    def fov(self):
        w = WIDE_FOV_W / self.mag()              # fraction of the pan circle (2 units)
        return w, w * (WORLD_W / WORLD_H) / ASPECT * 0.5  # height as a fraction of the tilt range, in same scale

    def centre_px(self):
        x = (self.pan + 1) / 2 * WORLD_W
        y = (1 - (self.tilt + 1) / 2) * WORLD_H
        return x, y

    def frame(self) -> bytes:
        fw, _ = self.fov()
        w_px = fw * WORLD_W
        h_px = w_px / ASPECT
        x, y = self.centre_px()
        box = (int(x - w_px / 2), int(y - h_px / 2), int(x + w_px / 2), int(y + h_px / 2))
        im = WORLD.crop(box).resize((640, 360))
        if self.inverted:
            im = im.rotate(180)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()

    def world_point(self, x, y):
        """World pixel under a click at (x, y) in -1..1 (+y up) of the current frame."""
        fw, _ = self.fov()
        w_px = fw * WORLD_W
        h_px = w_px / ASPECT
        if self.inverted:
            x, y = -x, -y
        cx, cy = self.centre_px()
        return cx + x * w_px / 2, cy - y * h_px / 2

    def hw(self):
        async def status():
            return {"pan": self.pan, "tilt": self.tilt, "zoom": self.zoom, "moving_pan_tilt": "IDLE", "moving_zoom": "IDLE"}

        async def move_abs(pan=None, tilt=None, zoom=None):
            self.moves += 1
            if pan is not None:
                self.pan = cal._wrap_pan(pan)
            if tilt is not None:
                self.tilt = max(-1, min(1, tilt))
            if zoom is not None:
                self.zoom = max(0, min(1, zoom))

        async def move_rel(pan=None, tilt=None):
            # An upside-down mount turns the same way as any head; only its PICTURE is rotated, so a pan right
            # slides the picture right instead of left.
            self.moves += 1
            if pan:
                self.pan = cal._wrap_pan(self.pan + pan)
            if tilt:
                self.tilt = max(-1, min(1, self.tilt + tilt))

        async def frame():
            return self.frame()

        async def nosleep(_s):
            return None

        return cal.Hw(status, move_abs, move_rel, frame, sleep=nosleep)


def _run(coro):
    return asyncio.run(coro)


def test_measure_shift_follows_the_camera_motion():
    cam = SimCam(pan=0.0, tilt=0.0)
    a = cam.frame()
    cam.pan += WIDE_FOV_W * 2 * 0.2              # pan right by a fifth of the view
    cam.tilt += 0.02
    b = cam.frame()
    sx, sy, conf = cal.measure_shift(a, b)
    assert conf > 0.5
    assert sx == pytest.approx(0.2, abs=0.02)    # scene slid left by a fifth = camera moved right
    assert sy > 0                                 # tilting up = positive


def test_featureless_frames_give_no_confident_answer():
    flat = Image.new("L", (640, 360), 128)
    buf = io.BytesIO()
    flat.save(buf, "JPEG")
    assert cal.measure_shift(buf.getvalue(), buf.getvalue())[2] < cal._MIN_CONFIDENCE


def _centre_error(cam: SimCam, x: float, y: float, zoom_factor=None) -> float:
    """Click (x, y), apply the planned move, return how far (in frame widths) the clicked spot is from centre."""
    target = cam.world_point(x, y)
    caps = {"absolute": {"pan_tilt": cam.absolute, "zoom": cam.absolute}}
    status = {"pan": cam.pan, "tilt": cam.tilt, "zoom": cam.zoom}
    plan = cal.plan_centre(cam_cal, status, x, y, zoom_factor, caps)
    hw = cam.hw()
    if plan["mode"] == "absolute":
        _run(hw.move_abs(pan=plan["pan"], tilt=plan["tilt"], zoom=plan.get("zoom")))
    else:
        _run(hw.move_rel(pan=plan["pan"], tilt=plan["tilt"]))
    cx, cy = cam.centre_px()
    fw, _ = cam.fov()
    return math.hypot(target[0] - cx, target[1] - cy) / (fw * WORLD_W)


cam_cal = None


@pytest.fixture(scope="module")
def calibrated():
    global cam_cal
    cam = SimCam(pan=0.1, tilt=0.05, zoom=0.3)
    doc = _run(cal.run(cam.hw(), {"absolute": {"pan_tilt": True, "zoom": True}}))
    cam_cal = doc
    return doc, cam


def test_calibration_measures_every_zoom_level_and_puts_the_camera_back(calibrated):
    doc, cam = calibrated
    assert doc["space"] == "absolute" and len(doc["points"]) == len(cal.ZOOM_LEVELS)
    assert (cam.pan, cam.tilt, cam.zoom) == pytest.approx((0.1, 0.05, 0.3))
    # pan per frame width at zoom 0 = the view's share of the pan circle (in -1..1 units)
    assert doc["points"][0]["pan_per_width"] == pytest.approx(WIDE_FOV_W * 2, rel=0.08)
    # and the magnification it implies matches the lens
    assert cal.magnification(doc, 1.0) == pytest.approx(MAX_MAG, rel=0.15)


@pytest.mark.parametrize("zoom", [0.0, 0.4, 0.85])
@pytest.mark.parametrize("click", [(0.8, 0.0), (-0.6, 0.5), (0.3, -0.7)])
def test_a_click_ends_up_in_the_centre_at_any_zoom(calibrated, zoom, click):
    cam = SimCam(pan=-0.2, tilt=0.1, zoom=zoom)
    assert _centre_error(cam, *click) < 0.04       # within 4 % of the frame width of dead centre


def test_old_fixed_gain_misses_when_zoomed_in():
    """The bug: uncalibrated (old behaviour) at zoom 0.85 a click near the edge flies far off."""
    global cam_cal
    saved, cam_cal = cam_cal, None
    try:
        cam = SimCam(pan=-0.2, tilt=0.1, zoom=0.85, absolute=False)
        assert _centre_error(cam, 0.8, 0.0) > 1.0  # more than a whole frame width off
    finally:
        cam_cal = saved


def test_drag_to_zoom_fills_the_frame_with_the_box(calibrated):
    cam = SimCam(pan=0.3, tilt=0.0, zoom=0.2)
    before = cam.mag()
    err = _centre_error(cam, -0.4, 0.3, zoom_factor=3.0)
    assert err < 0.05
    assert cam.mag() / before == pytest.approx(3.0, rel=0.15)


def test_inverted_ceiling_mount_still_centres():
    cam = SimCam(pan=0.0, tilt=0.0, zoom=0.0, inverted=True, absolute=False)
    doc = _run(cal.run(cam.hw(), {"absolute": {"pan_tilt": False, "zoom": False}}))
    assert doc["space"] == "relative" and doc["points"][0]["pan_per_width"] < 0
    global cam_cal
    saved, cam_cal = cam_cal, doc
    try:
        assert _centre_error(cam, 0.7, -0.4) < 0.04
    finally:
        cam_cal = saved


def test_camera_without_position_cannot_be_calibrated():
    hw = SimCam().hw()

    async def no_status():
        return None
    hw.status = no_status
    with pytest.raises(RuntimeError):
        _run(cal.run(hw, {"absolute": {"pan_tilt": True}}))


def test_right_click_zooms_out_by_the_factor(calibrated):
    cam = SimCam(pan=0.0, tilt=0.0, zoom=0.8)
    before = cam.mag()
    _centre_error(cam, 0.0, 0.0, zoom_factor=0.5)
    assert cam.mag() / before == pytest.approx(0.5, rel=0.15)


def test_a_blurry_full_zoom_level_is_extrapolated_not_frozen(calibrated):
    doc, _ = calibrated
    partial = {**doc, "points": [p for p in doc["points"] if p["zoom"] <= 0.75]}   # 1.0 couldn't be measured
    global cam_cal
    saved, cam_cal = cam_cal, partial
    try:
        assert _centre_error(SimCam(pan=0.1, tilt=0.0, zoom=0.95), 0.7, 0.3) < 0.06
    finally:
        cam_cal = saved


def test_a_camera_that_refuses_moves_outside_its_limits_still_calibrates():
    """alpha, i-PRO WV-X6533L, 2026-10-26: tilt limited to just above the horizon; a test move past it was
    answered "Destination out of bounds" and the WHOLE calibration failed. Now: moves stay inside the reported
    range, tilt goes toward the side with room, a refused move is retried the other way."""
    cam = SimCam(pan=0.38, tilt=-0.02, zoom=0.0)
    hw = cam.hw()
    real = hw.move_abs

    async def strict(pan=None, tilt=None, zoom=None):
        if tilt is not None and not (-1.0 <= tilt <= 0.05):
            raise RuntimeError("Unknown error: Destination out of bounds")
        return await real(pan=pan, tilt=tilt, zoom=zoom)
    hw.move_abs = strict
    # with the ranges reported
    doc = _run(cal.run(hw, {"absolute": {"pan_tilt": True, "zoom": True},
                            "ranges": {"pan": [-1, 1], "tilt": [-1, 0.05], "zoom": [0, 1]}}))
    assert len(doc["points"]) == len(cal.ZOOM_LEVELS) and doc["points"][0]["tilt_per_height"] > 0
    # and without them (an old probe): the refused move is retried downward
    cam2 = SimCam(pan=0.38, tilt=-0.02, zoom=0.0)
    hw2 = cam2.hw()
    real2 = hw2.move_abs

    async def strict2(pan=None, tilt=None, zoom=None):
        if tilt is not None and tilt > 0.01:
            raise RuntimeError("Unknown error: Destination out of bounds")
        return await real2(pan=pan, tilt=tilt, zoom=zoom)
    hw2.move_abs = strict2
    notes: list = []
    doc2 = _run(cal.run(hw2, {"absolute": {"pan_tilt": True, "zoom": True}}, notes=notes))
    assert doc2["points"] and (cam2.pan, cam2.tilt) == pytest.approx((0.38, -0.02))


def test_a_failed_calibration_says_what_it_saw():
    cam = SimCam()
    hw = cam.hw()

    async def blank():
        im = Image.new("L", (640, 360), 30)
        buf = io.BytesIO()
        im.save(buf, "JPEG")
        return buf.getvalue()
    hw.frame = blank
    with pytest.raises(RuntimeError) as e:
        _run(cal.run(hw, {"absolute": {"pan_tilt": True, "zoom": True}}))
    assert "match" in str(e.value)               # e.g. "pan step +0.017: picture moved +0.000, match 0.00"


class LaggingCam(SimCam):
    """A stream that shows where the head was a few grabs ago — the HLS live edge on alpha's i-PRO at night."""

    def __init__(self, lag=2, **kw):
        super().__init__(**kw)
        self.lag, self.seen = lag, []

    def hw(self):
        hw = super().hw()
        live = hw.frame

        async def frame():
            self.seen.append(await live())
            return self.seen[max(0, len(self.seen) - 1 - self.lag)]

        hw.frame = frame
        return hw


def test_a_lagging_stream_never_produces_a_wrong_calibration():
    """alpha, 2026-10-26: one stale frame per move was believed, the stored tilt had the wrong sign, every click went
    the wrong way. Each measurement now waits for a still picture and must slide back the same amount when the head
    returns — so a lagging stream gives the RIGHT numbers (or none), never wrong ones."""
    cam = LaggingCam(lag=2, pan=0.1, tilt=0.05, zoom=0.3)
    doc = _run(cal.run(cam.hw(), {"absolute": {"pan_tilt": True, "zoom": True}}))
    assert doc["points"]
    for p in doc["points"]:
        mag = MAX_MAG ** p["zoom_n"]
        assert p["pan_per_width"] == pytest.approx(WIDE_FOV_W * 2 / mag, rel=0.15)
        assert p["tilt_per_height"] > 0                                  # the right direction, every level


def test_a_picture_that_never_matches_gives_no_calibration():
    cam = SimCam()
    hw = cam.hw()

    async def noise():
        im = Image.effect_noise((640, 360), 120).convert("L")
        buf = io.BytesIO()
        im.save(buf, "JPEG")
        return buf.getvalue()

    hw.frame = noise
    with pytest.raises(RuntimeError):
        _run(cal.run(hw, {"absolute": {"pan_tilt": True, "zoom": True}}, zoom_levels=(0.0, 0.5)))


def test_levels_that_contradict_the_rest_are_dropped():
    notes: list = []
    pts = [{"zoom": 0, "zoom_n": 0.0, "pan_per_width": 0.5, "tilt_per_height": 0.3},
           {"zoom": 0.05, "zoom_n": 0.5, "pan_per_width": 0.2, "tilt_per_height": -0.1},     # tilt flipped
           {"zoom": 0.07, "zoom_n": 0.75, "pan_per_width": 0.4, "tilt_per_height": 0.2},     # wider than zoom 0 kept? no: than last kept
           {"zoom": 0.09, "zoom_n": 1.0, "pan_per_width": 0.05, "tilt_per_height": 0.03}]
    kept = cal._consistent(pts, notes)
    assert [p["zoom"] for p in kept] == [0, 0.07, 0.09]
    assert any("tilt direction flipped" in n for n in notes)
    notes2: list = []
    kept2 = cal._consistent([pts[0], {**pts[3], "zoom_n": 0.5}, {**pts[2], "zoom_n": 1.0}], notes2)
    assert [p["zoom"] for p in kept2] == [0, 0.09] and "wider" in notes2[0]
