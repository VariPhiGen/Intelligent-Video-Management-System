"""AI activity events over HTTP: who may write, who may read, what is refused.

Two different callers, two different gates. Only the analytics pipeline may
ASSERT that an activity fired — a signed-in user, admin included, never can —
and reading events needs the `ai_analytics` capability, enforced by the API
rather than by the SPA hiding a tab. Both only exist once a request is routed,
so they are asserted through the real app (conftest's `client`), not by calling
handlers.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import StubSession
from backend.services import ai_events

pytestmark = pytest.mark.asyncio

KEY = {"X-Internal-Key": "test-internal-key"}
URL = "/api/analytics/events"

GATE = SimpleNamespace(
    id=uuid.uuid4(), slug="gate-a1b2", name="Gate", enabled=True, retention_days=None,
    analytics_config={
        "regions": {"region_1": {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
                                 "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}},
        "activities": [{"type": "restricted_zone_entry", "regions": ["region_1"], "params": {}},
                       {"type": "no_person_area", "regions": ["region_1"], "params": {}}],
    },
)


class _Registry(StubSession):
    """StubSession that answers camera lookups from a list; everything else empty."""

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
    return _Registry([GATE])


def _event(**kw):
    return {"sensor_id": "gate-a1b2", "activity": "restricted_zone_entry",
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), **kw}


# ── Ingest ───────────────────────────────────────────────────────────────────

async def test_ingest_refuses_anonymous(client):
    r = await client.post(URL, json={"events": [_event()]})
    assert r.status_code == 401


async def test_ingest_refuses_a_signed_in_user_even_an_admin(client, mint):
    r = await client.post(URL, json={"events": [_event()]}, headers=mint(["admin"]))
    assert r.status_code == 403


async def test_the_pipeline_stores_events_only_for_configured_activities(client, monkeypatch):
    stored = []

    async def record(db, rows):
        stored.extend(rows)
        return {"inserted": len(rows), "closed": 0, "duplicates": 0}

    monkeypatch.setattr(ai_events, "record", record)
    r = await client.post(URL, headers=KEY, json={"events": [
        _event(zone="zone1", track_id=17),
        _event(activity="person_detection"),
        _event(sensor_id="nobody-0000"),
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["received"] == 3 and body["inserted"] == 1
    assert [(x["index"], x["reason"]) for x in body["rejected"]] == [
        (1, "activity_not_configured"), (2, "unknown_camera")]
    [row] = stored
    assert row["camera_id"] == GATE.id
    assert row["activity"] == "restricted_zone_entry"
    assert row["zone"] == "Zone 1"


async def test_a_batch_over_the_limit_is_refused(client):
    r = await client.post(URL, headers=KEY,
                          json={"events": [_event()] * (ai_events.INGEST_MAX_BATCH + 1)})
    assert r.status_code == 422


async def test_a_timestamp_without_a_timezone_is_refused(client):
    r = await client.post(URL, headers=KEY,
                          json={"events": [_event(started_at="2026-09-13T14:00:00")]})
    assert r.status_code == 422


# ── Reads ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [URL, f"{URL}/overview"])
async def test_reading_events_needs_the_ai_analytics_capability(client, mint, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=mint(["viewer"]))).status_code == 403
    assert (await client.get(path, headers=mint(["operator"]))).status_code == 200


async def test_the_overview_keeps_a_quiet_configured_camera(client, mint):
    r = await client.get(f"{URL}/overview", headers=mint(["operator"]))
    body = r.json()
    assert body["latest"] == [] and body["ticker_limit"] == 10
    [card] = body["cameras"]
    assert card["camera"]["slug"] == "gate-a1b2"
    assert [a["key"] for a in card["activities"]] == ["restricted_zone_entry", "no_person_area"]
    assert card["events"] == []


async def test_camera_activity_and_dates_reach_the_query(client, mint, monkeypatch):
    seen = {}

    async def list_events(db, f, catalog):
        seen["f"] = f
        return [], None

    monkeypatch.setattr(ai_events, "list_events", list_events)
    r = await client.get(URL, headers=mint(["operator"]), params={
        "camera": "gate-a1b2", "activity": "no_person_area",
        "from": "2026-09-13T10:00:00Z", "to": "2026-09-13T18:00:00+05:30", "limit": 25})
    assert r.status_code == 200 and r.json() == {"events": [], "next_before": None}
    f = seen["f"]
    assert f.camera_ids == [GATE.id] and f.activity == "no_person_area" and f.limit == 25
    assert f.frm == datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
    assert f.to == datetime(2026, 9, 13, 12, 30, tzinfo=timezone.utc)   # +05:30 normalised


@pytest.mark.parametrize("params, code", [
    ({"from": "2026-09-13T12:00:00Z", "to": "2026-09-13T10:00:00Z"}, 400),
    ({"from": "2026-09-13T12:00:00"}, 400),
    ({"before": "not-a-cursor"}, 400),
    ({"limit": 201}, 422),
])
async def test_bad_filters_are_refused(client, mint, params, code):
    r = await client.get(URL, headers=mint(["operator"]), params=params)
    assert r.status_code == code


async def test_per_camera_is_bounded(client, mint):
    r = await client.get(f"{URL}/overview", headers=mint(["operator"]), params={"per_camera": 21})
    assert r.status_code == 422


async def test_an_unknown_camera_is_a_404(client, mint, db):
    db.cameras = []
    r = await client.get(URL, headers=mint(["operator"]), params={"camera": "nobody-0000"})
    assert r.status_code == 404
