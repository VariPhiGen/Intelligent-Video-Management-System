"""The Activity Type catalog follows the CPU activity registry.

Which activities exist, whether they run, which zones they take and their
settings come from the analytics engine's code. These pin the VMS side of that:

  * the sync copies definitions in, keeps what an administrator owns (label,
    colour, order), and never deletes a row — a key the registry drops becomes
    `unregistered`;
  * the admin catalog endpoint can relabel and recolour, and cannot add a type,
    delete one or change its settings;
  * AI Config's save accepts only an available activity (keeping one already
    stored), enforces each activity's zone rule, and refuses setting values its
    code would not accept;
  * who may do each of those is unchanged.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import StubSession  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.models import AnalyticsActivityType, AnalyticsConfigBody  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402
from backend.services import activity_catalog as ac  # noqa: E402
from backend.services import analytics_client  # noqa: E402

DURATION = {"key": "required_duration_s", "label": "Required parking duration", "kind": "number",
            "default": 600, "unit": "s", "min": 1, "max": 86400, "integer": False, "options": None,
            "min_items": None, "help": None, "configurable": True}
CLASSES = {"key": "vehicle_classes", "label": "Vehicle types", "kind": "list",
           "default": ["car", "truck"], "unit": None, "min": None, "max": None, "integer": False,
           "options": ["car", "motorcycle", "bus", "truck"], "min_items": 1, "help": None,
           "configurable": True}
COOLDOWN = {**DURATION, "key": "cooldown_s", "label": "Cooldown", "default": 60}
#: People Gathering's settings as the analytics registry publishes them.
GATHERING = [
    {**DURATION, "key": "required_duration_s", "label": "Gathering duration", "default": 60},
    {**DURATION, "key": "last_time", "label": "Reset after no gathering", "default": 30, "max": 3600},
    {**DURATION, "key": "min_group_size", "label": "Minimum people", "default": 2, "unit": None,
     "min": 2, "max": 50, "integer": True},
    {**DURATION, "key": "proximity_factor", "label": "Proximity factor", "default": 1.35,
     "unit": "× box width", "min": 0.5, "max": 10.0},
    {**DURATION, "key": "cooldown_s", "label": "Cooldown", "default": 100.0},
]

REGISTRY = [
    {"key": "stray_parking", "label": "Stray parking", "description": "Parked too long.",
     "color": "#ffb020", "status": "available", "zone_rule": "required", "version": 1,
     "domains": ["vehicles"], "params_schema": [CLASSES, DURATION]},
    {"key": "restricted_zone_entry", "label": "Restricted zone entry", "description": "",
     "color": "#4fd1c5", "status": "available", "zone_rule": "optional", "version": 1,
     "params_schema": [COOLDOWN]},
    {"key": "entry_exit_WLE_logs", "label": "Entry / exit", "description": "Tripwire interaction",
     "color": "#4fd1c5", "status": "available", "zone_rule": "tripwire", "version": 1,
     "params_schema": []},
]


def row(key, **kw):
    base = dict(key=key, label=key.replace("_", " ").title(), color="#123456", sort_order=0,
                params_schema=[], status="unregistered", zone_rule="required",
                description=None, definition_version=None, synced_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def catalog_rows():
    return [
        row("stray_parking", label="Parking (site name)", color="#ff0000", sort_order=0,
            status="available", zone_rule="required", params_schema=[CLASSES, DURATION]),
        row("restricted_zone_entry", sort_order=1, status="available", zone_rule="optional",
            params_schema=[COOLDOWN]),
        row("entry_exit_WLE_logs", sort_order=2, status="available", zone_rule="tripwire"),
        row("car_detection", sort_order=3, status="unregistered"),
        row("idle_worker", sort_order=4, status="hold", zone_rule="required"),
        row("people_gathering", sort_order=5, status="available", zone_rule="optional",
            params_schema=GATHERING),
    ]


class _Scalars(list):
    def all(self):
        return list(self)


class CatalogSession(StubSession):
    """A StubSession whose catalog table is a list of rows (read in sort order,
    as the routes ask for it)."""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def execute(self, statement=None, *a, **kw):
        if "analytics_activity_types" in str(statement):
            ordered = sorted(self.rows, key=lambda r: r.sort_order)
            return SimpleNamespace(scalars=lambda: _Scalars(ordered))
        return await super().execute(statement, *a, **kw)

    def add(self, obj):
        super().add(obj)
        self.rows.append(obj)


# ── the sync ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_sync_copies_the_registry_and_keeps_what_the_admin_owns():
    db = CatalogSession([
        row("stray_parking", label="Parking (site name)", color="#ff0000", sort_order=4,
            params_schema=[{"key": "required_frames"}], status="unregistered"),
        row("idle_worker", sort_order=2, status="available"),
    ])
    counts = await ac.sync_catalog(db, ac.parse_definitions(REGISTRY))
    by = {r.key: r for r in db.rows}
    parking = by["stray_parking"]
    assert (parking.label, parking.color, parking.sort_order) == ("Parking (site name)", "#ff0000", 4)
    assert [f["key"] for f in parking.params_schema] == ["vehicle_classes", "required_duration_s"]
    assert (parking.status, parking.zone_rule, parking.definition_version) == ("available", "required", 1)
    assert isinstance(by["restricted_zone_entry"], AnalyticsActivityType)       # inserted
    assert by["restricted_zone_entry"].label == "Restricted zone entry" and by["restricted_zone_entry"].sort_order == 5
    assert by["idle_worker"].status == "unregistered"                           # kept, not deleted
    assert counts == {"registered": 3, "added": 2, "updated": 1, "withdrawn": 1}
    assert db.commits == 1


def test_a_malformed_definition_is_skipped_not_fatal():
    parsed = ac.parse_definitions([*REGISTRY, {"key": "bad key!", "label": "x"}])
    assert [d.key for d in parsed] == ["stray_parking", "restricted_zone_entry", "entry_exit_WLE_logs"]


@pytest.mark.asyncio
async def test_the_reconcile_loop_syncs_the_registry(monkeypatch):
    got = []

    async def fetch():
        return REGISTRY

    async def sync(db, definitions):
        got.extend(d.key for d in definitions)
        return {}

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(analytics_client, "fetch_activity_definitions", fetch)
    monkeypatch.setattr(ac, "sync_catalog", sync)
    monkeypatch.setattr("backend.db.AsyncSessionLocal", _Factory())
    await analytics_client.sync_activity_catalog()
    assert got == ["stray_parking", "restricted_zone_entry", "entry_exit_WLE_logs"]


# ── validation ───────────────────────────────────────────────────────────────

class TestValidateParams:
    @pytest.mark.parametrize("value, problem", [
        (600, None), (1, None), (0, "at least 1"), (90000, "at most 86400"),
        ("600", "must be a number"), (True, "must be a number"),
    ])
    def test_numbers(self, value, problem):
        errors = ac.validate_params([DURATION], {"required_duration_s": value})
        assert (problem is None and errors == []) or (problem and problem in errors[0])

    def test_a_missing_setting_is_fine_the_runtime_defaults_it(self):
        assert ac.validate_params([DURATION, CLASSES], {}) == []

    def test_lists_must_hold_supported_options(self):
        assert ac.validate_params([CLASSES], {"vehicle_classes": ["car"]}) == []
        assert "not supported" in ac.validate_params([CLASSES], {"vehicle_classes": ["bike"]})[0]
        assert "at least 1" in ac.validate_params([CLASSES], {"vehicle_classes": []})[0]


def _body(*activities, regions=None):
    regions = {"region_1": {"kind": "zone", "name": "Zone 1", "color": "#4fd1c5",
                            "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]},
               "wire": {"kind": "tripwire", "name": "Line", "color": "#4fd1c5",
                        "points": [[0.1, 0.5], [0.9, 0.5]], "direction": "a2b"}} \
        if regions is None else regions
    return AnalyticsConfigBody(regions=regions, activities=list(activities))


def _validate(body, stored=None):
    return ac.validate_activities({r.key: r for r in catalog_rows()}, stored or {},
                                  body.activities, body.regions)


class TestValidateActivities:
    def test_an_available_activity_with_the_right_zones_and_values_passes(self):
        body = _body({"type": "stray_parking", "regions": ["region_1"],
                      "params": {"required_duration_s": 300, "vehicle_classes": ["car"]}})
        assert _validate(body) == []

    def test_an_activity_on_hold_cannot_be_added(self):
        body = _body({"type": "idle_worker", "regions": ["region_1"], "params": {}})
        assert "on hold" in _validate(body)[0]

    def test_an_unregistered_activity_cannot_be_added(self):
        body = _body({"type": "car_detection", "regions": ["region_1"], "params": {}})
        assert "not part of the running analytics engine" in _validate(body)[0]

    def test_an_activity_already_stored_is_kept_even_if_no_longer_available(self):
        body = _body({"type": "car_detection", "regions": ["region_1"], "params": {}})
        stored = {"activities": [{"type": "car_detection", "regions": ["region_1"], "params": {}}]}
        assert _validate(body, stored) == []

    def test_a_zone_required_activity_needs_a_zone(self):
        body = _body({"type": "stray_parking", "regions": [], "params": {}})
        assert "needs at least one zone" in _validate(body)[0]

    def test_a_whole_frame_activity_may_have_no_zone_but_not_only_a_tripwire(self):
        assert _validate(_body({"type": "restricted_zone_entry", "regions": [], "params": {}})) == []
        tripwire = _body({"type": "restricted_zone_entry", "regions": ["wire"], "params": {}})
        assert "zones, not tripwires" in _validate(tripwire)[0]

    def test_a_tripwire_activity_needs_a_tripwire_and_takes_no_zone(self):
        entry = lambda regions: _body({"type": "entry_exit_WLE_logs",  # noqa: E731
                                       "regions": regions, "params": {}})
        assert _validate(entry(["wire"])) == []
        assert "needs at least one tripwire" in _validate(entry([]))[0]
        assert "watches tripwires, not zones" in _validate(entry(["region_1"]))[0]
        assert "watches tripwires, not zones" in _validate(entry(["region_1", "wire"]))[0]

    def test_people_gathering_behaviour_settings_are_checked(self):
        ok = _body({"type": "people_gathering", "regions": [], "params": {
            "required_duration_s": 120, "last_time": 30,
            "min_group_size": 5, "proximity_factor": 2.0, "cooldown_s": 300}})
        assert _validate(ok) == []
        for params, fragment in (
                ({"min_group_size": 2.5}, "Minimum people must be a whole number"),
                ({"min_group_size": 1}, "Minimum people must be at least 2"),
                ({"min_group_size": 51}, "Minimum people must be at most 50"),
                ({"proximity_factor": 0.1}, "Proximity factor must be at least 0.5"),
                ({"proximity_factor": "wide"}, "Proximity factor must be a number"),
                ({"cooldown_s": 0}, "Cooldown must be at least 1")):
            problems = _validate(_body({"type": "people_gathering", "regions": [], "params": params}))
            assert len(problems) == 1 and fragment in problems[0], (params, problems)

    def test_a_gathering_stored_before_the_new_settings_is_still_valid(self):
        body = _body({"type": "people_gathering", "regions": ["region_1"],
                      "params": {"required_duration_s": 60, "last_time": 30}})
        assert _validate(body) == []

    def test_an_invalid_setting_is_refused(self):
        body = _body({"type": "restricted_zone_entry", "regions": [], "params": {"cooldown_s": -1}})
        assert "Cooldown must be at least 1" in _validate(body)[0]


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    return CatalogSession(catalog_rows())


@pytest.mark.asyncio
async def test_the_catalog_reports_status_and_zone_rule(client, mint):
    r = await client.get("/api/cameras/analytics/types", headers=mint(["operator"]))
    first = r.json()[0]
    assert first["key"] == "stray_parking" and first["status"] == "available"
    assert first["zone_rule"] == "required" and first["params_schema"][1]["key"] == "required_duration_s"


@pytest.mark.asyncio
async def test_only_an_admin_may_change_the_catalog(client, mint):
    body = {"types": [{"key": "stray_parking", "label": "X", "color": "#000000"}]}
    for role in ("operator", "supervisor", "viewer"):
        r = await client.put("/api/cameras/analytics/types", json=body, headers=mint([role]))
        assert r.status_code == 403, role


@pytest.mark.asyncio
async def test_an_admin_relabels_and_recolours_but_cannot_touch_settings(client, mint, db):
    r = await client.put("/api/cameras/analytics/types", headers=mint(["admin"]), json={"types": [
        {"key": "restricted_zone_entry", "label": "No entry", "color": "#00ff00",
         "params_schema": [{"key": "invented", "label": "Invented", "kind": "number", "default": 1}]},
    ]})
    assert r.status_code == 200
    rze = {x["key"]: x for x in r.json()}["restricted_zone_entry"]
    assert (rze["label"], rze["color"]) == ("No entry", "#00ff00")
    assert [f["key"] for f in rze["params_schema"]] == ["cooldown_s"]
    assert len(r.json()) == 6                      # nothing deleted
    assert [x["key"] for x in r.json()][0] == "restricted_zone_entry"


@pytest.mark.asyncio
async def test_an_admin_cannot_invent_an_activity(client, mint, db):
    r = await client.put("/api/cameras/analytics/types", headers=mint(["admin"]), json={"types": [
        {"key": "weapon_detection", "label": "Weapon Detection", "color": "#ff0000"}]})
    assert r.status_code == 422 and "weapon_detection" in r.json()["detail"]
    assert "weapon_detection" not in {x.key for x in db.rows}


@pytest.fixture
def wired(monkeypatch):
    cam = SimpleNamespace(id=uuid.uuid4(), slug="cam3-2qyj", enabled=True, search_indexing=False,
                          search_domains=[], analytics_config={})
    pushed = []

    async def get_cam(camera_id, db):
        return cam

    async def principal(request):
        return SimpleNamespace(roles=["admin"])

    async def record(*a, **kw):
        return None

    async def add_camera(slug, domains=None, analytics_config=None):
        pushed.append(analytics_config)
        return True

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router.audit_svc, "record", record)
    monkeypatch.setattr(cam_router, "_build_response", lambda c: c)
    monkeypatch.setattr(analytics_client, "add_camera", add_camera)
    monkeypatch.setattr(settings, "analytics_api_url", "http://127.0.0.1:8015")
    monkeypatch.setattr(settings, "analytics_sync_enabled", True)
    return SimpleNamespace(cam=cam, pushed=pushed)


async def _save(cam, body):
    return await cam_router.update_analytics_config(cam.id, body, SimpleNamespace(),
                                                    CatalogSession(catalog_rows()))


@pytest.mark.asyncio
async def test_a_whole_frame_activity_saves_without_a_zone_and_reaches_analytics(wired):
    await _save(wired.cam, _body({"type": "restricted_zone_entry", "regions": [],
                                  "params": {"cooldown_s": 30}}))
    [cfg] = wired.pushed
    assert cfg["activities"] == [{"type": "restricted_zone_entry", "regions": [],
                                  "params": {"cooldown_s": 30}}]


@pytest.mark.asyncio
async def test_entry_exit_on_a_tripwire_saves_and_reaches_analytics(wired):
    await _save(wired.cam, _body({"type": "entry_exit_WLE_logs", "regions": ["wire"], "params": {}}))
    [cfg] = wired.pushed
    assert cfg["activities"] == [{"type": "entry_exit_WLE_logs", "regions": ["wire"], "params": {}}]
    assert cfg["regions"]["wire"]["kind"] == "tripwire"
    assert cfg["regions"]["wire"]["points"] == [[0.1, 0.5], [0.9, 0.5]]


@pytest.mark.asyncio
async def test_people_gathering_settings_save_load_and_reach_analytics(wired):
    params = {"active_hours": None, "active_days": ["Mon"], "required_duration_s": 120,
              "last_time": 30, "min_group_size": 5, "proximity_factor": 2.0, "cooldown_s": 300}
    await _save(wired.cam, _body({"type": "people_gathering", "regions": ["region_1"], "params": params}))
    [cfg] = wired.pushed
    assert cfg["activities"][0]["params"] == params                         # handed to analytics
    assert wired.cam.analytics_config["activities"][0]["params"] == params  # what AI Config loads


@pytest.mark.asyncio
async def test_a_gathering_saved_without_the_new_settings_is_left_as_it_is(wired):
    params = {"required_duration_s": 60, "last_time": 30}
    await _save(wired.cam, _body({"type": "people_gathering", "regions": [], "params": params}))
    [cfg] = wired.pushed
    assert cfg["activities"][0]["params"] == params    # nothing filled in; the engine uses its defaults


@pytest.mark.asyncio
@pytest.mark.parametrize("params, fragment", [
    ({"min_group_size": 2.5}, "must be a whole number"),
    ({"proximity_factor": 11}, "must be at most 10"),
])
async def test_ai_config_refuses_an_unusable_gathering_setting(wired, params, fragment):
    with pytest.raises(HTTPException) as exc:
        await _save(wired.cam, _body({"type": "people_gathering", "regions": [], "params": params}))
    assert exc.value.status_code == 422 and fragment in exc.value.detail
    assert wired.pushed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("activity, fragment", [
    ({"type": "stray_parking", "regions": [], "params": {}}, "needs at least one zone"),
    ({"type": "idle_worker", "regions": ["region_1"], "params": {}}, "on hold"),
    ({"type": "entry_exit_WLE_logs", "regions": [], "params": {}}, "needs at least one tripwire"),
    ({"type": "stray_parking", "regions": ["region_1"],
      "params": {"required_duration_s": "ten minutes"}}, "must be a number"),
])
async def test_ai_config_refuses_what_the_engine_would_not_run(wired, activity, fragment):
    with pytest.raises(HTTPException) as exc:
        await _save(wired.cam, _body(activity))
    assert exc.value.status_code == 422 and fragment in exc.value.detail
    assert wired.pushed == []
