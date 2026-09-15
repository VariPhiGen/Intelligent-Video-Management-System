"""keycloak_admin.py — the core's access to the Keycloak Admin REST API.

The product's identity lives in Keycloak (realm ``vms``). This module
authenticates as the bootstrap admin (``admin-cli`` client, master realm) with
the same credentials compose already provisions, caches the token until close
to expiry, and scopes every request to the product realm.

What stays here is what the core needs in every build: login events for the
audit trail, and the self-service password change (verify the current password,
set the new one). Administering OTHER accounts — listing, creating, editing and
deleting users, assigning roles — belongs to the identity extension, which is
not part of the open core and borrows `_req` / `_raise_for` from here so token
handling stays defined in one place.

Only the product's realm roles are exposed; Keycloak composites like
``default-roles-vms`` stay hidden.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
import structlog
from fastapi import HTTPException

from ..config import settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)
PRODUCT_ROLES = ("admin", "supervisor", "operator", "viewer", "dpo")

# Confidential client used ONLY to verify a user's CURRENT password (direct
# grant) when they change it themselves. It is deliberately not vms-web: the SPA
# client is public, and enabling password grants there would let anyone on the
# LAN trade credentials for tokens straight at the token endpoint, bypassing the
# browser flow and PKCE. deploy/keycloak/sync-client.py creates/repairs it.
PWCHECK_CLIENT_ID = "vms-pwcheck"

_client: Optional[httpx.AsyncClient] = None
_token: Optional[str] = None
_token_exp: float = 0.0
_pwcheck_secret: Optional[str] = None


def _base() -> str:
    return settings.keycloak_admin_url.rstrip("/")


def _realm() -> str:
    return settings.keycloak_realm


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _admin_token() -> str:
    """Bootstrap-admin token (master realm), cached until 30 s before expiry."""
    global _token, _token_exp
    if _token and time.monotonic() < _token_exp:
        return _token
    try:
        resp = await _get_client().post(
            f"{_base()}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": settings.keycloak_admin_user,
                "password": settings.keycloak_admin_password,
            },
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"Keycloak is unreachable: {exc}") from exc
    if resp.status_code != 200:
        log.error("kcadmin.token_failed", status=resp.status_code, body=resp.text[:200])
        raise HTTPException(502, detail="Keycloak admin authentication failed")
    body = resp.json()
    _token = body["access_token"]
    _token_exp = time.monotonic() + max(30, int(body.get("expires_in", 60)) - 30)
    return _token


async def _req(method: str, path: str, _retried: bool = False, **kwargs) -> httpx.Response:
    token = await _admin_token()
    try:
        resp = await _get_client().request(
            method,
            f"{_base()}/admin/realms/{_realm()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"Keycloak is unreachable: {exc}") from exc
    if resp.status_code == 401 and not _retried:
        # The token raced its expiry: drop it and try once with a fresh one.
        #
        # ONCE, and the flag is what makes that true. This recursed with no
        # bound until 2026-09-10, which is harmless for the case it was written
        # for — a genuinely stale token succeeds on the second attempt — and
        # not harmless at all for a 401 that will never clear. A bootstrap
        # admin that has lost its realm-management role answers 401 to every
        # request forever, so one API call became ~1000 requests to Keycloak
        # and then surfaced as a RecursionError, i.e. a 500 with no hint of
        # what was wrong. Retrying once and returning the 401 lets
        # `_raise_for` report it.
        global _token
        _token = None
        return await _req(method, path, _retried=True, **kwargs)
    return resp


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 400:
        detail = what
        try:
            detail = resp.json().get("errorMessage") or resp.json().get("error") or what
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            400 if resp.status_code < 500 else 502,
            detail=f"{what}: {detail}" if detail != what else what,
        )


# ── Login events (for the audit trail) ────────────────────────────────────────

async def get_events(types: list[str], max: int = 200) -> list[dict[str, Any]]:
    """Recent user events of the given types, newest-first. Requires event
    storage to be enabled on the realm (see get/set_events_config)."""
    params: list[tuple[str, Any]] = [("max", max)]
    params += [("type", t) for t in types]
    resp = await _req("GET", "/events", params=params)
    _raise_for(resp, "Could not fetch Keycloak events")
    return resp.json()


async def get_events_config() -> dict[str, Any]:
    resp = await _req("GET", "/events/config")
    _raise_for(resp, "Could not read Keycloak events config")
    return resp.json()


async def set_events_config(cfg: dict[str, Any]) -> None:
    resp = await _req("PUT", "/events/config", json=cfg)
    _raise_for(resp, "Could not update Keycloak events config")


# ── Setting a password (the extension's reset calls this too) ─────────────────

async def set_password(user_id: str, password: str, temporary: bool) -> None:
    resp = await _req("PUT", f"/users/{user_id}/reset-password", json={
        "type": "password", "value": password, "temporary": temporary,
    })
    _raise_for(resp, "Could not set password")


# ── Self-service password change ─────────────────────────────────────────────

async def get_user_by_username(username: str) -> dict[str, Any]:
    """Exact-match lookup. Keycloak's ?username= search is a substring match by
    default, so `exact=true` matters — "admin" would otherwise also match
    "admin2" and we could reset the wrong account's password."""
    resp = await _req("GET", "/users", params={"username": username, "exact": "true"})
    _raise_for(resp, "Could not look up user")
    users = resp.json()
    if not users:
        raise HTTPException(404, detail="User not found")
    return users[0]


async def _get_pwcheck_secret() -> str:
    """Client secret of the password-verification client, read once via the
    admin API and cached — so the secret never has to live in .env."""
    global _pwcheck_secret
    if _pwcheck_secret:
        return _pwcheck_secret
    resp = await _req("GET", "/clients", params={"clientId": PWCHECK_CLIENT_ID})
    _raise_for(resp, "Could not look up the password-verification client")
    clients = resp.json()
    if not clients:
        raise HTTPException(
            503,
            detail=f"Password verification is unavailable: the '{PWCHECK_CLIENT_ID}' "
                   "Keycloak client is missing. Re-run `./vms up -d keycloak_client_sync`.",
        )
    resp = await _req("GET", f"/clients/{clients[0]['id']}/client-secret")
    _raise_for(resp, "Could not read the password-verification client secret")
    secret = resp.json().get("value")
    if not secret:
        raise HTTPException(503, detail="Password verification is unavailable: no client secret")
    _pwcheck_secret = secret
    return secret


async def verify_password(username: str, password: str) -> bool:
    """Is this the user's current password? Direct grant against the confidential
    pwcheck client.

    A failed attempt counts toward Keycloak's brute-force protection (the realm
    ships failureFactor=5, temporary lockout) — deliberate, but it does mean a
    user who guesses their old password wrong five times gets locked out for a
    while, exactly as if they had fumbled the login form.
    """
    secret = await _get_pwcheck_secret()
    try:
        resp = await _get_client().post(
            f"{_base()}/realms/{_realm()}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": PWCHECK_CLIENT_ID,
                "client_secret": secret,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"Keycloak is unreachable: {exc}") from exc
    if resp.status_code == 200:
        return True

    body = {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        pass
    desc = str(body.get("error_description", ""))

    # Keycloak returns invalid_grant for BOTH "wrong password" and "this account
    # is incomplete" — same status, same error code. Reporting the latter as a
    # bad password sends the user hunting for a password that was never wrong.
    # VERIFY_PROFILE is intentionally ON (it prompts new users for their email at
    # first login), so a user with a valid session normally has a complete
    # profile; reaching this means a required action is still pending for them.
    if "not fully set up" in desc.lower():
        log.warning("kcadmin.pwverify_account_incomplete", user=username, detail=desc)
        raise HTTPException(
            409,
            detail="Your account has a pending required action in Keycloak (e.g. a "
                   "profile or password update), so its password cannot be changed "
                   "here yet. An administrator can clear it under Users → Details.",
        )

    if resp.status_code in (400, 401) and body.get("error") == "invalid_grant":
        return False  # genuinely the wrong password

    log.error("kcadmin.pwverify_failed", status=resp.status_code, body=resp.text[:200])
    raise HTTPException(502, detail="Could not verify the current password")
