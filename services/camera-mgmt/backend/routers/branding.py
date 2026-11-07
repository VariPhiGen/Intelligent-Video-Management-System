# services/camera-mgmt/backend/routers/branding.py
"""Branding — the client's logos (light + dark theme), the Video Wall title, and the admin's
DEFAULT video-wall settings.

Reads are open to any authenticated principal (every operator sees the wall);
changes are admin-only and audited. Logos are PNG/JPEG/WebP only — not SVG,
which can carry script — and capped at BRANDING_LOGO_MAX_BYTES.

Wall defaults: every browser starts from these and may override single fields locally
(frontend-react/src/pages/live/wallPrefs.ts). They are validated here — the colours end up in
style attributes on every operator's screen, so only plain #rrggbb ever gets stored.
"""
import hashlib
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from ..db import get_db
from ..models import BRANDING_LOGO_MAX_BYTES, Branding, sniff_image_content_type
from ..security import Principal, require_role
from ..services import audit as audit_svc

router = APIRouter(prefix="/branding", tags=["branding"])

_ADMIN = require_role("admin")
_LOGO_TYPES = {"image/png", "image/jpeg", "image/webp"}
_COLOR = r"^(#[0-9a-fA-F]{6})?$"   # '' = theme default
Theme = Annotated[Literal["light", "dark"], Query()]


class WallDefaults(BaseModel):
    """Admin default for the video wall. camelCase on the wire = the SPA's WallPrefs shape, so the
    browser merges server defaults and local overrides key-for-key with no mapping layer."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    follow_all: bool = True
    camera_ids: list[str] = Field(default_factory=list, max_length=1000)
    hide_offline: bool = True
    top_pad: int = Field(default=0, ge=0, le=240)
    logo_h: int = Field(default=36, ge=20, le=160)
    title_px: int = Field(default=15, ge=12, le=72)
    bg_light: str = Field(default="", pattern=_COLOR)
    bg_dark: str = Field(default="", pattern=_COLOR)
    title_color_light: str = Field(default="", pattern=_COLOR)
    title_color_dark: str = Field(default="", pattern=_COLOR)


class BrandingMeta(BaseModel):
    wall_title: Optional[str] = None
    has_logo: bool = False           # light-theme logo (the original single logo)
    has_logo_dark: bool = False
    # Change whenever the logo does, so the SPA can cache-bust its blob fetch.
    logo_version: Optional[str] = None
    logo_dark_version: Optional[str] = None
    wall_defaults: Optional[dict] = None   # camelCase WallDefaults, or None = built-in defaults


class BrandingBody(BaseModel):
    # Both optional and PATCH-like: a field left out is left alone (saving the wall defaults must not
    # wipe the title, and vice versa). An explicit blank title still means "back to the default".
    wall_title: Optional[str] = Field(default=None, max_length=80)
    wall_defaults: Optional[WallDefaults] = None


async def _row(db) -> Optional[Branding]:
    return (await db.execute(select(Branding).where(Branding.id == 1))).scalars().first()


def _ver(b: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(b).hexdigest()[:16] if b else None


def _meta(row: Optional[Branding]) -> BrandingMeta:
    if not row:
        return BrandingMeta()
    dark = getattr(row, "logo_dark", None)
    return BrandingMeta(
        wall_title=row.wall_title or None,
        has_logo=bool(row.logo),
        has_logo_dark=bool(dark),
        logo_version=_ver(row.logo),
        logo_dark_version=_ver(dark),
        wall_defaults=getattr(row, "wall_defaults", None) or None,
    )


async def _upsert(db) -> Branding:
    row = await _row(db)
    if row is None:
        row = Branding(id=1)
        db.add(row)
    return row


def _fields(theme: str) -> tuple[str, str]:
    return ("logo_dark", "logo_dark_content_type") if theme == "dark" else ("logo", "logo_content_type")


@router.get("")
async def get_branding(db=Depends(get_db)) -> BrandingMeta:
    return _meta(await _row(db))


@router.put("")
async def set_branding(body: BrandingBody, request: Request,
                       principal: Principal = Depends(_ADMIN), db=Depends(get_db)) -> BrandingMeta:
    row = await _upsert(db)
    sent = body.model_fields_set
    detail: dict = {}
    if "wall_title" in sent:
        row.wall_title = (body.wall_title or "").strip() or None
        detail["wall_title"] = row.wall_title
    if "wall_defaults" in sent:
        row.wall_defaults = body.wall_defaults.model_dump(by_alias=True) if body.wall_defaults else None
        detail["wall_defaults"] = "reset" if row.wall_defaults is None else {
            k: v for k, v in row.wall_defaults.items() if k != "cameraIds"} | {
            "cameras": len(row.wall_defaults.get("cameraIds") or [])}
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = principal.subject
    await db.commit()
    await audit_svc.record(request, principal, "branding.updated", target="branding", detail=detail)
    return _meta(row)


@router.get("/logo")
async def get_logo(request: Request, db=Depends(get_db), theme: Theme = "light") -> Response:
    row = await _row(db)
    data_f, type_f = _fields(theme)
    data = getattr(row, data_f, None) if row else None
    if not data:
        raise HTTPException(404, detail="No logo set")
    etag = f'"{hashlib.sha256(data).hexdigest()[:32]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return Response(content=data, media_type=getattr(row, type_f) or "application/octet-stream",
                    headers={"ETag": etag, "Cache-Control": "private, max-age=3600",
                             "X-Content-Type-Options": "nosniff"})


@router.post("/logo")
async def upload_logo(request: Request, file: UploadFile = File(...),
                      principal: Principal = Depends(_ADMIN), db=Depends(get_db),
                      theme: Theme = "light") -> BrandingMeta:
    data = await file.read(BRANDING_LOGO_MAX_BYTES + 1)
    if len(data) > BRANDING_LOGO_MAX_BYTES:
        raise HTTPException(422, detail=f"Logo exceeds {BRANDING_LOGO_MAX_BYTES // (1024 * 1024)} MB")
    content_type = sniff_image_content_type(data)
    if content_type not in _LOGO_TYPES:
        raise HTTPException(422, detail="Unsupported logo — upload a PNG, JPEG or WebP image")
    row = await _upsert(db)
    data_f, type_f = _fields(theme)
    setattr(row, data_f, data)
    setattr(row, type_f, content_type)
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = principal.subject
    await db.commit()
    await audit_svc.record(request, principal, "branding.logo_set", target="branding",
                           detail={"content_type": content_type, "bytes": len(data), "theme": theme})
    return _meta(row)


@router.delete("/logo")
async def delete_logo(request: Request, principal: Principal = Depends(_ADMIN),
                      db=Depends(get_db), theme: Theme = "light") -> BrandingMeta:
    row = await _row(db)
    data_f, type_f = _fields(theme)
    if row and getattr(row, data_f, None):
        setattr(row, data_f, None)
        setattr(row, type_f, None)
        row.updated_at = datetime.now(timezone.utc)
        row.updated_by = principal.subject
        await db.commit()
        await audit_svc.record(request, principal, "branding.logo_removed", target="branding",
                               detail={"theme": theme})
    return _meta(row)
