#!/usr/bin/env python3
"""capture_golden — freeze what the ORIGINAL motion implementations computed.

WHY THIS EXISTS. Two duplicate implementations are being replaced by the
canonical one: services/motion/detector/algorithm.py (the scalar) and
services/smartsearch/index/motion.py (the regions). Both were verified
identical to the canonical over 430 live and synthetic frames — but that proof
is a COMPARISON BETWEEN TWO LIVE IMPLEMENTATIONS, so it dies the moment the
duplicates are deleted, and every later change would be checked against
nothing.

So the proof is frozen here instead. This script runs the ORIGINAL code, on
deterministic frames covering every branch, and records what it produced. The
fixture then outlives the code that made it: a change to the canonical
implementation that would have altered an operator's motion events fails a
test, forever, without either duplicate needing to exist.

RUN THIS BEFORE DELETING ANYTHING, from a checkout where both duplicates are
still the originals. Re-running it later against replacement code would
re-baseline the fixture on the very thing it is supposed to be checking, which
is worse than having no fixture at all.

    python3 scripts/capture_golden.py --frames 220 --out tests/golden_motion.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "motion"))
sys.path.insert(0, str(HERE.parent / "smartsearch"))

from tests.golden_frames import synthetic_frames                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Freeze the original outputs")
    ap.add_argument("--frames", type=int, default=220)
    ap.add_argument("--out", default="tests/golden_motion.json")
    args = ap.parse_args()

    # ── the originals, with their shipped configuration ──────────────────────
    from detector.motion_core import compute_pixel_feature, preprocess_frame
    from detector.config import AppConfig as MotionConfig
    from index.config import AppConfig as SearchConfig
    from index.motion import MotionGate

    mcfg = MotionConfig.from_yaml(str(HERE.parent / "motion" / "config.yaml"))
    scfg = SearchConfig.from_yaml(str(HERE.parent / "smartsearch" / "config.yaml"))
    m = scfg.motion

    gate = MotionGate(
        scale_width=320, threshold=m.threshold,
        min_area_fraction=m.min_area_fraction,
        dilate_iterations=m.dilate_iterations, max_regions=m.max_regions,
        region_padding=m.region_padding,
        full_frame_fraction=m.full_frame_fraction,
        scene_change_fraction=m.scene_change_fraction,
        despeckle_iterations=m.despeckle_iterations,
    )
    floor = mcfg.detection.noise_floor_threshold
    cap_w, cap_h = mcfg.capture.frame_width, mcfg.capture.frame_height

    digest = hashlib.sha256()
    scalars: list[float] = []
    regions: list[dict] = []
    prev = None

    for frame in synthetic_frames(args.frames):
        # The INPUTS are fingerprinted too. If numpy ever changes what its
        # seeded generator produces, this fixture must fail saying the frames
        # changed — not leave a reader concluding the algorithm broke.
        digest.update(np.ascontiguousarray(frame).tobytes())

        res = gate.evaluate(frame)
        regions.append({
            "regions": None if res.regions is None else [list(r) for r in res.regions],
            "reason": res.reason,
        })

        cur = preprocess_frame(frame, cap_w, cap_h)
        scalars.append(None if prev is None
                       else compute_pixel_feature(prev, cur, floor))
        prev = cur

    out = {
        "note": "Captured from the ORIGINAL implementations before they were "
                "replaced by the canonical one. Do not regenerate against "
                "replacement code — see scripts/capture_golden.py.",
        "frames": args.frames,
        "frames_sha256": digest.hexdigest(),
        "numpy": np.__version__,
        "scalar": {
            "source": "services/motion/detector/algorithm.py (pre-consolidation)",
            "frame_width": cap_w, "frame_height": cap_h, "noise_floor": floor,
            "values": scalars,
        },
        "regions": {
            "source": "services/smartsearch/index/motion.py",
            "params": {
                "scale_width": 320, "threshold": m.threshold,
                "min_area_fraction": m.min_area_fraction,
                "despeckle_iterations": m.despeckle_iterations,
                "dilate_iterations": m.dilate_iterations,
                "max_regions": m.max_regions,
                "region_padding": m.region_padding,
                "full_frame_fraction": m.full_frame_fraction,
                "scene_change_fraction": m.scene_change_fraction,
            },
            "values": regions,
        },
    }
    path = HERE / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    # CLASSIFIED BY SHAPE, NOT BY THE REASON TEXT. `reason` is a human
    # sentence meant for a log line ("scene change", "no motion"); the branch
    # is the shape of `regions`, and that is what a consumer acts on. Checking
    # the prose would tie this fixture to wording nobody promised to keep.
    def branch(r) -> str:
        if r["regions"] is None:
            return "none"                      # skip the detector entirely
        return "full_frame" if not r["regions"] else "regions"

    mix: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in regions:
        b = branch(r)
        mix[b] = mix.get(b, 0) + 1
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    print(f"wrote {path}")
    print(f"  frames          {args.frames}  sha256 {digest.hexdigest()[:16]}...")
    print(f"  scalar samples  {sum(1 for v in scalars if v is not None)}")
    print(f"  branch mix      {mix}")
    print(f"  reason mix      {reasons}")
    missing = {"none", "regions", "full_frame"} - set(mix)
    if missing:
        print(f"  WARNING: no coverage of {sorted(missing)} — raise --frames")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
