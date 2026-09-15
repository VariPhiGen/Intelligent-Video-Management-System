#!/usr/bin/env python3
"""batch_bench — does batching the detector help on this CPU, and by how much?

MEASURED, NOT ASSUMED. Batching is worth it only if the runtime is
under-utilising the CPU on a single image. OpenVINO's ultralytics export is
built for batch=1 and loads in LATENCY mode ("Using OpenVINO LATENCY mode for
batch=1 inference on CPU" appears in the service log), which optimises for the
time of ONE inference rather than aggregate throughput. Whether a larger batch
in THROUGHPUT mode beats that on a 16-core CPU is an empirical question.

WHAT IS COMPARED, on identical frames from this site's own recordings:

    torch      N frames one at a time      vs   one call with a list of N
    openvino   N frames one at a time      vs   an IR exported with batch=N

The second row is the point: an OpenVINO IR has its batch size COMPILED IN, so
"batching" means re-exporting, not passing a longer list. A batch=1 IR handed
four images simply runs four inferences.

ALSO MEASURED SEPARATELY: CLIP embedding, which already batches (batch_size 32
in config) and is a different question from detector batching — the pipeline
embeds every surviving crop from one frame in a single call, so it is already
batched per frame but never across frames.

    python3 scripts/batch_bench.py --frames 24 --batches 1,2,4,8
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def real_frames(count: int) -> list:
    """Frames from the NVR's recorded segments, at native resolution.

    Reuses backend_parity's segment reader rather than a second copy: the
    timestamp→segment resolution and the un-transcoded decode are exactly the
    same problem, and two implementations would drift.
    """
    import sqlite3
    from datetime import datetime, timezone
    from backend_parity import frame_at, SEGMENT_DB

    con = sqlite3.connect(f"file:{SEGMENT_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT camera, start_epoch, duration FROM segments "
        "ORDER BY start_epoch DESC LIMIT 400").fetchall()
    con.close()

    frames, seen = [], set()
    for camera, start, dur in rows:
        if len(frames) >= count:
            break
        if camera in seen and len(seen) < 5:      # spread across cameras first
            continue
        seen.add(camera)
        when = datetime.fromtimestamp(start + dur / 2, tz=timezone.utc)
        f = frame_at(camera, when)
        if f is not None:
            frames.append(f)
    # Top up from whatever decodes if the spread pass came up short.
    for camera, start, dur in rows:
        if len(frames) >= count:
            break
        when = datetime.fromtimestamp(start + dur / 3, tz=timezone.utc)
        f = frame_at(camera, when)
        if f is not None:
            frames.append(f)
    return frames[:count]


def time_calls(model, frames: list, batch: int, classes, conf: float,
               rounds: int = 3) -> dict:
    """Wall clock for the whole set, fed `batch` images per predict call."""
    # Warm-up is discarded: the first call is where ultralytics builds the
    # backend, and folding that into a throughput figure would flatter batch=1
    # (it pays the cost once over more calls).
    #
    # WARMED WITH A FULL BATCH, because an OpenVINO IR has its batch size
    # compiled in: handing a batch=2 model one image raises "model input
    # (shape=[2,3,640,640]) and the tensor (shape=(1,3,640,640)) are
    # incompatible". The shape is fixed, not a maximum.
    warm = frames[:batch] if batch > 1 else frames[0]
    model(warm, classes=classes, conf=conf, verbose=False, rect=False)

    per_round = []
    for _ in range(rounds):
        t0 = time.monotonic()
        for i in range(0, len(frames), batch):
            chunk = frames[i:i + batch]
            if batch > 1 and len(chunk) < batch:
                # A compiled batch size is exact: pad the tail rather than
                # sending a short chunk the IR will refuse.
                chunk = chunk + [chunk[-1]] * (batch - len(chunk))
            model(chunk if batch > 1 else chunk[0],
                  classes=classes, conf=conf, verbose=False, rect=False)
        per_round.append(time.monotonic() - t0)
    best = min(per_round)
    return {
        "batch": batch,
        "total_s": round(best, 3),
        "per_frame_ms": round(best / len(frames) * 1000, 2),
        "fps": round(len(frames) / best, 2),
        "rounds_s": [round(r, 3) for r in per_round],
    }


def rss_mb() -> float:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Detector batching benchmark")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--batches", default="1,2,4,8")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    from ultralytics import YOLO
    from index.config import AppConfig
    from index.detector import _PERSON, _VEHICLE

    config = AppConfig.from_yaml(os.environ.get("SEARCH_CONFIG", "config.yaml"))
    conf = config.ingest.detect_confidence
    classes = [_PERSON, *_VEHICLE]
    sizes = [int(b) for b in args.batches.split(",") if b.strip()]

    print("decoding real frames from NVR segments…", flush=True)
    frames = real_frames(args.frames)
    if len(frames) < 2:
        print("could not decode enough frames", file=sys.stderr)
        return 1
    shapes = {f"{f.shape[1]}x{f.shape[0]}" for f in frames}
    print(f"  {len(frames)} frames, resolutions: {', '.join(sorted(shapes))}")

    report: dict = {"frames": len(frames), "resolutions": sorted(shapes),
                    "confidence": conf, "results": {}}
    work = tempfile.mkdtemp(prefix="batchbench-")
    try:
        base = os.path.join(work, "yolov8n.pt")
        YOLO(config.models.detector_weights)                # ensure downloaded
        shutil.copy2(config.models.detector_weights, base)

        print("\n--- torch (a list is batched internally) ---")
        print(f"{'batch':>6}{'per frame ms':>14}{'fps':>9}{'RSS MB':>9}")
        tm = YOLO(base)
        for b in sizes:
            r = time_calls(tm, frames, b, classes, conf)
            r["rss_mb"] = round(rss_mb(), 0)
            report["results"][f"torch_b{b}"] = r
            print(f"{b:>6}{r['per_frame_ms']:>14}{r['fps']:>9}{r['rss_mb']:>9.0f}")
        del tm

        print("\n--- openvino (batch is COMPILED INTO the IR) ---")
        print(f"{'batch':>6}{'export s':>10}{'per frame ms':>14}{'fps':>9}{'RSS MB':>9}")
        for b in sizes:
            t0 = time.monotonic()
            try:
                # A separate IR per batch size: the exported graph fixes it.
                src = os.path.join(work, f"b{b}.pt")
                shutil.copy2(base, src)
                ir = str(YOLO(src).export(format="openvino", batch=b, verbose=False))
            except Exception as exc:                           # noqa: BLE001
                print(f"{b:>6}   export failed: {type(exc).__name__}: {exc}")
                report["results"][f"openvino_b{b}"] = {"error": str(exc)}
                continue
            exp = time.monotonic() - t0
            om = YOLO(ir)
            r = time_calls(om, frames, b, classes, conf)
            r["export_s"] = round(exp, 1)
            r["rss_mb"] = round(rss_mb(), 0)
            report["results"][f"openvino_b{b}"] = r
            print(f"{b:>6}{exp:>10.1f}{r['per_frame_ms']:>14}{r['fps']:>9}{r['rss_mb']:>9.0f}")
            del om
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # What a batch COSTS: the newest frame in a batch of N waits for N-1
    # others to arrive. At 1 fps per camera that wait is set by camera count,
    # not by inference speed.
    print("\n--- what waiting for a batch costs, at 1 fps per camera ---")
    print(f"{'cameras':>8}{'batch 2':>10}{'batch 4':>10}{'batch 8':>10}   (seconds to fill)")
    for cams in (2, 4, 8, 16, 32):
        row = "".join(f"{max(0, b - 1) / cams:>10.2f}" for b in (2, 4, 8))
        print(f"{cams:>8}{row}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nreport written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
