"""The driver port: the vocabulary, and the one gate every actuation passes.

`resolve_target` is section 3.1 of the spec expressed as a function — a VMS rule
may act only on a peripheral in the VMS inventory. It lives here, alone, so
there is exactly one place to read to know what can be actuated, and so no
driver can be written that forgets it.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_base.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services.bridge import base  # noqa: E402

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def peripheral(**kw):
    base_ = {"slug": "gate-3-barrier", "name": "Gate 3 barrier", "enabled": True}
    base_.update(kw)
    return SimpleNamespace(**base_)


def binding(**kw):
    base_ = {"peripheral_slug": "gate-3-barrier", "driver": "mqtt",
             "address": "site/gate3/set", "config": {},
             "state_topic": "site/gate3/state", "verified_at": NOW}
    base_.update(kw)
    return base.Binding(**base_)


def test_a_known_enabled_verified_peripheral_is_allowed():
    assert base.resolve_target(peripheral(), binding()) is None


def test_an_unknown_peripheral_is_refused():
    """The boundary rule. Anything not in the VMS inventory is Home
    Assistant's business, and the bridge says so rather than reaching for it."""
    r = base.resolve_target(None, binding())
    assert r.ok is False
    assert r.detail == "unknown_peripheral"


def test_a_disabled_peripheral_is_refused():
    """`enabled` is an operator's explicit "leave this alone" — often because
    it is being serviced with someone's hands near it."""
    r = base.resolve_target(peripheral(enabled=False), binding())
    assert r.ok is False
    assert r.detail == "peripheral_disabled"


def test_a_peripheral_with_no_binding_is_refused():
    """Inventory-only is the supported default state. It is not an error, but
    it is certainly not actuable."""
    r = base.resolve_target(peripheral(), None)
    assert r.ok is False
    assert r.detail == "no_binding"


def test_an_unverified_binding_is_refused():
    """A binding the bridge has never confirmed resolves may point at a
    renamed entity. Refusing loudly beats a silent no-op at 2am."""
    r = base.resolve_target(peripheral(), binding(verified_at=None))
    assert r.ok is False
    assert r.detail == "binding_unverified"


def test_the_disabled_check_comes_before_the_binding_check():
    """Order is operator-visible. A device switched off for servicing should
    say so, not report the incidental fact that it also has no binding — the
    first is why it will not move, the second is a detail."""
    r = base.resolve_target(peripheral(enabled=False), None)
    assert r.detail == "peripheral_disabled"


def test_a_peripheral_object_with_no_enabled_attribute_is_refused():
    """Fail closed on a shape nobody expected. A duck-typed row missing the
    flag must not be read as permission."""
    r = base.resolve_target(SimpleNamespace(slug="x"), binding())
    assert r.ok is False
    assert r.detail == "peripheral_disabled"


def test_refusal_details_are_stable_snake_case_not_prose():
    """These strings land in alerts.notified and the audit log, and get
    filtered on. They are an interface, not a message."""
    for r in (base.resolve_target(None, binding()),
              base.resolve_target(peripheral(enabled=False), binding()),
              base.resolve_target(peripheral(), None),
              base.resolve_target(peripheral(), binding(verified_at=None))):
        assert r.detail.islower()
        assert " " not in r.detail
        assert "\n" not in r.detail


def test_driver_state_values_distinguish_not_configured_from_offline():
    """Different facts. "No bridge is configured" is a supported deployment;
    "the bridge is down" is a fault. The UI must be able to say which."""
    assert "not_configured" in base.DRIVER_STATES
    assert "offline" in base.DRIVER_STATES


def test_an_allowed_target_returns_none_rather_than_a_truthy_ok():
    """`None` means allowed, on purpose: a caller that forgets to check cannot
    accidentally treat a falsy ActuationResult as permission. `if refusal is
    not None: return refusal` is the only correct shape and this pins it."""
    assert base.resolve_target(peripheral(), binding()) is None
