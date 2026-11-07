"""The Home Assistant driver: service mapping, and why entity ids are resolved
at runtime rather than trusted.

HA 2026.5 changed managed go2rtc stream identifiers to prefer camera unique ids
over entity ids and broke playback across several integrations. Entity ids are a
moving target on a monthly release cadence, so a binding that has never been
confirmed to resolve refuses to actuate — a loud refusal beats a silent no-op.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_ha.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.bridge import base  # noqa: E402
from backend.services.bridge import homeassistant as ha  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)

#: Distinctive, so "it did not leak" is a real assertion. A long-lived HA token
#: does not expire, which is why a leak has such a long tail.
HA_TOKEN = "eyJhb-long-lived-ha-token-7d31c9"


def binding(address="switch.gate_3_barrier", **kw):
    base_ = {"peripheral_slug": "gate-3-barrier", "driver": "homeassistant",
             "address": address, "config": {}, "state_topic": None,
             "verified_at": NOW}
    base_.update(kw)
    return base.Binding(**base_)


class FakeHTTP:
    def __init__(self, status=200):
        self.status = status
        self.calls = []
        self.fail = False

    async def post(self, url, json=None, headers=None):
        if self.fail:
            raise ConnectionError("ha unreachable")
        self.calls.append({"url": url, "json": json, "headers": headers})
        status = self.status

        class R:
            status_code = status
        return R()


@pytest.fixture
def driver(monkeypatch):
    d = ha.HaDriver(url="http://ha.local:8123", token=HA_TOKEN)
    http = FakeHTTP()
    monkeypatch.setattr(d, "_http", http, raising=False)
    d._mark(True)
    return d, http


# ── Service mapping ──────────────────────────────────────────────────────────

def test_the_domain_comes_from_the_entity_id():
    assert ha.service_for("switch.gate_3", "open") == ("switch", "turn_on")
    assert ha.service_for("light.yard", "off") == ("light", "turn_off")


def test_lock_commands_map_to_lock_services():
    assert ha.service_for("lock.side_door", "unlock") == ("lock", "unlock")
    assert ha.service_for("lock.side_door", "lock") == ("lock", "lock")


def test_a_cover_opens_and_closes_rather_than_turning_on():
    """A boom gate is a cover in HA, and cover.turn_on does not exist — which
    is why this is not one generic command map."""
    assert ha.service_for("cover.boom_gate", "open") == ("cover", "open_cover")
    assert ha.service_for("cover.boom_gate", "close") == ("cover", "close_cover")


@pytest.mark.parametrize("command,service", [
    ("on", "turn_on"), ("open", "turn_on"), ("unlock", "turn_on"),
    ("flash", "turn_on"), ("trigger", "turn_on"), ("start", "turn_on"),
    ("off", "turn_off"), ("close", "turn_off"), ("lock", "turn_off"),
    ("stop", "turn_off"),
])
def test_a_switch_takes_the_relay_vocabulary(command, service):
    """A switch is usually a relay channel. The device gateway accepts these
    words for a relay, so a rule written as "unlock" must move the same relay
    whether it is reached through the gateway or through HA."""
    assert ha.service_for("switch.relay_ch1", command) == ("switch", service)


@pytest.mark.parametrize("entity,command,expected", [
    ("media_player.lobby", "announce", ("media_player", "play_media")),
    ("media_player.lobby", "stop", ("media_player", "media_stop")),
    ("media_player.lobby", "on", ("media_player", "turn_on")),
    ("scene.lockdown", "run", ("scene", "turn_on")),
    ("script.evacuate", "run", ("script", "turn_on")),
    ("script.evacuate", "stop", ("script", "turn_off")),
    ("button.doorbell_chime", "press", ("button", "press")),
    ("fan.server_room", "on", ("fan", "turn_on")),
    ("valve.sprinkler", "open", ("valve", "open_valve")),
    ("valve.sprinkler", "close", ("valve", "close_valve")),
])
def test_the_added_device_types(entity, command, expected):
    assert ha.service_for(entity, command) == expected


def test_a_scene_cannot_be_turned_off():
    """HA has no scene.turn_off — a scene is applied, not held."""
    assert ha.service_for("scene.lockdown", "off") is None


def test_an_unknown_command_maps_to_nothing():
    assert ha.service_for("switch.gate_3", "levitate") is None


def test_an_unknown_domain_maps_to_nothing():
    """A climate entity is not something this vocabulary drives, and guessing
    a service name for it would be a call HA rejects at best."""
    assert ha.service_for("climate.office", "open") is None


def test_an_entity_id_with_no_domain_maps_to_nothing():
    assert ha.service_for("gate_3", "open") is None


def test_an_empty_entity_id_maps_to_nothing():
    assert ha.service_for("", "open") is None


# ── Actuation ────────────────────────────────────────────────────────────────

@_aio
async def test_actuate_calls_the_right_service_endpoint(driver):
    d, http = driver
    r = await d.actuate(binding(), "open", {})
    assert r.ok is True
    assert http.calls[0]["url"].endswith("/api/services/switch/turn_on")
    assert http.calls[0]["json"] == {"entity_id": "switch.gate_3_barrier"}


@_aio
async def test_an_unknown_command_refuses_before_calling(driver):
    d, http = driver
    r = await d.actuate(binding(), "levitate", {})
    assert r.ok is False
    assert r.detail == "unsupported_command"
    assert http.calls == []


@_aio
async def test_an_unreachable_ha_is_a_refusal_not_an_exception(driver):
    d, http = driver
    http.fail = True
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False
    assert r.detail == "call_failed"


@_aio
async def test_a_non_2xx_is_a_refusal(driver):
    """HA answers 404 for an entity that no longer exists — exactly the rename
    case runtime verification exists for."""
    d, http = driver
    http.status = 404
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False
    assert r.detail == "call_rejected"


@_aio
async def test_actuating_while_disconnected_refuses_without_calling(driver):
    d, http = driver
    d._connected = False
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False and r.detail == "offline"
    assert http.calls == []


@_aio
async def test_params_ride_along_to_the_service(driver):
    """brightness, position, and anything else the service takes."""
    d, http = driver
    await d.actuate(binding("light.yard"), "on", {"brightness_pct": 40})
    assert http.calls[0]["json"] == {"entity_id": "light.yard",
                                     "brightness_pct": 40}


@_aio
async def test_an_announcement_needs_something_to_play(driver):
    """Refused before the call, with a reason the audit row can show —
    rather than an opaque 400 from HA."""
    d, http = driver
    r = await d.actuate(binding("media_player.lobby"), "announce", {})
    assert r.ok is False and r.detail == "missing_param:media_content_id"
    assert http.calls == []


@_aio
async def test_an_announcement_ducks_by_default(driver):
    d, http = driver
    url = "http://192.168.1.200:8099/sounds/evacuate.mp3"
    r = await d.actuate(binding("media_player.lobby"), "announce",
                        {"media_content_id": url})
    assert r.ok is True
    assert http.calls[0]["url"].endswith("/api/services/media_player/play_media")
    assert http.calls[0]["json"] == {"media_content_id": url,
                                     "media_content_type": "music",
                                     "announce": True,
                                     "entity_id": "media_player.lobby"}


@_aio
async def test_a_rule_can_override_the_announcement_defaults(driver):
    d, http = driver
    await d.actuate(binding("media_player.lobby"), "announce",
                    {"media_content_id": "x", "announce": False})
    assert http.calls[0]["json"]["announce"] is False


@pytest.mark.parametrize("entity,raw,expected", [
    ("switch.relay_ch1", "ON", "on"),
    ("media_player.lobby", "playing", "playing"),
    ("button.chime", "2026-10-26T19:07:58.123+00:00", "ready"),
    ("scene.lockdown", "2026-10-26T19:07:58+00:00", "ready"),
    ("button.chime", "unavailable", None),
    ("switch.relay_ch1", "unknown", None),
    ("switch.relay_ch1", "", None),
])
def test_wall_state(entity, raw, expected):
    """A button's or scene's HA state is the time it was last pressed. That
    is not a condition to put on the wall."""
    assert ha.wall_state(entity, raw) == expected


@_aio
async def test_params_cannot_override_the_entity_id(driver):
    """Otherwise a rule's params become a way to drive a device the boundary
    rule never approved — the whole of resolve_target bypassed by a dict key."""
    d, http = driver
    await d.actuate(binding("switch.gate_3_barrier"), "open",
                    {"entity_id": "lock.front_door"})
    assert http.calls[0]["json"]["entity_id"] == "switch.gate_3_barrier"


# ── Health and credentials ───────────────────────────────────────────────────

@_aio
async def test_health_is_not_configured_without_a_url():
    d = ha.HaDriver(url="", token=None)
    assert await d.health() == "not_configured"


@_aio
async def test_health_is_not_configured_without_a_token():
    """A URL with no token cannot call anything. Reporting "offline" would send
    somebody looking at the network."""
    d = ha.HaDriver(url="http://ha.local:8123", token=None)
    assert await d.health() == "not_configured"


class ProbeHTTP:
    """Answers GET /api/ with a fixed status, or fails to connect."""

    def __init__(self, status=200, fail=False):
        self.status, self.fail = status, fail
        self.gets = []

    async def get(self, url, headers=None):
        self.gets.append({"url": url, "headers": headers})
        if self.fail:
            raise ConnectionError("connect refused http://ha.local:8123")
        status = self.status

        class R:
            status_code = status
        return R()


def _probing(monkeypatch, http):
    d = ha.HaDriver(url="http://ha.local:8123", token=HA_TOKEN)
    monkeypatch.setattr(d, "_http", http, raising=False)
    return d


@_aio
async def test_health_probes_ha_on_first_use(monkeypatch):
    """THE STUB THIS REPLACES never connected: _connected stayed False, so every
    HA command was refused `offline` without a request leaving the process."""
    http = ProbeHTTP(200)
    d = _probing(monkeypatch, http)
    assert await d.health() == "connected"
    assert http.gets[0]["url"] == "http://ha.local:8123/api/"
    assert http.gets[0]["headers"]["Authorization"] == f"Bearer {HA_TOKEN}"


@_aio
async def test_a_rejected_token_is_offline_and_says_unauthorized(monkeypatch):
    """"Fix the token" and "fix the network" send somebody to different places."""
    d = _probing(monkeypatch, ProbeHTTP(401))
    assert await d.health() == "offline"
    assert d.last_error == "unauthorized"


@_aio
async def test_an_unreachable_ha_is_offline_and_says_unreachable(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    d = _probing(monkeypatch, ProbeHTTP(fail=True))
    assert await d.health() == "offline"
    assert d.last_error == "unreachable"
    assert HA_TOKEN not in caplog.text


@_aio
async def test_a_good_probe_is_believed_for_a_while(monkeypatch):
    """Bridge.actuate asks health() before EVERY command; probing each time
    would put a round trip in front of every siren."""
    http = ProbeHTTP(200)
    d = _probing(monkeypatch, http)
    await d.health()
    await d.health()
    assert len(http.gets) == 1


@_aio
async def test_an_offline_driver_probes_again_after_the_retry(monkeypatch):
    """...and a dead HA must not cost every rule a timeout either — but it must
    come back on its own when HA does."""
    http = ProbeHTTP(fail=True)
    d = _probing(monkeypatch, http)
    assert await d.health() == "offline"
    assert await d.health() == "offline"
    assert len(http.gets) == 1                  # inside RETRY_S: no new probe
    http.fail = False
    d._checked_at -= ha.RETRY_S + 1             # time passes
    assert await d.health() == "connected"


@_aio
async def test_a_call_that_cannot_reach_ha_marks_it_offline_at_once(driver):
    """Not at the next probe: the next rule must not wait a timeout to learn
    what this one already did."""
    d, http = driver
    http.fail = True
    await d.actuate(binding(), "open", {})
    assert d._connected is False and d.last_error == "unreachable"


@_aio
async def test_a_token_rejected_mid_call_is_reported_as_unauthorized(driver):
    d, http = driver
    http.status = 401
    r = await d.actuate(binding(), "open", {})
    assert r.ok is False and r.detail == "call_rejected:unauthorized"
    assert d.last_error == "unauthorized"


@_aio
async def test_reconfigure_is_a_no_op_when_nothing_changed(monkeypatch):
    """The sync loop calls it every few seconds in every worker."""
    http = ProbeHTTP(200)
    d = _probing(monkeypatch, http)
    await d.health()
    assert await d.reconfigure("http://ha.local:8123/", HA_TOKEN) is False
    assert d._http is http and d._connected is True


@_aio
async def test_reconfigure_forgets_the_old_verdict(monkeypatch):
    """A probe against the old URL says nothing about the new one."""
    http = ProbeHTTP(200)
    d = _probing(monkeypatch, http)
    http.aclose = lambda: _noop()
    await d.health()
    assert await d.reconfigure("http://other:8123", "new-token") is True
    assert d._connected is False and d._checked_at is None
    assert d._url == "http://other:8123" and d._token == "new-token"


@_aio
async def test_reconfigure_to_nothing_is_not_configured(monkeypatch):
    d = _probing(monkeypatch, ProbeHTTP(200))
    d._http.aclose = lambda: _noop()
    await d.reconfigure("", None)
    assert await d.health() == "not_configured"


async def _noop():
    return None


class StatesHTTP(ProbeHTTP):
    """GET /api/ -> 200; GET /api/states -> a list of entity states."""

    def __init__(self, states):
        super().__init__(200)
        self.states = states

    async def get(self, url, headers=None):
        self.gets.append({"url": url, "headers": headers})
        states = self.states

        class R:
            status_code = 200

            @staticmethod
            def json():
                return states
        return R()


@_aio
async def test_entities_lists_only_what_the_driver_can_drive(monkeypatch):
    """A sensor or a climate entity would be an import that refuses on every
    press."""
    http = StatesHTTP([
        {"entity_id": "switch.relay_ch2", "state": "off",
         "attributes": {"friendly_name": "Relay CH2"}},
        {"entity_id": "sensor.temperature", "state": "21"},
        {"entity_id": "climate.office", "state": "heat"},
        {"entity_id": "light.yard", "state": "on", "attributes": {}},
        {"not": "an entity"},
    ])
    d = _probing(monkeypatch, http)
    found = await d.entities()
    assert [e["entity_id"] for e in found] == ["light.yard", "switch.relay_ch2"]
    relay = found[1]
    assert relay == {"entity_id": "switch.relay_ch2", "domain": "switch",
                     "name": "Relay CH2", "state": "off"}
    assert found[0]["name"] == "light.yard"     # no friendly name: the id


@_aio
async def test_entities_is_none_when_ha_cannot_be_asked(monkeypatch):
    d = _probing(monkeypatch, ProbeHTTP(fail=True))
    assert await d.entities() is None


@_aio
async def test_stop_closes_the_client():
    d = ha.HaDriver(url="http://ha.local:8123", token=HA_TOKEN)

    class Closable:
        closed = False

        async def aclose(self):
            Closable.closed = True
    d._http = Closable()
    await d.stop()
    assert Closable.closed and d._http is None and d._connected is False


@_aio
async def test_the_token_is_never_logged_or_returned(driver, caplog):
    """A long-lived HA token in a log line is a credential leak with a long
    tail — they do not expire."""
    import logging
    caplog.set_level(logging.DEBUG)
    d, http = driver
    http.fail = True
    r = await d.actuate(binding(), "open", {})
    assert HA_TOKEN not in r.detail
    assert HA_TOKEN not in caplog.text


@_aio
async def test_the_token_goes_in_the_header_not_the_body(driver):
    """A token in a JSON body ends up in a proxy log. The header is the one
    place it belongs."""
    d, http = driver
    await d.actuate(binding(), "open", {})
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {HA_TOKEN}"
    assert HA_TOKEN not in str(http.calls[0]["json"])
