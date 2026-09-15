"""auth.py — auth discovery + session identity for the management UI.

Login itself happens at the OIDC provider (Keycloak), not here. The SPA calls
``/auth/config`` (public) to learn how to authenticate, then ``/me`` to read the
current principal once it holds a token.

``/me/password`` is the one write here: any signed-in user changes their OWN
password, proving they know the current one. It is deliberately NOT in users.py
— that whole router is admin-only, which is why non-admins previously had no way
to rotate their own password at all. Admins resetting SOMEONE ELSE's password
(no current password known) stays in users.py.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import settings
from ..security import Principal, require_authenticated
from ..services import audit as audit_svc

log = structlog.get_logger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/auth/config")
async def auth_config(request: Request) -> dict:
    """Public. Tells the SPA whether to run the OIDC flow or the dev bypass, and
    the parameters for the Keycloak JS adapter.

    The Keycloak URL is resolved from the origin the SPA was loaded from: a
    front door listed in OIDC_PUBLIC_HOSTS (e.g. a Cloudflare tunnel, whose
    Keycloak lives on a separate public subdomain, NOT on <host>:8085) gets its
    mapped URL; anything else falls back to OIDC_PUBLIC_URL or None, and the SPA
    derives <host>:KEYCLOAK_PORT itself — the LAN turnkey path. X-Forwarded-Host
    (set by the reverse proxy / cloudflared) is preferred over Host, which
    behind a proxy is often the loopback origin the app really answered on.
    """
    if settings.dev_auth:
        return {"mode": "dev"}
    fwd = request.headers.get("x-forwarded-host")
    host = (fwd.split(",")[0].strip() if fwd else None) or request.headers.get("host")
    return {
        "mode": "oidc",
        "url": settings.oidc_public_url_for(host),
        "port": settings.keycloak_port,
        "realm": settings.oidc_realm,
        "client_id": settings.oidc_client_id,
    }


@router.get("/me")
async def me(
    request: Request,
    principal: Principal = Depends(require_authenticated),
) -> dict:
    from ..services import policy as policy_svc

    # The SPA calls /me right after Keycloak returns a token, so this is our
    # login signal. record_login_once dedupes per session (reloads/refreshes
    # also hit /me but must not each log a login).
    await audit_svc.record_login_once(request, principal)

    return {
        "subject": principal.subject,
        "roles": sorted(principal.roles),
        "kind": principal.kind,
        # Effective capability map (Roles & permissions matrix) — drives UI
        # gating; the API enforces the same policy server-side.
        "permissions": await policy_svc.effective_for(principal),
    }


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.put("/me/password")
async def change_own_password(
    body: ChangePasswordBody,
    request: Request,
    principal: Principal = Depends(require_authenticated),
) -> dict:
    """Change the signed-in user's own password. Every role may do this.

    The current password is required even though the caller already holds a
    valid token: a token is a session, and a walked-away-from session must not
    be enough to lock the real owner out of their own account.
    """
    from ..services import keycloak_admin as kc

    if principal.kind != "user":
        # dev bypass / internal service key — there is no Keycloak account behind
        # the request, so there is nothing to change.
        raise HTTPException(
            400,
            detail="No user account is signed in (dev bypass or service key) — "
                   "nothing to change a password for",
        )
    if body.new_password == body.current_password:
        raise HTTPException(422, detail="The new password must differ from the current one")

    if not await kc.verify_password(principal.subject, body.current_password):
        raise HTTPException(403, detail="Current password is incorrect")

    user = await kc.get_user_by_username(principal.subject)
    await kc.set_password(user["id"], body.new_password, temporary=False)
    log.info("auth.password_changed", user=principal.subject)
    await audit_svc.record(request, principal, "auth.password_changed", target=principal.subject)
    return {"status": "ok"}
