"""config.py — typed configuration loaded from config.yaml + env overrides.

Env overrides (set by docker-compose):
  MOTION_BIND_HOST  — api.host  (bridge overlay binds 0.0.0.0 in-container)
  MOTION_PORT       — api.port
  MOTION_LOG_LEVEL  — logging.level
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class DetectionConfig:
    sample_interval_seconds: float = 0.5
    noise_floor_threshold: int = 10
    confirm_window_seconds: float = 3.0
    confirm_ratio: float = 0.6
    retrigger_cooldown_seconds: float = 30.0
    sensitivity_presets: dict = field(
        default_factory=lambda: {"low": 0.015, "medium": 0.005, "high": 0.002}
    )
    default_sensitivity: str = "medium"

    def threshold_for(self, sensitivity: str | None) -> tuple[str, float]:
        """Resolve a sensitivity preset name to (name, pixel_change_threshold).
        Unknown/missing names fall back to the default preset."""
        name = (sensitivity or self.default_sensitivity).lower()
        if name not in self.sensitivity_presets:
            name = self.default_sensitivity
        return name, float(self.sensitivity_presets[name])


@dataclass(frozen=True)
class CaptureConfig:
    frame_width: int = 320
    frame_height: int = 180
    rtsp_buffer_size: int = 1
    open_timeout_ms: int = 5000
    read_timeout_ms: int = 5000
    reconnect_backoff_initial_seconds: float = 2.0
    reconnect_backoff_max_seconds: float = 60.0


@dataclass(frozen=True)
class SourceConfig:
    """Where the motion signal comes from.

        capture  open our own RTSP session per camera and difference the
                 frames ourselves. What has always shipped, and the default.
        broker   take the changed-pixel fraction the vms_frames service
                 already computed, on a frame it decoded once for every
                 consumer. No decode here, no shared memory — this service
                 only ever needed one float per sample.

    EVENT SEMANTICS ARE IDENTICAL EITHER WAY. Both paths call the same
    worker.apply_feature: same confirmation window, same state machine, same
    cooldown, same Events API. Only the origin of the number changes.
    """
    frame_source: str = "capture"
    broker_url: str = "redis://127.0.0.1:6379/0"
    broker_channel_prefix: str = "vms.frames"


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8012


@dataclass(frozen=True)
class AppConfig:
    detection: DetectionConfig
    capture: CaptureConfig
    api: ApiConfig
    source: SourceConfig = SourceConfig()
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        raw: dict = {}
        p = Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

        det = raw.get("detection", {}) or {}
        cap = raw.get("capture", {}) or {}
        api = raw.get("api", {}) or {}
        src = raw.get("source", {}) or {}
        logging_ = raw.get("logging", {}) or {}

        frame_source = os.environ.get(
            "MOTION_FRAME_SOURCE", src.get("frame_source", "capture")
        ).strip().lower()
        if frame_source not in ("capture", "broker"):
            raise ValueError(
                f"MOTION_FRAME_SOURCE must be 'capture' or 'broker', "
                f"got {frame_source!r}")

        return cls(
            detection=DetectionConfig(**det),
            capture=CaptureConfig(**cap),
            api=ApiConfig(
                host=os.environ.get("MOTION_BIND_HOST", api.get("host", "127.0.0.1")),
                port=int(os.environ.get("MOTION_PORT", api.get("port", 8012))),
            ),
            source=SourceConfig(
                frame_source=frame_source,
                broker_url=os.environ.get(
                    "MOTION_BROKER_URL",
                    os.environ.get("REDIS_URL",
                                   src.get("broker_url",
                                           "redis://127.0.0.1:6379/0"))),
                broker_channel_prefix=src.get("broker_channel_prefix",
                                              "vms.frames"),
            ),
            log_level=os.environ.get(
                "MOTION_LOG_LEVEL", logging_.get("level", "INFO")
            ).upper(),
        )
