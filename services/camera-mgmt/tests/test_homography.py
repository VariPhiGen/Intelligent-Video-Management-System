"""Unit tests for the frame <-> plan projective map (backend/services/homography.py).

This module decides whether a camera's coverage on the site plan is real or
plausible-looking nonsense, and the failure mode it guards against is not an
exception — it is a polygon drawn confidently in the wrong place. So the tests
below concentrate on the transforms that *fit their own landmarks perfectly*
and are still unusable: mirrored, collapsed, or unconstrained.

Run via the containerized pytest command in the plan's Global Constraints.
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.homography import (  # noqa: E402
    HomographyError,
    _polygon_area,
    apply,
    determinant,
    footprint,
    invert,
    plan_scale_metres,
    project_polygon,
    reprojection_rms,
    validate,
)

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

# A camera looking at the ground at an angle: the third row is non-zero, so the
# far half of the frame compresses and a horizon exists inside the image. This
# is the shape every real registration has; the identity above is only useful
# for checking the algebra.
PERSPECTIVE = [[1.0, 0.0, 0.0], [0.0, 0.35, 0.30], [0.0, -0.75, 1.0]]


def _pairs_from(h, points):
    """Build exact landmark pairs by pushing frame points through `h`.

    Gives a fit with zero error by construction, which is what isolates the
    geometric checks: anything these tests reject is rejected for its shape,
    not because the landmarks disagreed.
    """
    return [(x, y, *apply(h, x, y)) for x, y in points]


# ─── Algebra ─────────────────────────────────────────────────────────────────

def test_identity_is_a_no_op():
    assert apply(IDENTITY, 0.25, 0.75) == (0.25, 0.75)


def test_invert_round_trips_a_perspective_view():
    """The stored matrix runs frame -> plan and zone projection needs the
    reverse, so an inverse that does not round-trip would place every zone
    somewhere wrong while looking entirely reasonable."""
    inverse = invert(PERSPECTIVE)
    for x, y in [(0.1, 0.1), (0.5, 0.5), (0.9, 0.2), (0.3, 0.85)]:
        u, v = apply(PERSPECTIVE, x, y)
        back_x, back_y = apply(inverse, u, v)
        assert back_x == pytest.approx(x, abs=1e-9)
        assert back_y == pytest.approx(y, abs=1e-9)


def test_singular_matrix_cannot_be_inverted():
    """A collapsed transform has no inverse, so zone projection is impossible —
    it must fail loudly here rather than divide by a near-zero later."""
    singular = [[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [1.0, 1.0, 1.0]]
    assert abs(determinant(singular)) < 1e-9
    with pytest.raises(HomographyError, match="degenerate"):
        invert(singular)


def test_point_on_the_horizon_raises_rather_than_returning_a_number():
    """w == 0 is the horizon. There is no finite ground position there, and a
    substituted one would silently place a zone at an arbitrary distance."""
    horizon_y = 1.0 / 0.75  # where PERSPECTIVE's third row reaches zero
    with pytest.raises(HomographyError, match="horizon"):
        apply(PERSPECTIVE, 0.5, horizon_y)


# ─── Reprojection error ──────────────────────────────────────────────────────

def test_exact_pairs_measure_zero_error():
    pairs = _pairs_from(PERSPECTIVE, [(0.1, 0.9), (0.9, 0.9), (0.9, 0.5), (0.1, 0.5)])
    assert reprojection_rms(PERSPECTIVE, pairs) == pytest.approx(0.0, abs=1e-12)


def test_error_is_rms_not_worst_case():
    """One landmark off by d among n contributes d/sqrt(n), not d. Asserted
    because the difference decides whether a fourth sloppy pin fails a
    registration that three good ones would pass."""
    pairs = list(_pairs_from(IDENTITY, [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]))
    fx, fy, mx, my = pairs[0]
    pairs[0] = (fx, fy, mx + 0.04, my)
    assert reprojection_rms(IDENTITY, pairs) == pytest.approx(0.04 / math.sqrt(4), abs=1e-12)


def test_a_landmark_at_the_horizon_reports_infinite_error():
    """Reported rather than skipped: it means the operator pinned a landmark
    somewhere this view cannot describe, which is what they need told."""
    pairs = [(0.5, 1.0 / 0.75, 0.5, 0.5)]
    assert math.isinf(reprojection_rms(PERSPECTIVE, pairs))


def test_no_pairs_is_an_error_not_a_zero():
    """Zero would read as a flawless fit; there is simply nothing to measure."""
    with pytest.raises(HomographyError):
        reprojection_rms(IDENTITY, [])


# ─── Footprint ───────────────────────────────────────────────────────────────

def test_identity_footprint_is_the_whole_plan_in_four_points():
    """Also pins the simplification: the border sampler emits ~96 vertices and
    a rectangle must not be stored as 96 points on every registration."""
    fp = footprint(IDENTITY)
    assert len(fp) == 4
    assert abs(_polygon_area(fp)) == pytest.approx(1.0, abs=1e-6)


def test_footprint_is_clipped_to_the_plan_not_clamped():
    """A view reaching past the plan edge must lose the outside area, not drag
    those vertices onto the border — clamping reports a camera covering a
    corner of the site it cannot see."""
    shifted = [[1.0, 0.0, 0.6], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    fp = footprint(shifted)
    assert fp, "a half-overlapping view still has a footprint"
    assert all(-1e-9 <= x <= 1.0 + 1e-9 and -1e-9 <= y <= 1.0 + 1e-9 for x, y in fp)
    # 40% of the frame remains on the plan.
    assert abs(_polygon_area(fp)) == pytest.approx(0.4, abs=1e-3)


def test_view_entirely_off_the_plan_has_no_footprint():
    """Registering against the wrong floor's plan. An empty footprint is what
    lets the caller say so instead of storing coverage nobody can see."""
    far_away = [[1.0, 0.0, 5.0], [0.0, 1.0, 5.0], [0.0, 0.0, 1.0]]
    assert footprint(far_away) == []


def test_perspective_footprint_survives_its_own_horizon():
    """The horizon truncates the view; it must not empty it. A camera with sky
    in frame is the normal case, not an error."""
    fp = footprint(PERSPECTIVE)
    assert len(fp) >= 3
    assert abs(_polygon_area(fp)) > 0.0


# ─── validate: the transforms that fit perfectly and are still wrong ─────────

def test_validate_accepts_a_clean_registration():
    pairs = _pairs_from(PERSPECTIVE, [(0.1, 0.95), (0.9, 0.95), (0.9, 0.6), (0.1, 0.6)])
    rms, fp = validate(PERSPECTIVE, pairs, max_rms=0.02)
    assert rms == pytest.approx(0.0, abs=1e-12)
    assert len(fp) >= 3


def test_validate_rejects_a_mirrored_registration():
    """Frame and plan points entered the wrong way round. This fits its own
    landmarks exactly and projects every zone mirror-imaged, so nothing but the
    winding direction catches it."""
    mirrored = [[-1.0, 0.0, 1.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    pairs = _pairs_from(mirrored, [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)])
    with pytest.raises(HomographyError, match="mirrored"):
        validate(mirrored, pairs, max_rms=0.02)


def test_validate_rejects_a_collapsed_transform():
    """Near-collinear landmarks leave the fit unconstrained along one axis."""
    collapsed = [[1.0, 0.0, 0.0], [0.0, 1e-12, 0.0], [0.0, 0.0, 1.0]]
    pairs = [(0.1, 0.1, 0.1, 0.0), (0.9, 0.1, 0.9, 0.0),
             (0.9, 0.9, 0.9, 0.0), (0.1, 0.9, 0.1, 0.0)]
    with pytest.raises(HomographyError, match="straight line"):
        validate(collapsed, pairs, max_rms=0.02)


def test_validate_rejects_landmarks_that_disagree():
    pairs = list(_pairs_from(IDENTITY, [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]))
    fx, fy, mx, my = pairs[0]
    pairs[0] = (fx, fy, mx + 0.3, my)
    with pytest.raises(HomographyError, match="disagree"):
        validate(IDENTITY, pairs, max_rms=0.02)


def test_validate_rejects_a_non_finite_matrix():
    """NaN fails every inequality, so an unchecked NaN passes every later
    threshold silently."""
    broken = [[float("nan"), 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    with pytest.raises(HomographyError, match="non-finite"):
        validate(broken, [(0.1, 0.1, 0.1, 0.1)], max_rms=0.02)


def test_validate_rejects_a_view_that_misses_the_plan():
    far_away = [[1.0, 0.0, 5.0], [0.0, 1.0, 5.0], [0.0, 0.0, 1.0]]
    pairs = _pairs_from(far_away, [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)])
    with pytest.raises(HomographyError, match="does not overlap"):
        validate(far_away, pairs, max_rms=0.02)


# ─── Zone projection ─────────────────────────────────────────────────────────

def test_project_polygon_round_trips_through_the_inverse():
    """The path a real zone takes: drawn on the plan, projected into the frame.
    Round-tripping proves the two directions agree."""
    plan_zone = [(0.20, 0.70), (0.60, 0.70), (0.60, 0.90), (0.20, 0.90)]
    in_frame = project_polygon(invert(PERSPECTIVE), plan_zone)
    back = project_polygon(PERSPECTIVE, in_frame)
    for (ax, ay), (bx, by) in zip(plan_zone, back):
        assert ax == pytest.approx(bx, abs=1e-9)
        assert ay == pytest.approx(by, abs=1e-9)


def test_project_polygon_raises_rather_than_dropping_a_vertex():
    """A polygon short one corner is still a polygon and would still be drawn —
    guarding the wrong area, silently. That is the whole failure this module
    exists to prevent, so it must raise."""
    with pytest.raises(HomographyError):
        project_polygon(PERSPECTIVE, [(0.1, 0.1), (0.5, 1.0 / 0.75), (0.9, 0.9)])


# ─── Plan scale ──────────────────────────────────────────────────────────────

def test_scale_is_none_without_georeferencing():
    """None, not zero: an uncalibrated plan cannot answer the question, and a
    zero would read as a perfect fit in the metre column."""
    assert plan_scale_metres(None) is None
    assert plan_scale_metres([]) is None
    assert plan_scale_metres([{"x": 0.1, "y": 0.1, "lat": 12.0, "lng": 77.0}]) is None


def test_scale_converts_plan_units_to_metres():
    """Two pins half a plan apart on a ~100 m site: the 0.02 normalized limit
    should land near the plan's 0.5-1 m ground-plane target."""
    # ~0.000899 degrees of latitude is ~100 m.
    cal = [{"x": 0.25, "y": 0.5, "lat": 12.0, "lng": 77.0},
           {"x": 0.75, "y": 0.5, "lat": 12.000899, "lng": 77.0}]
    scale = plan_scale_metres(cal)
    assert scale == pytest.approx(200.0, rel=0.02)   # 100 m across 0.5 plan units
    assert 0.02 * scale == pytest.approx(4.0, rel=0.02)


def test_scale_uses_the_longest_baseline():
    """A short baseline magnifies click error in both pins, so the widest pair
    must win rather than whichever happens to come first."""
    cal = [
        {"x": 0.50, "y": 0.5, "lat": 12.0, "lng": 77.0},
        {"x": 0.51, "y": 0.5, "lat": 12.0, "lng": 77.0},        # coincident in world
        {"x": 0.00, "y": 0.5, "lat": 12.0, "lng": 77.0},
        {"x": 1.00, "y": 0.5, "lat": 12.000899, "lng": 77.0},
    ]
    assert plan_scale_metres(cal) == pytest.approx(100.0, rel=0.02)


def test_malformed_control_point_is_skipped_not_fatal():
    """A plan the operator can still read beats a 500 on a display figure."""
    cal = [
        {"x": 0.0, "y": 0.5, "lat": 12.0, "lng": 77.0},
        {"x": 1.0, "y": 0.5, "lat": 12.000899, "lng": 77.0},
        {"x": 0.4, "y": 0.5, "lat": None, "lng": 77.0},
    ]
    assert plan_scale_metres(cal) == pytest.approx(100.0, rel=0.02)
