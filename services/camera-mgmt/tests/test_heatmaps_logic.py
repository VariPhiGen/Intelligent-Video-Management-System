"""Heatmap sessions: the rules that do not need a database.

What is configured, what the page is told, and the shape analytics must send.
The upsert, the unique capture index and the retention sweep need PostgreSQL
and live in test_heatmaps_db.py.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.services import heatmaps

HEAT = {"type": "Heatmap_overlay", "regions": [],
        "params": {"classes_for_heatmap": ["person"], "intensity": 1.0, "radius": 40,
                   "active_days": ["Mon", "Tue"],
                   "active_hours": {"start": "08:00", "end": "18:00"}}}
OTHER = {"type": "people_gathering", "regions": ["region_1"], "params": {}}


class TestWhichCamerasHaveOne:
    def test_a_camera_with_the_activity_is_configured(self):
        assert heatmaps.configured({"activities": [HEAT]})

    def test_a_camera_with_other_activities_is_not(self):
        assert not heatmaps.configured({"activities": [OTHER]})

    def test_no_config_at_all_is_not(self):
        assert not heatmaps.configured(None)
        assert not heatmaps.configured({})
        assert not heatmaps.configured({"activities": []})

    def test_a_malformed_entry_does_not_raise(self):
        assert not heatmaps.configured({"activities": [None, {"type": None}, 7]})


class TestTheScheduleThePageShows:
    def test_it_reports_the_configured_window(self):
        assert heatmaps.schedule_of({"activities": [HEAT]}) == {
            "active_days": ["Mon", "Tue"],
            "active_hours": {"start": "08:00", "end": "18:00"}}

    def test_all_day_every_day_is_reported_as_nothing_set(self):
        entry = {"type": "Heatmap_overlay", "regions": [], "params": {}}
        assert heatmaps.schedule_of({"activities": [entry]}) == {
            "active_days": None, "active_hours": None}

    def test_another_activitys_schedule_is_not_borrowed(self):
        other = {"type": "people_gathering", "params": {"active_days": ["Sun"]}}
        assert heatmaps.schedule_of({"activities": [other]}) is None


class TestTimestampsFromAnalytics:
    def test_epoch_seconds_become_utc(self):
        got = heatmaps._aware(1789800000.5)
        assert got == datetime(2026, 9, 19, 6, 40, 0, 500000, tzinfo=timezone.utc)

    def test_a_naive_datetime_is_read_as_utc(self):
        got = heatmaps._aware(datetime(2026, 9, 19, 12, 0, 0))
        assert got.tzinfo is timezone.utc

    def test_nothing_stays_nothing(self):
        assert heatmaps._aware(None) is None


class TestWhatThePageIsTold:
    def test_a_session_carries_its_period_and_why_it_stopped(self):
        started = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
        ended = datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc)
        row = SimpleNamespace(id=uuid.uuid4(), status="frozen", started_at=started,
                              ended_at=ended, close_reason="operator", points=120,
                              frames=1800, params={"intensity": 1.0})
        out = heatmaps._session_out(row)
        assert out["status"] == "frozen" and out["close_reason"] == "operator"
        assert (out["started_at"], out["ended_at"]) == (started, ended)
        assert out["points"] == 120 and out["params"] == {"intensity": 1.0}

    def test_no_session_is_reported_as_none_not_invented(self):
        assert heatmaps._session_out(None) is None
        assert heatmaps._artifact_out(None) is None

    def test_an_artifact_says_what_it_covers_but_carries_no_bytes(self):
        art = SimpleNamespace(id=7, session_id=uuid.uuid4(), kind="snapshot",
                              captured_at=datetime(2026, 9, 20, 14, 32, tzinfo=timezone.utc),
                              period_start=datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc),
                              width=1280, height=720)
        out = heatmaps._artifact_out(art)
        assert out["id"] == 7 and out["kind"] == "snapshot"
        assert out["width"] == 1280 and "image" not in out


class TestRefusals:
    """The ingest refuses with `Rejected`, which the router turns into 422 —
    never a 5xx, because the sink retries 5xx for ever."""

    @pytest.mark.asyncio
    async def test_a_missing_camera_or_session_is_refused(self):
        with pytest.raises(heatmaps.Rejected):
            await heatmaps.store(None, {"session_id": uuid.uuid4().hex}, None)
        with pytest.raises(heatmaps.Rejected):
            await heatmaps.store(None, {"camera": "cam1-9njq"}, None)

    @pytest.mark.asyncio
    async def test_a_session_id_that_is_not_a_uuid_is_refused(self):
        with pytest.raises(heatmaps.Rejected):
            await heatmaps.store(None, {"camera": "cam1-9njq", "session_id": "nope"}, None)


class TestConstants:
    def test_it_serves_one_activity_by_its_deepstream_key(self):
        assert heatmaps.ACTIVITY == "Heatmap_overlay"

    def test_the_three_states_are_what_the_table_allows(self):
        assert (heatmaps.ACTIVE, heatmaps.FROZEN, heatmaps.CLOSED) == \
            ("active", "frozen", "closed")

    def test_only_the_final_picture_is_archived(self):
        # `live` is the session's newest picture, replaced on every upload;
        # `snapshot` is legacy and stored as live; `final` is what the session
        # ended on. Nothing builds an archive of pictures any more.
        assert (heatmaps.LIVE, heatmaps.SNAPSHOT, heatmaps.FINAL) == \
            ("live", "snapshot", "final")
        assert heatmaps.ARCHIVED_KINDS == (heatmaps.FINAL,)
        assert heatmaps.LIVE not in heatmaps.ARCHIVED_KINDS, \
            "the live picture must never reach the object store"

    def test_no_rolling_window_survives(self):
        # The prototype discarded all but the newest three snapshots. With
        # half-hourly boundaries that would throw away 13 of a session's 16
        # historical records.
        assert not hasattr(heatmaps, "SNAPSHOTS_KEPT")
        assert not hasattr(heatmaps, "prune_snapshots")
