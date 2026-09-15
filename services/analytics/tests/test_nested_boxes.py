"""One object must not become two.

The regression these pin is a real pair of stored rows: cam1-h5e0 at
2026-09-09 19:48:40.075 produced 41577 (a full body, 102x232 at x1=805) and
41578 (the torso cut out of it, 86x132 at the same x1), and both were indexed
as separate objects. Everything downstream had already lost by then — the
tracker gave them different identities by invariant and CLIP dedup scored them
0.7698 apart because a torso and a whole body genuinely differ.
"""
import os
import sys

# Every test file here puts the service root on the path itself. This one
# didn't, so it passed only when an earlier file had done it, and failed with
# ModuleNotFoundError when run on its own.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.config import AppConfig, DetectConfig                    # noqa: E402
from analytics.detector import Detection, PERSON_DOMAIN, VEHICLE_DOMAIN  # noqa: E402
from analytics.nested_boxes import containment, suppress_nested          # noqa: E402

CONFIG_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")


def det(x1, y1, x2, y2, domain=PERSON_DOMAIN, conf=0.5, label="person"):
    return Detection(domain=domain, xyxy=(x1, y1, x2, y2),
                     confidence=conf, label=label)


# ── the measure ─────────────────────────────────────────────────────────────
def test_a_box_fully_inside_another_is_fully_contained():
    assert containment((10, 10, 20, 20), (0, 0, 100, 100)) == 1.0


def test_containment_is_symmetric():
    a, b = (10, 10, 20, 20), (0, 0, 100, 100)
    assert containment(a, b) == containment(b, a)


def test_disjoint_boxes_contain_nothing():
    assert containment((0, 0, 10, 10), (50, 50, 60, 60)) == 0.0


def test_touching_boxes_contain_nothing():
    """Sharing an edge is not overlapping. Without this a person standing
    directly behind another would suppress at a zero-area intersection."""
    assert containment((0, 0, 10, 10), (10, 0, 20, 10)) == 0.0


def test_containment_is_not_iou():
    """THE WHOLE REASON THIS EXISTS. The real 41577/41578 geometry: nested, so
    containment is 1.0, while IoU is 0.48 — under the 0.7 NMS default, which is
    why ultralytics let both through."""
    body = (805, 283, 907, 515)          # 102 x 232
    torso = (805, 287, 891, 419)         # 86 x 132, inside it
    assert containment(torso, body) == 1.0
    inter = 86 * 132
    union = 102 * 232 + 86 * 132 - inter
    assert round(inter / union, 2) == 0.48


# ── the suppression ─────────────────────────────────────────────────────────
def test_the_torso_is_dropped_and_the_body_kept():
    body = det(805, 283, 907, 515, conf=0.425)
    torso = det(805, 287, 891, 419, conf=0.368)
    assert suppress_nested([body, torso], 0.9) == [body]


def test_order_of_the_input_does_not_change_the_outcome():
    body = det(805, 283, 907, 515, conf=0.425)
    torso = det(805, 287, 891, 419, conf=0.368)
    assert suppress_nested([torso, body], 0.9) == [body]


def test_the_larger_box_wins_even_when_the_smaller_is_more_confident():
    """Measured on rows 31441/31442 — one man photographed to the waist at
    confidence 0.65 and to the ankles at 0.51. Ranking on confidence would keep
    the truncated crop, and every consumer downstream wants the whole object:
    CLIP embeds a person rather than a torso, ANPR ranks on legibility, and an
    operator reviewing a hit wants to see who it was."""
    waist = det(0, 0, 170, 303, conf=0.65)
    whole = det(1, 0, 170, 392, conf=0.51)
    assert suppress_nested([waist, whole], 0.9) == [whole]


def test_two_people_side_by_side_both_survive():
    a = det(0, 0, 100, 300)
    b = det(120, 0, 220, 300)
    assert suppress_nested([a, b], 0.9) == [a, b]


def test_partly_overlapping_people_both_survive():
    """Occlusion is normal and must never cost an identity. These overlap
    heavily but neither is inside the other."""
    a = det(0, 0, 100, 300)
    b = det(60, 0, 160, 300)
    assert len(suppress_nested([a, b], 0.9)) == 2


def test_a_person_inside_a_vehicle_is_not_suppressed():
    """THE RULE THAT MAY NOT BE RELAXED. A passenger in a doorway is
    legitimately inside the bus's box. Suppressing across domains would delete
    the person and keep the vehicle — a person no search can return."""
    bus = det(0, 0, 400, 300, domain=VEHICLE_DOMAIN, label="bus")
    passenger = det(100, 100, 160, 260, domain=PERSON_DOMAIN)
    assert suppress_nested([bus, passenger], 0.9) == [bus, passenger]


def test_a_chain_of_three_collapses_to_the_outermost():
    outer = det(0, 0, 200, 400)
    middle = det(10, 10, 180, 300)
    inner = det(20, 20, 100, 200)
    assert suppress_nested([outer, middle, inner], 0.9) == [outer]


def test_survivors_keep_the_input_order():
    """Nothing downstream may come to depend on this having reordered
    anything — the tracker's greedy matcher is order-sensitive.

    The boxes differ in size and arrive small, large, medium, because the
    function works largest-first internally. With equal sizes, input order and
    size order are the same list, so returning survivors in size order would
    have passed."""
    small = det(0, 0, 50, 100)
    large = det(200, 0, 350, 300)
    medium = det(400, 0, 500, 200)
    assert suppress_nested([small, large, medium], 0.9) == [small, large, medium]


# ── the switch ──────────────────────────────────────────────────────────────
def test_zero_disables_it():
    body = det(805, 283, 907, 515)
    torso = det(805, 287, 891, 419)
    assert len(suppress_nested([body, torso], 0.0)) == 2


def test_a_single_detection_is_returned_untouched():
    only = det(0, 0, 100, 300)
    assert suppress_nested([only], 0.9) == [only]


def test_nothing_in_nothing_out():
    assert suppress_nested([], 0.9) == []


def test_a_threshold_above_the_pair_keeps_both():
    """Sub-threshold containment is left alone, so raising the threshold is a
    real way to make this more conservative rather than a no-op."""
    outer = det(0, 0, 100, 400)
    inner = det(0, 0, 100, 200)          # exactly half inside → containment 1.0
    partly = det(0, 300, 100, 500)       # 100/200 of itself inside → 0.5
    assert suppress_nested([outer, partly], 0.9) == [outer, partly]
    assert suppress_nested([outer, inner], 0.9) == [outer]


def test_containment_exactly_at_the_threshold_is_suppressed():
    """The threshold is inclusive: 900 of the inner box's 1,000 px lie inside."""
    outer = det(0, 0, 100, 400)
    inner = det(0, 310, 10, 410)         # 10 x 100, 90 rows inside → 0.9
    assert containment(inner.xyxy, outer.xyxy) == 0.9
    assert suppress_nested([outer, inner], 0.9) == [outer]


def test_containment_just_under_the_threshold_is_kept():
    outer = det(0, 0, 100, 400)
    inner = det(0, 311, 10, 411)         # 89 rows inside → 0.89
    assert suppress_nested([outer, inner], 0.9) == [outer, inner]


def test_the_shipped_threshold_is_the_code_default(monkeypatch):
    """config.yaml and DetectConfig each say 0.9. Two copies of one number is
    how a default drifts without anyone deciding to change it."""
    monkeypatch.delenv("ANALYTICS_NESTED_CONTAINMENT", raising=False)
    shipped = AppConfig.from_yaml(CONFIG_YAML).detect.nested_containment
    assert shipped == DetectConfig().nested_containment


def test_the_environment_switch_turns_it_off(monkeypatch):
    """The documented no-rebuild off switch, as the loader reads it."""
    monkeypatch.setenv("ANALYTICS_NESTED_CONTAINMENT", "0")
    assert AppConfig.from_yaml(CONFIG_YAML).detect.nested_containment == 0.0
