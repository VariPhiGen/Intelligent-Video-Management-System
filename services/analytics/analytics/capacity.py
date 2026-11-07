"""capacity.py — the most accurate detector this machine can keep up with.

WHY THIS EXISTS. Model size is a trade between two failures an operator sees
differently but equally. Too small: the index fills with people who are a cloth
on a truck, a truck's flank and a blocked lens (yolov8n, site cameras, night,
2026-10-26 — four false people out of six, and a missed rider). Too large for
the CPU: the queue overflows, frames are evicted, and a busy gate goes
unindexed exactly when it matters. Neither failure announces itself, so the
choice cannot be a constant in a config file shipped to every machine.

THE RULE: walk `detector_candidates` most accurate first and take the first
whose MEASURED throughput covers the cameras' demand with headroom. Nothing fits
→ the fastest, flagged over capacity. Pinning a weights path skips all of it.

THE NUMBERS ARE MEASURED HERE, ONCE. A detector is timed on this machine with
the backend it will actually run (OpenVINO, or torch where OpenVINO cannot run)
and the result cached under the models volume, keyed by CPU, runtime, weights
and threads. A restart or a wake from hibernation reads the cache and costs
nothing; new hardware, a runtime upgrade or new weights miss it and re-measure.

DEMAND IS AN ESTIMATE, AND SAYS SO. A Smart Search camera is motion-gated, so
it needs `camera_fps x motion_share`; an activity camera sees every sampled
frame, so it needs `camera_fps` in full, on top. motion_share defaults to 0.5:
a busy gate, not a quiet night (measured 0.35% at night on this site), because
under-provisioning loses the frames that matter and over-provisioning only
costs a smaller model.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("analytics.capacity")

AUTO = "auto"

#: Timed runs per measurement, after warm-up. Few, because a warm-up holds
#: every camera's first detections back, and the median of six is stable to a
#: few percent on a quiet machine.
RUNS = 6
WARMUP_RUNS = 2


def demand_fps(search_cameras: int, activity_cameras: int, camera_fps: float,
               motion_share: float) -> float:
    """Detector calls per second this camera set can ask for at a busy moment."""
    return (max(0, search_cameras) * camera_fps * motion_share
            + max(0, activity_cameras) * camera_fps)


def measure_fps(detector, runs: int = RUNS, warmup: int = WARMUP_RUNS) -> float:
    """Sustained single-stream throughput: 1 / median latency of `detect`.

    The real call — letterbox, inference, decode, NMS — on a frame the size the
    broker delivers. Content barely moves a CNN's cost; noise is used so no
    sample image has to ship.
    """
    frame = np.random.default_rng(0).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    for _ in range(warmup):
        detector.detect(frame)
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        detector.detect(frame)
        times.append(time.perf_counter() - start)
    return 1.0 / max(statistics.median(times), 1e-6)


def ensure_weights(weights: str) -> None:
    """Have the weights on disk before anything tries to load them. RAISES.

    Checked here, not left to the loader, because the backend selector would
    count a missing file against OpenVINO's failure budget — one absent
    candidate must not cost this CPU its fast runtime for the life of the
    process. Ultralytics fetches a catalogue asset to exactly a full path
    (config.yaml explains why the paths are absolute); offline, the fetch fails
    and the candidate is skipped.
    """
    if not os.path.dirname(weights) or os.path.exists(weights):
        return
    try:
        from ultralytics.utils.downloads import attempt_download_asset
        attempt_download_asset(weights)
    except Exception as exc:                                        # noqa: BLE001
        raise FileNotFoundError(f"{weights} is not on disk and could not be "
                                f"downloaded ({type(exc).__name__}: {exc})") from None
    if not os.path.exists(weights):
        raise FileNotFoundError(f"{weights} is not on disk and could not be downloaded")


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.split(":")[0].strip() in ("model name", "Hardware", "CPU part"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def machine_key(weights: str, runtime: str, runtime_version: Optional[str],
                threads: int) -> str:
    """Identity of one measurement: what would make the number different."""
    try:
        size = os.path.getsize(weights)
    except OSError:
        size = -1
    ident = {
        "cpu": _cpu_model(), "cpus": os.cpu_count(), "arch": platform.machine(),
        "runtime": runtime, "runtime_version": runtime_version,
        "weights": os.path.basename(weights), "weights_size": size,
        "threads": threads,
    }
    return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:24]


class CapacityCache:
    """Measured throughput per machine_key, in one small JSON file.

    Never raises: a cache that cannot be read or written costs a measurement,
    not a detector.
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def get(self, key: str) -> Optional[float]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                value = json.load(fh).get(key, {}).get("fps")
            return float(value) if value else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def put(self, key: str, fps: float, detail: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            try:
                with open(self._path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data[key] = {"fps": round(fps, 2), "measured_at": time.time(), **detail}
            # Atomic replace: two processes sharing the volume never read half a file.
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._path), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("could not record detector capacity (%s); it will be "
                        "measured again on the next warm-up", exc)


@dataclass
class Choice:
    """What was picked and why — reported verbatim on /health."""
    weights: str
    mode: str                                   # "auto" | "pinned"
    reason: str
    demand_fps: float = 0.0
    capacity_fps: Optional[float] = None
    over_capacity: bool = False
    #: Every candidate looked at, in order: weights → measured fps (None = not
    #: measured because a more accurate one already fitted).
    measured: dict = field(default_factory=dict)
    #: Where the current capacity and demand figures come from: "estimate"
    #: (start-up measurement, assumed motion share) until enough live traffic
    #: has been seen, then "live" (timed real calls, observed peak load).
    basis: str = "estimate"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["model"] = os.path.splitext(os.path.basename(self.weights))[0]
        return d


def choose(candidates: list[str], demand: Callable[[], float], headroom: float,
           build: Callable[[str], object], fps_of: Callable[[str, object], float]):
    """(Choice, detector) — the most accurate candidate that keeps up.

    `demand` is read AT EACH COMPARISON, not once up front: camera-mgmt pushes
    the whole camera set in one burst, the first camera starts the warm-up, and
    a demand read before the model had loaded would count one camera of six.

    `build(weights)` returns a loaded detector (or raises); `fps_of(weights,
    detector)` returns its throughput, cached or measured. Candidates that
    cannot be built are skipped with their error recorded; only when none can
    be built does this raise, which the engine treats as any failed load.
    """
    measured: dict[str, Optional[float]] = {w: None for w in candidates}
    fallback = None                              # fastest seen: (fps, weights, detector)
    errors = []
    for weights in candidates:
        try:
            det = build(weights)
        except Exception as exc:                                    # noqa: BLE001
            errors.append(f"{os.path.basename(weights)}: {type(exc).__name__}: {exc}")
            measured[weights] = "unavailable"
            continue
        fps = fps_of(weights, det)
        measured[weights] = round(fps, 1)
        need = demand()
        if fps >= need * headroom:
            name = os.path.basename(weights)
            why = (f"{name} keeps up: {fps:.1f} fps measured vs {need:.1f} fps "
                   f"needed (x{headroom:g} headroom)")
            if weights != candidates[0]:
                why += "; more accurate models are too slow for this machine and camera load"
            return Choice(weights, AUTO, why, need, fps, False, measured), det
        if fallback is None or fps > fallback[0]:
            fallback = (fps, weights, det)
    if fallback is None:
        raise RuntimeError("no detector candidate could be loaded: " + "; ".join(errors))
    fps, weights, det = fallback
    need = demand()
    return Choice(
        weights, AUTO,
        (f"no candidate keeps up with {need:.1f} fps (x{headroom:g}); using the "
         f"fastest, {os.path.basename(weights)} at {fps:.1f} fps — expect dropped "
         f"frames at busy times. Fewer cameras or activity rules, or a faster machine, "
         f"would fix it."),
        need, fps, True, measured), det
