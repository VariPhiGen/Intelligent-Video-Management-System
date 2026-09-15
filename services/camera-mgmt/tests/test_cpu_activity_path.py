"""AI Config now executes on the CPU Activity Engine, not on DeepStream.

What changed, and what must not have:

  * saving AI Config hands the stored config to the analytics service (the CPU
    Activity Engine) immediately — and touches DeepStream not at all, because
    its projection is off by default;
  * the DeepStream integration is still there and still switches back on with
    its one flag, untouched;
  * AI Config's own validation is exactly what it was;
  * the event ingest accepts the CPU engine as a producer.

Same shape as test_search_audit.py: the save handler is called directly with
the database and every collaborator stubbed, and only what reached the
collaborators is asserted.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import inspect
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Settings, settings  # noqa: E402
from backend.models import AnalyticsConfigBody  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402
from backend.services import ai_events, analytics_client, deepstream_client  # noqa: E402
from backend.services import deepstream_projection  # noqa: E402

ZONE = {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
        "points": [[0.1, 0.2], [0.9, 0.2], [0.5, 0.9]]}
SAVED = {
    "regions": {"region_1": ZONE},
    "activities": [{"type": "restricted_zone_entry", "regions": ["region_1"],
                    "params": {"active_hours": None, "active_days": ["Mon"], "cooldown_s": 60}}],
}
#: The catalog as the CPU registry syncs it: (key, status, zone rule).
CATALOG = [
    SimpleNamespace(key=key, label=key, status=status, zone_rule=rule, params_schema=[])
    for key, status, rule in (
        ("restricted_zone_entry", "available", "optional"),
        ("people_gathering", "available", "optional"),
        ("stray_parking", "available", "required"),
        ("entry_exit_WLE_logs", "available", "tripwire"),
        ("idle_worker", "hold", "required"),
        ("car_detection", "hold", "required"),
        ("no_person_area", "hold", "required"),
    )
]


class _DB:
    def __init__(self):
        self.commits = 0

    async def execute(self, statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(CATALOG)))

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None


@pytest.fixture
def wired(monkeypatch):
    """The save handler with its camera, audit and response stubbed; the
    analytics push captured; DeepStream wired to FAIL the test if reached."""
    cam = SimpleNamespace(id=uuid.uuid4(), slug="gate-a1b2", enabled=True,
                          search_indexing=False, search_domains=["person"],
                          analytics_config={})
    pushed = []

    async def get_cam(camera_id, db):
        return cam

    async def principal(request):
        return SimpleNamespace(roles=["admin"])

    async def record(*a, **kw):
        return None

    async def add_camera(slug, domains=None, analytics_config=None):
        pushed.append((slug, domains, analytics_config))
        return True

    async def forbidden(*a, **kw):
        pytest.fail("DeepStream was contacted on the CPU activity path")

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router.audit_svc, "record", record)
    monkeypatch.setattr(cam_router, "_build_response", lambda c: c)
    monkeypatch.setattr(analytics_client, "add_camera", add_camera)
    monkeypatch.setattr(settings, "analytics_api_url", "http://127.0.0.1:8015")
    monkeypatch.setattr(settings, "analytics_sync_enabled", True)
    # PINNED, not assumed. The default is off (asserted below), but settings
    # are read from the environment, and an appliance that still projects to
    # DeepStream sets DEEPSTREAM_PROJECTION_ENABLED=true — so inside vms_api,
    # which is where scripts/as-if-open-core.sh runs this suite, the default
    # never applies and every test here failed for a reason that is not the
    # code under test.
    monkeypatch.setattr(settings, "deepstream_projection_enabled", False)
    monkeypatch.setattr(deepstream_client, "apply_camera", forbidden)
    monkeypatch.setattr(deepstream_client, "remove_camera", forbidden)
    monkeypatch.setattr(deepstream_projection, "project_now", forbidden)
    return SimpleNamespace(cam=cam, pushed=pushed)


async def _save(cam, body):
    return await cam_router.update_analytics_config(
        cam.id, AnalyticsConfigBody(**body), SimpleNamespace(), _DB())


# ── DeepStream is disconnected ──────────────────────────────────────────────

def test_the_deepstream_projection_is_off_by_default():
    assert Settings.model_fields["deepstream_projection_enabled"].default is False


@pytest.mark.asyncio
async def test_saving_ai_config_reaches_the_cpu_engine_and_never_deepstream(wired, monkeypatch):
    monkeypatch.setattr(settings, "deepstream_projection_enabled", False)
    await _save(wired.cam, SAVED)
    assert wired.cam.analytics_config == AnalyticsConfigBody(**SAVED).model_dump()
    [(slug, domains, cfg)] = wired.pushed
    assert slug == "gate-a1b2"
    assert domains == []            # not indexed: running an activity starts no indexing
    assert cfg["activities"][0]["type"] == "restricted_zone_entry"
    assert cfg["regions"]["region_1"]["points"] == ZONE["points"]


@pytest.mark.asyncio
async def test_removing_the_last_activity_stops_analysing_the_camera(wired, monkeypatch):
    removed = []

    async def remove(slug):
        removed.append(slug)
        return True

    monkeypatch.setattr(analytics_client, "remove_camera", remove)
    await _save(wired.cam, {"regions": {"region_1": ZONE}, "activities": []})
    assert wired.pushed == [] and removed == ["gate-a1b2"]


@pytest.mark.asyncio
async def test_the_deepstream_integration_still_switches_back_on(monkeypatch):
    """Disconnected, not deleted: the flag alone brings the projection back."""
    reached = []

    async def project_now(reason):
        reached.append(reason)
        return SimpleNamespace(locked_out=True, removed_slugs=[], written_slugs=[])

    monkeypatch.setattr(deepstream_projection, "project_now", project_now)
    monkeypatch.setattr(settings, "deepstream_projection_enabled", False)
    await deepstream_client.sync_now("off")
    monkeypatch.setattr(settings, "deepstream_projection_enabled", True)
    await deepstream_client.sync_now("on")
    assert reached == ["on"]


def test_startup_only_starts_the_deepstream_loop_behind_its_flag():
    from backend import main
    src = inspect.getsource(main.lifespan)
    guard = src.index("if settings.deepstream_projection_enabled:")
    assert guard < src.index('deepstream_client.sync_now("startup")')
    assert guard < src.index("deepstream_client.reconcile_loop")


# ── AI Config is unchanged ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_activity_type_outside_the_catalog_is_still_refused(wired):
    body = {"regions": {"region_1": ZONE},
            "activities": [{"type": "unauthorised_access", "regions": ["region_1"], "params": {}}]}
    with pytest.raises(HTTPException) as exc:
        await _save(wired.cam, body)
    assert exc.value.status_code == 422 and wired.pushed == []


def test_the_stored_config_shape_and_its_validation_are_unchanged():
    body = AnalyticsConfigBody(**SAVED).model_dump()
    assert set(body) == {"regions", "activities"}
    assert set(body["activities"][0]) == {"type", "regions", "params"}
    # Whether an activity may have no zone is now its definition's zone rule,
    # checked at save against the catalog (test_activity_catalog.py) — the
    # shape itself allows it.
    AnalyticsConfigBody(regions={"region_1": ZONE},
                        activities=[{"type": "restricted_zone_entry", "regions": [], "params": {}}])
    with pytest.raises(ValidationError):          # an activity may not point at a zone that does not exist
        AnalyticsConfigBody(regions={}, activities=[{"type": "restricted_zone_entry",
                                                     "regions": ["gone"], "params": {}}])


# ── the CPU engine is an accepted producer ──────────────────────────────────

def test_the_ingest_accepts_the_cpu_engine_as_a_source():
    cam = SimpleNamespace(id=uuid.uuid4(), slug="gate-a1b2", retention_days=None,
                          analytics_config=SAVED)
    now = datetime.now(timezone.utc)
    ev = ai_events.EventIn(sensor_id="gate-a1b2", activity="restricted_zone_entry",
                           zone="region_1", started_at=now - timedelta(seconds=5),
                           track_id="gate-a1b2:ab12.0:7", object_class="person",
                           confidence=0.9, source="cpu",
                           attributes={"full_frame_zone": False})
    [row], rejected = ai_events.prepare([ev], {"gate-a1b2": cam}, now=now,
                                        default_retention_days=30)
    assert rejected == []
    assert row["source"] == "cpu" and row["zone"] == "Zone 1"
    assert row["camera_id"] == cam.id and row["activity"] == "restricted_zone_entry"
