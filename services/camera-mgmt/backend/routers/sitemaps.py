# services/camera-mgmt/backend/routers/sitemaps.py
"""Sitemaps — floor-plan images for the Map tab.

Reads are open to any authenticated principal (the Map page is gated by the
ui-only map_view capability); mutations require the grantable camera_manage
capability and are audited. Camera placement lives in camera_metadata JSONB
({"sitemap": {"id", "x", "y"}}), so DELETE sweeps that key from affected
cameras in the same transaction — there is no FK to gate it.
"""
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..db import get_db
from ..models import (
    CalibrationBody, Camera, HaPlacementsBody, Peripheral, Sitemap,
    SitemapHaPlacement, SitemapMeta, SITEMAP_MAX_BYTES, image_dimensions,
    sniff_image_content_type, validate_sitemap_dimensions,
)
from ..security import Principal
from ..services import audit as audit_svc
from ..services.policy import require_capability

router = APIRouter(prefix="/sitemaps", tags=["sitemaps"])

_MANAGE = require_capability("camera_manage")


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _map_counts(rows: list[Camera]) -> dict[int, tuple[int, int]]:
    """(placed, assigned) per sitemap id.

    A camera can be assigned to a map without coordinates — that's what the
    Map tab's unplaced tray holds. Those are *assigned*, not *placed*, and the
    UI needs both: the site-plan picker counts dots you can see on the plan,
    while the delete confirmation counts every camera the delete will unassign.
    """
    counts: dict[int, list[int]] = {}
    for c in rows:
        ref = (c.camera_metadata or {}).get("sitemap") or {}
        sid = ref.get("id")
        if not isinstance(sid, int):
            continue
        e = counts.setdefault(sid, [0, 0])
        e[1] += 1
        if isinstance(ref.get("x"), (int, float)) and isinstance(ref.get("y"), (int, float)):
            e[0] += 1
    return {k: (v[0], v[1]) for k, v in counts.items()}


async def _get_or_404(db, sitemap_id: int) -> Sitemap:
    row = (
        await db.execute(select(Sitemap).where(Sitemap.id == sitemap_id))
    ).scalars().first()
    if row is None:
        raise HTTPException(404, f"Sitemap {sitemap_id} not found")
    return row


def _meta(
    s: Sitemap, counts: tuple[int, int], ha: list[dict] | None = None,
) -> SitemapMeta:
    placed, assigned = counts
    return SitemapMeta(
        id=s.id, name=s.name, content_type=s.content_type,
        created_at=s.created_at, cameras_placed=placed, cameras_assigned=assigned,
        calibration=s.calibration or None,
        ha_devices=ha or None,
    )


async def _ha_pins(db, sitemap_ids: list[int]) -> dict[int, list[dict]]:
    """Pins per sitemap, keyed by the peripheral SLUG rather than its row id.

    The slug is what the SPA and the map's stored placements speak, and it
    survives a re-created row; the numeric id never leaves the database."""
    if not sitemap_ids:
        return {}
    rows = (await db.execute(
        select(SitemapHaPlacement.sitemap_id, Peripheral.slug,
               SitemapHaPlacement.x, SitemapHaPlacement.y)
        .join(Peripheral, Peripheral.id == SitemapHaPlacement.peripheral_id)
        .where(SitemapHaPlacement.sitemap_id.in_(sitemap_ids))
        .order_by(Peripheral.name.asc())
    )).all()
    out: dict[int, list[dict]] = {}
    for sid, slug, x, y in rows:
        out.setdefault(sid, []).append({"id": slug, "x": x, "y": y})
    return out


@router.get("")
async def list_sitemaps(db=Depends(get_db)) -> list[SitemapMeta]:
    maps = (
        await db.execute(select(Sitemap).order_by(Sitemap.name.asc()))
    ).scalars().all()
    cams = (await db.execute(select(Camera))).scalars().all()
    counts = _map_counts(cams)
    pins = await _ha_pins(db, [s.id for s in maps])
    return [_meta(s, counts.get(s.id, (0, 0)), pins.get(s.id)) for s in maps]


@router.post("", status_code=201)
async def upload_sitemap(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=120),
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> SitemapMeta:
    data = await file.read()
    if len(data) > SITEMAP_MAX_BYTES:
        raise HTTPException(
            422, detail=f"Sitemap image exceeds {SITEMAP_MAX_BYTES // (1024*1024)} MB",
        )
    content_type = sniff_image_content_type(data)
    if content_type is None:
        raise HTTPException(
            422, detail="Unsupported image — upload PNG, JPEG, WebP or SVG",
        )
    # Dimensions, read from the header rather than by decoding. The byte cap
    # above bounds what we store; this bounds what every operator's browser has
    # to decode, and rejects plans too small or too elongated to place pins on.
    # SVG yields None (no pixel dimensions) and is exempt.
    dims = image_dimensions(data, content_type)
    if dims is not None:
        problem = validate_sitemap_dimensions(*dims)
        if problem:
            raise HTTPException(422, detail=problem)
    dup = (
        await db.execute(
            select(Sitemap).where(func.lower(Sitemap.name) == name.strip().lower())
        )
    ).scalars().first()
    if dup:
        raise HTTPException(409, detail=f"A sitemap named '{dup.name}' already exists")
    row = Sitemap(
        name=name.strip(), content_type=content_type, image=data,
        created_at=datetime.now(timezone.utc), uploaded_by=principal.subject,
    )
    db.add(row)
    # The pre-check above gives the friendly message; ix_sitemaps_name_lower
    # (unique on lower(name)) is the real guard, so a concurrent same-name
    # upload surfaces here as IntegrityError, not a 500 (cameras.py pattern).
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, detail=f"A sitemap named '{name.strip()}' already exists"
        )
    await db.refresh(row)
    await audit_svc.record(
        request, principal, "sitemap.created",
        target=f"sitemap/{row.id}",
        detail={"name": row.name, "content_type": content_type, "bytes": len(data)},
    )
    return _meta(row, (0, 0))


@router.get("/{sitemap_id}/image")
async def sitemap_image(sitemap_id: int, request: Request, db=Depends(get_db)) -> Response:
    row = await _get_or_404(db, sitemap_id)
    etag = f'"{hashlib.sha256(row.image).hexdigest()[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return Response(
        content=row.image,
        media_type=row.content_type,
        headers={"ETag": etag, "Cache-Control": "private, max-age=3600"},
    )


@router.patch("/{sitemap_id}")
async def rename_sitemap(
    sitemap_id: int,
    body: RenameBody,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> SitemapMeta:
    row = await _get_or_404(db, sitemap_id)
    new_name = body.name.strip()
    # Case-insensitive duplicate pre-check, excluding the row being renamed so
    # a case-only rename of the same map (e.g. "lobby" -> "Lobby") still works.
    dup = (
        await db.execute(
            select(Sitemap).where(
                func.lower(Sitemap.name) == new_name.lower(),
                Sitemap.id != sitemap_id,
            )
        )
    ).scalars().first()
    if dup:
        raise HTTPException(409, detail=f"A sitemap named '{dup.name}' already exists")
    old = row.name
    row.name = new_name
    # Backstop for the ix_sitemaps_name_lower unique index racing the pre-check
    # (cameras.py pattern): rollback and 409 instead of an unhandled 500.
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, detail=f"A sitemap named '{new_name}' already exists"
        )
    await db.refresh(row)
    await audit_svc.record(
        request, principal, "sitemap.renamed",
        target=f"sitemap/{row.id}", detail={"from": old, "to": row.name},
    )
    cams = (await db.execute(select(Camera))).scalars().all()
    pins = await _ha_pins(db, [row.id])
    return _meta(row, _map_counts(cams).get(row.id, (0, 0)), pins.get(row.id))


@router.patch("/{sitemap_id}/calibration")
async def set_calibration(
    sitemap_id: int,
    body: CalibrationBody,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> SitemapMeta:
    """Anchor the plan to the Earth with 2–3 control points, or clear it with
    an empty list. Once set, cameras carrying GPS auto-place on this map."""
    row = await _get_or_404(db, sitemap_id)
    points = [p.model_dump() for p in body.points]
    row.calibration = points or None
    await db.commit()
    await db.refresh(row)
    await audit_svc.record(
        request, principal,
        "sitemap.georeferenced" if points else "sitemap.calibration_cleared",
        target=f"sitemap/{row.id}", detail={"points": len(points)},
    )
    cams = (await db.execute(select(Camera))).scalars().all()
    pins = await _ha_pins(db, [row.id])
    return _meta(row, _map_counts(cams).get(row.id, (0, 0)), pins.get(row.id))


@router.put("/{sitemap_id}/ha-devices")
async def set_ha_devices(
    sitemap_id: int,
    body: HaPlacementsBody,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> SitemapMeta:
    """Replace the HA peripherals pinned to this plan (empty list clears them).

    Placement is per-map and lives here rather than on the device because
    peripherals have no store of their own yet — see migration 028."""
    row = await _get_or_404(db, sitemap_id)
    placements = [p.model_dump() for p in body.devices]
    # Resolve slugs to rows first: an id the inventory doesn't know is a 422,
    # not a silently-dropped pin. This is the check the JSONB column could
    # never make (migration 029 replaced it with a real foreign key).
    ids_by_slug: dict[str, int] = {}
    if placements:
        found = (await db.execute(
            select(Peripheral.slug, Peripheral.id)
            .where(Peripheral.slug.in_([p["id"] for p in placements]))
        )).all()
        ids_by_slug = {slug: pid for slug, pid in found}
        missing = [p["id"] for p in placements if p["id"] not in ids_by_slug]
        if missing:
            raise HTTPException(
                422, detail=f"Unknown peripheral(s): {', '.join(sorted(missing))}",
            )
    before = (await db.execute(
        select(func.count()).select_from(SitemapHaPlacement)
        .where(SitemapHaPlacement.sitemap_id == sitemap_id)
    )).scalar_one()
    # Replace wholesale in one transaction — the endpoint's contract is the
    # full list, so a partial apply would leave the plan in a state no client
    # asked for.
    await db.execute(
        delete(SitemapHaPlacement).where(SitemapHaPlacement.sitemap_id == sitemap_id)
    )
    for p in placements:
        db.add(SitemapHaPlacement(
            sitemap_id=sitemap_id, peripheral_id=ids_by_slug[p["id"]],
            x=p["x"], y=p["y"],
        ))
    await db.commit()
    await db.refresh(row)
    await audit_svc.record(
        request, principal,
        "sitemap.ha_devices_set" if placements else "sitemap.ha_devices_cleared",
        target=f"sitemap/{row.id}",
        detail={"placed": len(placements), "was": int(before)},
    )
    cams = (await db.execute(select(Camera))).scalars().all()
    pins = await _ha_pins(db, [row.id])
    return _meta(row, _map_counts(cams).get(row.id, (0, 0)), pins.get(row.id))


@router.delete("/{sitemap_id}")
async def delete_sitemap(
    sitemap_id: int,
    request: Request,
    principal: Principal = Depends(_MANAGE),
    db=Depends(get_db),
) -> dict[str, Any]:
    row = await _get_or_404(db, sitemap_id)
    # Sweep placement refs in the same transaction as the delete so no camera
    # is left pointing at a ghost map.
    cams = (await db.execute(select(Camera))).scalars().all()
    unplaced = 0
    for c in cams:
        meta = c.camera_metadata or {}
        ref = meta.get("sitemap") or {}
        if ref.get("id") == sitemap_id:
            meta = {k: v for k, v in meta.items() if k != "sitemap"}
            c.camera_metadata = meta
            unplaced += 1
    # HA pins need no sweep — sitemap_ha_placements cascades on the FK
    # (migration 029). Counted first only so the response can report it.
    pins = (await db.execute(
        select(func.count()).select_from(SitemapHaPlacement)
        .where(SitemapHaPlacement.sitemap_id == sitemap_id)
    )).scalar_one()
    name = row.name
    await db.delete(row)
    await db.commit()
    await audit_svc.record(
        request, principal, "sitemap.deleted",
        target=f"sitemap/{sitemap_id}",
        detail={"name": name, "cameras_unplaced": unplaced, "ha_pins_removed": int(pins)},
    )
    return {
        "deleted": sitemap_id, "cameras_unplaced": unplaced,
        "ha_pins_removed": int(pins),
    }
