"""homeassistant.py — for sites that run HA without a broker.

The bridge's fallback, not its centre. MQTT is the primary driver because one
implementation also serves ThingsBoard and any broker-speaking controller; this
exists so "you must run Mosquitto" is never a precondition.

WHY ENTITY IDS ARE RE-RESOLVED RATHER THAN TRUSTED.

HA ships monthly and entity ids move: 2026.5 changed managed go2rtc stream
identifiers to prefer camera unique ids over entity ids and broke playback
across several integrations. A binding whose address has never been CONFIRMED to
resolve therefore refuses to actuate (base.resolve_target), and the verify tick
re-resolves it and marks it degraded when it stops. A rename becomes a visible
degraded binding in the UI instead of a silent no-op at 2am.

The token is a long-lived HA access token. It does not expire, so a leak has a
long tail: it is never logged, never returned in a result detail and never
placed in a request body. It comes from HA_TOKEN today; storing it as Fernet
ciphertext set from the UI is the next step, not yet done.

CONNECTED MEANS "HA ANSWERED /api/ WITH THIS TOKEN", recently. There is no
session to hold, so the driver probes: on first use, then every PROBE_TTL_S
while it works and every RETRY_S while it does not. A call that cannot reach
HA marks it offline at once rather than waiting for the next probe. A 401 is
offline too, but recorded as `unauthorized`, because "fix the token" and "fix
the network" send somebody to different places.
"""
from __future__ import annotations

import time
from typing import Optional

import structlog

from .base import ActuationResult, Binding

log = structlog.get_logger(__name__)

#: Per request. HA on the same host answers in milliseconds; a rule must not
#: wait long on one that is not there.
TIMEOUT_S = 3.0
#: How long a successful probe (or call) is believed.
PROBE_TTL_S = 30.0
#: How soon an offline driver may probe again. Bridge.actuate asks health()
#: before every command, so without this a dead HA would cost every rule a
#: timeout.
RETRY_S = 5.0
#: Entity states that mean HA has the entity but cannot reach the DEVICE —
#: a relay board whose cable is out reads `unavailable`. Not an on/off.
_NO_DEVICE = frozenset({"unavailable", "unknown"})

#: command -> service, per HA domain. A boom gate is a `cover`, and
#: cover.turn_on does not exist — which is why this is not one generic map.
_SERVICES = {
    # A switch is usually a relay channel, and a relay does whatever is wired
    # to it: the same command words the device gateway accepts for a relay
    # (devgw/server.py RELAY_ON / RELAY_OFF), so a rule written as "unlock"
    # moves a maglock's relay whichever route reaches it.
    "switch": {"on": "turn_on", "open": "turn_on", "unlock": "turn_on",
               "flash": "turn_on", "trigger": "turn_on", "start": "turn_on",
               "off": "turn_off", "close": "turn_off", "lock": "turn_off",
               "stop": "turn_off"},
    "light": {"on": "turn_on", "open": "turn_on", "off": "turn_off",
              "close": "turn_off"},
    "lock": {"unlock": "unlock", "open": "unlock", "lock": "lock",
             "close": "lock"},
    "cover": {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"},
    "siren": {"on": "turn_on", "open": "turn_on", "off": "turn_off",
              "close": "turn_off"},
    # Speakers: Sonos, Google/Nest, Alexa, any DLNA renderer. `announce` plays
    # a sound or TTS URL over whatever is playing (see _REQUIRED / _DEFAULTS).
    "media_player": {"on": "turn_on", "off": "turn_off", "play": "media_play",
                     "pause": "media_pause", "stop": "media_stop",
                     "announce": "play_media"},
    # A scene or script is something already built in HA: "run it".
    "scene": {"run": "turn_on", "on": "turn_on", "activate": "turn_on",
              "trigger": "turn_on", "start": "turn_on"},
    "script": {"run": "turn_on", "on": "turn_on", "trigger": "turn_on",
               "start": "turn_on", "stop": "turn_off", "off": "turn_off"},
    "button": {"press": "press", "on": "press", "trigger": "press",
               "run": "press", "start": "press"},
    "fan": {"on": "turn_on", "start": "turn_on", "off": "turn_off",
            "stop": "turn_off"},
    "valve": {"open": "open_valve", "on": "open_valve", "close": "close_valve",
              "off": "close_valve", "stop": "stop_valve"},
}

#: Params a (domain, command) cannot do without. Refused before the call with
#: `missing_param:<name>`, which a rule's audit row can show — rather than an
#: opaque 400 from HA after the fact.
_REQUIRED = {
    ("media_player", "announce"): ("media_content_id",),
}
#: Filled in when the rule does not say. `announce: true` ducks whatever the
#: speaker is playing and resumes it afterwards, on speakers that support it.
_DEFAULTS = {
    ("media_player", "announce"): {"media_content_type": "music", "announce": True},
}
#: Domains whose HA state is the time of the last press, not a condition. The
#: wall shows them as `ready`: a timestamp is not something to read at 2am.
_STATELESS = frozenset({"button", "scene"})


def wall_state(entity_id: str, state) -> Optional[str]:
    """An entity's HA state as the device wall stores it, or None for unknown."""
    if not isinstance(state, str) or not state.strip():
        return None
    state = state.strip().lower()
    if state in _NO_DEVICE:
        return None
    if entity_id.split(".", 1)[0] in _STATELESS:
        return "ready"
    return state[:40]


def service_for(entity_id: str, command: str):
    """(domain, service), or None when the pairing is not supported.

    None for an unknown DOMAIN as well as an unknown command: guessing a
    service name for a domain this vocabulary does not drive produces a call HA
    rejects, and a refusal here is the same outcome without the round trip.
    """
    if not entity_id or "." not in entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    service = _SERVICES.get(domain, {}).get(command)
    return (domain, service) if service else None


class HaDriver:
    name = "homeassistant"

    def __init__(self, url: str, token: Optional[str]) -> None:
        self._url = url.rstrip("/") if url else ""
        self._token = token
        self._http = None
        self._connected = False
        #: monotonic time of the last probe or call that told us anything.
        self._checked_at: Optional[float] = None
        #: Why it is offline: `unreachable`, `unauthorized`, `http_<code>`.
        #: A stable token, never exception text — it may be shown.
        self.last_error: Optional[str] = None

    def _configured(self) -> bool:
        return bool(self._url and self._token)

    def _client(self):
        """The HTTP client, made on first use (as WebhookDriver does)."""
        if self._http is None and self._configured():
            import httpx
            self._http = httpx.AsyncClient(timeout=TIMEOUT_S)
        return self._http

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _mark(self, ok: bool, error: Optional[str] = None) -> None:
        self._connected = ok
        self.last_error = None if ok else error
        self._checked_at = time.monotonic()

    def _due(self) -> bool:
        if self._checked_at is None:
            return True
        age = time.monotonic() - self._checked_at
        return age > (PROBE_TTL_S if self._connected else RETRY_S)

    async def probe(self) -> str:
        """Ask HA whether it is there and takes this token. Never raises."""
        if not self._configured():
            return "not_configured"
        try:
            resp = await self._client().get(f"{self._url}/api/",
                                            headers=self._headers())
        except Exception as exc:                                # noqa: BLE001
            # str(exc) may carry the URL; the token is only ever in the header.
            log.info("bridge.ha.unreachable", error=str(exc))
            self._mark(False, "unreachable")
            return "offline"
        if resp.status_code in (401, 403):
            log.warning("bridge.ha.unauthorized", status=resp.status_code)
            self._mark(False, "unauthorized")
        elif 200 <= resp.status_code < 300:
            self._mark(True)
        else:
            self._mark(False, f"http_{resp.status_code}")
        return "connected" if self._connected else "offline"

    async def start(self) -> None:
        await self.probe()

    async def reconfigure(self, url: str, token: Optional[str]) -> bool:
        """Point the driver somewhere else. Returns whether anything changed.

        Called in every worker when an admin saves a connection, so it must be
        cheap when nothing changed — the sync loop calls it every few seconds.
        A change drops the old client and forgets the old verdict: a probe
        against the old URL says nothing about the new one.
        """
        url = url.rstrip("/") if url else ""
        if url == self._url and token == self._token:
            return False
        await self.stop()
        self._url, self._token = url, token
        self._checked_at = None
        self.last_error = None
        return True

    async def entities(self) -> Optional[list]:
        """Every entity this driver could drive, as HA reports them now.

        None when HA cannot be asked (offline, or it answered with something
        other than a list). Only domains in _SERVICES: listing a climate or a
        sensor would offer an import that refuses on every press.
        """
        if await self.health() != "connected" or self._http is None:
            return None
        try:
            resp = await self._http.get(f"{self._url}/api/states",
                                        headers=self._headers())
            rows = resp.json() if 200 <= resp.status_code < 300 else None
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.ha.list_failed", error=str(exc))
            self._mark(False, "unreachable")
            return None
        if not isinstance(rows, list):
            return None
        out = []
        for row in rows:
            entity_id = (row or {}).get("entity_id") if isinstance(row, dict) else None
            if not isinstance(entity_id, str) or "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in _SERVICES:
                continue
            attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            out.append({
                "entity_id": entity_id,
                "domain": domain,
                "name": str(attrs.get("friendly_name") or entity_id)[:120],
                "state": str(row.get("state") or "")[:40] or None,
            })
        out.sort(key=lambda e: e["entity_id"])
        return out

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._connected = False

    async def health(self) -> str:
        if not self._configured():
            # Either half missing means nothing can be called. That is a
            # supported deployment, not a network fault, and saying "offline"
            # would send somebody to look at the wrong thing.
            return "not_configured"
        if self._due():
            return await self.probe()
        return "connected" if self._connected else "offline"

    async def actuate(self, binding: Binding, command: str,
                      params: dict) -> ActuationResult:
        if await self.health() != "connected" or self._http is None:
            return ActuationResult(False, "offline")
        mapped = service_for(binding.address, command)
        if mapped is None:
            return ActuationResult(False, "unsupported_command")
        domain, service = mapped
        params = params or {}
        for name in _REQUIRED.get((domain, command), ()):
            if params.get(name) in (None, ""):
                return ActuationResult(False, f"missing_param:{name}")
        try:
            resp = await self._http.post(
                f"{self._url}/api/services/{domain}/{service}",
                # entity_id LAST, so a rule's params cannot override it.
                # Otherwise a dict key becomes a way to drive a device the
                # boundary rule never approved.
                json={**_DEFAULTS.get((domain, command), {}), **params,
                      "entity_id": binding.address},
                headers=self._headers(),
            )
        except Exception as exc:                                # noqa: BLE001
            # str(exc) may carry the URL; the token is only ever in the header.
            log.warning("bridge.ha.call_failed", entity=binding.address,
                        error=str(exc))
            self._mark(False, "unreachable")
            return ActuationResult(False, "call_failed")
        if resp.status_code in (401, 403):
            self._mark(False, "unauthorized")
            return ActuationResult(False, "call_rejected:unauthorized")
        if not (200 <= resp.status_code < 300):
            log.warning("bridge.ha.call_rejected", entity=binding.address,
                        status=resp.status_code)
            return ActuationResult(False, "call_rejected")
        self._mark(True)
        return ActuationResult(True, "called")

    async def verify(self, binding: Binding) -> ActuationResult:
        """Ask HA whether this entity still exists.

        THE ONE THAT ACTUALLY MOVES. Entity ids change between monthly releases
        and a binding that stops resolving has to become visibly unverified
        rather than silently doing nothing at 2am.

        A domain this driver cannot drive is refused even when HA resolves it:
        verifying it would promise a control that refuses on every press.

        The entity's STATE comes back with the result, and verify_loop writes
        it to the wall — so the wall shows on/off before there is any state
        subscription. An entity HA has but cannot reach (`unavailable`) is
        `unhealthy:`: the address is right, the device is not answering, and
        verify_bindings keeps the stamp and blanks the wall for exactly that.
        """
        if await self.health() != "connected" or self._http is None:
            return ActuationResult(False, "offline")
        if "." not in (binding.address or ""):
            return ActuationResult(False, "bad_address")
        domain = binding.address.split(".", 1)[0]
        if domain not in _SERVICES:
            return ActuationResult(False, "unsupported_domain")
        try:
            resp = await self._http.get(
                f"{self._url}/api/states/{binding.address}",
                headers=self._headers())
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.ha.verify_failed", entity=binding.address,
                        error=str(exc))
            self._mark(False, "unreachable")
            # `offline`, not `call_failed`: nothing was learnt about the
            # ADDRESS, so verify_bindings must keep the stamp it had.
            return ActuationResult(False, "offline")
        if resp.status_code == 404:
            return ActuationResult(False, "entity_missing")
        if resp.status_code in (401, 403):
            self._mark(False, "unauthorized")
            return ActuationResult(False, "offline")
        if not (200 <= resp.status_code < 300):
            return ActuationResult(False, "call_rejected")
        self._mark(True)
        raw = _state_of(resp)
        if raw in _NO_DEVICE:
            return ActuationResult(False, f"unhealthy:homeassistant/{raw}")
        return ActuationResult(True, "resolved", wall_state(binding.address, raw))

    def subscribe_state(self, cb) -> None:
        self._on_state = cb


def _state_of(resp) -> Optional[str]:
    """The `state` of a /api/states reply, as a short token, or None."""
    try:
        state = (resp.json() or {}).get("state")
    except Exception:                                           # noqa: BLE001
        return None
    if not isinstance(state, str) or not state.strip():
        return None
    return state.strip().lower()[:40]
