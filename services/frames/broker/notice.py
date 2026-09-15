"""notice.py — telling consumers a frame is ready, and where.

The notice is small and the frame is not. Consumers get ~400 bytes over Redis
saying which shared-memory slot to read; the 6.22 MB never crosses a socket.

DELIBERATELY FIRE-AND-FORGET. A consumer that has fallen behind must not be
able to slow the decoder down — pub/sub drops for a slow subscriber rather
than blocking the publisher, which is the behaviour we want and the same
posture SmartSearch's ingest queue already takes with drop-oldest. A missed
notice costs one sampled frame; a blocked decoder costs every camera.

NOT ZeroMQ, which the scope proposed. Valkey is already deployed in this stack
and camera-mgmt already depends on the client, so this adds no dependency and
no new container. If notices ever outgrow it the seam is this file alone.

FAILURE HERE MUST NOT STOP DECODING. If Redis is unreachable the broker keeps
decoding, keeps writing frames to shared memory and keeps serving /health;
consumers simply see nothing until it returns. Publishing is best-effort by
construction — the alternative is a cache outage taking recording-adjacent
analysis down with it.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

log = logging.getLogger("frames.notice")


class NoticePublisher:
    """One per process. Thread-safe enough for our use: redis-py's client is
    itself thread-safe and each camera worker publishes independently."""

    def __init__(self, url: str, channel_prefix: str = "vms.frames",
                 enabled: bool = True) -> None:
        self.url = url
        self.prefix = channel_prefix
        self.enabled = enabled
        self._client = None
        self._last_error: Optional[str] = None
        self._last_attempt = 0.0
        self.published = 0
        self.failed = 0
        if enabled:
            self._connect()

    def _connect(self) -> None:
        # Retry no more than every 5 s: a down Redis must not turn into a
        # reconnect storm inside the decode loop.
        if time.time() - self._last_attempt < 5.0:
            return
        self._last_attempt = time.time()
        try:
            import redis
            self._client = redis.Redis.from_url(
                self.url, socket_timeout=1.0, socket_connect_timeout=1.0)
            self._client.ping()
            self._last_error = None
            log.info("notice publisher connected to %s", self.url)
        except Exception as exc:                               # noqa: BLE001
            self._client = None
            self._last_error = str(exc)

    def channel(self, camera: str) -> str:
        return f"{self.prefix}.{camera}"

    def publish(self, camera: str, notice: dict) -> bool:
        """Best-effort. Returns whether it landed; never raises."""
        if not self.enabled:
            return False
        if self._client is None:
            self._connect()
            if self._client is None:
                self.failed += 1
                return False
        try:
            self._client.publish(self.channel(camera), json.dumps(notice))
            self.published += 1
            return True
        except Exception as exc:                               # noqa: BLE001
            self.failed += 1
            self._last_error = str(exc)
            self._client = None            # force a reconnect on the next call
            return False

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "connected": self._client is not None,
                "url": self.url, "channel_prefix": self.prefix,
                "published": self.published, "failed": self.failed,
                "last_error": self._last_error}


def build_notice(*, camera: str, ts: float, seq: int, slot: int, path: str,
                 width: int, height: int, channels: int, slots: int,
                 motion: dict, epoch: int = 0) -> dict:
    """THE CONTRACT between the broker and every consumer.

    One message serves both: the Motion service reads `motion.fraction` and
    ignores the geometry; SmartSearch reads `motion.regions` plus the shm
    coordinates and ignores the scalar. Keeping it one message is what
    guarantees they are describing the SAME frame — which is the entire reason
    this service exists.

    `ts` is wall-clock seconds and is the only timebase. Every consumer,
    every log line and every stored row uses it, so a frame can be traced end
    to end without translating between clocks.
    """
    return {
        "v": 1,
        "camera": camera,
        "ts": ts,
        "seq": seq,
        "slot": slot,
        "shm": path,
        "width": width, "height": height, "channels": channels,
        "ring_slots": slots,
        # WHICH RING THIS SLOT IS IN. The path outlives the file: after a
        # broker restart a consumer holding the previous mmap reads a frozen
        # image whose sequence never advances, refuses every frame, and goes
        # silently blind while still reporting its ring mapped. Consumers
        # remap when this changes. Absent or 0 means an older broker that
        # cannot say, and consumers fall back to matching on geometry alone.
        "epoch": epoch,
        "motion": motion,
    }
