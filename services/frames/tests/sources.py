"""Synthetic video sources for the timestamp tests: what every kind of camera,
relay and network does to (source timestamp, arrival).

Each generator returns a list of (pts_ms, arrival_s, capture_s): the timestamp
the source put on the frame (None/NaN when it gave none), when the broker took
it off the wire, and - the ground truth a test can hold the clock against -
when it was really shot. 25 FPS unless stated, starting at wall time 1000 s.

Also: the recorded sequences from the live appliance, and the contract check
every test ends with.
"""
from __future__ import annotations

import gzip
import math
import random
from pathlib import Path

FPS = 25.0
FRAME = 1.0 / FPS
START = 1000.0
FIXTURES = Path(__file__).parent / "fixtures" / "timestamps"


def native(seconds=20.0, latency=0.08, drift=0.0, jitter=0.0, seed=1, start=START):
    """A camera clock: frames 40 ms apart, delivered after `latency` (+ jitter).
    `drift` is how fast the camera's clock runs against the wall (0.03 = 3%)."""
    rnd = random.Random(seed)
    out, last_a = [], 0.0
    for k in range(int(seconds * FPS)):
        c = start + k * FRAME
        a = max(last_a + 0.0004, c + latency + rnd.uniform(0.0, jitter))
        out.append((k * FRAME * 1000.0 * (1.0 + drift), a, c))
        last_a = a
    return out


def native_stalls(seconds=30.0, every=2.0, stall=1.0, latency=0.08, start=START):
    """A camera clock over a link that stalls: the last `stall` s of every
    `every` s the wire goes quiet, then delivers what it held in one burst. No
    frame is lost - they are late. Sessions start live."""
    out = []
    for k in range(int(seconds * FPS)):
        c = start + k * FRAME
        phase = (c - start) % every
        a = c + latency
        if phase >= every - stall:                         # held to the cycle's end
            a = c - phase + every + latency
        if out and a <= out[-1][1]:
            a = out[-1][1] + 0.0004
        out.append((k * FRAME * 1000.0, a, c))
    return out


def native_holes(seconds=30.0, every=3.0, hole=0.8, latency=0.08, start=START):
    """A camera clock with frames LOST on the way: every `every` s, `hole` s of
    video never arrives. The clock says so - its steps jump across the hole."""
    out = []
    for k in range(int(seconds * FPS)):
        c = start + k * FRAME
        if (c - start) % every < hole and c - start >= every:
            continue
        out.append((k * FRAME * 1000.0, c + latency, c))
    return out


def jittery(seconds=20.0, amplitude_ms=17.5, latency=0.08, start=START):
    """A clock stamped on arrival somewhere upstream: steps alternate 5/75 ms
    around the real 40 (measured shape on cam1/cam3)."""
    out = []
    for k in range(int(seconds * FPS)):
        c = start + k * FRAME
        pts = k * FRAME * 1000.0 + (amplitude_ms if k % 2 else -amplitude_ms) + amplitude_ms
        out.append((pts, c + latency, c))
    return out


def restamped(seconds=30.0, every=1.0, stall=0.9, hop_latency=0.01, start=START):
    """Timestamps rewritten to ARRIVAL at an upstream hop (camera-mgmt's FFmpeg
    restamp): the link stalls, then a clump arrives - and is stamped - within a
    millisecond or two. The clump is video shot during the stall."""
    out, last_hop = [], 0.0
    for k in range(int(seconds * FPS)):
        c = start + k * FRAME
        phase = (c - start) % every
        hop = c + 0.05                                   # reached the hop
        if phase >= every - stall:                       # held to the cycle's end
            hop = c - phase + every + 0.05
        hop = max(hop, last_hop + 0.0004)                # a clump: 0.4 ms apart
        last_hop = hop
        out.append(((hop - start) * 1000.0, hop + hop_latency, c))
    return out


def restamped_lossy(frames=3000, seed=7, start=START):
    """The measured bad case: arrival-stamped over a lossy link. Chunks never
    arrive, others come in clumps, the rest trickle."""
    rnd = random.Random(seed)
    out, t, c, i = [], start, start, 0
    while i < frames:
        roll = rnd.random()
        if roll < 0.15:                                    # a chunk lost outright
            gap = rnd.uniform(0.5, 3.0)
            t += gap; c += gap; i += int(gap * FPS)
        elif roll < 0.35:                                  # held, then a clump
            held = rnd.randint(10, 60)
            t += held * FRAME
            for j in range(held):
                t += 0.0004
                out.append(((t - start) * 1000.0, t + 0.01, c + j * FRAME)); i += 1
            c += held * FRAME
        else:                                              # trickle
            for _ in range(rnd.randint(5, 30)):
                t += FRAME; c += FRAME
                out.append(((t - start) * 1000.0, t + 0.01, c)); i += 1
    return out


def with_pts(seq, fn):
    """The same frames with their source timestamps rewritten by fn(k, pts)."""
    return [(fn(k, p), a, c) for k, (p, a, c) in enumerate(seq)]


def wrapped(seconds=20.0, at=10.0, jump_ms=-4_294_967.296):
    """A recorder clock that jumps - the 32-bit microsecond wrap camera-mgmt's
    restamp hop exists for is -4295 s, every 71 m 35 s."""
    n = int(at * FPS)
    return with_pts(native(seconds), lambda k, p: p + (jump_ms if k >= n else 0.0) + 5_000_000.0)


def missing(seconds=20.0, every=1, bad=None):
    """No usable source timestamp at all (or on every `every`-th frame)."""
    return with_pts(native(seconds), lambda k, p: (bad if k % every == 0 else p))


def outage(before=10.0, gone=12.0, after=10.0, latency=0.08, start=START):
    """The source and the wire both gone for `gone` s - nothing shot is lost
    in transit; the camera simply is not there. Its clock carries the gap."""
    out = []
    for k in range(int((before + gone + after) * FPS)):
        c = start + k * FRAME
        if before <= c - start < before + gone:
            continue
        out.append((k * FRAME * 1000.0, c + latency, c))
    return out


def long_stall(seconds=30.0, at=10.0, stall=7.0, latency=0.08, start=START):
    """The case that stored events +2.8 s in the future: the wire holds a camera
    clock's video for LONGER than MAX_LATENCY_S, then flushes it."""
    out = []
    for k in range(int(seconds * FPS)):
        c = start + k * FRAME
        a = c + latency
        if at <= c - start < at + stall:
            a = start + at + stall + latency
        if out and a <= out[-1][1]:
            a = out[-1][1] + 0.0004
        out.append((k * FRAME * 1000.0, a, c))
    return out


def recorded(name):
    """A sequence recorded from the live appliance on 2026-09-18: (pts_ms,
    arrival_s, session). No ground truth - these are real."""
    rows = []
    with gzip.open(FIXTURES / (name + ".csv.gz"), "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            a, p, s = line.split(",")
            rows.append((float(p), START + float(a), int(s)))
    return rows


RECORDED = ("cam1_native_jittery", "cam2_native_clean_holes", "cam4_restamped",
            "cam4_restamped_lossy", "cam5_restamped")


def assert_contract(pairs, published=None, max_latency=5.0):
    """The contract, checked on every frame's (t, arrival) and on what was
    published: never after arrival, never more than max_latency before it,
    non-decreasing on every frame, strictly increasing when published."""
    for t, a in pairs:
        assert t <= a + 1e-9, f"stamped {t - a:+.4f} s after its arrival"
        assert t >= a - max_latency - 1e-9, f"stamped {a - t:.3f} s before its arrival"
    for (t0, _), (t1, _) in zip(pairs, pairs[1:]):
        assert t1 >= t0, "time ran backwards"
    if published is not None:
        for (t0, _), (t1, _) in zip(published, published[1:]):
            assert t1 > t0, "published timestamps must strictly increase"
        for t, a in published:
            assert t <= a + 1e-9 and t >= a - max_latency - 1e-9


def is_finite(x):
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))
