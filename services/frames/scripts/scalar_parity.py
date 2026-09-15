#!/usr/bin/env python3
"""scalar_parity — does the broker's fraction match what Motion would compute?

THE PHASE 3 EXIT CRITERION, in the only form that actually tests anything.
Events are byte-identical if and only if the FEATURE STREAM is identical: both
paths call the same worker.apply_feature, so the confirmation window, the
state machine and the cooldown cannot diverge. What can diverge is the number
fed into them.

AND THERE IS A KNOWN REASON IT MIGHT. The two implementations resize
differently:

    motion/detector/algorithm.py   cv2.resize(frame, (320, 180))   FIXED
    frames/broker/motion.py        320 wide, aspect PRESERVED

For 16:9 cameras these are the same thing — 1920x1080 scales to exactly
320x180. For anything else they are not: cam5 here is 704x576, which the old
path squashes to 320x180 and the broker renders 320x262. Different pixel
counts and different geometry produce a different changed-pixel fraction, and
the sensitivity presets were fitted against the squashed one.

So this measures the disagreement per camera rather than assuming there is
none, and reports whether it could flip a threshold decision — which is the
only thing that would change an operator's events.

    python3 scripts/scalar_parity.py --camera cam5-iohm --frames 80
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
MOTION = HERE.parent / "motion"
sys.path.insert(0, str(MOTION))

from broker.motion import CanonicalMotion, MotionParams          # noqa: E402


def load_reference():
    """The shipped Motion service algorithm and its configured thresholds."""
    from detector.motion_core import compute_pixel_feature, preprocess_frame
    from detector.config import AppConfig
    cfg = AppConfig.from_yaml(str(MOTION / "config.yaml"))
    return preprocess_frame, compute_pixel_feature, cfg


def live_frames(url: str, n: int, interval: float):
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit(f"scalar_parity: cannot open {url}")
    try:
        next_due, got = 0.0, 0
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


_REFERENCE_MODULE = "detector.motion_core"
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
    ap = argparse.ArgumentParser(description="Broker scalar vs Motion scalar")
    ap.add_argument("--camera", default="cam1-h5e0")
    ap.add_argument("--host", default=os.environ.get("RELAY_HOST", "mediamtx"))
    ap.add_argument("--port", default=os.environ.get("RELAY_PORT", "8654"))
    ap.add_argument("--frames", type=int, default=80)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    preprocess, pixel_feature, mcfg = load_reference()
    _announce_if_tautological()
    floor = mcfg.detection.noise_floor_threshold
    canon = CanonicalMotion(MotionParams(noise_floor=floor))

    url = f"rtsp://{args.host}:{args.port}/{args.camera}"
    prev = None
    rows: list[tuple[float, float]] = []
    dims = None

    for frame in live_frames(url, args.frames, args.interval):
        if dims is None:
            dims = (frame.shape[1], frame.shape[0])
        got = canon.analyse(frame)
        cur = preprocess(frame, mcfg.capture.frame_width, mcfg.capture.frame_height)
        if prev is not None:
            rows.append((pixel_feature(prev, cur, floor), got.fraction))
        prev = cur

    if not rows:
        print("scalar_parity: no frames — nothing proven")
        return 1

    ref = np.array([r[0] for r in rows])
    new = np.array([r[1] for r in rows])
    diff = np.abs(new - ref)

    print(f"camera            {args.camera}  {dims[0]}x{dims[1]}")
    print(f"aspect            {'16:9 — resizes agree' if abs(dims[0]/dims[1] - 16/9) < 0.01 else 'NOT 16:9 — resizes DIFFER'}")
    print(f"samples compared  {len(rows)}\n")
    print(f"{'':18}{'motion':>12}{'broker':>12}")
    print(f"{'mean':<18}{ref.mean():>12.6f}{new.mean():>12.6f}")
    print(f"{'max':<18}{ref.max():>12.6f}{new.max():>12.6f}")
    print(f"\nabsolute difference  mean {diff.mean():.6f}  max {diff.max():.6f}")

    # THE QUESTION THAT MATTERS. A difference only reaches an operator if it
    # lands on the far side of the sensitivity threshold from the reference —
    # everything else is noise below the decision.
    print(f"\n{'preset':<10}{'threshold':>11}{'motion says':>13}{'broker says':>13}{'DISAGREE':>10}")
    worst = 0
    for name, thr in sorted(mcfg.detection.sensitivity_presets.items(),
                            key=lambda kv: -kv[1]):
        a = ref >= thr
        b = new >= thr
        n_dis = int((a != b).sum())
        worst = max(worst, n_dis)
        print(f"{name:<10}{thr:>11.4f}{int(a.sum()):>13}{int(b.sum()):>13}"
              f"{n_dis:>10}")

    ok = worst == 0
    print(f"\nSCALAR PARITY: {'PASS' if ok else 'DIFFERS'}  "
          f"({worst} sample(s) would cross the threshold differently)")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
