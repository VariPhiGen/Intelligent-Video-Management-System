"""sampler.py — pull frames off a relay stream at up to N per second.

"UP TO" IS LOAD-BEARING. Phase 0 measured delivered frame rates across cameras
on one appliance spanning 0.08 fps to 15 fps — a 190x spread. A sampler that
assumes a frame is available every second spins on a starved camera and silently
under-samples a fast one. This one asks for whatever exists and never blocks the
pipeline waiting.

Frames are grabbed and discarded rather than decoded when they are not due:
`grab()` costs almost nothing, `retrieve()` is where the work is. That is how one
thread per camera stays cheap enough to run twenty of them.

Reconnect uses the same exponential backoff shape as the motion service, for the
same reason: a camera that is down must not become a busy loop.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np

log = logging.getLogger("analytics.sampler")

FrameHandler = Callable[[str, np.ndarray, float], None]


class CameraSampler:
    def __init__(self, slug: str, rtsp_url: str, max_fps: float,
                 on_frame: FrameHandler,
                 on_reconnect: Optional[Callable[[str], None]] = None,
                 open_timeout_ms: int = 5000, read_timeout_ms: int = 5000,
                 backoff_initial: float = 2.0, backoff_max: float = 60.0) -> None:
        self.slug = slug
        self.rtsp_url = rtsp_url
        self._interval = 1.0 / max_fps if max_fps > 0 else 1.0
        self._on_frame = on_frame
        # The motion gate keeps a previous-frame baseline. After an outage the
        # first new frame differs from the last old one by the length of the
        # gap, which is not motion — so the baseline has to be dropped.
        self._on_reconnect = on_reconnect
        self._open_timeout = open_timeout_ms
        self._read_timeout = read_timeout_ms
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Observable state — /health reports these rather than guessing.
        self.state = "CONNECTING"
        self.last_error: Optional[str] = None
        self.frames_sampled = 0
        self.last_frame_at: Optional[float] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"sample-{self.slug}",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _open(self) -> Optional[cv2.VideoCapture]:
        # TCP: the relay is local and lossless matters more than latency here,
        # since a torn frame is a wasted embedding.
        import os
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self._open_timeout)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self._read_timeout)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:                      # noqa: BLE001 — older OpenCV
            pass
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _run(self) -> None:
        backoff = self._backoff_initial
        while not self._stop.is_set():
            cap = self._open()
            if cap is None:
                self.state = "CONNECTING"
                self.last_error = "could not open stream"
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self._backoff_max)
                continue

            self.state = "SAMPLING"
            self.last_error = None
            if self._on_reconnect is not None:
                try:
                    self._on_reconnect(self.slug)
                except Exception:                  # noqa: BLE001
                    pass
            backoff = self._backoff_initial
            next_due = 0.0
            try:
                while not self._stop.is_set():
                    if not cap.grab():
                        self.state = "CONNECTING"
                        self.last_error = "stream ended or read timed out"
                        break
                    now = time.time()
                    if now < next_due:
                        continue               # grabbed and dropped: nearly free
                    ok, frame = cap.retrieve()
                    if not ok or frame is None:
                        continue
                    next_due = now + self._interval
                    self.frames_sampled += 1
                    self.last_frame_at = now
                    try:
                        self._on_frame(self.slug, frame, now)
                    except Exception as exc:   # noqa: BLE001
                        # A pipeline fault must never kill the capture thread —
                        # it is the thing that recovers everything else.
                        log.warning("frame handler failed for %s: %s", self.slug, exc)
            finally:
                cap.release()

            if not self._stop.is_set():
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self._backoff_max)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "frames_sampled": self.frames_sampled,
            "last_frame_at": self.last_frame_at,
            "last_error": self.last_error,
        }
