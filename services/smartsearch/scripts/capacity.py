#!/usr/bin/env python3
"""capacity — how many cameras will this machine actually carry?

WHAT THIS ANSWERS, AND WHY IT IS NOT A GUESS FROM THE MODEL BENCHMARK. Measured
2026-09-04 on a live two-camera deployment: the detector accounted for 1.59 s of
a 60 s window — 2.7% of one core — while the container as a whole sat at a 21%
average. Inference was about an eighth of the cost; the rest is decoding 1080p
streams and running the motion gate over them, which no backend choice touches.
Sizing an appliance from a model benchmark therefore overstates what matters and
understates what binds. Only adding cameras and watching the machine answers it.

ISOLATED FROM THE PRODUCTION INDEX, AND THAT IS A CORRECTION. The first version
of this tool registered load-test cameras with the RUNNING service and pointed
them at real relay streams. It measured the right thing and had a consequence
nobody wanted: the live pipeline indexed those frames, writing 122 rows and 122
crop JPEGs under camera slugs that did not exist in the registry. Removing the
cameras on exit did not — and could not — un-write them.

So the harness runs its OWN pipeline in-process: the same config, detector,
encoder and motion gate the service uses, but a store that counts and discards
and a crop directory under /tmp that is removed afterwards. Nothing is
registered anywhere, the production store never sees a row, and the running
service is not contacted at all.

MEASURING THIS PROCESS, NOT THE CONTAINER, for the same reason: the live
deployment keeps indexing throughout, so a cgroup-wide reading would attribute
its work to the test. CPU comes from /proc/self/stat and memory from VmRSS.

DETERMINISTIC REPLAY, NOT LIVE RTSP. Scaling a live test past a handful of
cameras puts many subscribers on one MediaMTX path and it starts refusing them:
a 32-camera step connected 23, which measured the relay rather than Smart
Search. Live scenes also change between steps, so a quiet-hours run recorded
almost no detections and measured decode with inference off by accident. Source
footage is now fixed recorded segments — see scripts/replay.py.

    python3 scripts/capacity.py --workload busy  --steps 2,4,8,16,24,32,40
    python3 scripts/capacity.py --workload quiet --steps 2,4,8,16
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


# ── metering this process ────────────────────────────────────────────────────
def proc_cpu_seconds() -> float:
    """CPU seconds this process has used, user + system.

    /proc/self/stat rather than the cgroup: the live service shares this
    container, and charging its work to the test would inflate every figure.
    """
    try:
        with open("/proc/self/stat", "r", encoding="utf-8") as fh:
            parts = fh.read().rsplit(") ", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / _TICKS   # utime + stime
    except (OSError, IndexError, ValueError):
        return float("nan")


def proc_rss_bytes() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def cpu_limit() -> float:
    """Cores this container may use — not the host's core count when a quota
    is set. See index/hardware.py."""
    from index.hardware import cpu_info
    return cpu_info().usable_cpus


# ── an isolated pipeline ─────────────────────────────────────────────────────
class NullStore:
    """A store that counts and forgets.

    THE ISOLATION THAT MATTERS. IngestPipeline writes a row for every crop that
    survives deduplication; against the real store those rows become searchable
    footage attributed to cameras that do not exist. Counted and dropped here,
    so a capacity run leaves the index exactly as it found it.
    """

    def __init__(self) -> None:
        self.write_failures = 0
        self.rows = 0

    def write_batch(self, domain: str, rows: list) -> int:
        self.rows += len(rows)
        return len(rows)


class TimedDetector:
    """Wraps the real detector to record per-call latency.

    THE DETECTOR IS NOT ASSUMED TO BE THE BOTTLENECK. It was measured at ~4% of
    container CPU at 16 cameras, with decode and the motion gate taking the
    rest, so a capacity run that reported only detector timing would point at
    the wrong thing. This exists to CHECK that share at each step, not to
    confirm it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.latencies: list[float] = []

    @property
    def backend(self):
        return self._inner.backend

    def detect(self, frame):
        t0 = time.monotonic()
        try:
            return self._inner.detect(frame)
        finally:
            self.latencies.append((time.monotonic() - t0) * 1000.0)

    def take(self) -> list[float]:
        out, self.latencies = self.latencies, []
        return out


def build_pipeline(config, crop_dir: str):
    """The service's own pipeline, wired to a store that discards.

    Deliberately the REAL detector, encoder and motion gate: a capacity figure
    from a cheaper stand-in would measure something the appliance never runs.
    """
    from index.detector import build_selected
    from index.embedder import ClipEmbedder
    from index.pipeline import IngestPipeline
    from index.selection import DetectorSelector
    from index.writer import CropWriter

    m = config.models
    embedder = ClipEmbedder(m.clip_model, m.clip_pretrained, m.device,
                            m.embed_batch_size)
    selector = DetectorSelector(m)
    detector = build_selected(selector, m.detector_weights,
                              config.ingest.detect_confidence, m.device,
                              m.square_letterbox)
    timed = TimedDetector(detector)
    pipeline = IngestPipeline(config, timed, embedder, NullStore(),
                              CropWriter(crop_dir),
                              queue_size=config.ingest.queue_size,
                              plate_reader=None)
    pipeline.start()
    return pipeline, timed, selector


def measure(pipeline, detector, samplers, window: float) -> dict:
    """A step's steady-state behaviour, and whether it KEPT UP.

    The capacity question is not "what percentage of CPU". It is whether the
    pipeline processed frames as fast as the cameras produced them. Everything
    else here explains why it did or did not.
    """
    t0 = time.monotonic()
    c0 = proc_cpu_seconds()
    base = {k: getattr(pipeline, k) for k in (
        "frames_queued", "frames_dropped", "frames_evicted", "frames_processed",
        "detector_calls", "crops_embedded", "rows_written",
        "frames_skipped_no_motion")}
    sampled0 = sum(s.frames_sampled for s in samplers)
    detector.take()                                    # discard warm-up timings
    peak = proc_rss_bytes()
    depths: list[int] = []

    end = t0 + window
    while time.monotonic() < end:
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        peak = max(peak, proc_rss_bytes())
        depths.append(pipeline._queue.qsize())

    dt = time.monotonic() - t0
    d = {k: getattr(pipeline, k) - v for k, v in base.items()}
    sampled = sum(s.frames_sampled for s in samplers) - sampled0
    lat = sorted(detector.take())

    def pct(xs, q):
        return round(xs[min(len(xs) - 1, int(len(xs) * q))], 1) if xs else 0.0

    processed_fps = d["frames_processed"] / dt
    sampled_fps = sampled / dt
    return {
        "seconds": round(dt, 1),
        "cores": round((proc_cpu_seconds() - c0) / dt, 3),
        "mem_mb": round(peak / 1e6, 1),
        "sampled_fps": round(sampled_fps, 2),
        "processed_fps": round(processed_fps, 2),
        # THE CAPACITY CRITERION. Below 1.0 the pipeline is not keeping up with
        # what the cameras are producing, whatever the CPU reads.
        "keeping_up": round(processed_fps / sampled_fps, 3) if sampled_fps else 1.0,
        "detector_calls_per_s": round(d["detector_calls"] / dt, 2),
        "embeddings_per_s": round(d["crops_embedded"] / dt, 2),
        "rows_per_s": round(d["rows_written"] / dt, 3),
        "gate_skipped": d["frames_skipped_no_motion"],
        "dropped": d["frames_dropped"],
        "evicted": d["frames_evicted"],
        "queue_depth_mean": round(sum(depths) / len(depths), 1) if depths else 0,
        "queue_depth_max": max(depths) if depths else 0,
        "backlog_age_s": round(pipeline.last_backlog_age, 2),
        "det_p50_ms": pct(lat, 0.5),
        "det_p95_ms": pct(lat, 0.95),
        "det_calls_timed": len(lat),
        # Per-camera sampling, to see whether any camera is being starved.
        "per_camera_sampled": {s.slug: s.frames_sampled for s in samplers},
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Smart Search camera capacity test")
    ap.add_argument("--steps", default="2,4,8,16",
                    help="camera counts to test, comma separated")
    ap.add_argument("--window", type=float, default=60.0,
                    help="measurement seconds per step (default 60)")
    ap.add_argument("--settle", type=float, default=20.0,
                    help="seconds to let a step stabilise before measuring")
    ap.add_argument("--workload", default="busy", choices=["busy", "quiet"],
                    help="busy = footage with people/vehicles (exercises the "
                         "detector and embedder); quiet = near-static footage "
                         "(measures decode + motion gate + queue only)")
    ap.add_argument("--hours", default="",
                    help="local-hour window for source segments, e.g. 15,16. "
                         "Defaults per workload.")
    ap.add_argument("--json", default="", help="write the full report here")
    args = ap.parse_args(argv)

    from index.config import AppConfig
    from replay import BUSY, build_cameras

    # Defaults come from this site's own recordings: 15:00-16:00 local held 224
    # indexed detections, the small hours almost none. Fixed windows keep the
    # input reproducible; replay.select_segments explains why the search index
    # is deliberately NOT used to choose footage.
    hours = (15, 16) if args.workload == BUSY else (1, 5)
    if args.hours:
        a, b = args.hours.split(",")
        hours = (int(a), int(b))

    config = AppConfig.from_yaml(os.environ.get("SEARCH_CONFIG", "config.yaml"))
    crop_dir = tempfile.mkdtemp(prefix="capacity-crops-")
    samplers: list = []
    pipeline = None
    results: list = []
    report: dict = {"workload": args.workload, "hours": list(hours),
                    "sample_fps": config.ingest.max_sample_fps,
                    "settle_s": args.settle, "window_s": args.window,
                    "steps": [], "completed": False}

    def _teardown(*_a) -> None:
        for cam in samplers:
            cam.stop(join_timeout=2.0)
        if pipeline is not None:
            pipeline.stop(join_timeout=5.0)
        shutil.rmtree(crop_dir, ignore_errors=True)

    signal.signal(signal.SIGINT, lambda *a: (_teardown(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *a: (_teardown(), sys.exit(143)))

    try:
        # Memory in stages, so the ~110 MB per camera seen in the live run can
        # be attributed rather than guessed at.
        mem_imports = proc_rss_bytes()
        print("streams               : replay, workload=%s hours=%02d:00-%02d:00 local"
              % (args.workload, hours[0], hours[1]))
        print("cores available       : %s" % cpu_limit())
        print("scratch crops         : %s (deleted on exit)" % crop_dir)
        print("RSS after imports     : %.0f MB" % (mem_imports / 1e6))
        print("loading models...", flush=True)
        pipeline, detector, selector = build_pipeline(config, crop_dir)
        mem_models = proc_rss_bytes()
        spec = detector.backend
        print("detector              : %s/%s on %s (%s)"
              % (spec.runtime, spec.representation, spec.device, spec.selected_by))
        print("RSS after models      : %.0f MB (+%.0f MB)"
              % (mem_models / 1e6, (mem_models - mem_imports) / 1e6))
        report["memory"] = {"after_imports_mb": round(mem_imports / 1e6, 1),
                            "after_models_mb": round(mem_models / 1e6, 1)}
        print("window per step       : %.0fs settle + %.0fs measure"
              % (args.settle, args.window))
        print()
        print("%4s%5s%7s%8s%9s%8s%9s%7s%7s%7s%6s%6s%7s%7s%7s"
              % ("req", "live", "cores", "memMB", "sampl/s", "proc/s", "keep-up",
                 "det/s", "emb/s", "evict", "drop", "qmax", "lag s", "p50ms", "p95ms"))
        print("-" * 106)

        for target in [int(x) for x in args.steps.split(",") if x.strip()]:
            cams, segs = build_cameras(
                args.workload, target, config.ingest.max_sample_fps,
                on_frame=pipeline.submit, on_reconnect=pipeline.reset_gate,
                hours=hours)
            if not cams:
                print("%4d   INVALID: no source segments in %02d:00-%02d:00 local"
                      % (target, hours[0], hours[1]))
                report["steps"].append({"requested": target, "status": "INVALID",
                                        "reason": "no source segments"})
                break
            report.setdefault("segments", [x.to_dict() for x in segs])
            for cam in cams[len(samplers):]:
                cam.start()
                samplers.append(cam)

            # RSS with every decoder open but before measuring: this is the
            # per-camera cost the live run could only infer.
            mem_open = proc_rss_bytes()
            time.sleep(args.settle)
            m = measure(pipeline, detector, samplers, args.window)
            live = sum(1 for c in samplers if c.state == "SAMPLING")
            m["requested"] = target
            m["cameras"] = live
            m["rss_with_decoders_mb"] = round(mem_open / 1e6, 1)
            m["status"] = "OK"
            results.append(m)
            report["steps"].append(m)

            print("%4d%5d%7.2f%8.0f%9.2f%8.2f%9.2f%7.2f%7.2f%7d%6d%6d%7.1f%7.1f%7.1f"
                  % (target, live, m["cores"], m["mem_mb"], m["sampled_fps"],
                     m["processed_fps"], m["keeping_up"], m["detector_calls_per_s"],
                     m["embeddings_per_s"], m["evicted"], m["dropped"],
                     m["queue_depth_max"], m["backlog_age_s"], m["det_p50_ms"],
                     m["det_p95_ms"]), flush=True)

            if live < target:
                # A harness problem, not a capacity limit. Saying so stops it
                # being read as the machine giving up.
                m["status"] = "INVALID"
                m["reason"] = "only %d of %d samplers running" % (live, target)
                print("     INVALID: %s" % m["reason"])

            # THE CAPACITY CRITERION: a sustained inability to keep up with the
            # 1 fps per camera being produced, shown by a processing deficit or
            # by frames being discarded. Never a CPU percentage.
            elif m["keeping_up"] < 0.9 or m["evicted"] > 0 or m["dropped"] > 0:
                m["status"] = "CAPACITY_FAILURE"
                print()
                print("  CAPACITY FAILURE at %d cameras: processed %.0f%% of "
                      "sampled, %d evicted, %d dropped"
                      % (live, m["keeping_up"] * 100, m["evicted"], m["dropped"]))
                break
        report["completed"] = True
    finally:
        _teardown()
        if args.json:
            try:
                with open(args.json, "w", encoding="utf-8") as fh:
                    json.dump(report, fh, indent=2, default=str)
                print("\nreport written to %s" % args.json)
            except OSError as exc:
                print("could not write report: %s" % exc, file=sys.stderr)

    if len(results) >= 2:
        first, last = results[0], results[-1]
        dn = last["cameras"] - first["cameras"]
        if dn > 0:
            per_cam = (last["cores"] - first["cores"]) / dn
            per_mem = (last["mem_mb"] - first["mem_mb"]) / dn
            print()
            print(f"marginal cost per camera : {per_cam:.3f} cores, {per_mem:.0f} MB")
            if per_cam > 0:
                base = first["cores"] - per_cam * first["cameras"]
                for share in (0.5, 0.7):
                    budget = cpu_limit() * share - base
                    print(f"  cameras at {share:.0%} of the box: "
                          f"~{int(max(0, budget / per_cam))}")
            print()
            print("Drop rate is the number to watch, not CPU: the pipeline does")
            print("not slow down under load, it discards the newest frame and")
            print("counts it. Any non-zero drop% is past capacity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
