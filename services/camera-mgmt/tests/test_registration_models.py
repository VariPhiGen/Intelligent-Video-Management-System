"""Request-model validation for camera-to-plan registration (backend/models.py).

The geometric checks live in test_homography.py. These cover the layer in front
of them: what a malformed or hostile request body is allowed to become before
any geometry runs. Both matter, and they fail differently — a body that slips
through here reaches `validate()` with the wrong shape entirely, which is a 500
rather than the operator-facing 422 the calibration card is written to show.

Run via the containerized pytest command in the plan's Global Constraints.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import (  # noqa: E402
    CameraRegistrationBody,
    LandmarkPair,
    REGISTRATION_MAX_PAIRS,
    REGISTRATION_MAX_RMS,
    REGISTRATION_MIN_PAIRS,
)
from pydantic import ValidationError  # noqa: E402

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _pairs(n: int) -> list[dict]:
    """n distinct in-range landmark pairs. Values are irrelevant here — these
    tests are about arity and bounds, never about whether the fit is any good."""
    return [{"fx": i / 100, "fy": i / 100, "mx": i / 100, "my": i / 100} for i in range(n)]


# ─── LandmarkPair ────────────────────────────────────────────────────────────

def test_landmark_accepts_the_full_normalized_range():
    """0 and 1 are both valid: a landmark can sit exactly on a frame edge or a
    plan corner, and rejecting the boundary would quietly forbid pinning the
    corner of a yard."""
    LandmarkPair(fx=0.0, fy=0.0, mx=1.0, my=1.0)


@pytest.mark.parametrize("field", ["fx", "fy", "mx", "my"])
@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_landmark_rejects_out_of_range(field, bad):
    """Everything spatial in this service is normalized 0-1. A value outside it
    is either a pixel coordinate sent by mistake or a click captured outside
    the image, and both project somewhere arbitrary."""
    values = {"fx": 0.5, "fy": 0.5, "mx": 0.5, "my": 0.5, field: bad}
    with pytest.raises(ValidationError):
        LandmarkPair(**values)


def test_landmark_rejects_unknown_fields():
    """`extra="forbid"` so a renamed or misspelled key fails loudly instead of
    being silently dropped and leaving the pair at a default."""
    with pytest.raises(ValidationError):
        LandmarkPair(fx=0.5, fy=0.5, mx=0.5, my=0.5, z=0.5)


# ─── CameraRegistrationBody ──────────────────────────────────────────────────

def test_minimum_pairs_is_five_not_four():
    """Four pairs DETERMINE a homography; five are needed to check one.

    At exactly four the solve is exact, so the fit passes through every landmark
    and the reprojection error reads zero regardless of where they were clicked
    — measured at 0.000000000000 with a pin moved a fifth of the plan away. The
    RMS gate is therefore vacuous at four pairs, and would accept an
    arbitrarily wrong registration with a perfect score. The fifth pair is the
    entire check, which is why the floor is five and not the mathematical
    minimum.
    """
    assert REGISTRATION_MIN_PAIRS == 5
    for count in (3, 4):
        with pytest.raises(ValidationError):
            CameraRegistrationBody(
                sitemap_id=1, landmark_pairs=_pairs(count), homography=IDENTITY,
            )
    CameraRegistrationBody(sitemap_id=1, landmark_pairs=_pairs(5), homography=IDENTITY)


def test_pair_count_is_capped():
    with pytest.raises(ValidationError):
        CameraRegistrationBody(
            sitemap_id=1,
            landmark_pairs=_pairs(REGISTRATION_MAX_PAIRS + 1),
            homography=IDENTITY,
        )


def test_preset_token_defaults_to_the_fixed_view():
    """'' is the fixed view every camera has. The column exists from day one so
    PTZ multi-preset registration adds rows rather than a schema change."""
    body = CameraRegistrationBody(
        sitemap_id=1, landmark_pairs=_pairs(5), homography=IDENTITY,
    )
    assert body.preset_token == ""


@pytest.mark.parametrize("bad", [
    [[1.0, 0.0], [0.0, 1.0]],                                  # 2x2
    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],                        # 2 rows
    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0]],            # ragged
    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 1.0]],  # 4 rows
])
def test_homography_shape_is_enforced(bad):
    with pytest.raises(ValidationError):
        CameraRegistrationBody(sitemap_id=1, landmark_pairs=_pairs(5), homography=bad)


def test_body_rejects_unknown_fields():
    """Guards against a client sending a precomputed `reprojection_error` or
    `footprint` and having it silently ignored — both are derived here on
    purpose, and a request that thinks otherwise should be told."""
    with pytest.raises(ValidationError):
        CameraRegistrationBody(
            sitemap_id=1, landmark_pairs=_pairs(5), homography=IDENTITY,
            reprojection_error=0.0,
        )


def test_quality_gate_is_expressed_in_plan_units():
    """The limit is deliberately normalized rather than metres: most plans carry
    no georeferencing, and a metre threshold would silently stop applying on
    exactly those. ~0.02 of a plan is ~1-2 m on the site sizes this runs on,
    bracketing the plan's 0.5-1 m ground-plane target."""
    assert 0.0 < REGISTRATION_MAX_RMS < 1.0
