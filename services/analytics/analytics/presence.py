"""presence.py — when each tracked object was in view, for the playback timeline.

THE TRACKER ALREADY KNOWS THIS AND THREW IT AWAY. Every track has a first and a
last sighting; the indexing policy keeps one good crop of it and the lifetime
was only ever a histogram on /health. This module keeps the lifetime itself —
"a person was in view 10:02:13–10:02:47" — and hands it to the VMS, which draws
it under the playback timeline (camera-mgmt services/timeline.py).

WHAT COUNTS. Only CONFIRMED tracks of a domain the camera indexes: a tentative
track is the tracker's own "not sure yet", and a domain the operator did not
switch on is not something the timeline should show. The start is the track's
FIRST sighting, not the moment it was confirmed — the object was there then.

SEGMENTS, NOT ONE ROW PER TRACK. A presence is cut every `segment_seconds`
while it lasts. That bounds how late the timeline learns about a long stay
(a parked car shows up within one segment, not when it drives away), bounds
every row the VMS stores (its overlap query depends on it), and means nothing
is ever updated after it is sent. The VMS merges touching segments back into
one bar.

HOW A PRESENCE ENDS. Three ways, whichever comes first:
  * the tracker retires the identity (`close`, fed from drain_expired);
  * the camera reconnects or goes away (the tracker forgets it, same path);
  * nothing has been seen of it for `idle_close_seconds` of WALL time
    (`flush`). The tracker only ages tracks out when a frame with detections
    arrives, and a motion-gated quiet camera may send none for hours — without
    this the last person of the day would stay open until tomorrow.

THREAD SAFETY. `observe`/`close` run on the detection worker; `flush` runs on
this module's own timer thread. One lock, held briefly; emission happens
outside it, into a sink that never blocks.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger("analytics.presence")

#: Detector domain → timeline kind. Anything else (plates, faces) is not a
#: presence of its own: a plate is read off a vehicle already counted.
KIND_OF = {"person": "person", "vehicles": "vehicle"}

#: A single sighting still means the object was there for about one sample
#: interval; a zero-length bar would be a claim of "never".
MIN_DURATION_S = 1.0


@dataclass
class _Open:
    slug: str
    kind: str
    track_id: str
    start: float
    last: float
    peak: float
    label: Optional[str]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class PresenceRecorder:
    """Open presences per track, emitted as segments. Never blocks, never raises."""

    def __init__(self, emit: Callable[[list[dict]], None], *, segment_seconds: float = 120.0,
                 idle_close_seconds: float = 10.0, flush_interval: float = 2.0,
                 source: str = "cpu") -> None:
        self._emit = emit
        self.segment_seconds = max(10.0, float(segment_seconds))
        self.idle_close_seconds = max(1.0, float(idle_close_seconds))
        self._flush_interval = flush_interval
        self._source = source
        self._open: dict[str, _Open] = {}
        #: track id → (end already sent, wall time it was closed). A closed
        #: identity can be revived by the tracker's re-association; its next
        #: presence must start where the sent one ended, or it would re-send
        #: that stretch under the same id and the VMS would drop it as a retry.
        self._sent_until: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.segments_emitted = 0
        self.closed_by_tracker = 0
        self.closed_idle = 0
        self.cut_long = 0

    # ── the detection worker ────────────────────────────────────────────────
    def observe(self, slug: str, domain: str, track_id: str, ts: float,
                first_seen: float, confidence: float, label: Optional[str] = None) -> None:
        kind = KIND_OF.get(domain)
        if kind is None:
            return
        out: list[dict] = []
        with self._lock:
            cur = self._open.get(track_id)
            if cur is None:
                # From the first sighting — or, for an identity closed and
                # since revived, from where its sent presence ended.
                start = min(first_seen, ts)
                sent = self._sent_until.pop(track_id, None)
                if sent is not None:
                    start = min(max(start, sent[0]), ts)
                self._open[track_id] = _Open(slug, kind, track_id, start, ts,
                                             float(confidence), label)
                return
            cur.last = max(cur.last, ts)
            cur.peak = max(cur.peak, float(confidence))
            if cur.last - cur.start >= self.segment_seconds:
                # Cut, and carry on from the cut: the next segment begins where
                # this one ends, so the merged bar has no seam.
                out.append(self._segment(cur))
                cur.start = cur.last
                cur.peak = float(confidence)
                self.cut_long += 1
        self._send(out)

    def close(self, track_ids: Iterable[str]) -> None:
        """The tracker retired these identities (drain_expired)."""
        out: list[dict] = []
        with self._lock:
            for tid in track_ids:
                cur = self._open.pop(tid, None)
                if cur is not None:
                    out.append(self._segment(cur))
                    self.closed_by_tracker += 1
        self._send(out)

    # ── the timer ───────────────────────────────────────────────────────────
    def flush(self, now: Optional[float] = None) -> int:
        """Close presences nothing has been seen of for idle_close_seconds."""
        now = time.time() if now is None else now
        out: list[dict] = []
        with self._lock:
            for tid in [t for t, o in self._open.items()
                        if now - o.last > self.idle_close_seconds]:
                out.append(self._segment(self._open.pop(tid)))
                self.closed_idle += 1
            # Revival is a matter of seconds; a minute is generous.
            for tid in [t for t, (_e, at) in self._sent_until.items() if now - at > 60.0]:
                del self._sent_until[tid]
        self._send(out)
        return len(out)

    def close_all(self) -> None:
        """Shutdown: whatever is open was in view up to its last sighting."""
        with self._lock:
            out = [self._segment(o) for o in self._open.values()]
            self._open.clear()
        self._send(out)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="presence-flush", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.close_all()

    def _run(self) -> None:
        while not self._stop.wait(self._flush_interval):
            try:
                self.flush()
            except Exception as exc:                          # noqa: BLE001
                log.warning("presence flush failed: %s", exc)

    # ── internals ───────────────────────────────────────────────────────────
    def _segment(self, o: _Open) -> dict:
        end = max(o.last, o.start + MIN_DURATION_S)
        self._sent_until[o.track_id] = (end, time.time())
        return {
            "sensor_id": o.slug,
            "kind": o.kind,
            "started_at": _iso(o.start),
            "ended_at": _iso(end),
            "peak_confidence": round(min(1.0, max(0.0, o.peak)), 3),
            "track_id": o.track_id,
            "label": (o.label or None) and str(o.label)[:32],
            "source": self._source,
        }

    def _send(self, segments: list[dict]) -> None:
        if not segments:
            return
        self.segments_emitted += len(segments)
        try:
            self._emit(segments)
        except Exception as exc:                              # noqa: BLE001
            log.warning("presence emit failed: %s", exc)

    def snapshot(self) -> dict:
        with self._lock:
            open_now = len(self._open)
        return {"open": open_now, "segments_emitted": self.segments_emitted,
                "closed_by_tracker": self.closed_by_tracker, "closed_idle": self.closed_idle,
                "cut_long": self.cut_long, "segment_seconds": self.segment_seconds,
                "idle_close_seconds": self.idle_close_seconds}
