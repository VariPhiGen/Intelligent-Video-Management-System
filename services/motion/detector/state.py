"""state.py — per-camera runtime state and the state machine.

One ``CameraState`` per camera, alive for as long as the camera is registered.
Identity fields (``name``, ``rtsp_url``, sensitivity) are set at construction
and read lock-free; every other field requires ``with cam.lock:``.

States:
    CONNECTING  — no live stream yet (startup or reconnecting)
    MONITORING  — stream up, watching for motion
    TRIGGERED   — sustained motion confirmed; auto re-arms after the cooldown
                  (or stays sticky when retrigger_cooldown_seconds = 0)

Memory budget: ~115 KB per camera (two 320×180 grayscale frames); the deque
and metadata are negligible. 100 cameras ≈ 12 MB.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock
from typing import Optional

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "CONNECTING": {"MONITORING"},
    "MONITORING": {"TRIGGERED", "CONNECTING"},
    "TRIGGERED": {"MONITORING", "CONNECTING"},
}


@dataclass
class CameraState:
    # Immutable identity (lock-free reads) ------------------------------------
    name: str                      # the registry slug
    rtsp_url: str                  # relay URL — never camera credentials
    sensitivity: str               # preset name (low / medium / high)
    threshold: float               # resolved pixel_change_threshold

    # Mutable runtime state (access under self.lock) --------------------------
    state: str = "CONNECTING"
    triggered_at: Optional[datetime] = None
    triggered_mono: float = 0.0    # monotonic stamp for the re-arm cooldown
    last_motion_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    reconnect_attempts: int = 0
    open_event: Optional[dict] = None   # engine event dict while TRIGGERED
    motion_window: deque = field(default_factory=lambda: deque(maxlen=6))

    # Concurrency primitives ---------------------------------------------------
    lock: Lock = field(default_factory=Lock)
    stop: Event = field(default_factory=Event)  # set by engine.remove_camera

    def update_maxlen(self, confirm_window_s: float, sample_interval_s: float) -> None:
        """Size the rolling window: ceil(window / interval), floor 2 so a single
        noisy sample can never trigger on its own."""
        maxlen = max(2, math.ceil(confirm_window_s / sample_interval_s))
        self.motion_window = deque(self.motion_window, maxlen=maxlen)

    def transition_to(self, new_state: str) -> None:
        """Must be called holding ``self.lock``. Raises ValueError on an
        illegal transition — never assign ``self.state`` directly."""
        if new_state not in _VALID_TRANSITIONS.get(self.state, set()):
            raise ValueError(
                f"Invalid transition {self.state} -> {new_state} for {self.name}"
            )
        self.state = new_state

    def to_dict(self) -> dict:
        """API snapshot. Must be called holding ``self.lock``."""
        iso = lambda dt: dt.isoformat() if dt else None  # noqa: E731
        return {
            "name": self.name,
            "state": self.state,
            "sensitivity": self.sensitivity,
            "triggered_at": iso(self.triggered_at),
            "last_motion_at": iso(self.last_motion_at),
            "last_seen_at": iso(self.last_seen_at),
            "reconnect_attempts": self.reconnect_attempts,
        }
