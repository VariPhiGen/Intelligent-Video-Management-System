"""Binding verification: the half that decides whether anything can act at all.

`base.resolve_target` refuses a binding whose `verified_at` is NULL, which is
the right default — an address the bridge has never confirmed may point at a
renamed entity, and a silent no-op at 2am is the worst outcome. But it means
that until something STAMPS that column, every binding is permanently
unverified and the entire feature is inert. That was true of the first cut of
this module and is what these tests exist to prevent recurring.

WHAT "VERIFY" MEANS IS PER DRIVER, and one of them deliberately does nothing:

  mqtt   a topic cannot be resolved — there is nobody to ask whether it
         "exists". The meaningful check is that the broker session is live.
  ha     GET /api/states/<entity_id>. This is the one that actually moves:
         entity ids change between monthly releases.
  webhook NOT PROBED. A request to an operator-supplied URL could actuate the
         device, and verifying a barrier by opening it is not verification.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_verify.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import bridge  # noqa: E402
from backend.services.bridge import base  # noqa: E402
from backend.services.bridge import homeassistant as ha  # noqa: E402
from backend.services.bridge import mqtt as mqtt_mod  # noqa: E402
from backend.services.bridge import webhook as wh  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def binding(driver="mqtt", address="site/gate3/set", **kw):
    base_ = {"peripheral_slug": "gate-3-barrier", "driver": driver,
             "address": address, "config": {}, "state_topic": None,
             "verified_at": None}
    base_.update(kw)
    return base.Binding(**base_)


# ── MQTT ─────────────────────────────────────────────────────────────────────

@_aio
async def test_mqtt_verifies_when_the_session_is_live():
    d = mqtt_mod.MqttDriver(url="mqtt://b:1883", username=None, password=None,
                            tls_verify=True, topic_prefix="vms")
    d._connected = True
    assert (await d.verify(binding())).ok is True


@_aio
async def test_mqtt_does_not_verify_while_disconnected():
    """Stamping verified on a dead session would let the next actuation through
    on the strength of a check that never happened."""
    d = mqtt_mod.MqttDriver(url="mqtt://b:1883", username=None, password=None,
                            tls_verify=True, topic_prefix="vms")
    d._connected = False
    r = await d.verify(binding())
    assert r.ok is False and r.detail == "offline"


@_aio
async def test_mqtt_refuses_to_verify_a_looping_topic_pair():
    """The save path already refuses this, but a row written by hand or by an
    older version must not be verified into usability."""
    d = mqtt_mod.MqttDriver(url="mqtt://b:1883", username=None, password=None,
                            tls_verify=True, topic_prefix="vms")
    d._connected = True
    r = await d.verify(binding(state_topic="site/gate3/#"))
    assert r.ok is False and r.detail == "topic_loop"


# ── Home Assistant ───────────────────────────────────────────────────────────

class FakeHTTP:
    def __init__(self, status=200):
        self.status = status
        self.calls = []
        self.fail = False

    async def get(self, url, headers=None):
        if self.fail:
            raise ConnectionError("unreachable")
        self.calls.append(url)
        status = self.status

        class R:
            status_code = status
        return R()


@_aio
async def test_ha_verifies_an_entity_that_resolves(monkeypatch):
    d = ha.HaDriver(url="http://ha.local:8123", token="t")
    http = FakeHTTP(200)
    monkeypatch.setattr(d, "_http", http, raising=False)
    d._mark(True)
    r = await d.verify(binding("homeassistant", "switch.gate_3"))
    assert r.ok is True
    assert http.calls[0].endswith("/api/states/switch.gate_3")


@_aio
async def test_ha_does_not_verify_an_entity_that_has_gone(monkeypatch):
    """THE case runtime verification exists for. 2026.5 moved managed go2rtc
    identifiers and broke integrations exactly this way; the binding has to
    become visibly unverified rather than silently doing nothing."""
    d = ha.HaDriver(url="http://ha.local:8123", token="t")
    monkeypatch.setattr(d, "_http", FakeHTTP(404), raising=False)
    d._mark(True)
    r = await d.verify(binding("homeassistant", "switch.renamed"))
    assert r.ok is False and r.detail == "entity_missing"


@_aio
async def test_ha_verification_failure_is_not_an_exception(monkeypatch):
    d = ha.HaDriver(url="http://ha.local:8123", token="t")
    http = FakeHTTP()
    http.fail = True
    monkeypatch.setattr(d, "_http", http, raising=False)
    d._mark(True)
    r = await d.verify(binding("homeassistant", "switch.g"))
    # `offline`, not `call_failed`: HA being unreachable says nothing about the
    # entity id, so verify_bindings must KEEP the stamp (_COULD_NOT_CHECK)
    # rather than lock every HA device out until the next tick.
    assert r.ok is False and r.detail == "offline"
    assert d._connected is False and d.last_error == "unreachable"


@_aio
async def test_ha_refuses_to_verify_a_command_it_could_never_send(monkeypatch):
    """An entity whose domain this driver cannot drive resolves in HA and is
    still useless here. Verifying it would promise a control that refuses."""
    d = ha.HaDriver(url="http://ha.local:8123", token="t")
    monkeypatch.setattr(d, "_http", FakeHTTP(200), raising=False)
    d._mark(True)
    r = await d.verify(binding("homeassistant", "climate.office"))
    assert r.ok is False and r.detail == "unsupported_domain"


class StateHTTP(FakeHTTP):
    """A /api/states reply with a body."""

    def __init__(self, state, status=200):
        super().__init__(status)
        self.state = state

    async def get(self, url, headers=None):
        self.calls.append(url)
        status, state = self.status, self.state

        class R:
            status_code = status

            @staticmethod
            def json():
                return {"entity_id": "switch.relay_ch1", "state": state}
        return R()


@_aio
async def test_ha_verify_carries_the_entity_state_to_the_wall(monkeypatch):
    """verify_bindings writes a verify's state to last_state — so the wall
    shows on/off from HA before any state subscription exists."""
    d = ha.HaDriver(url="http://ha.local:8123", token="t")
    monkeypatch.setattr(d, "_http", StateHTTP("on"), raising=False)
    d._mark(True)
    r = await d.verify(binding("homeassistant", "switch.relay_ch1"))
    assert r.ok is True and r.state == "on"


@_aio
async def test_ha_unavailable_is_an_unhealthy_device_not_an_off(monkeypatch):
    """Seen on the appliance: the relay board's PoE cable dropped and HA kept
    the entity as `unavailable`. That is not a state to paint on the wall; the
    address is right and the DEVICE is not answering — `unhealthy:`, which
    keeps the stamp and blanks the wall."""
    d = ha.HaDriver(url="http://ha.local:8123", token="t")
    monkeypatch.setattr(d, "_http", StateHTTP("unavailable"), raising=False)
    d._mark(True)
    r = await d.verify(binding("homeassistant", "switch.relay_ch1"))
    assert r.ok is False
    assert r.detail == "unhealthy:homeassistant/unavailable"
    assert r.detail.startswith(bridge.UNHEALTHY)


# ── Webhook ──────────────────────────────────────────────────────────────────

@_aio
async def test_a_webhook_is_verified_by_SHAPE_and_never_probed():
    """NOT A ROUND TRIP, deliberately. A request to an operator-supplied URL
    could open the barrier it describes, and verifying a gate by opening it is
    not verification."""
    d = wh.WebhookDriver(enabled=True)
    probed = []

    class Probe:
        async def post(self, *a, **kw):
            probed.append(a)

        async def get(self, *a, **kw):
            probed.append(a)

    d._http = Probe()
    assert (await d.verify(binding("webhook", "https://relay.local/gate3"))).ok is True
    assert probed == [], "verification must not call the operator's URL"


@_aio
async def test_a_webhook_with_a_bad_address_does_not_verify():
    d = wh.WebhookDriver(enabled=True)
    d._http = object()
    r = await d.verify(binding("webhook", "relay.local/gate3"))
    assert r.ok is False and r.detail == "bad_address"


# ── The pass over every binding ──────────────────────────────────────────────

class Row:
    def __init__(self, driver="mqtt", address="site/gate3/set"):
        self.peripheral_id = 1
        self.driver = driver
        self.address = address
        self.config = {}
        self.state_topic = None
        self.verified_at = None
        self.verify_error = None


class FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0

    async def execute(self, *a, **kw):
        from types import SimpleNamespace
        rows = self._rows
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: rows,
                                            first=lambda: rows[0] if rows else None))

    async def commit(self):
        self.commits += 1


@_aio
async def test_a_verified_binding_gets_a_timestamp_and_no_error(monkeypatch):
    """Without this the whole feature is inert: resolve_target refuses a NULL
    verified_at, so nothing can ever actuate."""
    row = Row()
    b = bridge.Bridge()

    class OK:
        async def verify(self, binding):
            return base.ActuationResult(True, "resolved")

    b._drivers = {"mqtt": OK()}
    db = FakeDB([row])
    assert await b.verify_bindings(db, now=NOW) == 1
    assert row.verified_at == NOW
    assert row.verify_error is None


@_aio
async def test_a_failed_verification_clears_the_stamp_and_records_why():
    """A binding that STOPS resolving must lose its verification, or a renamed
    entity keeps actuating on a check that passed last month."""
    row = Row(driver="homeassistant", address="switch.renamed")
    row.verified_at = NOW
    b = bridge.Bridge()

    class Gone:
        async def verify(self, binding):
            return base.ActuationResult(False, "entity_missing")

    b._drivers = {"homeassistant": Gone()}
    db = FakeDB([row])
    assert await b.verify_bindings(db, now=NOW) == 0
    assert row.verified_at is None
    assert row.verify_error == "entity_missing"


@_aio
async def test_a_broker_outage_does_not_strip_a_stamp():
    """The kill-the-broker step. `offline` means the check could not run, not
    that the address is wrong — and stripping on it turned every broker blip
    into a lockout of up to BRIDGE_VERIFY_INTERVAL_S after the session came
    back, with the UI blaming the address. actuate() refuses on a dead session
    already, so the stamp is not what stands between an outage and a gate."""
    row = Row()
    row.verified_at = NOW
    b = bridge.Bridge()

    class Down:
        async def verify(self, binding):
            return base.ActuationResult(False, "offline")

    b._drivers = {"mqtt": Down()}
    db = FakeDB([row])
    assert await b.verify_bindings(db, now=NOW) == 1
    assert row.verified_at == NOW           # kept
    assert row.verify_error == "offline"    # but the operator can see why


@_aio
async def test_a_broker_outage_does_not_verify_what_was_never_verified():
    """The other half of the rule above: keeping a stamp is not granting one."""
    row = Row()
    b = bridge.Bridge()

    class Down:
        async def verify(self, binding):
            return base.ActuationResult(False, "offline")

    b._drivers = {"mqtt": Down()}
    db = FakeDB([row])
    assert await b.verify_bindings(db, now=NOW) == 0
    assert row.verified_at is None
    assert row.verify_error == "offline"


@_aio
async def test_a_binding_for_an_unconfigured_driver_is_not_verified():
    row = Row(driver="homeassistant", address="switch.g")
    b = bridge.Bridge()
    b._drivers = {}
    db = FakeDB([row])
    assert await b.verify_bindings(db, now=NOW) == 0
    assert row.verified_at is None
    assert row.verify_error == "driver_not_configured"


@_aio
async def test_a_driver_that_raises_during_verification_is_not_a_crash():
    """The verify tick is a background loop. One bad driver must not stop every
    other binding being checked."""
    rows = [Row(), Row(driver="homeassistant", address="switch.g")]
    b = bridge.Bridge()

    class Boom:
        async def verify(self, binding):
            raise RuntimeError("boom")

    class OK:
        async def verify(self, binding):
            return base.ActuationResult(True, "resolved")

    b._drivers = {"mqtt": Boom(), "homeassistant": OK()}
    db = FakeDB(rows)
    assert await b.verify_bindings(db, now=NOW) == 1
    assert rows[0].verify_error == "verify_error"
    assert rows[1].verified_at == NOW


# ── Device-gateway health (webhook bindings with config.health) ──────────────

class GwResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class GwHTTP:
    def __init__(self, resp=None, raises=None):
        self.resp, self.raises, self.gets, self.posts = resp, raises, [], []

    async def get(self, url, **kw):
        self.gets.append(url)
        if self.raises:
            raise self.raises
        return self.resp

    async def post(self, url, **kw):
        self.posts.append((url, kw))
        if self.raises:
            raise self.raises
        return self.resp


GW = "http://127.0.0.1:8099/relay/192.168.1.200/8"


def gw_binding(**cfg):
    return binding("webhook", GW, config={"health": True, **cfg})


@_aio
async def test_a_gateway_binding_is_verified_by_its_read_only_health_check():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(GwResp(200, {"ok": True, "protocol": "modbus-tcp", "state": "on"}))
    r = await d.verify(gw_binding())
    assert (r.ok, r.detail, r.state) == (True, "healthy", "on")
    assert d._http.gets == [GW] and d._http.posts == [], "health is a GET, never a POST"


@_aio
async def test_an_unhealthy_device_names_its_protocol_and_reason():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(GwResp(502, {"ok": False, "protocol": "modbus-tcp", "error": "timeout"}))
    r = await d.verify(gw_binding())
    assert (r.ok, r.detail) == (False, "unhealthy:modbus-tcp/timeout")


@_aio
async def test_a_gateway_that_is_down_says_so():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(raises=ConnectionError("refused"))
    r = await d.verify(gw_binding())
    assert (r.ok, r.detail) == (False, "unhealthy:gateway_unreachable")


@_aio
async def test_a_reply_that_is_not_the_gateways_shape_is_an_http_status():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(GwResp(500, ValueError("not json")))
    assert (await d.verify(gw_binding())).detail == "unhealthy:http_500"


@_aio
async def test_a_device_cannot_write_anything_it_likes_into_the_detail():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(GwResp(502, {"protocol": "Modbus TCP!", "error": "<script>x</script>" * 9}))
    detail = (await d.verify(gw_binding())).detail
    assert detail.startswith("unhealthy:modbus_tcp/") and "<" not in detail and len(detail) < 100


@_aio
async def test_a_rejected_command_says_which_protocol_failed_and_why():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(GwResp(502, {"ok": False, "protocol": "modbus-tcp", "error": "no_reply"}))
    r = await d.actuate(gw_binding(), "on", {})
    assert (r.ok, r.detail) == (False, "call_rejected:modbus-tcp/no_reply")


@_aio
async def test_a_rejection_without_a_reason_keeps_the_old_token():
    d = wh.WebhookDriver(enabled=True)
    d._http = GwHTTP(GwResp(500, ValueError("html")))
    assert (await d.actuate(gw_binding(), "on", {})).detail == "call_rejected"


@_aio
async def test_the_driver_makes_its_own_client():
    """It used to have none: nothing called a start(), so every call failed
    `offline`. And health must not depend on this WORKER having made a call:
    actuate() refuses a driver that says not_configured, and only the elected
    worker runs the health checks."""
    d = wh.WebhookDriver(enabled=True)
    assert await d.health() == "connected"
    r = await d.actuate(binding("webhook", "http://127.0.0.1:1/x"), "off", {})
    assert r.detail == "call_failed" and d._http is not None
    assert await wh.WebhookDriver(enabled=False).health() == "not_configured"


@_aio
async def test_a_healthy_check_writes_the_devices_state_to_the_wall(monkeypatch):
    row = Row(driver="webhook", address=GW)
    written = []

    async def fake_write(db, pid, state, at):
        written.append((pid, state, at))
    monkeypatch.setattr(bridge, "_write_state", fake_write)
    b = bridge.Bridge()

    class Healthy:
        async def verify(self, binding):
            return base.ActuationResult(True, "healthy", "on")

    b._drivers = {"webhook": Healthy()}
    assert await b.verify_bindings(FakeDB([row]), now=NOW) == 1
    assert written == [(1, "on", NOW)] and row.verify_error is None


@_aio
async def test_an_unhealthy_device_keeps_its_stamp_but_shows_why():
    """One missed health check must not block an alarm; the attempt reports its
    own failure. But the reason is on the wall."""
    row = Row(driver="webhook", address=GW)
    row.verified_at = NOW
    b = bridge.Bridge()

    class Sick:
        async def verify(self, binding):
            return base.ActuationResult(False, "unhealthy:modbus-tcp/timeout")

    b._drivers = {"webhook": Sick()}
    assert await b.verify_bindings(FakeDB([row]), now=NOW) == 1
    assert row.verified_at == NOW and row.verify_error == "unhealthy:modbus-tcp/timeout"


@_aio
async def test_an_unhealthy_device_that_never_answered_is_not_verified():
    row = Row(driver="webhook", address=GW)
    b = bridge.Bridge()

    class Sick:
        async def verify(self, binding):
            return base.ActuationResult(False, "unhealthy:modbus-tcp/connection_refused")

    b._drivers = {"webhook": Sick()}
    assert await b.verify_bindings(FakeDB([row]), now=NOW) == 0
    assert row.verified_at is None


class BoundDB:
    """Answers "is any device bound to the webhook driver?"."""

    def __init__(self, bound: bool):
        self.bound = bound

    async def execute(self, *a, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(first=lambda: (1,) if self.bound else None)


@_aio
async def test_the_chip_says_no_bridge_when_no_device_uses_the_webhook():
    b = bridge.Bridge()
    b._drivers = {"webhook": wh.WebhookDriver(enabled=True)}
    assert (await b.health(BoundDB(False)))["state"] == "not_configured"


@_aio
async def test_the_chip_says_connected_once_a_device_is_bound_in_ANY_worker():
    """The bug this guards: health once depended on THIS worker having made a
    call, so the three workers that do not run health checks said
    not_configured -- the wall disabled every button and actuate() refused."""
    b = bridge.Bridge()
    b._drivers = {"webhook": wh.WebhookDriver(enabled=True)}
    assert b._drivers["webhook"]._http is None
    assert (await b.health(BoundDB(True)))["state"] == "connected"
