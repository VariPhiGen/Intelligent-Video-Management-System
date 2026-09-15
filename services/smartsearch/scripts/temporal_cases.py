#!/usr/bin/env python3
"""temporal_cases — what the chosen policy does to real situations.

A grid of reduction and coverage percentages says the policy is safe ON AVERAGE.
It cannot say whether the specific situations an operator cares about behave
sensibly, and those are the ones a reviewer should be able to check by eye.

So this pulls CONCRETE records out of the cached traces — real detections, real
timestamps, real geometry — for each situation, and prints the policy's decision
with the number that produced it.

    python3 scripts/temporal_cases.py --trace /out/traceA.pkl --window 10 --distance 1.5
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from temporal_study import (Det, TemporalSpatialAssign,               # noqa: E402
                            centre_distance_in_heights, iou)

sys.modules["__main__"].Det = Det


def run_policy(trace: list[Det], window: float, distance: float):
    """Decisions for every detection, frame by frame, exactly as evaluate does."""
    policy = TemporalSpatialAssign(window, distance)
    policy.reset()
    frames: dict[tuple[str, int], list[Det]] = defaultdict(list)
    for d in trace:
        frames[(d.camera, d.frame_id)].append(d)
    decision: dict[int, bool] = {}
    for key in sorted(frames, key=lambda k: (k[0], frames[k][0].t)):
        group = frames[key]
        for d, keep in zip(group, policy.index_frame(group)):
            decision[id(d)] = keep
    return decision, frames


def show(det: Det, decision: dict, note: str = "") -> str:
    verdict = "INDEX   " if decision[id(det)] else "SUPPRESS"
    return (f"      {verdict}  {det.domain:<8} t={det.t:>10.1f} "
            f"c=({det.cx:.3f},{det.cy:.3f}) h={det.h:.3f} conf={det.conf:.2f}"
            f"{('   ' + note) if note else ''}")


def case(title: str, why: str) -> None:
    print(f"\n{'-' * 88}\n{title}\n  {why}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Qualitative failure-case review")
    ap.add_argument("--trace", action="append", required=True)
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--distance", type=float, default=1.5)
    ap.add_argument("--examples", type=int, default=2)
    args = ap.parse_args(argv)

    trace: list[Det] = []
    for path in args.trace:
        with open(path, "rb") as fh:
            trace += pickle.load(fh)
    print(f"{len(trace)} detections, policy = assign "
          f"{args.window:g}s / {args.distance:g} heights")

    decision, frames = run_policy(trace, args.window, args.distance)
    ordered = sorted(frames.items(), key=lambda kv: (kv[0][0], kv[1][0].t))

    per_key: dict[tuple[str, str], list[Det]] = defaultdict(list)
    for d in trace:
        per_key[(d.camera, d.domain)].append(d)
    for v in per_key.values():
        v.sort(key=lambda d: d.t)

    # ── 1 & 4. simultaneous objects of one class ─────────────────────────────
    for domain, label in (("person", "1. TWO+ PEOPLE SIMULTANEOUSLY PRESENT"),
                          ("vehicles", "4. MULTIPLE VEHICLES SIMULTANEOUSLY")):
        case(label, "two boxes in one frame are two objects — both must reach "
                    "the index on first sight")
        shown = 0
        for (cam, _fid), group in ordered:
            same = [d for d in group if d.domain == domain]
            if len(same) < 2 or shown >= args.examples:
                continue
            idx = sum(1 for d in same if decision[id(d)])
            print(f"    {cam}  {len(same)} {domain} in one frame -> "
                  f"{idx} indexed, {len(same)-idx} suppressed")
            for a in same:
                near = min((centre_distance_in_heights(a, b)
                            for b in same if b is not a), default=float('nan'))
                print(show(a, decision, f"nearest peer {near:.2f}h"))
            shown += 1

    # ── 2. crossing / standing close ─────────────────────────────────────────
    case("2. TWO PEOPLE CLOSE TOGETHER (crossing or standing)",
         "the case the unrestricted rule got wrong: one entry silencing both")
    shown = 0
    for (cam, _fid), group in ordered:
        same = [d for d in group if d.domain == "person"]
        if len(same) < 2 or shown >= args.examples:
            continue
        pairs = [(centre_distance_in_heights(a, b), a, b)
                 for i, a in enumerate(same) for b in same[i + 1:]]
        pairs.sort()
        if not pairs or pairs[0][0] > args.distance:
            continue
        dist, a, b = pairs[0]
        print(f"    {cam}  two people {dist:.2f}h apart "
              f"(inside the {args.distance:g}h radius), IoU={iou(a, b):.2f}")
        print(show(a, decision))
        print(show(b, decision))
        shown += 1

    # ── 3. new object entering while one is present ──────────────────────────
    case("3. NEW PERSON ENTERS WHILE ANOTHER IS ALREADY PRESENT",
         "the newcomer must be indexed even though the scene was already busy")
    shown = 0
    prev: dict[str, list[Det]] = {}
    for (cam, _fid), group in ordered:
        same = [d for d in group if d.domain == "person"]
        before = prev.get(cam, [])
        prev[cam] = same
        if shown >= args.examples or len(same) <= len(before) or not before:
            continue
        newcomers = [d for d in same
                     if min((centre_distance_in_heights(d, p) for p in before),
                            default=9.9) > args.distance]
        if not newcomers:
            continue
        print(f"    {cam}  {len(before)} -> {len(same)} people")
        for d in newcomers[:2]:
            gap = min(centre_distance_in_heights(d, p) for p in before)
            print(show(d, decision, f"newcomer, {gap:.2f}h from anyone present"))
        shown += 1

    # ── 5, 6, 7. movement ────────────────────────────────────────────────────
    for domain, moving, title in (
            ("vehicles", False, "6. STATIONARY VEHICLE"),
            ("vehicles", True, "5. MOVING VEHICLE"),
            ("person", True, "7. OBJECT MOVING SIGNIFICANTLY")):
        case(title, "suppressed while it stays put; re-indexed once it has "
                    "moved past the radius, or once the window expires")
        shown = 0
        for (cam, domain2), dets in per_key.items():
            if domain2 != domain or shown >= args.examples:
                continue
            for i in range(len(dets) - 4):
                run = dets[i:i + 5]
                if run[-1].t - run[0].t > 12:
                    continue
                moved = max(centre_distance_in_heights(run[0], r) for r in run)
                if moving and moved < args.distance:
                    continue
                if not moving and moved > 0.2:
                    continue
                print(f"    {cam}  5 consecutive {domain}, "
                      f"max displacement {moved:.2f}h")
                for r in run:
                    print(show(r, decision,
                               f"moved {centre_distance_in_heights(run[0], r):.2f}h "
                               f"from first"))
                shown += 1
                break

    # ── 8 & 9. disappear and return ──────────────────────────────────────────
    for longer, title in (
            (True, f"8. RETURNS AFTER MORE THAN {args.window:g}s"),
            (False, f"9. RETURNS WITHIN {args.window:g}s")):
        case(title, "state expires on a timer, so a long absence is a fresh "
                    "appearance and a short one is treated as continuation")
        shown = 0
        for (cam, domain), dets in per_key.items():
            if shown >= args.examples:
                continue
            for a, b in zip(dets, dets[1:]):
                gap = b.t - a.t
                if gap <= 1.5:
                    continue
                if longer and gap <= args.window:
                    continue
                if not longer and (gap > args.window or gap < 2):
                    continue
                if centre_distance_in_heights(a, b) > args.distance:
                    continue          # returned somewhere else: a new object
                print(f"    {cam}  {domain} gap {gap:.1f}s, "
                      f"returned {centre_distance_in_heights(a, b):.2f}h away")
                print(show(a, decision, "before the gap"))
                print(show(b, decision, "after the gap"))
                shown += 1
                break

    print(f"\n{'-' * 88}")
    total = len(trace)
    kept = sum(1 for d in trace if decision[id(d)])
    print(f"overall: {kept} indexed of {total} "
          f"({100 * (1 - kept / total):.1f}% reduction)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
