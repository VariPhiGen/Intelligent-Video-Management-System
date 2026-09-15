#!/usr/bin/env python3
"""mem_probe — where does Smart Search's per-camera memory actually go?

THE QUESTION THIS SETTLES. Capacity runs are killed by memory long before CPU
matters: 1.17 GB of models plus roughly 150-200 MB for every camera added, and
an OOM at 24 cameras inside a 6 GB limit. "Probably decoder buffers" is a guess,
and the fix depends on which guess is right — a smaller queue, fewer buffered
frames, and a different decode strategy are three different pieces of work.

So each contributor is measured on its own, in order, in one process:

    baseline      python + numpy + cv2 imported, nothing open
    models        detector and encoder loaded
    decoders      N cv2.VideoCapture handles OPEN but never read
    decoding      the same handles after each has decoded frames
    frames        N decoded BGR arrays held simultaneously

Reading is separated from opening deliberately: an HEVC decoder allocates its
reference-picture buffers lazily, so a handle that has never decoded costs
almost nothing and tells you nothing.

    python3 scripts/mem_probe.py --cameras 8
"""
from __future__ import annotations

import argparse
import gc
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SEGMENT_DB = os.environ.get("NVR_SEGMENT_DB", "/data/nvr/segments.db")


def rss_mb() -> float:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def segments(count: int) -> list[tuple[str, str]]:
    con = sqlite3.connect(f"file:{SEGMENT_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT camera, filepath, file_size FROM segments "
            "ORDER BY start_epoch DESC LIMIT 500").fetchall()
    finally:
        con.close()
    out, seen = [], set()
    for cam, path, size in rows:
        if size and size > 2_000_000 and os.path.exists(path):
            out.append((cam, path))
            seen.add(cam)
        if len(out) >= count:
            break
    while len(out) < count and out:              # repeat if the site is small
        out.append(out[len(out) % max(1, len(seen))])
    return out[:count]


def step(label: str, before: float) -> float:
    now = rss_mb()
    print(f"  {label:<34} {now:>8.0f} MB   ({now - before:+.0f})")
    return now


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Per-camera memory attribution")
    ap.add_argument("--cameras", type=int, default=8)
    ap.add_argument("--decode-frames", type=int, default=30,
                    help="frames to pull per handle before re-measuring")
    args = ap.parse_args(argv)

    import cv2
    import numpy as np

    n = args.cameras
    print(f"attributing memory for {n} cameras\n")
    base = rss_mb()
    print(f"  {'baseline (imports only)':<34} {base:>8.0f} MB")

    from index.config import AppConfig
    config = AppConfig.from_yaml(os.environ.get("SEARCH_CONFIG", "config.yaml"))

    # ── models ───────────────────────────────────────────────────────────────
    from index.detector import build_selected
    from index.embedder import ClipEmbedder
    from index.selection import DetectorSelector
    m = config.models
    embedder = ClipEmbedder(m.clip_model, m.clip_pretrained, m.device,
                            m.embed_batch_size)
    after_embed = step("+ CLIP encoder", base)
    detector = build_selected(DetectorSelector(m), m.detector_weights,
                              config.ingest.detect_confidence, m.device,
                              m.square_letterbox)
    after_models = step("+ detector", after_embed)

    # ── decoders, opened but idle ────────────────────────────────────────────
    srcs = segments(n)
    if not srcs:
        print("no usable segments", file=sys.stderr)
        return 1
    caps = [cv2.VideoCapture(path) for _cam, path in srcs]
    opened = sum(1 for c in caps if c.isOpened())
    after_open = step(f"+ {opened} decoders OPEN (never read)", after_models)

    # ── decoders that have actually decoded ──────────────────────────────────
    for c in caps:
        for _ in range(args.decode_frames):
            if not c.grab():
                break
    after_grab = step(f"+ each decoded {args.decode_frames} frames", after_open)

    # ── one retrieved frame per camera, held ─────────────────────────────────
    frames = []
    for c in caps:
        ok, f = c.retrieve()
        if ok and f is not None:
            frames.append(f)
    after_frames = step(f"+ {len(frames)} decoded BGR frames held", after_grab)
    if frames:
        f = frames[0]
        print(f"       one frame is {f.shape[1]}x{f.shape[0]}x{f.shape[2]} "
              f"= {f.nbytes/1e6:.1f} MB")

    # ── the queue's own ceiling ──────────────────────────────────────────────
    qsize = config.ingest.queue_size
    per_frame = frames[0].nbytes / 1e6 if frames else 0.0
    print(f"\n  queue ceiling: {qsize} slots x {per_frame:.1f} MB "
          f"= {qsize * per_frame:.0f} MB worst case (shared, not per camera)")

    # ── attribution ──────────────────────────────────────────────────────────
    print("\nattribution")
    print(f"  models (fixed)                 {after_models - base:>8.0f} MB")
    print(f"  decoder handles, idle          {after_open - after_models:>8.0f} MB"
          f"   ({(after_open - after_models)/max(1,opened):>5.1f} MB/camera)")
    print(f"  decoder buffers once decoding  {after_grab - after_open:>8.0f} MB"
          f"   ({(after_grab - after_open)/max(1,opened):>5.1f} MB/camera)")
    print(f"  held frames                    {after_frames - after_grab:>8.0f} MB"
          f"   ({(after_frames - after_grab)/max(1,len(frames)):>5.1f} MB/camera)")
    print(f"  TOTAL per camera               "
          f"{(after_frames - after_models)/max(1,opened):>8.1f} MB")

    del frames
    for c in caps:
        c.release()
    gc.collect()
    print(f"\n  after releasing decoders       {rss_mb():>8.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
