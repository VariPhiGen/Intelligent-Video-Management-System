"""Time-lapse site defaults + the disk guard.

  * a camera stores only what it changes; everything else follows the site
    defaults, so changing a default reaches every camera that left it alone
  * the Defaults page can't carry a per-camera setting (a PTZ preset)
  * the disk guard trips below the free-space floor or over the time-lapse
    budget, switches every camera off with a remark, never by itself back on,
    and switching on (one camera or in bulk) is refused while it's tripped

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import uuid
from collections import namedtuple
from types import SimpleNamespace

import pytest

from conftest import StubSession

from backend.services import timelapse as tl

GB = 1024 ** 3
_du = namedtuple("du", "total used free")


class TestSiteDefaults:
    def test_a_camera_follows_a_changed_default_unless_it_changed_that_setting(self):
        tl._site["defaults"] = {"interval_s": 600}
        assert tl.effective_config({}).interval_s == 600
        own = {"interval_s": 60}
        assert tl.effective_config(own).interval_s == 60
        assert tl.effective_config(own).retention_days == 90          # still the built-in for the rest

    def test_only_differences_are_stored(self):
        tl._site["defaults"] = {"resolution": "720p"}
        cfg = tl.TimelapseConfig(**{**tl.site_defaults().model_dump(), "interval_s": 60})
        assert tl.overrides_of(cfg) == {"interval_s": 60}
        # The same value as the default is not a change: the camera keeps following it.
        assert tl.overrides_of(tl.site_defaults()) == {}

    def test_a_bad_stored_default_falls_back_to_builtin(self):
        tl._site["defaults"] = {"interval_s": 1}
        assert tl.site_defaults().interval_s == 300


def _usage(monkeypatch, total_gb, free_gb, frames_gb=0.0):
    monkeypatch.setattr(tl.shutil, "disk_usage", lambda p: _du(total_gb * GB, (total_gb - free_gb) * GB, free_gb * GB))

    class _Db(StubSession):
        async def execute(self, statement=None, *a, **kw):
            sql = str(statement)
            if "timelapse_frames" in sql:
                return SimpleNamespace(scalar=lambda: int(frames_gb * GB))
            return await super().execute(statement, *a, **kw)
    return _Db()


class TestDiskGuard:
    def test_plenty_of_room_is_not_over(self, monkeypatch):
        u = asyncio.run(tl.disk_usage(_usage(monkeypatch, 1000, 600, frames_gb=50)))
        assert u["over"] is False and u["reason"] == "" and u["free_pct"] == 60.0

    def test_below_the_free_floor_says_why(self, monkeypatch):
        u = asyncio.run(tl.disk_usage(_usage(monkeypatch, 1000, 100)))
        assert u["over"] and "90% full" in u["reason"] and "recordings are never deleted" in u["reason"]

    def test_over_the_budget_says_why(self, monkeypatch):
        u = asyncio.run(tl.disk_usage(_usage(monkeypatch, 1000, 600, frames_gb=250)))
        assert u["over"] and "over its budget of 20%" in u["reason"]

    def test_thresholds_come_from_the_site_settings(self, monkeypatch):
        tl._site["guard"] = {"floor_free_pct": 5.0}
        tl._site["at"] = 1e18                                          # fresh: don't re-read
        assert not asyncio.run(tl.disk_usage(_usage(monkeypatch, 1000, 100)))["over"]

    def test_an_unmeasurable_disk_never_pauses(self, monkeypatch):
        def boom(p):
            raise OSError("gone")
        db = _usage(monkeypatch, 1000, 10)
        monkeypatch.setattr(tl.shutil, "disk_usage", boom)
        assert asyncio.run(tl.disk_usage(db))["over"] is False

    def test_tripping_switches_every_camera_off_and_leaves_a_remark(self, monkeypatch):
        cams = [SimpleNamespace(id=uuid.uuid4(), slug=f"c{i}", timelapse_enabled=True) for i in range(3)]
        row = SimpleNamespace(paused=None, updated_at=None, defaults=None, guard=None)

        async def usage(db):
            return {"over": True, "reason": "the server disk is 90% full", "total_bytes": 1, "free_bytes": 0,
                    "timelapse_bytes": 0, "free_pct": 10.0}

        async def site_row(db):
            return row

        class _Db(StubSession):
            async def execute(self, statement=None, *a, **kw):
                if "FROM cameras" in str(statement):
                    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [c for c in cams if c.timelapse_enabled]))
                return await super().execute(statement, *a, **kw)

        monkeypatch.setattr(tl, "disk_usage", usage)
        monkeypatch.setattr(tl, "site_row", site_row)
        monkeypatch.setattr(tl, "_site_from_row", lambda r: tl._site.update(paused=r.paused))
        pause = asyncio.run(tl.guard_tick(_Db()))
        assert not any(c.timelapse_enabled for c in cams)
        assert pause["reason"] == "the server disk is 90% full"
        assert set(pause["camera_ids"]) == {str(c.id) for c in cams} and row.paused is pause
        # Nothing left on: the next tick does nothing (and never switches anything back on).
        assert asyncio.run(tl.guard_tick(_Db())) is None and not any(c.timelapse_enabled for c in cams)


GATE = SimpleNamespace(id=uuid.uuid4(), slug="gate-a1b2", name="Gate", enabled=True, timelapse_enabled=False,
                       timelapse_config={}, privacy_masks=[], ptz_capabilities=None,
                       stage="registered", health_status="connected")


class _Registry(StubSession):
    async def execute(self, statement=None, *a, **kw):
        sql = str(statement)
        if "FROM cameras" in sql and "timelapse_frames" not in sql:
            return SimpleNamespace(scalar_one_or_none=lambda: GATE, scalars=lambda: SimpleNamespace(all=lambda: [GATE]),
                                   all=lambda: [], first=lambda: None)
        if "timelapse_frames" in sql and "count" in sql.lower():
            return SimpleNamespace(one=lambda: (0, 0, None), scalar=lambda: 0, all=lambda: [])
        return await super().execute(statement, *a, **kw)


@pytest.fixture
def db():
    GATE.timelapse_enabled, GATE.timelapse_config = False, {}
    return _Registry()


@pytest.mark.asyncio
class TestHttp:
    async def test_switching_on_is_refused_while_the_disk_is_full(self, client, mint, monkeypatch):
        monkeypatch.setattr(tl.shutil, "disk_usage", lambda p: _du(1000 * GB, 950 * GB, 50 * GB))
        r = await client.put(f"/api/timelapse/cameras/{GATE.id}", json={"enabled": True, "config": {}},
                             headers=mint(["admin"]))
        assert r.status_code == 409 and "Free some space" in r.json()["detail"]
        assert GATE.timelapse_enabled is False
        r = await client.post("/api/timelapse/bulk", json={"camera_ids": [str(GATE.id)], "enabled": True},
                              headers=mint(["admin"]))
        assert r.status_code == 409

    async def test_the_defaults_page_refuses_a_per_camera_setting(self, client, mint):
        r = await client.put("/api/timelapse/defaults", json={"defaults": {"ptz_preset_only": "1"}},
                             headers=mint(["admin"]))
        assert r.status_code == 422 and "per camera" in r.json()["detail"]

    async def test_the_guard_limits_are_range_checked(self, client, mint):
        r = await client.put("/api/timelapse/defaults", json={"guard": {"floor_free_pct": 2}}, headers=mint(["admin"]))
        assert r.status_code == 422

    async def test_a_supervisor_may_change_the_defaults_but_not_the_disk_limits(self, client, mint):
        # Supervisor holds timelapse_manage by default. Sending the limits back unchanged (what the page does
        # on every save) is fine; changing them is the recorder's storage, so admin-only.
        r = await client.put("/api/timelapse/defaults", json={"defaults": {"interval_s": 600},
                                                              "guard": dict(tl.GUARD_DEFAULTS)},
                             headers=mint(["supervisor"]))
        assert r.status_code == 200, r.text
        r = await client.put("/api/timelapse/defaults", json={"guard": {"floor_free_pct": 5}},
                             headers=mint(["supervisor"]))
        assert r.status_code == 403 and "administrator" in r.json()["detail"]
        r = await client.put("/api/timelapse/defaults", json={"guard": {"floor_free_pct": 10}},
                             headers=mint(["admin"]))
        assert r.status_code == 200, r.text

    async def test_an_operator_may_not_change_the_defaults(self, client, mint):
        r = await client.put("/api/timelapse/defaults", json={"defaults": {"interval_s": 600}},
                             headers=mint(["operator"]))
        assert r.status_code == 403

    async def test_a_camera_lists_which_settings_it_changes(self, client, mint):
        GATE.timelapse_config = {"interval_s": 60}
        r = await client.get("/api/timelapse/changed", headers=mint(["operator"]))
        assert r.status_code == 200
        cam = r.json()["cameras"][0]
        assert cam["changes"] == {"interval_s": {"value": 60, "default": 300}}

    async def test_use_defaults_drops_the_changes_but_keeps_the_ptz_preset(self, client, mint):
        GATE.timelapse_config = {"interval_s": 60, "ptz_preset_only": "2"}
        r = await client.post(f"/api/timelapse/cameras/{GATE.id}/use-defaults", json={}, headers=mint(["admin"]))
        assert r.status_code == 200, r.text
        assert GATE.timelapse_config == {"ptz_preset_only": "2"}
