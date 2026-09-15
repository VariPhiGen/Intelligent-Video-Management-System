#!/usr/bin/env python3
"""sync_shared — code owned by one service, copied to those that need it.

THE PROBLEM THIS SOLVES. There is exactly one motion implementation now:
services/motion/detector/motion_core.py. It lives in the motion service
because that is where someone looks for motion detection, and because that
service always ships — the frame broker is profile-gated and off by default,
so an algorithm owned there would sit in a container most deployments never
start. Three services need it, and each builds from its OWN directory as its
Docker context, so none can import a file outside itself. Docker cannot copy from outside the build context, so a shared
directory would mean moving all three builds to a repo-root context — and the
repo's .dockerignore is written for the api image and EXCLUDES services/motion,
so that switch would silently produce a broken motion image. Per-Dockerfile
ignore files would fix that, but are honoured only under BuildKit and fail
silently without it.

So the file is copied, and the copies are held identical mechanically:

    python3 scripts/sync_shared.py            # copy sources -> vendored copies
    python3 scripts/sync_shared.py --check    # verify, non-zero if drifted

WHAT STOPS THIS BECOMING THREE IMPLEMENTATIONS AGAIN. Two things, and the
second is the one that matters. --check makes a drifted copy a hard failure.
And each service carries a GOLDEN-VECTOR test (tests/golden_motion.json,
captured from the ORIGINAL implementations before they were replaced), so a
copy that drifts in BEHAVIOUR fails that service's own suite, in its own
container, without needing this script or the other services present.

Edit the source. Never edit a vendored copy: it is overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAMES = ROOT / "services" / "frames"
SEARCH = ROOT / "services" / "smartsearch"
MOTION = ROOT / "services" / "motion"
ANALYTICS = ROOT / "services" / "analytics"

#: (source, [destinations]). Three artifacts, one source each:
#:
#:   the algorithm   the single implementation every service runs, including
#:                   each consumer's own entry points (preprocess_frame /
#:                   compute_pixel_feature for this service, MotionGate for
#:                   SmartSearch) so a service carries ONE motion file, not a
#:                   copy plus a hand-written shim
#:   the frames      the inputs the fixture was captured from — all three
#:                   services must replay byte-identical frames, or the golden
#:                   comparison is between different things
#:   the fixture     what the ORIGINAL implementations produced on them
#:
#: Destination names say what they are, so nobody opens one expecting it to be
#: the place to make a change.
ARTIFACTS = [
    (MOTION / "detector" / "motion_core.py", [
        FRAMES / "broker" / "motion.py",
        ANALYTICS / "analytics" / "motion.py",
    ]),
    (MOTION / "tests" / "golden_frames.py", [
        FRAMES / "tests" / "golden_frames.py",
        ANALYTICS / "tests" / "golden_frames.py",
    ]),
    (MOTION / "tests" / "golden_motion.json", [
        FRAMES / "tests" / "golden_motion.json",
        ANALYTICS / "tests" / "golden_motion.json",
    ]),

    # ── model-backend vocabulary, shared by the two services that load models
    #
    # analytics loads a detector and a plate reader; smartsearch loads the CLIP
    # encoder. Both need the same words for "which representation, on which
    # runtime, on which device" and the same hardware probing, or /health means
    # different things in each. Owned by smartsearch because that is where they
    # were written and where their tests live; analytics gets a copy for the
    # same reason every other copy here exists — separate Docker contexts.
    (SEARCH / "index" / "backends.py", [
        ANALYTICS / "analytics" / "backends.py",
    ]),
    (SEARCH / "index" / "hardware.py", [
        ANALYTICS / "analytics" / "hardware.py",
    ]),
]

# COMMENTS, NOT A DOCSTRING. A string literal here would be a second module
# docstring sitting ahead of the source's own `from __future__ import
# annotations`, and Python requires that to be the first statement in a file.
# The copies imported fine until they did not, with a SyntaxError pointing at
# line 53 of a generated file.
BANNER = """# ===========================================================================
# GENERATED COPY - DO NOT EDIT.
#
# Source: {src}
# Sync:   python3 scripts/sync_shared.py   (--check verifies, and CI should)
#
# Edits here are silently destroyed on the next sync. More to the point, they
# would recreate the exact duplication this replaced: two motion
# implementations that agreed until one of them was tuned. If this file needs
# to change, change the source and re-run the sync.
# ===========================================================================
"""


def rendered(src: Path) -> str:
    body = src.read_text(encoding="utf-8")
    # JSON has no comment syntax, so the fixture carries its warning in a
    # "note" field written by capture_golden.py rather than a banner here.
    if src.suffix == ".json":
        return body
    return BANNER.format(src=src.relative_to(ROOT).as_posix()) + body


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync shared modules between services")
    ap.add_argument("--check", action="store_true",
                    help="verify the copies match; do not write")
    args = ap.parse_args()

    drifted: list[str] = []
    for src, dests in ARTIFACTS:
        if not src.is_file():
            print(f"sync_motion: source missing: {src}")
            return 2
        want = rendered(src)
        sha = hashlib.sha256(want.encode()).hexdigest()
        print(f"{src.relative_to(ROOT).as_posix()}  sha256 {sha[:16]}...")
        for dst in dests:
            rel = dst.relative_to(ROOT).as_posix()
            have = dst.read_text(encoding="utf-8") if dst.is_file() else None
            if have == want:
                print(f"  ok       {rel}")
                continue
            drifted.append(rel)
            if args.check:
                print(f"  {'missing' if have is None else 'DRIFTED':<8} {rel}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(want, encoding="utf-8")
                print(f"  written  {rel}")

    if args.check and drifted:
        print("\nsync_motion: copies are out of date — run "
              "`python3 scripts/sync_shared.py`")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
