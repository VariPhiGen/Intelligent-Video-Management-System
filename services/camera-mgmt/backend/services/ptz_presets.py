"""ptz_presets.py — VMS's record of each PTZ camera's presets, kept in step with the camera.

The camera still holds the positions (GotoPreset by token); VMS keeps a row per preset (models.PtzPreset) with
the name operators gave it, the stored pan/tilt/zoom, a thumbnail and Home. `sync` reconciles the two every time
presets are listed:

  * a preset on the camera that VMS has never seen → added (its camera name, or "Preset <token>" when the factory
    left it blank);
  * one VMS knows → position refreshed from the camera (someone may have re-stored it in the camera's own web
    UI — its zones are then paused until lined up with the new view: services/ptz_view_changes); the NAME stays
    VMS's — a VMS rename never touches the camera (see models.PtzPresetPatch);
  * one VMS knows that the camera no longer lists → kept, flagged `missing_on_camera` (a camera reset or
    replacement), so it shows and can be put back with "Restore" (stored position → SetPreset, same token).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PtzPreset
from . import ptz_view_changes


def label(token: Any, name: Any) -> str:
    n = str(name or "").strip()
    return (n or f"Preset {token}")[:64]


def out(p: PtzPreset) -> dict:
    return {"id": str(p.id), "token": p.token, "name": p.name, "pan": p.pan, "tilt": p.tilt, "zoom": p.zoom,
            "is_home": bool(p.is_home), "sort_order": p.sort_order, "missing_on_camera": bool(p.missing_on_camera),
            "has_thumbnail": p.thumbnail is not None,
            "thumbnail_at": p.thumbnail_at.isoformat() if p.thumbnail_at else None,
            "created_by": p.created_by, "created_at": p.created_at.isoformat() if p.created_at else None,
            # Per-preset AI (services/ptz_views): what this view watches, and its pictures (day / night).
            "ai": ai_summary(getattr(p, "analytics_config", None)),
            "reference_at": _iso(getattr(p, "reference_at", None)),
            "reference_night_at": _iso(getattr(p, "reference_night_at", None)),
            # The view changed under the zones (services/ptz_view_changes): moved with it, or paused for a check.
            "review": ptz_view_changes.review_out(getattr(p, "analytics_review", None)),
            "has_previous_reference": getattr(p, "previous_reference", None) is not None}


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v else None


def ai_summary(cfg: Any) -> dict:
    """{zones, tripwires, rules} of a preset's config — the counts the preset list shows."""
    regions = (cfg or {}).get("regions") or {} if isinstance(cfg, dict) else {}
    acts = (cfg or {}).get("activities") or [] if isinstance(cfg, dict) else []
    kinds = [r.get("kind") for r in regions.values() if isinstance(r, dict)]
    return {"zones": kinds.count("zone"), "tripwires": kinds.count("tripwire"),
            "rules": sum(1 for a in acts if isinstance(a, dict) and a.get("type"))}


async def rows(db: AsyncSession, camera_id: uuid.UUID) -> list[PtzPreset]:
    return list((await db.execute(select(PtzPreset).where(PtzPreset.camera_id == camera_id)
                                  .order_by(PtzPreset.sort_order, PtzPreset.name))).scalars())


async def sync(db: AsyncSession, camera_id: uuid.UUID, camera_presets: Optional[list[dict]]) -> list[PtzPreset]:
    """Reconcile with what the camera listed (None = the camera could not be asked: keep VMS's rows as they are)."""
    have = {p.token: p for p in await rows(db, camera_id)}
    if camera_presets is None:
        return sorted(have.values(), key=lambda p: (p.sort_order, p.name))
    seen = set()
    next_order = max([p.sort_order for p in have.values()] or [0]) + 1
    for cp in camera_presets:
        token = str(cp.get("token") or "")
        if not token:
            continue
        seen.add(token)
        row = have.get(token)
        if row is None:
            row = PtzPreset(id=uuid.uuid4(), camera_id=camera_id, token=token, name=label(token, cp.get("name")),
                            sort_order=next_order)
            next_order += 1
            db.add(row)
            have[token] = row
        elif ptz_view_changes.position_moved(row, cp.get("pan"), cp.get("tilt"), cp.get("zoom")):
            # Re-stored in the camera's own web page (or another client): its zones were drawn on another view.
            ptz_view_changes.mark_moved_outside(row)
        row.pan, row.tilt, row.zoom = cp.get("pan"), cp.get("tilt"), cp.get("zoom")
        row.missing_on_camera = False
    for token, row in have.items():
        if token not in seen and not row.missing_on_camera:
            row.missing_on_camera = True
    await db.commit()
    return sorted(have.values(), key=lambda p: (p.sort_order, p.name))


async def upsert_saved(db: AsyncSession, camera_id: uuid.UUID, token: str, name: str,
                       position: Optional[dict], by: Optional[str], thumbnail: Optional[bytes]) -> PtzPreset:
    """Record a preset just stored on the camera ("save current view")."""
    row = (await db.execute(select(PtzPreset).where(PtzPreset.camera_id == camera_id, PtzPreset.token == token)
                            )).scalar_one_or_none()
    if row is None:
        order = max([p.sort_order for p in await rows(db, camera_id)] or [0]) + 1
        row = PtzPreset(id=uuid.uuid4(), camera_id=camera_id, token=token, sort_order=order, created_by=by)
        db.add(row)
    row.name = label(token, name)
    if position:
        row.pan, row.tilt, row.zoom = position.get("pan"), position.get("tilt"), position.get("zoom")
    row.missing_on_camera = False
    if thumbnail:
        row.thumbnail, row.thumbnail_at = thumbnail, datetime.now(timezone.utc)
    await db.commit()
    return row


def make_thumbnail(jpeg: bytes, width: int = 320) -> bytes:
    """A small JPEG for the preset list (~15 KB) — the full frame would be ~300 KB per preset in Postgres."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    im.thumbnail((width, width))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=75)
    return buf.getvalue()
