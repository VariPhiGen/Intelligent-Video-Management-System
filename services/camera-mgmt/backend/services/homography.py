"""homography.py — the projective map between a camera's frame and a site plan.

A camera dot on the Map tab says where a camera *is*. This module is what lets
the map say what a camera *sees*: a 3x3 homography, fitted from landmark pairs
an operator clicks, carrying any ground-plane point from frame coordinates into
plan coordinates and back.

**Both spaces are normalized 0-1**, matching every other spatial value in this
service — sitemap `calibration` control points, camera placement in
`camera_metadata`, HA device pins, and `analytics_config` regions. Nothing here
knows about pixels, so re-uploading a plan at a different resolution, or moving
a camera to a substream of a different size, both stay correct with no
migration and no rescale.

**Direction matters and is easy to get backwards.** The stored matrix is
FRAME -> MAP, because that is what a fit from (frame_point, map_point) pairs
produces directly and what the coverage footprint needs. Zone projection wants
the other direction and uses `invert()`. They are not interchangeable, and the
failure when they are swapped is not an exception — it is a plausible-looking
polygon in the wrong place, so the parameter names here say which space they
are in rather than `x`/`y`.

**Fitting happens in the browser, not here.** Solving H from n>=4 pairs needs a
least-squares/SVD step, and this service deliberately carries no numeric
dependency (see requirements.txt — every line there justifies itself). The
operator is already clicking pairs in the UI, where the reprojection error
updating live as the fourth pair lands beats a round trip. What this module
owns is the half that must *not* be delegated to a client: re-deriving the
error from the stored pairs, and refusing a matrix that is degenerate however
well it fits them. Applying a homography is a 3x3 multiply and a divide, so
that asymmetry costs nothing — `apply` and `invert` below are exact and
dependency-free.

**Ground-plane assumption.** A homography is exact only for points on a single
plane. Feet on flat ground are that plane; a person's head, the top of a
stacked pallet, or ground that slopes away are not, and their error grows with
height and with distance from the landmarks. This is why landmarks must be
picked at ground level — pole bases, fence-post feet, painted marks, never roof
corners — and why the plan's accuracy target is 0.5-1 m rather than
centimetres.

**The horizon is a real edge case, not a theoretical one.** The set of frame
points where the third (homogeneous) coordinate reaches zero is the horizon
line; beyond it, ground-plane points map behind the camera. Any camera with sky
in the top of frame has a horizon inside its image, so a frame corner that
projects to infinity is *normal* and must not fail a registration. The footprint
below therefore samples the frame border, drops samples at or past the horizon,
and clips what survives to the plan — which is also the honest answer, since
the area past the horizon genuinely has no coverage on this plan.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

Matrix3 = list[list[float]]
Point = tuple[float, float]

# Homogeneous w below this is treated as "at or past the horizon". Not a
# tolerance on rounding: w is a continuous function of position and passes
# through zero exactly at the horizon, so any cut here is a choice of how close
# to the horizon we still trust a point. 1e-6 in normalized units puts the
# cutoff far enough out that the projected point is already thousands of plan
# widths away — well past anything drawable.
_W_EPS = 1e-6

# A frame border sampled at this many points per edge before projection. Enough
# that the clipped footprint follows the curve a perspective transform puts in
# a straight frame edge near the horizon, cheap enough to run per camera on
# every map render.
_BORDER_SAMPLES_PER_EDGE = 24

# Rejection thresholds for `validate`. Each is a failure seen in practice with
# a matrix that fits its own landmark pairs perfectly:
#   - a near-singular matrix collapses the whole frame onto a line or a point
#   - an inverted footprint means the pairs were entered with frame and map
#     swapped, which fits fine and projects mirror-imaged
#   - a footprint larger than the plan many times over means the landmarks were
#     nearly collinear, so the fit is unconstrained along one axis
_MIN_ABS_DETERMINANT = 1e-9
_MAX_FOOTPRINT_AREA = 64.0  # plan areas; the unit square is 1.0


class HomographyError(ValueError):
    """A homography that cannot be used, with a reason fit for an operator.

    Deliberately a ValueError subclass: the router turns it straight into a 422
    and the message is shown in the calibration card, so it is written for the
    person placing pins, not for a log grep.
    """


def apply(h: Matrix3, x: float, y: float) -> Point:
    """Project one point through `h`, returning the dehomogenized result.

    Raises HomographyError when the point lies at or past the horizon, because
    there is no finite answer to return and a caller that silently substituted
    one would place a zone somewhere arbitrary. Callers that expect to meet the
    horizon (the footprint sampler) catch this rather than pre-checking, since
    the check *is* the division.
    """
    w = h[2][0] * x + h[2][1] * y + h[2][2]
    if abs(w) < _W_EPS:
        raise HomographyError("point projects to the horizon (no finite position)")
    u = (h[0][0] * x + h[0][1] * y + h[0][2]) / w
    v = (h[1][0] * x + h[1][1] * y + h[1][2]) / w
    return u, v


def determinant(h: Matrix3) -> float:
    """Determinant of the 3x3. Near zero means the transform is degenerate —
    it has collapsed a dimension, so it cannot be inverted and does not
    describe a view of a plane."""
    return (
        h[0][0] * (h[1][1] * h[2][2] - h[1][2] * h[2][1])
        - h[0][1] * (h[1][0] * h[2][2] - h[1][2] * h[2][0])
        + h[0][2] * (h[1][0] * h[2][1] - h[1][1] * h[2][0])
    )


def invert(h: Matrix3) -> Matrix3:
    """Invert the 3x3 via its adjugate — the map->frame direction.

    Written out rather than pulled from a library because it is nine cofactors
    and this service carries no numeric dependency. A homography is defined up
    to scale, so dividing by the determinant is presentational only; it keeps
    the numbers in a readable range for anyone reading a stored row.
    """
    det = determinant(h)
    if abs(det) < _MIN_ABS_DETERMINANT:
        raise HomographyError("transform is degenerate and cannot be inverted")
    c = [
        [h[1][1] * h[2][2] - h[1][2] * h[2][1],
         h[0][2] * h[2][1] - h[0][1] * h[2][2],
         h[0][1] * h[1][2] - h[0][2] * h[1][1]],
        [h[1][2] * h[2][0] - h[1][0] * h[2][2],
         h[0][0] * h[2][2] - h[0][2] * h[2][0],
         h[0][2] * h[1][0] - h[0][0] * h[1][2]],
        [h[1][0] * h[2][1] - h[1][1] * h[2][0],
         h[0][1] * h[2][0] - h[0][0] * h[2][1],
         h[0][0] * h[1][1] - h[0][1] * h[1][0]],
    ]
    return [[value / det for value in row] for row in c]


def reprojection_rms(h: Matrix3, pairs: Sequence[tuple[float, float, float, float]]) -> float:
    """RMS distance, in plan units, between each landmark's clicked map
    position and where `h` actually sends its frame position.

    This is the number that decides whether a registration is trustworthy, and
    it is recomputed here rather than accepted from the client precisely
    because the client is the one claiming the matrix is good.

    Normalized plan units, so it converts to metres only when the sitemap
    carries `calibration`. Callers that report it to an operator must say which
    they are showing — an unlabelled 0.02 reads as two centimetres and may be
    two metres.

    A pair at the horizon contributes an infinite error by construction, and is
    reported as such rather than skipped: it means the operator put a landmark
    somewhere this transform cannot describe, which is exactly what they need
    told.
    """
    if not pairs:
        raise HomographyError("no landmark pairs to measure against")
    total = 0.0
    for frame_x, frame_y, map_x, map_y in pairs:
        try:
            u, v = apply(h, frame_x, frame_y)
        except HomographyError:
            return math.inf
        total += (u - map_x) ** 2 + (v - map_y) ** 2
    return math.sqrt(total / len(pairs))


def _polygon_area(points: Sequence[Point]) -> float:
    """Signed shoelace area. The sign carries the winding direction, which is
    what catches a frame/map mix-up: a correctly entered registration preserves
    orientation, a swapped one mirrors it and comes back negative."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _clip_to_unit_square(points: Sequence[Point]) -> list[Point]:
    """Sutherland-Hodgman clip against the plan bounds.

    The plan is the unit square and is convex, which is the one precondition
    this algorithm needs. Used instead of clamping coordinates into range
    because clamping drags every out-of-bounds vertex onto the border and
    reports a footprint shaped nothing like the real one — it would show a
    camera covering a corner of the site it cannot see.
    """
    def inside(p: Point, edge: int) -> bool:
        x, y = p
        return (x >= 0.0, x <= 1.0, y >= 0.0, y <= 1.0)[edge]

    def intersect(p: Point, q: Point, edge: int) -> Point:
        (x1, y1), (x2, y2) = p, q
        if edge in (0, 1):                      # vertical boundary at x = 0 or 1
            bound = 0.0 if edge == 0 else 1.0
            t = (bound - x1) / (x2 - x1) if x2 != x1 else 0.0
            return bound, y1 + t * (y2 - y1)
        bound = 0.0 if edge == 2 else 1.0        # horizontal boundary at y = 0 or 1
        t = (bound - y1) / (y2 - y1) if y2 != y1 else 0.0
        return x1 + t * (x2 - x1), bound

    output = list(points)
    for edge in range(4):
        if not output:
            return []
        buf: list[Point] = []
        for i, current in enumerate(output):
            previous = output[i - 1]
            cur_in, prev_in = inside(current, edge), inside(previous, edge)
            if cur_in:
                if not prev_in:
                    buf.append(intersect(previous, current, edge))
                buf.append(current)
            elif prev_in:
                buf.append(intersect(previous, current, edge))
        output = buf
    return output


def _frame_border(samples_per_edge: int = _BORDER_SAMPLES_PER_EDGE) -> list[Point]:
    """The frame outline walked clockwise in normalized frame coordinates.

    Sampled rather than reduced to four corners because perspective bends a
    straight frame edge once it approaches the horizon: the four-corner version
    of a footprint cuts the corner off a view that actually reaches much
    further up the site.
    """
    step = 1.0 / samples_per_edge
    border: list[Point] = []
    for i in range(samples_per_edge):
        border.append((i * step, 0.0))          # top edge, left to right
    for i in range(samples_per_edge):
        border.append((1.0, i * step))          # right edge, down
    for i in range(samples_per_edge):
        border.append((1.0 - i * step, 1.0))    # bottom edge, right to left
    for i in range(samples_per_edge):
        border.append((0.0, 1.0 - i * step))    # left edge, up
    return border


# How far a vertex may sit off the line joining its neighbours and still be
# dropped as redundant, in plan units. Sized well under the 0.5-1 m accuracy
# the plan targets for a site plan of any realistic extent, so simplification
# can never move a footprint edge by more than the registration error already
# allows.
_SIMPLIFY_TOLERANCE = 1e-4


def _drop_collinear(points: Sequence[Point], tolerance: float = _SIMPLIFY_TOLERANCE) -> list[Point]:
    """Remove vertices that lie on the line between their neighbours.

    A sampled border comes back with a vertex every few degrees, so a view
    whose footprint is a plain quadrilateral arrives as ~96 points. They are
    all correct and all redundant: they cost storage on every registration and
    path length on every map render, for a shape a reader cannot distinguish
    from four corners. Curvature near the horizon is preserved, because those
    vertices are genuinely off the chord and fail the test below.

    Distance is measured as the triangle area over the base length — the
    perpendicular distance from the middle point to the line through its
    neighbours — with a degenerate (zero-length) base treated as collinear,
    since a duplicated point carries no shape either.
    """
    if len(points) < 4:
        return list(points)
    kept: list[Point] = []
    for i, (x, y) in enumerate(points):
        ax, ay = points[i - 1]
        bx, by = points[(i + 1) % len(points)]
        base = math.hypot(bx - ax, by - ay)
        if base == 0.0:
            continue
        # Twice the triangle area, over the base: the perpendicular offset.
        offset = abs((bx - ax) * (ay - y) - (ax - x) * (by - ay)) / base
        if offset > tolerance:
            kept.append((x, y))
    # Guard against simplifying a shape out of existence: a footprint so close
    # to a line that nothing survives is degenerate anyway, and the caller's
    # area checks should see the original rather than an empty list.
    return kept if len(kept) >= 3 else list(points)


def footprint(h: Matrix3) -> list[Point]:
    """The area of the plan this view covers, as a polygon in plan coordinates.

    Samples the frame border, drops everything at or past the horizon, then
    clips to the plan. Both losses are honest rather than approximations: past
    the horizon there is no ground position to claim, and outside the plan the
    coverage exists but this map does not describe it.

    Vertices left collinear by the sampling are dropped before returning, so a
    plain quadrilateral footprint stores as four points rather than the ~96 the
    border sampler produced, while curvature near the horizon survives.

    Returns an empty list when the view and the plan do not overlap at all —
    a camera registered against the wrong floor's plan, which the caller
    surfaces rather than storing a footprint nobody can see.
    """
    projected: list[Point] = []
    for x, y in _frame_border():
        try:
            projected.append(apply(h, x, y))
        except HomographyError:
            continue                            # past the horizon: no ground point exists
    if len(projected) < 3:
        return []
    return _drop_collinear(_clip_to_unit_square(projected))


def validate(
    h: Matrix3,
    pairs: Sequence[tuple[float, float, float, float]],
    *,
    max_rms: float,
) -> tuple[float, list[Point]]:
    """Accept or reject a registration, returning its error and footprint.

    Ordered cheapest-first, and each check corresponds to a way a matrix that
    fits its own pairs perfectly can still be unusable:

    1. shape and finiteness — a malformed or NaN-bearing matrix poisons every
       later comparison silently, since NaN fails every inequality
    2. determinant — a near-singular transform has collapsed the frame onto a
       line, so nothing downstream can be inverted for zone projection
    3. reprojection error — the operator's own landmarks disagree with it
    4. footprint exists — the view does not meet this plan
    5. footprint winding — negative area means frame and map points were
       entered the wrong way round, which fits beautifully and projects
       mirrored
    6. footprint size — a wildly oversized footprint means near-collinear
       landmarks left the fit unconstrained along one axis

    Raises HomographyError with an operator-facing reason; returns
    (rms, footprint) when the registration is usable.
    """
    if len(h) != 3 or any(len(row) != 3 for row in h):
        raise HomographyError("transform must be a 3x3 matrix")
    if any(not math.isfinite(value) for row in h for value in row):
        raise HomographyError("transform contains a non-finite value")

    if abs(determinant(h)) < _MIN_ABS_DETERMINANT:
        raise HomographyError(
            "landmarks are too close to a straight line to fix the view — "
            "spread them across the frame"
        )

    rms = reprojection_rms(h, pairs)
    if not math.isfinite(rms):
        raise HomographyError("a landmark projects to the horizon — move it onto visible ground")
    if rms > max_rms:
        raise HomographyError(
            f"landmarks disagree with the fitted view by {rms:.4f} plan units "
            f"(limit {max_rms:.4f}) — recheck the pins, or add one further from the others"
        )

    area = _polygon_area(prints := footprint(h))
    if not prints:
        raise HomographyError("this view does not overlap the plan — is it the right floor plan?")
    if area < 0:
        raise HomographyError(
            "the view comes out mirrored — frame and plan points look swapped"
        )
    if area > _MAX_FOOTPRINT_AREA:
        raise HomographyError(
            "the fitted view covers far more than the plan — landmarks are too close together"
        )
    return rms, prints


def project_polygon(h: Matrix3, points: Iterable[Point]) -> list[Point]:
    """Carry a polygon through `h`, whichever direction `h` runs.

    Used with `invert(stored)` to bring a plan-space geofence into a camera's
    frame. Raises rather than dropping a vertex: a polygon missing one corner
    is still a polygon, would still be drawn, and would silently guard the
    wrong area — which is the failure this whole module exists to prevent.
    """
    return [apply(h, x, y) for x, y in points]


# ─── Plan <-> world scale ─────────────────────────────────────────────────────
#
# The other half of the spatial picture. Everything above maps a camera frame to
# a plan; this maps plan units to metres, using the georeferencing control
# points a sitemap may carry (migration 026). Kept in this module so all the
# dependency-free spatial maths lives in one place.

# IUGG mean Earth radius. Any of the common values differs by well under the
# accuracy this is used at, so the choice is not load-bearing.
_EARTH_RADIUS_M = 6371008.8


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def plan_scale_metres(calibration: Sequence[dict] | None) -> float | None:
    """Metres per plan unit, or None when the plan is not georeferenced.

    Derived from the two control points furthest apart, because a short
    baseline magnifies the click error in both of them: two pins a few percent
    of the plan apart can be a metre out and still look right, and the ratio
    inherits all of it.

    **Approximate on purpose, and display-only.** Plan coordinates are
    normalized 0-1 on each axis independently, so on a plan image that is not
    square, one unit of x and one unit of y are different real distances; this
    returns a single figure that averages them. That is why the registration
    quality gate (`REGISTRATION_MAX_RMS`) is expressed in normalized units and
    checked there, while metres appear only where a person reads them. A limit
    in metres would be the more natural thing to state and the less honest
    thing to enforce, since most plans carry no calibration at all and it would
    silently stop applying on exactly those.

    Returns None rather than a guess when there are fewer than two control
    points or they coincide — callers show "not georeferenced" instead of a
    number nobody should act on.
    """
    if not calibration or len(calibration) < 2:
        return None
    best: tuple[float, float] | None = None   # (plan distance, metres)
    for i in range(len(calibration)):
        for j in range(i + 1, len(calibration)):
            a, b = calibration[i], calibration[j]
            try:
                plan_d = math.hypot(float(b["x"]) - float(a["x"]), float(b["y"]) - float(a["y"]))
                world_d = _haversine_m(
                    float(a["lat"]), float(a["lng"]), float(b["lat"]), float(b["lng"]),
                )
            except (KeyError, TypeError, ValueError):
                # A malformed control point is skipped rather than fatal: the
                # remaining pair may still give a usable scale, and a plan the
                # operator can still read beats a 500 on a display figure.
                continue
            if best is None or plan_d > best[0]:
                best = (plan_d, world_d)
    if best is None or best[0] <= 0.0 or best[1] <= 0.0:
        return None
    return best[1] / best[0]
