"""The bridge registry: routing to a driver, and refusing before it gets there.

`Bridge.actuate` is the ONLY entry point to physical actuation in the product.
Every refusal path is tested here, because a path that skips
base.resolve_target is how the boundary rule gets lost — quietly, in a driver
that seemed to have a good reason.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_registry.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import bridge  # noqa: E402
from backend.services.bridge import base  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


class RecordingDriver:
    name = "mqtt"

    def __init__(self, result=None, state="connected"):
        self.calls = []
        self.result = result or base.ActuationResult(True, "published")
        self.state = state

    async def health(self):
        return self.state

    async def actuate(self, binding, command, params):
        self.calls.append((binding.peripheral_slug, command, params))
        return self.result

    def subscribe_state(self, cb):
        pass


@pytest.fixture
def wired(monkeypatch):
    """A bridge with one driver and a lookup we control, so these tests need no
    database."""
    b = bridge.Bridge()
    driver = RecordingDriver()
    b._drivers = {"mqtt": driver}

    rows = {}

    async def _lookup(db, slug):
        return rows.get(slug, (None, None))

    monkeypatch.setattr(b, "_lookup", _lookup)
    return b, driver, rows


def _peripheral(slug="gate-3-barrier", enabled=True):
    return SimpleNamespace(id=7, slug=slug, name="Gate 3", enabled=enabled)


def _binding(driver="mqtt", verified=True):
    return base.Binding("gate-3-barrier", driver, "site/gate3/set", {},
                        "site/gate3/state", NOW if verified else None)


# ── The other entry point: a pulse ───────────────────────────────────────────
#
# pending.pulse owns the ordering (close committed before open publishes) and
# is tested in test_bridge_pending.py. What is pinned here is that it is
# REACHED, and through the same boundary as actuate(): it had no caller at
# all until a siren rule on the appliance fired an `on` with no `off`.

class _PulseSession:
    """Just enough of a session for pending.pulse: add + commit."""

    def __init__(self):
        self.added, self.commits = [], 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


@_aio
async def test_a_pulse_commits_its_follow_up_and_then_publishes(wired):
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(), _binding())
    db = _PulseSession()
    r = await b.pulse(db, "gate-3-barrier", "open", then="close", for_seconds=8,
                      params={}, alert_id=None, now=NOW)
    assert r.ok is True
    assert driver.calls == [("gate-3-barrier", "open", {})]
    row = db.added[0]
    assert row.peripheral_id == 7 and row.command == "close" and row.state == "pending"
    assert (row.due_at - NOW).total_seconds() == 8
    assert db.commits >= 1


@_aio
async def test_a_pulse_on_an_unknown_peripheral_leaves_no_follow_up(wired):
    """Refused at the boundary, BEFORE a row exists: a follow-up for a device
    the VMS does not own would retry, fail and raise a critical alert about
    somebody else's lamp."""
    b, driver, rows = wired
    db = _PulseSession()
    r = await b.pulse(db, "somebody-elses-lamp", "open", then="close",
                      for_seconds=8, params={}, alert_id=None, now=NOW)
    assert r.ok is False and r.detail == "unknown_peripheral"
    assert db.added == [] and driver.calls == []


@_aio
async def test_a_pulse_whose_open_is_refused_withdraws_its_follow_up(wired):
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(), _binding())
    driver.result = base.ActuationResult(False, "offline")
    db = _PulseSession()
    r = await b.pulse(db, "gate-3-barrier", "open", then="close", for_seconds=8,
                      params={}, alert_id=None, now=NOW)
    assert r.ok is False
    assert db.added[0].state == "abandoned"


# ── The one entry point ──────────────────────────────────────────────────────

@_aio
async def test_a_good_target_reaches_the_driver(wired):
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(), _binding())
    r = await b.actuate(None, "gate-3-barrier", "open", {})
    assert r.ok is True
    assert driver.calls == [("gate-3-barrier", "open", {})]


@_aio
async def test_an_unknown_peripheral_never_reaches_a_driver(wired):
    """THE boundary rule. Everything not in the VMS inventory is Home
    Assistant's business, and the customer's broker carries their whole smart
    home."""
    b, driver, rows = wired
    r = await b.actuate(None, "somebody-elses-lamp", "open", {})
    assert r.ok is False and r.detail == "unknown_peripheral"
    assert driver.calls == []


@_aio
async def test_a_disabled_peripheral_never_reaches_a_driver(wired):
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(enabled=False), _binding())
    r = await b.actuate(None, "gate-3-barrier", "open", {})
    assert r.ok is False and r.detail == "peripheral_disabled"
    assert driver.calls == []


@_aio
async def test_an_unverified_binding_never_reaches_a_driver(wired):
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(), _binding(verified=False))
    r = await b.actuate(None, "gate-3-barrier", "open", {})
    assert r.ok is False and r.detail == "binding_unverified"
    assert driver.calls == []


@_aio
async def test_a_peripheral_with_no_binding_never_reaches_a_driver(wired):
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(), None)
    r = await b.actuate(None, "gate-3-barrier", "open", {})
    assert r.ok is False and r.detail == "no_binding"
    assert driver.calls == []


@_aio
async def test_a_binding_naming_an_unconfigured_driver_refuses(wired):
    """A site that had HA configured and no longer does must not silently do
    nothing; the binding is still there and says so."""
    b, driver, rows = wired
    rows["gate-3-barrier"] = (_peripheral(), _binding(driver="homeassistant"))
    r = await b.actuate(None, "gate-3-barrier", "open", {})
    assert r.ok is False and r.detail == "driver_not_configured"
    assert driver.calls == []


@_aio
async def test_a_driver_that_raises_is_a_refusal_not_an_exception(wired):
    """The furthest caller is the ingest handler. A driver written later must
    not be able to 500 it by raising something nobody anticipated."""
    class Exploding:
        name = "mqtt"

        async def health(self):
            return "connected"

        async def actuate(self, *a, **kw):
            raise RuntimeError("boom")

        def subscribe_state(self, cb):
            pass

    b, _, rows = wired
    b._drivers = {"mqtt": Exploding()}
    rows["gate-3-barrier"] = (_peripheral(), _binding())
    r = await b.actuate(None, "gate-3-barrier", "open", {})
    assert r.ok is False and r.detail == "driver_error"


# ── Health ───────────────────────────────────────────────────────────────────

@_aio
async def test_health_with_no_drivers_is_not_configured():
    """The default deployment. Not a fault, and the UI must not draw it as one."""
    b = bridge.Bridge()
    b._drivers = {}
    assert (await b.health())["state"] == "not_configured"


@_aio
async def test_health_reports_each_driver(wired):
    b, _, _ = wired
    h = await b.health()
    assert h["state"] == "connected"
    assert h["drivers"]["mqtt"] == "connected"


@_aio
async def test_one_offline_driver_among_several_is_degraded_not_offline():
    """Partial is its own fact: "some devices unreachable" sends an operator to
    the right half of the site."""
    b = bridge.Bridge()
    b._drivers = {"mqtt": RecordingDriver(state="connected"),
                  "homeassistant": RecordingDriver(state="offline")}
    assert (await b.health())["state"] == "degraded"


@_aio
async def test_every_driver_offline_is_offline():
    b = bridge.Bridge()
    b._drivers = {"mqtt": RecordingDriver(state="offline")}
    assert (await b.health())["state"] == "offline"


@_aio
async def test_a_driver_that_is_merely_unconfigured_does_not_make_the_bridge_degraded():
    """An HA driver with no URL on an MQTT site is not a fault. Counting it as
    one would leave every MQTT-only deployment permanently "degraded"."""
    b = bridge.Bridge()
    b._drivers = {"mqtt": RecordingDriver(state="connected"),
                  "homeassistant": RecordingDriver(state="not_configured")}
    assert (await b.health())["state"] == "connected"
