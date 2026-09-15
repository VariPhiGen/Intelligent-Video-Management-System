"""worker.py — one thread per camera: decode once, analyse once, publish once.

This is the loop that used to exist twice. The motion service ran it to get a
scalar; SmartSearch's sampler ran it to get frames. Both decoded the same
relay independently, which is why their frames never lined up and why regions
computed by one could not be trusted by the other.

THE TWO-PHASE ACQUISITION ECONOMY IS PRESERVED, because it is what makes one
thread per camera affordable:

    cap.grab()     ~50 us  drains the RTSP buffer, keeps the session alive,
                           NO decode
    cap.retrieve() ~2-4 ms the actual decode, at most once per sample interval

Never replace this with cap.read(): it decodes every frame and multiplies CPU
by source_fps / sample_fps — about 5x at 25 fps into the 5 fps this samples at
now, and it was 12x when the rate was 2.

ORDER OF WORK WITHIN A SAMPLE, and it matters:

    1. write the frame to shared memory   consumers can read it the moment
                                          they hear about it
    2. analyse motion                     ~1 ms, on the frame just written
    3. publish the notice                 carries both, describing ONE frame

Publishing last is deliberate. A notice that arrives before its frame is in
shm is a race a consumer cannot defend against; a frame that sits in shm with
no notice yet is simply invisible for a moment, which is harmless.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import cv2

from .config import AppConfig
from .motion import CanonicalMotion
from .notice import NoticePublisher, build_notice
from .ring import FrameRing

log = logging.getLogger("frames.worker")


class CameraWorker:
    """Owns one camera's RTSP session, ring, motion state and thread."""

    def __init__(self, camera: str, rtsp_url: str, config: AppConfig,
                 publisher: NoticePublisher) -> None:
        self.camera = camera
        self.rtsp_url = rtsp_url
        self._cfg = config
        self._pub = publisher
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.state = "CONNECTING"
        self.last_error: Optional[str] = None
        self.frames_published = 0
        self.frames_decoded = 0
        self.reconnects = 0
        self.last_frame_at: Optional[float] = None
        self.motion = CanonicalMotion(config.motion)
        self._ring: Optional[FrameRing] = None
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"frames-{self.camera}",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            if self._ring is not None:
                self._ring.close()
                self._ring = None

    # ── the loop ─────────────────────────────────────────────────────────────
    def _open(self) -> Optional[cv2.VideoCapture]:
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, self._cfg.capture.rtsp_buffer_size)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._cfg.capture.open_timeout_ms)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._cfg.capture.read_timeout_ms)
        except Exception:                                      # noqa: BLE001
            pass                          # older OpenCV builds lack the props
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _ensure_ring(self, frame) -> FrameRing:
        """Built on the FIRST frame, not at construction — the stream's
        resolution is not known until something has been decoded, and guessing
        it would mean a ring that cannot hold what arrives."""
        h, w = frame.shape[:2]
        with self._lock:
            if self._ring is None or self._ring.width != w or self._ring.height != h:
                if self._ring is not None:
                    log.info("[%s] resolution changed to %dx%d; rebuilding ring",
                             self.camera, w, h)
                    self._ring.close()
                self._ring = FrameRing(self.camera, w, h,
                                       channels=frame.shape[2] if frame.ndim == 3 else 1,
                                       slots=self._cfg.capture.ring_slots)
                log.info("[%s] ring %dx%d x%d slots = %.1f MB", self.camera, w, h,
                         self._ring.slots, self._ring.nbytes / 1e6)
            return self._ring

    def _run(self) -> None:
        interval = (1.0 / self._cfg.capture.sample_fps
                    if self._cfg.capture.sample_fps > 0 else 0.5)
        backoff = self._cfg.capture.reconnect_backoff_initial_seconds

        while not self._stop.is_set():
            cap = self._open()
            if cap is None:
                self.state = "CONNECTING"
                self.last_error = "could not open stream"
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2,
                              self._cfg.capture.reconnect_backoff_max_seconds)
                continue

            self.state = "STREAMING"
            self.last_error = None
            backoff = self._cfg.capture.reconnect_backoff_initial_seconds
            # A new connection is a discontinuity, not motion: the first frame
            # differs from the last of the old session by the outage length.
            self.motion.reset()
            next_due = 0.0

            try:
                while not self._stop.is_set():
                    if not cap.grab():
                        self.state = "CONNECTING"
                        self.last_error = "stream ended or read timed out"
                        self.reconnects += 1
                        break
                    now = time.time()
                    if now < next_due:
                        continue                  # grabbed and dropped: free
                    ok, frame = cap.retrieve()
                    if not ok or frame is None:
                        continue
                    next_due = now + interval
                    self.frames_decoded += 1
                    self.last_frame_at = now
                    try:
                        self._publish(frame, now)
                    except Exception as exc:                   # noqa: BLE001
                        # A publish fault must never kill the capture thread —
                        # it is the thing that recovers everything else.
                        log.warning("[%s] publish failed: %s", self.camera, exc)
            finally:
                cap.release()

            if not self._stop.is_set():
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2,
                              self._cfg.capture.reconnect_backoff_max_seconds)

    def _publish(self, frame, ts: float) -> None:
        ring = self._ring if (self._ring is not None
                              and self._ring.width == frame.shape[1]
                              and self._ring.height == frame.shape[0]) \
            else self._ensure_ring(frame)
        seq, slot = ring.publish(frame, ts)        # 1. frame into shm
        result = self.motion.analyse(frame)        # 2. analyse it
        self._pub.publish(self.camera, build_notice(   # 3. tell consumers
            camera=self.camera, ts=ts, seq=seq, slot=slot, path=ring.path,
            width=ring.width, height=ring.height, channels=ring.channels,
            slots=ring.slots, motion=result.to_dict(), epoch=ring.epoch))
        self.frames_published += 1

    # ── observability ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        d = {
            "camera": self.camera,
            "state": self.state,
            "frames_decoded": self.frames_decoded,
            "frames_published": self.frames_published,
            "reconnects": self.reconnects,
            "last_frame_at": self.last_frame_at,
            "last_error": self.last_error,
            "motion": self.motion.snapshot(),
        }
        with self._lock:
            if self._ring is not None:
                d["ring"] = self._ring.snapshot()
        return d
