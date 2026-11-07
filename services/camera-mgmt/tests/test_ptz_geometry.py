"""Click-to-centre with real angles, the after-click check, and the picture comparison on REAL camera frames.

test_ptz_calibrate's SimCam slides a flat picture — pan moves it the same amount at every tilt, so it can't show
the two things that made clicks miss on real domes: looking steeply down, a pan move sweeps the picture far less
(∝ cos elevation); and a wide lens isn't linear toward its edges. `DomeCam` here renders what a real dome sees:
a spherical panorama through a rectilinear lens, at a pan/tilt/zoom given in ONVIF-style units whose degrees and
horizon the code under test is NOT told — calibration has to measure them, like on a real camera.
"""
from __future__ import annotations

import asyncio
import io
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageFilter  # noqa: E402

from backend.services import ptz_calibrate as cal  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "ptz"
# Real frames from a customer site's PTZ camera; the public tree leaves them out.
needs_frames = pytest.mark.skipif(not FIXTURES.is_dir(), reason="tests/fixtures/ptz is not in this tree")

PANO_W, PANO_H = 8192, 4096          # 360° × 180°
OUT_W, OUT_H = 480, 270
ASPECT = OUT_W / OUT_H
WIDE_HFOV = 60.0                     # degrees at zoom 0
MAX_MAG = 20.0
KP = 180.0                           # degrees per pan unit (-1..1 = the full circle)
KT = 100.0                           # degrees per tilt unit — NOT what anyone would guess
T0 = 0.1                             # the tilt value that looks at the horizon
TILT_RANGE = (-0.7, 0.25)            # -80° .. +15°
CAPS = {"absolute": {"pan_tilt": True, "zoom": True},
        "ranges": {"pan": [-1, 1], "tilt": list(TILT_RANGE), "zoom": [0, 1]}}


def _noise(w: int, h: int, seed: int) -> Image.Image:
    # seeded (PIL's effect_noise isn't): the same world every run, so the measured angles never flake
    return Image.frombytes("L", (w, h), random.Random(seed).randbytes(w * h))


def _pano() -> Image.Image:
    coarse = _noise(PANO_W // 16, PANO_H // 16, 1).resize((PANO_W, PANO_H), Image.BICUBIC)
    fine = _noise(PANO_W // 4, PANO_H // 4, 2).resize((PANO_W, PANO_H), Image.BICUBIC)
    return Image.blend(coarse, fine, 0.5).filter(ImageFilter.GaussianBlur(1.2))


PANO = _pano()


def _basis(pan: float, tilt: float):
    th, ph = math.radians(pan * KP), math.radians(KT * (tilt - T0))
    f = (math.cos(ph) * math.sin(th), math.cos(ph) * math.cos(th), math.sin(ph))
    r = (math.cos(th), -math.sin(th), 0.0)
    u = (-math.sin(ph) * math.sin(th), -math.sin(ph) * math.cos(th), math.cos(ph))
    return f, r, u


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class DomeCam:
    def __init__(self, pan=0.0, tilt=-0.1, zoom=0.0):
        self.pan, self.tilt, self.zoom = pan, tilt, zoom

    def fh(self):
        return math.tan(math.radians(WIDE_HFOV / MAX_MAG ** self.zoom) / 2)

    def ray(self, x, y):
        """World direction under picture point (x, y), -1..1, +y up."""
        f, r, u = _basis(self.pan, self.tilt)
        fh = self.fh()
        fv = fh / ASPECT
        return tuple(f[i] + x * fh * r[i] + y * fv * u[i] for i in range(3))

    def where(self, d):
        """Picture point (x, y) of world direction d (None behind the camera)."""
        f, r, u = _basis(self.pan, self.tilt)
        z = _dot(d, f)
        if z <= 0:
            return None
        fh = self.fh()
        return _dot(d, r) / z / fh, _dot(d, u) / z / (fh / ASPECT)

    def frame(self) -> bytes:
        n, m = 16, 9
        mesh = []

        def src(i, j):
            d = self.ray(i / n * 2 - 1, 1 - j / m * 2)
            az = math.atan2(d[0], d[1])
            el = math.atan2(d[2], math.hypot(d[0], d[1]))
            return (az / (2 * math.pi) + 0.5) * PANO_W, (0.5 - el / math.pi) * PANO_H

        for j in range(m):
            for i in range(n):
                box = (i * OUT_W // n, j * OUT_H // m, (i + 1) * OUT_W // n, (j + 1) * OUT_H // m)
                quad = (*src(i, j), *src(i, j + 1), *src(i + 1, j + 1), *src(i + 1, j))
                mesh.append((box, quad))
        im = PANO.transform((OUT_W, OUT_H), Image.MESH, mesh, Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()

    def hw(self):
        async def status():
            return {"pan": self.pan, "tilt": self.tilt, "zoom": self.zoom,
                    "moving_pan_tilt": "IDLE", "moving_zoom": "IDLE"}

        async def move_abs(pan=None, tilt=None, zoom=None):
            if pan is not None:
                self.pan = cal._wrap_pan(pan)
            if tilt is not None:
                self.tilt = max(TILT_RANGE[0], min(TILT_RANGE[1], tilt))
            if zoom is not None:
                self.zoom = max(0.0, min(1.0, zoom))

        async def move_rel(pan=None, tilt=None):
            await move_abs(pan=self.pan + (pan or 0), tilt=self.tilt + (tilt or 0))

        async def frame():
            return self.frame()

        async def nosleep(_s):
            return None

        return cal.Hw(status, move_abs, move_rel, frame, sleep=nosleep)


def _run(coro):
    return asyncio.run(coro)


def _click(cam: DomeCam, doc: dict, x: float, y: float) -> float:
    """Click (x, y), apply the plan, return how far the clicked spot is from the centre (fraction of the frame)."""
    target = cam.ray(x, y)
    plan = cal.plan_centre(doc, {"pan": cam.pan, "tilt": cam.tilt, "zoom": cam.zoom}, x, y, None, CAPS)
    assert plan["mode"] == "absolute"
    _run(cam.hw().move_abs(pan=plan["pan"], tilt=plan["tilt"]))
    p = cam.where(target)
    assert p is not None
    return max(abs(p[0]), abs(p[1])) / 2


@pytest.fixture(scope="module")
def dome_cal():
    cam = DomeCam(pan=0.05, tilt=-0.1, zoom=0.0)
    notes: list = []
    doc = _run(cal.run(cam.hw(), CAPS, zoom_levels=(0.0, 0.25, 0.5), notes=notes))
    return doc, notes


def test_calibration_measures_the_angles_it_was_never_told(dome_cal):
    doc, notes = dome_cal
    geo = doc["geometry"]
    assert geo, notes
    assert geo["kt"] == pytest.approx(KT, rel=0.15)
    assert geo["t0"] == pytest.approx(T0, abs=0.06)
    assert geo["kp"] == pytest.approx(KP, rel=0.12)
    assert any(n.startswith("angles:") for n in notes)


@pytest.mark.parametrize("tilt, within", [(0.05, 0.035), (-0.1, 0.035), (-0.35, 0.035), (-0.55, 0.06)])  # +5° … -65°
@pytest.mark.parametrize("click", [(0.8, 0.0), (-0.7, 0.6), (0.5, -0.8), (0.9, 0.9)])
def test_a_click_lands_in_the_centre_at_any_tilt(dome_cal, tilt, within, click):
    """Within 3.5 % of the frame from level to 45° down; 6 % at 65° down, where the straight line missed by 13-21 %
    (measured on this simulation) — what's left there is for the after-click check."""
    doc, _ = dome_cal
    assert _click(DomeCam(pan=-0.1, tilt=tilt, zoom=0.0), doc, *click) < within


def test_the_straight_line_model_misses_sideways_when_looking_down(dome_cal):
    """The bug the angles fix: at -65° a sideways click with the old model falls well short."""
    doc, _ = dome_cal
    linear = {**doc, "geometry": None}
    assert _click(DomeCam(pan=-0.1, tilt=-0.35), linear, 0.8, 0.0) > 0.1
    assert _click(DomeCam(pan=-0.1, tilt=-0.35), doc, 0.8, 0.0) < 0.035


def test_zoomed_in_clicks_land_too(dome_cal):
    doc, _ = dome_cal
    assert _click(DomeCam(pan=0.2, tilt=-0.3, zoom=0.4), doc, 0.7, -0.5) < 0.03


def test_a_camera_that_stays_level_keeps_the_straight_line_model():
    notes: list = []
    flat = [(0.0, 0.5), (-0.05, 0.501), (-0.1, 0.503), (-0.15, 0.502)]
    assert cal._geometry(flat, [{"pan_per_width": 0.5, "tilt_per_height": 0.3}], 0.0, 16 / 9, notes) is None
    assert "straight-line" in notes[0]


def test_after_a_bad_click_the_check_corrects_it_and_learns(dome_cal):
    """alpha's i-PRO: the stored tilt was 1.5x too big (haze), every vertical click overshot. The check measures the
    overshoot, sends one correction, and remembers it for that zoom."""
    doc, _ = dome_cal
    spoilt = {**doc, "geometry": None,
              "points": [{**p, "tilt_per_height": p["tilt_per_height"] * 1.5} for p in doc["points"]]}
    cam = DomeCam(pan=0.0, tilt=-0.15, zoom=0.0)
    before = cam.frame()
    st = {"pan": cam.pan, "tilt": cam.tilt, "zoom": cam.zoom}
    target = cam.ray(0.0, 0.5)
    plan = cal.plan_centre(spoilt, st, 0.0, 0.5, None, CAPS)
    _run(cam.hw().move_abs(pan=plan["pan"], tilt=plan["tilt"]))
    off = cam.where(target)
    assert abs(off[1]) / 2 > 0.08                                   # overshot, as on alpha
    res = _run(cal.verify_centre(cam.hw(), spoilt, CAPS, before, 0.0, 0.5, st))
    assert res["checked"] and res["corrected"], res
    assert res["gains"]["tilt"] == pytest.approx(1.5, rel=0.15)
    p = cam.where(target)
    assert max(abs(p[0]), abs(p[1])) / 2 < 0.03                     # now in the middle
    learned = res["learned"]
    assert cal.learned_gain(learned, 0.0, "tilt") > 1.1             # next click at this zoom overshoots less
    assert cal.learned_gain(learned, 0.0, "pan") == 1.0             # no sideways move, nothing learned


def test_a_check_that_cannot_see_changes_nothing(dome_cal):
    doc, _ = dome_cal
    cam = DomeCam()
    flat = Image.new("L", (OUT_W, OUT_H), 90)
    buf = io.BytesIO()
    flat.save(buf, "JPEG")
    hw = cam.hw()

    async def blank():
        return buf.getvalue()
    hw.frame = blank
    res = _run(cal.verify_centre(hw, doc, CAPS, buf.getvalue(), 0.6, 0.0, {"pan": 0, "tilt": -0.1, "zoom": 0}))
    assert not res["checked"] and res["corrected"] is None and res["learned"] is None


def test_a_stream_of_another_shape_is_not_compared(dome_cal):
    doc, _ = dome_cal
    cam = DomeCam()
    other = Image.new("L", (320, 240), 90)
    buf = io.BytesIO()
    other.save(buf, "JPEG")
    res = _run(cal.verify_centre(cam.hw(), doc, CAPS, buf.getvalue(), 0.6, 0.0, {"pan": 0, "tilt": -0.1, "zoom": 0}))
    assert not res["checked"] and "shape" in res["why"]


def test_learning_moves_part_way_and_is_bounded():
    doc = {"zoom_range": [0, 1], "points": []}
    once = cal.learn(doc, 0.0, {"tilt": 1.5})
    assert 1.1 < cal.learned_gain(once, 0.0, "tilt") < 1.2
    many = doc
    for _ in range(40):
        many = cal.learn(many, 0.0, {"tilt": 3.0})
    assert cal.learned_gain(many, 0.0, "tilt") == 2.0                # clamped: beyond that, recalibrate
    assert cal.learned_gain(many, 0.5, "tilt") == 1.0                # other zoom bands untouched
    assert many["learned"]["0.0"]["n"] == 40


def test_pan_stays_inside_a_zero_to_one_range():
    """i-PRO reports pan 0..1: 0.01 minus a bit used to be sent as -0.02 ("Destination out of bounds")."""
    caps = {"ranges": {"pan": [0, 1]}}
    assert cal._pan_target(0.01, -0.03, caps) == 0.0
    assert cal._pan_target(0.99, 0.05, caps) == 1.0
    assert cal._pan_target(0.95, 0.1, {"ranges": {"pan": [-1, 1]}}) == pytest.approx(-0.95)   # a real circle wraps


def test_zoom_scale_offers_doublings_up_to_the_most_there_is(dome_cal):
    doc, _ = dome_cal
    zs = cal.zoom_scale(doc, 0.25)
    mags = [s["mag"] for s in zs["steps"]]
    zooms = [s["zoom"] for s in zs["steps"]]
    assert mags[0] == 1.0 and mags == sorted(mags) and zooms == sorted(zooms)
    # measured to zoom 0.5 (≈4.5x) only; past that the estimate is capped at _EXTRAPOLATE_MAX more
    assert zs["steps"][-1].get("max") and zs["measured_to"] == pytest.approx(MAX_MAG ** 0.5, rel=0.15)
    assert zs["max_mag"] == pytest.approx(zs["measured_to"] * cal._EXTRAPOLATE_MAX, rel=0.05)
    assert zs["mag"] == pytest.approx(MAX_MAG ** 0.25, rel=0.15)
    assert cal.zoom_scale({"points": doc["points"][:1]}, 0.0) is None


def test_extrapolation_past_the_last_measured_zoom_is_bounded():
    """alpha: 1x → 14x over the first quarter; continuing that slope predicted thousands of x at full zoom."""
    doc = {"zoom_range": [0, 0.09375],
           "points": [{"zoom": 0.0, "pan_per_width": 0.147, "tilt_per_height": 0.2},
                      {"zoom": 0.0234, "pan_per_width": 0.0109, "tilt_per_height": 0.0148}]}
    assert cal.magnification(doc, 0.09375) == pytest.approx(0.147 / 0.0109 * cal._EXTRAPOLATE_MAX, rel=0.01)


# ─── Real frames ─────────────────────────────────────────────────────────────

def _pair(name):
    return (FIXTURES / f"{name}_before.jpg").read_bytes(), (FIXTURES / f"{name}_after.jpg").read_bytes()


@needs_frames
@pytest.mark.parametrize("name, want_x, want_y", [
    # alpha, i-PRO WV-X6533L, 2026-10-27, zoom 0. Clicks at x=+0.5 / y=+0.5 (planned slide 0.25). Pan landed
    # exactly; tilt overshot to 0.375 (counted by eye: the drill rig moved 135 of 360 px). The old brightness
    # comparison read this tilt pair as (-0.047, 0.245) — half sky, haze gradient — which is how calibration stored
    # a tilt 1.5x too big.
    ("ipro_pan", 0.25, 0.0), ("ipro_tilt", 0.0, 0.375),
    # kec01, CP Plus "Press Shop East Side View", same day, zoom 0.05: both clicks landed (ground fills the view).
    ("cpplus_pan", 0.24, 0.0), ("cpplus_tilt", 0.0, 0.25),
])
def test_real_frames_measure_what_the_pictures_show(name, want_x, want_y):
    sx, sy, conf = cal.measure_shift(*_pair(name))
    assert conf >= cal._CHECK_MIN_CONF
    assert sx == pytest.approx(want_x, abs=0.02)
    assert sy == pytest.approx(want_y, abs=0.02)


# ─── Aiming by degrees ───────────────────────────────────────────────────────

def _true_angles(cam: DomeCam) -> tuple[float, float]:
    """(azimuth, elevation) in degrees — the simulation's truth, not what calibration measured."""
    return cam.pan * KP, KT * (cam.tilt - T0)


@pytest.mark.parametrize("right, up", [(20, 0), (-20, 10), (45, -15), (-90, 0)])
def test_aiming_relative_to_home_turns_the_real_angle(dome_cal, right, up):
    doc, _ = dome_cal
    model = cal.angle_model(doc)
    home = {"pan": 0.1, "tilt": -0.15}
    cam = DomeCam(pan=-0.3, tilt=0.0)                     # wherever it happens to be
    aim = cal.aim_target(model, CAPS, {"pan": cam.pan, "tilt": cam.tilt}, home, right, up, "home")
    _run(cam.hw().move_abs(pan=aim["pan"], tilt=aim["tilt"]))
    az, el = _true_angles(cam)
    az0, el0 = home["pan"] * KP, KT * (home["tilt"] - T0)
    assert az - az0 == pytest.approx(right, abs=max(1.5, abs(right) * 0.12))
    assert el - el0 == pytest.approx(up, abs=max(1.5, abs(up) * 0.15))
    # and the readout says the same thing back
    deg = cal.degrees_of(model, CAPS, {"pan": cam.pan, "tilt": cam.tilt}, home)
    assert deg["right_of_home"] == pytest.approx(right, abs=0.5)
    assert deg["up_from_home"] == pytest.approx(up, abs=0.5)


def test_a_step_is_from_where_it_points_now(dome_cal):
    doc, _ = dome_cal
    model = cal.angle_model(doc)
    cam = DomeCam(pan=0.2, tilt=-0.2)
    az0, el0 = _true_angles(cam)
    aim = cal.aim_target(model, CAPS, {"pan": cam.pan, "tilt": cam.tilt}, None, -20, 0, "current")
    _run(cam.hw().move_abs(pan=aim["pan"], tilt=aim["tilt"]))
    az, el = _true_angles(cam)
    assert az - az0 == pytest.approx(-20, abs=2.4) and el == pytest.approx(el0)


def test_up_from_level_is_the_elevation(dome_cal):
    """Calibration found where level is — so "30° below level" means that, whatever Home is."""
    doc, _ = dome_cal
    model = cal.angle_model(doc)
    cam = DomeCam(pan=0.0, tilt=0.1)
    aim = cal.aim_target(model, CAPS, {"pan": cam.pan, "tilt": cam.tilt}, None, 0, -30, "current", up_from_level=True)
    _run(cam.hw().move_abs(pan=aim["pan"], tilt=aim["tilt"]))
    assert _true_angles(cam)[1] == pytest.approx(-30, abs=6)
    assert cal.degrees_of(model, CAPS, {"pan": cam.pan, "tilt": cam.tilt}, None)["elevation"] == pytest.approx(-30, abs=0.5)


def test_aiming_stays_inside_the_limits_and_says_so(dome_cal):
    doc, _ = dome_cal
    model = cal.angle_model(doc)
    aim = cal.aim_target(model, CAPS, {"pan": 0.0, "tilt": 0.2}, None, 0, 40, "current")    # past +15°
    assert aim["tilt"] == TILT_RANGE[1] and aim["clamped"]
    ok = cal.aim_target(model, CAPS, {"pan": 0.95, "tilt": 0.0}, None, 30, 0, "current")    # full circle: wraps
    assert ok["pan"] < 0 and not ok["clamped"]
    with pytest.raises(ValueError, match="Home"):
        cal.aim_target(model, CAPS, {"pan": 0.0, "tilt": 0.0}, None, 20, 0, "home")


def test_no_measured_angles_no_degrees(dome_cal):
    doc, _ = dome_cal
    assert cal.angle_model({**doc, "geometry": None}) is None


# ─── A head whose status says "arrived" at once ──────────────────────────────

class SlowLiarCam(DomeCam):
    """alpha's i-PRO (2026-10-27): GetStatus answers the COMMANDED position and IDLE right after AbsoluteMove, while
    the head takes seconds to get there; pictures meanwhile show it mid-turn. Time here is simulated: sleeps and
    frame grabs (0.6 s each) advance the clock, and the head travels for MOVE_S after every command."""

    MOVE_S = 2.5

    def __init__(self, **kw):
        super().__init__(**kw)
        self.clock = 0.0
        self.src = (self.pan, self.tilt, self.zoom)
        self.dst = self.src
        self.t_cmd = -99.0

    def _now_pos(self):
        f = min(1.0, max(0.0, (self.clock - self.t_cmd) / self.MOVE_S))
        return tuple(a + (b - a) * f for a, b in zip(self.src, self.dst))

    def hw(self):
        async def status():                                   # the lie: target, IDLE
            p, t, z = self.dst
            return {"pan": p, "tilt": t, "zoom": z, "moving_pan_tilt": "IDLE", "moving_zoom": "IDLE"}

        async def move_abs(pan=None, tilt=None, zoom=None):
            cur = self._now_pos()
            self.src, self.t_cmd = cur, self.clock
            self.dst = (cal._wrap_pan(pan) if pan is not None else cur[0],
                        max(TILT_RANGE[0], min(TILT_RANGE[1], tilt)) if tilt is not None else cur[1],
                        max(0.0, min(1.0, zoom)) if zoom is not None else cur[2])

        async def move_rel(pan=None, tilt=None):
            p, t, z = self.dst
            await move_abs(pan=p + (pan or 0), tilt=t + (tilt or 0))

        async def frame():
            self.clock += 0.6
            self.pan, self.tilt, self.zoom = self._now_pos()
            return self.frame()

        async def sleep(s):
            self.clock += s

        return cal.Hw(status, move_abs, move_rel, frame, sleep=sleep)


def test_a_head_that_says_arrived_at_once_still_calibrates_right():
    cam = SlowLiarCam(pan=0.05, tilt=-0.1, zoom=0.0)
    notes: list = []
    doc = _run(cal.run(cam.hw(), CAPS, zoom_levels=(0.0, 0.25), notes=notes))
    fh0 = math.tan(math.radians(WIDE_HFOV / 2))
    true_ppw0 = 2 * fh0 / math.radians(KP) / math.cos(math.radians(KT * (-0.1 - T0)))   # units per frame width
    pts = {p["zoom"]: p for p in doc["points"]}
    assert 0.0 in pts, notes
    # measured with small slides and centre-weighted frames, as on the straight simulation: within ~12 %
    assert abs(pts[0.0]["pan_per_width"]) == pytest.approx(true_ppw0, rel=0.12), notes
    assert len(doc["points"]) == 2, notes


def test_the_test_step_closes_in_instead_of_ping_ponging():
    """A head that ignores 0.01 but slides too far at 0.03 (what the notes showed) gets a step in between."""
    steps: list = []

    async def run():
        cam = DomeCam(pan=0.0, tilt=-0.1)
        hw = cam.hw()
        real = hw.move_abs

        async def move_abs(pan=None, tilt=None, zoom=None):
            if pan is not None and abs(pan - 0.0) > 1e-9:
                steps.append(round(pan, 4))
            await real(pan=pan, tilt=tilt, zoom=zoom)
        hw.move_abs = move_abs
        return await cal._measure_axis(hw, True, {"pan": 0.0, "tilt": -0.1, "zoom": 0.0}, "pan", 0.3, CAPS, [],
                                       target=(0.12, 0.14))
    ppw = _run(run())
    assert ppw is not None
    # 0.3 and 0.1 slid too far, 0.033 too little → the next try is BETWEEN 0.033 and 0.1 (not ×3 back to 0.1)
    assert steps[:3] == [0.3, 0.1, 0.0333]
    assert 0.0333 < steps[3] < 0.1
