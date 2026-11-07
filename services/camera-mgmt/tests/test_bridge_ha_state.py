"""Home Assistant's live state over its WebSocket.

The protocol is driven against a scripted fake socket: these must not reach a
real HA. What matters, in order:

  * only entities bound to a VMS peripheral reach the wall — HA carries the
    whole site, and the boundary rule applies inbound too;
  * `unavailable` (a relay board with its cable out) is UNKNOWN, never off;
  * a rejected token backs off instead of hammering HA;
  * the token appears in exactly one message: the auth frame.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_ha_state.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.bridge import ha_state  # noqa: E402

_aio = pytest.mark.asyncio

TOKEN = "eyJ-ha-ws-token-must-appear-once-only-9c1e"
BOUND = {"switch.relay_ch1": 11, "switch.relay_ch2": 12}


def state(entity_id, value):
    return {"entity_id": entity_id, "state": value, "attributes": {}}


def changed(entity_id, value):
    new = None if value is None else state(entity_id, value)
    return {"id": 1, "type": "event",
            "event": {"event_type": "state_changed",
                      "data": {"entity_id": entity_id, "new_state": new}}}


class FakeWS:
    """Plays HA's side from a script; records what the VMS sent."""

    def __init__(self, script):
        self.script = [json.dumps(m) if isinstance(m, dict) else m for m in script]
        self.sent = []

    async def recv(self):
        if not self.script:
            raise ConnectionError("closed by peer")
        item = self.script.pop(0)
        if item == "TIMEOUT":
            raise asyncio.TimeoutError()
        return item

    async def send(self, text):
        self.sent.append(json.loads(text))


def connector(ws):
    seen = {}

    class _CM:
        def __init__(self, url):
            seen["url"] = url

        async def __aenter__(self):
            return ws

        async def __aexit__(self, *exc):
            return False
    return _CM, seen


AUTH = [{"type": "auth_required"}, {"type": "auth_ok"}]


# ── Pure pieces ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("http,ws", [
    ("http://127.0.0.1:8123", "ws://127.0.0.1:8123/api/websocket"),
    ("http://127.0.0.1:8123/", "ws://127.0.0.1:8123/api/websocket"),
    ("https://ha.site:8443", "wss://ha.site:8443/api/websocket"),
])
def test_the_websocket_url_follows_the_http_one(http, ws):
    assert ha_state.ws_url(http) == ws


@pytest.mark.parametrize("raw,expected", [
    ({"state": "on"}, "on"),
    ({"state": "OFF"}, "off"),
    ({"state": "unavailable"}, None),
    ({"state": "unknown"}, None),
    ({"state": ""}, None),
    (None, None),                      # entity removed from HA
    ({}, None),
])
def test_state_values(raw, expected):
    assert ha_state.state_value(raw) == expected


def test_an_event_for_a_bound_entity_is_an_update():
    assert ha_state.updates_from(changed("switch.relay_ch1", "on"), BOUND) == [(11, "on")]


def test_an_event_for_anything_else_is_ignored():
    """sun.sun, the weather, every sensor on the site: not ours."""
    assert ha_state.updates_from(changed("sun.sun", "above_horizon"), BOUND) == []


def test_unavailable_is_unknown_not_off():
    """Seen on the appliance when the relay board's PoE cable dropped."""
    assert ha_state.updates_from(changed("switch.relay_ch1", "unavailable"),
                                 BOUND) == [(11, None)]


def test_a_pressed_button_is_ready_not_a_timestamp():
    bound = {"button.chime": 21}
    msg = changed("button.chime", "2026-10-26T19:07:58.123+00:00")
    assert ha_state.updates_from(msg, bound) == [(21, "ready")]


def test_other_event_types_are_ignored():
    msg = {"type": "event", "event": {"event_type": "call_service", "data": {}}}
    assert ha_state.updates_from(msg, BOUND) == []


def test_a_get_states_result_updates_every_bound_entity():
    msg = {"id": 2, "type": "result", "success": True,
           "result": [state("switch.relay_ch1", "off"), state("sun.sun", "x"),
                      state("switch.relay_ch2", "on")]}
    assert ha_state.updates_from(msg, BOUND) == [(11, "off"), (12, "on")]


def test_junk_is_ignored():
    for msg in (None, "x", [], {"type": "result", "result": None}, {"type": "pong"}):
        assert ha_state.updates_from(msg, BOUND) == []


# ── Handshake ────────────────────────────────────────────────────────────────

@_aio
async def test_handshake_sends_the_token_in_the_auth_frame():
    ws = FakeWS(AUTH)
    await ha_state.handshake(ws, TOKEN)
    assert ws.sent == [{"type": "auth", "access_token": TOKEN}]


@_aio
async def test_a_rejected_token_is_auth_failed():
    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_invalid"}])
    with pytest.raises(ha_state.AuthFailed):
        await ha_state.handshake(ws, TOKEN)


# ── A session ────────────────────────────────────────────────────────────────

async def _run_session(script, *, still_current=True, clock=None):
    ws = FakeWS(script)
    connect, seen = connector(ws)
    writes = []

    async def bound_ids():
        return dict(BOUND)

    async def write(updates, at):
        writes.extend(updates)

    async def current():
        return still_current

    kw = {"clock": clock} if clock else {}
    try:
        await ha_state.session("http://127.0.0.1:8123", TOKEN, connect=connect,
                               bound_ids=bound_ids, write=write,
                               still_current=current, **kw)
        ended = "returned"
    except ConnectionError:
        ended = "dropped"
    return ws, seen, writes, ended


@_aio
async def test_a_session_subscribes_syncs_and_follows_changes():
    ws, seen, writes, ended = await _run_session(AUTH + [
        {"id": 1, "type": "result", "success": True, "result": None},
        {"id": 2, "type": "result", "success": True,
         "result": [state("switch.relay_ch1", "off"), state("sun.sun", "x")]},
        changed("switch.relay_ch1", "on"),
        changed("weather.home", "rainy"),
        changed("switch.relay_ch1", "off"),
    ])
    assert seen["url"] == "ws://127.0.0.1:8123/api/websocket"
    types = [m["type"] for m in ws.sent]
    assert types == ["auth", "subscribe_events", "get_states"]
    assert ws.sent[1]["event_type"] == "state_changed"
    assert writes == [(11, "off"), (11, "on"), (11, "off")]
    assert ended == "dropped"          # the fake closes when its script ends


@_aio
async def test_the_token_is_only_ever_in_the_auth_frame():
    ws, _, _, _ = await _run_session(AUTH + [changed("switch.relay_ch1", "on")])
    carrying = [m for m in ws.sent if TOKEN in json.dumps(m)]
    assert carrying == [{"type": "auth", "access_token": TOKEN}]


@_aio
async def test_a_session_ends_when_the_saved_connection_changes():
    """An admin saved a new token: reconnect with it rather than keep a
    session authenticated with the old one."""
    ticks = iter([0.0, ha_state.REFRESH_S + 1, ha_state.REFRESH_S + 2])
    ws, _, _, ended = await _run_session(AUTH + ["TIMEOUT", "TIMEOUT"],
                                         still_current=False,
                                         clock=lambda: next(ticks))
    assert ended == "returned"


@_aio
async def test_a_quiet_session_resyncs_so_the_wall_does_not_go_stale():
    """A relay that stays off sends no events; without a periodic re-read
    _clear_stale_states would blank it after BRIDGE_STATE_STALE_S."""
    ticks = iter([0.0, ha_state.RESYNC_S + 1, ha_state.RESYNC_S + 2])
    ws, _, _, _ = await _run_session(AUTH + ["TIMEOUT"], clock=lambda: next(ticks))
    assert [m["type"] for m in ws.sent].count("get_states") == 2


# ── Reconnecting ─────────────────────────────────────────────────────────────

class Stop(Exception):
    pass


@_aio
async def test_a_rejected_token_backs_off_to_the_maximum(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise Stop()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def load():
        return SimpleNamespace(url="http://127.0.0.1:8123", token=TOKEN)

    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_invalid"}])
    connect, _ = connector(ws)

    async def noop(*a):
        return {}
    with pytest.raises(Stop):
        await ha_state.run_forever(bound_ids=noop, write=noop, load_config=load,
                                   connect=connect)
    assert sleeps == [ha_state.RECONNECT_MAX_S]


@_aio
async def test_nothing_configured_waits_without_connecting(monkeypatch):
    sleeps, connected = [], []

    async def fake_sleep(s):
        sleeps.append(s)
        raise Stop()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def load():
        return SimpleNamespace(url="", token=None)

    def connect(url):
        connected.append(url)

    async def noop(*a):
        return {}
    with pytest.raises(Stop):
        await ha_state.run_forever(bound_ids=noop, write=noop, load_config=load,
                                   connect=connect)
    assert connected == [] and sleeps == [ha_state.REFRESH_S]


@_aio
async def test_a_dropped_connection_retries_quickly_at_first(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise Stop()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def load():
        return SimpleNamespace(url="http://127.0.0.1:8123", token=TOKEN)

    class Refused:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            raise OSError("connection refused")

        async def __aexit__(self, *exc):
            return False

    async def noop(*a):
        return {}
    with pytest.raises(Stop):
        await ha_state.run_forever(bound_ids=noop, write=noop, load_config=load,
                                   connect=Refused)
    assert sleeps == [ha_state.RECONNECT_MIN_S]
