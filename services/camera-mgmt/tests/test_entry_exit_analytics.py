"""Entry / Exit analytics: the graph's ranges, buckets and totals, and the API serving them.

The individual Entry and Exit records stay in analytics_events; the API counts
them per time bucket for one camera. These pin the arithmetic (every range a
whole number of readable buckets, totals equal to the sum of the buckets,
directionless records never counted) and the HTTP contract (who may read,
which ranges exist, which tripwires may be filtered on). The SQL itself runs
against a real PostgreSQL in test_ai_events_db.py.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import StubSession
from backend.services import entry_exit as ee

URL = "/api/analytics/events/entry-exit"
NOW = datetime(2026, 9, 15, 10, 7, 13, tzinfo=timezone.utc)

DOOR = SimpleNamespace(
    id=uuid.uuid4(), slug="door-g7h8", name="Door", enabled=True, retention_days=None,
    analytics_config={
        "regions": {
            "region_1": {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
                         "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]},
            "region_2": {"kind": "tripwire", "name": "Front door", "color": "#ffb020",
                         "points": [[0.2, 0.5], [0.8, 0.5]], "direction": "a2b"},
            "region_3": {"kind": "tripwire", "name": "Side door", "color": "#ffb020",
                         "points": [[0.1, 0.3], [0.1, 0.8]], "direction": "both"},
            "region_4": {"kind": "tripwire", "name": "Unused line", "color": "#ffb020",
                         "points": [[0.3, 0.3], [0.6, 0.3]], "direction": "b2a"},
        },
        "activities": [
            {"type": "restricted_zone_entry", "regions": ["region_1"], "params": {}},
            {"type": "entry_exit_WLE_logs", "regions": ["region_2", "region_3"], "params": {}},
        ],
    },
)


# ── the window ───────────────────────────────────────────────────────────────

class TestWindow:
    @pytest.mark.parametrize("minutes, bucket, n", [
        (5, 10, 30), (15, 30, 30), (30, 60, 30), (60, 120, 30),
        (120, 300, 24), (360, 900, 24), (720, 1800, 24), (1440, 3600, 24),
    ])
    def test_every_range_is_a_whole_number_of_readable_buckets(self, minutes, bucket, n):
        win = ee.window(minutes, NOW)
        assert (win.bucket_s, win.buckets) == (bucket, n)
        assert win.end - win.start == timedelta(minutes=minutes)
        assert win.start <= NOW <= win.end and win.end - NOW < timedelta(seconds=bucket)
        assert int(win.start.timestamp()) % bucket == 0

    def test_exactly_these_ranges_one_hour_by_default_and_24_hours_at_most(self):
        assert list(ee.RANGES) == [5, 15, 30, 60, 120, 360, 720, 1440]
        assert ee.DEFAULT_MINUTES == 60 and max(ee.RANGES) == 1440

    def test_buckets_start_on_round_times_of_the_viewers_clock(self):
        ist = 330                                               # UTC+5:30; NOW is 15:37 there
        win = ee.window(1440, NOW, ist)
        assert (int(win.start.timestamp()) + ist * 60) % 3600 == 0
        assert win.end - win.start == timedelta(hours=24) and win.start <= NOW <= win.end
        out = ee.series([(win.first_slot, "entry", 2), (win.first_slot + 23, "exit", 1)], win)
        assert (out["start"], out["end"]) == ("2026-09-14T10:30:00Z", "2026-09-15T10:30:00Z")
        assert out["buckets"][0] == {"start": "2026-09-14T10:30:00Z", "entry": 2, "exit": 0}
        assert out["buckets"][23]["exit"] == 1 and out["tz_offset"] == ist


# ── the series ───────────────────────────────────────────────────────────────

class TestSeries:
    def test_counts_land_in_their_bucket_and_the_totals_are_their_sum(self):
        win = ee.window(60, NOW)
        first = int(win.start.timestamp()) // win.bucket_s
        out = ee.series([(first, "entry", 3), (first, "exit", 1), (first + 5, "entry", 2),
                         (first + 29, "exit", 4)], win)
        assert len(out["buckets"]) == 30
        assert out["buckets"][0] == {"start": out["start"], "entry": 3, "exit": 1}
        assert (out["buckets"][5]["entry"], out["buckets"][29]["exit"]) == (2, 4)
        assert out["totals"] == {"entry": 5, "exit": 5}
        assert out["totals"]["entry"] == sum(b["entry"] for b in out["buckets"])
        assert (out["minutes"], out["bucket_seconds"]) == (60, 120)

    def test_records_outside_the_window_or_without_a_direction_are_not_counted(self):
        win = ee.window(5, NOW)
        first = int(win.start.timestamp()) // win.bucket_s
        out = ee.series([(first - 1, "entry", 9), (first + 30, "exit", 9),
                         (first, "touch", 9), (first, "entry", 1)], win)
        assert out["totals"] == {"entry": 1, "exit": 0}

    def test_buckets_are_evenly_spaced_utc_times_covering_the_window(self):
        win = ee.window(15, NOW)
        out = ee.series([], win)
        starts = [datetime.fromisoformat(b["start"].replace("Z", "+00:00")) for b in out["buckets"]]
        assert starts[0] == win.start and all(s.tzinfo is not None for s in starts)
        assert {b - a for a, b in zip(starts, starts[1:])} == {timedelta(seconds=30)}
        assert starts[-1] + timedelta(seconds=30) == win.end
        assert out["end"].endswith("Z")


# ── the tripwires ────────────────────────────────────────────────────────────

class TestTripwires:
    def test_only_tripwires_entry_exit_watches_each_with_whether_it_has_an_entry_direction(self):
        assert ee.tripwires(DOOR) == [
            {"id": "region_2", "name": "Front door", "direction": "a2b", "oriented": True},
            {"id": "region_3", "name": "Side door", "direction": "both", "oriented": False},
        ]

    def test_a_camera_without_entry_exit_has_none(self):
        assert ee.tripwires(SimpleNamespace(analytics_config={})) == []
        assert ee.tripwires(SimpleNamespace(analytics_config=None)) == []


# ── over HTTP ────────────────────────────────────────────────────────────────

class _Registry(StubSession):
    """StubSession answering camera lookups from a list; everything else empty."""

    def __init__(self, cameras):
        super().__init__()
        self.cameras = cameras

    async def execute(self, statement=None, *a, **kw):
        sql = str(statement)
        if "FROM cameras" in sql and "analytics_events" not in sql:
            cams = self.cameras
            return SimpleNamespace(scalars=lambda: SimpleNamespace(
                all=lambda: list(cams), first=lambda: cams[0] if cams else None))
        return await super().execute(statement, *a, **kw)


@pytest.fixture
def db():
    return _Registry([DOOR])


@pytest.mark.asyncio
async def test_reading_needs_the_ai_analytics_capability(client, mint):
    params = {"camera": "door-g7h8"}
    assert (await client.get(URL, params=params)).status_code == 401
    assert (await client.get(URL, params=params, headers=mint(["viewer"]))).status_code == 403
    assert (await client.get(URL, params=params, headers=mint(["operator"]))).status_code == 200


@pytest.mark.asyncio
async def test_one_hour_is_served_by_default(client, mint):
    r = await client.get(URL, params={"camera": "door-g7h8"}, headers=mint(["operator"]))
    body = r.json()
    assert (body["minutes"], body["bucket_seconds"], len(body["buckets"])) == (60, 120, 30)
    assert body["totals"] == {"entry": 0, "exit": 0} and body["configured"] is True
    assert body["camera"]["slug"] == "door-g7h8" and body["tripwire"] is None
    assert [t["id"] for t in body["tripwires"]] == ["region_2", "region_3"]


@pytest.mark.asyncio
async def test_the_range_and_tripwire_reach_the_count_and_totals_come_from_it(client, mint, monkeypatch):
    seen = {}

    async def counts(db, camera_id, win, tripwire=None):
        seen.update(camera_id=camera_id, minutes=win.minutes, tripwire=tripwire)
        first = int(win.start.timestamp()) // win.bucket_s
        return [(first, "entry", 2), (first + 1, "exit", 1), (first + 1, "entry", 4)]

    monkeypatch.setattr(ee, "counts", counts)
    r = await client.get(URL, headers=mint(["operator"]),
                         params={"camera": "door-g7h8", "minutes": 15, "tripwire": "region_2"})
    assert r.status_code == 200
    assert seen == {"camera_id": DOOR.id, "minutes": 15, "tripwire": "region_2"}
    body = r.json()
    assert body["totals"] == {"entry": 6, "exit": 1} and body["tripwire"] == "region_2"
    assert body["buckets"][1] == {"start": body["buckets"][1]["start"], "entry": 4, "exit": 1}


@pytest.mark.asyncio
async def test_the_viewers_utc_offset_aligns_the_buckets(client, mint, monkeypatch):
    seen = {}

    async def counts(db, camera_id, win, tripwire=None):
        seen["offset"] = win.offset_s
        return []

    monkeypatch.setattr(ee, "counts", counts)
    r = await client.get(URL, headers=mint(["operator"]),
                         params={"camera": "door-g7h8", "minutes": 1440, "tz_offset": 330})
    assert r.status_code == 200 and seen == {"offset": 19800}
    start = datetime.fromisoformat(r.json()["start"].replace("Z", "+00:00"))
    assert (int(start.timestamp()) + 19800) % 3600 == 0 and r.json()["tz_offset"] == 330


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-841, 841])
async def test_an_offset_no_clock_has_is_refused(client, mint, offset):
    r = await client.get(URL, headers=mint(["operator"]),
                         params={"camera": "door-g7h8", "tz_offset": offset})
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 45, 1441, 2880])
async def test_only_the_listed_ranges_are_accepted(client, mint, minutes):
    r = await client.get(URL, headers=mint(["operator"]), params={"camera": "door-g7h8", "minutes": minutes})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_camera_is_a_404(client, mint, db):
    db.cameras = []
    r = await client.get(URL, headers=mint(["operator"]), params={"camera": "nobody-0000"})
    assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("tripwire", ["region_1", "region_4", "region_9"])
async def test_a_tripwire_entry_exit_does_not_watch_is_a_404(client, mint, tripwire):
    r = await client.get(URL, headers=mint(["operator"]),
                         params={"camera": "door-g7h8", "tripwire": tripwire})
    assert r.status_code == 404
