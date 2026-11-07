"""timelapse.py — per-camera time-lapse over HTTP.

Mounted at /api/timelapse behind require_authenticated (main.py); every route
self-gates on one of two grantable capabilities:

    timelapse_view    see stills, list/play/download videos, ask for a render
    timelapse_manage  switch a camera's time-lapse on/off, change its settings,
                      the site default settings, delete stills and videos
    admin role        the disk protection limits (below)

WHY THE DISK LIMITS ARE ADMIN-ONLY. The guard's floor and budget decide how close
stills may push the shared disk to the NVR's emergency eviction, i.e. whether
recorded footage gets deleted to make room for stills. That is the recorder's
storage, and every other setting that spends it (NVR writes, a camera's sub-track
recording) is admin-only; holding timelapse_manage must not be a back door to it.
The default settings stay on timelapse_manage: they are the same per-camera
settings, set once for all cameras, and the guard still bounds what they can cost.

WHY ITS OWN PAIR AND NOT camera_manage / playback_search. Switching time-lapse
on starts a durable record of a camera's view that outlives its footage by
design (90 days of stills on a 7-day camera), so it is a decision an admin may
want to delegate to a site lead without handing over the rest of camera
configuration. Viewing is likewise separate from playback: a client-facing
progress viewer should be grantable without opening recorded footage.

WHY A DOWNLOAD NEEDS ONLY timelapse_view, NOT export_reports. The nearest
existing precedent is the NVR's clip export — an MP4 of real footage — which is
gated by playback_search alone (routers/nvr.py). `export_reports` covers the
Excel/NVR reports and evidence packages, i.e. records about the system. A
time-lapse video is the same kind of artefact as a clip, made from stills the
viewer can already see one by one, so gating it harder than a clip would protect
nothing and contradict the clip rule.

BULK. POST /bulk switches many cameras on or off at once (the Inventory's bulk
bar) under the same timelapse_manage gate. It is not a second implementation:
each camera's outcome comes from services/timelapse.plan_bulk, which validates
through the same TimelapseConfig as PUT /cameras/{id} and overlays any settings
on the camera's own, so bulk and per-camera saves store identical shapes.

The logic lives in services/timelapse.py; this module is HTTP only.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Camera, CameraStage, TimelapseFrame, TimelapseRender
from ..security import get_principal
from ..services import audit as audit_svc
from ..services import object_store
from ..services import timelapse as tl
from ..services.audit import actor_of
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/timelapse", tags=["timelapse"])

_view = [Depends(require_capability("timelapse_view"))]
_manage = [Depends(require_capability("timelapse_manage"))]


class ConfigBody(BaseModel):
    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)


class BulkBody(BaseModel):
    # Strings, not uuid.UUID: one malformed id is that row's failure in the
    # result list, not a 422 that throws away the other 49 cameras' switch.
    camera_ids: list[str]
    enabled: bool
    # A PARTIAL settings overlay (see tl.plan_bulk), only meaningful with
    # enabled=true. Omitted/None = each camera keeps what it has (defaults for
    # one that was never configured).
    config: Optional[dict[str, Any]] = None


class EstimateBody(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class RenderBody(BaseModel):
    start: datetime
    end: datetime
    fps: int = 24
    overlay: bool = True
    deflicker: bool = False


async def _camera(db: AsyncSession, camera_id: uuid.UUID) -> Camera:
    cam = (await db.execute(select(Camera).where(
        Camera.id == camera_id, Camera.stage == CameraStage.REGISTERED.value))).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return cam


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_config(raw: dict[str, Any]) -> tl.TimelapseConfig:
    try:
        return tl.TimelapseConfig(**raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", ()))
        raise HTTPException(status_code=422, detail=f"{where}: {first.get('msg', 'invalid')}" if where
                            else str(exc)) from exc


async def _refuse_if_disk_full(db: AsyncSession) -> None:
    usage = await tl.disk_usage(db)
    if usage["over"]:
        raise HTTPException(status_code=409, detail=f"Time-lapse can't be switched on: {usage['reason']}. "
                                                    "Free some space or lower the settings first.")


@router.get("/cameras", dependencies=_view)
async def list_cameras(db: AsyncSession = Depends(get_db)) -> dict:
    """Every registered camera with its switch, and stats for the ones that have stills."""
    await tl.load_site(db)
    cams = (await db.execute(select(Camera).where(Camera.stage == CameraStage.REGISTERED.value)
                             .order_by(Camera.name.asc()))).scalars().all()
    out = []
    for c in cams:
        enabled = bool(c.timelapse_enabled)
        stats = await tl.totals(db, c.id)
        if not enabled and not stats["frames"]:
            out.append({"id": str(c.id), "name": c.name, "slug": c.slug, "enabled": False,
                        "frames": 0, "bytes": 0, "last_captured_at": None, "status": None})
            continue
        out.append({"id": str(c.id), "name": c.name, "slug": c.slug, "enabled": enabled, **stats,
                    "status": await tl.read_status(c.slug)})
    return {"cameras": out, "object_store": object_store.is_configured(), "paused": tl.paused_state()}


@router.get("/cameras/{camera_id}", dependencies=_view)
async def get_camera(camera_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    cam = await _camera(db, camera_id)
    await tl.load_site(db)
    base = tl.site_defaults()
    cfg = tl.effective_config(cam.timelapse_config, base)
    paused = tl.paused_state()
    return {"id": str(cam.id), "name": cam.name, "slug": cam.slug, "enabled": bool(cam.timelapse_enabled),
            "config": cfg.model_dump(), "estimate": tl.estimate(cfg), **(await tl.totals(db, cam.id)),
            # What the Defaults page says, and which settings this camera changes on top of it.
            "defaults": base.model_dump(), "changed": sorted(tl.overrides_of(cfg, base)),
            "paused": paused if paused and str(cam.id) in (paused.get("camera_ids") or []) and not cam.timelapse_enabled
            else None,
            "status": await tl.read_status(cam.slug), "privacy_masks": len(cam.privacy_masks or []),
            "ptz_capable": bool(getattr(cam, "ptz_capabilities", None)),
            "object_store": object_store.is_configured()}


@router.put("/cameras/{camera_id}", dependencies=_manage)
async def put_camera(camera_id: uuid.UUID, body: ConfigBody, request: Request,
                     db: AsyncSession = Depends(get_db)) -> dict:
    """Switch time-lapse on/off and/or change its settings. The page sends the
    whole effective config; the camera stores only what differs from the site
    defaults, so a later change to a default still reaches every setting this
    camera left alone."""
    cam = await _camera(db, camera_id)
    await tl.load_site(db)
    cfg = _parse_config(body.config)
    was = bool(cam.timelapse_enabled)
    if body.enabled and not was:
        await _refuse_if_disk_full(db)
    cam.timelapse_enabled = body.enabled
    cam.timelapse_config = tl.overrides_of(cfg)
    await db.commit()
    tl.forget(cam.id)          # a new interval / schedule starts its cadence fresh
    await audit_svc.record(request, await get_principal(request), "camera.timelapse_updated",
                           target=cam.slug, detail={"enabled": body.enabled, "was_enabled": was,
                                                    "interval_s": cfg.interval_s,
                                                    "retention_days": cfg.retention_days,
                                                    "resolution": cfg.resolution})
    log.info("timelapse.config_updated", slug=cam.slug, enabled=body.enabled)
    return await get_camera(camera_id, db)


@router.post("/bulk", dependencies=_manage)
async def bulk_switch(body: BulkBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Switch time-lapse on or off for many cameras at once (Inventory's bulk bar).

    Same capability, same validation and the same stored shape as PUT
    /cameras/{id} — tl.plan_bulk decides each camera's outcome with the exact
    TimelapseConfig model the per-camera save uses, so a camera switched on here
    is indistinguishable from one switched on from its own tab.

    WHY ONE TRANSACTION, PLANNED FIRST. Every camera is planned before anything
    is written and the changes go out in a single commit: either the selection
    the operator saw in the result is what the registry holds, or (commit
    failure) nothing changed at all and the answer says so. A per-row commit
    would leave "7 of 12 switched on" after a database blip, with no honest
    way to report which seven. Per-row problems (an unknown id) are not
    transaction failures — they are reported in that row and skipped.
    """
    ids = list(dict.fromkeys(body.camera_ids))       # de-dup, keep order — a double tick is one camera
    if not ids:
        raise HTTPException(status_code=422, detail="Select at least one camera")
    if len(ids) > tl.BULK_MAX:
        raise HTTPException(status_code=422,
                            detail=f"At most {tl.BULK_MAX} cameras per request — {len(ids)} were sent")
    await tl.load_site(db)
    patch = body.config or None
    if patch is not None:
        if not body.enabled:
            # Off never changes settings (tl.plan_bulk); accepting settings here
            # would read as "saved" while nothing was stored.
            raise HTTPException(status_code=422, detail="Settings can only be sent when switching time-lapse on")
        bad = sorted(set(patch) & tl.BULK_PER_CAMERA_ONLY)
        if bad:
            raise HTTPException(status_code=422, detail=f"{bad[0]}: set per camera on its Time-lapse tab, "
                                                        "not in bulk — a preset only exists on one camera")
        # Validate the overlay on its own first, so a bad interval is ONE 422
        # naming the field (same wording as the per-camera PUT) instead of the
        # same error repeated in every row. Only the keys sent are kept, in their
        # normalised form (a schedule comes back through RecordingSchedule).
        normal = _parse_config(patch).model_dump()
        patch = {k: normal[k] for k in patch}
    if body.enabled:
        await _refuse_if_disk_full(db)

    parsed: dict[str, uuid.UUID] = {}
    results: dict[str, dict[str, Any]] = {}
    for raw in ids:
        try:
            parsed[raw] = uuid.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            results[raw] = {"camera_id": raw, "ok": False, "error": "Not a camera id"}
    cams = {}
    if parsed:
        rows = (await db.execute(select(Camera).where(
            Camera.id.in_(list(parsed.values())), Camera.stage == CameraStage.REGISTERED.value))).scalars().all()
        cams = {c.id: c for c in rows}

    changed: list[tuple[Camera, bool, tl.BulkPlan]] = []
    for raw, cid in parsed.items():
        cam = cams.get(cid)
        if cam is None:
            results[raw] = {"camera_id": raw, "ok": False, "error": "Camera not found"}
            continue
        was = bool(cam.timelapse_enabled)
        try:
            plan = tl.plan_bulk(was_enabled=was, raw_config=cam.timelapse_config,
                                enabled=body.enabled, patch=patch)
        except ValidationError as exc:
            # The overlay was valid alone but not on top of this camera's own
            # settings (a stored value a newer rule rejects). This camera only.
            first = exc.errors()[0] if exc.errors() else {}
            results[raw] = {"camera_id": raw, "name": cam.name, "ok": False,
                            "error": f"{'.'.join(str(x) for x in first.get('loc', ()))}: {first.get('msg', 'invalid')}"}
            continue
        row: dict[str, Any] = {"camera_id": raw, "name": cam.name, "ok": True, "result": plan.result,
                               "notes": tl.bulk_notes(camera_enabled=bool(cam.enabled),
                                                      health_status=getattr(cam, "health_status", None),
                                                      ptz_capable=bool(getattr(cam, "ptz_capabilities", None)),
                                                      cfg=plan.config, enabled=body.enabled)}
        if plan.result.startswith("already_"):
            row["skipped_reason"] = plan.result
        results[raw] = row
        if plan.changed:
            cam.timelapse_enabled = plan.enabled
            cam.timelapse_config = plan.config
            changed.append((cam, was, plan))

    if changed:
        try:
            await db.commit()
        except Exception as exc:                                   # noqa: BLE001
            await db.rollback()
            log.warning("timelapse.bulk_commit_failed", error=str(exc)[:300], cameras=len(changed))
            raise HTTPException(status_code=503, detail="The registry could not be updated — "
                                                        "no camera was changed. Try again.") from exc
    principal = await get_principal(request) if changed else None
    for cam, was, plan in changed:
        tl.forget(cam.id)          # as the per-camera PUT: a new interval / schedule starts its cadence fresh
        # One audit row PER CAMERA, same action as the per-camera save, so the
        # camera's history reads the same however it was switched; `bulk` and
        # the batch size tell the two apart when it matters.
        await audit_svc.record(request, principal, "camera.timelapse_updated", target=cam.slug,
                               detail={"enabled": plan.enabled, "was_enabled": was, "bulk": True,
                                       "batch": len(ids), "interval_s": plan.config.get("interval_s"),
                                       "retention_days": plan.config.get("retention_days"),
                                       "resolution": plan.config.get("resolution")})
    out = [results[r] for r in ids]
    counts = {k: sum(1 for r in out if r.get("result") == k)
              for k in ("enabled", "updated", "disabled", "already_enabled", "already_disabled")}
    log.info("timelapse.bulk", enabled=body.enabled, requested=len(ids), changed=len(changed),
             failed=sum(1 for r in out if not r["ok"]))
    return {"requested": len(ids), **counts, "failed": sum(1 for r in out if not r["ok"]),
            "changed": len(changed), "results": out}


@router.get("/defaults", dependencies=_view)
async def defaults(db: AsyncSession = Depends(get_db)) -> dict:
    """The site's default settings (what every camera follows unless it changes a
    setting itself), the built-in ones they started from, the disk guard and the
    disk it watches. The bulk dialog starts from `config`."""
    await tl.load_site(db, force=True)
    cfg = tl.site_defaults()
    enabled = len((await db.execute(select(Camera.id).where(
        Camera.timelapse_enabled.is_(True), Camera.stage == CameraStage.REGISTERED.value))).scalars().all())
    return {"config": cfg.model_dump(), "builtin": tl.builtin_defaults(), "estimate": tl.estimate(cfg),
            "bulk_max": tl.BULK_MAX, "per_camera_only": sorted(tl.BULK_PER_CAMERA_ONLY),
            "site_only_excludes": sorted(tl.PER_CAMERA_ONLY), "guard": tl.guard_settings(),
            "disk": await tl.disk_usage(db), "paused": tl.paused_state(), "enabled_cameras": int(enabled)}


class SettingsBody(BaseModel):
    # A full or partial config; stored as the keys that differ from the built-in defaults.
    defaults: Optional[dict[str, Any]] = None
    guard: Optional[dict[str, float]] = None


@router.put("/defaults", dependencies=_manage)
async def put_defaults(body: SettingsBody, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Change the site defaults and/or the disk guard. Every camera that didn't
    change a setting itself follows the new default from its next still."""
    await tl.load_site(db, force=True)       # compare the limits against what's stored, not a 15 s-old copy
    row = await tl.site_row(db)
    detail: dict[str, Any] = {}
    if body.defaults is not None:
        bad = sorted(set(body.defaults) & tl.PER_CAMERA_ONLY)
        if bad:
            raise HTTPException(status_code=422, detail=f"{bad[0]}: set per camera on its Time-lapse tab — "
                                                        "a preset only exists on one camera")
        cfg = _parse_config({**tl.builtin_defaults(), **body.defaults})
        row.defaults = tl.overrides_of(cfg, tl.TimelapseConfig())
        detail["defaults"] = row.defaults
    if body.guard is not None:
        g = {**tl.guard_settings(), **{k: float(v) for k, v in body.guard.items() if k in tl.GUARD_DEFAULTS}}
        # Only a CHANGE needs admin: the page sends the limits back unchanged with every save.
        if g != tl.guard_settings() and not (await get_principal(request)).has_any(frozenset({"admin"})):
            raise HTTPException(status_code=403, detail="Only an administrator can change the disk protection limits")
        if not 5 <= g["floor_free_pct"] <= 50:
            raise HTTPException(status_code=422, detail="Minimum free disk must be between 5% and 50%")
        if not 1 <= g["budget_pct"] <= 90:
            raise HTTPException(status_code=422, detail="Time-lapse budget must be between 1% and 90% of the disk")
        row.guard = g
        detail["guard"] = g
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = actor_of(await get_principal(request))[1]
    await db.commit()
    tl._site_from_row(row)
    for cid in (await db.execute(select(Camera.id).where(Camera.timelapse_enabled.is_(True)))).scalars().all():
        tl.forget(cid)          # a new default interval / schedule starts its cadence fresh
    await audit_svc.record(request, await get_principal(request), "timelapse.defaults_updated",
                           target="timelapse", detail=detail)
    return await defaults(db)


@router.get("/changed", dependencies=_view)
async def changed_cameras(db: AsyncSession = Depends(get_db)) -> dict:
    """Cameras whose settings differ from the site defaults, setting by setting."""
    await tl.load_site(db)
    base = tl.site_defaults()
    b = base.model_dump()
    cams = (await db.execute(select(Camera).where(Camera.stage == CameraStage.REGISTERED.value)
                             .order_by(Camera.name.asc()))).scalars().all()
    out = []
    for c in cams:
        diff = tl.overrides_of(tl.effective_config(c.timelapse_config, base), base)
        if diff:
            out.append({"id": str(c.id), "name": c.name, "slug": c.slug, "enabled": bool(c.timelapse_enabled),
                        "changes": {k: {"value": v, "default": b.get(k)} for k, v in sorted(diff.items())}})
    return {"cameras": out, "defaults": b}


class UseDefaultsBody(BaseModel):
    # Settings to put back on the defaults; None = all of them (a PTZ preset is kept unless named).
    keys: Optional[list[str]] = None


@router.post("/cameras/{camera_id}/use-defaults", dependencies=_manage)
async def use_defaults(camera_id: uuid.UUID, body: UseDefaultsBody, request: Request,
                       db: AsyncSession = Depends(get_db)) -> dict:
    cam = await _camera(db, camera_id)
    await tl.load_site(db)
    own = tl.overrides_of(tl.effective_config(cam.timelapse_config))
    drop = set(body.keys) if body.keys is not None else set(own) - tl.PER_CAMERA_ONLY
    cam.timelapse_config = {k: v for k, v in own.items() if k not in drop}
    await db.commit()
    tl.forget(cam.id)
    await audit_svc.record(request, await get_principal(request), "camera.timelapse_updated", target=cam.slug,
                           detail={"use_defaults": sorted(drop & set(own))})
    return await get_camera(camera_id, db)


@router.post("/guard/resume", dependencies=_manage)
async def guard_resume(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Switch time-lapse back on for the cameras the disk guard paused — refused
    while the disk is still over the limit."""
    await tl.load_site(db, force=True)
    paused = tl.paused_state()
    if not paused:
        return {"resumed": 0}
    await _refuse_if_disk_full(db)
    ids = []
    for raw in paused.get("camera_ids") or []:
        try:
            ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    cams = (await db.execute(select(Camera).where(Camera.id.in_(ids), Camera.stage == CameraStage.REGISTERED.value)
                             )).scalars().all() if ids else []
    for c in cams:
        c.timelapse_enabled = True
    row = await tl.site_row(db)
    row.paused = None
    await db.commit()
    tl._site_from_row(row)
    for c in cams:
        tl.forget(c.id)
    await audit_svc.record(request, await get_principal(request), "timelapse.resumed", target="timelapse",
                           detail={"cameras": [c.slug for c in cams]})
    return {"resumed": len(cams)}


@router.post("/guard/dismiss", dependencies=_manage)
async def guard_dismiss(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Clear the pause remark without switching anything back on."""
    row = await tl.site_row(db)
    row.paused = None
    await db.commit()
    tl._site_from_row(row)
    await audit_svc.record(request, await get_principal(request), "timelapse.pause_dismissed", target="timelapse")
    return {"ok": True}


@router.post("/estimate", dependencies=_view)
async def estimate(body: EstimateBody, db: AsyncSession = Depends(get_db)) -> dict:
    """Storage and video length for a config, before it is saved. A partial config
    (the bulk dialog's four settings) is completed from the site defaults."""
    await tl.load_site(db)
    return tl.estimate(_parse_config({**tl.site_defaults().model_dump(), **body.config}))


@router.get("/cameras/{camera_id}/days", dependencies=_view)
async def list_days(camera_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    cam = await _camera(db, camera_id)
    return {"days": await tl.days(db, cam.id)}


@router.get("/cameras/{camera_id}/frames", dependencies=_view)
async def list_frames(camera_id: uuid.UUID, day: date = Query(...), db: AsyncSession = Depends(get_db)) -> dict:
    cam = await _camera(db, camera_id)
    return {"day": day.isoformat(), "frames": await tl.frames_of_day(db, cam.id, day)}


@router.get("/cameras/{camera_id}/daily", dependencies=_view)
async def list_daily(camera_id: uuid.UUID, since: Optional[date] = Query(None),
                     limit: int = Query(400, ge=1, le=3650), db: AsyncSession = Depends(get_db)) -> dict:
    """One still per day (nearest local noon), oldest first — the progress strip.
    `since` lets a puller (a cloud site-map replay, over X-Internal-Key)
    fetch only the days it doesn't have yet."""
    cam = await _camera(db, camera_id)
    return {"camera_id": str(cam.id), "name": cam.name,
            "days": await tl.daily(db, cam.id, since=since, limit=limit)}


@router.get("/cameras/{camera_id}/presets", dependencies=_manage)
async def list_presets(camera_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """The camera's ONVIF presets for the "framed on preset" picker. Gated by
    timelapse_manage, not ptz_control: choosing which preset a time-lapse
    expects moves nothing, and a site lead configuring time-lapse shouldn't need
    the right to steer the camera."""
    cam = await _camera(db, camera_id)
    from ..services import onvif_ptz, ptz_control
    target = await ptz_control._resolve_target(cam.slug)      # noqa: SLF001
    if target is None:
        raise HTTPException(status_code=409, detail="Camera has no ONVIF address or credentials")
    ip, port, user, password, profile = target
    try:
        presets = await onvif_ptz.get_presets(ip, port, user, password, profile)
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Camera did not list its presets: {str(exc)[:160]}") from exc
    return {"presets": [{"token": p.get("token"), "name": p.get("name")} for p in presets if p.get("token")]}


@router.delete("/cameras/{camera_id}/frames", dependencies=_manage)
async def delete_day(camera_id: uuid.UUID, request: Request, day: date = Query(...),
                     db: AsyncSession = Depends(get_db)) -> dict:
    cam = await _camera(db, camera_id)
    rows = (await db.execute(select(TimelapseFrame.id, TimelapseFrame.object_key).where(
        TimelapseFrame.camera_id == cam.id, TimelapseFrame.local_day == day))).all()
    n = await tl._delete_frames(db, rows)
    await audit_svc.record(request, await get_principal(request), "camera.timelapse_day_deleted",
                           target=cam.slug, detail={"day": day.isoformat(), "frames": n})
    return {"deleted": n}


@router.get("/frames/{frame_id}/image", dependencies=_view)
async def frame_image(frame_id: int, request: Request, w: Optional[int] = Query(None, ge=64, le=1920),
                      db: AsyncSession = Depends(get_db)) -> Response:
    """One still. Immutable once written, so it caches hard; the ETag is its id
    (and the width, when `w` asks for a smaller copy — scaled on the fly, never
    up, so a 640-px copy for a remote replay doesn't ship a full 1080p JPEG)."""
    row = await db.get(TimelapseFrame, frame_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Still not found")
    etag = f'"tl-{row.id}{f"-w{w}" if w else ""}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    try:
        data = await tl.frame_bytes(row)
    except object_store.ObjectStoreError as exc:
        raise HTTPException(status_code=503, detail="The image store is unreachable") from exc
    if not data:
        raise HTTPException(status_code=404, detail="Still no longer available")
    if w and (row.width or 0) > w:
        data = await asyncio.to_thread(tl.scale_jpeg, data, w)
    return Response(content=data, media_type="image/jpeg",
                    headers={"ETag": etag, "Cache-Control": "private, max-age=86400, immutable"})


@router.get("/cameras/{camera_id}/renders", dependencies=_view)
async def list_renders(camera_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    cam = await _camera(db, camera_id)
    rows = (await db.execute(select(TimelapseRender).where(TimelapseRender.camera_id == cam.id)
                             .order_by(TimelapseRender.created_at.desc()).limit(100))).scalars().all()
    return {"renders": [tl.render_out(r) for r in rows]}


@router.post("/cameras/{camera_id}/renders", dependencies=_view)
async def create_render(camera_id: uuid.UUID, body: RenderBody, request: Request,
                        db: AsyncSession = Depends(get_db)) -> dict:
    """Queue a video of a range. Viewers may ask: a render is a different playback
    of stills they can already see, and the queue is bounded."""
    cam = await _camera(db, camera_id)
    principal = await get_principal(request)
    try:
        row = await tl.enqueue_render(db, cam, start=_aware(body.start), end=_aware(body.end), fps=body.fps,
                                      overlay=body.overlay, deflicker=body.deflicker, kind="manual",
                                      requested_by=(actor_of(principal)[1] or actor_of(principal)[0]))
    except tl.Rejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit_svc.record(request, principal, "camera.timelapse_render_requested", target=cam.slug,
                           detail={"render_id": str(row.id), "from": row.range_start.isoformat(),
                                   "to": row.range_end.isoformat(), "frames": row.frames})
    return tl.render_out(row)


@router.get("/renders/{render_id}", dependencies=_view)
async def get_render(render_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(TimelapseRender, render_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Render not found")
    return tl.render_out(row)


@router.get("/renders/{render_id}/video", dependencies=_view)
async def render_video(render_id: uuid.UUID, request: Request, download: bool = False,
                       db: AsyncSession = Depends(get_db)) -> Response:
    row = await db.get(TimelapseRender, render_id)
    if row is None or row.status != "done":
        raise HTTPException(status_code=404, detail="Video not ready")
    try:
        data = await tl.video_bytes(row)
    except object_store.ObjectStoreError as exc:
        raise HTTPException(status_code=503, detail="The video store is unreachable") from exc
    if not data:
        raise HTTPException(status_code=404, detail="Video no longer available")
    headers = {"ETag": f'"tlr-{row.id}"', "Cache-Control": "private, max-age=3600"}
    if download:
        cam = await db.get(Camera, row.camera_id)
        name = f"timelapse-{cam.slug if cam else 'camera'}-{tl._local(row.range_start):%Y%m%d}-{tl._local(row.range_end):%Y%m%d}.mp4"
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
        await audit_svc.record(request, await get_principal(request), "camera.timelapse_downloaded",
                               target=cam.slug if cam else str(row.camera_id),
                               detail={"render_id": str(row.id), "bytes": len(data)})
    return Response(content=data, media_type="video/mp4", headers=headers)


@router.delete("/renders/{render_id}", dependencies=_manage)
async def delete_render(render_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(TimelapseRender, render_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Render not found")
    if row.status == "running":
        raise HTTPException(status_code=409, detail="This video is being rendered — delete it when it finishes")
    if row.object_key and object_store.is_configured():
        try:
            await asyncio.to_thread(object_store.delete, row.object_key)
        except object_store.ObjectStoreError as exc:
            raise HTTPException(status_code=503, detail="The video store is unreachable") from exc
    cam_id = row.camera_id
    await db.execute(delete(TimelapseRender).where(TimelapseRender.id == render_id))
    await db.commit()
    await audit_svc.record(request, await get_principal(request), "camera.timelapse_render_deleted",
                           target=str(cam_id), detail={"render_id": str(render_id)})
    return {"deleted": True}
