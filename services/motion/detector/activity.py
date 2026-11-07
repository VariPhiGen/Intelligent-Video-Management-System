"""activity.py — the stretches of footage in which something moved, for playback.

NOT THE INCIDENT LOG. An incident (engine.record_trigger) opens when motion is
confirmed and closes when the re-arm cooldown runs out — its length is the
cooldown, and with a cooldown of 0 it never closes at all. The playback
timeline needs something else: when the camera actually saw motion. So this
listens to the same per-sample decision apply_feature makes (feature ≥ the
camera's threshold) and keeps RUNS of it.

A RUN is consecutive motion samples with gaps no longer than `hold_seconds`
(a person pausing mid-stride is one run, not three). A run is only reported
if it would have CONFIRMED — at least as many motion samples as the
confirmation window demands (confirm_ratio × window / interval) — so a single
noisy frame, which never raises an incident, never draws a marker either.

SEGMENTS, like analytics' presence: a long run is cut every `segment_seconds`
so the timeline learns of it while it lasts and every row the VMS stores is
bounded. Runs close on a still sample past the hold, on camera removal, or —
for a camera that simply stopped sending samples — on the wall-clock flush.

Posted to the VMS as presence of kind "motion"
(POST /api/playback/timeline/presence), accepted only for cameras whose
motion detection is on.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("motion.activity")


@dataclass
class _Run:
    start: float
    last: float
    hits: int
    #: A continuation of an already-reported run (after a cut) needs no
    #: re-confirmation — it is the same motion going on.
    confirmed: bool


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class MotionActivityRecorder:
    """Runs of motion per camera, emitted as segments. Never blocks, never raises."""

    def __init__(self, emit: Callable[[list[dict]], None], *, sample_interval: float,
                 confirm_window: float, confirm_ratio: float, hold_seconds: float = 2.0,
                 segment_seconds: float = 120.0, flush_interval: float = 2.0) -> None:
        self._emit = emit
        self.sample_interval = max(0.05, float(sample_interval))
        self.min_hits = max(1, math.ceil(confirm_ratio * confirm_window / self.sample_interval))
        self.hold_seconds = max(self.sample_interval, float(hold_seconds))
        self.segment_seconds = max(10.0, float(segment_seconds))
        self._flush_interval = flush_interval
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.segments_emitted = 0
        self.runs_discarded = 0

    # ── the per-sample hook (apply_feature) ─────────────────────────────────
    def sample(self, camera: str, motion: bool, ts: float) -> None:
        out: list[dict] = []
        with self._lock:
            run = self._runs.get(camera)
            if run is not None and ts - run.last > self.hold_seconds:
                self._close(camera, out)
                run = None
            if motion:
                if run is None:
                    self._runs[camera] = _Run(ts, ts, 1, False)
                else:
                    run.last = max(run.last, ts)
                    run.hits += 1
                    if run.last - run.start >= self.segment_seconds:
                        if self._confirmed(run):
                            out.append(self._segment(camera, run))
                            run.confirmed = True
                        run.start, run.hits = run.last, 1
        self._send(out)

    def end(self, camera: str) -> None:
        """The camera was removed or reconnected: its run ends where it was last seen."""
        out: list[dict] = []
        with self._lock:
            self._close(camera, out)
        self._send(out)

    # ── the timer ───────────────────────────────────────────────────────────
    def flush(self, now: Optional[float] = None) -> int:
        """Close runs of cameras that have stopped sending samples."""
        now = time.time() if now is None else now
        out: list[dict] = []
        with self._lock:
            for cam in [c for c, r in self._runs.items()
                        if now - r.last > self.hold_seconds + 3.0]:
                self._close(cam, out)
        self._send(out)
        return len(out)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="motion-activity",
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        out: list[dict] = []
        with self._lock:
            for cam in list(self._runs):
                self._close(cam, out)
        self._send(out)

    def _run(self) -> None:
        while not self._stop.wait(self._flush_interval):
            try:
                self.flush()
            except Exception as exc:                              # noqa: BLE001
                log.warning("motion activity flush failed: %s", exc)

    # ── internals (lock held) ───────────────────────────────────────────────
    def _confirmed(self, run: _Run) -> bool:
        return run.confirmed or run.hits >= self.min_hits

    def _close(self, camera: str, out: list[dict]) -> None:
        run = self._runs.pop(camera, None)
        if run is None:
            return
        if self._confirmed(run):
            out.append(self._segment(camera, run))
        else:
            self.runs_discarded += 1

    def _segment(self, camera: str, run: _Run) -> dict:
        # The last motion sample stands for the interval it was taken over.
        end = run.last + self.sample_interval
        return {"sensor_id": camera, "kind": "motion", "started_at": _iso(run.start),
                "ended_at": _iso(end), "track_id": "motion", "source": "motion"}

    def _send(self, segments: list[dict]) -> None:
        if not segments:
            return
        self.segments_emitted += len(segments)
        try:
            self._emit(segments)
        except Exception as exc:                                  # noqa: BLE001
            log.warning("motion activity emit failed: %s", exc)

    def snapshot(self) -> dict:
        with self._lock:
            open_runs = len(self._runs)
        return {"open_runs": open_runs, "segments_emitted": self.segments_emitted,
                "runs_discarded": self.runs_discarded, "min_hits": self.min_hits,
                "hold_seconds": self.hold_seconds, "segment_seconds": self.segment_seconds}
