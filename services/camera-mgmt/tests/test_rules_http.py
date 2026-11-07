"""Rules API: who may read, who may write, and who may ARM.

Driven over ASGI with a real RS256 token — `conftest.py`'s `client` and `mint`.
`mint(["supervisor"])` returns a ready Authorization dict, so it is passed
straight as `headers=`. Nothing here patches out authentication: a test that
stubs `get_principal` cannot tell you authorisation works.

ARMING IS THE AUTHORITY THAT MATTERS and most of this file is about it. Editing
a log_only rule is comparatively harmless — it changes what a rule would match
and the rule still moves nothing. Promoting one to armed is the moment it
becomes able to open a barrier. So `mode` is not a patchable field, arming has
its own endpoint, and it audits as `rule.armed` rather than as a field diff.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT. The conftest `db` is a
`StubSession`: it keeps the objects it was handed and answers `get()` from
them, which is enough for create-then-arm, but it does not run queries. So
anything that depends on a SELECT or on a database constraint — case-insensitive
name uniqueness, the scope_value CHECK, what deletion does to an alert row —
lives in `test_alert_rules_db.py` behind RULES_TEST_DB, where a real Postgres
answers. Asserting those here would be asserting the stub.

Run (from services/camera-mgmt): python -m pytest tests/test_rules_http.py -q
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

URL = "/api/rules"
RULE = {
    "name": "gate on intrusion",
    "activity": "unauthorised_access",
    "scope_kind": "any",
    "severity": "warning",
    "actions": [{"type": "peripheral", "peripheral": "gate-3-barrier",
                 "command": "open", "for_seconds": 8, "then": "close"}],
}


@pytest.fixture
def audits(monkeypatch):
    """Capture audit writes at the router's own call site — the pattern
    test_search_audit.py uses, so what is asserted is the call this handler
    makes rather than anything the audit service does with it."""
    from backend.routers import rules as rules_router

    seen = []

    async def record(request, principal, action, *, target=None, detail=None,
                     outcome="success"):
        seen.append({"action": action, "target": target, "detail": detail or {}})

    monkeypatch.setattr(rules_router.audit_svc, "record", record)
    return seen


def _actions(audits):
    return [a["action"] for a in audits]


async def _create(client, mint, body=None, roles=("supervisor",)):
    return await client.post(URL, json=body or RULE, headers=mint(list(roles)))


# ── The default, and who may create at all ───────────────────────────────────

async def test_create_defaults_to_log_only(client, mint, audits):
    """A rule arrives unable to move anything, whatever its actions say. The
    alternative — trusting the author and defaulting to armed — makes the only
    way to validate a rule letting it swing a real gate."""
    r = await _create(client, mint)
    assert r.status_code == 201, r.text
    assert r.json()["mode"] == "log_only"


async def test_mode_cannot_be_smuggled_in_at_create(client, mint, audits):
    """`mode` is not a field of AlertRuleIn, and the model forbids extras, so
    asking for an armed rule at creation is a 422 rather than a value quietly
    dropped. PeripheralIn excludes `last_state` the same way."""
    r = await _create(client, mint, {**RULE, "mode": "armed"})
    assert r.status_code == 422


async def test_creating_a_rule_requires_rule_manage(client, mint):
    assert (await _create(client, mint, roles=("viewer",))).status_code == 403
    assert (await _create(client, mint, roles=("operator",))).status_code == 403


async def test_unauthenticated_gets_401(client):
    assert (await client.get(URL)).status_code == 401


async def test_creating_a_rule_is_audited(client, mint, audits):
    await _create(client, mint)
    assert "rule.created" in _actions(audits)


# ── Arming ───────────────────────────────────────────────────────────────────

async def test_arming_requires_rule_manage(client, mint, audits):
    rid = (await _create(client, mint)).json()["id"]
    r = await client.post(f"{URL}/{rid}/arm", json={}, headers=mint(["operator"]))
    assert r.status_code == 403


async def test_arming_flips_mode_and_audits_as_its_own_action(client, mint, audits):
    """A rule becoming able to open a gate must be legible in the audit log as
    exactly that — not as `rule.updated {changed: ["mode"]}`, which an auditor
    scanning for authority changes would read straight past."""
    rid = (await _create(client, mint)).json()["id"]
    r = await client.post(f"{URL}/{rid}/arm", json={}, headers=mint(["supervisor"]))
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "armed"
    assert "rule.armed" in _actions(audits)
    assert "rule.updated" not in _actions(audits)


async def test_the_arm_audit_records_what_the_rule_can_now_do(client, mint, audits):
    """"This rule was armed" is half the fact. Which peripheral it can now
    reach is the other half, and it is the half that cannot be reconstructed
    after the rule is edited."""
    rid = (await _create(client, mint)).json()["id"]
    await client.post(f"{URL}/{rid}/arm", json={}, headers=mint(["supervisor"]))
    entry = next(a for a in audits if a["action"] == "rule.armed")
    assert entry["detail"]["actions"][0]["peripheral"] == "gate-3-barrier"


async def test_disarming_one_rule_returns_it_to_log_only(client, mint, audits):
    rid = (await _create(client, mint)).json()["id"]
    await client.post(f"{URL}/{rid}/arm", json={}, headers=mint(["supervisor"]))
    r = await client.post(f"{URL}/{rid}/disarm", json={}, headers=mint(["supervisor"]))
    assert r.status_code == 200 and r.json()["mode"] == "log_only"
    assert "rule.disarmed" in _actions(audits)


async def test_patch_cannot_arm_a_rule(client, mint, audits):
    """422, not a silently-ignored field. An operator who PATCHes mode and gets
    a 200 back reasonably believes the rule is armed."""
    rid = (await _create(client, mint)).json()["id"]
    r = await client.patch(f"{URL}/{rid}", json={"mode": "armed"},
                           headers=mint(["supervisor"]))
    assert r.status_code == 422


async def test_patching_something_real_still_works_and_audits(client, mint, audits):
    rid = (await _create(client, mint)).json()["id"]
    r = await client.patch(f"{URL}/{rid}", json={"cooldown_s": 300},
                           headers=mint(["supervisor"]))
    assert r.status_code == 200 and r.json()["cooldown_s"] == 300
    assert "rule.updated" in _actions(audits)


async def test_an_unknown_rule_is_404_not_500(client, mint, audits):
    r = await client.post(f"{URL}/{uuid.uuid4()}/arm", json={},
                          headers=mint(["supervisor"]))
    assert r.status_code == 404


async def test_deleting_a_rule_is_audited(client, mint, audits):
    rid = (await _create(client, mint)).json()["id"]
    assert (await client.delete(f"{URL}/{rid}",
                                headers=mint(["supervisor"]))).status_code == 200
    assert "rule.deleted" in _actions(audits)


# ── What a rule is allowed to say ────────────────────────────────────────────

async def test_an_unknown_action_type_is_rejected(client, mint):
    """An unrecognised action accepted here becomes a rule that looks fully
    configured and silently never does anything. There is deliberately no
    email/SMS/push member of the union — see spec 4.3."""
    r = await _create(client, mint, {**RULE, "actions": [{"type": "email",
                                                          "to": "x@y.z"}]})
    assert r.status_code == 422


async def test_for_seconds_requires_a_then(client, mint):
    """A pulse with no follow-up is an open barrier with no close."""
    r = await _create(client, mint, {**RULE, "actions": [
        {"type": "peripheral", "peripheral": "gate-3-barrier",
         "command": "open", "for_seconds": 8}]})
    assert r.status_code == 422


async def test_a_scoped_rule_must_name_its_scope(client, mint):
    """Mirrors ck_alert_rules_scope_value. A camera-scoped rule with no camera
    matches nothing while looking completely configured — so it is refused at
    the door with a message, not by an IntegrityError at commit."""
    r = await _create(client, mint, {**RULE, "scope_kind": "camera"})
    assert r.status_code == 422


async def test_an_any_scoped_rule_must_not_name_one(client, mint):
    r = await _create(client, mint, {**RULE, "scope_kind": "any",
                                     "scope_value": "cam2-o8iu"})
    assert r.status_code == 422


async def test_a_scoped_rule_with_its_scope_is_accepted(client, mint, audits):
    r = await _create(client, mint, {**RULE, "scope_kind": "camera",
                                     "scope_value": "cam2-o8iu"})
    assert r.status_code == 201, r.text


async def test_confidence_outside_zero_to_one_is_rejected(client, mint):
    assert (await _create(client, mint, {**RULE, "min_confidence": 1.5})).status_code == 422


# ── The kill switch ──────────────────────────────────────────────────────────

async def test_disarm_requires_rule_manage(client, mint, audits):
    r = await client.post(f"{URL}/disarm", json={"reason": "gate flapping"},
                          headers=mint(["operator"]))
    assert r.status_code == 403


async def test_disarm_is_audited(client, mint, audits):
    r = await client.post(f"{URL}/disarm", json={"reason": "gate flapping"},
                          headers=mint(["supervisor"]))
    assert r.status_code == 200, r.text
    assert "rules.disarmed" in _actions(audits)


async def test_disarm_requires_a_reason(client, mint):
    """A disarm with no stated reason is an unexplained outage for whoever
    comes on shift next and finds the automation dead."""
    assert (await client.post(f"{URL}/disarm", json={},
                              headers=mint(["supervisor"]))).status_code == 422
    assert (await client.post(f"{URL}/disarm", json={"reason": "  "},
                              headers=mint(["supervisor"]))).status_code == 422


async def test_arm_state_names_who_disarmed_it_and_why(client, mint, audits):
    h = mint(["supervisor"])
    await client.post(f"{URL}/disarm", json={"reason": "gate flapping"}, headers=h)
    body = (await client.get(f"{URL}/arm-state", headers=h)).json()
    assert body["armed"] is False
    assert body["reason"] == "gate flapping"
    assert body["by"]
    assert body["degraded"] is False


async def test_rearm_restores_and_is_audited(client, mint, audits):
    h = mint(["supervisor"])
    await client.post(f"{URL}/disarm", json={"reason": "gate flapping"}, headers=h)
    r = await client.post(f"{URL}/rearm", json={"reason": "gate fixed"}, headers=h)
    assert r.status_code == 200 and r.json()["armed"] is True
    assert "rules.rearmed" in _actions(audits)


async def test_any_authenticated_principal_may_read_the_arm_state(client, mint):
    """The SPA shows the disarmed banner to everyone who can see the page. A
    viewer who cannot tell that automation is off is worse off than one who
    can."""
    assert (await client.get(f"{URL}/arm-state",
                             headers=mint(["viewer"]))).status_code == 200


async def test_arm_state_is_not_public(client):
    assert (await client.get(f"{URL}/arm-state")).status_code == 401
