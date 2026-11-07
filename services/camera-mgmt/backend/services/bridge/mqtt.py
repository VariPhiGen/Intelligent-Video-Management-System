"""mqtt.py — the primary driver.

One implementation buys three things: Home Assistant via MQTT Discovery, any
broker-speaking controller directly, and (in P3) the ThingsBoard uplink. It is
the reason the bridge is not an HA client with extra steps.

TWO RULES THAT ARE NOT NEGOTIABLE.

QoS 1, never 0. A lost "close the barrier" is worse than a duplicate one, and
every command in the v1 vocabulary is idempotent by design.

retain=False, always. A retained command replays to every future subscriber —
including the broker cheerfully handing "open" to a barrier controller that
reconnects at 3am. State topics may be retained; command topics never.

Nothing in this module raises at its caller. `actuate` is reached from the
ingest handler, and an exception there would cost the rest of the batch its
events.
"""
from __future__ import annotations

import re
import ssl
from typing import Callable, Optional

import structlog

from .base import ActuationResult, Binding

log = structlog.get_logger(__name__)


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _parse_url(url: str) -> dict:
    """host / port / tls from an mqtt(s):// URL.

    aiomqtt takes a hostname and a port, not a URL, and getting the default
    port wrong is a connection failure that reads like a credentials problem.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url if "://" in url else f"mqtt://{url}")
    tls = parsed.scheme in ("mqtts", "ssl", "mqtt+ssl")
    return {"host": parsed.hostname or "", 
            "port": parsed.port or (8883 if tls else 1883), "tls": tls}


def validate_topics(address: str, state_topic: Optional[str]) -> Optional[str]:
    """Refusal reason, or None. Called at binding-save time, not at publish.

    A state topic that the command topic would match is a message loop: the
    bridge hears its own command and records it as device state, which looks
    exactly like the device responding instantly and correctly — the worst way
    for a status column to be wrong.

    The equality case is also a database CHECK (migration 040). The wildcard
    cases cannot be expressed as one, which is why they live here.
    """
    if not address:
        return "empty_topic"
    if state_topic is None:
        return None                      # write-only device, a real thing
    if state_topic == address:
        return "topic_loop"
    # Multi-level wildcard: does the state subscription cover the command topic?
    if state_topic.endswith("/#") and address.startswith(state_topic[:-1]):
        return "topic_loop"
    if state_topic == "#":
        return "topic_loop"
    # Single-level wildcard: `+` matches exactly one level, so the comparison
    # is only meaningful at equal depth.
    if "+" in state_topic:
        s_parts, a_parts = state_topic.split("/"), address.split("/")
        if len(s_parts) == len(a_parts) and all(
                s == "+" or s == a for s, a in zip(s_parts, a_parts)):
            return "topic_loop"
    return None


#: A placeholder: `{name}` where name is a plain identifier. Anything else
#: between braces is left alone — see render_payload.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_payload(config: Optional[dict], command: str,
                   params: Optional[dict]) -> Optional[str]:
    """The published bytes, or None if the operator's template cannot be filled.

    Default is the bare command — the common case for a relay. A template may
    use `{command}` and any key in `params`.

    NOT `str.format`, AND THE REASON IS THE COMMON CASE. The most ordinary
    template here is a JSON body:

        {"action": "{command}", "src": "vms"}

    `str.format` reads the literal JSON braces as fields and raises
    `KeyError: '"action"'`. Escaping every brace as `{{` is not something to
    ask an operator to type into a text box. So substitution is a targeted
    replacement of `{identifier}` and every other brace is literal.

    A placeholder naming something unavailable returns None — a refused
    actuation. That is the operator's typo (`{comand}`) caught before a device
    is sent a payload with a brace in it, and it must never be an exception:
    the caller is the ingest handler, and raising there costs the rest of the
    batch its events.
    """
    template = (config or {}).get("payload_template")
    if not template:
        return command
    available = {"command": command, **(params or {})}
    missing: list[str] = []

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in available:
            missing.append(key)
            return match.group(0)
        return str(available[key])

    rendered = _PLACEHOLDER.sub(_sub, template)
    if missing:
        log.warning("bridge.mqtt.bad_payload_template", missing=sorted(set(missing)))
        return None
    return rendered


class MqttDriver:
    name = "mqtt"

    def __init__(self, url: str, username: Optional[str], password: Optional[str],
                 tls_verify: bool, topic_prefix: str) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._tls_verify = tls_verify
        self._prefix = topic_prefix
        self._client = None
        self._connected = False
        self._on_state: Optional[Callable] = None
        self._state_topics: list = []
        #: Topics this session has SUCCESSFULLY subscribed. Cleared on
        #: disconnect, because a dropped session loses its subscriptions
        #: and believing otherwise leaves the wall dark after a reconnect.
        self._subscribed: set = set()

    async def health(self) -> str:
        if not self._url:
            # A supported deployment, not a fault. The UI must not say
            # "offline" for a site that simply has no bridge.
            return "not_configured"
        return "connected" if self._connected else "offline"

    async def actuate(self, binding: Binding, command: str,
                      params: dict) -> ActuationResult:
        if not self._connected or self._client is None:
            return ActuationResult(False, "offline")
        payload = render_payload(binding.config, command, params)
        if payload is None:
            # BEFORE the publish. A refusal after it would mean the device
            # moved while the record says it did not.
            return ActuationResult(False, "bad_payload_template")
        try:
            await self._client.publish(binding.address, payload, qos=1,
                                       retain=False)
        except Exception as exc:                                # noqa: BLE001
            # The topic, never the credentials: those are only on the client.
            log.warning("bridge.mqtt.publish_failed", topic=binding.address,
                        error=str(exc))
            return ActuationResult(False, "publish_failed")
        return ActuationResult(True, "published")

    async def verify(self, binding: Binding) -> ActuationResult:
        """A TOPIC CANNOT BE RESOLVED. There is nobody to ask whether one
        "exists" — a broker accepts a publish to any name. So the meaningful
        check is that the session is live, plus the loop guard, in case the row
        was written by hand or by an older version than the one that validates
        on save.
        """
        reason = validate_topics(binding.address, binding.state_topic)
        if reason:
            return ActuationResult(False, reason)
        if not self._connected:
            # Stamping verified on a dead session would let the next actuation
            # through on the strength of a check that never happened.
            return ActuationResult(False, "offline")
        return ActuationResult(True, "broker_live")

    def subscribe_state(self, cb) -> None:
        self._on_state = cb

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self, *, subscriptions: bool = False) -> None:
        """Open the client, and — only when asked — hold the subscription.

        `subscriptions` is False for three of the four workers. They hold a
        publish-only client, because outbound commands are not elected: several
        MQTT clients publishing is normal and every v1 command is idempotent.
        The one ELECTED worker passes True, and it is the only writer of
        peripherals.last_state.

        Returns when the session ends. `state_loop` owns the lifetime.
        """
        if not self._url:
            return                       # not configured; nothing to open
        import aiomqtt                    # imported here so the dependency is
                                          # only needed by a site that uses it

        parsed = _parse_url(self._url)
        kwargs: dict = {"hostname": parsed["host"], "port": parsed["port"],
                        "username": self._username, "password": self._password}
        # TLS ARGUMENTS ONLY WHEN TLS IS IN USE. paho refuses `tls_insecure`
        # without a configured SSL context — "Must configure SSL context before
        # using tls_insecure_set" — even when the value is False, so passing it
        # unconditionally breaks every plain mqtt:// broker. Found against a
        # real Mosquitto; no unit test can reach it, because the test suite
        # forbids opening a socket and therefore never calls start().
        if parsed["tls"]:
            # cert_reqs=None is paho's default: CERT_REQUIRED against the
            # system CAs. `tls_insecure` alone only skips the hostname match,
            # so the off switch — "a private CA it has not installed" — needs
            # CERT_NONE as well, or it does not switch anything off.
            kwargs["tls_params"] = aiomqtt.TLSParameters(
                cert_reqs=None if self._tls_verify else ssl.CERT_NONE)
            if not self._tls_verify:
                kwargs["tls_insecure"] = True
        async with aiomqtt.Client(**kwargs) as client:
            self._client = client
            self._connected = True
            log.info("bridge.mqtt.connected", host=parsed["host"],
                     subscriptions=subscriptions)
            try:
                if subscriptions:
                    await self.sync_topics(self._state_topics)
                # EVERY session iterates messages, including the publish-only
                # ones that never subscribed and so never receive any. The
                # iterator is the one public thing that ENDS when the broker
                # goes: a publish-only session that merely slept through a
                # dead broker kept `_connected` True for as long as it liked,
                # so health said `connected` from every worker for minutes
                # after the broker had been stopped, and an actuation would
                # have been "published" into a closed socket. Found on the
                # appliance during the kill-the-broker step.
                async for message in client.messages:
                    if not subscriptions or self._on_state is None:
                        continue
                    await self._on_state(
                        str(message.topic),
                        message.payload.decode("utf-8", "replace")
                        if isinstance(message.payload, (bytes, bytearray))
                        else str(message.payload),
                        _utcnow())
            finally:
                self._connected = False
                self._client = None
                self._subscribed.clear()

    async def stop(self) -> None:
        self._connected = False
        self._client = None
        self._subscribed.clear()

    def set_state_topics(self, topics) -> None:
        """The desired topic set. Read at connect, and re-asserted by
        `sync_topics` for as long as the session lives."""
        self._state_topics = list(topics)

    async def sync_topics(self, topics) -> None:
        """Make the live session's subscriptions match `topics`.

        WHY THIS EXISTS. The set used to be fixed at connect: `set_state_topics`
        then `start()`, and nothing re-read it. An operator who bound a
        peripheral after the API booted got a device that actuated correctly
        and reported `last_state` NULL forever — the wall said "unknown", which
        reads as a broken bridge rather than as a binding the subscriber had
        never heard of. Found by binding a siren against a live Mosquitto and
        watching the retained state arrive only after a restart.

        Reconcile, not re-subscribe: only the difference is sent. Re-subscribing
        the whole set every tick is needless broker traffic, and on some brokers
        a duplicate subscription is a duplicate delivery.

        NEVER RAISES. This runs in a background task beside the message loop;
        an exception there kills the task and the subscription stops healing —
        exactly the failure this fix removes. A topic that failed to subscribe
        is simply not recorded as held, so the next tick tries it again.
        """
        self._state_topics = list(topics)
        if not self._connected or self._client is None:
            # Between sessions there is no client. The next connect subscribes
            # the full set, so a tick landing in the gap is a no-op, not a fault.
            return
        desired = set(self._state_topics)
        # In the ORDER given, not sorted: the topic list is the binding order
        # and a test pins it. Sorting also makes a log read as if the operator
        # had configured something they did not.
        for topic in [t for t in self._state_topics if t not in self._subscribed]:
            try:
                await self._client.subscribe(topic, qos=1)
            except Exception as exc:                            # noqa: BLE001
                log.warning("bridge.mqtt.subscribe_failed", topic=topic,
                            error=str(exc))
                continue
            self._subscribed.add(topic)
            log.info("bridge.mqtt.subscribed", topic=topic)
        for topic in sorted(self._subscribed - desired):
            try:
                await self._client.unsubscribe(topic)
            except Exception as exc:                            # noqa: BLE001
                log.warning("bridge.mqtt.unsubscribe_failed", topic=topic,
                            error=str(exc))
                continue
            self._subscribed.discard(topic)
            log.info("bridge.mqtt.unsubscribed", topic=topic)
