#!/usr/bin/env python3
"""temporal_sweep — find the knee, rather than defending the points I guessed.

Runs the suppression policies over CACHED detection traces (see
scripts/temporal_study.py --cache), so a full grid costs seconds instead of the
twelve minutes a rebuild takes. The traces are deterministic, so sweeping them
is exactly equivalent to re-running the detector for every combination.

SCORED ON COVERAGE, NOT ON REDUCTION. Two earlier metrics gave the wrong answer
here and both failed the same way — they moved with the policy under test:

  * `risky` applied the policy's own spatial rule, and reported 0.0% on a trace
    where 73.5% of certainly-different pairs sat inside the threshold.
  * `co_present_lost` only counted suppressions in frames that ALSO kept
    something, so a policy that keeps more first-frame detections is charged
    for more loss — it ranked the better policy worse.

`uncovered` has a floor the policy cannot move: inside a window, the largest
number of SIMULTANEOUS detections is a hard lower bound on distinct objects
present, because two boxes in one frame are two objects. Anything the policy
indexes below that floor is an object certainly never indexed, and therefore
never findable.

    python3 scripts/temporal_sweep.py --trace /out/traceA.pkl --label cam1/3/5
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import temporal_study                                                # noqa: E402
from temporal_study import (Det, FixedTemporal, TemporalSpatial,     # noqa: E402
                            TemporalSpatialAssign, evaluate)

# The cache was written by temporal_study.py running as a script, so its Det
# class was pickled as `__main__.Det`. Point that name at the real class rather
# than rebuilding a twelve-minute trace over a module path.
sys.modules["__main__"].Det = Det

WINDOWS = (5.0, 10.0, 15.0, 20.0, 30.0)
DISTANCES = (0.5, 0.75, 1.0, 1.25, 1.5)

#: An object never indexed is a person who cannot be found. Reduction is only
#: worth having below this, so it is a gate rather than a term to trade off.
MAX_UNCOVERED_PCT = 5.0


def grid(trace, label: str) -> list[dict]:
    print(f"\n{'=' * 92}")
    print(f"{label}  —  {len(trace)} detections, "
          f"{len({d.camera for d in trace})} cameras")
    print("=" * 92)

    rows = []
    print("\nASSIGNMENT POLICY — reduction %  (uncovered %)")
    header = "  win \\ dist" + "".join(f"{d:>16.2f}" for d in DISTANCES)
    print(header)
    for w in WINDOWS:
        cells = []
        for dist in DISTANCES:
            r = evaluate(trace, TemporalSpatialAssign(w, dist))
            r.update(kind="assign", window=w, distance=dist, label=label)
            rows.append(r)
            cells.append(f"{r['reduction_pct']:>9.1f} ({r['uncovered_pct']:>4.1f})")
        print(f"  {w:>4.0f}s      " + "".join(cells))

    print("\nUNRESTRICTED (no one-to-one matching) — reduction %  (uncovered %)")
    print(header)
    for w in WINDOWS:
        cells = []
        for dist in DISTANCES:
            r = evaluate(trace, TemporalSpatial(w, dist))
            r.update(kind="unrestricted", window=w, distance=dist, label=label)
            rows.append(r)
            cells.append(f"{r['reduction_pct']:>9.1f} ({r['uncovered_pct']:>4.1f})")
        print(f"  {w:>4.0f}s      " + "".join(cells))

    print("\nFIXED TEMPORAL (no geometry at all)")
    print(f"  {'window':>8}{'reduction':>12}{'uncovered':>12}")
    for w in WINDOWS:
        r = evaluate(trace, FixedTemporal(w))
        r.update(kind="fixed", window=w, distance=None, label=label)
        rows.append(r)
        print(f"  {w:>7.0f}s{r['reduction_pct']:>11.1f}%{r['uncovered_pct']:>11.1f}%")
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep suppression parameters")
    ap.add_argument("--trace", action="append", required=True,
                    help="cached trace pickle; repeatable")
    ap.add_argument("--label", action="append", default=[])
    args = ap.parse_args(argv)

    all_rows: list[dict] = []
    for i, path in enumerate(args.trace):
        if not os.path.exists(path):
            print(f"missing trace: {path}", file=sys.stderr)
            return 1
        with open(path, "rb") as fh:
            trace = pickle.load(fh)
        label = args.label[i] if i < len(args.label) else os.path.basename(path)
        all_rows += grid(trace, label)

    # ── the choice has to hold on EVERY trace, not on the average ────────────
    # A setting that is safe on a sparse camera and loses a third of the objects
    # on a crowded one is not a default; it is a setting that happens to suit
    # one site. So each candidate is scored by its WORST trace.
    labels = {r["label"] for r in all_rows}
    if len(labels) < 2:
        return 0

    print(f"\n{'=' * 92}")
    print("WORST-CASE ACROSS TRACES  "
          f"(a default must hold on both; gate = uncovered <= {MAX_UNCOVERED_PCT}%)")
    print("=" * 92)
    combos: dict[tuple, list[dict]] = {}
    for r in all_rows:
        combos.setdefault((r["kind"], r["window"], r["distance"]), []).append(r)

    scored = []
    for (kind, w, dist), rs in combos.items():
        if len(rs) < len(labels):
            continue
        worst_unc = max(r["uncovered_pct"] for r in rs)
        worst_red = min(r["reduction_pct"] for r in rs)
        scored.append((worst_unc, -worst_red, kind, w, dist, worst_red))

    passing = [s for s in scored if s[0] <= MAX_UNCOVERED_PCT]
    passing.sort(key=lambda s: -s[5])            # best reduction among the safe
    print(f"\n{len(passing)} of {len(scored)} settings keep uncovered "
          f"<= {MAX_UNCOVERED_PCT}% on BOTH traces. Best reduction among them:\n")
    print(f"  {'policy':<16}{'window':>8}{'dist':>7}"
          f"{'worst reduction':>18}{'worst uncovered':>18}")
    for worst_unc, _neg, kind, w, dist, worst_red in passing[:12]:
        d = "-" if dist is None else f"{dist:.2f}"
        print(f"  {kind:<16}{w:>7.0f}s{d:>7}{worst_red:>17.1f}%{worst_unc:>17.1f}%")

    if not passing:
        print("  NONE. Every setting loses more than the gate allows on at "
              "least one trace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
