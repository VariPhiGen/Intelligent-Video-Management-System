"""Manual control over HTTP: who may press the button, and what is recorded.

`peripheral_control` is deliberately NOT `peripheral_manage`. Curating an
inventory and unlocking a door are different authorities, and conflating them
would hand every inventory editor a key to the building.

The conftest `db` is a StubSession answering reads as an empty database, so an
unknown peripheral is the realistic case here and the 403-vs-404 boundary is
what these can prove. Anything needing rows is in test_bridge_db.py.

Run: python -m pytest tests/test_peripherals_control_http.py -q
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

BASE = "/api/peripherals"


@pytest.fixture
def audits(monkeypatch):
    from backend.routers import peripherals as p

    seen = []

    async def record(request, principal, action, *, target=None, detail=None,
                     outcome="success"):
        seen.append({"action": action, "target": target, "detail": detail or {},
                     "outcome": outcome})

    monkeypatch.setattr(p.audit_svc, "record", record)
    return seen


# ── Who may operate a device ─────────────────────────────────────────────────

async def test_command_requires_peripheral_control(client, mint, audits):
    """A viewer watching a wall cannot open a barrier."""
    r = await client.post(f"{BASE}/gate-3-barrier/command",
                          json={"command": "open"}, headers=mint(["viewer"]))
    assert r.status_code == 403


async def test_an_operator_does_not_hold_control_by_default(client, mint, audits):
    """The default matrix grants operators alerts, not keys. An admin can grant
    it; it is not assumed."""
    r = await client.post(f"{BASE}/gate-3-barrier/command",
                          json={"command": "open"}, headers=mint(["operator"]))
    assert r.status_code == 403


async def test_a_supervisor_gets_past_the_gate(client, mint, audits):
    """Not 403 — that is the assertion. A supervisor holds peripheral_control,
    so reaching 404 (no such device in this stub database) proves the
    capability was wired to the right permission."""
    r = await client.post(f"{BASE}/gate-3-barrier/command",
                          json={"command": "open"}, headers=mint(["supervisor"]))
    assert r.status_code != 403
    assert r.status_code == 404


async def test_unauthenticated_is_401(client):
    assert (await client.post(f"{BASE}/gate-3-barrier/command",
                              json={"command": "open"})).status_code == 401


async def test_an_unknown_peripheral_is_404(client, mint, audits):
    """The boundary rule, as a status code. Anything not in the inventory is
    somebody else's device."""
    r = await client.post(f"{BASE}/nope/command", json={"command": "open"},
                          headers=mint(["admin"]))
    assert r.status_code == 404


async def test_a_command_is_required(client, mint, audits):
    r = await client.post(f"{BASE}/gate-3-barrier/command", json={},
                          headers=mint(["admin"]))
    assert r.status_code == 422


# ── Bindings ─────────────────────────────────────────────────────────────────

async def test_saving_a_binding_requires_peripheral_manage(client, mint):
    r = await client.put(f"{BASE}/gate-3-barrier/binding",
                         json={"driver": "mqtt", "address": "site/gate3/set"},
                         headers=mint(["viewer"]))
    assert r.status_code == 403


async def test_saving_a_binding_rejects_a_topic_loop(client, mint, audits):
    """A state topic the command topic would match makes the bridge hear its
    own command and record it as device state — which looks exactly like the
    device responding instantly and correctly."""
    r = await client.put(f"{BASE}/gate-3-barrier/binding",
                         json={"driver": "mqtt", "address": "site/gate3/set",
                               "state_topic": "site/gate3/#"},
                         headers=mint(["supervisor"]))
    assert r.status_code == 422


async def test_saving_a_binding_rejects_an_identical_state_topic(client, mint,
                                                                 audits):
    r = await client.put(f"{BASE}/gate-3-barrier/binding",
                         json={"driver": "mqtt", "address": "site/gate3/set",
                               "state_topic": "site/gate3/set"},
                         headers=mint(["supervisor"]))
    assert r.status_code == 422


async def test_an_unknown_driver_is_rejected(client, mint):
    r = await client.put(f"{BASE}/gate-3-barrier/binding",
                         json={"driver": "carrier_pigeon", "address": "x"},
                         headers=mint(["supervisor"]))
    assert r.status_code == 422


async def test_a_state_topic_is_only_meaningful_for_mqtt(client, mint):
    """An HA binding with a state topic is a configuration nobody can honour —
    HA state arrives over its own WebSocket, not a topic."""
    r = await client.put(f"{BASE}/gate-3-barrier/binding",
                         json={"driver": "homeassistant", "address": "switch.g",
                               "state_topic": "site/gate3/state"},
                         headers=mint(["supervisor"]))
    assert r.status_code == 422


async def test_binding_to_an_unknown_peripheral_is_404(client, mint, audits):
    r = await client.put(f"{BASE}/nope/binding",
                         json={"driver": "mqtt", "address": "site/x/set"},
                         headers=mint(["supervisor"]))
    assert r.status_code == 404


async def test_removing_a_binding_requires_peripheral_manage(client, mint):
    r = await client.delete(f"{BASE}/gate-3-barrier/binding",
                            headers=mint(["viewer"]))
    assert r.status_code == 403


async def test_removing_a_binding_from_an_unknown_peripheral_is_404(client, mint,
                                                                    audits):
    r = await client.delete(f"{BASE}/nope/binding", headers=mint(["supervisor"]))
    assert r.status_code == 404
    assert audits == []


async def test_removing_a_binding_that_does_not_exist_is_404(client, mint, audits,
                                                             monkeypatch):
    """The stub database has the peripheral (patched) but no binding row."""
    from types import SimpleNamespace

    from backend.routers import peripherals as p

    async def found(db, slug):
        return SimpleNamespace(id=8, slug=slug)

    monkeypatch.setattr(p, "_get_or_404", found)
    r = await client.delete(f"{BASE}/gate-3-barrier/binding",
                            headers=mint(["supervisor"]))
    assert r.status_code == 404
    assert "no binding" in r.json()["detail"]
    assert audits == []


async def test_removing_a_binding_mid_pulse_is_409(client, mint, audits,
                                                  monkeypatch):
    """The wiring from FollowUpOwed to a status code. What decides it is
    tested against real rows in test_bridge_db.py."""
    from types import SimpleNamespace

    from backend.routers import peripherals as p
    from backend.services.bridge import FollowUpOwed

    async def found(db, slug):
        return SimpleNamespace(id=8, slug=slug)

    async def owed(db, pid):
        raise FollowUpOwed(1)

    monkeypatch.setattr(p, "_get_or_404", found)
    monkeypatch.setattr(p, "unbind", owed)
    r = await client.delete(f"{BASE}/gate-3-barrier/binding",
                            headers=mint(["supervisor"]))
    assert r.status_code == 409
    assert "1 follow-up" in r.json()["detail"]
    assert audits == []


async def test_removing_a_binding_is_audited(client, mint, audits, monkeypatch):
    from types import SimpleNamespace

    from backend.routers import peripherals as p

    async def found(db, slug):
        return SimpleNamespace(id=8, slug=slug)

    async def gone(db, pid):
        return True

    monkeypatch.setattr(p, "_get_or_404", found)
    monkeypatch.setattr(p, "unbind", gone)
    r = await client.delete(f"{BASE}/gate-3-barrier/binding",
                            headers=mint(["supervisor"]))
    assert r.status_code == 200
    assert r.json() == {"unbound": "gate-3-barrier"}
    assert [a["action"] for a in audits] == ["peripheral.binding_removed"]
    assert audits[0]["target"] == "peripheral/gate-3-barrier"


# ── Bridge health ────────────────────────────────────────────────────────────

async def test_bridge_health_distinguishes_not_configured_from_offline(client, mint):
    r = await client.get(f"{BASE}/bridge/health", headers=mint(["viewer"]))
    assert r.status_code == 200
    assert r.json()["state"] in ("connected", "degraded", "offline",
                                 "not_configured")


async def test_bridge_health_on_an_unconfigured_deployment_says_so(client, mint):
    """The default. Not a fault, and the wall must not draw it as one."""
    r = await client.get(f"{BASE}/bridge/health", headers=mint(["viewer"]))
    assert r.json()["state"] == "not_configured"


async def test_bridge_health_is_not_public(client):
    assert (await client.get(f"{BASE}/bridge/health")).status_code == 401


async def test_bridge_health_does_not_collide_with_the_slug_route(client, mint):
    """`/bridge/health` must not be read as a peripheral named "bridge". Route
    order decides this and nothing else would catch it."""
    r = await client.get(f"{BASE}/bridge/health", headers=mint(["viewer"]))
    assert r.status_code == 200
    assert "drivers" in r.json()
