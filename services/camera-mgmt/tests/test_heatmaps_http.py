"""Heatmap overlays over HTTP: who may upload, who may read, who may switch it off.

Three different callers, three different gates. Only the analytics service may
upload a heatmap — a signed-in user, admin included, never can. Reading one
needs `ai_analytics`, because a heatmap is a record of where people were.
Switching a camera's heatmap off is a configuration change on that camera, so
it needs `camera_manage`. All three only exist once a request is routed, so
they are asserted through the real app (conftest's `client`).

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from conftest import StubSession

pytestmark = pytest.mark.asyncio

KEY = {"X-Internal-Key": "test-internal-key"}
URL = "/api/analytics/heatmaps"
JPEG = b"\xff\xd8\xff\xe0heatmap-bytes"

HEAT_CONFIG = {
    "regions": {},
    "activities": [{"type": "Heatmap_overlay", "regions": [],
                    "params": {"classes_for_heatmap": ["person"], "intensity": 1.0,
                               "radius": 40}}],
}
PLAIN_CONFIG = {"regions": {}, "activities": [{"type": "people_gathering", "regions": []}]}

GATE = SimpleNamespace(id=uuid.uuid4(), slug="gate-a1b2", name="Gate", enabled=True,
                       retention_minutes=None, health_status="online",
                       analytics_config=HEAT_CONFIG)
YARD = SimpleNamespace(id=uuid.uuid4(), slug="yard-c3d4", name="Yard", enabled=True,
                       retention_minutes=None, health_status="online",
                       analytics_config=PLAIN_CONFIG)


class _Registry(StubSession):
    """StubSession that answers camera lookups from a list; everything else empty."""

    def __init__(self, cameras):
        super().__init__()
        self.cameras = cameras

    async def execute(self, statement=None, *a, **kw):
        sql = str(statement)
        if "FROM cameras" in sql and "heatmap" not in sql:
            cams = self.cameras
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    all=lambda: list(cams), first=lambda: cams[0] if cams else None),
                all=lambda: [])
        return await super().execute(statement, *a, **kw)


@pytest.fixture(autouse=True)
def cpu_is_the_engine(monkeypatch):
    """Pin the engine: the ingest now asks who is writing, and an unsigned
    message is attributed to the CPU engine (heatmaps.DEFAULT_SOURCE). Without
    this, every upload here would answer 409 whenever the developer's .env has
    DeepStream on. The 409 itself is asserted in TestTheEngineGate below."""
    from backend.config import settings
    monkeypatch.setattr(settings, "deepstream_sync_enabled", False)


@pytest.fixture
def db():
    return _Registry([GATE])


def meta(**kw):
    body = {"kind": "snapshot", "camera": GATE.slug, "session_id": uuid.uuid4().hex,
            "session_started_at": 1789800000.0, "captured_at": 1789800060.0,
            "status": "active", "width": 1280, "height": 720, "points": 42, "peak": 17.5}
    body.update(kw)
    return {"meta": json.dumps(body)}


class TestWhoMayUpload:
    async def test_without_the_internal_key_it_is_refused(self, client):
        r = await client.post(URL, data=meta(), files={"image": ("h.jpg", JPEG, "image/jpeg")})
        assert r.status_code in (401, 403)

    async def test_a_signed_in_admin_still_cannot_upload(self, client, mint):
        r = await client.post(URL, data=meta(), files={"image": ("h.jpg", JPEG, "image/jpeg")},
                              headers=mint(["admin"]))
        assert r.status_code == 403
        assert "analytics pipeline" in r.json()["detail"]

    async def test_meta_that_is_not_json_is_refused_once_not_retried_for_ever(self, client):
        r = await client.post(URL, data={"meta": "not json"}, headers=KEY)
        assert r.status_code == 422          # never 5xx: the sink retries 5xx

    async def test_a_session_id_that_is_not_a_uuid_is_refused(self, client):
        r = await client.post(URL, data=meta(session_id="nope"), headers=KEY)
        assert r.status_code == 422

    async def test_an_unknown_status_is_refused(self, client):
        r = await client.post(URL, data=meta(status="melting"), headers=KEY)
        assert r.status_code == 422


class TestTheEngineGate:
    """A producer that is not the engine gets 409, not 422.

    The distinction is the whole reason `WrongEngine` is its own exception: 422
    means the message is malformed and repeating it is pointless, while this
    message is perfectly good and would be stored the moment the operator
    switched the engine. 409 is also what the observation door already answers
    for `deepstream_not_engine`, so a sink can count the two together -- and
    both are 4xx, so neither is retried in a loop.
    """

    async def test_the_engine_that_does_not_own_the_camera_gets_409(self, client, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "deepstream_sync_enabled", True)
        r = await client.post(URL, data=meta(),        # unsigned = cpu
                              files={"image": ("h.jpg", JPEG, "image/jpeg")}, headers=KEY)
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "source_not_engine"

    async def test_the_owning_engine_is_accepted(self, client, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "deepstream_sync_enabled", True)
        r = await client.post(URL, data=meta(source="deepstream"),
                              files={"image": ("h.jpg", JPEG, "image/jpeg")}, headers=KEY)
        assert r.status_code == 200

    async def test_an_unsigned_message_is_still_the_cpu_engine(self, client):
        r = await client.post(URL, data=meta(),
                              files={"image": ("h.jpg", JPEG, "image/jpeg")}, headers=KEY)
        assert r.status_code == 200


class TestWhoMayRead:
    async def test_reading_needs_the_ai_analytics_capability(self, client, mint):
        r = await client.get(URL, headers=mint(["viewer"]))
        assert r.status_code == 403

    async def test_an_operator_sees_the_list(self, client, mint):
        r = await client.get(URL, headers=mint(["operator"]))
        assert r.status_code == 200
        body = r.json()
        assert "cameras" in body and isinstance(body["cameras"], list)

    async def test_without_a_token_nothing_is_readable(self, client):
        assert (await client.get(URL)).status_code in (401, 403)

    async def test_an_image_that_does_not_exist_is_a_404_not_an_empty_200(self, client, mint):
        r = await client.get(f"{URL}/artifacts/12345/image", headers=mint(["operator"]))
        assert r.status_code == 404

    async def test_an_image_needs_the_same_capability_as_the_list(self, client, mint):
        r = await client.get(f"{URL}/artifacts/12345/image", headers=mint(["viewer"]))
        assert r.status_code == 403

    async def test_sessions_need_the_capability_and_a_real_camera(self, client, mint):
        assert (await client.get(f"{URL}/{GATE.id}/sessions",
                                 headers=mint(["viewer"]))).status_code == 403
        r = await client.get(f"{URL}/{GATE.id}/sessions", headers=mint(["operator"]))
        assert r.status_code == 200 and r.json()["slug"] == GATE.slug


class TestWhoMaySwitchItOff:
    async def test_an_operator_may_not(self, client, mint):
        r = await client.post(f"{URL}/{GATE.id}/runtime", json={"enabled": False},
                              headers=mint(["operator"]))
        assert r.status_code == 403

    async def test_a_supervisor_may(self, client, mint):
        r = await client.post(f"{URL}/{GATE.id}/runtime",
                              json={"enabled": False, "reason": "shift change"},
                              headers=mint(["supervisor"]))
        assert r.status_code == 200
        assert r.json() == {"camera": GATE.slug, "runtime_enabled": False, "mode": "resume"}

    async def test_turning_it_on_carries_the_resume_or_new_choice(self, client, mint):
        r = await client.post(f"{URL}/{GATE.id}/runtime",
                              json={"enabled": True, "mode": "new"},
                              headers=mint(["admin"]))
        assert r.status_code == 200 and r.json()["mode"] == "new"

    async def test_an_unknown_mode_is_refused_before_anything_changes(self, client, mint):
        r = await client.post(f"{URL}/{GATE.id}/runtime",
                              json={"enabled": True, "mode": "sideways"},
                              headers=mint(["admin"]))
        assert r.status_code == 422


class TestACameraTheRegistryDoesNotHave:
    """Nothing is registered, so every lookup comes back empty."""

    @pytest.fixture
    def db(self):
        return _Registry([])

    async def test_an_upload_for_it_is_refused_once(self, client):
        r = await client.post(URL, data=meta(camera="ghost-0000"),
                              files={"image": ("h.jpg", JPEG, "image/jpeg")}, headers=KEY)
        assert r.status_code == 422
        assert "unknown camera" in r.json()["detail"]

    async def test_switching_it_is_a_404(self, client, mint):
        r = await client.post(f"{URL}/{uuid.uuid4()}/runtime", json={"enabled": False},
                              headers=mint(["admin"]))
        assert r.status_code == 404

    async def test_asking_for_its_sessions_is_a_404(self, client, mint):
        r = await client.get(f"{URL}/{uuid.uuid4()}/sessions", headers=mint(["operator"]))
        assert r.status_code == 404


class TestACameraWithoutTheActivity:
    @pytest.fixture
    def db(self):
        return _Registry([YARD])

    async def test_cannot_be_switched_on_or_off(self, client, mint):
        r = await client.post(f"{URL}/{YARD.id}/runtime", json={"enabled": False},
                              headers=mint(["admin"]))
        assert r.status_code == 409
        assert "not configured" in r.json()["detail"]

    async def test_and_does_not_appear_in_the_list(self, client, mint):
        r = await client.get(URL, headers=mint(["operator"]))
        assert r.status_code == 200 and r.json()["cameras"] == []
