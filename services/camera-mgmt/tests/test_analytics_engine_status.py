"""AI detection status: admin-only, read-only, and honest about failure."""
from types import SimpleNamespace

import httpx
import pytest

from backend.routers import analytics_engine
from backend.services import analytics_client

#: The shape the analytics service's /health returns (trimmed to what is read).
HEALTH = {
    "state": "RUNNING", "last_error": None, "total_cameras": 6,
    "models": {
        "detector_choice": {"weights": "/models/weights/yolov8s.pt", "mode": "auto",
                            "reason": "yolov8s.pt keeps up: 9.6 fps measured vs 6.2 fps needed",
                            "capacity_fps": 9.6, "demand_fps": 6.2, "over_capacity": False},
        "detector_backend": {"runtime": "torch", "device": "cpu"},
        "backend_selection": {"failures": [
            "openvino: probe crashed with SIGILL — the CPU or its VM rejects an instruction the runtime uses"]},
    },
    "lifecycle": {"lifetime": {"frames_processed": 38, "frames_skipped_no_motion": 10725,
                               "frames_evicted": 0, "detections_seen": 4}},
    "pipeline": {"frames_evicted": 0},
}


@pytest.fixture
def analytics(monkeypatch):
    state = {"health": HEALTH, "error": None}

    class Client:
        async def get(self, url):
            if state["error"]:
                raise state["error"]
            return httpx.Response(200, json=state["health"], request=httpx.Request("GET", url))
    monkeypatch.setattr(analytics_client, "is_configured", lambda: True)
    monkeypatch.setattr(analytics_client, "_base", lambda: "http://analytics:8015")
    monkeypatch.setattr(analytics_client, "_get_client", lambda: Client())
    return state


def test_summary_names_the_model_runtime_and_why():
    s = analytics_engine.summarise(HEALTH)
    assert (s["model"], s["mode"], s["runtime"], s["cameras"]) == ("yolov8s", "auto", "torch", 6)
    assert s["capacity_fps"] == 9.6 and not s["over_capacity"]
    assert any("OpenVINO runtime cannot run on this machine" in w and "SIGILL" in w
               for w in s["warnings"])


def test_over_capacity_is_a_warning_an_admin_can_read():
    h = {**HEALTH, "models": {**HEALTH["models"],
                              "detector_choice": {**HEALTH["models"]["detector_choice"],
                                                  "over_capacity": True}}}
    assert any("cannot keep up" in w for w in analytics_engine.summarise(h)["warnings"])


def test_no_openvino_warning_when_openvino_is_what_runs():
    h = {**HEALTH, "models": {**HEALTH["models"], "detector_backend": {"runtime": "openvino"}}}
    assert analytics_engine.summarise(h)["warnings"] == []


@pytest.mark.asyncio
async def test_admin_gets_the_summary(client, mint, analytics):
    r = await client.get("/api/analytics/engine", headers=mint(roles=["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "yolov8s" and r.json()["reachable"] is True


@pytest.mark.asyncio
async def test_non_admins_and_anonymous_are_refused(client, mint, analytics):
    assert (await client.get("/api/analytics/engine", headers=mint(roles=["viewer"]))).status_code == 403
    assert (await client.get("/api/analytics/engine")).status_code == 401


@pytest.mark.asyncio
async def test_an_unreachable_service_is_reported_not_hidden(client, mint, analytics):
    analytics["error"] = httpx.ConnectError("connection refused")
    r = await client.get("/api/analytics/engine", headers=mint(roles=["admin"]))
    assert r.status_code == 200
    assert r.json()["reachable"] is False and "not responding" in r.json()["warnings"][0]


@pytest.mark.asyncio
async def test_not_deployed_is_its_own_state(client, mint, monkeypatch):
    monkeypatch.setattr(analytics_client, "is_configured", lambda: False)
    r = await client.get("/api/analytics/engine", headers=mint(roles=["admin"]))
    assert r.json()["configured"] is False
