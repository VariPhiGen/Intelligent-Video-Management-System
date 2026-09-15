"""motion.py — authenticated reverse-proxy to the motion detection service.

The motion service (services/motion) has no auth of its own and binds to
127.0.0.1. Users reach it exclusively through this proxy, keeping one login:

    /api/motion/<path>  →  <MOTION_API_URL>/<path>

RBAC: reads (camera states, events, health) for any authenticated principal;
mutations (add/remove/reset — normally the registry sync's job) require the
``admin`` role.
"""
from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status

import re

from ..config import settings
from ..security import get_principal
from ..services import audit as audit_svc
from ..services import policy as policy_svc

log = structlog.get_logger(__name__)
router = APIRouter(tags=["motion"])

# Alarm acknowledgement (re-arm) is policy-gated: roles with 'motion_ack' may
# reset a TRIGGERED camera; every other mutation stays admin-only.
_RESET_PATH = re.compile(r"^cameras/[^/]+/reset/?$")

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_DROP_REQ = {"host", "content-length", "connection", "cookie", "authorization"}
_KEEP_RESP = {"content-type", "content-length"}


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request) -> Response:
    principal = await get_principal(request)
    if request.method != "GET":
        if request.method == "POST" and _RESET_PATH.match(path):
            await policy_svc.require(principal, "motion_ack")
        elif not principal.has_any(frozenset({"admin"})):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires role: admin",
            )

    url = f"{settings.motion_api_url.rstrip('/')}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            upstream = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
    except httpx.RequestError as exc:
        log.warning("motion.proxy.unreachable", path=path, error=str(exc))
        return Response(
            content=b'{"detail":"Motion service is unreachable"}',
            status_code=502,
            media_type="application/json",
        )

    # Audit a successful alarm acknowledgement (re-arm of a triggered camera).
    if (
        request.method == "POST"
        and _RESET_PATH.match(path)
        and upstream.status_code < 400
    ):
        camera_id = path.split("/")[1] if "/" in path else None
        await audit_svc.record(
            request, principal, "alarm.acknowledged", target=camera_id,
        )

    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() in _KEEP_RESP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )
