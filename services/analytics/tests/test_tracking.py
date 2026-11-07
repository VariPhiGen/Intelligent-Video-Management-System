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
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=0.0)
    t.update("cam", [det(500, 600)], 0.0)
    t.update("cam", [], 5.0)
    assert len(t.drain_expired()) == 1
    assert t.drain_expired() == [], "expired ids were reported twice"


def test_a_retirement_is_announced_only_once_the_identity_cannot_come_back():
    # Announcing a retirement is what makes a consumer discard this object's
    # activity state, so it waits until re-association can no longer save it.
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
    t.update("cam", [det(500, 600)], 0.0)
    t.update("cam", [], 5.0)
    assert t.drain_expired() == [], "announced while the identity could still return"
    t.update("cam", [], 6.0)
    assert t.drain_expired() == []
    t.update("cam", [], 7.5)
    assert len(t.drain_expired()) == 1
    assert t.drain_expired() == []


def test_a_revived_identity_is_never_announced_as_retired():
    """The bug this pins: the retirement was queued, the identity came back,
    and the queued announcement went out anyway.

    Announcing a retirement makes every consumer discard what it knows about
    that id. Sent for a track that is alive again, it wiped a Restricted Zone
    stay and its cooldown for a person still standing in the zone - who then
    raised a second violation on the next frame. Measured on the live
    appliance: cam4 track :1022 raised at 06:14:57 and again at 06:15:26, and
    :1049 19 s apart, both inside a 60 s cooldown.
    """
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
    first = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
    t.update("cam", [], 1.5)                      # retired, announcement held
    back = t.update("cam", [det(503, 601)], 2.0)[0].track.track_id
    assert back == first, "precondition: the identity was revived"
    for ts in (2.5, 3.0, 3.5, 4.0, 5.0):          # well past the window, still in view
        t.update("cam", [det(503, 601)], ts)
        assert first not in t.drain_expired(), "announced a live track as retired"
    snap = t.snapshot()
    assert snap["retirements_pending"] == 0
    assert snap["retirements_cancelled"] == snap["reassociations"] == 1


def test_a_revived_identity_that_dies_again_is_announced_once():
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
    first = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
    t.update("cam", [], 1.5)
    t.update("cam", [det(503, 601)], 2.0)         # revived
    t.update("cam", [], 3.5)                      # and gone for good
    announced = []
    for ts in (4.0, 6.0, 8.0):
        t.update("cam", [], ts)
        announced += t.drain_expired()
    assert announced == [first]


def test_one_cameras_clock_never_retires_anothers_tracks():
    """The second way a live track was announced as retired.

    One tracker serves every camera, and each camera's frames carry its OWN
    content time - which differ by seconds, because a camera catching up after
    a stall runs behind the rest. The announcement window was judged against
    whichever camera updated last, so a lagging camera's retirements went out
    early and the track was revived after its state had been wiped. Measured
    live: 82 revivals, only 61 of which still had an announcement to cancel.
    """
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
    first = t.update("slow", [det(500, 600)], 1000.0)[0].track.track_id
    t.update("slow", [], 1001.5)                  # retired on its own clock
    t.update("fast", [det(100, 600)], 1010.0)     # another camera, seconds ahead
    assert first not in t.drain_expired(), "announced on another camera's clock"
    back = t.update("slow", [det(503, 601)], 1002.0)[0].track.track_id
    assert back == first
    snap = t.snapshot()
    assert snap["revivals_already_announced"] == 0
    assert snap["retirements_cancelled"] == snap["reassociations"] == 1


def test_a_camera_that_stops_updating_still_has_its_retirements_announced():
    # Judged on its own clock, a camera that went quiet would hold its
    # retirements forever; far enough behind every other camera, they go.
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
    first = t.update("gone", [det(500, 600)], 1000.0)[0].track.track_id
    t.update("gone", [], 1001.5)                  # retired, then silence
    t.update("other", [det(100, 600)], 1100.0)
    assert first in t.drain_expired()


def test_a_reconnect_announces_everything_immediately():
    t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
    t.update("cam", [det(500, 600)], 0.0)
    t.update("cam", [], 5.0)                      # retires, held
    t.reset("cam")
    assert len(t.drain_expired()) == 1, "identity is gone across a reconnect"


class TestReassociation:
    """A track that dies and comes back in the same place is the same object."""

    def test_an_object_that_reappears_in_place_keeps_its_identity(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        first = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
        t.update("cam", [], 2.5)                  # older than max_age: retired
        again = t.update("cam", [det(505, 602)], 3.0)[0]
        assert again.track.track_id == first
        assert again.is_new is False, "a revived object is not a new sighting"
        assert t.snapshot()["reassociations"] == 1

    def test_it_keeps_its_age_and_its_confirmation(self):
        t = tracker(max_age_seconds=1.0, confirm_after_seconds=1.0,
                    reassociate_within_seconds=2.0)
        t.update("cam", [det(500, 600)], 0.0)
        t.update("cam", [det(500, 600)], 1.0)     # confirmed here
        t.update("cam", [], 2.5)
        back = t.update("cam", [det(502, 601)], 3.0)[0].track
        assert back.confirmed, "a known object must not go back to tentative"
        assert back.first_seen == 0.0

    def test_beyond_the_window_it_is_a_new_object(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        first = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
        t.update("cam", [], 2.5)
        later = t.update("cam", [det(500, 600)], 20.0)[0].track.track_id
        assert later != first

    def test_an_object_appearing_somewhere_else_is_not_the_same_object(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        first = t.update("cam", [det(200, 600)], 0.0)[0].track.track_id
        t.update("cam", [], 2.5)
        other = t.update("cam", [det(1500, 600)], 3.0)[0].track.track_id
        assert other != first
        assert t.snapshot()["reassociations"] == 0

    def test_a_near_miss_is_counted_as_a_probable_id_switch(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        t.update("cam", [det(500, 600)], 0.0)
        t.update("cam", [], 2.5)
        t.update("cam", [det(640, 600)], 3.0)     # ~1.4 heights away: refused
        snap = t.snapshot()
        assert snap["reassociations"] == 0 and snap["id_switch_suspects"] == 1

    def test_two_people_in_one_frame_never_share_an_identity(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        t.update("cam", [det(500, 600)], 0.0)
        t.update("cam", [], 2.5)
        out = t.update("cam", [det(500, 600), det(520, 604)], 3.0)
        assert len({o.track.track_id for o in out}) == 2

    def test_a_vehicle_never_inherits_a_person(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        person = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
        t.update("cam", [], 2.5)
        car = t.update("cam", [det(500, 600, domain=VEHICLE_DOMAIN)], 3.0)[0].track.track_id
        assert car != person

    def test_it_can_be_switched_off(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=0.0)
        first = t.update("cam", [det(500, 600)], 0.0)[0].track.track_id
        t.update("cam", [], 2.5)
        assert t.update("cam", [det(500, 600)], 3.0)[0].track.track_id != first


class TestInstrumentation:
    def test_the_snapshot_reports_identity_health(self):
        t = tracker(max_age_seconds=1.0, reassociate_within_seconds=2.0)
        t.update("cam", [det(500, 600)], 0.0)
        t.update("cam", [det(505, 601)], 0.4)
        t.update("cam", [], 2.5)
        snap = t.snapshot()
        for key in ("reassociations", "id_switch_suspects", "track_life_p50_s",
                    "track_life_p95_s", "tracks_measured", "lost_now",
                    "longest_unseen_s", "identities_remembered",
                    "retirements_pending", "last_update_ts", "max_age_seconds"):
            assert key in snap, key
        assert snap["tracks_measured"] == 1 and snap["track_life_p50_s"] == 0.4
        assert snap["retirements_pending"] == 1 and snap["identities_remembered"] == 1

    def test_a_carried_track_is_reported_as_unseen(self):
        t = tracker(max_age_seconds=5.0, confirm_after_seconds=0.0,
                    reassociate_within_seconds=2.0)
        t.update("cam", [det(500, 600)], 0.0)
        t.update("cam", [], 2.0)
        snap = t.snapshot()
        assert snap["lost_now"] == 1 and snap["longest_unseen_s"] == 2.0


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
