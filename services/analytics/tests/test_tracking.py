"""The object tracker: is this the same physical object as before?

WHAT THESE PROTECT. The tracker's value is a stable identity while an object is
visible; its risk is claiming two different objects are one. For forensic
search a merged identity is the worse failure — it is a person who never comes
back as a hit — so the tests that matter most are the ones asserting that
distinct objects stay distinct.

Several cases here are inherited verbatim from the temporal gate this replaced:
one-to-one assignment, cameras not sharing state, person and vehicle not
sharing state, bounded state. That logic was measured on 5,079 real detections
and did not change when it became a tracker, so neither did its tests.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.detector import Detection, PERSON_DOMAIN, VEHICLE_DOMAIN  # noqa: E402
from analytics.tracking import (ACTIVE, CONFIRMED, LOST, STATIONARY,      # noqa: E402
                            TENTATIVE, ObjectTracker)


def det(cx: float, by: float, h: float = 100.0, domain: str = PERSON_DOMAIN,
        conf: float = 0.9) -> Detection:
    """A detection at a bottom-centre point, in pixels."""
    w = h / 2
    return Detection(domain=domain,
                     xyxy=(int(cx - w / 2), int(by - h),
                           int(cx + w / 2), int(by)),
                     confidence=conf, label=domain)


def tracker(**kw) -> ObjectTracker:
    opts = dict(max_association_cost=1.75, velocity_alpha=0.5,
                confirm_after_seconds=1.0, max_age_seconds=3.0,
                stationary_after_seconds=4.0, stationary_distance=0.15)
    opts.update(kw)
    return ObjectTracker(**opts)


def ids(results):
    return [r.track.track_id for r in results]


# ── A. one person standing still ─────────────────────────────────────────────
def test_a_stationary_person_keeps_one_identity():
    t = tracker()
    first = t.update("cam", [det(500, 600)], 0.0)
    tid = first[0].track.track_id
    for i in range(1, 30):                      # 15 s at 2 FPS
        out = t.update("cam", [det(500 + (i % 2), 600)], i * 0.5)
        assert ids(out) == [tid], "a motionless person fragmented into new tracks"


def test_a_stationary_person_becomes_STATIONARY():
    t = tracker(stationary_after_seconds=4.0)
    t.update("cam", [det(500, 600)], 0.0)
    for i in range(1, 20):
        out = t.update("cam", [det(500, 600)], i * 0.5)
    assert out[0].track.state == STATIONARY


# ── B. one person walking ────────────────────────────────────────────────────
def test_a_walking_person_keeps_one_identity():
    """0.5 object heights per sample — a normal walk at 2 FPS."""
    t = tracker()
    out = t.update("cam", [det(200, 600)], 0.0)
    tid = out[0].track.track_id
    for i in range(1, 20):
        out = t.update("cam", [det(200 + i * 50, 600)], i * 0.5)
        assert ids(out) == [tid], f"identity broke at step {i}"


def test_velocity_is_learned_and_used():
    """Prediction should make a moving object CHEAPER to associate, not just
    possible. Without it a fast walker eventually exceeds the threshold."""
    t = tracker(velocity_alpha=0.9)
    t.update("cam", [det(200, 600)], 0.0)
    for i in range(1, 6):
        t.update("cam", [det(200 + i * 60, 600)], i * 0.5)
    track = t.update("cam", [det(200 + 6 * 60, 600)], 3.0)[0].track
    assert track.vx > 0, "velocity was never learned"
    px, _py = track.predict(3.5)
    assert px > track.cx, "prediction does not lead a moving object"


# ── C/D. distinct objects must stay distinct ─────────────────────────────────
def test_two_people_in_one_frame_are_two_tracks():
    t = tracker()
    out = t.update("cam", [det(300, 600), det(900, 600)], 0.0)
    assert len(set(ids(out))) == 2


def test_one_track_can_never_absorb_two_detections():
    """THE INVARIANT, inherited from the temporal gate. Two detections in one
    frame are certainly two objects; letting one track claim both would merge
    two identities, which is the failure that made ByteTrack unusable here."""
    t = tracker(max_association_cost=99.0)      # everything is "close enough"
    t.update("cam", [det(500, 600)], 0.0)
    out = t.update("cam", [det(505, 600), det(515, 600)], 0.5)
    assert len(set(ids(out))) == 2, "one track absorbed two detections"


def test_two_vehicles_close_together_stay_apart():
    t = tracker()
    out = t.update("cam", [det(400, 700, h=200, domain=VEHICLE_DOMAIN),
                           det(700, 700, h=200, domain=VEHICLE_DOMAIN)], 0.0)
    assert len(set(ids(out))) == 2
    out = t.update("cam", [det(410, 700, h=200, domain=VEHICLE_DOMAIN),
                           det(710, 700, h=200, domain=VEHICLE_DOMAIN)], 0.5)
    assert len(set(ids(out))) == 2


def test_size_change_is_part_of_the_distance():
    """A passing car and a parked car can share a centre; they cannot share a
    size trajectory. This is Frigate's refinement over plain centre distance."""
    t = tracker()
    t.update("cam", [det(500, 600, h=100, domain=VEHICLE_DOMAIN)], 0.0)
    # Same place, four times the height: not a plausible continuation.
    out = t.update("cam", [det(500, 600, h=400, domain=VEHICLE_DOMAIN)], 0.5)
    assert out[0].is_new, "a 4x size jump was accepted as the same object"


# ── E. crossing people — measured, not asserted ──────────────────────────────
def test_crossing_people_id_switch_rate_is_reported():
    """Two people walking THROUGH each other. We do not assert zero switches —
    geometry alone cannot always tell who emerged on which side, and claiming
    otherwise would be a test that lies. We assert the tracker keeps exactly
    two identities and REPORT the switch count so a tuning change that makes it
    worse is visible."""
    t = tracker()
    seen: list[tuple[str, str]] = []
    for i in range(21):
        ts = i * 0.5
        a_x, b_x = 300 + i * 30, 900 - i * 30      # cross at i = 10
        out = t.update("cam", [det(a_x, 600), det(b_x, 600)], ts)
        assert len(set(ids(out))) == 2, f"lost an identity at step {i}"
        by_x = sorted(((r.detection.xyxy[0], r.track.track_id) for r in out))
        seen.append((by_x[0][1], by_x[1][1]))

    before, after = seen[0], seen[-1]
    switched = before != after
    # Informational: printed by pytest -s, and the number a sweep would track.
    print(f"\n  crossing: left/right identities {before} -> {after} "
          f"(switched={switched})")
    assert len({i for pair in seen for i in pair}) <= 4, \
        "crossing produced an ID explosion"


# ── F. occlusion ─────────────────────────────────────────────────────────────
def test_a_track_survives_a_short_miss():
    t = tracker(max_age_seconds=3.0)
    tid = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
    t.update("cam", [det(520, 600)], 0.5)
    for ts in (1.0, 1.5, 2.0):                  # occluded
        t.update("cam", [], ts)
    out = t.update("cam", [det(560, 600)], 2.5)
    assert ids(out) == [tid], "a 1.5 s occlusion broke identity"


def test_a_long_absence_expires_rather_than_reassociating():
    """Short expiry on purpose: a wrong re-association merges two people, and
    for search that is worse than an extra track."""
    t = tracker(max_age_seconds=3.0)
    tid = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
    for i in range(1, 12):
        t.update("cam", [], i * 0.5)            # 5.5 s of nothing
    out = t.update("cam", [det(500, 600)], 6.0)
    assert ids(out) != [tid]
    assert out[0].is_new


# ── G. leaves and returns ────────────────────────────────────────────────────
def test_leaving_and_returning_creates_a_new_track():
    t = tracker(max_age_seconds=3.0)
    first = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
    for i in range(1, 10):
        t.update("cam", [], i * 0.5)
    second = t.update("cam", [det(500, 600)], 30.0)[0].track.track_id
    assert first != second, "re-associated across a 30 s absence"
    # CLIP dedup is the backstop that decides whether this earns a second row.


# ── isolation ────────────────────────────────────────────────────────────────
def test_cameras_do_not_share_identity():
    t = tracker()
    a = t.update("cam-a", [det(500, 600)], 0.0)[0].track.track_id
    b = t.update("cam-b", [det(500, 600)], 0.1)[0].track.track_id
    assert a != b


def test_person_and_vehicle_do_not_share_identity():
    t = tracker()
    out = t.update("cam", [det(500, 600, domain=PERSON_DOMAIN),
                           det(505, 600, domain=VEHICLE_DOMAIN)], 0.0)
    assert len(set(ids(out))) == 2
    assert {r.track.domain for r in out} == {PERSON_DOMAIN, VEHICLE_DOMAIN}


# ── M/N. reconnect and hibernation ───────────────────────────────────────────
def test_reconnect_resets_identity_and_cannot_reuse_ids():
    t = tracker()
    before = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
    t.reset("cam")
    after = t.update("cam", [det(500, 600)], 0.5)[0].track.track_id
    assert before != after, "identity survived a reconnect"
    assert t.drain_expired(), "the retired track was not reported for pruning"


def test_reconnect_on_one_camera_leaves_the_other_alone():
    t = tracker()
    a = t.update("cam-a", [det(500, 600)], 0.0)[0].track.track_id
    b = t.update("cam-b", [det(500, 600)], 0.0)[0].track.track_id
    t.reset("cam-a")
    assert t.update("cam-a", [det(500, 600)], 0.5)[0].track.track_id != a
    assert t.update("cam-b", [det(500, 600)], 0.5)[0].track.track_id == b


def test_ids_from_separate_tracker_instances_never_collide():
    """Hibernation drops the pipeline and builds a new tracker on wake. A
    stored tracker_id must not mean two different objects either side of that."""
    a = tracker().update("cam", [det(500, 600)], 0.0)[0].track.track_id
    b = tracker().update("cam", [det(500, 600)], 0.0)[0].track.track_id
    assert a != b, "two tracker instances minted the same id"


# ── J. bounds ────────────────────────────────────────────────────────────────
def test_state_is_bounded_under_a_flood():
    t = tracker(max_tracks_per_key=16, max_age_seconds=600.0)
    for i in range(200):
        t.update("cam", [det(i * 40 + 20, 600, h=20)], i * 0.5)
    assert t.live_tracks() <= 16


def test_expired_tracks_are_reported_exactly_once():
    t = tracker(max_age_seconds=1.0)
    t.update("cam", [det(500, 600)], 0.0)
    t.update("cam", [], 5.0)
    assert len(t.drain_expired()) == 1
    assert t.drain_expired() == [], "expired ids were reported twice"


# ── lifecycle ────────────────────────────────────────────────────────────────
def test_a_track_starts_tentative_and_is_confirmed_by_persistence():
    t = tracker(confirm_after_seconds=1.0)
    out = t.update("cam", [det(500, 600)], 0.0)
    assert out[0].track.state == TENTATIVE
    assert not out[0].track.confirmed
    out = t.update("cam", [det(505, 600)], 0.5)
    assert out[0].track.state == TENTATIVE, "confirmed before confirm_after"
    out = t.update("cam", [det(510, 600)], 1.0)
    assert out[0].track.state == CONFIRMED
    assert out[0].track.confirmed


def test_a_missed_detection_marks_the_track_lost_not_gone():
    t = tracker(max_age_seconds=3.0)
    for i in range(4):
        t.update("cam", [det(500, 600)], i * 0.5)
    t.update("cam", [], 2.0)
    out = t.update("cam", [det(500, 600)], 2.5)
    assert out[0].track.hits >= 5, "the track was recreated rather than resumed"


def test_a_moving_track_is_active_not_stationary():
    t = tracker()
    for i in range(20):
        out = t.update("cam", [det(200 + i * 40, 600)], i * 0.5)
    assert out[0].track.state == ACTIVE


def test_disabling_the_tracker_still_returns_every_detection():
    """Identity off must not mean detections vanish — that would be a silent
    recall failure rather than a disabled feature."""
    t = ObjectTracker(enabled=False)
    out = t.update("cam", [det(300, 600), det(900, 600)], 0.0)
    assert len(out) == 2
    assert all(r.track.confirmed for r in out)


def test_an_empty_frame_is_harmless():
    assert tracker().update("cam", [], 0.0) == []


def test_counters_reconcile():
    t = tracker()
    t.update("cam", [det(300, 600), det(900, 600)], 0.0)
    t.update("cam", [det(305, 600), det(905, 600)], 0.5)
    snap = t.snapshot()
    assert snap["tracks_created"] == 2
    assert snap["associations"] == 2
    assert snap["live_tracks"] == 2
