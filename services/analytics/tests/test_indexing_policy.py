"""Which observations of a known object earn another row.

THE INTERVAL FLOOR is the newest rule and the one these pin hardest. It is what
stops one object reaching Smart Search — and the Recent-detections feed built on
it — five times in ten seconds, which tracker 3721 on cam3 did on 2026-09-11
before it existed.

The change-driven rules predate it. Their basic contract is pinned here too,
because this module moved into analytics without a suite of its own.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.config import AppConfig                                  # noqa: E402
from analytics.indexing_policy import (DISPLACEMENT, FIRST_SIGHT,       # noqa: E402
                                       HEARTBEAT, QUALITY_GAIN,
                                       SUPPRESSION_OFF, IndexingPolicy)
from analytics.tracking import Track                                    # noqa: E402


def track(tid: str = "cam-a:n.0:1") -> Track:
    return Track(track_id=tid, domain="person", state="ACTIVE", first_seen=0.0,
                 last_seen=0.0, cx=0, by=0, w=0, h=0)


class Det:
    def __init__(self, x1, y1, x2, y2, conf=0.5) -> None:
        self.xyxy = (x1, y1, x2, y2)
        self.confidence = conf


def still(conf: float = 0.5, dx: int = 0) -> Det:
    """A 200 px tall person; `dx` moves them sideways."""
    return Det(100 + dx, 100, 150 + dx, 300, conf)


def step(policy: IndexingPolicy, t: Track, det: Det, ts: float):
    """What the pipeline does: ask, and move the anchor only if recorded."""
    reason = policy.decide(t, det, ts)
    if reason:
        policy.record(t, det, ts, reason)
    return reason


def floor(**kw) -> IndexingPolicy:
    return IndexingPolicy(min_interval_seconds=10, heartbeat_seconds=10, **kw)


# ── the interval floor ──────────────────────────────────────────────────────
def test_first_sight_is_never_delayed():
    assert step(floor(), track(), still(), 0.0) == FIRST_SIGHT


def test_a_staying_object_is_recorded_once_per_interval():
    """30 s at 5 FPS: records at 0, 10, 20 and 30 s — nothing in between."""
    p, t = floor(), track()
    got = [step(p, t, still(), i / 5) for i in range(151)]
    recorded = [r for r in got if r]
    assert recorded == [FIRST_SIGHT, HEARTBEAT, HEARTBEAT, HEARTBEAT]


def test_movement_inside_the_interval_adds_no_row():
    """The case that produced five cards: an object that keeps changing. With
    the floor it waits; without it, it is recorded at once — which proves the
    floor is what held it back."""
    p, t = floor(), track()
    step(p, t, still(), 0.0)
    assert step(p, t, still(dx=600), 2.0) is None      # 3 heights away, 2 s in
    assert step(p, t, still(dx=600), 10.0) is not None

    old, t2 = IndexingPolicy(heartbeat_seconds=0), track("cam-a:n.0:2")
    step(old, t2, still(), 0.0)
    assert step(old, t2, still(dx=600), 2.0) == DISPLACEMENT


def test_a_better_look_waits_for_the_interval_too():
    p, t = floor(), track()
    step(p, t, still(conf=0.40), 0.0)
    assert step(p, t, still(conf=0.95), 3.0) is None
    assert step(p, t, still(conf=0.95), 10.0) in (HEARTBEAT, QUALITY_GAIN)


def test_the_floor_is_per_track_so_a_new_object_is_never_held_back():
    p = floor()
    assert step(p, track("a"), still(), 0.0) == FIRST_SIGHT
    assert step(p, track("b"), still(dx=400), 0.5) == FIRST_SIGHT


def test_a_retired_track_starts_again_with_first_sight():
    p, t = floor(), track()
    step(p, t, still(), 0.0)
    p.forget([t.track_id])
    assert step(p, t, still(), 1.0) == FIRST_SIGHT


def test_zero_turns_the_floor_off():
    p, t = IndexingPolicy(min_interval_seconds=0, heartbeat_seconds=0), track()
    step(p, t, still(), 0.0)
    assert step(p, t, still(dx=600), 1.0) == DISPLACEMENT


def test_suppression_off_still_bypasses_everything():
    p, t = floor(suppression_enabled=False), track()
    assert step(p, t, still(), 0.0) == SUPPRESSION_OFF
    assert step(p, t, still(), 0.2) == SUPPRESSION_OFF


def test_the_floor_is_reported():
    assert floor().snapshot()["min_interval_seconds"] == 10


def test_the_shipped_config_is_the_operators_rule():
    """Pins the decision, not just the mechanism: shipped as first sight, then
    one look every 10 s while the object stays."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = AppConfig.from_yaml(os.path.join(here, "config.yaml"))
    assert cfg.index_policy.min_interval_seconds == 10
    assert cfg.index_policy.heartbeat_seconds == 10
