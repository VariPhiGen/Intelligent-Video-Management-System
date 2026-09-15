"""worker.py — the per-camera capture loop (hot path).

One DEDICATED thread per camera, owned by MotionEngine. This deliberately
replaces the original motion_gateway's shared ThreadPoolExecutor: capture
loops are infinite and never yield their thread, so a pool sized at
cameras/8 silently starved every camera beyond the pool size — only the
first N workers ever ran. Threads here are cheap: the loop spends nearly
all its time blocked inside ffmpeg C code (GIL released).

The two-phase acquisition economy is preserved:

    cap.grab()     ~50 µs — drains the RTSP buffer, keeps the session alive,
                   NO decode
    cap.retrieve() ~2–4 ms — the actual decode, at most once per
                   sample_interval_seconds

Never replace this with cap.read() — it decodes every frame and multiplies
CPU by the source fps / sample rate ratio (~12× at 25 fps → 2 fps sampling).

Loop invariants (preserve these):
  • The loop NEVER raises — every exception routes to reconnect or
    sample-skip.
  • prev_frame resets to None after a reconnect so the first post-reconnect
    sample can't compare across an arbitrary time gap and false-trigger.
  • RTSP transport is forced to TCP (env var read by OpenCV at capture
    creation).
  • The 5 ms sleep per iteration is load-bearing: it caps busy-wait CPU when
    many threads share few cores.
  • cam.stop (a threading.Event) is the ONLY stop signal; it also aborts
    reconnect backoff waits so removal is prompt.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import cv2
import numpy as np

from detector.motion_core import compute_pixel_feature, preprocess_frame
from detector.config import AppConfig
from detector.state import CameraState

if TYPE_CHECKING:  # pragma: no cover — import cycle guard, typing only
    from detector.engine import MotionEngine

logger = logging.getLogger("motion.worker")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def open_rtsp_capture(rtsp_url: str, config: AppConfig) -> cv2.VideoCapture:
    """Open an RTSP stream with hardened settings (TCP, small buffer, timeouts)."""
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, config.capture.rtsp_buffer_size)
    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, config.capture.open_timeout_ms)
    cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, config.capture.read_timeout_ms)
    return cap


def _handle_reconnect(
    cam: CameraState, cap: cv2.VideoCapture, config: AppConfig,
    engine: "MotionEngine",
) -> cv2.VideoCapture:
    """Release, back off exponentially (abortable by cam.stop), reopen."""
    try:
        cap.release()
    except Exception:
        pass

    closed_event = None
    with cam.lock:
        cam.reconnect_attempts += 1
        attempt = cam.reconnect_attempts
        if cam.state != "CONNECTING":
            cam.transition_to("CONNECTING")
            # A disconnect ends any open motion incident — the evidence stream
            # is gone, and re-arming on reconnect beats a stale TRIGGERED.
            closed_event, cam.open_event = cam.open_event, None
            cam.triggered_at = None
            cam.motion_window.clear()
    if closed_event:
        engine.close_event(closed_event)

    delay = min(
        config.capture.reconnect_backoff_initial_seconds * (2 ** (attempt - 1)),
        config.capture.reconnect_backoff_max_seconds,
    )
    logger.warning("[%s] stream lost; attempt %d, backoff %.1fs", cam.name, attempt, delay)
    cam.stop.wait(delay)  # abortable sleep: removal doesn't wait out the backoff
    return open_rtsp_capture(cam.rtsp_url, config)


def apply_feature(
    cam: CameraState, feature: float, config: AppConfig, engine: "MotionEngine",
) -> None:
    """THE DECISION. Everything an operator sees comes from this function.

    Split out from the capture loop so the two ways of OBTAINING a feature —
    decoding a frame ourselves, or reading one the frame broker already
    computed — reach event state through identical code. They cannot diverge,
    because there is only one implementation of the confirmation window, the
    state machine and the cooldown.

    That is the whole safety argument for Phase 3: the source of the number
    changed, the meaning of the number did not, and nothing about how it turns
    into an event was touched.
    """
    motion = feature >= cam.threshold
    logger.debug("[%s] pixel_feature=%.5f threshold=%.5f motion=%s",
                 cam.name, feature, cam.threshold, motion)

    triggered = False
    with cam.lock:
        cam.motion_window.append(motion)
        if motion:
            cam.last_motion_at = _utcnow()
        window = cam.motion_window
        if (
            len(window) == window.maxlen
            and sum(window) / len(window) >= config.detection.confirm_ratio
            and cam.state == "MONITORING"
        ):
            cam.transition_to("TRIGGERED")
            cam.triggered_at = _utcnow()
            cam.triggered_mono = time.monotonic()
            triggered = True

    if triggered:
        event = engine.record_trigger(cam.name)
        with cam.lock:
            cam.open_event = event
        logger.warning("[%s] MOTION TRIGGERED (window %.1fs, sensitivity %s)",
                       cam.name, config.detection.confirm_window_seconds, cam.sensitivity)


def _process_sample(
    cam: CameraState, frame: np.ndarray, prev: np.ndarray,
    config: AppConfig, engine: "MotionEngine",
) -> np.ndarray:
    """Decode-side sample: difference this frame against the previous one and
    hand the result to apply_feature. Returns the preprocessed current frame
    (the next sample's ``prev``)."""
    curr = preprocess_frame(frame, config.capture.frame_width,
                            config.capture.frame_height)
    feature = compute_pixel_feature(prev, curr,
                                    config.detection.noise_floor_threshold)
    apply_feature(cam, feature, config, engine)
    return curr


def _maybe_rearm(cam: CameraState, config: AppConfig, engine: "MotionEngine") -> None:
    """Auto re-arm TRIGGERED → MONITORING after the cooldown (0 = sticky)."""
    cooldown = config.detection.retrigger_cooldown_seconds
    if cooldown <= 0:
        return
    closed_event = None
    with cam.lock:
        if cam.state == "TRIGGERED" and time.monotonic() - cam.triggered_mono >= cooldown:
            cam.transition_to("MONITORING")
            closed_event, cam.open_event = cam.open_event, None
            cam.triggered_at = None
            cam.motion_window.clear()
    if closed_event:
        engine.close_event(closed_event)
        logger.info("[%s] re-armed after %.0fs cooldown", cam.name, cooldown)


def capture_loop(cam: CameraState, config: AppConfig, engine: "MotionEngine") -> None:
    """Thread body for one camera. Runs until cam.stop is set. Never raises."""
    cam.update_maxlen(
        config.detection.confirm_window_seconds,
        config.detection.sample_interval_seconds,
    )
    logger.info("[%s] connecting to %s", cam.name, cam.rtsp_url)
    cap = open_rtsp_capture(cam.rtsp_url, config)
    last_sample = time.monotonic()
    prev_frame: np.ndarray | None = None

    while not cam.stop.is_set():
        try:
            grabbed = cap.grab()
        except Exception:
            logger.exception("[%s] grab() failed; reconnecting", cam.name)
            grabbed = False

        if not grabbed:
            if cam.stop.is_set():
                break
            cap = _handle_reconnect(cam, cap, config, engine)
            prev_frame = None
            last_sample = time.monotonic()
            continue

        with cam.lock:
            cam.last_seen_at = _utcnow()
            if cam.state == "CONNECTING":
                cam.transition_to("MONITORING")
                cam.reconnect_attempts = 0
                logger.info("[%s] stream connected; MONITORING", cam.name)

        now = time.monotonic()
        if now - last_sample >= config.detection.sample_interval_seconds:
            last_sample = now
            try:
                ret, frame = cap.retrieve()
            except Exception:
                logger.exception("[%s] retrieve() failed; skipping sample", cam.name)
                ret, frame = False, None
            if ret and frame is not None:
                try:
                    if prev_frame is not None:
                        prev_frame = _process_sample(cam, frame, prev_frame, config, engine)
                    else:
                        prev_frame = preprocess_frame(
                            frame, config.capture.frame_width, config.capture.frame_height
                        )
                except Exception:
                    logger.exception("[%s] motion processing error; skipping sample", cam.name)
                    prev_frame = None

        _maybe_rearm(cam, config, engine)

        # Yield CPU — load-bearing with many threads on few cores.
        time.sleep(0.005)

    # Cleanup: close any open incident so events never dangle after removal.
    closed_event = None
    with cam.lock:
        closed_event, cam.open_event = cam.open_event, None
    if closed_event:
        engine.close_event(closed_event)
    try:
        cap.release()
    except Exception:
        pass
    logger.info("[%s] capture loop exited", cam.name)
