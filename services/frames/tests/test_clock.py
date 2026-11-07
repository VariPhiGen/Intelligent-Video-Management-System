"""The timestamp contract (broker/clock.py), held against every kind of source.

Every frame of every camera: never stamped after it arrived, never more than
MAX_LATENCY_S before, never backwards; spaced like the source when the source's
clock is trustworthy and at the stream's own frame rate when it is not. Each
source below is a real behaviour measured on this appliance or a failure a
camera can produce; none of them is told to the clock - it works out which it
is facing from the stream itself.

Run (from services/frames): python -m pytest tests -q
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broker.clock import (AUTO, LAG_TARGET_S, MAX_LATENCY_S,  # noqa: E402
                          MAX_START_BACKLOG_S, PACED, SOURCE, StreamClock)
from broker.sampling import FrameSampler  # noqa: E402
import sources as S  # noqa: E402


def run(seq, **kw):
    """Feed a source through a fresh sampler. Returns (sampler, every frame's
    (t, arrival), published (ts, arrival, capture))."""
    s = FrameSampler(5.0, S.FPS, **kw)
    every, published = [], []
    for pts, arrival, capture in seq:
        ts = s.consider(pts, arrival)
        every.append((s.clock._t, arrival))
        if ts is not None:
            published.append((ts, arrival, capture))
    return s, every, published


def gaps(published, skip=0):
    return [b[0] - a[0] for a, b in zip(published[skip:], published[skip + 1:])]


ALL_SOURCES = {
    "native": lambda: S.native(),
    "native, jittery delivery": lambda: S.native(jitter=0.3),
    "native, clock 3% fast": lambda: S.native(drift=0.03),
    "native, clock 3% slow": lambda: S.native(drift=-0.03, seconds=60),
    "native, stalls + bursts": lambda: S.native_stalls(),
    "native, lost frames": lambda: S.native_holes(),
    "native, long stall": lambda: S.long_stall(),
    "jittery upstream stamps": lambda: S.jittery(),
    "restamped on arrival": lambda: S.restamped(),
    "restamped, lossy": lambda: S.restamped_lossy(),
    "backwards wrap": lambda: S.wrapped(),
    "forward leap": lambda: S.wrapped(jump_ms=3_600_000.0),
    "no timestamps": lambda: S.missing(),
    "NaN timestamps": lambda: S.missing(bad=float("nan")),
    "infinite timestamps": lambda: S.missing(bad=float("inf")),
    "negative timestamps": lambda: S.missing(bad=-40.0),
    "every other timestamp missing": lambda: S.missing(every=2),
    "outage": lambda: S.outage(),
}


class TestTheContractHoldsForEverySource:
    @pytest.mark.parametrize("name", sorted(ALL_SOURCES))
    def test_never_after_arrival_never_too_early_never_backwards(self, name):
        _, every, published = run(ALL_SOURCES[name]())
        S.assert_contract(every, [(t, a) for t, a, _ in published])

    @pytest.mark.parametrize("name", sorted(ALL_SOURCES))
    def test_never_more_than_the_sample_rate(self, name):
        seq = ALL_SOURCES[name]()
        _, _, published = run(seq)
        span = seq[-1][1] - seq[0][1]
        assert len(published) <= span * 5.0 + 2

    @pytest.mark.parametrize("name", sorted(ALL_SOURCES))
    def test_the_same_input_gives_the_same_output(self, name):
        seq = ALL_SOURCES[name]()
        assert run(seq)[2] == run(seq)[2]


class TestCleanCameraClocks:
    """A clock with no impossible steps is trusted: its spacing IS the video's."""

    def test_it_is_trusted(self):
        s, _, _ = run(S.native())
        assert s.mode == SOURCE and s.clock.snapshot()["implausible_steps_pct"] == 0.0

    def test_it_publishes_the_sample_rate_exactly_spaced(self):
        _, _, published = run(S.native(seconds=20))
        assert 4.9 <= len(published) / 20.0 <= 5.05
        assert all(abs(g - 0.2) < 1e-6 for g in gaps(published, 2))

    def test_a_stall_is_late_video_not_missing_video(self):
        # Held for 1 s every 2 s, then flushed: every published frame keeps its
        # own capture time, however late it arrived - one constant offset.
        s, _, published = run(S.native_stalls())
        assert s.mode == SOURCE
        assert 4.9 <= len(published) / 30.0 <= 5.05
        offset = published[0][0] - published[0][2]
        assert all(abs((ts - c) - offset) < 0.002 for ts, _, c in published)
        assert max(a - ts for ts, a, _ in published) > 0.9, "the held ones were stamped early"

    def test_lost_frames_stay_a_gap(self):
        # A clean clock says where video is missing; smearing frames over the
        # hole would invent motion at times nothing was seen.
        _, _, published = run(S.native_holes(every=3.0, hole=0.8))
        big = [g for g in gaps(published) if g > 0.5]
        assert big and all(abs(g - 1.0) < 0.05 for g in big)

    def test_the_old_future_timestamp_case(self):
        # Stored +2.79 s in the future on cam1 (08:57 UTC): a stall longer than
        # the latency limit, then the held video flushed.
        s, every, published = run(S.long_stall(stall=7.0))
        assert max(t - a for t, a in every) <= 0.0
        assert s.clock.lifted_to_floor > 0, "precondition: it went past the limit"

    def test_a_fast_camera_clock_is_held_at_arrival(self):
        s, every, _ = run(S.native(drift=0.03, seconds=40))
        assert s.clock.held_at_arrival > 0
        assert max(t - a for t, a in every) <= 0.0

    def test_a_slow_camera_clock_is_disciplined(self):
        # 3% slow - far worse than real cameras drift: left alone it would sit
        # at the 5 s limit within three minutes. Disciplined, it stays within a
        # fraction of a second, oscillating just above LAG_TARGET_S.
        s, every, _ = run(S.native(drift=-0.03, seconds=180))
        worst = max(a - t for t, a in every[250:])
        assert worst < LAG_TARGET_S + 0.5, f"{worst:.2f} s behind"
        assert s.clock.lifted_to_floor == 0


class TestArrivalStampedClocks:
    """Timestamps rewritten to an upstream arrival time: the clock notices."""

    def test_it_is_recognised_without_being_told(self):
        s, _, _ = run(S.restamped())
        assert s.mode == PACED and s.clock.switches == 1

    def test_each_clump_is_spread_over_the_stall_before_it(self):
        # One clump per second, stalled 90% of the time: a stream-time rule
        # publishes ~1 per clump. Measured once the clock has recognised the
        # stream (DECIDE_AFTER steps; the next test pins how soon).
        _, _, published = run(S.restamped(seconds=30))
        per_second = [sum(1 for x in published if int(x[1] - S.START) == k) for k in range(4, 30)]
        assert per_second == [5] * len(per_second)
        g = gaps([x for x in published if x[1] >= S.START + 4.0])
        assert all(0.15 <= x <= 0.25 for x in g)

    def test_it_is_recognised_within_the_first_seconds(self):
        s = FrameSampler(5.0, S.FPS)
        for pts, a, _ in S.restamped(seconds=30):
            s.consider(pts, a)
            if s.mode == PACED:
                break
        assert a - S.START < 3.5, f"took {a - S.START:.1f} s"

    def test_spread_back_means_earlier_than_arrival_not_later(self):
        _, _, published = run(S.restamped(seconds=30))
        assert all(ts <= a for ts, a, _ in published)
        assert max(a - ts for ts, a, _ in published[20:]) > 0.5, "the clump went back into its gap"

    def test_a_lossy_arrival_stamped_stream_stays_inside_the_envelope(self):
        s, every, published = run(S.restamped_lossy())
        assert s.mode == PACED
        S.assert_contract(every, [(t, a) for t, a, _ in published])


class TestJitteryClocks:
    def test_a_clock_stamped_upstream_is_paced(self):
        # Steps alternate 5/75 ms: the video is fine, the clock is not.
        s, _, published = run(S.jittery())
        assert s.mode == PACED
        assert all(abs(g - 0.2) < 0.03 for g in gaps(published, 20))


class TestDiscontinuities:
    def test_a_backwards_wrap_is_one_step_not_a_stall(self):
        s, every, published = run(S.wrapped())
        assert s.clock.discontinuities == 1
        g = gaps(published, 2)
        assert max(g) < 0.3, "the timeline carried on through the wrap"

    def test_a_forward_leap_is_held_to_arrival(self):
        s, every, _ = run(S.wrapped(jump_ms=3_600_000.0))
        assert s.clock.discontinuities == 1
        assert max(t - a for t, a in every) <= 0.0

    def test_a_repeated_timestamp_is_not_a_discontinuity(self):
        seq = S.with_pts(S.native(), lambda k, p: p - 0.5 if k == 100 else p)
        s, _, _ = run(seq)
        assert s.clock.discontinuities == 0


class TestMissingTimestamps:
    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -40.0, "x", True])
    def test_a_stream_without_timestamps_is_paced_on_arrival(self, bad):
        s, _, published = run(S.missing(bad=bad))
        assert s.mode == PACED
        assert s.clock.without_source_ts == len(S.missing())
        assert 4.8 <= len(published) / 20.0 <= 5.05

    def test_time_is_not_counted_twice_across_missing_ones(self):
        # A usable timestamp after missing ones must not ALSO add the span the
        # missing frames already advanced by.
        seq = S.missing(every=2, seconds=60)
        _, every, _ = run(seq)
        span_t = every[-1][0] - every[25][0]
        span_a = every[-1][1] - every[25][1]
        assert abs(span_t - span_a) < 0.5


class TestOutages:
    def test_an_outage_is_real_time_passing(self):
        s, every, published = run(S.outage(before=10, gone=12, after=10))
        assert s.clock.outages == 1
        after = [x for x in published if x[2] > S.START + 22]
        assert all(a - ts < 0.2 for ts, a, _ in after[1:]), "no lag carried out of it"
        assert s.clock.lifted_to_floor == 0

    def test_after_an_outage_it_resumes_at_the_sample_rate_not_in_a_burst(self):
        _, _, published = run(S.outage(before=10, gone=12, after=10))
        back = [x for x in published if x[2] > S.START + 22]
        assert all(g >= 0.19 for g in gaps(back))


class TestTheFirstFrame:
    def test_without_an_open_time_it_is_its_arrival(self):
        c = StreamClock(S.FPS)
        assert c.normalize(0.0, 1000.0) == 1000.0

    def test_an_open_that_took_time_means_older_video(self):
        c = StreamClock(S.FPS, open_seconds=0.6)
        assert c.normalize(0.0, 1000.0) == pytest.approx(999.4)

    def test_but_never_older_than_the_cap(self):
        c = StreamClock(S.FPS, open_seconds=30.0)
        assert c.normalize(0.0, 1000.0) == pytest.approx(1000.0 - MAX_START_BACKLOG_S)

    def test_a_backlog_burst_at_open_is_published_not_squashed(self):
        # 1 s of video FFmpeg read while opening, delivered in 25 ms, then live.
        live = S.native(seconds=10)
        seq = [(p, S.START + 0.001 * k if k < 25 else a, c) for k, (p, a, c) in enumerate(live)]
        with_open = run(seq, open_seconds=1.0)[2]
        without = run(seq)[2]
        first = lambda pub: sum(1 for ts, a, c in pub if a < S.START + 1.0)
        assert first(with_open) > first(without)


class TestPolicy:
    def test_auto_is_the_default(self):
        assert FrameSampler(5.0, 25.0).clock.policy == AUTO

    def test_a_deployment_can_pin_either_discipline(self):
        assert run(S.restamped(), policy=SOURCE)[0].mode == SOURCE
        assert run(S.native(), policy=PACED)[0].mode == PACED

    def test_a_pinned_discipline_still_keeps_the_contract(self):
        for policy in (SOURCE, PACED):
            _, every, published = run(S.restamped_lossy(), policy=policy)
            S.assert_contract(every, [(t, a) for t, a, _ in published])

    def test_an_unknown_policy_is_refused(self):
        with pytest.raises(ValueError):
            StreamClock(25.0, policy="camera4")


class TestSwitching:
    def test_a_stream_that_starts_clumping_switches_and_stays_ordered(self):
        seq = S.native(seconds=20) + [(p + 20_000.0, a + 20.0, c + 20.0) for p, a, c in S.restamped(seconds=20)]
        s, every, published = run(seq)
        assert s.mode == PACED and s.clock.switches == 1
        S.assert_contract(every, [(t, a) for t, a, _ in published])

    def test_and_goes_back_once_its_clock_is_clean_again(self):
        seq = S.restamped(seconds=20)
        seq += [(p + seq[-1][0] + 40.0, a + 20.1, c + 20.0) for p, a, c in S.native(seconds=30)]
        s, _, _ = run(seq)
        assert s.mode == SOURCE and s.clock.switches == 2


class TestManyCameras:
    """50 cameras are 50 independent clocks: no camera is special, and none can
    affect another."""

    KINDS = [ALL_SOURCES[k] for k in sorted(ALL_SOURCES)]

    def test_fifty_cameras_of_every_kind_each_keep_the_contract(self):
        cams = {f"cam{i:02d}": self.KINDS[i % len(self.KINDS)]() for i in range(50)}
        samplers = {name: FrameSampler(5.0, S.FPS) for name in cams}
        every = {name: [] for name in cams}
        published = {name: [] for name in cams}
        # Interleaved in arrival order, as 50 capture threads would deliver.
        merged = sorted((a, name, k) for name, seq in cams.items()
                        for k, (_, a, _) in enumerate(seq))
        for a, name, k in merged:
            pts = cams[name][k][0]
            ts = samplers[name].consider(pts, a)
            every[name].append((samplers[name].clock._t, a))
            if ts is not None:
                published[name].append((ts, a))
        for name in cams:
            S.assert_contract(every[name], published[name])

    def test_interleaving_changes_nothing(self):
        a_seq, b_seq = S.restamped(), S.native_holes()
        alone = run(a_seq)[2]
        sa, sb = FrameSampler(5.0, S.FPS), FrameSampler(5.0, S.FPS)
        out = []
        for (pa, aa, ca), (pb, ab, cb) in zip(a_seq, b_seq):
            ts = sa.consider(pa, aa)
            if ts is not None:
                out.append((ts, aa, ca))
            sb.consider(pb, ab)
        assert out == alone[:len(out)]

    def test_each_camera_chooses_its_own_discipline(self):
        chosen = {kind: run(ALL_SOURCES[kind]())[0].mode
                  for kind in ("native", "restamped on arrival", "jittery upstream stamps",
                               "no timestamps", "native, lost frames")}
        assert chosen == {"native": SOURCE, "restamped on arrival": PACED,
                          "jittery upstream stamps": PACED, "no timestamps": PACED,
                          "native, lost frames": SOURCE}


class TestObservability:
    def test_the_snapshot_says_how_the_clock_is_working(self):
        s, _, _ = run(S.restamped())
        snap = s.snapshot()
        for key in ("clock", "clock_policy", "implausible_steps_pct", "pace_ms", "latency_ms",
                    "latency_floor_ms", "rate_correction", "held_at_arrival", "lifted_to_floor",
                    "discontinuities", "outages", "without_source_ts", "clock_switches",
                    "sample_fps", "published", "skipped"):
            assert key in snap, key
        assert snap["clock"] == PACED and snap["latency_ms"] >= 0
        assert snap["published"] + snap["skipped"] == len(S.restamped())

    def test_latency_is_never_negative(self):
        for kind in sorted(ALL_SOURCES):
            s, _, _ = run(ALL_SOURCES[kind]())
            snap = s.snapshot()
            assert snap["latency_ms"] >= 0 and snap["latency_floor_ms"] >= 0, kind

    def test_it_is_cheap_enough_for_fifty_cameras(self):
        # 50 cameras x 25 FPS = 1250 frames/s through the clock. Budget: under
        # 5% of one core, i.e. 40 microseconds a frame on a slow machine.
        import time
        seq = S.restamped_lossy(frames=5000)
        s = FrameSampler(5.0, S.FPS)
        t0 = time.perf_counter()
        for pts, a, _ in seq:
            s.consider(pts, a)
        per_frame = (time.perf_counter() - t0) / len(seq)
        assert per_frame < 40e-6, f"{per_frame * 1e6:.1f} us per frame"
