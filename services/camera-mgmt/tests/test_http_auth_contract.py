"""The security boundary, asserted over HTTP.

security.py is well written and, until this file, entirely unexercised through a
request. That gap matters more here than the usual "we should test the HTTP
layer" argument, because authorisation in this backend does not live in the
handlers — it lives in `dependencies=[...]` on a mount in main.py or on a route
decorator. It comes into existence when a request is ROUTED. Every direct-call
test in this suite, however thorough, walks straight past it.

test_router_authz_wiring.py proves the gates are DECLARED. These tests prove
they FIRE, and that the thing they fire on is a real token: a throwaway RSA key
signs each one and the real `jwt.decode` verifies it, so expiry, signature and
issuer are genuinely checked rather than stubbed into always-true.

Scope is the boundary itself and a representative slice of the camera API —
not every endpoint. Endpoints are parametrised where the assertion is identical
across them, so adding a protected route to that list is one line.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.asyncio


# A slice across every mount that carries an include-level gate. Not the full
# 96 routes on purpose: these are the ones whose exposure would be worst, and
# the assertion below is the same sentence for all of them.
PROTECTED_GET = [
    "/api/cameras",
    "/api/cameras/uptime/summary",
    "/api/cameras/analytics/types",
    "/api/me",
    "/api/audit",
    "/api/sitemaps",
    "/api/peripherals",
    "/api/discovery/devices",
    "/api/search/cameras",
]

PROTECTED_WRITE = [
    ("post", "/api/cameras"),
    ("post", "/api/sitemaps"),
    ("post", "/api/peripherals"),
    ("post", "/api/discovery/scan"),
]


# ── Unauthenticated ────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", PROTECTED_GET)
async def test_an_anonymous_get_is_refused(client, path):
    r = await client.get(path)
    assert r.status_code == 401, f"{path} served an anonymous caller"


@pytest.mark.parametrize("method,path", PROTECTED_WRITE)
async def test_an_anonymous_write_is_refused(client, method, path):
    r = await getattr(client, method)(path, json={})
    assert r.status_code == 401, f"{method.upper()} {path} accepted anonymously"


async def test_a_401_tells_the_client_how_to_authenticate(client):
    """The SPA drives its login off this header; without it a session that has
    expired mid-use looks like a generic failure rather than "log in again"."""
    r = await client.get("/api/cameras")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"


async def test_the_auth_config_route_stays_public(client):
    """The SPA must read this BEFORE it can log in. If it ever starts requiring
    a token, login becomes impossible rather than merely broken."""
    r = await client.get("/api/auth/config")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# ── Authenticated ──────────────────────────────────────────────────────────

async def test_a_valid_token_reaches_the_handler(client, mint):
    r = await client.get("/api/cameras", headers=mint(["admin"]))
    assert r.status_code == 200
    assert r.json() == []


async def test_me_reports_the_roles_the_token_carried(client, mint):
    r = await client.get("/api/me", headers=mint(["supervisor"]))
    assert r.status_code == 200
    body = r.json()
    assert "supervisor" in body.get("roles", []), body


async def test_roles_are_read_from_client_claims_too(client, mint):
    """Keycloak puts client roles under resource_access, not realm_access.
    `_roles_from_claims` merges both; a regression there would silently
    downgrade every user who holds a client-scoped role."""
    headers = mint([], client_roles={"vms-frontend": ["supervisor"]})
    r = await client.get("/api/me", headers=headers)
    assert r.status_code == 200
    assert "supervisor" in r.json().get("roles", [])


# ── Invalid tokens ─────────────────────────────────────────────────────────

async def test_an_expired_token_is_refused(client, mint):
    r = await client.get("/api/cameras", headers=mint(["admin"], expires_in=-60))
    assert r.status_code == 401


async def test_a_token_signed_by_the_wrong_key_is_refused(client, mint):
    """The check that makes the rest meaningful: without signature
    verification a caller writes their own roles claim."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    r = await client.get("/api/cameras", headers=mint(["admin"], key=attacker))
    assert r.status_code == 401


async def test_a_token_from_another_realm_is_refused(client, mint):
    """Signature alone is not enough — `_decode_bearer` pins `iss` to the
    allow-list so a token minted for a different realm is rejected."""
    headers = mint(["admin"], issuer="https://evil.example/realms/other")
    r = await client.get("/api/cameras", headers=headers)
    assert r.status_code == 401


@pytest.mark.parametrize("claim", ["exp", "iat", "iss"])
async def test_a_token_missing_a_required_claim_is_refused(client, mint, claim):
    r = await client.get("/api/cameras", headers=mint(["admin"], omit=[claim]))
    assert r.status_code == 401


@pytest.mark.parametrize("value", [
    "",
    "Bearer",
    "Bearer ",
    "Bearer not-a-jwt",
    "Bearer a.b.c",
    "Basic YWRtaW46YWRtaW4=",
    "bearer lowercase-scheme",
])
async def test_a_malformed_authorization_header_is_refused(client, value):
    """None of these may 500. A crash in the auth path is both an outage and
    an information leak."""
    r = await client.get("/api/cameras", headers={"Authorization": value})
    assert r.status_code == 401, f"{value!r} produced {r.status_code}"


async def test_a_rejected_token_does_not_echo_itself_back(client, mint):
    """An error body that repeats the credential puts it into logs and
    browser consoles."""
    headers = mint(["admin"], expires_in=-60)
    token = headers["Authorization"].split(" ", 1)[1]
    r = await client.get("/api/cameras", headers=headers)
    assert token not in r.text


# ── Role enforcement ───────────────────────────────────────────────────────

async def test_a_viewer_cannot_delete_a_camera(client, mint, camera_id):
    """DELETE is `_admin_only`. A viewer reaching it would be able to destroy
    a camera and its recordings."""
    r = await client.delete(f"/api/cameras/{camera_id}", headers=mint(["viewer"]))
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["viewer", "operator", "supervisor", "dpo"])
async def test_only_an_admin_may_delete_a_camera(client, mint, camera_id, role):
    r = await client.delete(f"/api/cameras/{camera_id}", headers=mint([role]))
    assert r.status_code == 403, f"{role} was allowed past the admin gate"


async def test_an_admin_passes_the_admin_gate(client, mint, camera_id):
    """Past the gate, not necessarily to success — the camera does not exist,
    so a 404 is the right answer and proves the gate was not what stopped it."""
    r = await client.delete(f"/api/cameras/{camera_id}", headers=mint(["admin"]))
    assert r.status_code != 403
    assert r.status_code == 404


# ── Capability enforcement ─────────────────────────────────────────────────

@pytest.mark.parametrize("role,expected", [
    ("supervisor", True),    # DEFAULT_POLICY grants camera_manage
    ("operator", False),
    ("viewer", False),
    ("dpo", False),
])
async def test_camera_manage_follows_the_policy_matrix(client, mint, role, expected):
    """POST /api/cameras is gated on the GRANTABLE camera_manage capability
    rather than a fixed role, so the answer has to come from DEFAULT_POLICY.
    A 422 means the body was rejected — which is past the gate, and that is
    what this test is asking about."""
    r = await client.post("/api/cameras", headers=mint([role]), json={})
    if expected:
        assert r.status_code != 403, f"{role} should hold camera_manage"
    else:
        assert r.status_code == 403, f"{role} should not hold camera_manage"


async def test_an_admin_holds_every_capability_without_a_grant(client, mint):
    """`policy.allowed` short-circuits for admin. If that stops being true,
    an admin locks themselves out of routes no policy row mentions."""
    r = await client.post("/api/cameras", headers=mint(["admin"]), json={})
    assert r.status_code != 403


async def test_an_unknown_role_holds_nothing(client, mint):
    """A token carrying a role this product does not define must not be
    treated as anything."""
    r = await client.post("/api/cameras", headers=mint(["wizard"]), json={})
    assert r.status_code == 403


async def test_a_403_says_which_permission_is_missing(client, mint):
    """The UI shows this string to an operator who cannot do something. It
    must name the permission and how to get it, not just refuse."""
    r = await client.post("/api/cameras", headers=mint(["viewer"]), json={})
    assert r.status_code == 403
    detail = r.json().get("detail", "")
    assert "permission" in detail.lower(), detail


# ── The two non-user principals ────────────────────────────────────────────

async def test_the_internal_key_is_accepted_as_a_service_principal(client):
    """Sibling services call in with a shared key rather than a user token."""
    r = await client.get("/api/cameras", headers={"X-Internal-Key": "test-internal-key"})
    assert r.status_code == 200


async def test_a_wrong_internal_key_is_not_a_principal(client):
    r = await client.get("/api/cameras", headers={"X-Internal-Key": "wrong"})
    assert r.status_code == 401


async def test_an_empty_internal_key_is_not_a_principal(client):
    r = await client.get("/api/cameras", headers={"X-Internal-Key": ""})
    assert r.status_code == 401


async def test_an_unset_internal_key_does_not_authenticate_everybody(client, monkeypatch):
    """The dangerous case, which a non-empty configured key hides.

    `_internal_key_ok` is `bool(key) and compare_digest(key, configured)`. Drop
    the `bool(key)` and the function still behaves correctly for every test
    that configures a real key — an empty header simply fails the comparison.
    It only turns into "every anonymous request is a fully trusted service
    principal" when the CONFIGURED key is itself empty, which is precisely the
    state a deployment lands in when INTERNAL_API_KEY is unset.

    So the guard has to be exercised with an empty configuration, or the test
    passes for a reason unrelated to the thing it protects.
    """
    from backend.config import settings

    monkeypatch.setattr(settings, "internal_api_key", "")

    for headers in ({}, {"X-Internal-Key": ""}, {"X-Internal-Key": "anything"}):
        r = await client.get("/api/cameras", headers=headers)
        assert r.status_code == 401, f"{headers} authenticated against an unset key"


async def test_a_service_principal_may_reach_an_admin_route(client, camera_id):
    """`Principal.has_any` trusts service/dev for everything. Pinned because
    it is the rule that makes the internal key so consequential."""
    r = await client.delete(
        f"/api/cameras/{camera_id}", headers={"X-Internal-Key": "test-internal-key"}
    )
    assert r.status_code != 403


async def test_dev_auth_is_off_in_these_tests(client, mint):
    """A guard on the suite itself. DEV_AUTH grants every caller all five
    roles; if it leaked into this fixture, every authorisation test above
    would pass no matter what the code did."""
    from backend.config import settings

    assert settings.dev_auth is False
    r = await client.get("/api/cameras")
    assert r.status_code == 401


async def test_dev_auth_when_on_admits_an_anonymous_caller(client, monkeypatch):
    """The bypass, asserted rather than assumed — this is the switch that must
    never be on in production, so its effect should be written down somewhere
    a reader will find it."""
    from backend.config import settings

    monkeypatch.setattr(settings, "dev_auth", True)
    r = await client.get("/api/cameras")
    assert r.status_code == 200
