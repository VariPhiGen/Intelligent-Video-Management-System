"""ring.py — frames crossing the container boundary through /dev/shm.

WHY SHARED MEMORY AND NOT A QUEUE. A 1080p BGR frame is 6.22 MB. At the 5 FPS
this samples at, across five cameras, that is 156 MB/s — nothing for RAM and
impossible for a socket. Consumers map the same pages the decoder wrote; nothing is
serialised and nothing is sent.

THE FAILURE THIS GUARDS AGAINST IS TEARING, and it is invisible without a
guard. A consumer that copies a slot while the writer is mid-write gets the
front of one frame and the back of another, and every stage downstream
processes the result happily — wrong crops, wrong embeddings, wrong rows, no
error anywhere. So each slot carries a sequence number under a seqlock:

    writer:   seq -> ODD    (in progress, do not read)
              ... write the frame ...
              seq -> EVEN   (stable)

    reader:   read seq; skip if odd
              copy the frame
              read seq again; discard the copy if it moved

Verified 2026-09-08 in a two-container spike on Docker Desktop / WSL2: 485
frames across two runs at 62.2 MB/s, ZERO torn reads, copy 0.95-1.31 ms, and
write-to-read latency p95 13 ms against a 500 ms sample interval.

CONSUMERS COPY OUT IMMEDIATELY AND DO NOT HOLD SLOTS. That is deliberate.
SmartSearch's ingest queue holds up to 64 frames; if those were references
into this ring, a slot could be recycled underneath a queued reference. Sizing
the ring to match would cost 64 x 5 x 6.22 MB = 2 GB. Copying on receipt costs
~1 ms and removes the cross-process lifetime problem entirely — and it is not
a regression, because the consumer already held a private array per queued
frame before any of this existed.
"""
from __future__ import annotations

import mmap
import os
import struct
import time
from typing import Optional

import numpy as np

#: Control block, page aligned, ahead of the frame slots. One 16-byte entry per
#: slot: sequence number then wall-clock timestamp.
CTRL_BYTES = 4096
_ENTRY = 16          # u64 seq + f64 ts
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
_SHM_DIR = os.environ.get("FRAMES_SHM_DIR", "/dev/shm")


def shm_path(camera: str) -> str:
    #: One file per camera rather than one shared file: cameras are added and
    #: removed at runtime, and a per-camera file can be unlinked without
    #: disturbing anyone else's mapping.
    return os.path.join(_SHM_DIR, f"vms_frames.{camera}")


class FrameRing:
    """Writer side. One per camera, owned by that camera's worker."""

    def __init__(self, camera: str, width: int, height: int,
                 channels: int = 3, slots: int = 4) -> None:
        self.camera = camera
        self.width, self.height, self.channels = width, height, channels
        self.slots = max(2, slots)
        self.frame_bytes = width * height * channels
        self.path = shm_path(camera)
        total = CTRL_BYTES + self.slots * self.frame_bytes

        with open(self.path, "wb") as fh:
            fh.truncate(total)
        self._fd = os.open(self.path, os.O_RDWR)
        self._mm = mmap.mmap(self._fd, total)
        self._mm[0:CTRL_BYTES] = b"\x00" * CTRL_BYTES
        self._seq = 0
        self.bytes_written = 0
        #: WHICH INCARNATION OF THIS RING. The path is stable across a broker
        #: restart but the file behind it is not: the old one is replaced, and
        #: a consumer still holding an mmap of the previous inode reads a
        #: frozen copy forever. Its sequence numbers never advance, so every
        #: read is refused and the camera goes quiet with no error anywhere —
        #: observed here as SmartSearch delivering zero frames after a broker
        #: restart while reporting five rings healthily mapped.
        #:
        #: NOT the inode, which was the first attempt and does not work: the
        #: ring unlinks its file on shutdown and /dev/shm is a tmpfs, so the
        #: five recreated files came back with the SAME five inode numbers.
        #: Measured on this appliance — epochs 2..6 before a broker restart
        #: and 2..6 after it, so nothing remapped and the consumer stayed
        #: blind. Creation time cannot collide that way.
        self.epoch = time.time_ns()

    @property
    def nbytes(self) -> int:
        return CTRL_BYTES + self.slots * self.frame_bytes

    def publish(self, frame: np.ndarray, ts: float) -> tuple[int, int]:
        """Write one frame. Returns (seq, slot) for the notice.

        The odd/even sequence bracket is the entire safety mechanism; a reader
        that ignores it will eventually get a torn frame and never know.
        """
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            raise ValueError(
                f"{self.camera}: frame {frame.shape[1]}x{frame.shape[0]} does "
                f"not match ring {self.width}x{self.height}")
        self._seq += 1
        seq = self._seq
        slot = seq % self.slots
        off = slot * _ENTRY

        struct.pack_into("<Q", self._mm, off, seq * 2 - 1)      # ODD: writing
        base = CTRL_BYTES + slot * self.frame_bytes
        # `frame` may be non-contiguous after a crop or a slice; tobytes()
        # would copy twice, so require contiguity from the caller instead.
        self._mm[base:base + self.frame_bytes] = memoryview(
            frame if frame.flags["C_CONTIGUOUS"] else np.ascontiguousarray(frame)
        ).cast("B")
        struct.pack_into("<d", self._mm, off + 8, ts)
        struct.pack_into("<Q", self._mm, off, seq * 2)          # EVEN: stable
        self.bytes_written += self.frame_bytes
        return seq, slot

    def close(self, unlink: bool = True) -> None:
        try:
            self._mm.close()
        except (BufferError, ValueError):
            pass                       # a consumer view may still be exported
        try:
            os.close(self._fd)
        except OSError:
            pass
        if unlink:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def snapshot(self) -> dict:
        return {"camera": self.camera, "path": self.path,
                "width": self.width, "height": self.height,
                "slots": self.slots, "frame_bytes": self.frame_bytes,
                "ring_bytes": self.nbytes, "seq": self._seq}


class FrameReader:
    """Consumer side. Lives in SmartSearch (and in the parity harness).

    Ships here rather than in each consumer so the seqlock discipline has one
    definition — a consumer that reimplements it slightly wrong gets torn
    frames that look like bad detections.
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
        self.torn_reads = 0

    def read(self, slot: int, expect_seq: int) -> Optional[np.ndarray]:
        """A private copy of `slot`, or None if it was not stable.

        None is normal under load and means "the writer moved on" — the caller
        should drop the frame rather than retry, which is the same drop-oldest
        posture the ingest queue already takes.
        """
        off = slot * _ENTRY
        before = struct.unpack_from("<Q", self._mm, off)[0]
        if before % 2 == 1 or before // 2 != expect_seq:
            self.torn_reads += 1
            return None
        base = CTRL_BYTES + slot * self.frame_bytes
        buf = np.frombuffer(self._mm, dtype=np.uint8,
                            count=self.frame_bytes, offset=base).copy()
        after = struct.unpack_from("<Q", self._mm, off)[0]
        if after != before:
            self.torn_reads += 1
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


def shm_capacity() -> dict:
    """What /dev/shm can actually hold — the limit that bites first.

    Docker gives a container 64 MB by default, which is under half of one
    five-camera ring set. Reporting it on /health turns a silent failure to
    allocate into something an operator can see before it happens.
    """
    try:
        st = os.statvfs(_SHM_DIR)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total_bytes": total, "free_bytes": free,
                "total_mb": round(total / 1e6, 1), "free_mb": round(free / 1e6, 1)}
    except OSError as exc:                                     # noqa: BLE001
        return {"error": str(exc)}
