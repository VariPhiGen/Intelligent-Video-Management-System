"""webhook.py — the driver that makes "you must install Home Assistant" untrue.

A POST to an address the operator supplies. Nothing clever, and that is the
point: a site with a relay board behind a small HTTP shim, or a PSIM that
already exposes one, needs neither a broker nor HA.

`url_ref`-style indirection is deliberate elsewhere in the product; here the
address IS the URL, because a binding row is already the operator-controlled
indirection — the rule names a peripheral, not a URL, and only somebody with
`peripheral_manage` can say where that peripheral lives.

THE DEVICE GATEWAY (services/device-gateway) is the usual thing behind it: one
URL per device, POST to switch it, GET for a read-only health check. Two
things follow from that contract and nowhere else:

  * HEALTH. A binding whose config says `"health": true` is verified by GETting
    its URL and reading `{"ok", "state", "protocol", "error"}` back. That is
    only safe because the gateway promises GET never moves anything, which is
    why it is opt-in per binding: an arbitrary operator URL is still verified
    by SHAPE and never called, exactly as before.
  * WHY IT FAILED. A refusal from the gateway carries `protocol` and `error`;
    the detail becomes `call_rejected:modbus-tcp/timeout` rather than a bare
    `call_rejected`, so the trigger log says which protocol failed and why.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import structlog

from .base import ActuationResult, Binding
from .mqtt import render_payload

log = structlog.get_logger(__name__)

#: A gateway answers at once (a speaker streams in the background, a relay on
#: the local switch replies in milliseconds), so a slow reply means something
#: is wrong, not busy. Short, so a dead device cannot hold a rule or the
#: occupancy loop (which also caps every call, at pending.CALL_TIMEOUT_S).
TIMEOUT_S = 2.0
#: What may appear in a detail token taken from a reply. Anything else in a
#: device's answer is dropped rather than written into the audit log.
_TOKEN = re.compile(r"[^a-z0-9_.\-]+")


def _token(value: Any, limit: int = 40) -> str:
    return _TOKEN.sub("_", str(value or "").strip().lower())[:limit].strip("_")


def _reason(resp) -> Optional[str]:
    """`protocol/error` from a gateway reply, or None if the body is not one."""
    try:
        body = resp.json()
    except Exception:                                           # noqa: BLE001
        return None
    if not isinstance(body, dict) or not body.get("error"):
        return None
    protocol, error = _token(body.get("protocol")), _token(body.get("error"))
    return f"{protocol}/{error}" if protocol else error


def health_checked(binding: Binding) -> bool:
    return bool((binding.config or {}).get("health"))


class WebhookDriver:
    name = "webhook"

    def __init__(self, enabled: bool = True) -> None:
        # No global URL: every binding carries its own. `enabled` exists so a
        # deployment can switch the whole driver off without deleting bindings.
        self._enabled = enabled
        self._http = None

    def _client(self):
        """The HTTP client, made on first use. There was a time it was never
        made at all — nothing called a start() — so every webhook binding
        reported the driver `not_configured` and every call `offline`."""
        if self._http is None and self._enabled:
            import httpx
            self._http = httpx.AsyncClient(timeout=TIMEOUT_S)
        return self._http

    async def health(self) -> str:
        # Per process, and actuate() refuses a driver that says
        # not_configured — so this must not depend on whether THIS worker has
        # made a call yet. Whether a site HAS webhook devices is a question
        # for the database; Bridge.health(db) asks it for the wall's chip.
        if not self._enabled:
            return "not_configured"
        # There is no session to hold open — each call is independent — so a
        # webhook driver that is switched on is as connected as it gets.
        return "connected"

    async def actuate(self, binding: Binding, command: str,
                      params: dict) -> ActuationResult:
        if not self._enabled or self._client() is None:
            return ActuationResult(False, "offline")
        if not binding.address.startswith(("http://", "https://")):
            return ActuationResult(False, "bad_address")
        # Same template mechanism as MQTT, for the same reason: the ordinary
        # body here is JSON and its braces must survive.
        body = render_payload(binding.config, command, params)
        if body is None:
            return ActuationResult(False, "bad_payload_template")
        try:
            resp = await self._http.post(
                binding.address,
                json={"command": command, "params": params or {},
                      "peripheral": binding.peripheral_slug, "body": body},
            )
        except Exception as exc:                                # noqa: BLE001
            log.warning("bridge.webhook.call_failed", url=binding.address,
                        error=str(exc))
            return ActuationResult(False, "call_failed")
        if not (200 <= resp.status_code < 300):
            reason = _reason(resp)
            log.warning("bridge.webhook.call_rejected", url=binding.address,
                        status=resp.status_code, reason=reason)
            return ActuationResult(False, f"call_rejected:{reason}" if reason
                                   else "call_rejected")
        return ActuationResult(True, "called")

    async def verify(self, binding: Binding) -> ActuationResult:
        """BY SHAPE, unless the binding opted into the gateway's health check.

        A request to an operator-supplied URL could open the barrier it
        describes. Verifying a gate by opening it is not verification, so an
        ordinary webhook is checked for being addressable and nothing more —
        and a test asserts that no call is made. Only a binding marked
        `health` (a device-gateway URL, whose GET is read-only by contract) is
        asked, and then the answer is the device's real health and state.
        """
        if not self._enabled:
            return ActuationResult(False, "not_configured")
        if not binding.address.startswith(("http://", "https://")):
            return ActuationResult(False, "bad_address")
        if not health_checked(binding):
            return ActuationResult(True, "addressable")
        try:
            resp = await self._client().get(binding.address)
        except Exception as exc:                                # noqa: BLE001
            log.info("bridge.webhook.health_unreachable", url=binding.address,
                     error=str(exc))
            return ActuationResult(False, "unhealthy:gateway_unreachable")
        reason = _reason(resp)
        if 200 <= resp.status_code < 300 and reason is None:
            try:
                state = (resp.json() or {}).get("state")
            except Exception:                                   # noqa: BLE001
                state = None
            return ActuationResult(True, "healthy", _token(state) or None)
        return ActuationResult(False, f"unhealthy:{reason or f'http_{resp.status_code}'}")

    def subscribe_state(self, cb) -> None:
        # A webhook is outbound only. State comes back through the health
        # check (verify), not through a subscription.
        self._on_state = cb
