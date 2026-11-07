"""Administration → DeepStream: the detector models the analytics pipeline holds.

A thin, admin-only proxy onto the pipeline's control API on :8081
(``GET /model``, ``POST /model/add``, ``POST /model/restart``). The browser
never sees the pipeline or its ``X-Internal-Key``; this router adds the key
(``deepstream_client._headers``) and relays the pipeline's answer — status and
body — so its validation messages reach the administrator verbatim.

RESTARTING THE PIPELINE INTERRUPTS EVERY CAMERA ON IT, including hand-authored
ones the VMS does not own. Both the upload (``restart=true``) and the explicit
restart are therefore audited, and the screen says so before the button.

WHERE THE PIPELINE IS lives under ``/deepstream/settings``: an address saved
here (table ``deepstream_settings``) wins over ``DEEPSTREAM_API_URL``, and the
camera sync loop follows it too — one pipeline, one address. Every route below
reloads it first, since the save may have landed on another api worker.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import DeepstreamSettings
from ..security import Principal, require_role
from ..services import audit as audit_svc
from ..services import deepstream_client as ds

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/deepstream", tags=["deepstream"])

_ADMIN = require_role("admin")

# The name becomes a directory/config name on the pipeline side.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _relay(resp: httpx.Response) -> Any:
    """Pass a pipeline answer through: JSON body on 2xx, its text as the error otherwise."""
    try:
        body: Any = resp.json()
    except ValueError:
        body = {"detail": resp.text[:2000]}
    if resp.status_code >= 400:
        detail = ((body.get("error") or body.get("detail") or body.get("message"))
                  if isinstance(body, dict) else None)
        raise HTTPException(resp.status_code if resp.status_code < 500 else 502,
                            f"DeepStream refused ({resp.status_code}): {detail or resp.text[:500] or 'no detail'}")
    return body


def _unreachable(exc: ds.PipelineUnreachable) -> HTTPException:
    return HTTPException(502, f"DeepStream pipeline at {ds._base()} is unreachable: {exc}")


# ── Where the pipeline is ────────────────────────────────────────────────────

class UrlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Blank / null = go back to DEEPSTREAM_API_URL.
    api_url: Optional[str] = Field(default=None, max_length=500)


def _clean_url(raw: Optional[str]) -> Optional[str]:
    url = (raw or "").strip().rstrip("/")
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(422, "Pipeline address must be an http:// or https:// URL, e.g. http://127.0.0.1:8081")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise HTTPException(422, "Pipeline address takes no query, fragment or credentials.")
    try:
        parts.port   # raises on a non-numeric / out-of-range port
    except ValueError:
        raise HTTPException(422, "Pipeline address has an invalid port.")
    return url


async def _settings_view(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(DeepstreamSettings, 1)
    effective = await ds.load_url(db)
    return {
        "api_url": row.api_url if row else None,
        "effective_url": effective,
        "default_url": ds.default_url(),
        "updated_by": row.updated_by if row else None,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db),
                       principal: Principal = Depends(_ADMIN)) -> dict[str, Any]:
    return await _settings_view(db)


@router.put("/settings")
async def save_settings(body: UrlBody, request: Request, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(_ADMIN)) -> dict[str, Any]:
    url = _clean_url(body.api_url)
    row = await db.get(DeepstreamSettings, 1)
    if row is None:
        row = DeepstreamSettings(id=1)
        db.add(row)
    before = row.api_url
    row.api_url = url
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = principal.subject
    await db.commit()
    ds.use_url(url)
    await audit_svc.record(request, principal, "deepstream.settings_updated",
                           target="deepstream", detail={"api_url": url, "previous": before})
    log.info("deepstream.settings_updated", api_url=url or ds.default_url(), by=principal.subject)
    return await _settings_view(db)


@router.post("/settings/test")
async def test_settings(body: UrlBody, db: AsyncSession = Depends(get_db),
                        principal: Principal = Depends(_ADMIN)) -> dict[str, Any]:
    """Can this address be reached? Nothing is saved. Blank tests the default."""
    url = _clean_url(body.api_url) or ds.default_url()
    try:
        resp = await ds.probe(url)
    except ds.PipelineUnreachable as exc:
        raise HTTPException(502, f"No answer from {url}: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(502, f"{url}/health answered {resp.status_code}: {resp.text[:300]}")
    return {"ok": True, "url": url, "health": _relay(resp)}


# ── Models ───────────────────────────────────────────────────────────────────

@router.get("/models")
async def list_models(db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(_ADMIN)) -> Any:
    await ds.load_url(db)
    try:
        return _relay(await ds.list_models())
    except ds.PipelineUnreachable as exc:
        raise _unreachable(exc)


@router.post("/models")
async def add_model(
    request: Request,
    name: str = Form(...),
    onnx: UploadFile = File(...),
    labels: UploadFile = File(...),
    batch_size: int = Form(1, ge=1, le=64),
    network_size: Optional[int] = Form(None, ge=32, le=4096),
    restart: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(_ADMIN),
) -> Any:
    name = name.strip()
    if not _NAME.match(name):
        raise HTTPException(422, "Model name: letters, digits, '_', '-' or '.', up to 64 characters.")
    if not (onnx.filename or "").lower().endswith(".onnx"):
        raise HTTPException(422, "The model file must be an .onnx export.")
    if network_size is not None and network_size % 32:
        raise HTTPException(422, "Network size must be a multiple of 32.")
    await ds.load_url(db)
    try:
        resp = await ds.add_model(
            name=name, onnx=(onnx.filename or "model.onnx", onnx.file),
            labels=(labels.filename or "labels.txt", labels.file),
            batch_size=batch_size, network_size=network_size, restart=restart)
    except ds.PipelineUnreachable as exc:
        raise _unreachable(exc)
    body = _relay(resp)
    await audit_svc.record(
        request, principal, "deepstream.model_added", target=f"deepstream-model:{name}",
        detail={"onnx": onnx.filename, "labels": labels.filename, "batch_size": batch_size,
                "network_size": network_size, "restart": restart})
    log.info("deepstream.model_added", name=name, restart=restart, by=principal.subject)
    return body


@router.post("/models/restart")
async def restart(request: Request, db: AsyncSession = Depends(get_db),
                  principal: Principal = Depends(_ADMIN)) -> Any:
    await ds.load_url(db)
    try:
        body = _relay(await ds.restart_pipeline())
    except ds.PipelineUnreachable as exc:
        raise _unreachable(exc)
    await audit_svc.record(request, principal, "deepstream.restarted",
                           target="deepstream", detail={})
    log.info("deepstream.restart_requested", by=principal.subject)
    return body
