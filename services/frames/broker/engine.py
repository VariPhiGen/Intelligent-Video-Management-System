"""engine.py — camera registry for the broker.

Starts with ZERO cameras. camera-mgmt pushes them at runtime and re-asserts
the set via its reconcile loop, so a restart self-heals within one sync
interval — the same lifecycle the NVR, motion and smartsearch services use.

A CAMERA HERE IS NOT AN OPT-IN. The broker analyses whatever it is given,
because both consumers need frames and neither should be able to starve the
other by toggling its own feature flag. Which cameras produce operator EVENTS
is the Motion service's business (`motion_detection`), and which produce index
rows is SmartSearch's (`search_indexing`). The broker just supplies the frames.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .config import AppConfig
from .notice import NoticePublisher
from .ring import shm_capacity
from .worker import CameraWorker

log = logging.getLogger("frames.engine")


class FrameEngine:
    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        self._lock = threading.Lock()
        self._workers: dict[str, CameraWorker] = {}
        self._pub = NoticePublisher(config.notice.url,
                                    config.notice.channel_prefix,
                                    config.notice.enabled)

    def add_camera(self, camera: str, rtsp_url: str) -> CameraWorker:
        with self._lock:
            if camera in self._workers:
                raise KeyError(camera)
            w = CameraWorker(camera, rtsp_url, self._cfg, self._pub)
            self._workers[camera] = w
        w.start()
        log.info("added camera %s (%s)", camera, rtsp_url)
        return w

    def upsert_camera(self, camera: str, rtsp_url: str) -> CameraWorker:
        """A URL change is a remove-then-add, so the reconcile loop can call
        this unconditionally without knowing whether anything changed."""
        with self._lock:
            existing = self._workers.get(camera)
            unchanged = existing is not None and existing.rtsp_url == rtsp_url
        if unchanged:
            return existing
        self.remove_camera(camera)
        return self.add_camera(camera, rtsp_url)

    def remove_camera(self, camera: str) -> bool:
        with self._lock:
            w = self._workers.pop(camera, None)
        if w is None:
            return False
        w.stop()
        log.info("removed camera %s", camera)
        return True

    def get(self, camera: str) -> Optional[CameraWorker]:
        with self._lock:
            return self._workers.get(camera)

    def snapshot(self) -> list[dict]:
        with self._lock:
            workers = list(self._workers.values())
        return [w.snapshot() for w in workers]

    def health(self) -> dict:
        cams = self.snapshot()
        by_state: dict[str, int] = {}
        for c in cams:
            by_state[c["state"]] = by_state.get(c["state"], 0) + 1
        ring_bytes = sum(c.get("ring", {}).get("ring_bytes", 0) for c in cams)
        shm = shm_capacity()
        # A ring set that will not fit is the failure this service is most
        # likely to hit on a default Docker install, where /dev/shm is 64 MB —
        # under half of one five-camera set. Say so before it happens.
        warning = None
        if shm.get("total_bytes") and ring_bytes > shm["total_bytes"] * 0.9:
            warning = (f"rings need {ring_bytes/1e6:.0f} MB of a "
                       f"{shm['total_mb']} MB /dev/shm — raise shm_size")
        return {
            "status": "ok",
            "total_cameras": len(cams),
            "by_state": by_state,
            "sample_fps": self._cfg.capture.sample_fps,
            "ring_slots": self._cfg.capture.ring_slots,
            "ring_bytes_total": ring_bytes,
            "shm": shm,
            "shm_warning": warning,
            "notices": self._pub.snapshot(),
            "cameras": cams,
        }

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()
