"""The scalar this service decides on has not changed.

WHAT THIS REPLACES. detector/algorithm.py used to be a SECOND implementation, and it
was verified against the canonical one by running both over the same frames.
That proof was a comparison between two live implementations, so it died the
moment the duplicate was replaced by a call.

So the proof was frozen first. tests/golden_motion.json holds what the
ORIGINAL algorithm.py produced on 220 deterministic frames, captured before it
was touched (services/frames/scripts/capture_golden.py). This replays them.

A change to the canonical implementation that would alter an operator's motion
events fails here — in this container, needing neither the frame broker nor
the deleted code to exist.

Run: python3 -m pytest tests -q   (from services/motion)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from detector.motion_core import compute_pixel_feature, preprocess_frame  # noqa: E402
from golden_frames import synthetic_frames                              # noqa: E402


@pytest.fixture(scope="module")
def golden() -> dict:
    with open(os.path.join(HERE, "golden_motion.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def replay(golden):
    """Regenerate the frames, and prove they ARE the frames first."""
    g = golden["scalar"]
    digest = hashlib.sha256()
    out, prev = [], None
    for frame in synthetic_frames(golden["frames"]):
        digest.update(np.ascontiguousarray(frame).tobytes())
        cur = preprocess_frame(frame, g["frame_width"], g["frame_height"])
        out.append(None if prev is None
                   else compute_pixel_feature(prev, cur, g["noise_floor"]))
        prev = cur
    return digest.hexdigest(), out


def test_the_inputs_are_the_same_frames(golden, replay):
    """CHECKED FIRST, so a numpy change that alters the seeded generator
    reports itself as changed INPUTS. Without this, the same failure would
    look like the motion algorithm silently breaking."""
    got, _ = replay
    assert got == golden["frames_sha256"], (
        "the synthetic frames changed, so the fixture below is comparing "
        f"different pictures (fixture captured under numpy {golden['numpy']}, "
        f"running {np.__version__})")


def test_the_scalar_matches_the_original_implementation(golden, replay):
    """THE ONE THAT MATTERS. Every sensitivity preset is a threshold on this
    number, so a drift here moves the point at which an operator gets an
    alert — silently, on every camera at once."""
    _, got = replay
    want = golden["scalar"]["values"]
    assert len(got) == len(want)
    bad = [(i, w, g) for i, (w, g) in enumerate(zip(want, got)) if w != g]
    assert not bad, (
        f"{len(bad)} of {len(want)} samples differ from what the original "
        f"algorithm.py produced; first: index {bad[0][0]} "
        f"was {bad[0][1]!r}, now {bad[0][2]!r}")


def test_the_fixture_actually_covers_moving_and_still_frames(golden):
    """A fixture of all-zero samples would pass anything."""
    vals = [v for v in golden["scalar"]["values"] if v is not None]
    assert len(vals) > 100
    assert max(vals) > 0.1, "no frame in the fixture has real motion"
    assert min(vals) < 0.01, "no quiet frame in the fixture"


def test_motion_core_is_the_source_not_a_copy():
    """This service OWNS the implementation; the other two get copies.

    If motion_core.py ever grows the generated banner, the direction of the
    sync has been reversed and edits made here are being overwritten by a
    copy elsewhere — silently, which is how the duplication would come back.
    """
    import inspect

    from detector import motion_core
    src = inspect.getsource(motion_core)
    assert "GENERATED COPY - DO NOT EDIT" not in src,         "motion_core.py is being generated; the sync direction is reversed"
    # And it really is the implementation, not a forwarder.
    assert "cv2.absdiff" in src and "class CanonicalMotion" in src


def test_the_service_calls_the_shared_entry_points():
    """worker.py must reach the measurement through motion_core, not its own
    copy of the four OpenCV calls."""
    import inspect

    from detector import worker
    src = inspect.getsource(worker)
    assert "motion_core" in src
    for forbidden in ("cv2.absdiff", "cv2.GaussianBlur", "count_nonzero"):
        assert forbidden not in src,             f"worker.py reimplements {forbidden!r}"
