"""Playback presence (person/vehicle markers): the ingest rules and the HTTP gates.

The ingest refuses what AI Config does not have the camera producing — presence
for a kind the camera is not indexing — for the same reason event ingest refuses
an unconfigured activity: the timeline must show what the operator switched on,
not whatever a producer happened to forward. Reads need `ai_analytics`, enforced
by the API.

The merge query itself (gaps and islands) needs a real PostgreSQL and is
covered by test_timeline_presence_db.py.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import StubSession
from backend.services import timeline

KEY = {"X-Internal-Key": "test-internal-key"}
URL = "/api/playback/timeline"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _cam(**kw):
    base = dict(id=uuid.uuid4(), slug="gate-a1b2", name="Gate", enabled=True,
                retention_minutes=None, search_indexing=True,
                search_domains=["person", "vehicles"], analytics_config={})
    return SimpleNamespace(**{**base, **kw})


GATE = _cam()


def _seg(**kw):
    start = kw.pop("start", NOW - timedelta(minutes=2))
    end = kw.pop("end", start + timedelta(seconds=40))
    return timeline.PresenceIn(**{"sensor_id": "gate-a1b2", "kind": "person",
                                  "started_at": start, "ended_at": end, **kw})


# ── Which kinds a camera produces ────────────────────────────────────────────

def test_indexed_kinds_follow_ai_config():
    assert timeline.indexed_kinds(_cam()) == {"person", "vehicle"}
    assert timeline.indexed_kinds(_cam(search_domains=["vehicles", "plate"])) == {"vehicle"}
    assert timeline.indexed_kinds(_cam(search_domains=["plate", "face"])) == set()


def test_domains_mean_nothing_while_indexing_is_off():
    assert timeline.indexed_kinds(_cam(search_indexing=False)) == set()


def test_motion_follows_the_cameras_motion_detection_switch():
    assert "motion" in timeline.indexed_kinds(_cam(motion_detection=True))
    assert timeline.indexed_kinds(_cam(motion_detection=True, search_indexing=False)) == {"motion"}
    assert "motion" not in timeline.indexed_kinds(_cam(motion_detection=False))


def test_motion_segments_need_motion_detection_on():
    on = _cam(motion_detection=True)
    rows, rejected = timeline.prepare([_seg(kind="motion", source="motion")],
                                      {on.slug: on}, now=NOW)
    assert rejected == [] and rows[0]["kind"] == "motion" and rows[0]["source"] == "motion"
    off = _cam(motion_detection=False)
    _, rejected = timeline.prepare([_seg(kind="motion")], {off.slug: off}, now=NOW)
    assert [r["reason"] for r in rejected] == ["kind_not_indexed"]


# ── prepare ──────────────────────────────────────────────────────────────────

def test_prepare_stores_a_valid_segment_with_retention_from_the_camera():
    cam = _cam(retention_minutes=90)
    rows, rejected = timeline.prepare([_seg(track_id=7, label="person")],
                                      {cam.slug: cam}, now=NOW)
    assert rejected == []
    [row] = rows
    assert row["camera_id"] == cam.id and row["kind"] == "person"
    assert row["track_id"] == "7"
    assert row["expires_at"] == row["ended_at"] + timedelta(minutes=90)


def test_a_retry_derives_the_same_id():
    a, _ = timeline.prepare([_seg(track_id="t1")], {GATE.slug: GATE}, now=NOW)
    b, _ = timeline.prepare([_seg(track_id="t1")], {GATE.slug: GATE}, now=NOW)
    assert a[0]["id"] == b[0]["id"]


@pytest.mark.parametrize("seg, cams, reason", [
    (dict(sensor_id="nobody-0000"), None, "unknown_camera"),
    (dict(kind="vehicle"), {"gate-a1b2": _cam(search_domains=["person"])}, "kind_not_indexed"),
    (dict(sensor_id="yard-c3d4"),
     {"gate-a1b2": GATE, "yard-c3d4": _cam(slug="yard-c3d4", search_indexing=False)},
     "kind_not_indexed"),
    (dict(start=NOW, end=NOW - timedelta(seconds=1)), None, "ended_before_started"),
    (dict(start=NOW - timedelta(minutes=20), end=NOW - timedelta(minutes=9)), None, "too_long"),
    (dict(start=NOW + timedelta(minutes=10)), None, "in_future"),
    (dict(start=NOW - timedelta(days=40)), {"gate-a1b2": _cam(retention_minutes=60)},
     "outside_retention"),
])
def test_prepare_rejects_per_segment(seg, cams, reason):
    rows, rejected = timeline.prepare([_seg(**seg), _seg()], cams or {GATE.slug: GATE}, now=NOW)
    assert [(r["index"], r["reason"]) for r in rejected] == [(0, reason)]
    assert len(rows) == 1, "one bad segment must not cost the rest of the batch"


def test_the_segment_bound_matches_the_tables_check():
    """migration 043's CHECK says 10 minutes; the ingest must refuse first."""
    assert timeline.MAX_SEGMENT == timedelta(minutes=10)


# ── merge resolution ─────────────────────────────────────────────────────────

def test_merge_gap_is_two_pixels_and_never_below_the_floor():
    day = (NOW - timedelta(days=1), NOW)
    assert timeline.merge_gap_seconds(*day, px=1200) == pytest.approx(144.0)
    ten_min = (NOW - timedelta(minutes=10), NOW)
    assert timeline.merge_gap_seconds(*ten_min, px=1200) == timeline.MIN_MERGE_GAP_S


# ── HTTP ─────────────────────────────────────────────────────────────────────

class _Registry(StubSession):
    def __init__(self, cameras):
        super().__init__()
        self.cameras = cameras

    async def execute(self, statement=None, *a, **kw):
        if "FROM cameras" in str(statement):
            cams = self.cameras
            return SimpleNamespace(scalars=lambda: SimpleNamespace(
                all=lambda: list(cams), first=lambda: cams[0] if cams else None))
        return await super().execute(statement, *a, **kw)


@pytest.fixture
def db():
    return _Registry([GATE])


def _wire(**kw):
    s = _seg(**kw)
    return {"sensor_id": s.sensor_id, "kind": s.kind,
            "started_at": s.started_at.isoformat(), "ended_at": s.ended_at.isoformat()}


@pytest.mark.asyncio
async def test_ingest_is_for_the_analytics_service_only(client, mint):
    body = {"segments": [_wire()]}
    assert (await client.post(f"{URL}/presence", json=body)).status_code == 401
    r = await client.post(f"{URL}/presence", json=body, headers=mint(["admin"]))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_ingest_stores_and_reports_rejections(client, monkeypatch):
    stored = []

    async def record(db, rows):
        stored.extend(rows)
        return {"inserted": len(rows), "duplicates": 0}

    monkeypatch.setattr(timeline, "record", record)
    now = datetime.now(timezone.utc)
    r = await client.post(f"{URL}/presence", headers=KEY, json={"segments": [
        _wire(start=now - timedelta(minutes=1)),
        _wire(start=now - timedelta(minutes=1), sensor_id="nobody-0000"),
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["inserted"] == 1
    assert [(x["index"], x["reason"]) for x in body["rejected"]] == [(1, "unknown_camera")]
    assert stored[0]["camera_id"] == GATE.id


@pytest.mark.asyncio
async def test_reading_presence_needs_ai_analytics(client, mint, monkeypatch):
    async def presence(db, cid, frm, to, kinds, px):
        return {"merge_gap_s": 3.0, "kinds": {k: [] for k in kinds}, "truncated": False}

    monkeypatch.setattr(timeline, "presence", presence)
    params = {"camera": "gate-a1b2", "from": "2026-09-23T10:00:00Z", "to": "2026-09-23T12:00:00Z"}
    assert (await client.get(URL, params=params)).status_code == 401
    assert (await client.get(URL, params=params, headers=mint(["viewer"]))).status_code == 403
    r = await client.get(URL, params=params, headers=mint(["operator"]))
    assert r.status_code == 200
    body = r.json()
    assert body["kinds"] == {"person": [], "vehicle": [], "motion": []}
    assert body["indexed"] == ["person", "vehicle"]


@pytest.mark.asyncio
async def test_the_window_and_kinds_reach_the_query(client, mint, monkeypatch):
    seen = {}

    async def presence(db, cid, frm, to, kinds, px):
        seen.update(cid=cid, frm=frm, to=to, kinds=kinds, px=px)
        return {"merge_gap_s": 3.0, "kinds": {k: [] for k in kinds}, "truncated": False}

    monkeypatch.setattr(timeline, "presence", presence)
    r = await client.get(URL, headers=mint(["operator"]), params={
        "camera": "gate-a1b2", "from": "2026-09-23T10:00:00Z",
        "to": "2026-09-23T18:00:00+05:30", "kinds": "vehicle", "px": 900})
    assert r.status_code == 200
    assert seen["cid"] == GATE.id and seen["kinds"] == ["vehicle"] and seen["px"] == 900
    assert seen["to"] == datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize("params, code", [
    ({"from": "2026-09-23T12:00:00Z", "to": "2026-09-23T10:00:00Z"}, 400),
    ({"from": "2026-09-23T10:00:00", "to": "2026-09-23T12:00:00Z"}, 400),
    ({"from": "2026-09-01T00:00:00Z", "to": "2026-09-23T00:00:00Z"}, 400),
    ({"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T12:00:00Z", "kinds": "face"}, 400),
    ({"from": "2026-09-23T10:00:00Z", "to": "2026-09-23T12:00:00Z", "px": 5}, 422),
])
async def test_bad_windows_are_refused(client, mint, params, code):
    r = await client.get(URL, headers=mint(["operator"]), params={"camera": "gate-a1b2", **params})
    assert r.status_code == code


@pytest.mark.asyncio
async def test_an_unknown_camera_is_a_404(client, mint, db):
    db.cameras = []
    r = await client.get(URL, headers=mint(["operator"]), params={
        "camera": "nobody-0000", "from": "2026-09-23T10:00:00Z", "to": "2026-09-23T12:00:00Z"})
    assert r.status_code == 404


# ── next / previous ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_next_needs_ai_analytics_and_passes_its_arguments(client, mint, monkeypatch):
    seen = {}

    async def adjacent(db, cid, at, direction, kinds, events, hidden):
        seen.update(cid=cid, at=at, direction=direction, kinds=kinds, events=events,
                    hidden=hidden)
        return {"row": "person", "activity": None, "start": 1.0, "end": 2.0, "seek_at": -2.0}

    monkeypatch.setattr(timeline, "adjacent", adjacent)
    params = {"camera": "gate-a1b2", "at": "2026-09-23T10:00:00Z", "dir": "prev",
              "kinds": "person,motion", "events": "false", "hide": "a,b"}
    assert (await client.get(f"{URL}/next", params=params)).status_code == 401
    assert (await client.get(f"{URL}/next", params=params,
                             headers=mint(["viewer"]))).status_code == 403
    r = await client.get(f"{URL}/next", params=params, headers=mint(["operator"]))
    assert r.status_code == 200 and r.json()["marker"]["row"] == "person"
    assert seen["cid"] == GATE.id and seen["direction"] == "prev"
    assert seen["kinds"] == ["person", "motion"] and seen["events"] is False
    assert seen["hidden"] == ["a", "b"]


@pytest.mark.asyncio
async def test_next_with_no_kinds_is_events_only(client, mint, monkeypatch):
    seen = {}

    async def adjacent(db, cid, at, direction, kinds, events, hidden):
        seen["kinds"] = kinds
        return None

    monkeypatch.setattr(timeline, "adjacent", adjacent)
    r = await client.get(f"{URL}/next", headers=mint(["operator"]), params={
        "camera": "gate-a1b2", "at": "2026-09-23T10:00:00Z", "kinds": ""})
    assert r.status_code == 200 and r.json()["marker"] is None
    assert seen["kinds"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"at": "2026-09-23T10:00:00"},
    {"at": "2026-09-23T10:00:00Z", "dir": "sideways"},
    {"at": "2026-09-23T10:00:00Z", "kinds": "face"},
])
async def test_next_refuses_bad_arguments(client, mint, params):
    r = await client.get(f"{URL}/next", headers=mint(["operator"]),
                         params={"camera": "gate-a1b2", **params})
    assert r.status_code in (400, 422)
