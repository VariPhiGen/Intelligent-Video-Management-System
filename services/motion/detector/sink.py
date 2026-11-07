"""sink.py — delivering motion activity to the VMS, off the sampling threads.

The same delivery rules as analytics' event sink (analytics/activities/
events.py), which this service cannot import — each service builds from its own
directory: a bounded queue, one sender thread posting batches, transient
failures retried at the head of the queue with backoff, a refusal that repeating
cannot fix (4xx other than 408/429) dropped and counted. During a long outage
the OLDEST segments are evicted, counted and reported on /health.
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

log = logging.getLogger("motion.sink")

INGEST_PATH = "/api/playback/timeline/presence"
INTERNAL_HEADER = "X-Internal-Key"


class _Permanent(Exception):
    """A refusal that retrying cannot turn into a success."""


class PresenceSink:
    def __init__(self, url: str, api_key: str, *, timeout: float = 10.0,
                 queue_size: int = 2048, batch_size: int = 200,
                 max_backoff: float = 30.0) -> None:
        self._url = url.rstrip("/") + INGEST_PATH
        self._key = api_key
        self._timeout = timeout
        self._batch_size = max(1, min(batch_size, 500))
        self._capacity = max(1, queue_size)
        self._max_backoff = max_backoff
        self._queue: collections.deque = collections.deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.queued = 0
        self.evicted = 0
        self.sent = 0
        self.inserted = 0
        self.rejected: dict[str, int] = {}
        self.dropped_http = 0
        self.failures = 0
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    def __call__(self, segments: list[dict]) -> None:
        """Never blocks, never raises."""
        with self._cond:
            for seg in segments:
                if len(self._queue) >= self._capacity:
                    self._queue.popleft()
                    self.evicted += 1
                self._queue.append(seg)
                self.queued += 1
            self._cond.notify()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="motion-presence-sink",
                                            daemon=True)
            self._thread.start()
            log.info("motion activity -> %s", self._url)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _take(self) -> list[dict]:
        with self._cond:
            while not self._queue and not self._stop.is_set():
                self._cond.wait(timeout=1.0)
            batch = []
            while self._queue and len(batch) < self._batch_size:
                batch.append(self._queue.popleft())
            return batch

    def _requeue(self, batch: list[dict]) -> None:
        with self._cond:
            for item in reversed(batch):
                if len(self._queue) >= self._capacity:
                    self.evicted += 1
                    continue
                self._queue.appendleft(item)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            batch = self._take()
            if not batch:
                continue
            try:
                self.deliver(batch)
                backoff = 1.0
            except _Permanent as exc:
                self.dropped_http += len(batch)
                self.last_error = str(exc)
                log.warning("motion activity refused, not retried: %s", exc)
            except Exception as exc:                                  # noqa: BLE001
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.info("motion activity delivery failed (%s); retrying in %.0fs",
                         self.last_error, backoff)
                self._requeue(batch)
                self._stop.wait(backoff)
                backoff = min(self._max_backoff, backoff * 2)

    def deliver(self, batch: list[dict]) -> dict:
        body = json.dumps({"segments": batch}).encode()
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json", INTERNAL_HEADER: self._key})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                got = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise _Permanent(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc
            raise
        self.sent += len(batch)
        self.last_sent_at = time.time()
        self.last_error = None
        self.inserted += int(got.get("inserted", 0) or 0)
        for r in got.get("rejected") or []:
            reason = str(r.get("reason"))
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
        return got

    def snapshot(self) -> dict:
        with self._cond:
            depth = len(self._queue)
        return {"url": self._url, "queue_depth": depth, "queue_capacity": self._capacity,
                "queued": self.queued, "evicted": self.evicted, "sent": self.sent,
                "inserted": self.inserted, "rejected": dict(self.rejected),
                "dropped_http": self.dropped_http, "failures": self.failures,
                "last_sent_at": self.last_sent_at, "last_error": self.last_error}
