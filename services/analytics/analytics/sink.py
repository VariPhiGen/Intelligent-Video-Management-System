"""sink.py — handing finished observations to Smart Search.

WHERE THIS SERVICE ENDS. Detection, plates, tracking and the indexing policy
have already run; what is left is a crop and the metadata describing it. Smart
Search embeds it, deduplicates it and stores it, because that needs the CLIP
encoder and the database, neither of which belongs here.

OFF THE DETECTION THREAD, ON PURPOSE. The obvious implementation POSTs straight
from the worker, and that couples detection latency to the far service: one
slow response and frames back up behind an HTTP call that has nothing to do
with detecting. So observations go through a bounded queue and a sender thread,
and the worker never waits.

THE QUEUE DROPS THE OLDEST, the same discipline the frame queue uses and for
the same reason: if the receiver is behind, the observation that just happened
is worth more than one from a minute ago. Dropping is counted and reported, so
a backlog is visible before anyone wonders why rows stopped appearing.

FAILURE IS LOSS, AND IT IS COUNTED. There is no disk spool and no retry queue:
this is a live index, not a ledger, and an observation that cannot be delivered
now is worth less than the ones arriving behind it. What must never happen is
silent loss, which is what the counters on /health are for.
"""
from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time
from typing import Optional

import numpy as np

log = logging.getLogger("analytics.sink")


class ObservationSink:
    """Best-effort delivery of observations to Smart Search."""

    #: How often to announce we are alive, in seconds. The receiver treats a
    #: producer as stale at three times this, so one missed beat is not an
    #: alarm and a real outage is visible inside two minutes.
    HEARTBEAT_SECONDS = 30.0

    def __init__(self, url: str, timeout: float = 10.0,
                 queue_size: int = 256, producer: str = "analytics") -> None:
        base = url.rstrip("/")
        self._url = base + "/observations"
        self._beat_url = f"{base}/producers/{producer}/heartbeat"
        self._producer = producer
        self._timeout = timeout
        #: Filled in by the engine so the heartbeat carries something worth
        #: reading — how many cameras, how much has been produced — rather
        #: than being a bare ping.
        self._stats_fn = None
        self._last_beat = 0.0
        self.beats_sent = 0
        self.beats_failed = 0
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session = None

        self.queued = 0
        #: Made room for a newer observation. NOT the same as `dropped`, and
        #: the frame queue draws the same distinction for the same reason: an
        #: eviction is the policy working, a drop is the rare case where even
        #: after evicting there was no room. Counting them as one number makes
        #: normal backpressure look like loss.
        self.evicted = 0
        self.dropped = 0
        self.sent = 0
        #: Observations that carried a whole frame for the feed.
        self.frames_sent = 0
        self.deduped_by_receiver = 0
        self.rows_written = 0
        self.failed = 0
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None

    # ── producer side, called on the detection worker ───────────────────────
    def __call__(self, slug: str, crop: np.ndarray, obs: dict,
                 frame: Optional[bytes] = None) -> None:
        """Never blocks, never raises. The worker's only job here is to hand
        the observation over and get back to detecting.

        `frame` is the whole frame as JPEG bytes, already downscaled by the
        pipeline — tens of KB, so a full queue holds a few MB rather than the
        1.5 GB that 256 raw 1080p frames would be."""
        item = (crop, obs, frame)
        try:
            self._queue.put_nowait(item)
            self.queued += 1
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
            self.evicted += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(item)
            self.queued += 1
        except queue.Full:
            self.dropped += 1

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="obs-sink",
                                        daemon=True)
        self._thread.start()
        log.info("observation sink -> %s", self._url)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def set_stats_source(self, fn) -> None:
        """What the heartbeat should carry. Called by the engine."""
        self._stats_fn = fn

    def _maybe_beat(self) -> None:
        """Announce we are alive, on a timer, regardless of traffic.

        ON THE SENDER THREAD rather than its own, because it must fail the
        same way a real delivery does: if the receiver is unreachable the
        heartbeat stops too, which is precisely the signal.
        """
        now = time.time()
        if now - self._last_beat < self.HEARTBEAT_SECONDS:
            return
        self._last_beat = now
        try:
            stats = self._stats_fn() if self._stats_fn else {}
        except Exception:                                    # noqa: BLE001
            stats = {}
        try:
            self._post(self._beat_url, json.dumps(stats).encode(),
                       "application/json")
            self.beats_sent += 1
        except Exception as exc:                             # noqa: BLE001
            self.beats_failed += 1
            log.debug("heartbeat failed: %s", exc)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._maybe_beat()
            try:
                crop, obs, frame = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send(crop, obs, frame)
            except Exception as exc:                        # noqa: BLE001
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                # Debug, not warning: a receiver that is warming up will
                # refuse for a while, and a line per observation would bury
                # the one message that matters.
                log.debug("observation delivery failed: %s", exc)

    # ── the wire ────────────────────────────────────────────────────────────
    def _encode(self, crop: np.ndarray) -> bytes:
        """PNG, NOT JPEG, and that is a correctness choice rather than taste.

        The receiver embeds the crop BEFORE writing it to disk, so a lossy hop
        here would change the vector and therefore the appearance-dedup
        decision — the one thing that must stay identical while this path and
        the in-process one run side by side. The crop is still stored as JPEG
        at the far end exactly as before; only the transport is lossless.
        """
        import cv2
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            raise RuntimeError("could not PNG-encode the crop")
        return buf.tobytes()

    def _post(self, url: str, body: bytes, content_type: str) -> dict:
        """One HTTP implementation for both the observations and the
        heartbeat, so a change to timeouts or headers cannot apply to one and
        not the other. A body that is not JSON comes back as {} rather than
        raising: the request succeeded, and the caller only reads optional
        fields from it."""
        import urllib.request

        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": content_type,
                     "Content-Length": str(len(body))})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read() or b"{}"
        try:
            got = json.loads(raw)
        except ValueError:
            return {}
        return got if isinstance(got, dict) else {}

    def _send(self, crop: np.ndarray, obs: dict,
              frame: Optional[bytes] = None) -> None:
        payload = self._encode(crop)
        boundary = f"----analytics{time.time_ns():x}"
        parts = []
        parts.append(
            f'--{boundary}\r\n'
            'Content-Disposition: form-data; name="meta"\r\n'
            'Content-Type: application/json\r\n\r\n'
            f'{json.dumps(obs)}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\n'
            'Content-Disposition: form-data; name="crop"; filename="crop.png"\r\n'
            'Content-Type: image/png\r\n\r\n'.encode())
        parts.append(payload)
        if frame:
            # The whole frame, for the Recent-detections feed. JPEG, unlike the
            # crop: nothing embeds it, so a lossy hop changes nothing that is
            # ever compared — it is only looked at.
            parts.append(
                f'\r\n--{boundary}\r\n'
                'Content-Disposition: form-data; name="frame"; filename="frame.jpg"\r\n'
                'Content-Type: image/jpeg\r\n\r\n'.encode())
            parts.append(frame)
        parts.append(f'\r\n--{boundary}--\r\n'.encode())
        body = b"".join(parts)

        got = self._post(self._url, body,
                         f"multipart/form-data; boundary={boundary}")
        self.sent += 1
        if frame:
            self.frames_sent += 1
        self.last_sent_at = time.time()
        self.last_error = None
        written = int(got.get("rows_written", 0) or 0)
        self.rows_written += written
        if got.get("deduped"):
            # NOT a failure. The receiver's appearance dedup rejected it as an
            # object already represented in its window, which is the saving
            # working. Counted separately so the two are never confused.
            self.deduped_by_receiver += 1

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "configured": True,
            "url": self._url,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "queued": self.queued,
            "evicted": self.evicted,
            "dropped": self.dropped,
            "sent": self.sent,
            "frames_sent": self.frames_sent,
            "failed": self.failed,
            "rows_written": self.rows_written,
            "deduped_by_receiver": self.deduped_by_receiver,
            "last_sent_at": self.last_sent_at,
            # The heartbeat is what lets the receiver tell "quiet scene" from
            # "producer gone". If these stop climbing, it will say so.
            "heartbeats_sent": self.beats_sent,
            "heartbeats_failed": self.beats_failed,
            "last_error": self.last_error,
        }
