"""Retention is one number in one unit, and the unit is minutes.

`retention_days` became `retention_minutes` (migration 042) as a RENAME, not an
addition, and that is the load-bearing decision the tests here defend. Retention
is not one number used in one place: the recorder's sweep, the Smart Search
index's `expires_at`, `analytics_events`, the heatmap buckets, the sub track's
clamp and the DPDP posture report all derive a deletion deadline from it. Two
columns means two of those can disagree, and the shape of that disagreement is
the appliance reporting a retention it is not keeping while a sweep quietly
deletes on the other one.

Days have not gone away as an INPUT — the API still accepts `retention_days`,
.env still carries NVR_DEFAULT_RETENTION_DAYS, the UI still offers days as a
unit — but each of those is converted at the edge, exactly once, here.

What is pinned:

  * the conversion, both directions, and that display never drives a deadline
  * the appliance default resolving from either spelling, so an appliance
    configured before minutes existed keeps its policy across the upgrade
  * the two-units-at-once rejection, because a caller asserting two different
    deletion deadlines should be told, not have one silently picked
  * the human spelling, which the compliance report and the audit trail print

Run (from services/camera-mgmt): python -m pytest tests/test_retention_units.py -q
"""
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import settings  # noqa: E402
from backend.models import CameraUpdate, SubTrackBody  # noqa: E402
from backend.services import retention_units as ru  # noqa: E402

DAY = ru.MINUTES_PER_DAY


# ── conversion ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days,minutes", [(1, 1440), (2, 2880), (30, 43200), (0, 0)])
def test_days_convert_to_minutes(days, minutes):
    assert ru.days_to_minutes(days) == minutes


def test_none_passes_through_rather_than_becoming_zero():
    """None means "the appliance default"; 0 means "reset to it". Collapsing
    the two would turn "leave this alone" into a write."""
    assert ru.days_to_minutes(None) is None
    assert ru.minutes_to_days(None) is None


def test_minutes_to_days_is_fractional_and_for_display_only():
    """It must not round. A 30-minute retention floored to "0 days" is a sweep
    that stops deleting; rounded up to "1 day" it is a promise nobody made."""
    assert ru.minutes_to_days(720) == 0.5
    assert ru.minutes_to_days(30) == pytest.approx(30 / 1440)


def test_the_floor_matches_the_recorder_segment_length():
    """Segments are 60 s and retention deletes whole ones, so a minute is the
    finest period the storage layer can actually honour. This is a physical
    limit, not a policy choice."""
    assert ru.MIN_RETENTION_MINUTES == 1


def test_the_ceiling_is_unchanged_by_the_unit_move():
    """3650 days is what the day-based API always enforced. The unit change
    must not quietly widen what an operator can set."""
    assert ru.MAX_RETENTION_MINUTES == 3650 * DAY


# ── the appliance default ────────────────────────────────────────────────────

def test_the_default_falls_back_to_the_day_setting(monkeypatch):
    """THE UPGRADE PATH. An appliance whose .env predates minutes carries only
    NVR_DEFAULT_RETENTION_DAYS, and must keep exactly the policy it had."""
    monkeypatch.setattr(settings, "nvr_retention_minutes", None)
    monkeypatch.setattr(settings, "nvr_default_retention_days", 14)
    assert ru.default_retention_minutes() == 14 * DAY


def test_an_explicit_minute_default_wins(monkeypatch):
    monkeypatch.setattr(settings, "nvr_retention_minutes", 20)
    monkeypatch.setattr(settings, "nvr_default_retention_days", 30)
    assert ru.default_retention_minutes() == 20


def test_the_sub_default_follows_the_same_rule(monkeypatch):
    monkeypatch.setattr(settings, "nvr_sub_retention_minutes", None)
    monkeypatch.setattr(settings, "nvr_sub_retention_days", 3)
    assert ru.default_sub_retention_minutes() == 3 * DAY


def test_a_blank_env_value_reads_as_unset_not_as_invalid():
    """docker-compose passes every declared variable through, so an appliance
    that has not set NVR_RETENTION_MINUTES hands the field an empty string. If
    that were a validation error the api container would refuse to start on a
    stock .env — the exact upgrade every existing appliance performs."""
    from backend.config import Settings
    assert Settings(nvr_retention_minutes="",
                    nvr_sub_retention_minutes="").nvr_retention_minutes is None


def test_a_null_camera_resolves_to_the_concrete_default(monkeypatch):
    """NULL on the row is a fine answer for the recorder, which resolves the
    same default itself, but not for anything that has to STAMP a deadline on
    a row it is writing. Those callers need the number — the same number."""
    monkeypatch.setattr(settings, "nvr_retention_minutes", 20)
    assert ru.effective_retention_minutes(None) == 20
    assert ru.effective_retention_minutes(0) == 20
    assert ru.effective_retention_minutes(45) == 45


# ── the human spelling ───────────────────────────────────────────────────────

@pytest.mark.parametrize("minutes,text", [
    (1, "1 minute"), (20, "20 minutes"), (45, "45 minutes"),
    (60, "1 hour"), (120, "2 hours"), (90, "90 minutes"),
    (1440, "1 day"), (2880, "2 days"), (43200, "30 days"),
])
def test_a_period_is_spelled_in_its_largest_whole_unit(minutes, text):
    """The compliance report and the audit trail print this. "43200" is not an
    answer to "how long did you keep it?", and "0 days" about a camera kept for
    twenty minutes turns a data-minimisation win into an apparent violation."""
    assert ru.format_retention(minutes) == text


def test_an_unset_retention_says_so_rather_than_printing_a_number():
    assert ru.format_retention(None) == "appliance default"


# ── the two spellings on the wire ────────────────────────────────────────────

def test_minutes_are_taken_as_given():
    assert CameraUpdate(retention_minutes=20).resolved_retention_minutes() == 20


def test_the_deprecated_day_spelling_is_converted():
    assert CameraUpdate(retention_days=2).resolved_retention_minutes() == 2 * DAY


def test_omitting_retention_means_leave_it_alone():
    assert CameraUpdate(name="Gate A").resolved_retention_minutes() is None


def test_zero_survives_as_the_reset_sentinel():
    """0 has always meant "clear the override" and is stored as NULL. It must
    not be confused with "not being changed", which is None."""
    assert CameraUpdate(retention_minutes=0).resolved_retention_minutes() == 0
    assert CameraUpdate(retention_days=0).resolved_retention_minutes() == 0


def test_sending_both_units_is_rejected_rather_than_resolved():
    """A caller that sets both is asserting two different deletion deadlines
    for the same footage. Picking one silently is how an appliance ends up
    holding data it reported as deleted."""
    with pytest.raises(ValidationError):
        CameraUpdate(retention_minutes=20, retention_days=30)


def test_the_sub_track_body_follows_the_same_two_rules():
    assert SubTrackBody(recording_enabled=True,
                        retention_days=3).resolved_retention_minutes() == 3 * DAY
    with pytest.raises(ValidationError):
        SubTrackBody(recording_enabled=True, retention_minutes=20, retention_days=3)


def test_the_wire_ceiling_matches_the_module_ceiling():
    """Two places state the maximum; a test rather than a comment keeps them
    in step."""
    with pytest.raises(ValidationError):
        CameraUpdate(retention_minutes=ru.MAX_RETENTION_MINUTES + 1)
    assert CameraUpdate(
        retention_minutes=ru.MAX_RETENTION_MINUTES
    ).resolved_retention_minutes() == ru.MAX_RETENTION_MINUTES
