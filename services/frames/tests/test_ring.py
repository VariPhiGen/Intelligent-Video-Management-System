"""The shared-memory frame ring.

WHAT THESE PROTECT. Tearing is the failure that matters and it is silent: a
consumer that copies a slot mid-write gets the front of one frame and the back
of another, then crops, embeds and stores the result without any error
anywhere. The seqlock is the only thing standing between us and that, so it is
tested directly rather than assumed.

The cross-CONTAINER case was proved separately in the Phase 0 spike — 485
frames at 62.2 MB/s with zero torn reads on Docker Desktop / WSL2. These tests
cover the discipline itself, in-process, where a deliberate mid-write can be
staged.

Run: python3 -m pytest tests -q   (from services/frames)
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.ring import (CTRL_BYTES, FrameReader,                # noqa: E402
                         FrameRing, shm_capacity, shm_path)

W, H, C = 160, 90, 3


@pytest.fixture
def ring():
    name = f"pytest-{os.getpid()}-{time.time_ns()}"
    r = FrameRing(name, W, H, C, slots=4)
    yield r
    r.close()


def frame(value: int) -> np.ndarray:
    return np.full((H, W, C), value, np.uint8)


# ── the basics ───────────────────────────────────────────────────────────────
def test_a_frame_survives_the_round_trip(ring):
    seq, slot = ring.publish(frame(7), 1234.5)
    rd = FrameReader(ring.camera, W, H, C, slots=ring.slots)
    try:
        got = rd.read(slot, seq)
        assert got is not None
        assert got.shape == (H, W, C)
        assert int(got.min()) == int(got.max()) == 7
    finally:
        rd.close()


def test_slots_are_reused_in_order(ring):
    seen = [ring.publish(frame(i % 250), float(i))[1] for i in range(1, 13)]
    assert seen == [i % ring.slots for i in range(1, 13)]


def test_the_ring_reports_its_real_size(ring):
    assert ring.nbytes == CTRL_BYTES + ring.slots * W * H * C
    assert ring.snapshot()["frame_bytes"] == W * H * C


def test_a_mismatched_frame_is_refused(ring):
    """A ring built for one resolution must not silently accept another —
    it would write past the slot and corrupt its neighbour."""
    with pytest.raises(ValueError):
        ring.publish(np.zeros((H + 10, W, C), np.uint8), 1.0)


def test_a_non_contiguous_frame_is_handled(ring):
    """Crops and slices arrive non-contiguous; writing their raw buffer would
    store garbage."""
    big = np.full((H * 2, W * 2, C), 11, np.uint8)
    view = big[::2, ::2]                       # non-contiguous, correct shape
    assert not view.flags["C_CONTIGUOUS"]
    seq, slot = ring.publish(view, 1.0)
    rd = FrameReader(ring.camera, W, H, C, slots=ring.slots)
    try:
        got = rd.read(slot, seq)
        assert got is not None and int(got.min()) == int(got.max()) == 11
    finally:
        rd.close()


# ── the seqlock, which is the whole safety argument ──────────────────────────
def test_a_stale_sequence_is_refused(ring):
    """A consumer asking for a frame the writer has already replaced must be
    told no, not handed whatever is in the slot now."""
    seq, slot = ring.publish(frame(1), 1.0)
    for i in range(ring.slots):                # lap the ring
        ring.publish(frame(50 + i), 2.0 + i)
    rd = FrameReader(ring.camera, W, H, C, slots=ring.slots)
    try:
        assert rd.read(slot, seq) is None
        assert rd.torn_reads == 1
    finally:
        rd.close()


def test_a_slot_marked_mid_write_is_refused(ring):
    """The odd-sequence state, staged directly. A reader that ignores it gets
    a frame that is half one image and half another."""
    seq, slot = ring.publish(frame(9), 1.0)
    struct.pack_into("<Q", ring._mm, slot * 16, seq * 2 - 1)   # force ODD
    rd = FrameReader(ring.camera, W, H, C, slots=ring.slots)
    try:
        assert rd.read(slot, seq) is None
        assert rd.torn_reads == 1
    finally:
        rd.close()


def test_a_write_completing_during_a_read_is_detected(ring, monkeypatch):
    """The race the seqlock exists for: the sequence moves between the check
    before the copy and the check after it.

    Staged by making the SECOND sequence read return a different value, which
    is exactly what a writer finishing mid-read would produce.
    """
    import broker.ring as ring_mod
    seq, slot = ring.publish(frame(3), 1.0)
    rd = FrameReader(ring.camera, W, H, C, slots=ring.slots)

    real_unpack = ring_mod.struct.unpack_from
    calls = {"n": 0}

    def shifting_unpack(fmt, buf, off=0):
        out = real_unpack(fmt, buf, off)
        if fmt == "<Q":
            calls["n"] += 1
            if calls["n"] == 2:            # the post-copy check
                return ((seq + ring.slots) * 2,)
        return out

    try:
        monkeypatch.setattr(ring_mod.struct, "unpack_from", shifting_unpack)
        assert rd.read(slot, seq) is None, "a torn read was accepted"
        assert rd.torn_reads == 1
    finally:
        rd.close()


def test_concurrent_writer_never_yields_a_torn_frame(ring):
    """Every frame is a single repeated byte, so a torn read contains two —
    min() != max() catches it. Same detector as the Phase 0 spike."""
    stop = threading.Event()
    published: list[tuple[int, int, int]] = []

    def writer():
        i = 0
        while not stop.is_set() and i < 400:
            i += 1
            value = i % 250
            seq, slot = ring.publish(frame(value), time.time())
            published.append((seq, slot, value))
            time.sleep(0.001)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    rd = FrameReader(ring.camera, W, H, C, slots=ring.slots)
    torn = checked = 0
    try:
        deadline = time.time() + 3
        while time.time() < deadline and t.is_alive():
            if not published:
                time.sleep(0.002); continue
            seq, slot, value = published[-1]
            got = rd.read(slot, seq)
            if got is None:
                continue                       # correctly refused, not torn
            checked += 1
            if int(got.min()) != int(got.max()):
                torn += 1
        stop.set(); t.join(timeout=2)
    finally:
        rd.close()
    assert checked > 0, "the test never managed to read anything"
    assert torn == 0, f"{torn} torn frames out of {checked} accepted reads"


# ── operational ──────────────────────────────────────────────────────────────
def test_closing_unlinks_the_backing_file():
    name = f"pytest-unlink-{os.getpid()}-{time.time_ns()}"
    r = FrameRing(name, W, H, C, slots=2)
    path = shm_path(name)
    assert os.path.exists(path)
    r.close()
    assert not os.path.exists(path), "a removed camera leaked its shm file"


def test_a_recreated_ring_gets_a_new_epoch():
    """CONSUMERS DEPEND ON THIS TO NOTICE A RESTART. A reader holding the
    previous mapping reads a frozen image whose sequence never advances and
    refuses every frame — silently, with the ring still reported as mapped.
    The epoch is how it learns to remap.

    The inode CANNOT serve here, which is how this was found: close() unlinks
    the file and /dev/shm is a tmpfs, so recreated rings came back with the
    same inode numbers (2..6 before a restart on the appliance, 2..6 after).
    """
    name = f"pytest-epoch-{os.getpid()}-{time.time_ns()}"
    first = FrameRing(name, W, H, C, slots=2)
    e1, ino1 = first.epoch, os.stat(shm_path(name)).st_ino
    first.close()
    second = FrameRing(name, W, H, C, slots=2)
    try:
        assert second.epoch != e1, "a restarted ring looked like the old one"
        if os.stat(shm_path(name)).st_ino == ino1:
            # The exact tmpfs behaviour that broke the first attempt. Not
            # asserted as required — it is filesystem-dependent — but when it
            # happens the epoch must still differ, which is the point.
            assert second.epoch != e1
    finally:
        second.close()


def test_shm_capacity_is_reportable():
    """Reported on /health because 64 MB is Docker's default and under half of
    one five-camera ring set — a silent allocation failure otherwise."""
    cap = shm_capacity()
    assert "total_bytes" in cap and cap["total_bytes"] > 0
    assert cap["free_bytes"] <= cap["total_bytes"]
