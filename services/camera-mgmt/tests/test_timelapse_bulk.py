"""Bulk time-lapse switch: the Inventory's "Time-lapse on/off" for many cameras.

Two layers, like the rest of the time-lapse suites:

  plan_bulk / bulk_notes   pure decisions — what each camera ends up storing,
                           asserted as data (no app, no DB)
  POST /api/timelapse/bulk through the real app with real RS256 tokens
                           (conftest's `client`), against a stub session that
                           holds several cameras, so the permission gate, the
                           cap, mixed results and the single commit are all
                           exercised exactly as routed in production

The contract that matters most: a camera switched on in bulk stores exactly
what its own Time-lapse tab would have stored — only the settings that differ
from the site defaults ({} for a fresh camera, which then follows the Defaults
page; its own changes kept for one already configured).

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from conftest import StubSession

from backend.services import timelapse as tl

DEFAULTS = tl.TimelapseConfig().model_dump()


# ── plan_bulk: pure ──────────────────────────────────────────────────────────

class TestPlan:
    def test_a_fresh_camera_follows_the_site_defaults(self):
        p = tl.plan_bulk(was_enabled=False, raw_config={}, enabled=True)
        assert p.result == "enabled" and p.changed and p.enabled
        assert p.config == {}

    def test_switching_on_keeps_a_cameras_own_settings(self):
        own = {"interval_s": 60, "retention_days": 365, "ptz_preset_only": "3"}
        p = tl.plan_bulk(was_enabled=False, raw_config=own, enabled=True)
        assert p.result == "enabled" and p.config == own

    def test_settings_overlay_and_never_wipe_what_the_dialog_did_not_show(self):
        own = {**DEFAULTS, "retention_days": 365, "ptz_preset_only": "3"}
        p = tl.plan_bulk(was_enabled=False, raw_config=own, enabled=True, patch={"interval_s": 600})
        assert p.config["interval_s"] == 600
        assert p.config["retention_days"] == 365 and p.config["ptz_preset_only"] == "3"

    def test_already_on_without_settings_is_a_no_op(self):
        own = {**DEFAULTS, "interval_s": 60}
        p = tl.plan_bulk(was_enabled=True, raw_config=own, enabled=True)
        assert p.result == "already_enabled" and not p.changed and p.config == own

    def test_already_on_with_the_same_settings_is_still_a_no_op(self):
        own = {**DEFAULTS, "interval_s": 600}
        p = tl.plan_bulk(was_enabled=True, raw_config=own, enabled=True, patch={"interval_s": 600})
        assert p.result == "already_enabled" and not p.changed

    def test_already_on_with_new_settings_is_an_update(self):
        p = tl.plan_bulk(was_enabled=True, raw_config=DEFAULTS, enabled=True, patch={"interval_s": 600})
        assert p.result == "updated" and p.changed and p.config["interval_s"] == 600

    def test_off_keeps_the_settings_for_the_next_on(self):
        own = {**DEFAULTS, "interval_s": 60}
        p = tl.plan_bulk(was_enabled=True, raw_config=own, enabled=False)
        assert p.result == "disabled" and p.changed and not p.enabled and p.config == own
        q = tl.plan_bulk(was_enabled=False, raw_config=own, enabled=False)
        assert q.result == "already_disabled" and not q.changed

    def test_an_invalid_merge_raises_like_the_tab_would(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            tl.plan_bulk(was_enabled=False, raw_config={}, enabled=True, patch={"interval_s": 5})


class TestNotes:
    def test_offline_and_disabled_cameras_are_switched_on_with_a_note(self):
        n = tl.bulk_notes(camera_enabled=True, health_status="disconnected", ptz_capable=False,
                          cfg=DEFAULTS, enabled=True)
        assert len(n) == 1 and "Offline" in n[0]
        n = tl.bulk_notes(camera_enabled=False, health_status="disconnected", ptz_capable=False,
                          cfg=DEFAULTS, enabled=True)
        assert len(n) == 1 and "disabled" in n[0]

    def test_a_ptz_camera_without_a_preset_is_pointed_at_its_tab(self):
        n = tl.bulk_notes(camera_enabled=True, health_status="connected", ptz_capable=True,
                          cfg=DEFAULTS, enabled=True)
        assert len(n) == 1 and "preset" in n[0]
        assert tl.bulk_notes(camera_enabled=True, health_status="connected", ptz_capable=True,
                             cfg={**DEFAULTS, "ptz_preset_only": "1"}, enabled=True) == []

    def test_switching_off_needs_no_notes(self):
        assert tl.bulk_notes(camera_enabled=False, health_status="error", ptz_capable=True,
                             cfg=DEFAULTS, enabled=False) == []


# ── POST /api/timelapse/bulk: routed ─────────────────────────────────────────

URL = "/api/timelapse/bulk"


def _cam(name, *, on=False, cfg=None, health="connected", enabled=True, ptz=None):
    return SimpleNamespace(id=uuid.uuid4(), slug=f"{name.lower()}-{uuid.uuid4().hex[:4]}", name=name,
                           enabled=enabled, health_status=health, timelapse_enabled=on,
                           timelapse_config=dict(cfg or {}), privacy_masks=[], ptz_capabilities=ptz)


class _Fleet(StubSession):
    """Answers `SELECT cameras WHERE id IN (...)` with the cameras whose ids were
    bound into the statement — so an unknown id really is missing."""

    def __init__(self, cams):
        super().__init__()
        self.cams = cams
        self.fail_commit = False

    async def execute(self, statement=None, *a, **kw):
        sql = str(statement)
        if "FROM cameras" in sql:
            wanted = set()
            for v in statement.compile().params.values():
                wanted |= set(v) if isinstance(v, (list, tuple)) else {v}
            hits = [c for c in self.cams if c.id in wanted]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: hits))
        return await super().execute(statement, *a, **kw)

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("connection reset")
        await super().commit()


@pytest.fixture
def fleet():
    return [
        _cam("Tower"),                                                           # fresh
        _cam("Gate", on=True, cfg={**DEFAULTS, "interval_s": 60}),               # already on, custom
        _cam("Yard", health="disconnected"),                                     # offline
        _cam("Crane", ptz={"pan": True}, cfg={**DEFAULTS, "ptz_preset_only": "2"}),  # PTZ with its preset
    ]


@pytest.fixture
def db(fleet):
    return _Fleet(fleet)


@pytest.fixture
def audits(monkeypatch):
    from backend.services import audit
    seen = []

    async def record(request, principal, action, *, target=None, detail=None, outcome="success"):
        seen.append((action, target, detail))

    monkeypatch.setattr(audit, "record", record)
    return seen


@pytest.mark.asyncio
class TestBulkHttp:
    async def test_an_operator_may_not_switch_in_bulk(self, client, mint, fleet, db):
        r = await client.post(URL, json={"camera_ids": [str(c.id) for c in fleet], "enabled": True},
                              headers=mint(["operator"]))
        assert r.status_code == 403
        assert not any(c.timelapse_enabled for c in fleet if c.name != "Gate") and db.commits == 0

    async def test_enable_many_in_one_commit_with_per_camera_results(self, client, mint, fleet, db, audits):
        tower, gate, yard, crane = fleet
        r = await client.post(URL, json={"camera_ids": [str(c.id) for c in fleet], "enabled": True},
                              headers=mint(["supervisor"]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["enabled"], body["already_enabled"], body["failed"], body["changed"]) == (3, 1, 0, 3)
        assert db.commits == 1
        assert all(c.timelapse_enabled for c in fleet)
        assert tower.timelapse_config == {}                           # what the tab would store: follows the defaults
        assert gate.timelapse_config["interval_s"] == 60              # untouched, idempotent
        assert crane.timelapse_config["ptz_preset_only"] == "2"       # the per-camera preset survives
        by = {x["name"]: x for x in body["results"]}
        assert by["Gate"]["skipped_reason"] == "already_enabled"
        assert any("Offline" in n for n in by["Yard"]["notes"])
        assert by["Crane"]["notes"] == []
        # One audit row per CHANGED camera, same action as the per-camera save.
        assert sorted(t for _, t, _ in audits) == sorted([tower.slug, yard.slug, crane.slug])
        assert all(a == "camera.timelapse_updated" and d["bulk"] for a, _, d in audits)

    async def test_settings_apply_to_all_and_keep_each_cameras_own(self, client, mint, fleet, db, audits):
        tower, gate, yard, crane = fleet
        r = await client.post(URL, json={"camera_ids": [str(c.id) for c in fleet], "enabled": True,
                                         "config": {"interval_s": 900, "schedule": None}},
                              headers=mint(["admin"]))
        assert r.status_code == 200, r.text
        assert all(c.timelapse_config["interval_s"] == 900 and c.timelapse_config["schedule"] is None
                   for c in fleet)
        assert r.json()["updated"] == 1                                # Gate: already on, new interval
        assert crane.timelapse_config["ptz_preset_only"] == "2"

    async def test_disable_many_keeps_settings(self, client, mint, fleet, db, audits):
        for c in fleet:
            c.timelapse_enabled = True
        fleet[0].timelapse_enabled = False
        r = await client.post(URL, json={"camera_ids": [str(c.id) for c in fleet], "enabled": False},
                              headers=mint(["supervisor"]))
        body = r.json()
        assert r.status_code == 200 and (body["disabled"], body["already_disabled"]) == (3, 1)
        assert not any(c.timelapse_enabled for c in fleet)
        assert fleet[1].timelapse_config["interval_s"] == 60

    async def test_mixed_results_unknown_and_malformed_ids_fail_only_their_row(self, client, mint, fleet, db, audits):
        ghost = str(uuid.uuid4())
        ids = [str(fleet[0].id), ghost, "not-a-uuid", str(fleet[0].id)]   # dup collapses
        r = await client.post(URL, json={"camera_ids": ids, "enabled": True}, headers=mint(["admin"]))
        body = r.json()
        assert r.status_code == 200 and body["requested"] == 3
        assert (body["enabled"], body["failed"]) == (1, 2)
        errs = {x["camera_id"]: x["error"] for x in body["results"] if not x["ok"]}
        assert errs == {ghost: "Camera not found", "not-a-uuid": "Not a camera id"}
        assert fleet[0].timelapse_enabled is True

    async def test_rerunning_the_same_request_changes_nothing(self, client, mint, fleet, db, audits):
        ids = [str(c.id) for c in fleet]
        await client.post(URL, json={"camera_ids": ids, "enabled": True}, headers=mint(["admin"]))
        before = [dict(c.timelapse_config) for c in fleet]
        commits, n_audit = db.commits, len(audits)
        r = await client.post(URL, json={"camera_ids": ids, "enabled": True}, headers=mint(["admin"]))
        assert r.json()["already_enabled"] == 4 and r.json()["changed"] == 0
        assert [c.timelapse_config for c in fleet] == before
        assert db.commits == commits and len(audits) == n_audit

    async def test_the_cap_is_a_plain_422(self, client, mint, db):
        ids = [str(uuid.uuid4()) for _ in range(tl.BULK_MAX + 1)]
        r = await client.post(URL, json={"camera_ids": ids, "enabled": True}, headers=mint(["admin"]))
        assert r.status_code == 422 and f"At most {tl.BULK_MAX}" in r.json()["detail"]

    async def test_an_empty_selection_is_a_422(self, client, mint, db):
        r = await client.post(URL, json={"camera_ids": [], "enabled": True}, headers=mint(["admin"]))
        assert r.status_code == 422

    @pytest.mark.parametrize("cfg,enabled,needle", [
        ({"interval_s": 5}, True, "interval_s"),
        ({"nope": 1}, True, "nope"),
        ({"ptz_preset_only": "1"}, True, "per camera"),
        ({"interval_s": 600}, False, "switching time-lapse on"),
    ])
    async def test_bad_settings_refuse_the_whole_request(self, client, mint, fleet, db, cfg, enabled, needle):
        r = await client.post(URL, json={"camera_ids": [str(c.id) for c in fleet], "enabled": enabled,
                                         "config": cfg}, headers=mint(["admin"]))
        assert r.status_code == 422 and needle in r.json()["detail"]
        assert db.commits == 0 and not fleet[0].timelapse_enabled

    async def test_a_failed_commit_says_nothing_changed(self, client, mint, fleet, db, audits):
        db.fail_commit = True
        r = await client.post(URL, json={"camera_ids": [str(fleet[0].id)], "enabled": True},
                              headers=mint(["admin"]))
        assert r.status_code == 503 and "no camera was changed" in r.json()["detail"]
        assert audits == []

    async def test_defaults_are_the_models_own(self, client, mint, db):
        r = await client.get("/api/timelapse/defaults", headers=mint(["operator"]))
        assert r.status_code == 200
        assert r.json()["config"] == DEFAULTS and r.json()["bulk_max"] == tl.BULK_MAX
