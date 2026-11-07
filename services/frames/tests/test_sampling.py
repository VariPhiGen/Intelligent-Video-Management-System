"""The sampler: which frames to publish - the configured rate of VIDEO, chosen
on the normalized time broker/clock.py gives every frame (its own tests are in
test_clock.py).

The bug these pin: frames arrive in bursts, and a wall-clock gate publishes one
frame per burst. Measured on this appliance, that turned a 5 FPS target into
2.5-3.3 FPS.

Run (from services/frames): python -m pytest tests -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broker.sampling import FrameSampler  # noqa: E402
import sources as S  # noqa: E402


def published(sampler, seq):
    return [ts for pts, a, _ in seq if (ts := sampler.consider(pts, a)) is not None]


class TestTheRate:
    def test_video_held_up_and_then_flushed_is_published_at_the_rate(self):
        # The failure case: a second of video held by the network, then
        # delivered inside a few milliseconds. A wall-clock gate got one frame.
        out = published(FrameSampler(5.0, 25.0), S.native_stalls(seconds=30, every=2.0, stall=1.0))
        assert 148 <= len(out) <= 151, len(out)

    def test_the_backlog_read_while_opening_is_published_at_the_rate(self):
        # FFmpeg reads ahead while it opens; that second arrives in 25 ms.
        live = S.native(seconds=10)
        seq = [(p, S.START + 0.001 * k if k < 25 else a, c) for k, (p, a, c) in enumerate(live)]
        out = published(FrameSampler(5.0, 25.0, open_seconds=1.0), seq)
        assert sum(1 for ts in out if ts < S.START + 1.0) >= 4

    def test_real_time_arrival_publishes_the_rate(self):
        assert 49 <= len(published(FrameSampler(5.0, 25.0), S.native(seconds=10))) <= 51

    def test_the_first_frame_is_always_published(self):
        assert FrameSampler(5.0, 25.0).consider(0.0, 1000.0) == 1000.0

    def test_frames_landing_past_their_deadline_do_not_drag_the_rate_down(self):
        # One of this appliance's sources delivers distinct frames ~52 ms apart,
        # so a frame almost always lands PAST its 200 ms deadline. Advancing
        # from the frame instead of the deadline cost ~12% of the rate in
        # production (4.4 FPS against a 5 FPS target).
        s = FrameSampler(5.0, 19.2)
        out = [ts for i in range(600) if (ts := s.consider(i * 52.0, 1000.0 + i * 0.052)) is not None]
        rate = len(out) / (600 * 0.052)
        assert 4.9 <= rate <= 5.1, f"{rate:.2f} FPS"

    def test_a_different_target_rate_is_honoured(self):
        assert len(published(FrameSampler(1.0, 25.0), S.native(seconds=4))) == 4
        assert len(published(FrameSampler(2.0, 25.0), S.native(seconds=4))) == 8


class TestGapsAndSessions:
    def test_a_gap_in_the_stream_does_not_produce_a_catch_up_burst(self):
        s = FrameSampler(5.0, 25.0)
        s.consider(0.0, 1000.0)
        # three seconds of video missing, then the stream resumes
        assert s.consider(3000.0, 1003.0) is not None
        assert s.consider(3040.0, 1003.04) is None, "resumes at the sample rate"
        assert s.consider(3200.0, 1003.2) is not None

    def test_each_session_starts_clean(self):
        # A sampler is built per session, so nothing carries across a reconnect.
        first = FrameSampler(5.0, 25.0)
        first.consider(10_000.0, 1000.0)
        assert FrameSampler(5.0, 25.0).consider(0.0, 2000.0) == 2000.0

    def test_published_timestamps_strictly_increase(self):
        for seq in (S.restamped(), S.native_holes(), S.missing(), S.wrapped()):
            out = published(FrameSampler(5.0, 25.0), seq)
            assert all(b > a for a, b in zip(out, out[1:]))


class TestObservability:
    def test_the_snapshot_says_how_sampling_is_working(self):
        s = FrameSampler(5.0, 25.0)
        published(s, S.native(seconds=5))
        snap = s.snapshot()
        assert snap["sample_fps"] == 5.0 and snap["clock"] == "source"
        assert snap["published"] + snap["skipped"] == 125
        assert 24 <= snap["published"] <= 26
