"""Time-lapse over HTTP: who may look, who may switch it on, and what a bad setting gets back.

Two grantable capabilities gate /api/timelapse — `timelapse_view` and
`timelapse_manage` — and both only exist once a request is routed, so they are
asserted through the real app (conftest's `client`), with real RS256 tokens.

Defaults under test (services/policy.DEFAULT_POLICY):
    view    admin, supervisor, operator        (not viewer, not dpo)
    manage  admin, supervisor

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from conftest import StubSession

pytestmark = pytest.mark.asyncio

GATE = SimpleNamespace(id=uuid.uuid4(), slug="gate-a1b2", name="Gate", enabled=True,
                       timelapse_enabled=False, timelapse_config={}, privacy_masks=[[[0, 0], [1, 0], [1, 1]]],
                       ptz_capabilities=None)


class _Registry(StubSession):
    """Answers the camera lookup with GATE and the frame totals with zeros."""

    async def execute(self, statement=None, *a, **kw):
        sql = str(statement)
        if "FROM cameras" in sql and "timelapse_frames" not in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: GATE, scalar=lambda: GATE,
                                   scalars=lambda: SimpleNamespace(all=lambda: [GATE], first=lambda: GATE),
                                   all=lambda: [], first=lambda: None)
        if "timelapse_frames" in sql and "count" in sql.lower():
            return SimpleNamespace(one=lambda: (0, 0, None), scalar=lambda: 0, all=lambda: [])
        return await super().execute(statement, *a, **kw)


@pytest.fixture
def db():
    GATE.timelapse_enabled, GATE.timelapse_config = False, {}
    return _Registry()


URL = "/api/timelapse"


class TestWhoMaySee:
    async def test_anonymous_is_refused(self, client):
        assert (await client.get(f"{URL}/cameras")).status_code == 401

    @pytest.mark.parametrize("role", ["viewer", "dpo"])
    async def test_roles_without_the_grant_are_refused(self, client, mint, role):
        r = await client.get(f"{URL}/cameras", headers=mint([role]))
        assert r.status_code == 403
        assert "Time-lapse" in r.json()["detail"]

    @pytest.mark.parametrize("role", ["operator", "supervisor", "admin"])
    async def test_working_roles_see_the_list(self, client, mint, role):
        r = await client.get(f"{URL}/cameras", headers=mint([role]))
        assert r.status_code == 200
        assert r.json()["cameras"][0]["slug"] == "gate-a1b2"

    async def test_a_camera_reads_back_with_defaults_and_an_estimate(self, client, mint):
        r = await client.get(f"{URL}/cameras/{GATE.id}", headers=mint(["operator"]))
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False and body["config"]["interval_s"] == 300
        assert body["estimate"]["frames_per_day"] > 0 and body["privacy_masks"] == 1


class TestWhoMaySwitchItOn:
    async def test_an_operator_may_look_but_not_switch(self, client, mint):
        r = await client.put(f"{URL}/cameras/{GATE.id}", json={"enabled": True, "config": {}},
                             headers=mint(["operator"]))
        assert r.status_code == 403
        assert GATE.timelapse_enabled is False

    async def test_a_supervisor_switches_it_on_and_only_the_change_is_stored(self, client, mint):
        r = await client.put(f"{URL}/cameras/{GATE.id}", json={"enabled": True, "config": {"interval_s": 600}},
                             headers=mint(["supervisor"]))
        assert r.status_code == 200
        assert GATE.timelapse_enabled is True
        # Only what differs from the site defaults: the rest follows the Defaults page.
        assert GATE.timelapse_config == {"interval_s": 600}
        body = r.json()
        assert body["config"]["retention_days"] == 90 and body["changed"] == ["interval_s"]

    @pytest.mark.parametrize("bad", [{"interval_s": 5}, {"resolution": "4k"}, {"nope": 1}])
    async def test_a_bad_setting_is_a_422_that_names_the_field(self, client, mint, bad):
        r = await client.put(f"{URL}/cameras/{GATE.id}", json={"enabled": True, "config": bad},
                             headers=mint(["admin"]))
        assert r.status_code == 422
        assert GATE.timelapse_enabled is False


class TestEstimateAndRenders:
    async def test_the_estimate_needs_only_view(self, client, mint):
        r = await client.post(f"{URL}/estimate", json={"config": {"interval_s": 60, "schedule": None}},
                              headers=mint(["operator"]))
        assert r.status_code == 200 and r.json()["frames_per_day"] == 1440

    async def test_a_render_with_the_range_backwards_is_refused_plainly(self, client, mint):
        r = await client.post(f"{URL}/cameras/{GATE.id}/renders",
                              json={"start": "2026-10-12T10:00:00Z", "end": "2026-10-12T09:00:00Z"},
                              headers=mint(["operator"]))
        assert r.status_code == 422 and "after the start" in r.json()["detail"]

    async def test_deleting_a_render_needs_manage(self, client, mint):
        r = await client.delete(f"{URL}/renders/{uuid.uuid4()}", headers=mint(["operator"]))
        assert r.status_code == 403


class TestPhase3:
    """The progress strip a cloud site-map pulls (over X-Internal-Key) and the preset picker."""

    async def test_daily_is_view_gated_and_the_internal_key_reads_it(self, client, mint, monkeypatch):
        from backend.services import timelapse as tl

        async def daily(db, cam_id, since=None, limit=400):
            assert since is not None and since.isoformat() == "2026-10-01"
            return [{"day": "2026-10-01", "frame_id": 7, "captured_at": "2026-10-01T06:30:00+00:00",
                     "width": 1920, "height": 1080}]

        monkeypatch.setattr(tl, "daily", daily)
        r = await client.get(f"{URL}/cameras/{GATE.id}/daily?since=2026-10-01", headers=mint(["viewer"]))
        assert r.status_code == 403
        r = await client.get(f"{URL}/cameras/{GATE.id}/daily?since=2026-10-01",
                             headers={"X-Internal-Key": "test-internal-key"})
        assert r.status_code == 200 and r.json()["days"][0]["frame_id"] == 7

    async def test_presets_need_manage_not_ptz_control(self, client, mint, monkeypatch):
        from backend.services import onvif_ptz, ptz_control

        async def target(slug):
            return ("10.0.0.5", 80, "u", "p", None)

        async def presets(*a):
            return [{"token": "1", "name": "Tower", "pan": 0.1}, {"token": None, "name": "broken"}]

        monkeypatch.setattr(ptz_control, "_resolve_target", target)
        monkeypatch.setattr(onvif_ptz, "get_presets", presets)
        assert (await client.get(f"{URL}/cameras/{GATE.id}/presets", headers=mint(["operator"]))).status_code == 403
        r = await client.get(f"{URL}/cameras/{GATE.id}/presets", headers=mint(["supervisor"]))
        assert r.status_code == 200 and r.json() == {"presets": [{"token": "1", "name": "Tower"}]}

    async def test_presets_without_credentials_is_a_plain_409(self, client, mint, monkeypatch):
        from backend.services import ptz_control

        async def none(slug):
            return None

        monkeypatch.setattr(ptz_control, "_resolve_target", none)
        r = await client.get(f"{URL}/cameras/{GATE.id}/presets", headers=mint(["admin"]))
        assert r.status_code == 409 and "credentials" in r.json()["detail"]
