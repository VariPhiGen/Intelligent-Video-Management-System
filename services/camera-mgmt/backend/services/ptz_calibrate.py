"""ptz_calibrate.py — click-to-centre and drag-to-zoom that land where the operator clicked, on any make.

WHY. Click-to-centre sent one fixed fraction (0.22) of the camera's WHOLE pan range for a click at the frame
edge, whatever the zoom. ONVIF's generic spaces map -1..1 to the full mechanical range (often 360° of pan), and
the field of view at 30x zoom is a thirtieth of the one at 1x — so zoomed out it over/under-shot, and zoomed in a
click near the edge swung the camera tens of degrees away (KEC kec01, CP Plus, 2026-10-26: "it never makes the
click the centre"). ONVIF does not report the field of view, and it differs per make and lens (alpha runs
Panasonic). So it is MEASURED, once per camera, from the camera's own pictures:

  for a few zoom levels: grab a frame, move pan by a small known amount, grab again, find how far the picture
  slid (`measure_shift`); same for tilt; then put the camera back where it was.

That gives `pan_per_width(zoom)` — the pan move that slides the scene by one whole frame width — and the same
for tilt per frame height. Their ratio to the zoomed-out value is the optical magnification, which also tells
drag-to-zoom which zoom position makes a drawn box fill the frame. Values between measured zoom levels are
interpolated in log space (field of view shrinks roughly geometrically with zoom).

Moves are made in the space the calibration was measured in: ABSOLUTE when the camera advertises it (one
AbsoluteMove carries pan + tilt + zoom, from the position GetStatus reports), else RELATIVE. A camera that was
never calibrated keeps the old fixed-gain behaviour, so nothing gets worse before someone presses Calibrate.

The frame comparison is plain Pillow (already a dependency): both frames are shrunk and greyed, and the shift
with the lowest mean difference over the overlap wins, coarse to fine. Good enough for a scene with any texture;
a blank wall or the sky at full zoom gives no confident match, and that zoom level is simply skipped.

TRUST NOTHING ONE PICTURE SAYS (alpha, i-PRO WV-X6533L, 2026-10-26). The first version took one frame after each
move from the HLS live edge — 2-4 s behind the head — after a fixed 2 s wait, and believed a match score of 0.25.
At night half the "after" frames were still the "before" view ("moved +0.000") or mid-move blur that matched at
the edge of the search ("moved -0.613"); those were saved, the stored tilt came out with the WRONG SIGN at the
zoom in use, and every click above the centre tilted the camera down. Now:
  * frames come straight from the stream (RTSP, the caller's job) and a frame counts only once two grabs in a
    row show the same view (`_still_frame`) — the head and the picture have both stopped;
  * every test move is checked by moving BACK: the picture must slide back by the same amount, the other way,
    with a good match both times (`_measure_axis`); one-way answers are thrown away;
  * levels must agree with each other (`_consistent`): same direction on every level, and a view that gets
    WIDER as zoom goes up is impossible — such a level is dropped, with a note saying why.

COMPARE DETAIL, NOT BRIGHTNESS (alpha, i-PRO, 2026-10-27). Even with verified frames the stored tilt was 1.5x
too big: a tilt-up click moved the scene 0.375 of the frame instead of 0.25 (pan was exact). The view is half
hazy sky; the comparison worked on contrast-stretched brightness, the haze's top-to-bottom gradient dragged the
best match toward a smaller slide, and each frame was stretched on its own histogram (more sky after tilting up
= the same ground a different shade). `_grey` now keeps only fine detail — the frame minus a heavily blurred copy
of itself — so sky, haze and exposure flatten to grey and only edges and texture decide the match. On the real
frames: i-PRO tilt 0.271 → 0.375 (what the pictures show), CP Plus unchanged (0.24 / 0.25). No AI: plain
pixel arithmetic, milliseconds on the CPU, with a confidence that says when there is nothing to match.

REAL ANGLES, NOT A STRAIGHT LINE. The first model was linear — twice as far from the centre = twice the move —
and pan per frame width was measured at ONE tilt. Both are wrong in a way that grows: pointing steeply down, a
pan move sweeps the picture far less (∝ cos elevation), so sideways clicks fell short; and a wide lens isn't
linear toward the edge. ONVIF reports neither degrees nor the horizon, so calibration MEASURES them: pan per
frame width at a few more tilts (downward — ground has detail, sky doesn't) follows 1/cos(elevation), and the
fit gives degrees per tilt unit and the tilt of the horizon (`_fit_tilt_model`); the picture's shape then gives
the field of view in degrees both ways and degrees per pan unit (`_geometry`). A click becomes a real direction
in 3-D and the camera is sent to the pan/tilt that points at it (`_deltas`). A camera whose test tilts barely
changed the pan reading (it stayed near level, or has no tilt room) keeps the linear model — the two agree there.

CHECK AFTER EVERY CLICK (`verify_centre`). The browser sends the frame the operator clicked on; once the head
has stopped, the server grabs a still frame and measures where the clicked spot actually went. Off by more than
_CHECK_TOL → one correction move, and the measured overshoot is kept per zoom band (`learned`, an average that
moves a third of the way each click) so the next click at that zoom is right first time. A check that can't see
(blank view, a different stream shape, an implausible reading) changes nothing.
"""
from __future__ import annotations

import asyncio
import io
import math
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import structlog

log = structlog.get_logger(__name__)

VERSION = 2
# Denser at the wide end: magnification climbs fastest there (alpha's i-PRO is ~14x a quarter of the way in), and
# the old 0 / 0.25 step left the whole climb to interpolation.
ZOOM_LEVELS = (0.0, 0.03, 0.0625, 0.125, 0.25, 0.5, 0.75, 1.0)
FALLBACK_GAIN = 0.22          # the old fixed fraction, for cameras nobody has calibrated
_TARGET_SHIFT = (0.08, 0.55)  # a usable measured slide, as a fraction of the frame (0.45 threw away good,
                              # agreed out-and-back readings of 0.46-0.55 on alpha's i-PRO and then ping-ponged)
_UNTRUSTED_SETTLE_S = 3.0     # extra wait after a move on a head whose status never says MOVING (see _settle)
_MAX_TRIES = 6                # per axis per zoom level: grow/shrink the test move until the slide is usable
_MIN_STEP = 0.0002            # smallest test move; 0.002 was bigger than half a frame at the i-PRO's long end
_EXTRAPOLATE_MAX = 2.5        # past the last measured zoom, assume at most this much MORE magnification
_GEOMETRY_TILTS = (0.2, 0.4, 0.6)   # extra pan readings at these fractions of the downward tilt room
_GEOMETRY_SLIDE = (0.03, 0.09)      # ... each a small slide (see _measure_geometry)
_GEOMETRY_MIN_SPREAD = 1.08   # the pan readings must differ by 8 %+ across tilts to fit angles from them
_GEOMETRY_MAX_RMS = 0.04      # ... and fit 1/cos(elevation) this well (log units)
_CHECK_TOL = 0.04             # a click that lands within 4 % of the frame of the centre needs no correction
_CHECK_MIN_CONF = 0.5
_LEARN_RATE = 0.35
_SETTLE_TIMEOUT_S = 10.0
_STREAM_LAG_S = 1.0           # the picture trails the head a little even from RTSP; then _still_frame decides
_MIN_CONFIDENCE = 0.4         # 0.25 let night-time guesses through (alpha); a real match on any texture is 0.6+
_STILL_TRIES, _STILL_GAP_S, _STILL_MAX_SHIFT = 5, 1.0, 0.012
_BACK_TOLERANCE = 0.2         # the slide back may differ from the slide out by this fraction


# ─── Frame comparison ────────────────────────────────────────────────────────

def _load(jpeg: bytes, centre: Optional[float] = None):
    """The frame in grey; `centre` keeps only that middle fraction of it (both ways)."""
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("L")
    if centre:
        w, h = im.size
        im = im.crop((round(w * (1 - centre) / 2), round(h * (1 - centre) / 2),
                      round(w * (1 + centre) / 2), round(h * (1 + centre) / 2)))
    return im


def _grey(im, width: int):
    """Detail only: the shrunk frame minus a heavily blurred copy of itself (see "COMPARE DETAIL" above). The light
    pre-blur keeps sensor noise from passing as detail; radii scale with the working width (64 and 256)."""
    from PIL import Image, ImageChops, ImageFilter, ImageOps
    h = max(8, round(im.height * width / im.width))
    im = im.resize((width, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(width / 64))
    base = im.filter(ImageFilter.GaussianBlur(max(2, width / 8)))
    return ImageOps.autocontrast(ImageChops.subtract(im, base, 1, 128), cutoff=1)


def frame_aspect(jpeg: Optional[bytes]) -> Optional[float]:
    if not jpeg:
        return None
    from PIL import Image
    try:
        w, h = Image.open(io.BytesIO(jpeg)).size
    except Exception:                                   # noqa: BLE001 — not a picture
        return None
    return w / h if h else None


def _diff(a, b, dx: int, dy: int) -> Optional[float]:
    """Mean absolute difference between `a` and `b` moved by (dx, dy) — over the overlap only. None when the
    overlap is too small to mean anything."""
    from PIL import ImageChops, ImageStat
    w, h = a.size
    ax0, ay0 = max(0, dx), max(0, dy)
    bx0, by0 = max(0, -dx), max(0, -dy)
    ow, oh = w - abs(dx), h - abs(dy)
    if ow < w * 0.35 or oh < h * 0.35:
        return None
    ca = a.crop((ax0, ay0, ax0 + ow, ay0 + oh))
    cb = b.crop((bx0, by0, bx0 + ow, by0 + oh))
    return ImageStat.Stat(ImageChops.difference(ca, cb)).mean[0]


def _best(a, b, xs, ys) -> tuple[int, int, float, float, dict]:
    best = (0, 0, float("inf"))
    scores = {}
    for dy in ys:
        for dx in xs:
            d = _diff(a, b, dx, dy)
            if d is None:
                continue
            scores[(dx, dy)] = d
            if d < best[2]:
                best = (dx, dy, d)
    vals = sorted(scores.values())
    median = vals[len(vals) // 2] if vals else float("inf")
    return best[0], best[1], best[2], median, scores


def _subpixel(scores: dict, dx: int, dy: int) -> tuple[float, float]:
    """Parabola through the best offset and its neighbours, each way: a fraction of a pixel more precise — what
    makes a small test move (the angle readings) measurable at all."""
    def off(m, c, p):
        if m is None or p is None:
            return 0.0
        den = m - 2 * c + p
        return 0.0 if den <= 0 else max(-0.5, min(0.5, (m - p) / (2 * den)))
    c = scores[(dx, dy)]
    return (dx + off(scores.get((dx - 1, dy)), c, scores.get((dx + 1, dy))),
            dy + off(scores.get((dx, dy - 1)), c, scores.get((dx, dy + 1))))


def measure_shift(before: bytes, after: bytes, centre: Optional[float] = None) -> tuple[float, float, float]:
    """How far the scene slid between two frames: (sx, sy, confidence).

    sx > 0 = the content moved LEFT by sx frame widths (what panning right does); sy > 0 = it moved DOWN by sy
    frame heights (what tilting up does) — i.e. both signs follow the camera's motion, not the picture's.
    confidence 0..1 = how much the best match beats a typical wrong one; under ~0.25 the answer is a guess.

    `centre` (e.g. 0.5) compares only the middle of the frames — still in fractions of the WHOLE frame. For small
    moves where the picture also turns (a pan while looking steeply down) or where the lens stretches the edges:
    the middle slides like the centre does."""
    img_a, img_b = _load(before, centre), _load(after, centre)
    small_a, small_b = _grey(img_a, 64), _grey(img_b, 64)
    w, h = small_a.size
    rx, ry = int(w * 0.6), int(h * 0.6)
    dx, dy, best, median, _ = _best(small_a, small_b, range(-rx, rx + 1), range(-ry, ry + 1))
    if not math.isfinite(best):
        return 0.0, 0.0, 0.0
    scale = 4
    big_a, big_b = _grey(img_a, 64 * scale), _grey(img_b, 64 * scale)
    cx, cy = dx * scale, dy * scale
    fdx, fdy, fbest, _, scores = _best(big_a, big_b, range(cx - scale - 1, cx + scale + 2),
                                       range(cy - scale - 1, cy + scale + 2))
    if math.isfinite(fbest):
        sdx, sdy = _subpixel(scores, fdx, fdy)
        dx, dy, best = sdx / scale, sdy / scale, fbest / 1.0
    else:
        dx, dy = float(dx), float(dy)
    confidence = 0.0 if not math.isfinite(median) or median <= 0 else max(0.0, min(1.0, 1 - best / median))
    # Content at x in `before` sits at x - dx' in `after` when the crop offsets say dx; panning right slides the
    # scene left, so the camera's motion is +dx. Tilt up slides the scene DOWN in image rows, i.e. dy < 0 in
    # this crop convention → camera motion = -dy.
    k = centre or 1.0
    return dx / w * k, -dy / h * k, confidence


# ─── The model ───────────────────────────────────────────────────────────────

def _points(cal: Optional[dict], key: str) -> list[tuple[float, float]]:
    pts = []
    for p in (cal or {}).get("points") or []:
        z, v = p.get("zoom"), p.get(key)
        if isinstance(z, (int, float)) and isinstance(v, (int, float)) and v != 0:
            pts.append((float(z), float(v)))
    return sorted(pts)


def per_unit(cal: Optional[dict], zoom: Optional[float], key: str) -> Optional[float]:
    """pan_per_width / tilt_per_height at `zoom`, interpolated in log space (sign kept — a ceiling-mounted head
    can be inverted). None when the camera has no usable calibration for that axis."""
    pts = _points(cal, key)
    if not pts:
        return None
    z = float(zoom or 0.0)
    if len(pts) == 1:
        return pts[0][1]

    def along(seg, at):
        (z0, v0), (z1, v1) = seg
        sign = 1.0 if v0 > 0 else -1.0
        a, b = math.log(abs(v0)), math.log(abs(v1))
        t = 0.0 if z1 == z0 else (at - z0) / (z1 - z0)
        return sign * math.exp(a + (b - a) * t)

    # Outside the measured span the nearest segment's trend is continued (log-linear), not frozen: at a long
    # zoom's far end (i-PRO WV-X6533L, 40x) the picture is often too soft to measure, and freezing the last
    # measured field of view would over-shoot every click up there. But not without limit: alpha's i-PRO measured
    # 1x → 14x over its first quarter, and continuing THAT slope to full zoom predicted thousands of x — every
    # click up there barely moved. At most _EXTRAPOLATE_MAX more, and never wider than the last measured view.
    if z <= pts[0][0]:
        return along((pts[0], pts[1]), z)
    if z >= pts[-1][0]:
        last = pts[-1][1]
        v = abs(along((pts[-2], pts[-1]), z))
        return math.copysign(min(abs(last), max(v, abs(last) / _EXTRAPOLATE_MAX)), last)
    for seg in zip(pts, pts[1:]):
        if seg[0][0] <= z <= seg[1][0]:
            return along(seg, z)
    return pts[-1][1]


def magnification(cal: Optional[dict], zoom: Optional[float]) -> Optional[float]:
    """Optical magnification at `zoom` relative to the widest measured view (from how the field of view shrank)."""
    wide = per_unit(cal, _points(cal, "pan_per_width")[0][0], "pan_per_width") if _points(cal, "pan_per_width") else None
    here = per_unit(cal, zoom, "pan_per_width")
    if not wide or not here:
        return None
    return abs(wide) / abs(here)


def zoom_for_magnification(cal: Optional[dict], mag: float) -> Optional[float]:
    """Inverse of `magnification`: the zoom position that gives `mag` (bisection; clamped to the measured span)."""
    pts = _points(cal, "pan_per_width")
    if len(pts) < 2:
        return None
    lo, hi = (cal or {}).get("zoom_range") or (0.0, 1.0)   # the camera's whole zoom range (extrapolated past
    lo, hi = float(lo), float(hi)                            # the measured span)
    if mag <= (magnification(cal, lo) or 1.0):
        return lo
    if mag >= (magnification(cal, hi) or 1.0):
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if (magnification(cal, mid) or 1.0) < mag:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def zoom_scale(cal: Optional[dict], zoom: Optional[float]) -> Optional[dict]:
    """For the zoom control: the magnification now, the most there is, and the zoom position for 1x, 2x, 4x … —
    from the calibration (None for a camera with fewer than two measured zoom levels: no honest "x" to show)."""
    pts = _points(cal, "pan_per_width")
    if len(pts) < 2:
        return None
    lo, hi = (cal or {}).get("zoom_range") or (0.0, 1.0)
    lo, hi = float(lo), float(hi)
    top = magnification(cal, hi) or 1.0
    steps = [{"mag": 1.0, "zoom": lo}]
    m = 2.0
    while m < top * 0.9:
        z = zoom_for_magnification(cal, m)
        if z is not None:
            steps.append({"mag": m, "zoom": z})
        m *= 2
    steps.append({"mag": round(top, 1), "zoom": hi, "max": True})
    now = magnification(cal, zoom) if zoom is not None else None
    return {"mag": round(now, 2) if now else None, "max_mag": round(top, 1), "steps": steps,
            # above this the "x" is an estimate (extrapolated, see per_unit)
            "measured_to": round(magnification(cal, pts[-1][0]) or 1.0, 1)}


# ─── What the clicks taught it ───────────────────────────────────────────────

def _zoom_n(cal: Optional[dict], zoom: Optional[float]) -> float:
    lo, hi = (cal or {}).get("zoom_range") or (0.0, 1.0)
    lo, hi = float(lo), float(hi)
    z = float(zoom or 0.0)
    return max(0.0, min(1.0, (z - lo) / (hi - lo))) if hi > lo else 0.0


def _band(cal: Optional[dict], zoom: Optional[float]) -> str:
    return f"{round(_zoom_n(cal, zoom) * 10) / 10:.1f}"     # 11 bands across the zoom range


def learned_gain(cal: Optional[dict], zoom: Optional[float], axis: str) -> float:
    """How much further than planned clicks at this zoom have been going (1 = spot on; 1.5 = 50 % too far)."""
    g = (((cal or {}).get("learned") or {}).get(_band(cal, zoom)) or {}).get(axis)
    return float(g) if isinstance(g, (int, float)) and 0.3 <= g <= 3.0 else 1.0


def learn(cal: dict, zoom: Optional[float], gains: dict) -> dict:
    """A copy of `cal` with this click's measured gains folded into its zoom band (a running average that moves
    _LEARN_RATE of the way, so one odd reading can't swing it; clamped to 0.5..2 — beyond that recalibrate)."""
    learned = dict(cal.get("learned") or {})
    band = dict(learned.get(_band(cal, zoom)) or {})
    for axis, g in gains.items():
        if g is None:
            continue
        old = band.get(axis, 1.0)
        band[axis] = round(max(0.5, min(2.0, old * (g ** _LEARN_RATE))), 4)   # geometric: 2x and 0.5x are symmetric
    band["n"] = int(band.get("n", 0)) + 1
    learned[_band(cal, zoom)] = band
    return {**cal, "learned": learned}


# ─── Angles ──────────────────────────────────────────────────────────────────

def _fit_tilt_model(samples: list[tuple[float, float]]) -> Optional[tuple[float, float, float]]:
    """(degrees per tilt unit, tilt of the horizon, rms) from pan-per-width readings at several tilts.

    Panning at elevation e slides the picture by (pan angle × cos e), so pan-per-width ∝ 1/cos(e), with
    e = k·(tilt − t0). A coarse-then-fine grid over (k, t0); the scale is free. None when there are fewer than
    three readings or they hardly differ (the camera stayed near level — nothing to fit, nothing to correct)."""
    if len(samples) < 3:
        return None
    vals = [abs(v) for _, v in samples]
    if max(vals) / min(vals) < _GEOMETRY_MIN_SPREAD:
        return None
    ts = [t for t, _ in samples]
    logs = [math.log(v) for v in vals]
    mid = (max(ts) + min(ts)) / 2

    def rms(k_deg: float, e_mid: float) -> float:
        # parametrised by the elevation at the middle sample: the horizon can be far outside the tilts measured
        res = []
        for t, lv in zip(ts, logs):
            c = math.cos(math.radians(e_mid + k_deg * (t - mid)))
            if c <= 0.03:
                return float("inf")
            res.append(lv + math.log(c))
        mean = sum(res) / len(res)
        return math.sqrt(sum((r - mean) ** 2 for r in res) / len(res))

    best = (float("inf"), 0.0, 0.0)
    for i in range(90):
        k = 20 * (720 / 20) ** (i / 89)                                 # 20 .. 720 °/unit, log-spaced
        for e in range(-88, 89):
            r = rms(k, float(e))
            if r < best[0]:
                best = (r, k, float(e))
    r, k, e_mid = best
    dk, de = k * 0.04, 1.0
    for _ in range(3):                                                  # refine around the coarse answer
        for kk in [k + dk * i / 5 for i in range(-5, 6)]:
            for ee in [e_mid + de * j / 5 for j in range(-5, 6)]:
                rr = rms(kk, ee)
                if rr < r:
                    r, k, e_mid = rr, kk, ee
        dk, de = dk / 5, de / 5
    t0 = mid - e_mid / k
    if not math.isfinite(r):
        return None
    return k, t0, r


def _geometry(samples: list[tuple[float, float]], points: list[dict], cal_tilt: float, aspect: Optional[float],
              notes: list) -> Optional[dict]:
    """Angles for `_deltas`, or None (with a note) when they can't be trusted — then clicks use the linear model."""
    fit = _fit_tilt_model(samples)
    if fit is None:
        notes.append("angles: pan barely changed with tilt — using the straight-line model (fine near level)")
        return None
    k_deg, t0, r = fit
    if r > _GEOMETRY_MAX_RMS:
        notes.append(f"angles: readings didn't follow the lens geometry (rms {r:.3f}) — straight-line model kept")
        return None
    a = aspect or 16 / 9
    kt = math.radians(k_deg)
    cos_c = math.cos(kt * (cal_tilt - t0))
    kps = []
    for p in points:
        ppw, tph = abs(p["pan_per_width"]), abs(p["tilt_per_height"])
        if ppw > 0 and cos_c > 0.05:
            kps.append(a * kt * tph / (ppw * cos_c))
    if not kps:
        return None
    kp = sorted(kps)[len(kps) // 2]
    vfov0 = math.degrees(2 * math.atan(kt * abs(points[0]["tilt_per_height"]) / 2))
    if not (1.0 <= vfov0 <= 130.0) or not (10.0 <= math.degrees(kp) <= 2000.0):
        notes.append(f"angles: implausible fit (view {vfov0:.0f}° tall, {math.degrees(kp):.0f}° per pan unit) — "
                     "straight-line model kept")
        return None
    hfov0 = math.degrees(2 * math.atan(a * math.tan(math.radians(vfov0) / 2)))
    notes.append(f"angles: {k_deg:.0f}° per tilt unit, level at tilt {t0:+.3f}, widest view ≈ {hfov0:.0f}° × "
                 f"{vfov0:.0f}° (fit rms {r:.3f})")
    return {"kt": round(k_deg, 3), "t0": round(t0, 5), "kp": round(math.degrees(kp), 3), "aspect": round(a, 4),
            "cal_tilt": round(cal_tilt, 5), "rms": round(r, 4),
            "samples": [[round(t, 5), round(v, 6)] for t, v in samples]}


def _wrap_pan(p: float) -> float:
    # generic pan -1..1 is a circle on a 360° head: past +1 comes back in at -1
    while p > 1.0:
        p -= 2.0
    while p < -1.0:
        p += 2.0
    return p


def _deltas(cal: Optional[dict], status: Optional[dict], x: float, y: float) -> Optional[tuple[float, float]]:
    """The pan/tilt change (camera units) that brings the picture point (x, y) to the centre, or None uncalibrated.

    With measured angles: the click is a ray through the lens (rectilinear: x·tan(h/2), y·tan(v/2)), turned into
    world directions at the head's current elevation, and the answer is the pan/tilt that points along it — so a
    sideways click while looking steeply down pans as far as it really needs to. Without: the straight line."""
    st = status or {}
    ppw = per_unit(cal, st.get("zoom"), "pan_per_width")
    tph = per_unit(cal, st.get("zoom"), "tilt_per_height")
    if not ppw or not tph:
        return None
    geo = (cal or {}).get("geometry")
    tilt = st.get("tilt")
    if geo and tilt is not None:
        kt, kp = math.radians(geo["kt"]), math.radians(geo["kp"])
        t0 = float(geo["t0"])
        s_p, s_t = math.copysign(1, ppw), math.copysign(1, tph)       # +units = picture-right / picture-up
        fv = kt * abs(tph) / 2                                         # tan(half the view height)
        fh = kp * abs(ppw) * math.cos(kt * (float(geo["cal_tilt"]) - t0)) / 2
        e = s_t * kt * (float(tilt) - t0)                              # elevation now (picture-up positive)
        dx = x * fh
        dy = math.cos(e) - y * fv * math.sin(e)
        dz = math.sin(e) + y * fv * math.cos(e)
        d_az = math.atan2(dx, dy)
        e2 = math.atan2(dz, math.hypot(dx, dy))
        dpan, dtilt = s_p * d_az / kp, s_t * (e2 - e) / kt
    else:
        dpan, dtilt = x / 2 * ppw, y / 2 * tph
    return (dpan / learned_gain(cal, st.get("zoom"), "pan"), dtilt / learned_gain(cal, st.get("zoom"), "tilt"))


def _pan_target(cur: float, dpan: float, caps: dict) -> float:
    """Absolute pan target inside the camera's limits. Wraps only on a head whose range is the full -1..1 circle;
    any other range (i-PRO: 0..1) is clamped — sending -0.02 there is "Destination out of bounds"."""
    lo, hi = _bounds(caps, "pan")
    p = cur + dpan
    if lo <= -0.99 and hi >= 0.99:
        return _wrap_pan(p)
    return max(lo, min(hi, p))


def plan_centre(cal: Optional[dict], status: Optional[dict], x: float, y: float,
                zoom_factor: Optional[float], caps: dict) -> dict:
    """The move for a click at (x, y) (-1..1 from the centre, +y up), optionally zooming in by `zoom_factor`.

    {"mode": "absolute", "pan", "tilt", "zoom"} | {"mode": "relative", "pan", "tilt", "zoom"} plus "calibrated".
    Absolute needs the calibration to have been measured in absolute space AND a reported position; relative
    otherwise. Uncalibrated = the old fixed gain (relative)."""
    space = (cal or {}).get("space")
    d = _deltas(cal, status, x, y)
    if d is None:
        zoom = None
        if zoom_factor and zoom_factor > 1:
            zoom = round(min(0.5, (1 - 1 / zoom_factor) * 0.5), 4)
        elif zoom_factor and zoom_factor < 1:
            zoom = -round(min(0.3, (1 - zoom_factor) * 0.3), 4)
        return {"mode": "relative", "calibrated": False, "pan": round(x * FALLBACK_GAIN, 4),
                "tilt": round(y * FALLBACK_GAIN, 4), "zoom": zoom}
    dpan, dtilt = d
    cur = status or {}
    abs_ok = (space == "absolute" and (caps.get("absolute") or {}).get("pan_tilt")
              and cur.get("pan") is not None and cur.get("tilt") is not None)
    target_zoom = None
    if zoom_factor and zoom_factor != 1:
        mag_now = magnification(cal, cur.get("zoom")) or 1.0
        target_zoom = zoom_for_magnification(cal, mag_now * zoom_factor)
    if abs_ok:
        tlo, thi = _bounds(caps, "tilt")
        out = {"mode": "absolute", "calibrated": True,
               "pan": round(_pan_target(float(cur["pan"]), dpan, caps), 5),
               "tilt": round(max(tlo, min(thi, float(cur["tilt"]) + dtilt)), 5), "zoom": None}
        if target_zoom is not None and (caps.get("absolute") or {}).get("zoom"):
            out["zoom"] = target_zoom
        return out
    dz = None
    if target_zoom is not None and cur.get("zoom") is not None:
        dz = round(target_zoom - float(cur["zoom"]), 4)
    return {"mode": "relative", "calibrated": True, "pan": round(dpan, 5), "tilt": round(dtilt, 5), "zoom": dz}


# ─── Running a calibration ───────────────────────────────────────────────────

class Hw:
    """What a calibration needs from the outside world — injectable so the logic is testable without a camera."""

    def __init__(self, status: Callable[[], Awaitable[Optional[dict]]],
                 move_abs: Callable[..., Awaitable[Any]], move_rel: Callable[..., Awaitable[Any]],
                 frame: Callable[[], Awaitable[Optional[bytes]]], sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 progress: Optional[Callable[[str], Awaitable[Any]]] = None):
        self.status, self.move_abs, self.move_rel, self.frame, self.sleep = status, move_abs, move_rel, frame, sleep
        self.progress = progress
        self.saw_moving = False      # has GetStatus ever said MOVING? (see _settle)


async def _settle(hw: Hw) -> Optional[dict]:
    """Wait for the head to stop. HEADS THAT SAY "ARRIVED" AT ONCE (alpha, i-PRO WV-X6533L, 2026-10-27): GetStatus
    answered the COMMANDED position and IDLE 0.7 s after every AbsoluteMove while the head was still turning, so
    pictures were taken mid-turn — a 0.01 test pan read as no movement and 0.03 as 0.47 of the frame (really 0.10
    and 0.30, measured by hand on the same camera), the steps ping-ponged and two of four cameras failed or got one
    level. A head that has never once reported MOVING gets a fixed extra wait and a stricter still-picture check
    (_still_frame) instead of being believed."""
    t0 = time.monotonic()
    st = None
    while time.monotonic() - t0 < _SETTLE_TIMEOUT_S:
        await hw.sleep(0.5)
        st = await hw.status()
        moving = "MOVING" in {str((st or {}).get("moving_pan_tilt") or "").upper(),
                              str((st or {}).get("moving_zoom") or "").upper()}
        if moving:
            hw.saw_moving = True
        if not st or not moving:
            break
    await hw.sleep(_STREAM_LAG_S + (0.0 if hw.saw_moving else _UNTRUSTED_SETTLE_S))
    return st


async def _same_view(a: bytes, b: bytes) -> bool:
    sx, sy, conf = await asyncio.to_thread(measure_shift, a, b)
    return conf >= _MIN_CONFIDENCE and abs(sx) <= _STILL_MAX_SHIFT and abs(sy) <= _STILL_MAX_SHIFT


async def _still_frame(hw: Hw, moved_from: Optional[bytes] = None) -> Optional[bytes]:
    """A picture of where the head IS. After a move (`moved_from` = the picture before it): first wait for the view
    to CHANGE — a lagging stream keeps showing the old view, and an old view looks perfectly still — then for two
    grabs in a row to show the same view (head and picture both stopped). At most _STILL_TRIES grabs per phase; a
    move too small to see, or a scene that never holds still, ends with the last grab and is then judged by the
    back-move check, not trusted blindly."""
    prev = await hw.frame()
    if moved_from is not None:
        for _ in range(_STILL_TRIES):
            if prev and not await _same_view(moved_from, prev):
                break
            await hw.sleep(_STILL_GAP_S)
            prev = (await hw.frame()) or prev
    # Two grabs in a row that agree = still; THREE on a head whose status can't be believed (see _settle).
    need = 1 if hw.saw_moving else 2
    agreed = 0
    for _ in range(_STILL_TRIES + need - 1):
        await hw.sleep(_STILL_GAP_S)
        cur = await hw.frame()
        if prev and cur and await _same_view(prev, cur):
            agreed += 1
            if agreed >= need:
                return cur
        else:
            agreed = 0
        prev = cur or prev
    return prev


def _bounds(caps: dict, axis: str) -> tuple[float, float]:
    r = (caps.get("ranges") or {}).get(axis)
    if isinstance(r, (list, tuple)) and len(r) == 2:
        return float(r[0]), float(r[1])
    return (0.0, 1.0) if axis == "zoom" else (-1.0, 1.0)


def _room(value: float, lo: float, hi: float, margin: float = 0.01) -> tuple[float, float]:
    """How far the head can move down / up from `value` inside [lo, hi]."""
    return max(0.0, value - lo - margin), max(0.0, hi - value - margin)


async def _measure_axis(hw: Hw, absolute: bool, base: dict, axis: str, start: float,
                        caps: Optional[dict] = None, notes: Optional[list] = None,
                        target: tuple[float, float] = _TARGET_SHIFT, centre: Optional[float] = None,
                        min_conf: float = _MIN_CONFIDENCE) -> Optional[float]:
    """Units per frame (width for pan, height for tilt) at the current zoom, or None if no confident answer.

    Test moves stay inside the camera's reported limits (caps["ranges"]) and point toward the side with more
    room; a move the camera refuses (e.g. i-PRO "Destination out of bounds") is retried the other way once.
    `notes` collects one line per try, so a failed calibration can say what it saw."""
    caps = caps or {}
    lo, hi = _bounds(caps, axis)
    wraps = axis == "pan" and lo <= -0.99 and hi >= 0.99      # a full 360° head: pan never runs out of room
    step = start
    flipped = False
    # The biggest step that slid too little and the smallest that slid too far (or lost the match): once both are
    # known the next try is between them — ×3 / ÷3 alone ping-ponged 0.01 ↔ 0.03 for every try on alpha's i-PRO.
    too_small, too_big = 0.0, math.inf
    for _ in range(_MAX_TRIES):
        if absolute and not wraps:
            down, up = _room(base[axis], lo, hi)
            room = up if step > 0 else down
            if abs(step) > room:
                other = down if step > 0 else up
                if other > room:
                    step = -step                            # more room the other way
                    room = other
                step = math.copysign(min(abs(step), room), step)
            if max(down, up) < 0.002:
                if notes is not None:
                    notes.append(f"{axis}: no room to move inside the camera's limits")
                return None
        if abs(step) < _MIN_STEP:
            # Not "no room" (what alpha's i-PRO said at 0.003, the old floor): even the smallest test move slid the
            # picture too far to match, or the picture is too soft at this zoom.
            if notes is not None:
                notes.append(f"{axis}: even a {_MIN_STEP} move gave no clean picture shift at this zoom")
            return None
        try:
            if absolute:
                await hw.move_abs(pan=base["pan"], tilt=base["tilt"], zoom=base.get("zoom"))
            await _settle(hw)
            a = await _still_frame(hw)
            if absolute:
                pan = (_wrap_pan(base["pan"] + step) if wraps else base["pan"] + step) if axis == "pan" else base["pan"]
                tilt = base["tilt"] + step if axis == "tilt" else base["tilt"]
                await hw.move_abs(pan=pan, tilt=tilt, zoom=base.get("zoom"))
            else:
                await hw.move_rel(pan=step if axis == "pan" else None, tilt=step if axis == "tilt" else None)
            await _settle(hw)
            b = await _still_frame(hw, moved_from=a)
            # ... and back: the picture must slide back by the same amount. A stale or blurred frame can't fake that.
            if absolute:
                await hw.move_abs(pan=base["pan"], tilt=base["tilt"], zoom=base.get("zoom"))
            else:
                await hw.move_rel(pan=-step if axis == "pan" else None, tilt=-step if axis == "tilt" else None)
            await _settle(hw)
            c = await _still_frame(hw, moved_from=b)
        except Exception as exc:                            # noqa: BLE001 — a refused move: try the other way once
            if notes is not None:
                notes.append(f"{axis} step {step:+.3f}: camera refused ({str(exc)[:80]})")
            if flipped:
                return None
            step, flipped = -step, True
            continue
        if not a or not b or not c:
            if notes is not None:
                notes.append(f"{axis}: no picture from the stream")
            return None
        sx, sy, conf = await asyncio.to_thread(measure_shift, a, b, centre)
        bx, by, bconf = await asyncio.to_thread(measure_shift, b, c, centre)
        moved, back = (sx, bx) if axis == "pan" else (sy, by)
        both = conf >= min_conf and bconf >= min_conf
        agree = both and moved * back < 0 and abs(abs(moved) - abs(back)) <= _BACK_TOLERANCE * max(abs(moved), abs(back))
        tiny = both and abs(moved) < target[0] / 3 and abs(back) < target[0] / 3
        if notes is not None:
            verdict = "" if agree else (" — barely moved" if tiny else " — out and back disagree, ignored")
            notes.append(f"{axis} step {step:+.3f}: picture moved {moved:+.3f} (match {conf:.2f}), "
                         f"back {back:+.3f} (match {bconf:.2f}){verdict}")
        avg = math.copysign((abs(moved) + abs(back)) / 2, moved) if agree else 0.0
        if agree and target[0] <= abs(avg) <= target[1]:
            return step / avg
        if tiny or (agree and abs(avg) < target[0]):
            too_small = max(too_small, abs(step))
            step *= 3 if abs(avg) < target[0] / 3 else 2  # too small a move to see: make it bigger
        else:
            too_big = min(too_big, abs(step))
            step /= 3                                   # no match / disagreement usually = it slid out of view
        if too_small and math.isfinite(too_big) and too_small < too_big:
            step = math.copysign(math.sqrt(too_small * too_big), step)
        step = max(-0.9, min(0.9, step))
    return None


def _consistent(points: list, notes: list) -> list:
    """Levels must agree: the same direction for pan and for tilt on every level (the majority decides), and the
    view can't get WIDER as zoom goes up (allowing 30 % measuring slack). A level that breaks either is dropped."""
    if len(points) < 2:
        return points
    sign = {k: (1.0 if sum(math.copysign(1, p[k]) for p in points) >= 0 else -1.0)
            for k in ("pan_per_width", "tilt_per_height")}
    out: list = []
    for p in sorted(points, key=lambda q: q["zoom_n"]):
        bad = [f"{k.split('_')[0]} direction flipped" for k in sign if math.copysign(1, p[k]) != sign[k]]
        if not bad and out:
            bad = [f"{k.split('_')[0]} view wider than at lower zoom" for k in sign if abs(p[k]) > abs(out[-1][k]) * 1.3]
        if bad:
            notes.append(f"zoom {p['zoom']:.4g}: dropped — {', '.join(bad)}")
            continue
        out.append(p)
    return out


async def run(hw: Hw, caps: dict, zoom_levels=ZOOM_LEVELS, notes: Optional[list] = None) -> dict:
    """Measure the camera and put it back where it was. Returns the calibration document (raises when not a
    single zoom level could be measured, with what was seen). A level that fails — a refused move, a blurry or
    featureless picture at long zoom — is skipped, not fatal."""
    notes = notes if notes is not None else []
    start = await hw.status()
    if not start or start.get("pan") is None or start.get("tilt") is None:
        raise RuntimeError("the camera doesn't report its position — calibration needs it")
    absolute = bool((caps.get("absolute") or {}).get("pan_tilt"))
    # Tilt test moves go toward the side with more room — for most domes that is DOWN, at the ground, which
    # also has more detail to match than sky.
    tlo, thi = _bounds(caps, "tilt")
    down, up = _room(float(start["tilt"]), tlo, thi)
    tilt_dir = -1.0 if down >= up else 1.0
    zoom_ok = (caps.get("absolute") or {}).get("zoom") and start.get("zoom") is not None
    zlo, zhi = _bounds(caps, "zoom")
    levels = [zlo + (zhi - zlo) * z for z in zoom_levels] if zoom_ok else [start.get("zoom")]
    points, last_pan, last_tilt = [], 0.1, 0.1
    aspect = frame_aspect(await hw.frame())             # the picture's shape: turns a measured height into a width
    geometry = None
    failed_in_a_row = 0
    try:
        for i, z in enumerate(levels):
            if failed_in_a_row >= 2:
                # Two levels in a row with nothing usable: longer zoom only gets softer and darker. Each failed level
                # is dozens of grabs (minutes on a real stream), so stop rather than grind through the rest.
                notes.append(f"zoom {z:.2f} and up: not tried (two levels in a row gave nothing usable)")
                break
            if hw.progress:
                await hw.progress(f"zoom level {i + 1} of {len(levels)}")
            base = {"pan": float(start["pan"]), "tilt": float(start["tilt"]), "zoom": z}
            try:
                if not absolute and zoom_ok:
                    await hw.move_abs(pan=None, tilt=None, zoom=z)
                # The next level's first guess: the last answer scaled for roughly a quarter-frame slide.
                pan_guess = abs(last_pan) * 0.25 if points else 0.05
                ppw = await _measure_axis(hw, absolute, base, "pan", pan_guess, caps, notes)
                tph = await _measure_axis(hw, absolute, base, "tilt",
                                          tilt_dir * (abs(last_tilt) * 0.25 if points else 0.05), caps, notes)
            except Exception as exc:                        # noqa: BLE001 — one level failing is not the end
                notes.append(f"zoom {z:.2f}: {str(exc)[:120]}")
                failed_in_a_row += 1
                continue
            if ppw is None or tph is None:
                notes.append(f"zoom {z:.2f}: skipped (no usable picture shift)")
                log.info("ptz_calibrate.level_skipped", zoom=z, pan=ppw, tilt=tph)
                failed_in_a_row += 1
                continue
            failed_in_a_row = 0
            # Stored against the camera's own zoom units (normalised 0..1 of its range for the model).
            zn = (z - zlo) / (zhi - zlo) if zoom_ok and zhi > zlo else (z or 0.0)
            points.append({"zoom": round(z, 4), "zoom_n": round(zn, 4),
                           "pan_per_width": round(ppw, 6), "tilt_per_height": round(tph, 6)})
            last_pan, last_tilt = ppw, tph
        if absolute and points and points[0]["zoom"] == round(levels[0] or 0.0, 4):
            if hw.progress:
                await hw.progress("measuring angles")
            geometry = await _measure_geometry(hw, caps, start, levels[0], points, aspect, notes)
    finally:
        # Back where it was. Relative-only heads already undid each test move; they only need the zoom back.
        try:
            if absolute:
                await hw.move_abs(pan=float(start["pan"]), tilt=float(start["tilt"]), zoom=start.get("zoom"))
            elif zoom_ok:
                await hw.move_abs(pan=None, tilt=None, zoom=start.get("zoom"))
        except Exception as exc:                        # noqa: BLE001 — report, the caller audits
            log.warning("ptz_calibrate.restore_failed", error=str(exc))
    points = _consistent(points, notes)
    if not points:
        why = "; ".join(notes[-4:]) if notes else ""
        raise RuntimeError("no zoom level gave a usable picture shift — point the camera at a detailed, well-lit "
                           "view and try again" + (f" ({why})" if why else ""))
    if geometry and points[0]["zoom"] != geometry.get("zoom"):
        geometry = None                                 # its zoom-0 reading was dropped as inconsistent
    return {"version": VERSION, "space": "absolute" if absolute else "relative", "points": points,
            "zoom_range": [zlo, zhi], "geometry": geometry, "aspect": round(aspect, 4) if aspect else None,
            "notes": notes[-60:], "measured_at": datetime.now(timezone.utc).isoformat()}


async def _measure_geometry(hw: Hw, caps: dict, start: dict, zoom: Optional[float], points: list,
                            aspect: Optional[float], notes: list) -> Optional[dict]:
    """Pan-per-width at a few more tilts, at the widest zoom, toward the ground (see "REAL ANGLES" above)."""
    p0 = points[0]
    tlo, thi = _bounds(caps, "tilt")
    t_start = float(start["tilt"])
    down_dir = -math.copysign(1, p0["tilt_per_height"])     # units that point the camera at the ground
    down, up = _room(t_start, tlo, thi)
    room = down if down_dir < 0 else up
    if room < 0.05:
        notes.append("angles: no tilt room toward the ground to measure them — straight-line model kept")
        return None
    # The level reading is taken again the same way (small, middle only): mixed methods would differ by the lens's
    # edge stretch (~6 % at 60° wide) and the fit would read that as elevation.
    samples: list = []
    for f in (0.0, *_GEOMETRY_TILTS):
        t = t_start + down_dir * room * f
        base = {"pan": float(start["pan"]), "tilt": t, "zoom": zoom}
        try:
            # Small slides, middle of the picture only: looking down, a pan also TURNS the picture (by the pan
            # angle × sin elevation) and a whole-frame match of a big slide falls apart past ~40° down.
            ppw = await _measure_axis(hw, True, base, "pan", abs((samples or [(0, p0["pan_per_width"])])[-1][1]) * 0.06,
                                      caps, notes,
                                      target=_GEOMETRY_SLIDE, centre=0.5, min_conf=0.3)
        except Exception as exc:                        # noqa: BLE001 — a refused tilt: fewer samples
            notes.append(f"angles: tilt {t:+.3f}: {str(exc)[:80]}")
            continue
        if ppw is not None:
            samples.append((t, abs(ppw)))
    if not samples or samples[0][0] != t_start:
        notes.append("angles: no reading at the starting tilt — straight-line model kept")
        return None
    geo = _geometry(samples, points, t_start, aspect, notes)
    if geo:
        geo["zoom"] = p0["zoom"]
    return geo


# ─── Checking a click ────────────────────────────────────────────────────────

async def verify_centre(hw: Hw, cal: dict, caps: dict, before: bytes, x: float, y: float,
                        status_before: Optional[dict]) -> dict:
    """After a click-to-centre move: did the clicked spot land in the middle? (see "CHECK AFTER EVERY CLICK")

    `before` is the frame the operator clicked on. Returns what it saw: {"checked", "want", "got", "conf",
    "corrected": [x, y] | None, "learned": <new calibration doc> | None, "why"}. Moves the camera at most once
    (the correction); the caller saves "learned"."""
    out: dict = {"checked": False, "corrected": None, "learned": None}
    await _settle(hw)
    # moved_from: the check starts the moment the move is sent, and the stream trails the head — wait for the
    # view to change from the clicked one first, or the old view would look "still" and read as "didn't move".
    after = await _still_frame(hw, moved_from=before)
    if not after:
        out["why"] = "no picture from the stream"
        return out
    a_before, a_after = frame_aspect(before), frame_aspect(after)
    if not a_before or not a_after or abs(a_before - a_after) > 0.05 * a_after:
        # The browser's picture is a different stream shape (some sub-streams are 4:3 crops) — fractions of one
        # frame aren't fractions of the other.
        out["why"] = "the clicked picture and the camera stream differ in shape"
        return out
    sx, sy, conf = await asyncio.to_thread(measure_shift, before, after)
    want = (x / 2, y / 2)
    out.update(want=[round(want[0], 4), round(want[1], 4)], got=[round(sx, 4), round(sy, 4)], conf=round(conf, 3))
    if conf < _CHECK_MIN_CONF:
        out["why"] = f"no clear match (confidence {conf:.2f})"
        return out
    gains: dict = {}
    for axis, w, g in (("pan", want[0], sx), ("tilt", want[1], sy)):
        if abs(w) < 0.08:
            continue                                    # too small a move to say how far off it was
        ratio = g / w
        if not 0.4 <= ratio <= 2.5:
            # The wrong way, or wildly off: more likely a picture that was stale when clicked (the browser's
            # stream trails the head) than a calibration that bad. Neither correct nor learn from it.
            out["why"] = f"implausible {axis} reading ({ratio:.2f}x of planned)"
            return out
        gains[axis] = ratio
    out["checked"] = True
    out["gains"] = {k: round(v, 3) for k, v in gains.items()}
    if gains:
        out["learned"] = learn(cal, (status_before or {}).get("zoom"), gains)
    rx, ry = want[0] - sx, want[1] - sy
    out["residual"] = [round(rx, 4), round(ry, 4)]
    if max(abs(rx), abs(ry)) <= _CHECK_TOL:
        return out
    # One correction for what's left, with this click's own measured over/undershoot taken out of it.
    cx = max(-1.0, min(1.0, 2 * rx / gains.get("pan", 1.0)))
    cy = max(-1.0, min(1.0, 2 * ry / gains.get("tilt", 1.0)))
    now = await hw.status()
    plan = plan_centre(cal, now, cx, cy, None, caps)
    if not plan.get("calibrated"):
        return out
    if plan["mode"] == "absolute":
        await hw.move_abs(pan=plan["pan"], tilt=plan["tilt"], zoom=None)
    else:
        await hw.move_rel(pan=plan["pan"], tilt=plan["tilt"])
    out["corrected"] = [round(cx, 4), round(cy, 4)]
    return out


# ─── Aiming in degrees ───────────────────────────────────────────────────────
#
# "20° left of Home, 10° up" (2026-10-27). The angles calibration measures (`geometry`: degrees per pan and tilt
# unit, the tilt of level) turn camera units into degrees and back. Left/right is measured from Home — the
# camera's own pan zero is wherever the maker put it; up/down from Home or from LEVEL (the one natural zero).
# Accurate to the calibration (about ±5-10 %), clamped to the camera's limits.

def angle_model(cal: Optional[dict]) -> Optional[dict]:
    """Degrees per unit and directions, or None when this calibration has no measured angles."""
    geo = (cal or {}).get("geometry")
    pts = (cal or {}).get("points") or []
    if not geo or not pts:
        return None
    return {"kp": float(geo["kp"]), "kt": float(geo["kt"]), "t0": float(geo["t0"]),
            # +units = picture-right / picture-up when positive (an inverted mount flips both)
            "s_p": math.copysign(1, pts[0]["pan_per_width"]), "s_t": math.copysign(1, pts[0]["tilt_per_height"])}


def _pan_diff(a: float, b: float, caps: dict) -> float:
    """a − b in pan units, the short way round on a full-circle head."""
    lo, hi = _bounds(caps, "pan")
    d = a - b
    return _wrap_pan(d) if lo <= -0.99 and hi >= 0.99 else d


def degrees_of(model: dict, caps: dict, status: Optional[dict], home: Optional[dict]) -> Optional[dict]:
    """Where the head points, in degrees: right of Home, up from Home, and up from level (+ = up)."""
    st = status or {}
    if st.get("pan") is None or st.get("tilt") is None:
        return None
    out = {"elevation": round(model["s_t"] * model["kt"] * (float(st["tilt"]) - model["t0"]), 1)}
    if home and home.get("pan") is not None and home.get("tilt") is not None:
        out["right_of_home"] = round(model["s_p"] * model["kp"] * _pan_diff(float(st["pan"]), float(home["pan"]), caps), 1)
        out["up_from_home"] = round(model["s_t"] * model["kt"] * (float(st["tilt"]) - float(home["tilt"])), 1)
    return out


def aim_target(model: dict, caps: dict, status: dict, home: Optional[dict], right: float, up: float,
               origin: str, up_from_level: bool = False) -> dict:
    """Absolute pan/tilt for "`right`° right / `up`° up" (negative = left / down) from `origin` ("current" or
    "home"); with `up_from_level`, `up` is the elevation above level instead. Clamped to the camera's limits
    ("clamped": True says it couldn't go all the way)."""
    base = home if origin == "home" else status
    if not base or base.get("pan") is None or base.get("tilt") is None:
        raise ValueError("Home position isn't known yet" if origin == "home" else "the camera doesn't report its position")
    pan = float(base["pan"]) + model["s_p"] * right / model["kp"]
    if up_from_level:
        tilt = model["t0"] + model["s_t"] * up / model["kt"]
    else:
        tilt = float(base["tilt"]) + model["s_t"] * up / model["kt"]
    plo, phi = _bounds(caps, "pan")
    tlo, thi = _bounds(caps, "tilt")
    if plo <= -0.99 and phi >= 0.99:
        p2 = _wrap_pan(pan)
    else:
        p2 = max(plo, min(phi, pan))
    t2 = max(tlo, min(thi, tilt))
    return {"pan": round(p2, 5), "tilt": round(t2, 5),
            "clamped": abs(_wrap_pan(p2 - pan)) > 1e-6 or abs(t2 - tilt) > 1e-6}
