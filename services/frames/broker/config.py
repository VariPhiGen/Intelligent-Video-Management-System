"""Configuration for the frame broker.

Cameras are NOT configured here. The camera-mgmt registry pushes them at
runtime (POST /cameras/{slug}?rtsp_url=...) and re-asserts the set via its
reconcile loop — the same lifecycle the NVR, motion and smartsearch services
already use, so a restart self-heals within one sync interval.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .motion import MotionParams


@dataclass
class CaptureConfig:
    """Decode cadence. THE ONE PLACE THE SAMPLE RATE IS DECIDED, now that both
    consumers read the broker's frames rather than decoding their own.

    5.0 since 2026-09-10, for the tracker: the same footage replayed at 2 and
    5 FPS cut association failure 14.9% -> 9.2% (live 16.0% -> 12.9%), for
    about 1.4x broker CPU. Motion needed no change — its window is in seconds
    and resizes to the cadence. See config.yaml for the full reasoning and the
    one caveat (the gate's thresholds were fitted at 2 FPS).
    """
    sample_fps: float = 5.0
    #: Frames held per camera in shared memory. 4 is 0.8 s of history at
    #: 5 FPS — still far more than the ~13 ms a consumer takes to copy one out,
    #: and small enough that five cameras fit in 124 MB.
    ring_slots: int = 4
    rtsp_buffer_size: int = 1
    open_timeout_ms: int = 5000
    read_timeout_ms: int = 5000
    reconnect_backoff_initial_seconds: float = 2.0
    reconnect_backoff_max_seconds: float = 60.0


@dataclass
class NoticeConfig:
    """How consumers are told a frame is ready.

    Redis rather than the ZeroMQ the scope proposed: Valkey is ALREADY in this
    stack and camera-mgmt already depends on the client, so this adds no
    dependency and no new infrastructure. A notice is ~400 bytes at 10/s
    across five cameras, which is far inside what pub/sub handles locally.
    """
    enabled: bool = True
    url: str = "redis://127.0.0.1:6379/0"
    channel_prefix: str = "vms.frames"


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8014


@dataclass
class AppConfig:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    motion: MotionParams = field(default_factory=MotionParams)
    notice: NoticeConfig = field(default_factory=NoticeConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str) -> "AppConfig":
        raw: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

        capture = CaptureConfig(**(raw.get("capture") or {}))
        motion = MotionParams(**(raw.get("motion") or {}))
        notice = NoticeConfig(**(raw.get("notice") or {}))
        api = ApiConfig(**(raw.get("api") or {}))
        log_level = str((raw.get("logging") or {}).get("level", "INFO")).upper()

        # Environment wins over the file: the file is the shipped default, the
        # environment is what a deployment set deliberately.
        api.host = os.environ.get("FRAMES_BIND_HOST", api.host)
        api.port = int(os.environ.get("FRAMES_PORT", api.port))
        capture.sample_fps = float(
            os.environ.get("FRAMES_SAMPLE_FPS", capture.sample_fps))
        capture.ring_slots = int(
            os.environ.get("FRAMES_RING_SLOTS", capture.ring_slots))
        notice.url = os.environ.get("REDIS_URL", notice.url)
        if "FRAMES_NOTICES_ENABLED" in os.environ:
            notice.enabled = os.environ["FRAMES_NOTICES_ENABLED"].lower() \
                not in ("0", "false", "no")
        log_level = os.environ.get("LOG_LEVEL", log_level).upper()
        return cls(capture=capture, motion=motion, notice=notice, api=api,
                   log_level=log_level)
