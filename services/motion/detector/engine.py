"""engine.py — camera lifecycle + the motion-event log.

MotionEngine owns one dedicated thread per camera (see worker.py for why a
shared pool is wrong here) and a bounded in-memory event log. All engine
state is in-memory by design: the camera-mgmt registry is the source of
truth and re-asserts cameras after a restart via its reconcile loop, exactly
like it does for MediaMTX and the NVR.

Events are dicts shared by reference between the log deque and the owning
camera's ``open_event`` — closing an incident just stamps ``ended_at`` on
the same dict the log already holds.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from detector.config import AppConfig
from detector.state import CameraState
from detector.worker import capture_loop

logger = logging.getLogger("motion.engine")

_EVENT_LOG_SIZE = 1000


class MotionEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self._cameras: dict[str, CameraState] = {}
        #: Present only in broker mode. One subscription serves every camera,
        #: so it is owned here rather than per-camera.
        self._subscriber = None
        self._threads: dict[str, threading.Thread] = {}
        self._registry_lock = threading.Lock()
        self._events: deque[dict] = deque(maxlen=_EVENT_LOG_SIZE)
        self._event_seq = 0
        self._event_lock = threading.Lock()

    # ── Camera lifecycle ──────────────────────────────────────────────────────

    def add_camera(self, name: str, rtsp_url: str, sensitivity: Optional[str] = None) -> CameraState:
        """Register a camera and start its capture thread immediately.

        Raises KeyError if the name is already registered (the API maps this
        to 409, which the registry sync treats as success).
        """
        preset, threshold = self.config.detection.threshold_for(sensitivity)
        with self._registry_lock:
            if name in self._cameras:
                raise KeyError(name)
            cam = CameraState(
                name=name, rtsp_url=rtsp_url,
                sensitivity=preset, threshold=threshold,
            )
            self._cameras[name] = cam
            if self._subscriber is not None:
                # BROKER MODE: no capture thread and no RTSP session. The
                # frame broker already decoded this camera and computed the
                # changed-pixel fraction; notices arrive on the subscription
                # thread and are fed to the same apply_feature the capture
                # loop uses. The window is sized from the observed cadence on
                # the first samples.
                cam.update_maxlen(self.config.detection.confirm_window_seconds,
                                  self.config.detection.sample_interval_seconds)
                logger.info("[%s] added via broker (sensitivity=%s, threshold=%.4f)",
                            name, preset, threshold)
                return cam
            thread = threading.Thread(
                target=capture_loop, args=(cam, self.config, self),
                name=f"motion-{name}", daemon=True,
            )
            self._threads[name] = thread
        thread.start()
        logger.info("[%s] added (sensitivity=%s, threshold=%.4f)", name, preset, threshold)
        return cam

    def remove_camera(self, name: str) -> bool:
        """Stop the camera's thread and drop it. Returns False if unknown."""
        with self._registry_lock:
            cam = self._cameras.pop(name, None)
            thread = self._threads.pop(name, None)
        if cam is None:
            return False
        cam.stop.set()
        if thread is not None:
            # Bounded join outside the registry lock; the loop notices stop
            # within one read timeout even mid-reconnect (abortable backoff).
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("[%s] capture thread still draining after stop", name)
        logger.info("[%s] removed", name)
        return True

    def list_cameras(self) -> list[CameraState]:
        """Snapshot of live camera state. Used by the notice subscriber to run
        cooldown re-arming, which in capture mode each camera's own thread
        does for itself."""
        with self._registry_lock:
            return list(self._cameras.values())

    def attach_subscriber(self, subscriber) -> None:
        self._subscriber = subscriber

    def source_snapshot(self) -> dict:
        if self._subscriber is not None:
            return self._subscriber.snapshot()
        return {"source": "capture",
                "sample_interval_seconds":
                    self.config.detection.sample_interval_seconds}

    def get_camera(self, name: str) -> Optional[CameraState]:
        with self._registry_lock:
            return self._cameras.get(name)

    def snapshot(self) -> list[dict]:
        """Locked point-in-time view of every camera, for the API."""
        with self._registry_lock:
            cams = list(self._cameras.values())
        out = []
        for cam in cams:
            with cam.lock:
                out.append(cam.to_dict())
        return out

    def reset_camera(self, name: str) -> Optional[str]:
        """Manually re-arm a TRIGGERED camera. Returns the new state, or None
        if the camera is unknown."""
        cam = self.get_camera(name)
        if cam is None:
            return None
        closed_event = None
        with cam.lock:
            if cam.state == "TRIGGERED":
                cam.transition_to("MONITORING")
                closed_event, cam.open_event = cam.open_event, None
                cam.triggered_at = None
                cam.motion_window.clear()
            state = cam.state
        if closed_event:
            self.close_event(closed_event)
            logger.info("[%s] manually reset to MONITORING", name)
        return state

    def stop_all(self) -> None:
        with self._registry_lock:
            names = list(self._cameras)
        for name in names:
            self.remove_camera(name)

    # ── Event log ─────────────────────────────────────────────────────────────

    def record_trigger(self, camera: str) -> dict:
        """Open a motion incident; returns the (mutable, shared) event dict."""
        with self._event_lock:
            self._event_seq += 1
            event = {
                "id": self._event_seq,
                "camera": camera,
                "started_at": datetime.now(tz=timezone.utc).isoformat(),
                "ended_at": None,
            }
            self._events.append(event)
        return event

    def close_event(self, event: dict) -> None:
        with self._event_lock:
            if event.get("ended_at") is None:
                event["ended_at"] = datetime.now(tz=timezone.utc).isoformat()

    def list_events(
        self, since_id: int = 0, camera: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        """Newest-first event list. ``since_id`` enables cheap incremental
        polling: pass the highest id you've seen, get only newer events."""
        with self._event_lock:
            events = [
                dict(e) for e in reversed(self._events)
                if e["id"] > since_id and (camera is None or e["camera"] == camera)
            ]
        return events[:limit]

    @property
    def camera_count(self) -> int:
        with self._registry_lock:
            return len(self._cameras)
