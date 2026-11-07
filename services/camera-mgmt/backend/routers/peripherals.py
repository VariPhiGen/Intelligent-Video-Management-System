# services/camera-mgmt/backend/routers/peripherals.py
"""Peripherals — the operator's real device inventory (migration 029).

Replaces the hardcoded array the SPA used to ship (peripheralsData.ts), so the
Map tab's HA pins reference rows with a foreign key instead of demo ids.

Scope is deliberate: this endpoint stores what a person can know about a
device — what it is, where it is, and the address a bridge will bind to. It
does NOT accept `last_state` or `last_seen`. Those belong to a Home-Assistant
bridge that does not exist yet, and a hand-typed status shown next to a live
camera wall would be read as fact. Until an integration writes them the wall
says "state unknown", which is true.

Reads are open to any authenticated principal (the page is gated by the
ui-only `peripherals` capability); writes need the grantable
`peripheral_manage` capability and are audited, same shape as sitemaps.py.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..db import get_db
from ..models import (
    BindingIn, BindingOut, CommandIn, Peripheral, PeripheralBinding,
    PeripheralIn, PeripheralOut, PeripheralPatch, SitemapHaPlacement,
    generate_slug,
)
from ..security import Principal
from .. import crypto
from ..services import audit as audit_svc
from ..services.bridge import FollowUpOwed, get_bridge, unbind
from ..services.policy import require_capability

router = APIRouter(prefix="/peripherals", tags=["peripherals"])

_MANAGE = require_capability("peripheral_manage")
# NOT the same authority as _MANAGE, deliberately: curating an
# inventory and unlocking a door are different things, and one
# capability for both would hand every inventory editor a key to the
# building.
_CONTROL = require_capability("peripheral_control")


def _out(row: Peripheral, placements: int = 0,
         binding_verified: bool = False,
         binding: "PeripheralBinding | None" = None) -> PeripheralOut:
    return PeripheralOut(
        id=row.slug, name=row.name, category=row.category, location=row.location,
        vendor=row.vendor, external_id=row.external_id, notes=row.notes,
        enabled=row.enabled, last_state=row.last_state, last_seen=row.last_seen,
        placements=placements, binding_verified=binding_verified,
        binding=BindingOut.model_validate(binding) if binding is not None else None,
    )


async def _bindings(db) -> dict:
    """Every binding, by peripheral id.

    One query for the whole list rather than one per tile: the wall renders
    every device at once and a control that cannot be offered has to say so —
    and say WHY — without a round trip per card.
    """
    rows = (await db.execute(select(PeripheralBinding))).scalars().all()
    return {b.peripheral_id: b for b in rows}


async def _placement_counts(db) -> dict[int, int]:
    rows = (await db.execute(
        select(SitemapHaPlacement.peripheral_id, func.count())
        .group_by(SitemapHaPlacement.peripheral_id)
    )).all()
    return {pid: n for pid, n in rows}


async def _get_or_404(db, slug: str) -> Peripheral:
    row = (await db.execute(
        select(Peripheral).where(Peripheral.slug == slug)
    )).scalars().first()
    if row is None:
        raise HTTPException(404, f"Peripheral '{slug}' not found")
    return row


async def _reject_duplicate_name(db, name: str, exclude_id: int | None = None) -> None:
    """Case-insensitive name pre-check for the friendly message; the unique
    index ix_peripherals_name_lower is the real guard (cameras.py pattern)."""
    q = select(Peripheral).where(func.lower(Peripheral.name) == name.strip().lower())
    if exclude_id is not None:
        q = q.where(Peripheral.id != exclude_id)
    dup = (await db.execute(q)).scalars().first()
    if dup:
        raise HTTPException(409, detail=f"A peripheral named '{dup.name}' already exists")


@router.get("")
async def list_peripherals(db=Depends(get_db)) -> list[PeripheralOut]:
    rows = (await db.execute(
        select(Peripheral).order_by(Peripheral.category.asc(), Peripheral.name.asc())
    )).scalars().all()
    counts = await _placement_counts(db)
    bindings = await _bindings(db)
    return [_out(r, counts.get(r.id, 0),
                 bindings.get(r.id) is not None
                 and bindings[r.id].verified_at is not None,
                 bindings.get(r.id))
            for r in rows]


@router.post("", status_code=201)
async def create_peripheral(
    body: PeripheralIn,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> PeripheralOut:
    await _reject_duplicate_name(db, body.name)
    now = datetime.now(timezone.utc)
    row = Peripheral(
        slug=generate_slug(body.name), name=body.name, category=body.category,
        location=body.location, vendor=body.vendor, external_id=body.external_id,
        notes=body.notes, enabled=body.enabled,
        created_at=now, updated_at=now, created_by=principal.subject,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail=f"A peripheral named '{body.name}' already exists")
    await db.refresh(row)
    await audit_svc.record(
        request, principal, "peripheral.created", target=f"peripheral/{row.slug}",
        detail={"name": row.name, "category": row.category},
    )
    return _out(row)


@router.patch("/{slug}")
async def update_peripheral(
    slug: str,
    body: PeripheralPatch,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> PeripheralOut:
    row = await _get_or_404(db, slug)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"]:
        await _reject_duplicate_name(db, fields["name"], exclude_id=row.id)
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail="A peripheral with that name already exists")
    await db.refresh(row)
    await audit_svc.record(
        request, principal, "peripheral.updated", target=f"peripheral/{row.slug}",
        detail={"changed": sorted(fields.keys())},
    )
    counts = await _placement_counts(db)
    bindings = await _bindings(db)
    b = bindings.get(row.id)
    return _out(row, counts.get(row.id, 0),
                b is not None and b.verified_at is not None, b)


@router.delete("/{slug}")
async def delete_peripheral(
    slug: str,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> dict[str, object]:
    """Deleting takes its map pins with it — that's the FK's ON DELETE CASCADE,
    not an application sweep, so it holds even for a direct SQL delete."""
    row = await _get_or_404(db, slug)
    pins = (await db.execute(
        select(func.count()).select_from(SitemapHaPlacement)
        .where(SitemapHaPlacement.peripheral_id == row.id)
    )).scalar_one()
    name = row.name
    await db.delete(row)
    await db.commit()
    await audit_svc.record(
        request, principal, "peripheral.deleted", target=f"peripheral/{slug}",
        detail={"name": name, "pins_removed": int(pins)},
    )
    return {"deleted": slug, "pins_removed": int(pins)}


# ── The bridge (P2) ──────────────────────────────────────────────────────────
# DECLARED BEFORE nothing in particular, but note the path: /bridge/health sits
# under the same prefix as /{slug}. There is no GET /{slug} route, so there is
# no ambiguity today — and a test pins it, because adding one later would
# silently turn this into a lookup for a peripheral named "bridge".


@router.get("/bridge/health")
async def bridge_health(db=Depends(get_db)) -> dict:
    """Any authenticated principal may see whether the bridge is up.

    The device wall shows it, and hiding it from a viewer would make the wall
    lie by omission — an operator seeing "unknown" on every tile deserves to
    know whether that is the devices or the bridge.
    """
    return await get_bridge().health(db)


@router.put("/{slug}/binding")
async def put_binding(slug: str, body: BindingIn, request: Request,
                      principal: Principal = Depends(_MANAGE),
                      db=Depends(get_db)) -> BindingOut:
    """Say where a device lives. Curating the inventory covers this; it does
    NOT cover operating the device, which is `peripheral_control` below."""
    row = await _get_or_404(db, slug)
    now = datetime.now(timezone.utc)
    existing = (await db.execute(
        select(PeripheralBinding)
        .where(PeripheralBinding.peripheral_id == row.id)
    )).scalars().first()

    if existing is None:
        existing = PeripheralBinding(peripheral_id=row.id, created_at=now,
                                     updated_at=now)
        db.add(existing)
    existing.driver = body.driver
    existing.address = body.address
    existing.config = body.config
    existing.state_topic = body.state_topic
    if body.secret is not None:
        # Fernet, the same path camera credentials already use. There is no
        # plaintext column to write instead by mistake.
        existing.secret_enc = crypto.encrypt(body.secret) or None
    # REPLACING A BINDING INVALIDATES ITS VERIFICATION. A new address has never
    # been confirmed to resolve and must not inherit the old one's trust —
    # otherwise a typo'd entity id actuates once on the strength of the address
    # it replaced.
    existing.verified_at = None
    existing.verify_error = None
    existing.updated_at = now
    await db.commit()
    await db.refresh(existing)

    await audit_svc.record(request, principal, "peripheral.binding_set",
                           target=f"peripheral/{slug}",
                           # The driver and address, never the secret.
                           detail={"driver": body.driver, "address": body.address})
    return BindingOut.model_validate(existing)


@router.delete("/{slug}/binding")
async def delete_binding(slug: str, request: Request,
                         principal: Principal = Depends(_MANAGE),
                         db=Depends(get_db)) -> dict[str, object]:
    """Forget where a device lives. The peripheral and its map pins stay;
    from here on the wall says "unknown" and a rule naming it is refused.

    409 while a pulse still owes this device its `off`: the address is the
    only way that second half can be delivered, and taking it away mid-pulse
    would leave a siren sounding with a critical alert as the only record.
    """
    row = await _get_or_404(db, slug)
    try:
        removed = await unbind(db, row.id)
    except FollowUpOwed as exc:
        raise HTTPException(
            409, detail=f"{exc.owed} follow-up(s) still owed to this device; "
                        "wait for the pulse to finish")
    if not removed:
        raise HTTPException(404, detail="Peripheral has no binding")
    await audit_svc.record(request, principal, "peripheral.binding_removed",
                           target=f"peripheral/{slug}")
    return {"unbound": slug}


@router.post("/{slug}/command")
async def send_command(slug: str, body: CommandIn, request: Request,
                       principal: Principal = Depends(_CONTROL),
                       db=Depends(get_db)) -> dict:
    """Operate a device by hand.

    AUDITED WHETHER IT WORKED OR NOT. An operator pressing a button on a dead
    bridge must leave a trace, or "I pressed it and nothing happened" is
    unanswerable afterwards.

    503 rather than 200-with-a-flag on refusal: the caller asked for a device
    to move and it did not, and a UI that has to read a field to discover that
    is a UI that will forget to.
    """
    await _get_or_404(db, slug)
    result = await get_bridge().actuate(db, slug, body.command, body.params)
    await audit_svc.record(
        request, principal, "peripheral.actuated", target=f"peripheral/{slug}",
        detail={"command": body.command, "source": "manual",
                "detail": result.detail},
        outcome="success" if result.ok else "failure")
    if not result.ok:
        raise HTTPException(503, detail=result.detail)
    return {"ok": True, "detail": result.detail}
