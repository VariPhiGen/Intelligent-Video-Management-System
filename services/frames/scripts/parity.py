#!/usr/bin/env python3
"""parity — does the canonical motion reproduce the gate it replaces?

THE PHASE 1 EXIT CRITERION. services/frames/broker/motion.py is meant to be a
drop-in replacement for services/smartsearch/index/motion.py's region output.
"Meant to be" is worth nothing: this runs BOTH implementations over the SAME
frames and compares region-for-region.

Any disagreement is a defect in the canonical version, because the gate's
constants are the ones that were fitted against real footage — the 8-person
test recorded in smartsearch/config.yaml. A canonical implementation that
finds different regions has silently changed what gets detected.

Frames come from the live relay by default, so the comparison runs on the
pixels the deployment actually sees. Add --synthetic for a deterministic run
that needs no cameras (useful in CI, useless as evidence about real scenes).

    python3 scripts/parity.py --camera cam1-h5e0 --frames 120
    python3 scripts/parity.py --synthetic --frames 200
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
#: The gate being replaced. Imported from the smartsearch tree directly rather
#: than vendored, so this compares against what actually ships there.
SMARTSEARCH = HERE.parent / "smartsearch"
sys.path.insert(0, str(SMARTSEARCH))

from broker.motion import CanonicalMotion, MotionParams          # noqa: E402


def load_reference():
    """The shipped SmartSearch gate, with its shipped configuration."""
    from index.config import AppConfig                            # noqa: E402
    from index.motion import MotionGate                           # noqa: E402
    cfg = AppConfig.from_yaml(str(SMARTSEARCH / "config.yaml"))
    m = cfg.motion
    gate = MotionGate(
        scale_width=320, threshold=m.threshold,
        min_area_fraction=m.min_area_fraction,
        dilate_iterations=m.dilate_iterations, max_regions=m.max_regions,
        region_padding=m.region_padding,
        full_frame_fraction=m.full_frame_fraction,
        scene_change_fraction=m.scene_change_fraction,
        despeckle_iterations=m.despeckle_iterations,
    )
    params = MotionParams(
        scale_width=320, threshold=m.threshold,
        min_area_fraction=m.min_area_fraction,
        despeckle_iterations=m.despeckle_iterations,
        dilate_iterations=m.dilate_iterations, max_regions=m.max_regions,
        region_padding=m.region_padding,
        full_frame_fraction=m.full_frame_fraction,
        scene_change_fraction=m.scene_change_fraction,
    )
    return gate, params


# THE GENERATOR LIVES WITH THE FIXTURE IT PRODUCED, in tests/golden_frames.py,
# because every service replays the same frames and they must be identical.
from tests.golden_frames import synthetic_frames                  # noqa: E402,F401


def live_frames(url: str, n: int, interval: float):
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit(f"parity: cannot open {url}")
    try:
        next_due = 0.0
        got = 0
        while got < n:
            if not cap.grab():
                break
            now = time.time()
            if now < next_due:
                continue
            ok, f = cap.retrieve()
            if not ok or f is None:
                continue
            next_due = now + interval
            got += 1
            yield f
    finally:
        cap.release()


def classify(regions) -> str:
    if regions is None:
        return "none"
    return "full_frame" if len(regions) == 0 else "regions"


_REFERENCE_MODULE = "index.motion"
def _same_implementation_now() -> bool:
    """True once the reference is an adapter over the canonical module.

    THIS SCRIPT PROVED THE MIGRATION AND CANNOT PROVE IT TWICE. It compared
    two independent implementations over the same frames; now there is one,
    and the "reference" it imports is a thin adapter over it. Comparing code
    to itself would print PASS forever, including after a change that broke
    everything — a check that cannot fail is worse than no check, because
    someone will trust it.

    The proof that outlived the duplicates is tests/golden_motion.json: what
    the ORIGINAL implementations produced, replayed by each service's own
    suite. See scripts/capture_golden.py.
    """
    import inspect
    try:
        mod = sys.modules.get(_REFERENCE_MODULE)
        if mod is None:
            return False
        # The reference and this script's subject are the same implementation
        # once the reference module either IS motion_core or is a generated
        # copy of it — both define CanonicalMotion.
        return "class CanonicalMotion" in inspect.getsource(mod)
    except (OSError, TypeError):
        return False


def _announce_if_tautological() -> None:
    if not _same_implementation_now():
        return
    print("=" * 74)
    print("NOTE: the reference is now an ADAPTER over the same canonical")
    print("implementation this compares against, so agreement below is by")
    print("construction and proves nothing. Kept because it still exercises")
    print("the real pipeline on live frames and reports timing.")
    print()
    print("The proof that survived the duplicates being removed is")
    print("tests/golden_motion.json, replayed by each service's own suite.")
    print("=" * 74)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Canonical vs shipped motion gate")
    ap.add_argument("--camera", default="cam1-h5e0")
    ap.add_argument("--host", default=os.environ.get("RELAY_HOST", "mediamtx"))
    ap.add_argument("--port", default=os.environ.get("RELAY_PORT", "8654"))
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args()

    gate, params = load_reference()
    _announce_if_tautological()
    canon = CanonicalMotion(params)

    src = (synthetic_frames(args.frames) if args.synthetic
           else live_frames(f"rtsp://{args.host}:{args.port}/{args.camera}",
                            args.frames, args.interval))
    print(f"parity: {'synthetic' if args.synthetic else args.camera}, "
          f"{args.frames} frames\n", flush=True)

    n = agree = 0
    verdict_mismatch = region_mismatch = 0
    by_verdict: dict[str, int] = {}
    ref_ms = can_ms = 0.0
    first_failures: list[str] = []

    for frame in src:
        n += 1
        t0 = time.perf_counter()
        ref = gate.evaluate(frame)
        ref_ms += (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        got = canon.analyse(frame)
        can_ms += (time.perf_counter() - t0) * 1000

        rv = classify(ref.regions)
        by_verdict[rv] = by_verdict.get(rv, 0) + 1

        if rv != got.verdict:
            verdict_mismatch += 1
            if len(first_failures) < 5:
                first_failures.append(
                    f"  frame {n}: verdict ref={rv} canonical={got.verdict} "
                    f"(ref reason={ref.reason!r} canonical={got.reason!r})")
            continue
        ref_boxes = [] if ref.regions is None else [tuple(b) for b in ref.regions]
        got_boxes = [] if got.regions is None else [tuple(b) for b in got.regions]
        if ref_boxes != got_boxes:
            region_mismatch += 1
            if len(first_failures) < 5:
                first_failures.append(
                    f"  frame {n}: regions differ\n"
                    f"      ref       ={ref_boxes}\n"
                    f"      canonical ={got_boxes}")
            continue
        agree += 1

    if n == 0:
        print("parity: no frames — nothing proven"); return 1

    print(f"{'frames compared':<28}{n}")
    print(f"{'identical':<28}{agree}  ({100*agree/n:.1f}%)")
    print(f"{'verdict mismatches':<28}{verdict_mismatch}")
    print(f"{'region mismatches':<28}{region_mismatch}")
    print(f"\nreference verdict mix: {by_verdict}")
    print(f"\n{'shipped gate':<28}{ref_ms/n:.2f} ms/frame")
    print(f"{'canonical (both outputs)':<28}{can_ms/n:.2f} ms/frame")
    if first_failures:
        print("\nfirst disagreements:")
        for f in first_failures:
            print(f)

    ok = (verdict_mismatch == 0 and region_mismatch == 0)
    print(f"\nPARITY: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
