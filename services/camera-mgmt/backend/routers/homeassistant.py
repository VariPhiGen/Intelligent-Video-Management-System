# services/camera-mgmt/backend/routers/homeassistant.py
"""Home Assistant — connect the VMS to it, and bring its devices in.

VMS OWNS THE RULES, HA ONLY EXECUTES. Nothing here creates anything inside HA;
it stores where HA is, asks it what it can drive, and creates ordinary VMS
peripherals bound to those entities. From then on a rule acts on them through
Bridge.actuate like any other device.

Browsers never talk to HA. The token never leaves this process in a response,
a log line or an audit row — the panel learns only whether one is set.

  GET    /ha/connection        admin    where HA is, and whether it answers
  PUT    /ha/connection        admin    save url + token (tested first)
  POST   /ha/connection/test   admin    test without saving
  DELETE /ha/connection        admin    forget the saved one (env still applies)
  GET    /ha/entities          peripheral_manage   what HA can drive here
  POST   /ha/import            peripheral_manage   make peripherals of them

The connection is admin-only, not `peripheral_manage`: a long-lived HA token
is owner-level access to the whole of HA, a wider key than this inventory.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db import get_db
from ..models import (
    HaConnectionIn, HaConnectionOut, HaImportIn, Peripheral,
    PeripheralBinding, generate_slug,
)
from ..security import Principal, require_role
from ..services import audit as audit_svc
from ..services.bridge import apply_ha_config, get_bridge, ha_settings
from ..services.bridge.homeassistant import HaDriver
from ..services.policy import require_capability

router = APIRouter(prefix="/ha", tags=["homeassistant"])

_ADMIN = require_role("admin")
_MANAGE = require_capability("peripheral_manage")

#: An imported entity's category, from its HA domain. The operator can say
#: otherwise per item; this is only the default the import dialog shows.
DEFAULT_CATEGORY = {
    "light": "Lighting",
    "switch": "Access control",   # a relay channel: gate, barrier, maglock
    "lock": "Access control",
    "cover": "Access control",
    "siren": "Audio · Sensors",
    "media_player": "Audio · Sensors",
    "scene": "Automation",
    "script": "Automation",
    "button": "Automation",
    "fan": "Climate · Utilities",
    "valve": "Climate · Utilities",
}


def _driver() -> Optional[HaDriver]:
    return get_bridge()._drivers.get("homeassistant")


async def _report(cfg, driver: Optional[HaDriver]) -> HaConnectionOut:
    state = await driver.health() if driver is not None else "not_configured"
    return HaConnectionOut(
        source=cfg.source, url=cfg.url or None, token_set=bool(cfg.token),
        state=state, error=driver.last_error if driver is not None else None,
        updated_at=cfg.updated_at, updated_by=cfg.updated_by)


async def _try(url: str, token: Optional[str]) -> tuple[str, Optional[str]]:
    """Probe a candidate connection with a throwaway driver."""
    probe = HaDriver(url=url, token=token)
    try:
        state = await probe.probe()
        return state, probe.last_error
    finally:
        await probe.stop()


# ── The connection ───────────────────────────────────────────────────────────

@router.get("/connection")
async def get_connection(principal: Principal = Depends(_ADMIN),
                         db=Depends(get_db)) -> HaConnectionOut:
    cfg = await apply_ha_config(db)
    return await _report(cfg, _driver())


@router.post("/connection/test")
async def test_connection(body: Optional[HaConnectionIn] = None,
                          principal: Principal = Depends(_ADMIN),
                          db=Depends(get_db)) -> dict:
    """Test a URL and token without saving them.

    With no body, tests what is live. With a body and no token, tests the new
    URL with the stored token — the same rule as saving.
    """
    cfg = await ha_settings.load(db)
    url = body.url if body is not None else cfg.url
    token = body.token if body is not None and body.token else cfg.token
    if not url or not token:
        return {"state": "not_configured", "error": None}
    state, error = await _try(url, token)
    return {"state": state, "error": error}


@router.put("/connection")
async def put_connection(body: HaConnectionIn, request: Request,
                         principal: Principal = Depends(_ADMIN),
                         db=Depends(get_db)) -> HaConnectionOut:
    """Save the connection, after testing it.

    A token HA REJECTS is not saved: replacing a working token with a wrong
    one would take every HA device offline on a typo. An HA that is merely
    UNREACHABLE is saved — it may simply not be up yet — and the answer says
    offline/unreachable.
    """
    cfg = await ha_settings.load(db)
    token = body.token or (cfg.token if cfg.source == "ui" else None)
    if not token:
        raise HTTPException(422, detail="A token is required")
    state, error = await _try(body.url, token)
    if error == "unauthorized":
        raise HTTPException(
            400, detail="Home Assistant rejected this token; nothing was saved")
    await ha_settings.save(db, url=body.url, token=body.token, by=principal.subject,
                           now=datetime.now(timezone.utc))
    cfg = await apply_ha_config(db)
    await audit_svc.record(
        request, principal, "ha.connection_set", target="integration/homeassistant",
        # The URL and whether the token changed. Never the token.
        detail={"url": body.url, "token_changed": body.token is not None,
                "state": state, "error": error})
    return await _report(cfg, _driver())


@router.delete("/connection")
async def delete_connection(request: Request,
                            principal: Principal = Depends(_ADMIN),
                            db=Depends(get_db)) -> HaConnectionOut:
    """Forget the saved connection. HA_URL / HA_TOKEN, if set, apply again."""
    if not await ha_settings.clear(db):
        raise HTTPException(404, detail="No saved Home Assistant connection")
    cfg = await apply_ha_config(db)
    await audit_svc.record(request, principal, "ha.connection_cleared",
                           target="integration/homeassistant")
    return await _report(cfg, _driver())


# ── Its devices ──────────────────────────────────────────────────────────────

async def _bound(db) -> dict[str, str]:
    """entity_id -> peripheral slug, for every HA binding."""
    rows = (await db.execute(
        select(PeripheralBinding.address, Peripheral.slug)
        .join(Peripheral, Peripheral.id == PeripheralBinding.peripheral_id)
        .where(PeripheralBinding.driver == "homeassistant"))).all()
    return {address: slug for address, slug in rows}


async def _entities_or_503(db) -> list:
    await apply_ha_config(db)
    driver = _driver()
    found = await driver.entities() if driver is not None else None
    if found is None:
        state = await driver.health() if driver is not None else "not_configured"
        raise HTTPException(
            503, detail=f"Home Assistant is {state}"
            + (f" ({driver.last_error})" if driver and driver.last_error else ""))
    return found


@router.get("/entities")
async def list_entities(principal: Principal = Depends(_MANAGE),
                        db=Depends(get_db)) -> list[dict]:
    """What HA can drive, with the category an import would default to and the
    peripheral already bound to it, if any."""
    found = await _entities_or_503(db)
    bound = await _bound(db)
    return [{**e, "category": DEFAULT_CATEGORY.get(e["domain"]),
             "bound_to": bound.get(e["entity_id"])} for e in found]


@router.post("/import")
async def import_entities(body: HaImportIn, request: Request,
                          principal: Principal = Depends(_MANAGE),
                          db=Depends(get_db)) -> dict:
    """Make a peripheral, bound to the entity, for each item.

    Per item, never all-or-nothing: one name clash must not cost the operator
    the other seven relays. Each item answers `imported` (with its slug) or a
    stable reason — not_found, already_bound, name_taken.

    The new bindings are verified before this returns, so the devices can be
    used at once instead of after the next verify tick (300 s by default).
    """
    found = {e["entity_id"]: e for e in await _entities_or_503(db)}
    bound = await _bound(db)
    now = datetime.now(timezone.utc)
    results = []
    created = 0
    for item in body.items:
        entity = found.get(item.entity_id)
        if entity is None:
            results.append({"entity_id": item.entity_id, "result": "not_found"})
            continue
        if item.entity_id in bound:
            results.append({"entity_id": item.entity_id, "result": "already_bound",
                            "peripheral": bound[item.entity_id]})
            continue
        name = (item.name or entity["name"]).strip()[:120]
        dup = (await db.execute(select(Peripheral.id).where(
            func.lower(Peripheral.name) == name.lower()))).first()
        if dup is not None:
            results.append({"entity_id": item.entity_id, "result": "name_taken",
                            "name": name})
            continue
        row = Peripheral(
            slug=generate_slug(name), name=name,
            category=item.category or DEFAULT_CATEGORY.get(entity["domain"], "Access control"),
            location=item.location, vendor="Home Assistant",
            external_id=item.entity_id, enabled=True,
            created_at=now, updated_at=now, created_by=principal.subject)
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            results.append({"entity_id": item.entity_id, "result": "name_taken",
                            "name": name})
            continue
        db.add(PeripheralBinding(
            peripheral_id=row.id, driver="homeassistant", address=item.entity_id,
            config={}, state_topic=None, created_at=now, updated_at=now))
        await db.commit()
        bound[item.entity_id] = row.slug
        created += 1
        results.append({"entity_id": item.entity_id, "result": "imported",
                        "peripheral": row.slug})
        await audit_svc.record(
            request, principal, "peripheral.created", target=f"peripheral/{row.slug}",
            detail={"name": name, "category": row.category, "source": "homeassistant",
                    "entity_id": item.entity_id})
    if created:
        await get_bridge().verify_bindings(db, now=now)
    return {"imported": created, "results": results}
