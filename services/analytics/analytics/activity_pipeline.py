"""activity_pipeline.py — the CPU activity path: every frame, its own YOLO, its own tracker.

    broker frame (5 FPS, EVERY frame) → OpenVINO YOLO → tracker → ActivityEngine

WHY NOT THE SMART SEARCH PIPELINE. DetectionPipeline is motion gated: a frame
the broker found no motion in never reaches its detector. That is right for an
index — a still scene has nothing new to record — and fatal for activities: a
parked car or a group standing still produces no motion, so it would produce
no detections, its track would expire, and "how long has it been there" could
never be answered. So activities get their own path, fed every frame the
broker samples for an activity camera, whether anything moved or not.

OPTION 1, DELIBERATELY: A SECOND DETECTOR. This pipeline runs its own YOLO
instance (built by the same selector, so OpenVINO on CPU) and its own tracker.
Sharing inference with Smart Search would couple the two paths' gating and
queueing; that trade is to be measured, not assumed. Smart Search is untouched.

THE TRACKER IS THE EXISTING ONE (analytics/tracking.py), a separate instance
with the same configuration. It is updated on every frame, including frames
with no detection, so tracks age out on real time; retired ids are handed to
the activities so their per-track state goes with them.

BACKPRESSURE IS A DROP OF THE OLDEST, as in every queue in this service.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

import numpy as np

from .activities.base import FrameContext, TrackedObject
from .config import AppConfig
from .nested_boxes import suppress_nested
from .tracking import ObjectTracker

log = logging.getLogger("analytics.activity_pipeline")


class ActivityPipeline:
    def __init__(self, config: AppConfig, detector, activities) -> None:
        self._cfg = config
        self._detector = detector
        self._activities = activities
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, config.activities.queue_size))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        t = config.tracking
        self._tracker = ObjectTracker(
            enabled=t.enabled,
            max_association_cost=t.max_association_cost,
            velocity_alpha=t.velocity_alpha,
            confirm_after_seconds=t.confirm_after_seconds,
            max_age_seconds=t.max_age_seconds,
            stationary_after_seconds=t.stationary_after_seconds,
            stationary_distance=t.stationary_distance,
            max_tracks_per_key=t.max_tracks_per_key,
        )

        self.frames_queued = 0
        self.frames_evicted = 0
        self.frames_dropped = 0
        self.frames_processed = 0
        #: Frames for a camera whose activities are no longer runnable.
        self.frames_skipped_no_work = 0
        self.detector_calls = 0
        self.detections_seen = 0
        self.detections_nested = 0
        self.tracks_retired = 0
        self.last_backlog_age = 0.0
        self.last_error: Optional[str] = None

    # ── intake ──────────────────────────────────────────────────────────────
    def submit(self, slug: str, frame: np.ndarray, ts: float) -> None:
        """Queue a frame, preferring the NEWEST when there is no room."""
        try:
            self._queue.put_nowait((slug, frame, ts))
            self.frames_queued += 1
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
            self.frames_evicted += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait((slug, frame, ts))
            self.frames_queued += 1
        except queue.Full:
            self.frames_dropped += 1

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="activities", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def reset(self, slug: str) -> None:
        """The stream was interrupted: identity across the gap is guesswork."""
        self._tracker.reset(slug)
        self._retire(self._tracker.drain_expired())
        self._activities.reset(slug)

    def forget(self, slug: str) -> None:
        self._tracker.forget(slug)
        self._retire(self._tracker.drain_expired())

    def _retire(self, expired: list) -> None:
        if expired:
            self.tracks_retired += len(expired)
            self._activities.forget_tracks(expired)

    # ── worker ──────────────────────────────────────────────────────────────
    def _run(self) -> None:
        log.info("activity worker started (queue=%d) — every frame, no motion gate",
                 self._queue.maxsize)
        while not self._stop.is_set():
            try:
                slug, frame, ts = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self.last_backlog_age = max(0.0, time.time() - ts)
            try:
                self.process(slug, frame, ts)
                self.frames_processed += 1
            except Exception as exc:                                   # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("activity frame failed for %s", slug)

    def process(self, slug: str, frame: np.ndarray, ts: float) -> None:
        """One frame through detector → tracker → activities. No gate."""
        if not self._activities.has_work(slug):
            self.frames_skipped_no_work += 1
            return
        domains = self._activities.wanted_domains(slug)
        self.detector_calls += 1
        found = [d for d in self._detector.detect(frame) if d.domain in domains]
        if len(found) >= 2:
            kept = suppress_nested(found, self._cfg.detect.nested_containment)
            self.detections_nested += len(found) - len(kept)
            found = kept
        self.detections_seen += len(found)

        # Updated on EVERY frame, detections or not, so tracks age on time.
        tracked = self._tracker.update(slug, found, ts)
        self._retire(self._tracker.drain_expired())

        h, w = frame.shape[:2]
        objects = []
        for td in tracked:
            det, track = td.detection, td.track
            x1, y1, x2, y2 = det.xyxy
            objects.append(TrackedObject(
                track_id=track.track_id if track is not None else None,
                domain=det.domain, label=det.label, confidence=float(det.confidence),
                bbox=(x1 / w, y1 / h, x2 / w, y2 / h),
                confirmed=bool(track.confirmed) if track is not None else False,
                is_new=bool(td.is_new),
                state=track.state if track is not None else None,
                first_seen=track.first_seen if track is not None else None,
            ))
        self._activities.observe(FrameContext(camera=slug, ts=ts, width=w, height=h,
                                               objects=tuple(objects), frame=frame))

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "state": "RUNNING" if self._thread is not None and self._thread.is_alive() else "STOPPED",
            "motion_gated": False,
            "detector": type(self._detector).__name__,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "frames_queued": self.frames_queued,
            "frames_evicted": self.frames_evicted,
            "frames_dropped": self.frames_dropped,
            "frames_processed": self.frames_processed,
            "frames_skipped_no_work": self.frames_skipped_no_work,
            "backlog_age_s": round(self.last_backlog_age, 2),
            "detector_calls": self.detector_calls,
            "detections_seen": self.detections_seen,
            "detections_nested": self.detections_nested,
            "tracks_retired": self.tracks_retired,
            "tracker": self._tracker.snapshot(),
            "last_error": self.last_error,
        }
