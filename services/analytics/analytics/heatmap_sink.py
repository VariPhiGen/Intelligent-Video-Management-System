"""heatmap_sink.py — heatmap artifacts and session changes, delivered to the VMS.

The same posture as the activity event sink (activities/events.py): the worker
thread that renders must never block on HTTP, so a bounded queue and one
background sender sit between them. What is carried is bigger than an event —
a rendered JPEG of a few hundred KB — so the queue is short and the OLDEST
artifact is the one dropped when it overflows: a stale overlay is worthless
next to the one just made.

ONE ENDPOINT, TWO KINDS OF MESSAGE. Both go to POST {api}/api/analytics/heatmaps
with the internal key:

  * `snapshot` / `final` — multipart: `meta` (JSON) + `image` (JPEG);
  * `closed` — metadata only, when a frozen session is abandoned for a new one.

The receiver keys everything on `session_id`, which analytics mints, so a
retried delivery updates the same row instead of making a second one.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger("analytics.heatmap")

#: Where 5-minute heat blocks (numpy arrays) go; a second sink instance uses it.
CHUNK_PATH = "/api/analytics/heatmaps/chunks"
INGEST_PATH = "/api/analytics/heatmaps"
INTERNAL_HEADER = "X-Internal-Key"


class _Permanent(Exception):
    """A refusal repeating cannot fix."""


class HeatmapSink:
    """Queues artifacts and posts them. Never blocks the caller, never raises."""

    def __init__(self, url: str, api_key: str, *, timeout: float = 20.0,
                 queue_size: int = 8, max_backoff: float = 30.0,
                 path: str = INGEST_PATH) -> None:
        self._url = url.rstrip("/") + path
        self._key = api_key
        self._timeout = timeout
        self._capacity = max(1, queue_size)
        self._max_backoff = max_backoff
        self._queue: collections.deque = collections.deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.queued = 0
        self.evicted = 0
        self.sent = 0
        self.dropped_http = 0
        self.failures = 0
        self.bytes_sent = 0
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    # ── producer side: the activity worker ──────────────────────────────────
    def __call__(self, kind: str, session, snapshot) -> None:
        """The runtime's publish hook. Never blocks, never raises."""
        meta = {
            "kind": kind,
            "camera": session.camera,
            "session_id": session.id,
            "session_started_at": session.started_at,
            "session_ended_at": session.ended_at,
            "status": session.status,
            "close_reason": session.close_reason,
            "params": dict(session.params or {}),
            "points": session.wire().get("points", 0),
            "frames": session.wire().get("frames", 0),
        }
        image = None
        if snapshot is not None:
            meta.update({"captured_at": snapshot.captured_at, "width": snapshot.width,
                         "height": snapshot.height, "peak": round(snapshot.peak, 2)})
            image = snapshot.jpeg
        self._enqueue(meta, [("image", "heatmap.jpg", "image/jpeg", image)] if image else [])

    def chunk(self, meta: dict, heat_npz: bytes, background: Optional[bytes]) -> None:
        """The runtime's block hook: one 5-minute heat array, maybe a camera frame."""
        parts = [("heat", "heat.npz", "application/octet-stream", heat_npz)]
        if background:
            parts.append(("background", "background.jpg", "image/jpeg", background))
        self._enqueue(meta, parts)

    def _enqueue(self, meta: dict, parts: list) -> None:
        with self._cond:
            while len(self._queue) >= self._capacity:
                self._queue.popleft()
                self.evicted += 1
            self._queue.append((meta, parts))
            self.queued += 1
            self._cond.notify()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="heatmap-sink", daemon=True)
        self._thread.start()
        log.info("heatmap artifacts -> %s", self._url)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── sender side ─────────────────────────────────────────────────────────
    def _take(self):
        with self._cond:
            while not self._queue and not self._stop.is_set():
                self._cond.wait(timeout=1.0)
            return self._queue.popleft() if self._queue else None

    def _requeue(self, item) -> None:
        with self._cond:
            if len(self._queue) >= self._capacity:
                self.evicted += 1                    # the newer artifact wins
                return
            self._queue.appendleft(item)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            item = self._take()
            if item is None:
                continue
            try:
                self.deliver(*item)
                backoff = 1.0
            except _Permanent as exc:
                self.dropped_http += 1
                self.last_error = str(exc)
                log.warning("heatmap artifact refused, not retried: %s", exc)
            except Exception as exc:                 # noqa: BLE001
                self.failures += 1
                self.last_error = str(exc)
                log.info("heatmap delivery failed (%s); retrying in %.0fs", exc, backoff)
                self._requeue(item)
                self._stop.wait(backoff)
                backoff = min(self._max_backoff, backoff * 2)

    def deliver(self, meta: dict, files) -> dict:
        """POST one artifact. Raises `_Permanent` for a refusal to keep is pointless.
        `files` is [(field, filename, content_type, bytes)], or a bare JPEG/None."""
        if files is None or isinstance(files, (bytes, bytearray)):
            files = [("image", "heatmap.jpg", "image/jpeg", files)] if files else []
        boundary = f"----heatmap{time.time_ns():x}"
        parts = [f'--{boundary}\r\n'
                 'Content-Disposition: form-data; name="meta"\r\n'
                 'Content-Type: application/json\r\n\r\n'
                 f'{json.dumps(meta)}\r\n'.encode()]
        for name, filename, ctype, data in files:
            parts.append(f'--{boundary}\r\n'
                         f'Content-Disposition: form-data; name="{name}"; '
                         f'filename="{filename}"\r\n'
                         f'Content-Type: {ctype}\r\n\r\n'.encode())
            parts.append(data)
            parts.append(b"\r\n")
        parts.append(f'--{boundary}--\r\n'.encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     INTERNAL_HEADER: self._key})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (408, 429) or exc.code >= 500:
                raise                                 # worth retrying
            raise _Permanent(f"{exc.code} {exc.reason}") from exc
        self.sent += 1
        self.bytes_sent += len(body)
        self.last_sent_at = time.time()
        self.last_error = None
        try:
            return json.loads(raw) or {}
        except ValueError:
            return {}

    def snapshot(self) -> dict:
        with self._cond:
            depth = len(self._queue)
        return {"url": self._url, "queue_depth": depth, "queue_capacity": self._capacity,
                "queued": self.queued, "evicted": self.evicted, "sent": self.sent,
                "bytes_sent": self.bytes_sent, "dropped_http": self.dropped_http,
                "failures": self.failures, "last_error": self.last_error,
                "last_sent_at": self.last_sent_at}
