"""AI activity events: what gets stored, against which camera, and how it reads back.

The Events tab is only as honest as the rule deciding what counts as an event.
An event exists because an activity the operator CONFIGURED on that camera said
something happened — not because the detector saw a person. These tests pin
that rule, the camera/activity association, and the pieces of the read path
that are pure logic: zone naming (which must number zones exactly as the
pipeline was given them), newest-first ordering, the ticker's cap, the camera
cards, and where Open Playback lands.

What needs Postgres — the upsert, the LATERAL slice, the index — is in
test_ai_events_db.py.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import ai_events as ev  # noqa: E402
from backend.services.deepstream import deepstream_config  # noqa: E402

NOW = datetime(2026, 9, 13, 14, 0, 0, tzinfo=timezone.utc)
SLUG = "cam2-o8iu"


def _zone(name, points=None, kind="zone", direction=None):
    return {"kind": kind, "name": name, "color": "#4fd1c5",
            "points": points or [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]], "direction": direction}


def camera(slug=SLUG, activities=("unauthorised_access",), regions=None,
           retention_days=None, name=None):
    regions = regions if regions is not None else {"region_1": _zone("Zone 1")}
    acts = [{"type": a, "regions": list(regions), "params": {}} for a in activities]
    return SimpleNamespace(id=uuid.uuid4(), slug=slug, name=name or slug.split("-")[0],
                           enabled=True, retention_days=retention_days,
                           analytics_config={"regions": regions, "activities": acts})


def event(**kw):
    base = dict(sensor_id=SLUG, activity="unauthorised_access",
                started_at=NOW - timedelta(minutes=1))
    base.update(kw)
    return ev.EventIn(**base)


def prepare(items, *cams, default_retention_days=30):
    return ev.prepare(items, {c.slug: c for c in cams}, now=NOW,
                      default_retention_days=default_retention_days)


# ── Event creation from configured activities ───────────────────────────────

class TestCreation:
    def test_an_event_from_a_configured_activity_is_stored_against_its_camera(self):
        cam = camera()
        rows, rejected = prepare([event(confidence=0.91, track_id=42)], cam)
        assert rejected == []
        [row] = rows
        assert row["camera_id"] == cam.id
        assert row["activity"] == "unauthorised_access"
        assert row["source"] == "deepstream"
        assert row["track_id"] == "42"          # text column, whatever the producer sent
        assert row["confidence"] == 0.91

    def test_a_raw_detection_is_not_an_event(self):
        """The pipeline sees people all day. Unless the camera is configured for
        an activity that turns that into an event, nothing is stored."""
        cam = camera(activities=("unauthorised_access",))
        rows, rejected = prepare([event(activity="person_detection")], cam)
        assert rows == []
        assert rejected[0]["reason"] == "activity_not_configured"

    def test_an_activity_configured_on_another_camera_does_not_count(self):
        gate = camera(slug="gate-a1b2", activities=("restricted_zone_entry",))
        yard = camera(slug="yard-c3d4", activities=("stray_parking",))
        rows, rejected = prepare(
            [event(sensor_id="yard-c3d4", activity="restricted_zone_entry")], gate, yard)
        assert rows == []
        assert rejected[0] == {"index": 0, "reason": "activity_not_configured",
                               "sensor_id": "yard-c3d4", "activity": "restricted_zone_entry"}

    def test_every_activity_on_a_multi_activity_camera_is_accepted(self):
        cam = camera(activities=("restricted_zone_entry", "no_person_area", "idle_worker"))
        rows, rejected = prepare(
            [event(activity=a) for a in ("restricted_zone_entry", "no_person_area", "idle_worker")],
            cam)
        assert rejected == []
        assert [r["activity"] for r in rows] == ["restricted_zone_entry", "no_person_area",
                                                 "idle_worker"]
        assert {r["camera_id"] for r in rows} == {cam.id}

    def test_an_unregistered_camera_is_refused(self):
        rows, rejected = prepare([event(sensor_id="nobody-0000")], camera())
        assert rows == [] and rejected[0]["reason"] == "unknown_camera"

    def test_one_bad_event_does_not_cost_the_rest_of_its_batch(self):
        cam = camera()
        rows, rejected = prepare(
            [event(), event(sensor_id="nobody-0000"), event(started_at=NOW - timedelta(seconds=5))],
            cam)
        assert len(rows) == 2
        assert [r["index"] for r in rejected] == [1]

    def test_an_interval_that_ends_before_it_starts_is_refused(self):
        rows, rejected = prepare(
            [event(started_at=NOW - timedelta(seconds=10), ended_at=NOW - timedelta(seconds=20))],
            camera())
        assert rows == [] and rejected[0]["reason"] == "ended_before_started"

    def test_a_start_beyond_the_clock_skew_allowance_is_refused(self):
        cam = camera()
        ok, _ = prepare([event(started_at=NOW + ev.FUTURE_SKEW - timedelta(seconds=1))], cam)
        rows, rejected = prepare([event(started_at=NOW + ev.FUTURE_SKEW + timedelta(seconds=1))], cam)
        assert len(ok) == 1
        assert rows == [] and rejected[0]["reason"] == "in_future"

    def test_expiry_follows_the_camera_footage_retention(self):
        """An event must not outlive the footage its playback link points at."""
        start = NOW - timedelta(hours=1)
        [short], _ = prepare([event(started_at=start)], camera(retention_days=7))
        [default], _ = prepare([event(started_at=start)], camera(), default_retention_days=30)
        assert short["expires_at"] == start + timedelta(days=7)
        assert default["expires_at"] == start + timedelta(days=30)

    def test_an_event_older_than_the_footage_it_would_link_to_is_refused(self):
        rows, rejected = prepare([event(started_at=NOW - timedelta(days=8))],
                                 camera(retention_days=7))
        assert rows == [] and rejected[0]["reason"] == "outside_retention"

    def test_oversized_attributes_are_refused(self):
        big = {"blob": "x" * (ev.ATTRIBUTES_MAX_BYTES + 1)}
        rows, rejected = prepare([event(attributes=big)], camera())
        assert rows == [] and rejected[0]["reason"] == "attributes_too_large"

    def test_timestamps_without_a_timezone_are_not_accepted(self):
        with pytest.raises(ValidationError):
            ev.EventIn(sensor_id=SLUG, activity="unauthorised_access",
                       started_at=datetime(2026, 9, 13, 14, 0))

    def test_a_retry_without_a_producer_id_lands_on_the_same_row(self):
        cam = camera()
        [a], _ = prepare([event(track_id=7)], cam)
        [b], _ = prepare([event(track_id=7)], cam)
        [c], _ = prepare([event(track_id=7, started_at=NOW - timedelta(seconds=30))], cam)
        assert a["id"] == b["id"]
        assert a["id"] != c["id"]

    def test_a_producer_minted_id_is_kept(self):
        mine = uuid.uuid4()
        [row], _ = prepare([event(id=mine)], camera())
        assert row["id"] == mine

    def test_configured_activities_skip_malformed_entries_and_keep_order(self):
        cam = camera()
        cam.analytics_config["activities"] = [
            {"type": "no_person_area"}, {"regions": ["region_1"]}, "junk",
            {"type": "restricted_zone_entry"}, {"type": "no_person_area"},
        ]
        assert ev.configured_activities(cam) == ["no_person_area", "restricted_zone_entry"]


# ── Zones: named the way the operator drew them ─────────────────────────────

class TestZones:
    def test_the_pipeline_zone_key_resolves_to_the_region_name(self):
        cam = camera(regions={"region_1": _zone("Loading bay"), "region_2": _zone("Gate")})
        assert ev.zone_name(cam, "unauthorised_access", "zone2") == "Gate"

    def test_a_region_id_or_name_is_accepted_too(self):
        cam = camera(regions={"region_1": _zone("Loading bay")})
        assert ev.zone_name(cam, "unauthorised_access", "region_1") == "Loading bay"
        assert ev.zone_name(cam, "unauthorised_access", "Loading bay") == "Loading bay"

    def test_a_zone_that_no_longer_resolves_is_kept_verbatim(self):
        assert ev.zone_name(camera(), "unauthorised_access", "zone9") == "zone9"
        assert ev.zone_name(camera(), "unauthorised_access", None) is None

    def test_zone_numbering_matches_what_the_pipeline_was_given(self):
        """The pipeline only knows `zoneN`. If this numbering drifts from
        services/deepstream.py, every event is filed under the wrong zone."""
        regions = {
            "r1": _zone("North", [[0.0, 0.0], [0.4, 0.0], [0.2, 0.3]]),
            "r2": _zone("South", [[0.0, 0.6], [0.4, 0.6], [0.2, 0.9]]),
            "r3": _zone("East", [[0.6, 0.0], [0.9, 0.0], [0.8, 0.3]]),
        }
        cam = camera(activities=("restricted_zone_entry",), regions=regions)
        # A region the activity still references but that has since been deleted.
        cam.analytics_config["activities"][0]["regions"] = ["r1", "gone", "r2", "r3"]

        emitted = deepstream_config(cam)["activities_data"]["restricted_zone_entry"]["zones"]
        for key, points in emitted.items():
            name = ev.zone_name(cam, "restricted_zone_entry", key)
            assert [r for r in regions.values() if r["name"] == name][0]["points"] == points

    def test_line_crossing_zones_number_only_usable_tripwires(self):
        regions = {
            "poly": _zone("Area"),
            "wire": _zone("Doorway line", [[0.2, 0.5], [0.8, 0.5]], kind="tripwire", direction="a2b"),
        }
        cam = camera(activities=("entry_exit_WLE_logs",), regions=regions)
        emitted = deepstream_config(cam)["activities_data"]["entry_exit_WLE_logs"]["zones"]
        assert list(emitted) == ["zone1"]
        assert ev.zone_name(cam, "entry_exit_WLE_logs", "zone1") == "Doorway line"


# ── Reading back ─────────────────────────────────────────────────────────────

def stored(camera_id, activity="unauthorised_access", started_at=NOW, ended_at=None, **kw):
    row = {"id": uuid.uuid4(), "camera_id": camera_id, "activity": activity, "zone": "Zone 1",
           "started_at": started_at, "ended_at": ended_at, "confidence": None,
           "track_id": None, "object_class": "person", "attributes": {}, "source": "deepstream"}
    row.update(kw)
    return row


CATALOG = {"restricted_zone_entry": {"label": "Restricted zone entry", "color": "#ffb020"},
           "no_person_area": {"label": "No-person area", "color": "#4fd1c5"}}


class TestPlayback:
    def test_open_playback_starts_before_the_event(self):
        started = datetime(2026, 9, 13, 20, 14, 32, tzinfo=timezone.utc)
        win = ev.playback_window(SLUG, started, None)
        assert win["camera"] == SLUG
        assert win["start"] == int(started.timestamp()) - ev.PLAYBACK_PRE_ROLL_S
        assert win["end"] == int(started.timestamp()) + ev.PLAYBACK_POST_ROLL_S

    def test_an_interval_event_plays_through_its_end(self):
        started = datetime(2026, 9, 13, 20, 14, 32, tzinfo=timezone.utc)
        ended = started + timedelta(minutes=3)
        win = ev.playback_window(SLUG, started, ended)
        assert win["end"] == int(ended.timestamp()) + ev.PLAYBACK_POST_ROLL_S

    def test_every_shaped_event_carries_its_playback_target(self):
        cam = camera()
        out = ev.shape(stored(cam.id), slug=cam.slug, name="cam2", catalog=CATALOG)
        assert out["playback"] == ev.playback_window(cam.slug, NOW, None)


class TestShape:
    def test_labels_come_from_the_catalog(self):
        cam = camera()
        out = ev.shape(stored(cam.id, activity="restricted_zone_entry"),
                       slug=cam.slug, name="cam2", catalog=CATALOG)
        assert out["activity"] == {"key": "restricted_zone_entry",
                                   "label": "Restricted zone entry", "color": "#ffb020"}

    def test_a_withdrawn_type_still_reads_as_words(self):
        """unauthorised_access left the catalog in 033; its history must not
        turn into a raw key or vanish."""
        assert ev.activity_meta("unauthorised_access", CATALOG)["label"] == "Unauthorised access"

    def test_times_are_utc_and_duration_is_derived(self):
        cam = camera()
        out = ev.shape(stored(cam.id, started_at=NOW, ended_at=NOW + timedelta(seconds=95)),
                       slug=cam.slug, name=None, catalog=CATALOG)
        assert out["started_at"] == "2026-09-13T14:00:00Z"
        assert out["ended_at"] == "2026-09-13T14:01:35Z"
        assert out["duration_s"] == 95.0
        assert out["camera"] == {"id": str(cam.id), "slug": cam.slug, "name": cam.slug}

    def test_a_point_event_has_no_duration(self):
        cam = camera()
        out = ev.shape(stored(cam.id), slug=cam.slug, name="cam2", catalog=CATALOG)
        assert out["ended_at"] is None and out["duration_s"] is None


class TestCursor:
    def test_a_cursor_round_trips(self):
        c = ev.Cursor(NOW, uuid.uuid4())
        assert ev.Cursor.decode(c.encode()) == c

    @pytest.mark.parametrize("token", ["", "garbage", "bm90LWEtY3Vyc29y"])
    def test_anything_else_is_refused(self, token):
        with pytest.raises(ValueError):
            ev.Cursor.decode(token)


# ── The query: filters and newest-first ordering ────────────────────────────

def _sql(f: ev.EventFilter):
    compiled = ev.events_query(f).compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()), compiled.params


class TestQuery:
    def test_newest_first_by_when_it_happened_with_a_stable_tie_break(self):
        sql, _ = _sql(ev.EventFilter())
        assert "ORDER BY analytics_events.started_at DESC, analytics_events.id DESC" in sql

    def test_only_registered_cameras_and_unexpired_rows(self):
        sql, params = _sql(ev.EventFilter())
        assert "cameras.stage = %(stage_1)s" in sql and params["stage_1"] == "registered"
        assert "analytics_events.expires_at > now()" in sql

    def test_camera_activity_and_date_filters_are_all_applied(self):
        cid = uuid.uuid4()
        frm, to = NOW - timedelta(hours=2), NOW
        sql, params = _sql(ev.EventFilter(camera_ids=[cid], activity="no_person_area",
                                          frm=frm, to=to, limit=25))
        assert "analytics_events.camera_id IN" in sql
        assert "analytics_events.activity = " in sql
        assert "analytics_events.started_at >= " in sql
        assert "analytics_events.started_at <= " in sql
        values = list(params.values())
        assert [cid] in values and "no_person_area" in values
        assert frm in values and to in values and 25 in values

    def test_unfiltered_reads_add_no_filters(self):
        sql, _ = _sql(ev.EventFilter())
        assert "camera_id IN" not in sql and "activity = " not in sql
        assert "started_at >=" not in sql and "started_at <=" not in sql

    def test_the_next_page_starts_strictly_after_the_cursor(self):
        sql, _ = _sql(ev.EventFilter(before=ev.Cursor(NOW, uuid.uuid4())))
        assert "(analytics_events.started_at, analytics_events.id) < " in sql


# ── Camera cards and the ticker ─────────────────────────────────────────────

class TestCards:
    def test_every_configured_activity_is_listed_on_its_card(self):
        cam = camera(activities=("restricted_zone_entry", "no_person_area", "unauthorised_access"))
        [card] = ev.camera_cards([cam], {}, CATALOG)
        assert [a["key"] for a in card["activities"]] == [
            "restricted_zone_entry", "no_person_area", "unauthorised_access"]
        assert [a["label"] for a in card["activities"]] == [
            "Restricted zone entry", "No-person area", "Unauthorised access"]

    def test_a_configured_camera_with_no_events_keeps_its_card(self):
        [card] = ev.camera_cards([camera()], {}, CATALOG)
        assert card["events"] == [] and card["last_event_at"] is None

    def test_a_camera_with_nothing_configured_gets_no_card(self):
        bare = camera(slug="lobby-e5f6", activities=())
        assert ev.camera_cards([bare], {}, CATALOG) == []

    def test_a_card_shows_its_own_camera_events_across_all_its_activities(self):
        cam = camera(activities=("restricted_zone_entry", "no_person_area"))
        rows = [stored(cam.id, "no_person_area", NOW),
                stored(cam.id, "restricted_zone_entry", NOW - timedelta(minutes=5))]
        [card] = ev.camera_cards([cam], {cam.id: rows}, CATALOG)
        assert [e["activity"]["key"] for e in card["events"]] == [
            "no_person_area", "restricted_zone_entry"]
        assert card["last_event_at"] == "2026-09-13T14:00:00Z"

    def test_cards_hold_their_place_by_name(self):
        cams = [camera(slug="zulu-0001", name="Zulu"), camera(slug="alpha-0002", name="alpha"),
                camera(slug="mike-0003", name="Mike")]
        assert [c["camera"]["name"] for c in ev.camera_cards(cams, {}, CATALOG)] == [
            "alpha", "Mike", "Zulu"]


class _CamerasOnly:
    def __init__(self, cameras):
        self.cameras = cameras

    async def execute(self, statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.cameras))


@pytest.mark.parametrize("stored_count", [3, 10, 15])
def test_the_ticker_never_carries_more_than_ten(monkeypatch, stored_count):
    import asyncio

    asked = {}
    many = [ev.shape(stored(uuid.uuid4(), started_at=NOW - timedelta(seconds=i)),
                     slug=SLUG, name="cam2", catalog=CATALOG) for i in range(stored_count)]

    async def list_events(db, f, catalog):
        asked["limit"] = f.limit
        return many, None

    async def nothing(*a, **kw):
        return {}

    monkeypatch.setattr(ev, "list_events", list_events)
    monkeypatch.setattr(ev, "load_catalog", nothing)
    monkeypatch.setattr(ev, "latest_per_camera", nothing)

    out = asyncio.run(ev.overview(_CamerasOnly([camera()]), per_camera=5))
    assert asked["limit"] == ev.TICKER_LIMIT == 10
    assert len(out["latest"]) == min(stored_count, 10)
    assert out["ticker_limit"] == 10
    assert [e["started_at"] for e in out["latest"]] == sorted(
        (e["started_at"] for e in out["latest"]), reverse=True)
