"""The gate still decides exactly what it used to.

WHAT THIS REPLACES. index/motion.py used to BE an implementation, verified
against the canonical one by running both over the same frames. That proof was
a comparison between two live implementations, so it died the moment the
duplicate was replaced by a call.

So the proof was frozen first. tests/golden_motion.json holds what the
ORIGINAL MotionGate produced on 220 deterministic frames — every branch it can
take — captured before it was touched (services/frames/scripts/
capture_golden.py). This replays them.

A change to the canonical implementation that would send different pixels to
the detector fails here, in this container, needing neither the frame broker
nor the deleted code to exist.

Run: python3 -m pytest tests -q   (from services/analytics)
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

from golden_frames import synthetic_frames                            # noqa: E402
from analytics.motion import MotionGate                                   # noqa: E402


@pytest.fixture(scope="module")
def golden() -> dict:
    with open(os.path.join(HERE, "golden_motion.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def replay(golden):
    """Regenerate the frames, and prove they ARE the frames first."""
    gate = MotionGate(**golden["regions"]["params"])
    digest = hashlib.sha256()
    out = []
    for frame in synthetic_frames(golden["frames"]):
        digest.update(np.ascontiguousarray(frame).tobytes())
        r = gate.evaluate(frame)
        out.append({
            "regions": None if r.regions is None else [list(x) for x in r.regions],
            "reason": r.reason,
        })
    return digest.hexdigest(), out, gate


def test_the_inputs_are_the_same_frames(golden, replay):
    """CHECKED FIRST, so a numpy change that alters the seeded generator
    reports itself as changed INPUTS rather than looking like the gate
    silently breaking."""
    got, _, _ = replay
    assert got == golden["frames_sha256"], (
        "the synthetic frames changed, so the fixture below is comparing "
        f"different pictures (captured under numpy {golden['numpy']}, "
        f"running {np.__version__})")


def test_every_region_matches_the_original_gate(golden, replay):
    """THE ONE THAT MATTERS. These coordinates are the crops the detector
    runs on, so a drift here changes what gets indexed and what is missed."""
    _, got, _ = replay
    want = golden["regions"]["values"]
    assert len(got) == len(want)
    bad = [(i, w, g) for i, (w, g) in enumerate(zip(want, got))
           if w["regions"] != g["regions"]]
    assert not bad, (
        f"{len(bad)} of {len(want)} frames differ from the original gate; "
        f"first: frame {bad[0][0]} was {bad[0][1]['regions']!r}, "
        f"now {bad[0][2]['regions']!r}")


def test_the_skip_decision_matches_frame_for_frame(golden, replay):
    """Separately from the coordinates: a frame the original skipped entirely
    must still be skipped, and one it ran on must still run. This is the
    saving, and it is the half an operator would never see go wrong."""
    _, got, _ = replay
    want = golden["regions"]["values"]
    def branch(r):
        return "none" if r["regions"] is None else (
            "full_frame" if not r["regions"] else "regions")
    mism = [(i, branch(w), branch(g))
            for i, (w, g) in enumerate(zip(want, got)) if branch(w) != branch(g)]
    assert not mism, f"{len(mism)} frames took a different branch: {mism[:5]}"


def test_the_fixture_exercises_all_three_branches(golden):
    """A fixture of all-skips would pass anything."""
    vals = golden["regions"]["values"]
    seen = {("none" if v["regions"] is None else
             ("full_frame" if not v["regions"] else "regions")) for v in vals}
    assert seen == {"none", "regions", "full_frame"}, seen


def test_health_keys_are_unchanged(replay):
    """snapshot() feeds /health per_camera. The canonical implementation counts
    under different names; the adapter must not leak them into an API an
    operator and our dashboards already read."""
    _, _, gate = replay
    snap = gate.snapshot()
    assert set(snap) == {"frames_seen", "frames_skipped", "skip_rate",
                         "frames_full_frame", "frames_regional",
                         "regions_emitted", "frames_scene_change"}
    assert snap["frames_seen"] > 0
    assert snap["frames_skipped"] > 0


def test_the_gate_module_is_a_generated_copy():
    """index/motion.py is no longer hand-written: it is a copy of
    services/motion/detector/motion_core.py, produced by scripts/sync_motion.py.

    The banner is the invariant. If someone replaces this file with their own
    implementation the banner goes with it, and the duplication this removed —
    two motion implementations that agreed until one was tuned — is back.
    """
    import inspect

    from analytics import motion
    src = inspect.getsource(motion)
    assert "GENERATED COPY - DO NOT EDIT" in src,         "index/motion.py is not a generated copy of motion_core.py"
    assert "services/motion/detector/motion_core.py" in src,         "the generated copy does not name its source"
