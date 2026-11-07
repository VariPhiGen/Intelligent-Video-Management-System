"""The AI engine switch: CPU analytics or DeepStream, never both.

DEEPSTREAM_SYNC_ENABLED decides. Off (the default), everything stays exactly as
it was: CPU analytics detects for Smart Search, reports playback presence and
runs activities, and the pipeline only receives zone/activity config. On,
DeepStream does all of it and posts back — and the VMS must (a) stop handing
cameras to CPU analytics, (b) hand the pipeline every camera it must detect
for, with what to report and where, and (c) accept results only from the
engine that owns the camera, so no object is ever counted twice.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import StubSession
from backend.config import settings
from backend.models import CameraStage
from backend.services import analytics_client, deepstream, deepstream_client, detection_engine
from backend.services import frames_client, timeline

KEY = {"X-Internal-Key": "test-internal-key"}
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
ZONE_CFG = {"regions": {"r1": {"kind": "zone", "name": "Z", "color": "#fff",
                               "points": [[0, 0], [1, 0], [1, 1]]}},
            "activities": [{"type": "loitering", "regions": ["r1"], "params": {}}]}


def cam(**kw):
    base = dict(id=uuid.uuid4(), slug="gate-a1b2", name="Gate", enabled=True,
                stage=CameraStage.REGISTERED.value, retention_minutes=None,
                search_indexing=True, search_domains=["person", "vehicles"],
                motion_detection=False, analytics_config={})
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def ds_on(monkeypatch):
    monkeypatch.setattr(settings, "deepstream_sync_enabled", True)


@pytest.fixture
def ds_off(monkeypatch):
    monkeypatch.setattr(settings, "deepstream_sync_enabled", False)


# ── The switch itself ────────────────────────────────────────────────────────

def test_cpu_is_the_default_engine(ds_off):
    assert detection_engine.engine() == "cpu"


def test_the_deepstream_switch_makes_it_the_engine(ds_on):
    assert detection_engine.engine() == "deepstream"


# ── CPU analytics gets cameras only while it is the engine ───────────────────

def test_cpu_mode_keeps_the_old_rule(ds_off):
    assert analytics_client.wants(True, True, ["person"], {})
    assert analytics_client.wants(True, False, [], ZONE_CFG)
    assert not analytics_client.wants(True, False, ["person"], {})
    assert not analytics_client.wants(False, True, ["person"], ZONE_CFG)


def test_deepstream_mode_hands_cpu_analytics_nothing(ds_on):
    for domains in (["person"], ["vehicles", "plate"], ["face"]):
        assert not analytics_client.wants(True, True, domains, ZONE_CFG)
    desired, absent = analytics_client.desired_sets(
        [("a", True, True, ["person"], {}), ("b", True, False, [], ZONE_CFG)])
    assert desired == {} and absent == {"a", "b"}


def test_the_frame_broker_keeps_decoding_only_for_motion_in_deepstream_mode(ds_on):
    desired, absent = frames_client.desired_sets([
        ("motion-cam", True, True, False, [], {}),
        ("ai-cam", True, False, True, ["person"], ZONE_CFG),
    ])
    assert desired == {"motion-cam"} and "ai-cam" in absent


# ── The pipeline gets every camera it must detect for ────────────────────────

def test_cpu_mode_projects_only_zone_cameras_and_the_envelope_is_unchanged(ds_off):
    assert not deepstream_client.should_project(cam())
    zoned = cam(analytics_config=ZONE_CFG)
    assert deepstream_client.should_project(zoned)
    assert "vms_ingest" not in deepstream.deepstream_config(zoned)


def test_deepstream_mode_projects_indexed_cameras_too(ds_on):
    assert deepstream_client.should_project(cam())
    assert not deepstream_client.should_project(cam(search_indexing=False))
    assert not deepstream_client.should_project(cam(search_domains=[]))
    assert not deepstream_client.should_project(cam(enabled=False))


def test_the_envelope_says_what_to_report_and_where(ds_on, monkeypatch):
    monkeypatch.setattr(settings, "api_port", 8097)
    monkeypatch.setattr(settings, "deepstream_callback_url", "")
    env = deepstream.deepstream_config(cam(search_domains=["person", "vehicles", "plate", "face"],
                                           analytics_config=ZONE_CFG))
    ing = env["vms_ingest"]
    assert ing["engine"] == "deepstream"
    assert ing["search_domains"] == ["person", "vehicles", "plate", "face"]
    assert ing["presence_kinds"] == ["person", "vehicle"]
    assert ing["activities"] is True
    # A zone activity is neither a heatmap nor a distribution: each flag gates a
    # separate channel, and a camera configured for one must not be told to
    # produce the others.
    assert ing["heatmap"] is False
    assert ing["distribution"] is False
    assert ing["endpoints"] == {
        "observations": "http://127.0.0.1:8097/api/search/observations",
        "heartbeat": "http://127.0.0.1:8097/api/search/producers/deepstream/heartbeat",
        "presence": "http://127.0.0.1:8097/api/playback/timeline/presence",
        "events": "http://127.0.0.1:8097/api/analytics/events",
        "heatmaps": "http://127.0.0.1:8097/api/analytics/heatmaps",
        "distribution": "http://127.0.0.1:8097/api/analytics/people-distribution",
    }


HEATMAP_CFG = {"regions": {},
               "activities": [{"type": "Heatmap_overlay", "regions": [], "params": {}}]}


def test_the_heatmap_is_pushed_as_cumulative(ds_on):
    """The catalog DEFINES this activity as cumulative -- "nothing decays",
    accumulated "for as long as the heatmap runs on this camera". The pipeline's
    implementation defaults to decay=0.95, a 0.7s half-life at 20fps, so the
    VMS sends the only value consistent with its own contract."""
    cfg = deepstream.deepstream_config(cam(analytics_config=HEATMAP_CFG))
    params = cfg["activities_data"]["Heatmap_overlay"]["parameters"]
    assert params["decay"] == 1.0, f"decay is pushed as 1.0, got {params['decay']}"

    # An engine parameter, not an operator one: it must not leak onto other
    # activities, which have no such concept.
    zone = deepstream.deepstream_config(cam(analytics_config=ZONE_CFG))
    other = zone["activities_data"]["loitering"]["parameters"]
    assert "decay" not in other, f"decay leaked onto loitering: {other}"


def test_the_heatmap_flag_follows_the_configured_activity(ds_on):
    """A heatmap is a picture, not an event, so it has its own flag and its own
    endpoint — and a camera with only zone activities must not be told to
    render one."""
    assert deepstream.deepstream_config(
        cam(analytics_config=HEATMAP_CFG))["vms_ingest"]["heatmap"] is True
    assert deepstream.deepstream_config(
        cam(analytics_config=ZONE_CFG))["vms_ingest"]["heatmap"] is False
    assert deepstream.deepstream_config(
        cam())["vms_ingest"]["heatmap"] is False


def test_a_remote_pipeline_is_given_the_configured_callback(ds_on, monkeypatch):
    monkeypatch.setattr(settings, "deepstream_callback_url", "https://vms.example:8443/")
    ing = deepstream.deepstream_config(cam())["vms_ingest"]
    assert ing["endpoints"]["presence"] == "https://vms.example:8443/api/playback/timeline/presence"


def test_an_activities_only_camera_reports_no_detections(ds_on):
    ing = deepstream.deepstream_config(cam(search_indexing=False, analytics_config=ZONE_CFG))["vms_ingest"]
    assert ing["search_domains"] == [] and ing["presence_kinds"] == [] and ing["activities"] is True


# ── Only the owning engine may write ─────────────────────────────────────────

def _seg(**kw):
    start = NOW - timedelta(minutes=2)
    return timeline.PresenceIn(**{"sensor_id": "gate-a1b2", "kind": "person", "started_at": start,
                                  "ended_at": start + timedelta(seconds=30), **kw})


@pytest.mark.parametrize("mode, source, ok", [
    ("ds_off", "cpu", True), ("ds_off", "deepstream", False),
    ("ds_on", "deepstream", True), ("ds_on", "cpu", False),
])
def test_presence_is_accepted_only_from_the_engine(request, mode, source, ok):
    request.getfixturevalue(mode)
    c = cam()
    rows, rejected = timeline.prepare([_seg(source=source)], {c.slug: c}, now=NOW)
    assert bool(rows) is ok
    assert [r["reason"] for r in rejected] == ([] if ok else ["wrong_source"])


@pytest.mark.parametrize("mode", ["ds_off", "ds_on"])
def test_motion_presence_comes_from_the_motion_service_in_both_modes(request, mode):
    request.getfixturevalue(mode)
    c = cam(motion_detection=True)
    rows, _ = timeline.prepare([_seg(kind="motion", source="motion")], {c.slug: c}, now=NOW)
    assert rows
    _, rejected = timeline.prepare([_seg(kind="motion", source="deepstream")], {c.slug: c}, now=NOW)
    assert [r["reason"] for r in rejected] == ["wrong_source"]


def test_observation_rules(ds_on):
    c = cam(search_domains=["person", "vehicles"])
    assert detection_engine.observation_refusal(c, "person", False) is None
    assert detection_engine.observation_refusal(c, "face", False) == "domain_not_indexed"
    assert detection_engine.observation_refusal(c, "vehicles", True) == "plate_not_indexed"
    assert detection_engine.observation_refusal(
        cam(search_domains=["vehicles", "plate"]), "vehicles", True) is None
    assert detection_engine.observation_refusal(
        cam(search_domains=["face"]), "face", False) is None
    assert detection_engine.observation_refusal(
        cam(search_indexing=False), "person", False) == "camera_not_indexed"


def test_observations_are_refused_while_cpu_is_the_engine(ds_off):
    assert detection_engine.observation_refusal(cam(), "person", False) == "deepstream_not_engine"


@pytest.mark.parametrize("mode, source, ok", [
    ("ds_off", "cpu", True), ("ds_off", "deepstream", False),
    ("ds_on", "deepstream", True), ("ds_on", "cpu", False),
])
def test_heatmaps_are_accepted_only_from_the_engine(request, mode, source, ok):
    """Both sides can render `Heatmap_overlay`, so the camera's history would
    otherwise take pictures from an engine that no longer owns it."""
    request.getfixturevalue(mode)
    assert detection_engine.heatmap_source_ok(
        cam(analytics_config=HEATMAP_CFG), source) is ok


def test_the_heatmap_gate_asks_who_not_what_is_configured(ds_on):
    """DELIBERATELY not a configuration check. A producer that has just lost the
    activity still has a session to close, and the close is the message that
    says so — refusing it would strand the session until the stale sweep. The
    engine is the only question this gate answers."""
    assert detection_engine.heatmap_source_ok(cam(analytics_config={}), "deepstream") is True
    assert detection_engine.heatmap_configured(cam(analytics_config={}).analytics_config) is False


# ── The observation door over HTTP ───────────────────────────────────────────

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
    return _Registry([cam()])


class _Index:
    """Stands in for the Smart Search index's /observations and heartbeat."""

    def __init__(self, status=200, body=None):
        self.calls = []
        self.status, self.body = status, body or {"accepted": True, "rows_written": 1,
                                                  "deduped": False}

    def client(self, **kw):
        index = self

        class C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                index.calls.append((url, kw))
                return SimpleNamespace(status_code=index.status, json=lambda: index.body,
                                       text=json.dumps(index.body))
        return C()


@pytest.fixture
def index(monkeypatch):
    from backend.routers import search_ingest
    monkeypatch.setattr(settings, "smartsearch_index_url", "http://127.0.0.1:8013")
    fake = _Index()
    monkeypatch.setattr(search_ingest.httpx, "AsyncClient", fake.client)
    return fake


def _obs(**meta):
    m = {"camera": "gate-a1b2", "ts": NOW.timestamp(), "domain": "person", "confidence": 0.9,
         "bbox": [0.1, 0.1, 0.3, 0.6], "tracker_id": "ds:42", **meta}
    return {"data": {"meta": json.dumps(m)},
            "files": {"crop": ("c.png", b"\x89PNG-crop", "image/png"),
                      "frame": ("f.jpg", b"\xff\xd8frame", "image/jpeg")}}


URL = "/api/search/observations"


@pytest.mark.asyncio
async def test_the_door_is_for_the_pipeline_only(client, mint, ds_on, index):
    assert (await client.post(URL, **_obs())).status_code == 401
    assert (await client.post(URL, headers=mint(["admin"]), **_obs())).status_code == 403
    assert index.calls == []


@pytest.mark.asyncio
async def test_the_door_is_shut_while_cpu_is_the_engine(client, ds_off, index):
    r = await client.post(URL, headers=KEY, **_obs())
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "deepstream_not_engine"
    assert index.calls == []


@pytest.mark.asyncio
async def test_an_observation_is_forwarded_to_the_index_untouched(client, ds_on, index):
    r = await client.post(URL, headers=KEY, **_obs())
    assert r.status_code == 200 and r.json()["rows_written"] == 1
    [(url, kw)] = index.calls
    assert url == "http://127.0.0.1:8013/observations"
    assert json.loads(kw["data"]["meta"])["tracker_id"] == "ds:42"
    assert kw["files"]["crop"][1] == b"\x89PNG-crop"
    assert kw["files"]["frame"][1] == b"\xff\xd8frame"


@pytest.mark.asyncio
@pytest.mark.parametrize("meta, reason", [
    ({"camera": "nobody-0000"}, "unknown_camera"),
    ({"domain": "face"}, "domain_not_indexed"),
    ({"domain": "vehicles", "plate": "KA01AB1234"}, "plate_not_indexed"),
])
async def test_what_ai_config_does_not_ask_for_is_refused(client, ds_on, index, db, meta, reason):
    if meta.get("camera") == "nobody-0000":
        db.cameras = []
    r = await client.post(URL, headers=KEY, **_obs(**meta))
    assert r.status_code == 422 and r.json()["detail"]["reason"] == reason
    assert index.calls == []


@pytest.mark.asyncio
async def test_the_indexs_own_refusal_is_passed_back(client, ds_on, monkeypatch):
    from backend.routers import search_ingest
    monkeypatch.setattr(settings, "smartsearch_index_url", "http://127.0.0.1:8013")
    fake = _Index(status=503, body={"detail": "ingest not ready"})
    monkeypatch.setattr(search_ingest.httpx, "AsyncClient", fake.client)
    r = await client.post(URL, headers=KEY, **_obs())
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_no_index_deployed_is_a_503(client, ds_on, monkeypatch):
    monkeypatch.setattr(settings, "smartsearch_index_url", "")
    assert (await client.post(URL, headers=KEY, **_obs())).status_code == 503


@pytest.mark.asyncio
async def test_the_heartbeat_reaches_the_index(client, ds_on, index):
    r = await client.post("/api/search/producers/deepstream/heartbeat", headers=KEY,
                          json={"fps": 25})
    assert r.status_code == 200
    [(url, kw)] = index.calls
    assert url == "http://127.0.0.1:8013/producers/deepstream/heartbeat"
    assert kw["json"] == {"fps": 25}
    bad = await client.post("/api/search/producers/Bad Name/heartbeat", headers=KEY, json={})
    assert bad.status_code == 422
