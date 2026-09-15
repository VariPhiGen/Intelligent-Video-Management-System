#!/usr/bin/env python3
"""tracker_sweep — choose the tracker's operating point from evidence.

Replays a cached detection trace through the REAL index/tracking.py and
index/indexing_policy.py, so the numbers belong to the shipped code rather than
to a reimplementation of it. Same discipline as the temporal-gate sweep this
replaces: publish the table, pick the knee, record it in the config comment.

THE TWO NUMBERS THAT MATTER, AND THEY PULL AGAINST EACH OTHER:

  fragmentation   tracks created per distinct object actually present. 1.0 is
                  perfect. Above that, one object is being given several
                  identities — which for search means one person appearing as
                  several strangers, and it is what a too-tight association
                  threshold produces.

  merge risk      proxied by association cost at the accepted margin. A loose
                  threshold stops fragmenting and starts MERGING, which is the
                  worse failure: a merged identity is a person who never comes
                  back as a hit. There is no label in the trace that can prove
                  a merge, so this is watched rather than measured — see the
                  honesty note in the report.

GROUND TRUTH, SUCH AS IT IS. The trace carries no identity labels, so distinct
objects are floored the same way the temporal study floored them: the most
detections seen SIMULTANEOUSLY in a window is a hard lower bound on how many
objects were present, because two boxes in one frame are two objects.

    python3 scripts/tracker_sweep.py --trace /out/fps2.pkl
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from index.detector import Detection            # noqa: E402
from index.indexing_policy import IndexingPolicy  # noqa: E402
from index.tracking import ObjectTracker        # noqa: E402

#: Traces store normalised boxes; the tracker works in detector units. A single
#: common scale factor cancels in every ratio the cost function computes, so
#: this changes nothing except making the integers meaningful.
SCALE = 10000


def load_trace(path: str):
    import temporal_study                                        # noqa: F401
    sys.modules["__main__"].Det = temporal_study.Det
    with open(path, "rb") as fh:
        trace = pickle.load(fh)
    trace.sort(key=lambda d: (d.t, d.camera))
    return trace


def to_detection(d) -> Detection:
    x1, y1, x2, y2 = d.bbox
    return Detection(domain=d.domain,
                     xyxy=(int(x1 * SCALE), int(y1 * SCALE),
                           int(x2 * SCALE), int(y2 * SCALE)),
                     confidence=float(d.conf), label=d.label)


def object_floor(trace, window: float = 30.0) -> int:
    """Hard lower bound on distinct objects present."""
    total = 0
    by = defaultdict(list)
    for d in trace:
        by[(d.camera, d.domain)].append(d)
    for dets in by.values():
        dets.sort(key=lambda x: x.t)
        start = dets[0].t
        while start <= dets[-1].t:
            chunk = [d for d in dets if start <= d.t < start + window]
            if chunk:
                per_frame = defaultdict(int)
                for d in chunk:
                    per_frame[round(d.t, 3)] += 1
                total += max(per_frame.values())
            start += window
    return total


def run(trace, *, max_cost: float, alpha: float, max_age: float,
        confirm_after: float, stationary_distance: float,
        displacement: float, heartbeat: float, scale_change: float,
        index_tentative: bool) -> dict:
    tracker = ObjectTracker(
        max_association_cost=max_cost, velocity_alpha=alpha,
        confirm_after_seconds=confirm_after, max_age_seconds=max_age,
        stationary_after_seconds=4.0, stationary_distance=stationary_distance)
    policy = IndexingPolicy(
        displacement_heights=displacement, heartbeat_seconds=heartbeat,
        scale_change_ratio=scale_change, quality_confidence_gain=0.15)

    frames = defaultdict(list)
    for d in trace:
        frames[(d.camera, round(d.t, 3))].append(d)

    indexed = tentative_dropped = 0
    #: Tracks that existed but never produced a single observation. THE RECALL
    #: NUMBER — losing detections is fine if the object is recorded some other
    #: time; losing a whole track is a person no search can return.
    seen_tracks: set[str] = set()
    indexed_tracks: set[str] = set()
    for (camera, ts), dets in sorted(frames.items(), key=lambda kv: kv[0][1]):
        tracked = tracker.update(camera, [to_detection(d) for d in dets], ts)
        gone = tracker.drain_expired()
        if gone:
            policy.forget(gone)
        for td in tracked:
            seen_tracks.add(td.track.track_id)
            if not td.track.confirmed and not index_tentative:
                tentative_dropped += 1
                continue
            reason = policy.decide(td.track, td.detection, ts)
            if reason is None:
                policy.suppress()
                continue
            policy.record(td.track, td.detection, ts, reason)
            indexed_tracks.add(td.track.track_id)
            indexed += 1

    tsnap, psnap = tracker.snapshot(), policy.snapshot()
    floor = object_floor(trace)
    return {
        "max_cost": max_cost, "alpha": alpha, "max_age": max_age,
        "confirm_after": confirm_after, "displacement": displacement,
        "heartbeat": heartbeat,
        "detections": len(trace),
        "indexed": indexed,
        "reduction_pct": round(100 * (1 - indexed / max(1, len(trace))), 1),
        "tracks_created": tsnap["tracks_created"],
        "object_floor": floor,
        # >1 means identities are splitting; 1.0 is ideal. Below 1 does NOT
        # mean better — it means fewer identities than objects known to be
        # present, i.e. merging.
        "fragmentation": round(tsnap["tracks_created"] / max(1, floor), 2),
        "assoc_fail_rate": tsnap["association_failure_rate"],
        "tentative_dropped": tentative_dropped,
        "tentative_pct": round(100 * tentative_dropped / max(1, len(trace)), 1),
        "tracks_never_indexed": len(seen_tracks - indexed_tracks),
        "tracks_never_indexed_pct": round(
            100 * len(seen_tracks - indexed_tracks) / max(1, len(seen_tracks)), 1),
        "by_reason": psnap["indexed_by_reason"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Tracker parameter sweep")
    ap.add_argument("--trace", required=True)
    ap.add_argument("--json", default="")
    ap.add_argument("--quick", action="store_true",
                    help="defaults only, no sweep")
    args = ap.parse_args()

    trace = load_trace(args.trace)
    cams = sorted({d.camera for d in trace})
    print(f"trace: {len(trace)} detections, {len(cams)} cameras, "
          f"object floor {object_floor(trace)}\n")

    base = dict(max_cost=1.75, alpha=0.5, max_age=3.0, confirm_after=1.0,
                stationary_distance=0.15, displacement=1.5, heartbeat=60.0,
                scale_change=1.5, index_tentative=True)
    rows = []
    if args.quick:
        rows.append(("defaults", run(trace, **base)))
    else:
        for c in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
            rows.append((f"max_cost {c}", run(trace, **{**base, "max_cost": c})))
        for a in (0.0, 0.3, 0.5, 0.7, 1.0):
            rows.append((f"alpha {a}", run(trace, **{**base, "alpha": a})))
        for m in (1.0, 2.0, 3.0, 5.0, 10.0):
            rows.append((f"max_age {m}", run(trace, **{**base, "max_age": m})))
        for d in (1.0, 1.5, 2.0, 3.0):
            rows.append((f"displacement {d}", run(trace, **{**base, "displacement": d})))
        for hb in (0.0, 30.0, 60.0, 120.0):
            rows.append((f"heartbeat {hb}", run(trace, **{**base, "heartbeat": hb})))
        for ca in (0.0, 1.0, 2.0):
            rows.append((f"confirm_after {ca}", run(trace, **{**base, "confirm_after": ca})))

    print(f"{'variant':<22}{'indexed':>9}{'reduce':>9}{'tracks':>8}"
          f"{'frag':>7}{'tentative':>11}{'LOST TRACKS':>13}")
    print("-" * 82)
    for label, r in rows:
        print(f"{label:<22}{r['indexed']:>9}{r['reduction_pct']:>8.1f}%"
              f"{r['tracks_created']:>8}{r['fragmentation']:>7.2f}"
              f"{r['tentative_pct']:>10.1f}%"
              f"{r['tracks_never_indexed']:>8} {r['tracks_never_indexed_pct']:>4.1f}%")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([{"variant": k, **v} for k, v in rows], fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
