"""/api/ha over HTTP: who may connect HA, who may import from it, and what
never leaves the server.

The connection is ADMIN-only: a long-lived HA token is owner-level access to
the whole of Home Assistant, a wider key than `peripheral_manage`. Listing and
importing entities is `peripheral_manage`, like the rest of the inventory —
so a supervisor can import devices and still cannot see or change the token.

The conftest `db` is a StubSession (an empty database), and the HA driver is
replaced by a fake: these are contract tests and must not reach a real HA.

Run: python -m pytest tests/test_homeassistant_http.py -q
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

BASE = "/api/ha"
TOKEN = "eyJ-a-long-lived-ha-token-never-to-be-echoed-41f2"

RELAYS = [
    {"entity_id": "switch.relay_ch1", "domain": "switch", "name": "Relay CH1", "state": "off"},
    {"entity_id": "light.yard", "domain": "light", "name": "Yard", "state": "on"},
]


class FakeDriver:
    """Stands in for HaDriver inside the bridge."""

    def __init__(self, state="connected", entities=RELAYS, error=None):
        self.state, self._entities, self.last_error = state, entities, error
        self.configured = []

    async def reconfigure(self, url, token):
        self.configured.append((url, token))
        return False

    async def health(self):
        return self.state

    async def entities(self):
        return list(self._entities) if self.state == "connected" else None

    async def verify(self, binding):
        from backend.services.bridge.base import ActuationResult
        return ActuationResult(True, "resolved", "off")


@pytest.fixture
def ha(monkeypatch):
    """Install a fake HA driver and a recorder for audits and probes."""
    from backend.routers import homeassistant as r
    from backend.services.bridge import get_bridge

    driver = FakeDriver()
    monkeypatch.setitem(get_bridge()._drivers, "homeassistant", driver)

    seen = {"audits": [], "tried": [], "probe": ("connected", None)}

    async def record(request, principal, action, *, target=None, detail=None,
                     outcome="success"):
        seen["audits"].append({"action": action, "detail": detail or {}})

    async def _try(url, token):
        seen["tried"].append((url, token))
        return seen["probe"]

    monkeypatch.setattr(r.audit_svc, "record", record)
    monkeypatch.setattr(r, "_try", _try)
    seen["driver"] = driver
    return seen


# ── Who may see and change the connection ────────────────────────────────────

async def test_unauthenticated_is_401(client):
    assert (await client.get(f"{BASE}/connection")).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "operator", "supervisor"])
async def test_only_an_admin_sees_the_connection(client, mint, ha, role):
    """Supervisor included: peripheral_manage is not a key to the whole of HA."""
    r = await client.get(f"{BASE}/connection", headers=mint([role]))
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["operator", "supervisor"])
async def test_only_an_admin_saves_a_connection(client, mint, ha, role):
    r = await client.put(f"{BASE}/connection", headers=mint([role]),
                         json={"url": "http://127.0.0.1:8123", "token": TOKEN})
    assert r.status_code == 403
    assert ha["tried"] == []


async def test_an_admin_sees_the_connection_without_a_token(client, mint, ha):
    r = await client.get(f"{BASE}/connection", headers=mint(["admin"]))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "none" and body["token_set"] is False
    assert "token" not in body


# ── Saving ───────────────────────────────────────────────────────────────────

async def test_a_rejected_token_is_not_saved(client, mint, ha, db):
    """Replacing a working token with a wrong one would take every HA device
    offline on a typo."""
    ha["probe"] = ("offline", "unauthorized")
    before = db.commits
    r = await client.put(f"{BASE}/connection", headers=mint(["admin"]),
                         json={"url": "http://127.0.0.1:8123", "token": TOKEN})
    assert r.status_code == 400
    assert db.commits == before
    assert ha["audits"] == []


async def test_an_unreachable_ha_is_saved_and_says_so(client, mint, ha):
    """It may simply not be up yet."""
    ha["probe"] = ("offline", "unreachable")
    r = await client.put(f"{BASE}/connection", headers=mint(["admin"]),
                         json={"url": "http://127.0.0.1:8123", "token": TOKEN})
    assert r.status_code == 200
    assert ha["audits"][0]["detail"]["error"] == "unreachable"


async def test_a_save_never_echoes_or_audits_the_token(client, mint, ha):
    r = await client.put(f"{BASE}/connection", headers=mint(["admin"]),
                         json={"url": "http://127.0.0.1:8123", "token": TOKEN})
    assert r.status_code == 200
    assert TOKEN not in r.text
    assert TOKEN not in str(ha["audits"])
    assert ha["audits"][0]["action"] == "ha.connection_set"
    assert ha["audits"][0]["detail"]["token_changed"] is True


async def test_the_first_save_needs_a_token(client, mint, ha):
    r = await client.put(f"{BASE}/connection", headers=mint(["admin"]),
                         json={"url": "http://127.0.0.1:8123"})
    assert r.status_code == 422


async def test_a_url_must_be_http(client, mint, ha):
    r = await client.put(f"{BASE}/connection", headers=mint(["admin"]),
                         json={"url": "127.0.0.1:8123", "token": TOKEN})
    assert r.status_code == 422


async def test_testing_a_connection_saves_nothing(client, mint, ha, db):
    ha["probe"] = ("connected", None)
    before = db.commits
    r = await client.post(f"{BASE}/connection/test", headers=mint(["admin"]),
                          json={"url": "http://127.0.0.1:8123", "token": TOKEN})
    assert r.status_code == 200 and r.json() == {"state": "connected", "error": None}
    assert ha["tried"] == [("http://127.0.0.1:8123", TOKEN)]
    assert db.commits == before and TOKEN not in r.text


async def test_forgetting_a_connection_that_was_never_saved_is_404(client, mint, ha):
    r = await client.delete(f"{BASE}/connection", headers=mint(["admin"]))
    assert r.status_code == 404


# ── Entities and import ──────────────────────────────────────────────────────

async def test_listing_entities_needs_peripheral_manage(client, mint, ha):
    assert (await client.get(f"{BASE}/entities",
                             headers=mint(["operator"]))).status_code == 403


async def test_a_supervisor_lists_entities_with_default_categories(client, mint, ha):
    r = await client.get(f"{BASE}/entities", headers=mint(["supervisor"]))
    assert r.status_code == 200
    by_id = {e["entity_id"]: e for e in r.json()}
    assert by_id["switch.relay_ch1"]["category"] == "Access control"
    assert by_id["light.yard"]["category"] == "Lighting"
    assert by_id["switch.relay_ch1"]["bound_to"] is None


def test_every_drivable_domain_has_a_default_category():
    """An entity the driver can drive but import cannot place would land in
    a category the SPA has no icon or actions for."""
    from backend.models import PERIPHERAL_CATEGORIES
    from backend.routers.homeassistant import DEFAULT_CATEGORY
    from backend.services.bridge.homeassistant import _SERVICES
    assert set(DEFAULT_CATEGORY) == set(_SERVICES)
    assert set(DEFAULT_CATEGORY.values()) <= set(PERIPHERAL_CATEGORIES)


def test_the_category_list_matches_its_migration():
    """models.PeripheralCategory and ck_peripherals_category must agree, or
    the API accepts a value the database refuses with a 500."""
    import re
    from pathlib import Path
    from backend.models import PERIPHERAL_CATEGORIES
    mig = (Path(__file__).resolve().parents[1] / "migrations" / "versions"
           / "082_peripheral_categories.py").read_text()
    new = re.search(r'_NEW = \((.*?)\)\n', mig, re.S).group(1)
    assert set(re.findall(r"'([^']+)'", new)) == set(PERIPHERAL_CATEGORIES)


async def test_an_offline_ha_is_503_naming_why(client, mint, ha):
    ha["driver"].state, ha["driver"].last_error = "offline", "unauthorized"
    r = await client.get(f"{BASE}/entities", headers=mint(["admin"]))
    assert r.status_code == 503
    assert "unauthorized" in r.json()["detail"]


async def test_import_makes_a_bound_peripheral(client, mint, ha, db):
    r = await client.post(f"{BASE}/import", headers=mint(["supervisor"]),
                          json={"items": [{"entity_id": "switch.relay_ch1",
                                           "name": "Gate Barrier"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 1
    assert body["results"][0]["result"] == "imported"
    from backend.models import Peripheral, PeripheralBinding
    periph = [o for o in db.added if isinstance(o, Peripheral)][0]
    binding = [o for o in db.added if isinstance(o, PeripheralBinding)][0]
    assert periph.name == "Gate Barrier" and periph.external_id == "switch.relay_ch1"
    assert periph.category == "Access control"
    assert (binding.driver, binding.address) == ("homeassistant", "switch.relay_ch1")
    assert ha["audits"][0]["detail"]["source"] == "homeassistant"


async def test_an_entity_ha_does_not_have_is_not_imported(client, mint, ha, db):
    r = await client.post(f"{BASE}/import", headers=mint(["admin"]),
                          json={"items": [{"entity_id": "switch.nope"}]})
    assert r.status_code == 200
    assert r.json() == {"imported": 0,
                        "results": [{"entity_id": "switch.nope", "result": "not_found"}]}
    assert db.added == []
