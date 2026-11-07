"""base.py — the driver port: the vocabulary, and the one gate.

Everything the bridge shares lives here and nothing here does I/O, so the rules
about WHAT may be actuated can be read and tested without a broker.

`resolve_target` is spec section 3.1 as a function: a VMS rule may act only on a
peripheral present in the VMS inventory. Put anywhere else it would be four
copies in three drivers; here it is one place to read, and no driver can be
written that forgets it.

Refusal details are stable snake_case tokens, never prose. They are written into
alerts.notified and the audit log and are filtered on — an interface, not a
message.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, NamedTuple, Optional, Protocol

#: connected      — the driver has a live session
#: degraded       — connected, but something is wrong (unverified bindings, say)
#: offline        — configured and NOT connected. A fault.
#: not_configured — nothing is set up. A SUPPORTED deployment, not a fault, and
#:                  a different fact from offline. The UI must say which.
DRIVER_STATES = ("connected", "degraded", "offline", "not_configured")


class Binding(NamedTuple):
    """How the bridge reaches one peripheral, as the drivers see it.

    A NamedTuple rather than the ORM row: a driver must not be able to write to
    the database through the object it was handed, and a test must be able to
    build one without a session.
    """

    peripheral_slug: str
    driver: str
    address: str
    config: dict
    state_topic: Optional[str]
    verified_at: Optional[datetime]


class ActuationResult(NamedTuple):
    ok: bool
    #: Stable snake_case token. Never a stack trace — this is stored and shown.
    detail: str
    #: What a verify learnt about the device, for drivers that can ask
    #: (webhook health checks). None = nothing learnt; the wall keeps what it had.
    state: Optional[str] = None


class Driver(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> str: ...
    async def actuate(self, binding: Binding, command: str,
                      params: dict) -> ActuationResult: ...
    async def verify(self, binding: Binding) -> ActuationResult: ...
    def subscribe_state(self,
                        cb: Callable[[str, str, datetime], None]) -> None: ...


def resolve_target(peripheral: Any,
                   binding: Optional[Binding]) -> Optional[ActuationResult]:
    """The boundary rule. Returns a refusal, or None meaning allowed.

    NONE MEANS ALLOWED, on purpose. A caller that forgets to check cannot
    accidentally treat a falsy ActuationResult as permission — the only correct
    shape is `if refusal is not None: return refusal`, and returning a truthy
    "yes" object would make the wrong shape work by accident.

    Four ways to be refused, each a different operator-visible fact:

      unknown_peripheral  — not in the inventory. Someone else's device, and
                            the customer's broker carries their whole smart
                            home.
      peripheral_disabled — an operator's explicit "leave this alone", often
                            because it is being serviced with hands near it.
                            Checked BEFORE the binding, because that is the
                            reason it will not move; a missing binding is an
                            incidental detail beside it.
      no_binding          — inventory-only, the supported default. Not an
                            error, but certainly not actuable.
      binding_unverified  — the bridge has never confirmed this address
                            resolves. HA entity ids move between releases and a
                            silent no-op at 2am is the worst outcome.
    """
    if peripheral is None:
        return ActuationResult(False, "unknown_peripheral")
    # getattr's default is False, not True: a row shape nobody expected must
    # fail closed rather than be read as permission.
    if not getattr(peripheral, "enabled", False):
        return ActuationResult(False, "peripheral_disabled")
    if binding is None:
        return ActuationResult(False, "no_binding")
    if binding.verified_at is None:
        return ActuationResult(False, "binding_unverified")
    return None
