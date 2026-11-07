"""'Until the person leaves the zone' — the occupancy hold, without a database.

ON the moment the per-frame zone count reads a person, OFF the moment it reads
zero, never later than the cap. These pin the parts that decide WHEN, and the
guard rails that keep a dead device from sticking anything.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_hold.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.bridge import pending  # noqa: E402

_aio = pytest.mark.asyncio
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
TS = NOW.timestamp()


def presence(count, *, zone="Zone 1", zid="region_1", frame=0, age=0.0):
    return {"camera": "realcam", "ts": TS - age,
            "zones": {zid: {"name": zone, "count": count, "confirmed": 0},
                      "__frame__": {"name": "__frame__", "count": frame}}}


class TestOccupied:
    def test_counts_by_name_or_id(self):
        assert pending.occupied(presence(2), "Zone 1", TS) == 2
        assert pending.occupied(presence(1), "region_1", TS) == 1

    def test_a_comma_list_of_zones(self):
        p = presence(1)
        p["zones"]["region_2"] = {"name": "Zone 2", "count": 2}
        assert pending.occupied(p, "region_1,region_2", TS) == 3

    def test_unconfirmed_people_count(self):
        """The per-frame count, not the tracker's confirmed one: confirmation
        takes a second, which is the delay this loop exists to remove."""
        assert pending.occupied(presence(1), "Zone 1", TS) == 1

    def test_another_zone_is_not_this_one(self):
        assert pending.occupied(presence(3), "Zone 2", TS) == 0

    def test_the_whole_frame_is_not_a_zone(self):
        assert pending.occupied(presence(0, frame=4), None, TS) == 0

    def test_no_zone_named_means_every_drawn_zone(self):
        assert pending.occupied(presence(2), None, TS) == 2

    def test_a_stale_or_missing_reading_counts_as_nobody(self):
        assert pending.occupied(presence(5, age=pending.PRESENCE_STALE_S + 0.5), "Zone 1", TS) == 0
        assert pending.occupied(None, "Zone 1", TS) == 0


class TestZonesFor:
    def test_the_activitys_own_zones(self):
        cfg = {"activities": [{"type": "restricted_zone_entry", "regions": ["region_1"]},
                              {"type": "people_distribution", "regions": ["region_9"]}]}
        assert pending.zones_for(cfg, "restricted_zone_entry") == "region_1"

    def test_no_zones_means_any(self):
        assert pending.zones_for({"activities": [{"type": "x", "regions": []}]}, "x") is None
        assert pending.zones_for(None, "x") is None


def rule(**kw):
    base = dict(name="siren", enabled=True, mode="armed", scope_kind="camera",
                scope_value="realcam", active_hours=None, activity="restricted_zone_entry",
                actions=[{"type": "peripheral", "peripheral": "ch8", "command": "on",
                          "until": "zone_empty", "then": "off", "for_seconds": 600}])
    base.update(kw)
    return SimpleNamespace(**base)


CFG = {"realcam": {"activities": [{"type": "restricted_zone_entry", "regions": ["region_1"]}]}}


class TestHoldTargets:
    def test_an_armed_hold_rule_is_a_target(self):
        [t] = pending.hold_targets([rule()], CFG, NOW, site_tz="UTC")
        assert (t.peripheral, t.command, t.then, t.camera, t.zones, t.grace_s, t.cap_seconds) == \
            ("ch8", "on", "off", "realcam", "region_1", 0, 600)

    @pytest.mark.parametrize("over", [{"mode": "log_only"}, {"enabled": False},
                                      {"scope_kind": "any"}, {"scope_value": "other"},
                                      {"actions": [{"type": "peripheral", "peripheral": "ch8",
                                                    "command": "on", "for_seconds": 8,
                                                    "then": "off"}]}])
    def test_anything_else_is_not(self, over):
        assert pending.hold_targets([rule(**over)], CFG, NOW, site_tz="UTC") == []


def target(peripheral="ch8"):
    return pending.Target("siren", peripheral, "on", "off", 600, 0, "realcam", "region_1")


def hold_row(slug="ch8", **kw):
    base = dict(slug=slug, hold_camera="realcam", hold_zone="region_1", hold_grace_s=0,
                empty_since=None, due_at=NOW + timedelta(minutes=10))
    base.update(kw)
    return SimpleNamespace(**base)


class TestDecide:
    def test_a_person_starts_a_hold_at_once(self):
        start, end = pending.decide([target()], [], {"realcam": presence(1)}, now=NOW, busy=set())
        assert [t.peripheral for t in start] == ["ch8"] and end == []

    def test_an_empty_zone_starts_nothing(self):
        assert pending.decide([target()], [], {"realcam": presence(0)}, now=NOW, busy=set()) == ([], [])

    def test_a_running_hold_is_not_started_again(self):
        """Re-sending ON every tick would undo an operator's manual OFF."""
        start, _ = pending.decide([target()], [hold_row()], {"realcam": presence(1)},
                                  now=NOW, busy=set())
        assert start == []

    def test_a_device_with_a_call_in_flight_or_backing_off_is_left_alone(self):
        start, _ = pending.decide([target()], [], {"realcam": presence(1)}, now=NOW, busy={"ch8"})
        assert start == []

    def test_two_rules_one_siren_one_on(self):
        start, _ = pending.decide([target(), target()], [], {"realcam": presence(1)},
                                  now=NOW, busy=set())
        assert len(start) == 1

    def test_the_zone_reading_empty_ends_the_hold_at_once(self):
        h = hold_row()
        _, end = pending.decide([], [h], {"realcam": presence(0)}, now=NOW, busy=set())
        assert end == [h] and h.due_at == NOW

    def test_an_occupied_zone_keeps_the_hold_and_resets_any_grace(self):
        h = hold_row(empty_since=NOW - timedelta(seconds=1))
        _, end = pending.decide([], [h], {"realcam": presence(1)}, now=NOW, busy=set())
        assert end == [] and h.empty_since is None and h.due_at > NOW

    def test_a_grace_when_a_rule_asks_for_one(self):
        h = hold_row(hold_grace_s=2)
        assert pending.decide([], [h], {"realcam": presence(0)}, now=NOW, busy=set())[1] == []
        later = NOW + timedelta(seconds=2)
        assert pending.decide([], [h], {"realcam": presence(0)}, now=later, busy=set())[1] == [h]

    def test_analytics_down_ends_a_hold(self):
        h = hold_row()
        assert pending.decide([], [h], {"realcam": None}, now=NOW, busy=set())[1] == [h]

    def test_ending_is_never_blocked_by_a_busy_device(self):
        h = hold_row()
        assert pending.decide([], [h], {"realcam": presence(0)}, now=NOW, busy={"ch8"})[1] == [h]

    def test_a_hold_is_never_pushed_later(self):
        h = hold_row(due_at=NOW - timedelta(seconds=1))
        assert pending.decide([], [h], {"realcam": presence(0)}, now=NOW, busy=set())[1] == []
        assert h.due_at == NOW - timedelta(seconds=1)


class TestLoopState:
    def test_a_failure_backs_off_then_clears(self):
        st = pending._LoopState()
        st.in_flight.add("ch1")
        st.retry_after["ch8"] = 100.0
        assert st.busy(99.0) == {"ch1", "ch8"}
        assert st.busy(101.0) == {"ch1"}


class TestTheAction:
    def test_until_needs_then(self):
        from pydantic import ValidationError
        from backend.models import PeripheralAction
        with pytest.raises(ValidationError):
            PeripheralAction(type="peripheral", peripheral="siren", command="on",
                             until="zone_empty")
        assert PeripheralAction(type="peripheral", peripheral="siren", command="on",
                                until="zone_empty", then="off").until == "zone_empty"

    def _dispatch(self, monkeypatch, reading):
        from backend.services import rules_engine
        calls = []

        class FakeBridge:
            async def hold(self, db, key, command, **kw):
                calls.append((key, command, kw))
                return SimpleNamespace(ok=True, detail="held")

        async def read(cam):
            return reading

        async def no_audit(*a, **kw):
            return None
        monkeypatch.setattr(rules_engine, "get_bridge", lambda: FakeBridge())
        monkeypatch.setattr(pending, "read_presence", read)
        monkeypatch.setattr(rules_engine.audit_svc, "record", no_audit)
        monkeypatch.setattr(rules_engine, "_why_not", lambda *a: None)
        r = rule()
        out = asyncio_run(rules_engine.dispatch_actions(
            None, r, SimpleNamespace(id="a1", created_at=NOW), None, None, False, False,
            event={"camera_slug": "realcam", "zone": "Zone 1"}))
        return out, calls

    def test_the_event_confirms_a_running_hold(self, monkeypatch):
        import time
        p = presence(1)
        p["ts"] = time.time()
        out, calls = self._dispatch(monkeypatch, p)
        assert out == {"ch8": "held"}
        key, command, kw = calls[0]
        assert (kw["camera"], kw["zone"], kw["then"], kw["grace_s"]) == ("realcam", "Zone 1", "off", 0)

    def test_an_event_after_the_person_left_does_not_chirp_the_siren(self, monkeypatch):
        import time
        p = presence(0)
        p["ts"] = time.time()
        out, calls = self._dispatch(monkeypatch, p)
        assert out == {"ch8": "zone_already_empty"} and calls == []


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
