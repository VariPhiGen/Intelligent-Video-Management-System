"""notices.py — taking the motion scalar from the frame broker.

WHAT CHANGES AND WHAT DOES NOT. This service used to open its own RTSP
session per camera and difference the frames itself. In broker mode it does
neither: vms_frames decoded the camera once, ran the CANONICAL motion analysis
on that frame, and published the resulting changed-pixel fraction. We take
that number and run exactly the same confirmation window, state machine,
cooldown and event logic as before — worker.apply_feature, unchanged and
shared with the capture path.

THIS SERVICE NEVER NEEDS THE PIXELS. Only the scalar, which is why there is no
shared memory here and no IPC namespace to join: a notice is ~400 bytes over
Redis. SmartSearch needs frames because it crops and embeds them; we only ever
needed one float per sample, and paying a full 1080p decode to compute it was
the duplication this phase removes.

WHY THE EVENTS CANNOT DRIFT. The decision code is not reimplemented here — it
is called. The only thing that changed is where `feature` comes from, and the
broker computes it with the same operations at the same thresholds as
detector/algorithm.py did (resize, grey, blur, absdiff, THRESH_TOZERO at the
noise floor, non-zero fraction).

DEGRADED MODE IS SILENCE, NOT NOISE. If notices stop, cameras go CONNECTING
and no events are raised. That is the honest failure: this service's job is to
say when something moved, and with no input it does not know. Inventing
MONITORING would claim a camera is being watched when it is not.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from detector.config import AppConfig
from detector.state import CameraState
from detector.worker import apply_feature

logger = logging.getLogger(__name__)

#: A camera whose notices stopped for longer than this is reported CONNECTING
#: rather than left looking healthy.
#:
#: NOT derived from the sample cadence alone, which was the first attempt and
#: made this path twitchier than the one it replaces: 6 x 0.53 s flipped
#: cameras to CONNECTING after 3.2 s, where the capture loop tolerates a full
#: capture.read_timeout_ms (5 s) before calling a stream lost. Matching the
#: capture path is the point of this phase, so the tolerance comes from the
#: same setting; the cadence only sets a floor, for a broker running much
#: slower than the configured rate.
_STALE_INTERVALS = 6


def _stale_after(config, interval: float) -> float:
    return max(config.capture.read_timeout_ms / 1000.0,
               _STALE_INTERVALS * interval)


class NoticeSubscriber:
    """One per process. Feeds every registered camera from one subscription."""

    def __init__(self, config: AppConfig, engine, url: str,
                 channel_prefix: str = "vms.frames") -> None:
        self._config = config
        self._engine = engine
        self._url = url
        self._prefix = channel_prefix
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        #: Observed notice cadence per camera, used to keep the confirmation
        #: window three SECONDS rather than three samples — see _tune_window.
        self._last_ts: dict[str, float] = {}
        self._interval: dict[str, float] = {}

        self.state = "CONNECTING"
        self.last_error: Optional[str] = None
        self.notices_seen = 0
        self.notices_ignored = 0
        self.samples_applied = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="motion-notices",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                import redis
                client = redis.Redis.from_url(self._url, socket_timeout=5.0,
                                              socket_connect_timeout=5.0)
                client.ping()
                ps = client.pubsub(ignore_subscribe_messages=True)
                ps.psubscribe(f"{self._prefix}.*")
                self.state = "SUBSCRIBED"
                self.last_error = None
                backoff = 1.0
                logger.info("subscribed to %s.* on %s", self._prefix, self._url)
                while not self._stop.is_set():
                    msg = ps.get_message(timeout=1.0)
                    if msg is None:
                        self._rearm_all()
                        continue
                    try:
                        self._handle(json.loads(msg["data"]))
                    except Exception:                      # noqa: BLE001
                        # One bad notice must not end the subscription — it is
                        # the only input this service has in broker mode.
                        logger.exception("notice handling failed")
                    self._rearm_all()
            except Exception as exc:                       # noqa: BLE001
                self.state = "CONNECTING"
                self.last_error = str(exc)
                logger.warning("notice subscription lost: %s", exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    # ── the hot path ─────────────────────────────────────────────────────────
    def _handle(self, notice: dict) -> None:
        self.notices_seen += 1
        name = notice.get("camera")
        cam = self._engine.get_camera(name) if name else None
        if cam is None:
            # The broker serves whatever it is given; this service analyses
            # only cameras an operator opted in. Its set is not ours.
            self.notices_ignored += 1
            return

        motion = notice.get("motion") or {}
        if not motion.get("baseline_valid", True):
            # The broker reconnected, so this "difference" spans the outage and
            # is not motion. Clearing the window prevents an outage from
            # confirming itself into an alert — the same reason the capture
            # path drops its previous frame on reconnect.
            with cam.lock:
                cam.motion_window.clear()
            logger.info("[%s] broker baseline reset; window cleared", name)
            return

        ts = float(notice.get("ts", time.time()))
        self._tune_window(cam, name, ts)
        with cam.lock:
            if cam.state == "CONNECTING":
                cam.transition_to("MONITORING")
            cam.last_seen_at = _now()
        self.samples_applied += 1
        apply_feature(cam, float(motion.get("fraction", 0.0)),
                      self._config, self._engine)

    def _tune_window(self, cam: CameraState, name: str, ts: float) -> None:
        """Keep the confirmation window three SECONDS wide whatever rate the
        broker runs at.

        The window is a deque of samples, so its length only means three
        seconds while the sample interval matches what the config assumed. The
        broker owns the rate now and may not agree with detection.
        sample_interval_seconds, so the interval is measured rather than
        assumed. At matched rates this is a no-op, which is why events are
        unchanged today.
        """
        prev = self._last_ts.get(name)
        self._last_ts[name] = ts
        if prev is None or ts <= prev:
            return
        gap = ts - prev
        if gap > 10.0:
            return                                  # a stall, not a cadence
        smoothed = self._interval.get(name)
        smoothed = gap if smoothed is None else 0.7 * smoothed + 0.3 * gap
        self._interval[name] = smoothed
        want = max(2, round(self._config.detection.confirm_window_seconds / smoothed))
        if cam.motion_window.maxlen != want:
            cam.update_maxlen(self._config.detection.confirm_window_seconds, smoothed)
            logger.info("[%s] notice cadence %.2fs -> confirmation window %d samples",
                        name, smoothed, want)

    def _rearm_all(self) -> None:
        """Cooldown re-arming and staleness, which the capture loop did on its
        own thread. Without this a TRIGGERED camera would never re-arm in
        broker mode, and a camera whose notices stopped would sit looking
        healthy forever."""
        from detector.worker import _maybe_rearm
        for cam in self._engine.list_cameras():
            _maybe_rearm(cam, self._config, self._engine)
            interval = self._interval.get(cam.name,
                                          self._config.detection.sample_interval_seconds)
            last = self._last_ts.get(cam.name)
            if last is None:
                continue
            gap = time.time() - last
            if gap <= _stale_after(self._config, interval):
                continue
            # THE SAME TEARDOWN THE CAPTURE PATH DOES ON A LOST STREAM, and it
            # has to be, because an operator sees these events. Losing the
            # input ENDS an open incident: the evidence stream is gone, and
            # re-arming on reconnect beats a stale TRIGGERED. Clearing the
            # window without closing the event left it open indefinitely —
            # a divergence in the Events API itself, which is the one thing
            # this phase may not change.
            closed_event = None
            went_stale = False
            with cam.lock:
                if cam.state != "CONNECTING":
                    cam.transition_to("CONNECTING")
                    closed_event, cam.open_event = cam.open_event, None
                    cam.triggered_at = None
                    cam.motion_window.clear()
                    went_stale = True
            if closed_event:
                self._engine.close_event(closed_event)
            if went_stale:
                # LOGGED ON THE EDGE, NOT THE STATE. _rearm_all runs on every
                # notice and every idle second, so logging whenever a camera
                # IS stale writes a line per pass for as long as the outage
                # lasts — 362 lines from one broker restart, drowning the
                # event that explains it. The transition is the news.
                logger.warning("[%s] no notices for %.1fs; CONNECTING",
                               cam.name, gap)

    # ── observability ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {"source": "broker", "state": self.state, "url": self._url,
                "notices_seen": self.notices_seen,
                "notices_ignored": self.notices_ignored,
                "samples_applied": self.samples_applied,
                "observed_intervals": {k: round(v, 3)
                                       for k, v in self._interval.items()},
                "last_error": self.last_error}


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
