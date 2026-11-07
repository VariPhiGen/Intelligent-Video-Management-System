"""Rule predicate logic — what a rule will and will not match.

These are pure functions with no database and no clock of their own: `now` is
always a parameter. A rule that opens a gate is only as safe as the predicate
deciding it should, so every branch here has a test, including the ones that
exist to say "no".

Run (from services/camera-mgmt): python -m pytest tests/test_rules_logic.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import rules  # noqa: E402


def ev(**kw):
    base = {"activity": "unauthorised_access", "zone": "Zone 1", "confidence": 0.9,
            "object_class": "person", "track_id": "7", "attributes": {}}
    base.update(kw)
    return base


def test_empty_conditions_match_everything():
    assert rules.matches_conditions({}, ev()) is True


def test_all_requires_every_clause():
    c = {"all": [{"field": "object_class", "op": "eq", "value": "vehicle"},
                 {"field": "attributes.plate", "op": "matches", "value": "^KA[0-9]{2}"}]}
    assert rules.matches_conditions(c, ev(object_class="vehicle",
                                          attributes={"plate": "KA05AB1234"})) is True
    assert rules.matches_conditions(c, ev(object_class="vehicle",
                                          attributes={"plate": "MH01XY9999"})) is False
    assert rules.matches_conditions(c, ev(object_class="person",
                                          attributes={"plate": "KA05AB1234"})) is False


def test_any_requires_one_clause():
    c = {"any": [{"field": "object_class", "op": "eq", "value": "vehicle"},
                 {"field": "object_class", "op": "eq", "value": "person"}]}
    assert rules.matches_conditions(c, ev(object_class="person")) is True
    assert rules.matches_conditions(c, ev(object_class="animal")) is False


def test_missing_field_is_false_not_an_error():
    c = {"all": [{"field": "attributes.ppe_helmet", "op": "eq", "value": False}]}
    assert rules.matches_conditions(c, ev(attributes={})) is False


def test_exists_op_distinguishes_absent_from_falsy():
    c = {"all": [{"field": "attributes.ppe_helmet", "op": "exists", "value": True}]}
    assert rules.matches_conditions(c, ev(attributes={"ppe_helmet": False})) is True
    assert rules.matches_conditions(c, ev(attributes={})) is False


def test_matches_op_against_a_non_string_is_false_not_a_crash():
    """A plate field that arrives as a number must not raise TypeError inside
    the evaluator — a malformed detection cannot be allowed to 500 the ingest
    endpoint for the rest of its batch."""
    c = {"all": [{"field": "attributes.plate", "op": "matches", "value": "^KA"}]}
    assert rules.matches_conditions(c, ev(attributes={"plate": 1234})) is False


def test_unknown_op_is_false_not_an_error():
    c = {"all": [{"field": "object_class", "op": "sudo", "value": "person"}]}
    assert rules.matches_conditions(c, ev()) is False


def test_malformed_condition_is_false():
    assert rules.matches_conditions({"all": "not-a-list"}, ev()) is False
    assert rules.matches_conditions({"all": [{"nope": 1}]}, ev()) is False


@pytest.mark.parametrize("op,value,field_value,expected", [
    ("eq", 5, 5, True), ("ne", 5, 6, True),
    ("gt", 5, 6, True), ("gt", 5, 5, False),
    ("gte", 5, 5, True), ("lt", 5, 4, True), ("lte", 5, 5, True),
    ("in", ["a", "b"], "a", True), ("in", ["a", "b"], "c", False),
])
def test_leaf_ops(op, value, field_value, expected):
    c = {"all": [{"field": "attributes.x", "op": op, "value": value}]}
    assert rules.matches_conditions(c, ev(attributes={"x": field_value})) is expected


def test_comparison_against_incomparable_types_is_false():
    """gt between a string and an int raises in Python; it must not here."""
    c = {"all": [{"field": "attributes.x", "op": "gt", "value": 5}]}
    assert rules.matches_conditions(c, ev(attributes={"x": "big"})) is False


# ── Active hours ──────────────────────────────────────────────────────────────

IST = "Asia/Kolkata"


def _at(y, m, d, hh, mm=0):
    """A UTC instant. The evaluator converts into the rule's timezone itself."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_null_active_hours_is_always_armed():
    assert rules.within_active_hours(None, _at(2026, 9, 18, 12), site_tz="UTC") is True


def test_simple_window_inside_and_outside():
    ah = {"windows": [["09:00", "17:00"]]}
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 12), site_tz="UTC") is True
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 20), site_tz="UTC") is False


def test_window_crossing_midnight_fires_at_0200_and_not_at_noon():
    """THE test. 22:00-06:00 means 22:00 today through 06:00 tomorrow. Testing
    `start <= t < end` instead of `t >= start or t < end` makes this window
    match nothing at all, silently."""
    ah = {"windows": [["22:00", "06:00"]]}
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 2), site_tz="UTC") is True
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 23), site_tz="UTC") is True
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 12), site_tz="UTC") is False
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 6), site_tz="UTC") is False


def test_window_is_evaluated_in_the_rules_timezone_not_utc():
    """18:30 UTC is 00:00 IST the next day. An operator writing 22:00-06:00
    means local night, and must get local night."""
    ah = {"tz": IST, "windows": [["22:00", "06:00"]]}
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 18, 30), site_tz="UTC") is True
    # 09:00 UTC == 14:30 IST, the middle of the afternoon.
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 9), site_tz="UTC") is False


def test_site_tz_is_used_when_the_rule_names_none():
    ah = {"windows": [["22:00", "06:00"]]}
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 18, 30), site_tz=IST) is True


def test_days_bind_to_the_day_the_window_opens():
    """A Friday-night window still fires at 02:00 on Saturday. Binding `days`
    to the instant rather than to the window's opening would stop the alarm
    halfway through the night it was armed for."""
    ah = {"windows": [["22:00", "06:00"]], "days": [4]}     # Friday only
    # 2026-09-18 is a Friday. 23:00 Friday: inside.
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 23), site_tz="UTC") is True
    # 02:00 Saturday belongs to Friday's window.
    assert rules.within_active_hours(ah, _at(2026, 9, 19, 2), site_tz="UTC") is True
    # 02:00 Friday belongs to THURSDAY's window, which is not armed.
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 2), site_tz="UTC") is False


def test_multiple_windows_are_ored():
    ah = {"windows": [["06:00", "09:00"], ["17:00", "20:00"]]}
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 7), site_tz="UTC") is True
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 18), site_tz="UTC") is True
    assert rules.within_active_hours(ah, _at(2026, 9, 18, 12), site_tz="UTC") is False


def test_malformed_window_is_not_armed():
    """A broken schedule must not silently mean 'always'. An operator who typed
    a bad time should get a rule that does not fire, not one that fires
    constantly."""
    assert rules.within_active_hours({"windows": [["25:00", "06:00"]]},
                                     _at(2026, 9, 18, 2), site_tz="UTC") is False
    assert rules.within_active_hours({"windows": "nope"},
                                     _at(2026, 9, 18, 2), site_tz="UTC") is False


def test_empty_windows_list_is_not_armed():
    assert rules.within_active_hours({"windows": []}, _at(2026, 9, 18, 2),
                                     site_tz="UTC") is False


def test_unknown_timezone_falls_back_to_not_armed():
    assert rules.within_active_hours({"tz": "Mars/Olympus", "windows": [["00:00", "23:59"]]},
                                     _at(2026, 9, 18, 2), site_tz="UTC") is False


# ── The gate chain ────────────────────────────────────────────────────────────

def rule(**kw):
    base = {"enabled": True, "mode": "armed", "activity": "unauthorised_access",
            "scope_kind": "any", "scope_value": None, "min_confidence": None,
            "conditions": {}, "max_event_age_s": 30, "active_hours": None}
    base.update(kw)
    return base


def event(**kw):
    base = {"activity": "unauthorised_access", "camera_slug": "cam2-o8iu",
            "zone": "Zone 1", "confidence": 0.9, "object_class": "person",
            "track_id": "7", "attributes": {}, "started_at": _at(2026, 9, 18, 12)}
    base.update(kw)
    return base


NOW = _at(2026, 9, 18, 12)


def test_a_matching_rule_fires_and_may_actuate():
    d = rules.should_fire(rule(), event(), NOW, site_tz="UTC")
    assert d.fires is True and d.reason == "fires" and d.actuate is True


def test_disabled_rule_never_fires():
    assert rules.should_fire(rule(enabled=False), event(), NOW, site_tz="UTC").reason == "disabled"


def test_activity_must_match():
    assert rules.should_fire(rule(activity="loitering"), event(), NOW,
                             site_tz="UTC").reason == "activity"


def test_camera_scope():
    r = rule(scope_kind="camera", scope_value="cam2-o8iu")
    assert rules.should_fire(r, event(), NOW, site_tz="UTC").fires is True
    assert rules.should_fire(r, event(camera_slug="cam9-zzzz"), NOW,
                             site_tz="UTC").reason == "scope"


def test_zone_scope():
    r = rule(scope_kind="zone", scope_value="Zone 1")
    assert rules.should_fire(r, event(), NOW, site_tz="UTC").fires is True
    assert rules.should_fire(r, event(zone="Zone 4"), NOW, site_tz="UTC").reason == "scope"


def test_zone_scope_against_a_whole_frame_event_does_not_match():
    """zone=None is a real configuration (a whole-frame detector), not 'unset'.
    A zone-scoped rule must not silently adopt every whole-frame detection."""
    r = rule(scope_kind="zone", scope_value="Zone 1")
    assert rules.should_fire(r, event(zone=None), NOW, site_tz="UTC").reason == "scope"


def test_confidence_gate():
    r = rule(min_confidence=0.8)
    assert rules.should_fire(r, event(confidence=0.9), NOW, site_tz="UTC").fires is True
    assert rules.should_fire(r, event(confidence=0.7), NOW,
                             site_tz="UTC").reason == "confidence"


def test_null_confidence_passes_only_when_the_rule_asks_for_no_minimum():
    """NULL confidence means the detector reported none, which is distinct from
    0.0. Treating it as 0.0 would silently mute every rule with a threshold on
    a detector that does not score."""
    assert rules.should_fire(rule(min_confidence=None), event(confidence=None), NOW,
                             site_tz="UTC").fires is True
    assert rules.should_fire(rule(min_confidence=0.5), event(confidence=None), NOW,
                             site_tz="UTC").reason == "confidence"


def test_conditions_are_applied():
    r = rule(conditions={"all": [{"field": "object_class", "op": "eq", "value": "vehicle"}]})
    assert rules.should_fire(r, event(), NOW, site_tz="UTC").reason == "conditions"


def test_schedule_is_applied():
    r = rule(active_hours={"windows": [["22:00", "06:00"]]})
    assert rules.should_fire(r, event(), NOW, site_tz="UTC").reason == "schedule"


def test_a_stale_event_still_fires_but_must_not_actuate():
    """Spec 6.4: the detection happened, so the alert is evidence and is kept —
    suppressing it would corrupt the false-positive denominator. But a queue
    backlog must not open a gate ten minutes late."""
    stale = event(started_at=_at(2026, 9, 18, 11, 50))     # 10 minutes old
    d = rules.should_fire(rule(max_event_age_s=30), stale, NOW, site_tz="UTC")
    assert d.fires is True
    assert d.actuate is False
    assert d.reason == "stale"


def test_log_only_mode_fires_but_does_not_actuate():
    d = rules.should_fire(rule(mode="log_only"), event(), NOW, site_tz="UTC")
    assert d.fires is True and d.actuate is False and d.reason == "log_only"


def test_an_event_from_the_future_is_not_treated_as_stale():
    """Camera clock skew runs both ways. A slightly-ahead clock must not
    disable actuation."""
    d = rules.should_fire(rule(max_event_age_s=30), event(started_at=_at(2026, 9, 18, 12, 0)),
                          NOW, site_tz="UTC")
    assert d.actuate is True
