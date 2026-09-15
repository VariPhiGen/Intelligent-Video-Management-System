"""frame_source.py — where frames come from: our own decoder, or the broker.

TWO SOURCES, ONE CONTRACT. Both call `on_frame(slug, frame, ts, motion)`:

    sampler   index/sampler.py opens its own RTSP session and decodes.
              `motion` is None, so pipeline._detect runs the local MotionGate
              exactly as it always has. This is the fallback and it stays.

    broker    vms_frames decoded the frame already, ran the CANONICAL motion
              analysis on it, and published a notice saying which shared-memory
              slot it is in. `motion` carries that analysis, so the local gate
              is skipped — not because it is wrong, but because running it
              again on the same pixels would produce the same answer twice.

WHY THIS IS WORTH DOING. Today the Motion service and SmartSearch each decode
the same relay independently, so they are never looking at the same frame.
Regions computed by one cannot describe the other's picture: measured, at
2 FPS a walking person moves 0.5-1.0 object heights between samples, which is
a whole body-width of error. One decoder removes the problem rather than
bounding it.

THE READER BELOW IS A VENDORED COPY of services/frames/broker/ring.py's
FrameReader. The two services have separate Docker build contexts, so an
import is not available without widening both — and widening them to
deduplicate sixty lines is the trade this project already declined once for
the motion primitive. The copy is small, the wire format is the real contract,
and services/frames/scripts/parity.py checks the two agree.

IF YOU CHANGE THE SEQLOCK DISCIPLINE, CHANGE IT IN BOTH. Getting it subtly
wrong does not raise; it hands the detector half of one frame and half of
another, and every stage downstream processes that happily.
"""
from __future__ import annotations

import json
import logging
import mmap
import os
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("analytics.frames")

#: Must match services/frames/broker/ring.py.
CTRL_BYTES = 4096
_ENTRY = 16
#: WHERE THE RINGS LIVE, and it is deliberately configurable.
#:
#: NOT the container's own /dev/shm by default in broker deployments. Sharing
#: memory by joining the broker's IPC NAMESPACE (ipc: "service:frames") was
#: the first design and it fails in a way nothing reports: the namespace is
#: bound once, at container start, so when the broker restarts it gets a NEW
#: namespace and every consumer keeps the dead one. Measured here — after a
#: broker restart the consumer's /dev/shm was EMPTY while the broker held five
#: healthy rings, and SmartSearch delivered zero frames for as long as it was
#: left running, with /health still reporting five rings mapped.
#:
#: A shared tmpfs VOLUME mounted into both containers has no such lifetime
#: coupling: it outlives either container, so a restart on either side
#: reconnects on its own. Verified on this appliance, writer restarted under a
#: live reader.
_SHM_DIR = os.environ.get("ANALYTICS_SHM_DIR", "/dev/shm")

#: (slug, frame, ts, motion|None)
FrameHandler = Callable[[str, np.ndarray, float, Optional[dict]], None]


def shm_path(camera: str) -> str:
    return os.path.join(_SHM_DIR, f"vms_frames.{camera}")


class FrameReader:
    """Consumer half of the broker's ring. Copies out, never holds a slot.

    Copying is deliberate. The ingest queue holds up to 64 frames; if those
    were references into the ring, a slot could be recycled underneath a
    queued reference and the worker would process a frame that no longer
    exists. Sizing the ring to match would cost 2 GB. The copy is ~1 ms
    (measured 0.95-1.31 ms) and it is not a regression — a frame arriving from
    cv2 was always a private array.
    """

    def __init__(self, camera: str, width: int, height: int,
                 channels: int = 3, slots: int = 4) -> None:
        self.camera = camera
        self.width, self.height, self.channels = width, height, channels
        self.slots = slots
        self.frame_bytes = width * height * channels
        total = CTRL_BYTES + slots * self.frame_bytes
        self._fd = os.open(shm_path(camera), os.O_RDONLY)
        # ACCESS_READ rather than prot=PROT_READ: identical on Linux, and
        # the only spelling that exists on Windows, where the test
        # suite also has to run.
        self._mm = mmap.mmap(self._fd, total, access=mmap.ACCESS_READ)
        self.refused = 0

    def read(self, slot: int, expect_seq: int) -> Optional[np.ndarray]:
        """A private copy, or None if the slot was not stable.

        None is normal under load and means the writer moved on. Drop the
        frame rather than retrying — the same posture submit() already takes.
        """
        off = slot * _ENTRY
        before = struct.unpack_from("<Q", self._mm, off)[0]
        if before % 2 == 1 or before // 2 != expect_seq:
            self.refused += 1
            return None
        base = CTRL_BYTES + slot * self.frame_bytes
        buf = np.frombuffer(self._mm, dtype=np.uint8,
                            count=self.frame_bytes, offset=base).copy()
        if struct.unpack_from("<Q", self._mm, off)[0] != before:
            self.refused += 1
            return None
        return buf.reshape((self.height, self.width, self.channels))

    def close(self) -> None:
        try:
            self._mm.close()
        except (BufferError, ValueError):
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass


class BrokerSubscriber:
    """One per process. Subscribes to notices and delivers frames.

    Process-wide rather than per-camera because one Redis subscription serves
    every camera; the engine registers slugs it wants and everything else is
    ignored. That keeps the broker's camera set independent of ours — it may
    legitimately serve cameras this service does not index.
    """

    def __init__(self, url: str, on_frame: FrameHandler, *,
                 channel_prefix: str = "vms.frames",
                 on_discontinuity: Optional[Callable[[str], None]] = None,
                 on_gated: Optional[Callable[[str], None]] = None,
                 on_activity_frame: Optional[Callable[[str, np.ndarray, float], None]] = None,
                 ) -> None:
        self._url = url
        self._on_frame = on_frame
        #: The CPU activity path. It receives EVERY frame the broker samples
        #: for a camera registered with want_every_frame(), motion or not:
        #: a parked car or a group standing still makes no motion, and an
        #: activity that times how long something stays needs those frames.
        #: Smart Search's gating below is unchanged by it.
        self._on_activity_frame = on_activity_frame
        self._every_frame: set[str] = set()
        self._on_discontinuity = on_discontinuity
        #: Told about frames dropped by the broker's verdict before any copy,
        #: so the pipeline's gate stats stay truthful about what was gated.
        self._on_gated = on_gated
        self._prefix = channel_prefix
        self._wanted: set[str] = set()
        self._readers: dict[str, tuple[tuple, FrameReader]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.state = "CONNECTING"
        self.last_error: Optional[str] = None
        self.notices_seen = 0
        self.frames_delivered = 0
        self.frames_refused = 0
        #: Gated out by the broker's own analysis, so never copied at all.
        self.frames_gated_before_read = 0
        self.notices_ignored = 0
        #: Frames handed to the activity path (ungated).
        self.activity_frames_delivered = 0
        self.last_frame_at: Optional[float] = None

    # ── camera registration ──────────────────────────────────────────────────
    def want(self, slug: str) -> None:
        with self._lock:
            self._wanted.add(slug)

    def unwant(self, slug: str) -> None:
        with self._lock:
            self._wanted.discard(slug)
            entry = (self._readers.pop(slug, None)
                     if slug not in self._every_frame else None)
        if entry is not None:
            entry[1].close()

    def want_every_frame(self, slug: str) -> None:
        """Deliver every frame of `slug` to the activity path, ungated."""
        with self._lock:
            self._every_frame.add(slug)

    def unwant_every_frame(self, slug: str) -> None:
        with self._lock:
            self._every_frame.discard(slug)
            entry = (self._readers.pop(slug, None)
                     if slug not in self._wanted else None)
        if entry is not None:
            entry[1].close()

    def cameras(self) -> list[str]:
        with self._lock:
            return sorted(self._wanted | self._every_frame)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="broker-sub",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            readers = list(self._readers.values())
            self._readers.clear()
        for _key, r in readers:
            r.close()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                import redis
                client = redis.Redis.from_url(self._url, socket_timeout=5.0,
                                              socket_connect_timeout=5.0)
                client.ping()
                ps = client.pubsub(ignore_subscribe_messages=True)
                ps.psubscribe(f"{self._prefix}.*")
                self.state = "SUBSCRIBED"
                self.last_error = None
                backoff = 1.0
                log.info("subscribed to %s.* on %s", self._prefix, self._url)
                while not self._stop.is_set():
                    msg = ps.get_message(timeout=1.0)
                    if msg is None:
                        continue
                    try:
                        self._handle(json.loads(msg["data"]))
                    except Exception as exc:              # noqa: BLE001
                        # One bad notice must never end the subscription — it
                        # is the only path frames arrive by.
                        log.warning("notice handling failed: %s", exc)
            except Exception as exc:                      # noqa: BLE001
                self.state = "CONNECTING"
                self.last_error = str(exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)

    # ── the hot path ─────────────────────────────────────────────────────────
    def _handle(self, notice: dict) -> None:
        self.notices_seen += 1
        slug = notice.get("camera")
        with self._lock:
            search = slug in self._wanted
            every = slug in self._every_frame and self._on_activity_frame is not None
            if not (search or every):
                self.notices_ignored += 1
                return
        motion = notice.get("motion") or {}
        # LIVENESS IS THE NOTICE, NOT THE COPY. This is what /health shows an
        # operator as "last frame", so it has to answer "is this camera still
        # arriving?" — not "was anything moving?". Stamping it only on frames
        # that pass the gate would make a camera watching an empty corridor
        # report a last frame from an hour ago and read as dead.
        self.last_frame_at = time.time()

        # BEFORE THE READ, deliberately. The broker lost and regained its
        # stream, so its motion baseline is a different scene. Tell the
        # pipeline, which resets the tracker: frames either side of an outage
        # are separated by however long it lasted, and associating across that
        # gap is guesswork wearing the costume of continuity. Ordering this
        # after the read would drop the reset on exactly the frame most likely
        # to be refused — the first one after a reconnect.
        if not motion.get("baseline_valid", True) and self._on_discontinuity:
            try:
                self._on_discontinuity(slug)
            except Exception:                             # noqa: BLE001
                pass

        # THE GATE DECIDES BEFORE THE COPY. The broker already ran the
        # canonical analysis on these pixels and told us the answer; a frame it
        # found no motion in is dropped by pipeline._detect without the frame
        # ever being touched, and _process returns before the tracker is even
        # called. Copying 6 MB out of shared memory to reach that conclusion
        # was pure waste — measured here at ~75% of all frames.
        #
        # This is not a second gate: the rule is `regions is None`, the same
        # expression _detect applies to the same field. It is only asked
        # earlier, and the frame that survives it takes the identical path.
        #
        # The activity path is the one exception: a camera it wants every
        # frame of is read regardless, and only Smart Search's delivery below
        # is gated — exactly as it was.
        gated = self._gated_out(motion)
        if gated and not every:
            self._note_gated(slug)
            return

        # The epoch is part of the ring's identity, not just its geometry: a
        # restarted broker replaces the file behind an unchanged path, and a
        # reader still mapped to the old inode reads a frozen frame whose
        # sequence never advances. Every read is then refused, forever, with
        # the ring still reported as mapped. Keying the reader on it makes a
        # restart a remap instead of permanent silence.
        shape = (notice["width"], notice["height"], notice["channels"],
                 notice["ring_slots"], notice.get("epoch", 0))
        reader = self._reader_for(slug, shape)
        if reader is None:
            return
        frame = reader.read(notice["slot"], notice["seq"])
        if frame is None:
            self.frames_refused += 1
            return

        ts = float(notice["ts"])
        if every:
            self.activity_frames_delivered += 1
            try:
                self._on_activity_frame(slug, frame, ts)
            except Exception as exc:                      # noqa: BLE001
                log.warning("activity frame delivery failed for %s: %s", slug, exc)
        if not search:
            return
        if gated:
            self._note_gated(slug)
            return
        self.frames_delivered += 1
        self._on_frame(slug, frame, ts, motion)

    def _note_gated(self, slug: str) -> None:
        self.frames_gated_before_read += 1
        if self._on_gated is not None:
            try:
                self._on_gated(slug)
            except Exception:                             # noqa: BLE001
                pass

    @staticmethod
    def _gated_out(motion: dict) -> bool:
        """True when the broker's analysis found nothing worth detecting on.

        Mirrors pipeline._detect's `result.regions is None` on the same field.
        A notice with no motion block at all is NOT gated out: an older or
        partial broker should degrade to sending us frames, never to silently
        indexing nothing.
        """
        if not motion:
            return False
        return "regions" in motion and motion["regions"] is None

    def _reader_for(self, slug: str, shape: tuple) -> Optional[FrameReader]:
        with self._lock:
            entry = self._readers.get(slug)
            if entry is not None and entry[0] == shape:
                return entry[1]
        # Resolution changed, the broker restarted, or first sight of this
        # camera. Rebuilding rather than reusing matters: a reader mapped at
        # the wrong size reads past its slot and returns a frame stitched from
        # two, and one mapped to a replaced file reads nothing at all.
        try:
            w, h, c, slots = shape[:4]
            reader = FrameReader(slug, w, h, c, slots)
        except OSError as exc:
            # The ring may not exist yet if we heard the notice first.
            log.debug("no ring for %s yet: %s", slug, exc)
            return None
        with self._lock:
            old = self._readers.get(slug)
            self._readers[slug] = (shape, reader)
        if old is not None:
            old[1].close()
        log.info("mapped %s ring %dx%d x%d slots (epoch %s)",
                 slug, shape[0], shape[1], shape[3],
                 shape[4] if len(shape) > 4 else "-")
        return reader

    # ── observability ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            # NOT added to self.frames_refused: FrameReader.read increments
            # its own counter and returns None, at which point _handle counts
            # the same refusal again. Summing both reported roughly double.
            per_reader = sum(r.refused for _s, r in self._readers.values())
            mapped = len(self._readers)
        return {
            "source": "broker",
            "state": self.state,
            "url": self._url,
            "cameras_wanted": len(self._wanted),
            "cameras_every_frame": len(self._every_frame),
            "activity_frames_delivered": self.activity_frames_delivered,
            "rings_mapped": mapped,
            "notices_seen": self.notices_seen,
            "notices_ignored": self.notices_ignored,
            "frames_delivered": self.frames_delivered,
            # What the sampler path calls a sampled frame: everything that
            # arrived for a camera we want, before any gating. Kept comparable
            # on purpose — /health shows this number for both sources.
            "frames_sampled": (self.frames_delivered
                               + self.frames_gated_before_read),
            # A frame the ring had already overwritten. Normal under load and
            # equivalent to a queue eviction; a climbing rate means this
            # service is not keeping up with the broker.
            "frames_refused": self.frames_refused,
            "frames_refused_at_reader": per_reader,
            "frames_gated_before_read": self.frames_gated_before_read,
            "last_frame_at": self.last_frame_at,
            "last_error": self.last_error,
        }
