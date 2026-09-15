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
    Peripheral, PeripheralIn, PeripheralOut, PeripheralPatch, SitemapHaPlacement,
    generate_slug,
)
from ..security import Principal
from ..services import audit as audit_svc
from ..services.policy import require_capability

router = APIRouter(prefix="/peripherals", tags=["peripherals"])

_MANAGE = require_capability("peripheral_manage")


def _out(row: Peripheral, placements: int = 0) -> PeripheralOut:
    return PeripheralOut(
        id=row.slug, name=row.name, category=row.category, location=row.location,
        vendor=row.vendor, external_id=row.external_id, notes=row.notes,
        enabled=row.enabled, last_state=row.last_state, last_seen=row.last_seen,
        placements=placements,
    )


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
    return [_out(r, counts.get(r.id, 0)) for r in rows]


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
    return _out(row, counts.get(row.id, 0))


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
