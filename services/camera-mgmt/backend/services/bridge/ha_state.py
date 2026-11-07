"""ha_state.py — Home Assistant's state, pushed to the device wall as it changes.

Without this the wall learns an HA device's state only from the verify tick
(every BRIDGE_VERIFY_INTERVAL_S, 300 s by default) — a relay that clicked a
second ago still shows its old state for minutes.

HA's WebSocket API, the documented part only:

  ← {"type": "auth_required"}
  → {"type": "auth", "access_token": "<token>"}
  ← {"type": "auth_ok"}  |  {"type": "auth_invalid"}
  → {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
  → {"id": 2, "type": "get_states"}
  ← {"id": 2, "type": "result", "success": true, "result": [<state>, ...]}
  ← {"id": 1, "type": "event", "event": {"event_type": "state_changed",
        "data": {"entity_id": "switch.relay_ch1", "new_state": {"state": "on"}}}}

ONLY OUR ENTITIES. HA carries the whole site — sun, weather, every sensor — and
only entities bound to a VMS peripheral are written. The boundary rule applies
inbound as well as out.

ELECTED: runs under run_as_leader("bridge_ha_state"), because the rows it
writes are the ones the wall renders, through bridge._write_state like every
other state source.

STALENESS. A relay that stays off sends no events, and _clear_stale_states
blanks any reading older than BRIDGE_STATE_STALE_S. So while connected, the
full state is re-read every RESYNC_S, which also refreshes last_seen: the wall
stays known for as long as HA is reachable, and goes to unknown on its own
when it is not.

`unavailable` / `unknown` — HA has the entity but cannot reach the device (a
relay board whose cable is out) — is written as NO state, never as off.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import structlog

log = structlog.get_logger(__name__)

#: Full state re-read while connected. Well under BRIDGE_STATE_STALE_S.
RESYNC_S = 60.0
#: How often the set of bound entities, and the saved connection, are re-read
#: — a peripheral imported a moment ago starts updating within this.
REFRESH_S = 10.0
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0
#: Per recv. Not a liveness timeout (pings are) — how often the loop wakes to
#: do its periodic work when HA is quiet.
TICK_S = 5.0
#: get_states returns EVERY entity; a large HA is well over the 1 MiB default.
MAX_MESSAGE = 16 * 1024 * 1024

#: (peripheral_id, state or None). None = unknown: write no state.
Update = tuple[int, Optional[str]]


class AuthFailed(Exception):
    """HA refused the token. Retrying at full speed will not change that."""


def ws_url(http_url: str) -> str:
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/api/websocket"


def state_value(new_state, entity_id: str = "") -> Optional[str]:
    """A state object's state, as the wall stores it, or None for unknown.

    None also for a removed entity (`new_state: null`): the wall must stop
    claiming a state for something HA no longer has. Same rules as a verify
    (homeassistant.wall_state), so the two sources never disagree.
    """
    from .homeassistant import wall_state

    if not isinstance(new_state, dict):
        return None
    return wall_state(entity_id or str(new_state.get("entity_id") or ""),
                      new_state.get("state"))


def updates_from(msg, bound: dict[str, int]) -> list[Update]:
    """The wall updates one message carries, for bound entities only."""
    if not isinstance(msg, dict):
        return []
    if msg.get("type") == "event":
        event = msg.get("event") or {}
        if not isinstance(event, dict) or event.get("event_type") != "state_changed":
            return []
        data = event.get("data") or {}
        if not isinstance(data, dict):
            return []
        pid = bound.get(data.get("entity_id"))
        return [] if pid is None else [
            (pid, state_value(data.get("new_state"), data.get("entity_id")))]
    if msg.get("type") == "result" and isinstance(msg.get("result"), list):
        out = []
        for row in msg["result"]:
            if isinstance(row, dict) and row.get("entity_id") in bound:
                out.append((bound[row["entity_id"]], state_value(row, row["entity_id"])))
        return out
    return []


async def handshake(ws, token: str) -> None:
    first = json.loads(await ws.recv())
    if first.get("type") != "auth_required":
        raise ConnectionError(f"unexpected first message {first.get('type')!r}")
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    reply = json.loads(await ws.recv())
    if reply.get("type") == "auth_invalid":
        raise AuthFailed()
    if reply.get("type") != "auth_ok":
        raise ConnectionError(f"unexpected auth reply {reply.get('type')!r}")


async def session(url: str, token: str, *,
                  connect: Callable,
                  bound_ids: Callable[[], Awaitable[dict[str, int]]],
                  write: Callable[[list[Update], datetime], Awaitable[None]],
                  still_current: Callable[[], Awaitable[bool]],
                  clock: Callable[[], float] = time.monotonic) -> None:
    """One connection, for as long as it lasts.

    Returns when the saved connection changes (the caller reconnects with the
    new one); raises when the connection fails. Everything it touches outside
    the socket is passed in, so the protocol is testable without HA.
    """
    async with connect(ws_url(url)) as ws:
        await handshake(ws, token)
        ids = itertools.count(1)
        await ws.send(json.dumps({"id": next(ids), "type": "subscribe_events",
                                  "event_type": "state_changed"}))
        await ws.send(json.dumps({"id": next(ids), "type": "get_states"}))
        bound = await bound_ids()
        log.info("bridge.ha_state.connected", bound=len(bound))
        last_sync = last_refresh = clock()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=TICK_S)
            except asyncio.TimeoutError:
                raw = None
            if raw is not None:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and msg.get("type") == "result" \
                        and msg.get("success") is False:
                    log.warning("bridge.ha_state.command_failed",
                                error=(msg.get("error") or {}).get("code"))
                updates = updates_from(msg, bound)
                if updates:
                    await write(updates, datetime.now(timezone.utc))
            now = clock()
            if now - last_refresh >= REFRESH_S:
                last_refresh = now
                if not await still_current():
                    log.info("bridge.ha_state.reconfigured")
                    return
                bound = await bound_ids()
            if now - last_sync >= RESYNC_S:
                last_sync = now
                await ws.send(json.dumps({"id": next(ids), "type": "get_states"}))


def _connect(url: str):
    from websockets.asyncio.client import connect
    return connect(url, open_timeout=5, ping_interval=20, ping_timeout=20,
                   max_size=MAX_MESSAGE)


async def run_forever(*, bound_ids, write, load_config,
                      connect: Callable = _connect) -> None:
    """Hold a session whenever HA is configured; reconnect with back-off.

    A rejected token backs off to the maximum at once: it will not start
    working by itself, and an admin saving a new one is picked up within
    RECONNECT_MAX_S.
    """
    delay = RECONNECT_MIN_S
    while True:
        cfg = await load_config()
        if not cfg.url or not cfg.token:
            await asyncio.sleep(REFRESH_S)
            continue

        async def still_current(_url=cfg.url, _token=cfg.token) -> bool:
            now = await load_config()
            return (now.url, now.token) == (_url, _token)

        started = time.monotonic()
        try:
            await session(cfg.url, cfg.token, connect=connect, bound_ids=bound_ids,
                          write=write, still_current=still_current)
            delay = RECONNECT_MIN_S
            continue                       # reconfigured: reconnect at once
        except asyncio.CancelledError:
            raise
        except AuthFailed:
            log.warning("bridge.ha_state.unauthorized")
            delay = RECONNECT_MAX_S
        except Exception as exc:                                # noqa: BLE001
            # str(exc) may carry the URL; the token is never in it.
            log.info("bridge.ha_state.disconnected", error=str(exc)[:200])
            if time.monotonic() - started > RESYNC_S:
                # It had been working: an HA restart, not a dead HA. Come
                # back quickly rather than at the back-off earlier failures
                # built up.
                delay = RECONNECT_MIN_S
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_S)
