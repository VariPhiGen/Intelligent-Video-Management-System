"""The timestamp contract against sequences RECORDED from the live appliance.

Five captures from 2026-09-18 (tests/fixtures/timestamps): the (source
timestamp, arrival) of every frame the broker would have grabbed, as the broker
saw them through the local relay. They are the real behaviours the generic
clock was built for - nothing here tells it which is which:

  cam1_native_jittery      a camera clock stamped upstream on arrival
                           (18% of steps impossible for 25 FPS)
  cam2_native_clean_holes  a clean camera clock over a lossy link (0% impossible
                           steps; ~50% of the video never arrives)
  cam4_restamped           timestamps rewritten to arrival at our FFmpeg hop,
                           everything delivered
  cam4_restamped_lossy     the same hop while only ~10 of 25 frames/s arrived
  cam5_restamped           the same hop, smoother delivery

Measured with the clock this replaced (anchor + pts, cam4/cam5 flagged by
camera-mgmt's restamp setting): cam1 up to +0.19 s and cam2 up to +0.65 s in the
future, cam2 jumping 5.3 s at a re-anchor, cam4 lossy 4.07 FPS.

Run (from services/frames): python -m pytest tests -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broker.clock import PACED, SOURCE  # noqa: E402
from broker.sampling import FrameSampler  # noqa: E402
import sources as S  # noqa: E402


def replay(name):
    """As the broker does it: one sampler per capture session."""
    rows = S.recorded(name)
    every, published, samplers, sess, s = [], [], [], None, None
    for pts, arrival, session in rows:
        if session != sess:
            s, sess = FrameSampler(5.0, 25.0), session
            samplers.append(s)
        ts = s.consider(pts, arrival)
        every.append((s.clock._t, arrival))
        if ts is not None:
            published.append((ts, arrival))
    return rows, every, published, samplers


def steady(rows, published, skip=3.0):
    t0 = rows[0][1] + skip
    out = [x for x in published if x[1] >= t0]
    wall = rows[-1][1] - t0
    g = [b[0] - a[0] for a, b in zip(out, out[1:])]
    return len(out) / wall, g


@pytest.mark.parametrize("name", S.RECORDED)
def test_the_contract_holds_on_every_recording(name):
    _, every, published, _ = replay(name)
    S.assert_contract(every, published)


@pytest.mark.parametrize("name,expected", [
    ("cam1_native_jittery", PACED), ("cam2_native_clean_holes", SOURCE),
    ("cam4_restamped", PACED), ("cam4_restamped_lossy", PACED), ("cam5_restamped", PACED)])
def test_each_stream_gets_the_discipline_its_own_timestamps_call_for(name, expected):
    _, _, _, samplers = replay(name)
    assert samplers[-1].mode == expected


@pytest.mark.parametrize("name", ["cam1_native_jittery", "cam4_restamped", "cam5_restamped"])
def test_a_fully_delivered_stream_publishes_five_evenly_spaced_frames_a_second(name):
    rows, _, published, _ = replay(name)
    rate, g = steady(rows, published)
    assert rate >= 4.95, f"{rate:.2f} FPS"
    assert sum(1 for x in g if 0.15 <= x <= 0.25) / len(g) >= 0.99


def test_a_clean_clock_keeps_its_holes_and_never_jumps_like_a_reanchor():
    rows, _, published, _ = replay("cam2_native_clean_holes")
    rate, g = steady(rows, published)
    assert 2.5 <= rate <= 3.2, "only as many frames as actually arrived"
    assert max(g) < 3.0, "the old clock jumped 5.3 s re-anchoring"


def test_the_lossy_hop_publishes_more_than_the_old_clock_did():
    rows, _, published, _ = replay("cam4_restamped_lossy")
    rate, _ = steady(rows, published)
    assert rate > 4.07, f"{rate:.2f} FPS"


@pytest.mark.parametrize("name", S.RECORDED)
def test_no_published_frame_is_in_the_future(name):
    _, _, published, _ = replay(name)
    assert max(ts - a for ts, a in published) <= 0.0
