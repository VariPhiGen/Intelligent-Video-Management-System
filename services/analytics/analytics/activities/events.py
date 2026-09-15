"""events.py — delivering activity events to the VMS.

WHERE THEY GO. camera-mgmt's `POST /api/analytics/events`, the ingest that
validates each event against the camera's stored configuration, stamps its
retention and stores it in `analytics_events` — the table the Events page and
its API read. Authenticated with the internal service key, the same trust the
VMS gives its other sibling services.

OFF THE DETECTION THREAD. Like the observation sink: events go into a bounded
queue and a sender thread POSTs them in batches, so a slow or restarting API
never stalls detection.

UNLIKE OBSERVATIONS, A FAILED DELIVERY IS RETRIED. An observation is one look
among many at an object that will be seen again; an event is the record that
something happened, and there may be no second one. Transient failures (the
API restarting, a timeout) keep the batch at the head of the queue with
backoff. A 4xx other than 408/429 will not succeed by repeating, so that batch
is dropped and counted. The queue is still bounded: during a long outage the
oldest events are evicted, counted, and reported on /health.
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

from .base import ActivityEvent

log = logging.getLogger("analytics.activities.events")

INGEST_PATH = "/api/analytics/events"
INTERNAL_HEADER = "X-Internal-Key"


class ActivityEventSink:
    def __init__(self, url: str, api_key: str, *, timeout: float = 10.0,
                 queue_size: int = 1024, batch_size: int = 50,
                 max_backoff: float = 30.0) -> None:
        self._url = url.rstrip("/") + INGEST_PATH
        self._key = api_key
        self._timeout = timeout
        self._batch_size = max(1, min(batch_size, 500))
        self._max_backoff = max_backoff
        self._queue: collections.deque = collections.deque()
        self._capacity = queue_size
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.queued = 0
        self.evicted = 0
        self.sent = 0
        self.inserted = 0
        self.duplicates = 0
        self.rejected: dict[str, int] = {}
        self.dropped_http = 0
        self.failures = 0
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    # ── producer side (detection worker) ────────────────────────────────────
    def __call__(self, events: list[ActivityEvent]) -> None:
        """Never blocks, never raises."""
        with self._cond:
            for ev in events:
                if len(self._queue) >= self._capacity:
                    self._queue.popleft()
                    self.evicted += 1
                self._queue.append(ev.to_ingest())
                self.queued += 1
            self._cond.notify()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="activity-events",
                                        daemon=True)
        self._thread.start()
        log.info("activity events -> %s", self._url)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ── sender ──────────────────────────────────────────────────────────────
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
                    self.evicted += 1       # the batch outranks nothing newer
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
                log.warning("activity events refused, not retried: %s", exc)
            except Exception as exc:                               # noqa: BLE001
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.info("activity event delivery failed (%s); retrying in %.0fs",
                         self.last_error, backoff)
                self._requeue(batch)
                self._stop.wait(backoff)
                backoff = min(self._max_backoff, backoff * 2)

    def deliver(self, batch: list[dict]) -> dict:
        """POST one batch. Raises _Permanent for a refusal repeating cannot fix."""
        body = json.dumps({"events": batch}).encode()
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
        self.duplicates += int(got.get("duplicates", 0) or 0)
        for r in got.get("rejected") or []:
            reason = str(r.get("reason"))
            self.rejected[reason] = self.rejected.get(reason, 0) + 1
            # Usually configuration moving under a running camera (an activity
            # removed between push and delivery). Worth one line each.
            log.warning("event refused by the VMS: %s", r)
        return got

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "configured": True,
            "url": self._url,
            "queue_depth": len(self._queue),
            "queue_capacity": self._capacity,
            "queued": self.queued,
            "evicted": self.evicted,
            "sent": self.sent,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "rejected": dict(self.rejected),
            "dropped_http": self.dropped_http,
            "failures": self.failures,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
        }


class _Permanent(Exception):
    """A refusal that retrying cannot turn into a success."""
