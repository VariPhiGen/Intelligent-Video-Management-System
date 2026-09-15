"""security.py — identity & access control for the Camera Management service.

User authentication is **OIDC** (Keycloak in production, §16 of the blueprint):
clients present a Bearer JWT, which this service validates against the issuer's
JWKS (RS256 signature, issuer, expiry, optional audience). Roles are read from
the token's ``realm_access`` / ``resource_access`` claims and enforced with
``require_role``.

Two non-user principals are also accepted:
  • **Service** — the Discovery microservice presents the shared ``X-Internal-Key``
    (machine-to-machine; graduates to mTLS in a later hardening pass).
  • **Dev** — when ``DEV_AUTH=true`` the stack runs with no Keycloak: every
    request is treated as a full admin. Development only; never in production.
"""
from __future__ import annotations

import hmac

import jwt
import structlog
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

from .config import settings

log = structlog.get_logger(__name__)

INTERNAL_HEADER = "X-Internal-Key"

# Roles a service/dev principal implicitly holds (so RBAC checks pass for them).
# Keep in step with keycloak_admin.PRODUCT_ROLES — /me reports these verbatim.
_ALL_ROLES = frozenset({"admin", "supervisor", "operator", "viewer", "dpo"})


class Principal:
    """The authenticated caller: who they are, what roles they hold, how they
    proved it (``user`` via OIDC, ``service`` via internal key, ``dev`` bypass).

    ``session_id`` is the Keycloak login-session id (``sid``) for user tokens —
    stable across token refreshes within one login, new on each fresh login. The
    audit trail uses it to record each user's login exactly once (see
    services/audit.record_login_once). ``None`` for service/dev principals."""

    __slots__ = ("subject", "roles", "kind", "session_id")

    def __init__(
        self, subject: str, roles: frozenset[str], kind: str,
        session_id: str | None = None,
    ) -> None:
        self.subject = subject
        self.roles = roles
        self.kind = kind
        self.session_id = session_id

    def has_any(self, wanted: frozenset[str]) -> bool:
        # Service/dev principals are trusted for everything.
        return self.kind in ("service", "dev") or bool(self.roles & wanted)


# ── OIDC token validation ─────────────────────────────────────────────────────

_jwk_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        # PyJWKClient caches keys and refreshes on unknown kid.
        _jwk_client = PyJWKClient(settings.jwks_url, cache_keys=True)
    return _jwk_client


def _roles_from_claims(payload: dict) -> frozenset[str]:
    roles: set[str] = set(payload.get("realm_access", {}).get("roles", []))
    for client in payload.get("resource_access", {}).values():
        roles.update(client.get("roles", []))
    return frozenset(roles)


def _decode_bearer(token: str) -> dict:
    signing_key = _jwks().get_signing_key_from_jwt(token).key
    audience = settings.oidc_audience or None
    payload = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        audience=audience,
        options={"verify_aud": bool(audience), "require": ["exp", "iat", "iss"]},
    )
    # Issuer is checked against an allow-list, not one pinned URL: Keycloak's
    # hostname is dynamic, so `iss` follows the host the browser logged in
    # through (localhost on the box, SERVER_IP from the LAN). The signature
    # check above already proves the token came from OUR Keycloak — the pin
    # only rejects tokens minted for a different realm.
    iss = str(payload.get("iss", "")).rstrip("/")
    if iss not in settings.oidc_allowed_issuers:
        raise jwt.InvalidIssuerError(f"Issuer {iss!r} not in the allowed set")
    return payload


# ── Principal resolution ──────────────────────────────────────────────────────

def _internal_key_ok(request: Request) -> bool:
    key = request.headers.get(INTERNAL_HEADER)
    return bool(key) and hmac.compare_digest(key, settings.internal_api_key)


async def get_principal(request: Request) -> Principal:
    # 1. Trusted internal service (Discovery proxy / relay callback).
    if _internal_key_ok(request):
        return Principal("internal", _ALL_ROLES, "service")

    # 2. OIDC Bearer token.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        try:
            payload = _decode_bearer(token)
        except Exception as exc:  # noqa: BLE001 — any failure is an auth failure
            log.info("auth.bearer.rejected", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        subject = payload.get("preferred_username") or payload.get("sub") or "user"
        # Keycloak login-session id; older tokens use `session_state`. Fall back
        # to the token id so login auditing still dedupes if neither is present.
        session_id = payload.get("sid") or payload.get("session_state") or payload.get("jti")
        return Principal(subject, _roles_from_claims(payload), "user", session_id)

    # 3. Dev bypass (no Keycloak). Development only.
    if settings.dev_auth:
        return Principal("dev", _ALL_ROLES, "dev")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def require_authenticated(request: Request) -> Principal:
    """Any valid principal (user / service / dev)."""
    return await get_principal(request)


def require_role(*roles: str):
    """Dependency factory: caller must hold at least one of ``roles``."""
    wanted = frozenset(roles)

    async def _checker(request: Request) -> Principal:
        principal = await get_principal(request)
        if not principal.has_any(wanted):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {', '.join(sorted(wanted))}",
            )
        return principal

    return _checker
