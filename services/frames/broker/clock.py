"""clock.py — what a frame's timestamp means, the same for every camera.

THE CONTRACT. Every frame of every camera leaves the broker with a timestamp
`ts` that satisfies all of these, whatever its source did:

  1. `ts` is wall-clock epoch seconds - the broker's estimate of when the frame
     was captured. It is the only timebase downstream: the tracker divides by
     differences of it, activities time durations with it, an event's
     started_at IS it, and Smart Search and Open Playback look up NVR footage
     by it.
  2. NEVER AFTER THE FRAME ARRIVED: ts <= arrival, the moment the broker took
     the frame off the wire. Nothing is shot after it arrives, so a later
     timestamp is always wrong - and a clip window built from it would start
     past the footage that exists.
  3. Never more than MAX_LATENCY_S before it arrived. A frame older than that is
     stamped as late as the contract allows rather than placed on a guess.
  4. Strictly increasing per capture session (the sampler publishes only
     strictly increasing values; `normalize` never goes backwards).
  5. The spacing between frames is the source's own spacing when the source's
     clock can be trusted, and the stream's measured frame interval when it
     cannot (see SOURCE and PACED below).
  6. The long-run rate is the wall clock's.

WHY THE OLD CLOCK BROKE IT. It stamped `anchor + pts`, the anchor fixed from
the first frame's arrival and moved by special rules: a gradual drain when the
result ran ahead of the wall clock, a jump forward when it fell 5 s behind. The
two fought - after a long stall the jump lifted the anchor, the delayed video
then caught up past real time, and the drain could only shed 0.2 s per second:
events were stored up to +2.8 s in the future (cam1, 08:57:22 UTC). And a
stream whose timestamps were really arrival times needed a per-camera switch
(camera-mgmt's restamp flag) to be paced at all.

HOW THIS ONE WORKS. Each frame brings two observations - the source timestamp
p (whatever the source put in the stream: a camera clock, an upstream arrival
time, garbage, nothing) and its arrival a. Neither is the capture time, so the
clock uses neither absolutely. It INTEGRATES STEPS inside an envelope:

    t_i = clamp(t_(i-1) + step_i,  a_i - MAX_LATENCY_S,  a_i)

The upper edge is what guarantees (2), and it finds the stream's latency floor
on its own: whenever the source claims a frame is newer than its arrival
allows, the clock is held at the arrival. The step comes from one of two
disciplines, chosen per stream from its own behaviour - never from which
camera it is:

  SOURCE - the source's own step p_i - p_(i-1). Used while the source's steps
      are physically plausible. No camera shoots two frames closer together
      than its frame interval, so a step under PLAUSIBLE_FRACTION of it can only
      be a delivery artefact. A clean camera clock keeps its exact spacing,
      holes included (measured: cam2 0.0% implausible steps).
  PACED - the stream's measured frame interval: the mean source step over the
      last WINDOW frames (or the mean arrival step for a stream with no usable
      timestamps). Used once more than ENTER_PACED of recent steps are
      implausible - measured 18% on a camera stamped upstream on arrival
      (cam1), 37-64% on streams restamped at our own FFmpeg hop (cam4, cam5).
      A clump that follows a stall is then spread back over the gap before it
      instead of collapsing onto one instant.

Either way:
  * a source step of OUTAGE_MS or more that ARRIVAL confirms is an outage -
    real time passes, capped at the arrival;
  * a step that runs backwards, or leaps further ahead than arrival allows
    (a wrap, a recorder resetting its clock), is a discontinuity: the source's
    baseline moves to the new value and this frame advances by the pace;
  * a slow creep of the latency floor (a camera clock running slow, delivery
    settling at a higher latency) is taken up by a small rate correction,
    RATE_SLEW, while even the freshest frame of the last WINDOW trails its
    arrival by more than LAG_TARGET_S.

A SESSION'S FIRST FRAME. FFmpeg reads ahead while it opens a connection and
hands those frames over in a burst - measured 0.7-2.2 s of video in the first
0.5 s. None of it can be older than the time the open took, so the first frame
starts at `arrival - open duration` (at most MAX_START_BACKLOG_S) rather than at
its arrival, which would pin the whole backlog to one instant.

Per-stream state only: a clock is built for one capture session and knows
nothing about any other camera. That is the whole of the multi-camera story -
50 cameras are 50 independent clocks.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Optional

#: How long before its arrival a frame may be stamped. Also the motion
#: service's stale threshold and the old clock's "behind" limit - unchanged.
MAX_LATENCY_S = 5.0

#: A source step this long, confirmed by arrival, is an outage: real time.
OUTAGE_MS = MAX_LATENCY_S * 1000.0

#: A step shorter than this fraction of the frame interval is implausible -
#: no camera captures faster than its frame rate. A quarter is 10 ms at 25 FPS,
#: which also tolerates a source declaring half or double its real rate.
PLAUSIBLE_FRACTION = 0.25

#: Frames of history behind every per-stream statistic: ~10 s at 25 FPS.
WINDOW = 250

#: Steps seen before the discipline is first judged: ~2 s at 25 FPS.
DECIDE_AFTER = 50

#: Implausible share that switches a stream to PACED, and the share it must
#: fall back under (over a full window) to return to SOURCE. Measured shares:
#: 0.0% on a clean camera clock, 18-64% on the others - both margins are wide.
ENTER_PACED = 0.05
LEAVE_PACED = 0.01

#: Rate correction while the latency floor trails: 5% faster, i.e. one second
#: of lag repaid in twenty. Spacing is 5% long while it applies - far inside
#: what the tracker's association tolerates.
RATE_SLEW = 0.05
LAG_TARGET_S = 0.25

#: The most backlog a session's first frame is assumed to carry. The open time
#: over-states the backlog by however long FFmpeg waited for a keyframe, and an
#: over-statement leaves timestamps early until the rate correction repays it:
#: replayed, a 2 s cap left cam1/cam5 ~1.3 s early ten seconds in, 1 s left
#: ~0.3 s, while still recovering cam4's larger backlog (15 -> 17-19 frames
#: published in the first 3 s).
MAX_START_BACKLOG_S = 1.0

#: Steps needed before a measured mean is trusted over the declared rate.
MIN_PACE_SAMPLES = 20

AUTO = "auto"
SOURCE = "source"
PACED = "paced"
POLICIES = (AUTO, SOURCE, PACED)


class _Window:
    """Running mean over the last `size` values."""

    def __init__(self, size: int) -> None:
        self._v: deque = deque()
        self._size = size
        self._sum = 0.0

    def add(self, x: float) -> None:
        self._v.append(x)
        self._sum += x
        if len(self._v) > self._size:
            self._sum -= self._v.popleft()

    def __len__(self) -> int:
        return len(self._v)

    def mean(self) -> float:
        return self._sum / len(self._v)


class _SlidingMin:
    """Minimum of the last `size` values, O(1) amortised per value."""

    def __init__(self, size: int) -> None:
        self._q: deque = deque()          # (index, value), values increasing
        self._size = size
        self._n = 0

    def add(self, x: float) -> None:
        while self._q and self._q[-1][1] >= x:
            self._q.pop()
        self._q.append((self._n, x))
        self._n += 1
        while self._q[0][0] <= self._n - 1 - self._size:
            self._q.popleft()

    def __len__(self) -> int:
        return min(self._n, self._size)

    def min(self) -> float:
        return self._q[0][1]


class StreamClock:
    """Turns (source timestamp, arrival) into a contract timestamp, per frame.

    `declared_fps` is what the source says its frame rate is (0 = unknown; the
    rate is then measured). `policy` is AUTO for every camera - SOURCE or PACED
    only pins the discipline for a deployment that knows better. Neither is
    per-camera code: both come from what the stream itself declares.
    """

    def __init__(self, declared_fps: float = 0.0, policy: str = AUTO,
                 open_seconds: float = 0.0) -> None:
        if policy not in POLICIES:
            raise ValueError(f"clock policy must be one of {POLICIES}, not {policy!r}")
        self.policy = policy
        self.declared_ms: Optional[float] = (1000.0 / declared_fps
                                             if 1.0 <= declared_fps <= 240.0 else None)
        self.mode = PACED if policy == PACED else SOURCE
        self._start_backlog = min(max(0.0, open_seconds), MAX_START_BACKLOG_S)
        self._t: Optional[float] = None
        self._p: Optional[float] = None
        self._a: Optional[float] = None
        self._steps = _Window(WINDOW)          # usable source steps, outages excluded
        self._arrivals = _Window(WINDOW)       # arrival steps, for a stream with no clock
        self._flags: deque = deque()           # was each recent step implausible
        self._implausible = 0
        self._lag = _SlidingMin(WINDOW)
        self._rate = 1.0
        self.frames = 0
        self.held_at_arrival = 0               # the source ran ahead of arrival
        self.lifted_to_floor = 0               # older than MAX_LATENCY_S
        self.discontinuities = 0               # backwards, or an impossible leap
        self.without_source_ts = 0
        self.outages = 0
        self.switches = 0

    # ── the one entry point ─────────────────────────────────────────────────
    def normalize(self, pts_ms, arrival: float) -> float:
        """The contract timestamp for a frame that arrived at `arrival`."""
        p = float(pts_ms) if _usable(pts_ms) else None
        da_ms = (arrival - self._a) * 1000.0 if self._a is not None else None
        step = self._source_step(p, da_ms)
        # A frame without a usable timestamp breaks the source's baseline: the
        # next usable one starts afresh rather than spanning the frames between,
        # which have already advanced by the pace.
        self._p = p
        if self._a is not None and da_ms is not None and da_ms < OUTAGE_MS:
            self._arrivals.add(max(0.0, da_ms))
        self._decide()

        if self._t is None:
            t = arrival - self._start_backlog
        else:
            t = self._t + self._advance(step) * self._rate / 1000.0
        if t > arrival:
            t = arrival
            self.held_at_arrival += 1
        if t < arrival - MAX_LATENCY_S:
            t = arrival - MAX_LATENCY_S
            self.lifted_to_floor += 1
        if self._t is not None and t < self._t:
            # Only when the host clock itself stepped backwards: ordering wins.
            t = self._t
        self._t, self._a = t, arrival
        self.frames += 1
        self._lag.add(arrival - t)
        self._rate = (1.0 + RATE_SLEW if len(self._lag) >= DECIDE_AFTER
                      and self._lag.min() > LAG_TARGET_S else 1.0)
        return t

    # ── the pieces ──────────────────────────────────────────────────────────
    def _source_step(self, p: Optional[float], da_ms: Optional[float]):
        """This frame's source step, classified: (kind, ms or None)."""
        if p is None:
            self.without_source_ts += 1
            self._flag(True)
            return ("none", None)
        if self._p is None:
            return ("first", None)
        dp = p - self._p
        if dp < -1.0 or (da_ms is not None and dp - da_ms > OUTAGE_MS):
            self.discontinuities += 1
            self._flag(True)
            return ("jump", None)
        if dp >= OUTAGE_MS:
            self.outages += 1
            self._flag(False)
            return ("outage", dp)
        dp = max(0.0, dp)
        self._steps.add(dp)
        self._flag(dp < self.frame_ms * PLAUSIBLE_FRACTION)
        return ("step", dp)

    def _advance(self, step) -> float:
        kind, dp = step
        if kind == "outage":
            return dp
        if kind == "step" and self.mode == SOURCE:
            return dp
        return self.pace_ms

    def _flag(self, implausible: bool) -> None:
        self._flags.append(implausible)
        self._implausible += implausible
        if len(self._flags) > WINDOW:
            self._implausible -= self._flags.popleft()

    def _decide(self) -> None:
        if self.policy != AUTO or len(self._flags) < DECIDE_AFTER:
            return
        share = self._implausible / len(self._flags)
        if self.mode == SOURCE and share > ENTER_PACED:
            self.mode, self.switches = PACED, self.switches + 1
        elif (self.mode == PACED and len(self._flags) >= WINDOW
              and share < LEAVE_PACED):
            self.mode, self.switches = SOURCE, self.switches + 1

    # ── measured rates ──────────────────────────────────────────────────────
    @property
    def pace_ms(self) -> float:
        """The stream's frame interval as it is actually arriving."""
        if len(self._steps) >= MIN_PACE_SAMPLES:
            return self._steps.mean()
        if len(self._arrivals) >= MIN_PACE_SAMPLES:
            return self._arrivals.mean()
        return self.frame_ms

    @property
    def frame_ms(self) -> float:
        """The source's frame interval: declared, or else measured."""
        if self.declared_ms is not None:
            return self.declared_ms
        if len(self._steps) >= MIN_PACE_SAMPLES:
            return self._steps.mean()
        if len(self._arrivals) >= MIN_PACE_SAMPLES:
            return self._arrivals.mean()
        return 40.0

    def snapshot(self) -> dict:
        lag_now = (self._a - self._t) if self._t is not None else None
        return {
            "clock": self.mode,
            "clock_policy": self.policy,
            "implausible_steps_pct": (round(100.0 * self._implausible / len(self._flags), 1)
                                      if self._flags else None),
            "pace_ms": round(self.pace_ms, 1),
            "latency_ms": round(lag_now * 1000.0) if lag_now is not None else None,
            "latency_floor_ms": round(self._lag.min() * 1000.0) if len(self._lag) else None,
            "rate_correction": self._rate != 1.0,
            "held_at_arrival": self.held_at_arrival,
            "lifted_to_floor": self.lifted_to_floor,
            "discontinuities": self.discontinuities,
            "outages": self.outages,
            "without_source_ts": self.without_source_ts,
            "clock_switches": self.switches,
        }


def _usable(pts_ms) -> bool:
    if pts_ms is None or isinstance(pts_ms, bool) or not isinstance(pts_ms, (int, float)):
        return False
    return not (math.isnan(pts_ms) or math.isinf(pts_ms) or pts_ms < 0.0)
