"""Alerts API: reading the trail, closing the loop, and the evidence hold.

Four authorities meet on this surface and the interesting tests are about where
the lines fall. Watching a wall and marking an alert handled are different
things; so are closing an alert and deciding what it turned out to be.

`resolution` is the column that makes per-rule false-positive reporting
possible at all, and it cannot be reconstructed after the fact — so it is a
constrained Literal from the first row rather than a free string that would
accumulate four spellings of "false alarm" before anyone tried to GROUP BY it.

WHAT IS NOT HERE. The conftest `db` is a StubSession: it answers `get()` from
rows it was handed and runs no queries, and nothing can hand it an Alert
because alerts are raised by the engine, not by an endpoint. So the state
machine — ack stamping an actor, a resolution reaching the column, legal_hold
surviving the sweep — lives in `test_alert_rules_db.py` behind RULES_TEST_DB.
A 403-vs-404 distinction is still worth making here, though: it is how you tell
"the capability refused this caller" from "the caller got through and there was
nothing there", and only the routed request can tell you that.

Run (from services/camera-mgmt): python -m pytest tests/test_alerts_http.py -q
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

URL = "/api/alerts"


@pytest.fixture
def audits(monkeypatch):
    from backend.routers import alerts as alerts_router

    seen = []

    async def record(request, principal, action, *, target=None, detail=None,
                     outcome="success"):
        seen.append({"action": action, "target": target, "detail": detail or {}})

    monkeypatch.setattr(alerts_router.audit_svc, "record", record)
    return seen


# ── Reading ──────────────────────────────────────────────────────────────────

async def test_listing_requires_authentication(client):
    assert (await client.get(URL)).status_code == 401


async def test_a_viewer_may_read_the_trail(client, mint):
    """alerts_view is granted to viewer by default: somebody watching a wall
    should be able to see what fired without being able to touch it."""
    assert (await client.get(URL, headers=mint(["viewer"]))).status_code == 200


async def test_a_role_without_alerts_view_is_refused(client, mint):
    """dpo is the role the default matrix withholds it from, so it is the one
    that proves the gate is real rather than decorative."""
    assert (await client.get(URL, headers=mint(["dpo"]))).status_code == 403


async def test_filtering_by_state_is_accepted(client, mint):
    r = await client.get(f"{URL}?state=new", headers=mint(["operator"]))
    assert r.status_code == 200
    assert all(a["state"] == "new" for a in r.json()["alerts"])


async def test_an_unknown_camera_filter_returns_nothing_rather_than_everything(
        client, mint):
    """A filter that cannot be resolved must narrow to nothing, never widen to
    the unfiltered set — that is how a scoping bug leaks another camera's
    alerts into a filtered view."""
    r = await client.get(f"{URL}?camera=no-such-cam", headers=mint(["operator"]))
    assert r.status_code == 200
    assert r.json() == {"total": 0, "alerts": []}


# ── Closing the loop ─────────────────────────────────────────────────────────

async def test_a_viewer_may_not_acknowledge(client, mint, audits):
    """Watching a wall and marking an alert handled are different authorities."""
    r = await client.post(f"{URL}/{uuid.uuid4()}/ack", json={},
                          headers=mint(["viewer"]))
    assert r.status_code == 403


async def test_an_operator_gets_past_the_gate_and_finds_nothing(client, mint, audits):
    """404, not 403 — which is the whole point of asserting it. An operator
    holds alerts_ack, so a refusal here would mean the capability was wired to
    the wrong permission and nobody would notice from a 403 alone."""
    r = await client.post(f"{URL}/{uuid.uuid4()}/ack", json={},
                          headers=mint(["operator"]))
    assert r.status_code == 404


async def test_an_unknown_alert_is_404_not_500(client, mint, audits):
    for path, body in ((f"/{uuid.uuid4()}/ack", {}),
                       (f"/{uuid.uuid4()}/resolve", {"resolution": "false_positive"}),
                       (f"/{uuid.uuid4()}/hold", {"legal_hold": True})):
        r = await client.post(URL + path, json=body, headers=mint(["operator"]))
        assert r.status_code == 404, path


async def test_resolution_is_constrained_to_the_known_verdicts(client, mint, audits):
    """422, not a stored string. Reporting that has to GROUP BY this column
    cannot survive free text, and the column cannot be cleaned up afterwards
    because nobody remembers what "probably fine" meant in March."""
    r = await client.post(f"{URL}/{uuid.uuid4()}/resolve",
                          json={"resolution": "probably_fine"},
                          headers=mint(["operator"]))
    assert r.status_code == 422


async def test_resolving_requires_a_verdict_at_all(client, mint, audits):
    r = await client.post(f"{URL}/{uuid.uuid4()}/resolve", json={},
                          headers=mint(["operator"]))
    assert r.status_code == 422


async def test_every_known_verdict_is_accepted(client, mint, audits):
    """Reaching 404 means the body validated — the alert simply does not exist.
    A 422 here would mean a verdict the product documents is unusable."""
    for verdict in ("true_positive", "false_positive", "duplicate", "no_action"):
        r = await client.post(f"{URL}/{uuid.uuid4()}/resolve",
                              json={"resolution": verdict},
                              headers=mint(["operator"]))
        assert r.status_code == 404, verdict


async def test_a_viewer_may_not_place_an_evidence_hold(client, mint, audits):
    r = await client.post(f"{URL}/{uuid.uuid4()}/hold", json={"legal_hold": True},
                          headers=mint(["viewer"]))
    assert r.status_code == 403
