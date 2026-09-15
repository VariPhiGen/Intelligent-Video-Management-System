#!/usr/bin/env python3
"""activity_survey — which camera-hours actually contain activity?

WHY A SURVEY IS NEEDED AT ALL. Choosing footage by clock window worked for the
first study only because cam2 happened to be busy then. Extending the study to
another camera means finding where that camera is busy, and the obvious cheap
proxy does not work here: segment file size sits within 0.98-1.09x of each
camera's own median across every hour, because these encoders are
constant-bitrate. Size carries no motion signal on this site.

So this samples a handful of frames from each camera-hour and runs the real
detector over them. It is a SURVEY, not a selection on the existing SmartSearch
index — the index is post-dedup output of an older pipeline, and picking
footage by it would make the study's input depend on the behaviour under study.
Running the detector fresh over unbiased time samples avoids that.

Deliberately coarse: a few frames per hour is enough to rank hours, and cheap
enough to scan a day in minutes.

    python3 scripts/activity_survey.py --per-hour 8
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SEGMENT_DB = os.environ.get("NVR_SEGMENT_DB", "/data/nvr/segments.db")
TZ_OFFSET_HOURS = 5.5


def camera_hours(min_segments: int) -> dict:
    con = sqlite3.connect(f"file:{SEGMENT_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT camera, filepath, start_epoch, file_size FROM segments "
            "WHERE file_size > 2000000 ORDER BY start_epoch").fetchall()
    finally:
        con.close()
    buckets: dict = collections.defaultdict(list)
    for cam, path, ep, _size in rows:
        if not os.path.exists(path):
            continue
        lt = datetime.datetime.fromtimestamp(ep + TZ_OFFSET_HOURS * 3600,
                                             datetime.timezone.utc)
        buckets[(cam, lt.strftime("%m-%d %H"))].append(path)
    return {k: v for k, v in buckets.items() if len(v) >= min_segments}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Detector-based activity survey")
    ap.add_argument("--per-hour", type=int, default=8,
                    help="frames sampled per camera-hour")
    ap.add_argument("--min-segments", type=int, default=20)
    ap.add_argument("--exclude", default="",
                    help="comma separated cameras to skip")
    args = ap.parse_args(argv)

    import cv2
    from index.config import AppConfig
    from index.detector import build_selected
    from index.selection import DetectorSelector

    config = AppConfig.from_yaml(os.environ.get("SEARCH_CONFIG", "config.yaml"))
    m = config.models
    detector = build_selected(DetectorSelector(m), m.detector_weights,
                              config.ingest.detect_confidence, m.device,
                              m.square_letterbox)
    skip = {c.strip() for c in args.exclude.split(",") if c.strip()}

    buckets = camera_hours(args.min_segments)
    print(f"surveying {len(buckets)} camera-hours, {args.per_hour} frames each\n")
    scored = []
    for (cam, hour), paths in sorted(buckets.items()):
        if cam in skip:
            continue
        step = max(1, len(paths) // args.per_hour)
        picked = paths[::step][:args.per_hour]
        persons = vehicles = frames = multi = 0
        for path in picked:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                continue
            try:
                # Mid-segment, seeking early and reading forward so HEVC has
                # its reference frames — same approach as the other tools.
                cap.set(cv2.CAP_PROP_POS_MSEC, 28_000)
                frame = None
                for _ in range(200):
                    ok, cand = cap.read()
                    if not ok or cand is None:
                        break
                    frame = cand
                    if cap.get(cv2.CAP_PROP_POS_MSEC) >= 30_000:
                        break
                if frame is None:
                    continue
                frames += 1
                dets = list(detector.detect(frame))
                p = sum(1 for d in dets if d.domain == "person")
                v = sum(1 for d in dets if d.domain == "vehicles")
                persons += p
                vehicles += v
                if p + v >= 2:
                    multi += 1
            finally:
                cap.release()
        if frames:
            scored.append((persons + vehicles, persons, vehicles, multi,
                           frames, cam, hour))

    scored.sort(reverse=True)
    print(f"{'camera':<14}{'hour':<9}{'frames':>7}{'person':>8}{'vehicle':>8}"
          f"{'multi':>7}{'det/frame':>11}")
    print("-" * 64)
    for total, p, v, multi, frames, cam, hour in scored[:20]:
        print(f"{cam:<14}{hour:<9}{frames:>7}{p:>8}{v:>8}{multi:>7}"
              f"{total/frames:>11.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
