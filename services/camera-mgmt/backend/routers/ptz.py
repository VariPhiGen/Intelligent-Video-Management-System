"""ptz.py — ONVIF pan/tilt/zoom control for cameras that actually have it.

Mounted under /cameras/{camera_id}/ptz rather than inside cameras.py: that file
is already 2k lines, and PTZ is the one camera surface with live-control
semantics (leases, watchdogs, a command rate no other endpoint here sees).

Three layers, deliberately separate:

    routers/ptz.py          HTTP, RBAC, audit, capability gating   ← you are here
    services/ptz_control.py leases + the deadman watchdog
    services/onvif_ptz.py   the ONVIF protocol itself

**Capability gating is the product rule.** A PTZ control never appears on, and
never reaches, a fixed camera. Every route here goes through `_ptz_target`,
which refuses with a 409 naming the reason when the camera is not movable. The
answer is cached on the camera row (probed once off the device, refreshed on
credential entry and on demand) because the UI needs it for every camera at
once and a SOAP round trip per camera per render is not viable.

**Permissions split along the same line the risk does.** Moving a camera is
`ptz_control` — an operational act with a physical consequence, granted to
console roles. Creating or deleting a preset, and setting the home position,
are configuration and stay on `camera_manage`. An operator who tracks someone
across a forecourt does not thereby get to re-aim the camera permanently.

**What is deliberately NOT here:** auto-tracking (needs the analytics pipeline
in the loop) and digital PTZ on fixed cameras (a client-side crop, no device
involvement — it belongs in the player, not in an ONVIF router). Tours/patrols
live in extensions/ptz_patrols.

**Presets, position and calibration (2026-10, KEC kec01).** Presets are still
stored ON the camera, but VMS keeps a record of each (services/ptz_presets) for
names, thumbnails, Home and "the camera lost it". Where each head points, and the
history of who moved it, comes from services/ptz_track. Click-to-centre and
drag-to-zoom go through POST /ptz/centre, which turns a point in the picture into
a move from the camera's own measured field of view (services/ptz_calibrate) —
the old fixed fraction of the whole pan range never landed on the click.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import (
    AnalyticsConfigBody,
    Camera,
    PtzAbsoluteBody,
    PtzAimBody,
    PtzCentreBody,
    PtzGotoBody,
    PtzMoveBody,
    PtzPositionLog,
    PtzPreset,
    PtzPresetBody,
    PtzPresetPatch,
    PtzStepBody,
)
from ..security import get_principal, require_role
from ..services import audit as audit_svc
from ..services import onvif_media, onvif_ptz, ptz_calibrate, ptz_control, ptz_presets, ptz_track
from ..services import policy as policy_svc
from ..services.policy import require_capability
# Camera lookup and ONVIF credential resolution are camera-generic and already
# battle-tested in cameras.py (they own the 404 / "credentials do not decrypt"
# wording the SPA deep-links on). Imported rather than re-implemented so the two
# surfaces cannot drift; cameras.py does not import this module, so no cycle.
from .cameras import _get_camera_or_404, _onvif_target_or_409

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/cameras", tags=["ptz"])

_can_ptz = [Depends(require_capability("ptz_control"))]
_can_manage = [Depends(require_capability("camera_manage"))]
# Who moved which camera, when: admin-only unless granted (services/policy "ptz_history").
_can_history = [Depends(require_capability("ptz_history"))]


async def _sees_history(request: Request) -> bool:
    return await policy_svc.allowed(await get_principal(request), "ptz_history")


def _anonymous(pos: Optional[dict]) -> Optional[dict]:
    """A current position without WHO moved it — what everyone without PTZ movement history gets. Where it points,
    and that a patrol or a preset did it, stay: the console needs those; a name is history."""
    return {**pos, "actor": None} if isinstance(pos, dict) else pos


# ─── Capability resolution ────────────────────────────────────────────────────

async def _probe_and_store(camera: Camera, db: AsyncSession) -> dict[str, Any]:
    """Ask the camera what it can do and cache the answer on its row.

    A probe that cannot reach the device is NOT written: overwriting a known-good
    capability block with "unreachable" would make a working PTZ camera look
    fixed for as long as it is offline, and the control would vanish from the UI
    exactly when an operator is trying to work out what is wrong with it.
    """
    ip, port, user, password = _onvif_target_or_409(camera)
    try:
        caps = await onvif_ptz.probe(ip, port, user, password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))

    caps["probed_at"] = datetime.now(timezone.utc).isoformat()
    camera.ptz_capabilities = caps
    await db.commit()
    log.info("ptz.probed", slug=camera.slug, supported=caps.get("supported"),
             reason=caps.get("reason"))
    return caps


async def _capabilities(camera: Camera, db: AsyncSession, *, refresh: bool = False) -> dict[str, Any]:
    """Cached capabilities, probing on first use or on demand.

    First use probes because there is no other honest moment: discovery runs
    before credentials exist on many installs, and a background sweep of every
    camera would be a SOAP storm to answer a question most operators never ask.
    """
    if refresh or camera.ptz_capabilities is None:
        return await _probe_and_store(camera, db)
    return camera.ptz_capabilities


async def _ptz_target(
    camera_id: uuid.UUID, db: AsyncSession
) -> tuple[Camera, tuple[str, int, str, str], Optional[str], dict[str, Any]]:
    """The single gate every control route passes through.

    Returns (camera, onvif target, ptz profile token, capabilities), or refuses:
    409 with the camera's own reason when it does not pan. This is where "PTZ
    only on PTZ cameras" is actually enforced — the UI hiding the pad is a
    courtesy, this is the rule.
    """
    camera = await _get_camera_or_404(camera_id, db)
    caps = await _capabilities(camera, db)
    if not ptz_control.is_capable(caps):
        raise HTTPException(
            status_code=409,
            detail=caps.get("reason") or "This camera has no ONVIF PTZ control",
        )
    return camera, _onvif_target_or_409(camera), ptz_control.profile_token(caps), caps


def _require_move(caps: dict[str, Any], kind: str, axes: dict[str, Optional[float]]) -> None:
    """Refuse a move the camera never advertised, with a message that says what
    it does support instead of relaying a vendor SOAP fault."""
    if (axes.get("pan") is not None or axes.get("tilt") is not None) and \
            not ptz_control.supports(caps, kind, "pan_tilt"):
        raise HTTPException(409, detail=f"Camera does not support {kind} pan/tilt")
    if axes.get("zoom") is not None and not ptz_control.supports(caps, kind, "zoom"):
        raise HTTPException(409, detail=f"Camera does not support {kind} zoom")


def _onvif_http(exc: onvif_media.OnvifError) -> HTTPException:
    return HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))


async def _lease(camera: Camera, request: Request, *, via: str) -> None:
    """Take/refresh the control lease and audit the START of a session only.

    A drag is ~40 requests a second's worth of ticks; a row each would bury the
    trail while telling a reader nothing the session row does not. What matters
    for a surveillance audit is that this person aimed this camera at this time.

    The lease acquire IS awaited — a 409 has to block the move, and it is a
    single fast Redis round trip. The audit write is NOT: it opens its own DB
    transaction under an advisory lock, and on the first press of every session
    that was sitting directly in front of the SOAP command, so the camera did
    not start moving until a Postgres write had committed. The lease TTL is a
    few seconds, so "first press of a session" happens every time an operator
    pauses and resumes — which is exactly the intermittent "movement starts
    late" that shows up on real hardware. Fired detached instead: the row still
    lands, the camera moves now. A best-effort audit that loses a row to a
    process exit in that sub-second window is the right trade for a control
    surface; the reconcile-critical writes elsewhere stay awaited.
    """
    principal = await get_principal(request)
    started = await ptz_control.acquire(camera.slug, principal)
    # For the position history (services/ptz_track): the next change on this camera was this person's doing.
    await ptz_control.note_mover(camera.slug, {"preset": "preset", "home": "home"}.get(via, "operator"),
                                 ptz_control.holder_id(principal))
    if started:
        asyncio.create_task(_audit_session(request, principal, camera.slug, via))


async def _audit_session(request: Request, principal, slug: str, via: str) -> None:
    try:
        await audit_svc.record(
            request, principal, "camera.ptz_session",
            target=slug, detail={"via": via},
        )
    except Exception as exc:  # noqa: BLE001 — a lost audit row must not disturb control
        log.warning("ptz.session_audit_failed", slug=slug, error=str(exc))


# ─── Capabilities ─────────────────────────────────────────────────────────────

@router.get("/{camera_id}/ptz")
async def get_ptz_capabilities(
    camera_id: uuid.UUID,
    refresh: bool = Query(False, description="Re-probe the device instead of using the cache"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """What this camera can do, and who currently holds it.

    Readable by any authenticated user: knowing a camera is a PTZ dome is not
    privileged, and the UI needs it to decide whether to render the pad at all
    (greyed, with the reason, for someone without `ptz_control`).
    """
    camera = await _get_camera_or_404(camera_id, db)
    caps = await _capabilities(camera, db, refresh=refresh)
    return {
        "camera_id": str(camera.id),
        "slug": camera.slug,
        "capabilities": caps,
        "held_by": await ptz_control.current_holder(camera.slug),
        "move_timeout_seconds": settings.ptz_move_timeout,
        "default_speed": settings.ptz_default_speed,
    }


@router.post("/{camera_id}/ptz/detect", dependencies=_can_manage)
async def detect_ptz(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Force a re-probe. The button for "I swapped this camera for a dome" and
    for a firmware upgrade that renumbered profile tokens."""
    camera = await _get_camera_or_404(camera_id, db)
    caps = await _probe_and_store(camera, db)
    return {"camera_id": str(camera.id), "capabilities": caps}


# ─── Movement ─────────────────────────────────────────────────────────────────

@router.post("/{camera_id}/ptz/move", dependencies=_can_ptz)
async def ptz_move(
    camera_id: uuid.UUID,
    body: PtzMoveBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Continuous move. Returns immediately; the camera keeps moving.

    Three things stop it, in the order they are likely to fire: the client's
    Stop on mouse-up, the ONVIF Timeout carried on the command, and the
    server-side watchdog for firmwares that ignore that field. The client is
    expected to re-issue while a direction is held — see ptz_control's header
    for why holding one request open would be the wrong shape.
    """
    camera, target, token, caps = await _ptz_target(camera_id, db)
    axes = {"pan": body.pan, "tilt": body.tilt, "zoom": body.zoom}
    if all(v is None for v in axes.values()):
        raise HTTPException(422, detail="No axis to move")
    _require_move(caps, "continuous", axes)

    await _lease(camera, request, via="continuous")

    ip, port, user, password = target
    timeout = settings.ptz_move_timeout
    t0 = time.monotonic()
    try:
        await onvif_ptz.continuous_move(
            ip, port, user, password, token,
            pan=body.pan, tilt=body.tilt, zoom=body.zoom, timeout_s=timeout,
        )
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)

    # Armed only after the camera accepted the move — arming for a rejected
    # command would schedule a Stop for a move that never started.
    await ptz_control.arm(camera.slug, target, token, timeout)
    # One line per move, keyed by slug: total_ms is what the BROWSER waited for
    # a response; the paired onvif_ptz.timing line's soap_ms is what the CAMERA
    # cost. total_ms − soap_ms is our own overhead and should be a millisecond
    # or two. This is the pair to read when diagnosing lag — see the module note
    # in services/onvif_ptz.py.
    log.info("ptz.move", slug=camera.slug, total_ms=round((time.monotonic() - t0) * 1000, 1))
    return {"status": "moving", "stops_in_seconds": timeout}


@router.post("/{camera_id}/ptz/stop", dependencies=_can_ptz)
async def ptz_stop(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Stop both axes and hand the lease back.

    Deliberately NOT gated on the control lease, and not on "was it moving".
    Anyone with `ptz_control` may stop any camera: this is the control someone
    reaches for when a camera is visibly running away, and making them wait out
    another operator's lease to press it would be indefensible. Releasing is
    still holder-checked, so stopping someone else's camera does not steal their
    lease — it just stops the head.

    Two orderings here are load-bearing, and both were wrong in the first cut:

    **Disarm only AFTER the camera confirms the stop.** Standing the watchdog
    down first looks tidier, but if the Stop then fails on a network blip we
    have just cancelled the only thing left that would have stopped a moving
    camera. Leaving it armed means the watchdog retries the stop moments later,
    which is exactly what it is for. A redundant Stop on an already-stopped
    camera costs nothing.

    **Release the lease even when the stop fails.** The lease is our state, not
    the camera's. Holding it because we could not reach the device is backwards:
    if the camera might still be moving, the next operator needs to be able to
    grab it and issue their own stop, not wait out a TTL.
    """
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    ip, port, user, password = target
    try:
        await onvif_ptz.stop(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    else:
        await ptz_control.disarm(camera.slug)
    finally:
        await ptz_control.release(camera.slug, await get_principal(request))
    return {"status": "stopped"}


@router.post("/{camera_id}/ptz/step", dependencies=_can_ptz)
async def ptz_step(
    camera_id: uuid.UUID,
    body: PtzStepBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Relative move — a bounded nudge that ends on its own.

    Also the click-to-centre path: the browser converts a click's offset from
    frame centre into a pan/tilt fraction and sends it here. There is no ONVIF
    "centre on this point" operation; every VMS that offers the gesture does
    this conversion client-side, because only the client knows the rendered
    frame geometry.
    """
    camera, target, token, caps = await _ptz_target(camera_id, db)
    axes = {"pan": body.pan, "tilt": body.tilt, "zoom": body.zoom}
    if all(v is None for v in axes.values()):
        raise HTTPException(422, detail="No axis to move")
    _require_move(caps, "relative", axes)

    await _lease(camera, request, via="step")
    ip, port, user, password = target
    try:
        await onvif_ptz.relative_move(
            ip, port, user, password, token,
            pan=body.pan, tilt=body.tilt, zoom=body.zoom,
            speed=body.speed if body.speed is not None else settings.ptz_default_speed,
        )
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    return {"status": "stepped"}


@router.post("/{camera_id}/ptz/absolute", dependencies=_can_ptz)
async def ptz_absolute(
    camera_id: uuid.UUID,
    body: PtzAbsoluteBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Absolute move to a normalised position. Offered only when the camera
    advertises the absolute spaces — most budget domes do not."""
    camera, target, token, caps = await _ptz_target(camera_id, db)
    axes = {"pan": body.pan, "tilt": body.tilt, "zoom": body.zoom}
    if all(v is None for v in axes.values()):
        raise HTTPException(422, detail="No axis to move")
    _require_move(caps, "absolute", axes)

    await _lease(camera, request, via="absolute")
    ip, port, user, password = target
    try:
        await onvif_ptz.absolute_move(
            ip, port, user, password, token,
            pan=body.pan, tilt=body.tilt, zoom=body.zoom,
            speed=body.speed if body.speed is not None else settings.ptz_default_speed,
        )
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    return {"status": "moving"}


@router.get("/{camera_id}/ptz/status")
async def ptz_status(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Current position and whether the head is moving. Advisory — a camera
    that answers GetStatus with a fault is still perfectly controllable, so
    this degrades to `null` rather than failing the page."""
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    # The pad reads status on mount, before the operator presses anything, so
    # this doubles as the "camera is in play" signal that warms every worker's
    # ONVIF session (services/ptz_control.keepwarm_loop) ahead of the first
    # command — otherwise the first press lands on a cold worker and builds.
    await ptz_control.mark_warm(camera.slug)
    ip, port, user, password = target
    try:
        status = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        log.debug("ptz.status_unavailable", slug=camera.slug, error=str(exc))
        status = None
    zoom = (status or {}).get("zoom")
    model = ptz_calibrate.angle_model(camera.ptz_calibration)
    home = await _home_ref(camera, db) if model else None
    return {
        "camera_id": str(camera.id),
        "status": status,
        "held_by": await ptz_control.current_holder(camera.slug),
        # "3.2x" and the 1x/2x/4x… choices, from the calibration (null when it can't say — uncalibrated, or a
        # camera without absolute zoom to go to a chosen step)
        "zoom_scale": (ptz_calibrate.zoom_scale(camera.ptz_calibration, zoom)
                       if ptz_control.supports(_caps, "absolute", "zoom") else None),
        # degrees right of / up from Home and above level — null without measured angles
        "angles": ptz_calibrate.degrees_of(model, _caps, status, home) if model else None,
        "home_known": home is not None,
    }


# ─── Presets ──────────────────────────────────────────────────────────────────

@router.get("/{camera_id}/ptz/presets")
async def list_presets(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The camera's presets as VMS records them (name, position, thumbnail, Home, missing-on-camera), re-synced
    from the camera on every read. A camera that can't be asked still lists what VMS knows, with the reason —
    the preset buttons should not vanish because one SOAP call failed."""
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    ip, port, user, password = target
    camera_error = None
    try:
        listed = await onvif_ptz.get_presets(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        listed, camera_error = None, str(exc)
    rows = await ptz_presets.sync(db, camera.id, listed)
    if listed is None and not rows:
        raise HTTPException(status_code=502, detail=camera_error or "Camera did not answer")
    # Visit counts and last visit are movement history (ptz_history); without it they read as unknown, not 0.
    visits = await ptz_track.preset_visits(db, camera.id) if await _sees_history(request) else None
    none = {"visits": None, "last_visit": None}
    return {"presets": [{**ptz_presets.out(p), **(visits.get(p.token, {"visits": 0, "last_visit": None})
                                                  if visits is not None else none)} for p in rows],
            "camera_error": camera_error}


async def _fresh_jpeg(slug: str) -> Optional[bytes]:
    """A frame taken NOW (not the 10 s shared cache — a preset's thumbnail must show the view just saved)."""
    from ..services import snapshot as snapshot_svc
    try:
        return await snapshot_svc.decode_snapshot(settings.internal_rtsp_url(slug), slug)
    except Exception as exc:                            # noqa: BLE001 — a thumbnail is a nicety
        log.info("ptz.thumbnail_failed", slug=slug, error=str(exc)[:200])
        return None


async def _preset_or_404(db: AsyncSession, camera_id: uuid.UUID, preset_token: str) -> PtzPreset:
    from sqlalchemy import select
    row = (await db.execute(select(PtzPreset).where(PtzPreset.camera_id == camera_id,
                                                    PtzPreset.token == preset_token))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="No such preset on this camera")
    return row


@router.patch("/{camera_id}/ptz/presets/{preset_token}", dependencies=_can_manage)
async def patch_preset(
    camera_id: uuid.UUID,
    preset_token: str,
    body: PtzPresetPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Rename / mark Home / reorder — VMS-side only (models.PtzPresetPatch says why a rename never goes to the
    camera). One Home per camera."""
    from sqlalchemy import update
    camera = await _get_camera_or_404(camera_id, db)
    row = await _preset_or_404(db, camera.id, preset_token)
    changes = body.model_dump(exclude_none=True)
    if body.is_home:
        await db.execute(update(PtzPreset).where(PtzPreset.camera_id == camera.id, PtzPreset.token != preset_token)
                         .values(is_home=False))
    for k, v in changes.items():
        setattr(row, k, v.strip() if isinstance(v, str) else v)
    await db.commit()
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "ptz_preset", "action": "update", "preset_token": preset_token,
                                   **changes})
    return {"preset": ptz_presets.out(row)}


@router.get("/{camera_id}/ptz/presets/{preset_token}/thumbnail")
async def preset_thumbnail(
    camera_id: uuid.UUID,
    preset_token: str,
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response
    row = await _preset_or_404(db, camera_id, preset_token)
    if not row.thumbnail:
        raise HTTPException(404, detail="No thumbnail yet")
    return Response(row.thumbnail, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@router.post("/{camera_id}/ptz/presets/{preset_token}/thumbnail", dependencies=_can_manage)
async def refresh_preset_thumbnail(
    camera_id: uuid.UUID,
    preset_token: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Take the thumbnail again — only while the camera is AT that preset (else it would show another view)."""
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    row = await _preset_or_404(db, camera.id, preset_token)
    ip, port, user, password = target
    try:
        status = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    state, hit = ptz_track.classify(status, [{"token": row.token, "pan": row.pan, "tilt": row.tilt, "zoom": row.zoom}])
    if state != "at_preset":
        raise HTTPException(409, detail="The camera is not at this preset — go to it first, then refresh the picture")
    jpeg = await _fresh_jpeg(camera.slug)
    if not jpeg:
        raise HTTPException(502, detail="No picture from the stream right now")
    row.thumbnail, row.thumbnail_at = ptz_presets.make_thumbnail(jpeg), datetime.now(timezone.utc)
    await db.commit()
    return {"preset": ptz_presets.out(row)}


# ─── Per-preset AI: each preset is a fixed-camera view (services/ptz_views) ──

@router.get("/{camera_id}/ptz/presets/{preset_token}/analytics")
async def get_preset_analytics(
    camera_id: uuid.UUID,
    preset_token: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """This preset's zones + rules (the same shape as a camera's analytics config) and its reference pictures."""
    row = await _preset_or_404(db, camera_id, preset_token)
    return {"preset": ptz_presets.out(row), "analytics_config": row.analytics_config or {"regions": {}, "activities": []}}


@router.put("/{camera_id}/ptz/presets/{preset_token}/analytics", dependencies=_can_manage)
async def put_preset_analytics(
    camera_id: uuid.UUID,
    preset_token: str,
    body: AnalyticsConfigBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Replace this preset's zones + rules — validated exactly like a fixed camera's (PUT /cameras/{id}/analytics).
    The first preset given a rule switches the camera's AI to following its presets; it applies at once when the
    camera is confirmed at this preset (else at its next arrival)."""
    from sqlalchemy import select as sa_select

    from ..models import AnalyticsActivityType
    from ..services import activity_catalog, ptz_views
    camera = await _get_camera_or_404(camera_id, db)
    row = await _preset_or_404(db, camera.id, preset_token)
    catalog = {t.key: t for t in (await db.execute(sa_select(AnalyticsActivityType))).scalars().all()}
    unknown = sorted({a.type for a in body.activities if a.type not in catalog})
    if unknown:
        raise HTTPException(422, f"Unknown activity type(s): {', '.join(unknown)}")
    problems = activity_catalog.validate_activities(catalog, row.analytics_config or {}, body.activities, body.regions)
    if problems:
        raise HTTPException(422, "; ".join(problems))
    cfg = body.model_dump()
    row.analytics_config = cfg if (cfg.get("regions") or cfg.get("activities")) else None
    # Saved in the editor, which shows them on the preset's CURRENT picture: that is the check a moved view waited for.
    reviewed = row.analytics_review
    row.analytics_review, row.previous_reference = None, None
    await db.commit()
    await db.refresh(row)
    try:
        await ptz_views.push_now(camera.id)
    except Exception as exc:                            # noqa: BLE001 — saved; the reconcile loop applies it anyway
        log.warning("ptz.preset_analytics_push_failed", slug=camera.slug, error=str(exc)[:200])
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed", target=camera.slug,
        detail={"section": "preset_analytics", "preset": row.name, "preset_token": row.token,
                "regions": len(body.regions), "activities": len(body.activities),
                **({"reviewed": reviewed.get("state")} if isinstance(reviewed, dict) else {})},
    )
    return {"preset": ptz_presets.out(row), "analytics_config": row.analytics_config or {"regions": {}, "activities": []}}


@router.get("/{camera_id}/ptz/presets/{preset_token}/reference")
async def preset_reference(
    camera_id: uuid.UUID,
    preset_token: str,
    night: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """The preset's reference picture (day, or night with ?night=1) — what its zones are drawn on. Falls back to the
    other light, then the small thumbnail."""
    from fastapi.responses import Response
    row = await _preset_or_404(db, camera_id, preset_token)
    jpeg = (row.reference_night if night else row.reference) or row.reference or row.reference_night or row.thumbnail
    if not jpeg:
        raise HTTPException(404, detail="No picture of this preset yet — go to it and take one")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})


@router.post("/{camera_id}/ptz/presets/{preset_token}/reference", dependencies=_can_manage)
async def take_preset_reference(
    camera_id: uuid.UUID,
    preset_token: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Take the preset's reference picture now — only while the camera is AT that preset. Stored as the day or night
    picture by what it shows (an IR picture is grey)."""
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    row = await _preset_or_404(db, camera.id, preset_token)
    ip, port, user, password = target
    try:
        status = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    state, _hit = ptz_track.classify(status, [{"token": row.token, "pan": row.pan, "tilt": row.tilt, "zoom": row.zoom}])
    if state != "at_preset":
        raise HTTPException(409, detail="The camera is not at this preset — go to it first, then take the picture")
    jpeg = await _fresh_jpeg(camera.slug)
    if not jpeg:
        raise HTTPException(502, detail="No picture from the stream right now")
    from ..services import ptz_view_changes, ptz_views
    small = ptz_track.shrink(jpeg)
    now = datetime.now(timezone.utc)
    night = ptz_track.is_night(small)
    # Waiting for a first picture of a view re-stored outside VMS: this is it — the zones are lined up against it.
    handled, _review = await ptz_view_changes.after_first_picture(row, jpeg)
    if not handled:
        if night:
            row.reference_night, row.reference_night_at = small, now
        else:
            row.reference, row.reference_at = small, now
    if not row.thumbnail:
        row.thumbnail, row.thumbnail_at = ptz_presets.make_thumbnail(jpeg), now
    await db.commit()
    await db.refresh(row)
    if handled:
        try:
            await ptz_views.push_now(camera.id)
        except Exception as exc:                        # noqa: BLE001 — the reconcile loop applies it anyway
            log.warning("ptz.preset_reference_push_failed", slug=camera.slug, error=str(exc)[:200])
    return {"preset": ptz_presets.out(row), "night": night}


# ─── A preset's view changed under its zones; deleted presets' zones (services/ptz_view_changes) ─

@router.post("/{camera_id}/ptz/presets/{preset_token}/review", dependencies=_can_manage)
async def review_preset_zones(
    camera_id: uuid.UUID,
    preset_token: str,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """{"action": "accept"} — the zones are right on the new picture: the preset's rules run again.
    {"action": "undo"} — put the zones back where they were before they were moved with the view (stays in review)."""
    from ..services import ptz_view_changes, ptz_views
    camera = await _get_camera_or_404(camera_id, db)
    row = await _preset_or_404(db, camera.id, preset_token)
    review = row.analytics_review
    action = str((body or {}).get("action") or "")
    if not isinstance(review, dict):
        raise HTTPException(409, detail="Nothing to review on this preset")
    if action == "accept":
        if review.get("state") == "awaiting_picture":
            raise HTTPException(409, detail="There is no picture of the new view yet — go to the preset first")
        row.analytics_review, row.previous_reference = None, None
    elif action == "undo":
        if review.get("previous_config") is None:
            raise HTTPException(409, detail="There is nothing to go back to")
        row.analytics_config = review["previous_config"]
        row.analytics_review = {**review, "state": "review", "previous_config": None,
                                "note": "zones put back where they were before the view moved — check them"}
    else:
        raise HTTPException(422, detail="action must be accept or undo")
    await db.commit()
    await db.refresh(row)
    try:
        await ptz_views.push_now(camera.id)
    except Exception as exc:                            # noqa: BLE001 — the reconcile loop applies it anyway
        log.warning("ptz.preset_review_push_failed", slug=camera.slug, error=str(exc)[:200])
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "preset_analytics", "action": f"review_{action}", "preset": row.name,
                                   "was": review.get("state")})
    return {"preset": ptz_presets.out(row), "review": ptz_view_changes.review_out(row.analytics_review)}


@router.get("/{camera_id}/ptz/presets/{preset_token}/previous-reference")
async def preset_previous_reference(
    camera_id: uuid.UUID,
    preset_token: str,
    db: AsyncSession = Depends(get_db),
):
    """The view's picture from before it changed — the "before" of a review."""
    from fastapi.responses import Response
    row = await _preset_or_404(db, camera_id, preset_token)
    if not row.previous_reference:
        raise HTTPException(404, detail="No earlier picture")
    return Response(row.previous_reference, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})


async def _archived_or_404(db: AsyncSession, camera_id: uuid.UUID, archive_id: uuid.UUID):
    from ..models import PtzViewArchive
    a = await db.get(PtzViewArchive, archive_id)
    if a is None or a.camera_id != camera_id:
        raise HTTPException(404, detail="No such deleted preset")
    return a


@router.get("/{camera_id}/ptz/archived-views")
async def list_archived_views(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Zones + rules of this camera's deleted presets, newest first."""
    from sqlalchemy import select as sa_select

    from ..models import PtzViewArchive
    from ..services import ptz_view_changes
    await _get_camera_or_404(camera_id, db)
    rows = (await db.execute(sa_select(PtzViewArchive).where(PtzViewArchive.camera_id == camera_id)
                             .order_by(PtzViewArchive.archived_at.desc()))).scalars().all()
    return {"archived": [ptz_view_changes.archive_out(a) for a in rows]}


@router.get("/{camera_id}/ptz/archived-views/{archive_id}/picture")
async def archived_view_picture(
    camera_id: uuid.UUID,
    archive_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response
    a = await _archived_or_404(db, camera_id, archive_id)
    if not a.picture:
        raise HTTPException(404, detail="No picture was kept")
    return Response(a.picture, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.post("/{camera_id}/ptz/archived-views/{archive_id}/copy", dependencies=_can_manage)
async def copy_archived_view(
    camera_id: uuid.UUID,
    archive_id: uuid.UUID,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Copy a deleted preset's zones + rules onto preset {"preset_token"} — replacing its own only with
    {"replace": true}. Lined up with the preset's picture when it can be, and always left for a check ("review")."""
    from ..services import ptz_view_changes, ptz_views
    camera = await _get_camera_or_404(camera_id, db)
    a = await _archived_or_404(db, camera.id, archive_id)
    row = await _preset_or_404(db, camera.id, str((body or {}).get("preset_token") or ""))
    if ptz_presets.ai_summary(row.analytics_config) != {"zones": 0, "tripwires": 0, "rules": 0} \
            and not (body or {}).get("replace"):
        raise HTTPException(409, detail=f"“{row.name}” has zones or rules of its own — replace them?")
    review = await ptz_view_changes.copy_from_archive(row, a, ptz_control.holder_id(await get_principal(request)))
    await db.commit()
    await db.refresh(row)
    try:
        await ptz_views.push_now(camera.id)
    except Exception as exc:                            # noqa: BLE001
        log.warning("ptz.archive_copy_push_failed", slug=camera.slug, error=str(exc)[:200])
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "preset_analytics", "action": "copy_from_deleted", "from": a.name,
                                   "preset": row.name, "note": review.get("note")})
    return {"preset": ptz_presets.out(row)}


@router.delete("/{camera_id}/ptz/archived-views/{archive_id}", dependencies=_can_manage)
async def forget_archived_view(
    camera_id: uuid.UUID,
    archive_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    camera = await _get_camera_or_404(camera_id, db)
    a = await _archived_or_404(db, camera.id, archive_id)
    name = a.name
    await db.delete(a)
    await db.commit()
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "preset_analytics", "action": "forget_deleted", "preset": name})
    return {"status": "forgotten"}


@router.post("/{camera_id}/ptz/presets/{preset_token}/restore", dependencies=_can_manage)
async def restore_preset(
    camera_id: uuid.UUID,
    preset_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Put a preset the camera lost (reset / replaced) back: move to the position VMS stored, then SetPreset
    with the same token and name. Moves the camera, so it takes the control lease like any move."""
    camera, target, token, caps = await _ptz_target(camera_id, db)
    row = await _preset_or_404(db, camera.id, preset_token)
    if row.pan is None and row.tilt is None and row.zoom is None:
        raise HTTPException(409, detail="VMS has no stored position for this preset")
    if not (caps.get("absolute") or {}).get("pan_tilt"):
        raise HTTPException(409, detail="This camera can't be sent to a stored position (no absolute move)")
    await _lease(camera, request, via="preset")
    ip, port, user, password = target
    try:
        await onvif_ptz.absolute_move(ip, port, user, password, token, pan=row.pan, tilt=row.tilt, zoom=row.zoom,
                                      speed=settings.ptz_default_speed)
        await asyncio.sleep(4.0)                        # let the head arrive before storing "here" as the preset
        result = await onvif_ptz.set_preset(ip, port, user, password, token, row.name, row.token)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    new_token = result.get("preset_token") or row.token
    if new_token != row.token:
        row.token = new_token                           # a camera that insists on its own numbering
    row.missing_on_camera = False
    await db.commit()
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "ptz_preset", "action": "restore", "preset_token": row.token})
    return {"preset": ptz_presets.out(row)}


@router.post("/{camera_id}/ptz/presets/{preset_token}/goto", dependencies=_can_ptz)
async def goto_preset(
    camera_id: uuid.UUID,
    preset_token: str,
    body: PtzGotoBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Recall a preset. Operational, not configuration — `ptz_control`."""
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    await _lease(camera, request, via="preset")
    ip, port, user, password = target
    try:
        result = await onvif_ptz.goto_preset(
            ip, port, user, password, token, preset_token,
            body.speed if body.speed is not None else settings.ptz_default_speed,
        )
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    return {"status": "moving", **result}


@router.put("/{camera_id}/ptz/presets", dependencies=_can_manage)
async def save_preset(
    camera_id: uuid.UUID,
    body: PtzPresetBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Store the camera's current position as a preset.

    `camera_manage`, not `ptz_control`: this permanently changes where the
    camera can be sent by anyone afterwards, which is configuration. Audited
    for the same reason.
    """
    from sqlalchemy import select as sa_select

    from ..services import ptz_view_changes, ptz_views
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    ip, port, user, password = target
    principal = await get_principal(request)
    by = ptz_control.holder_id(principal)
    # Re-saving over a preset moves its VIEW: what its zones were drawn on, before it changes (phase 3).
    before = None
    if body.token:
        before = (await db.execute(sa_select(PtzPreset).where(PtzPreset.camera_id == camera.id,
                                                              PtzPreset.token == body.token))).scalar_one_or_none()
    old_zoom = before.zoom if before is not None else None
    old_pictures = (before.reference, before.reference_night, before.thumbnail) if before is not None else None
    try:
        result = await onvif_ptz.set_preset(
            ip, port, user, password, token, body.name, body.token
        )
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    new_token = str(result.get("preset_token") or "")
    if not body.token and new_token:
        # A NEW preset in a slot VMS still has a row for (a preset the camera lost, its number handed out again): that
        # row is another view — its zones go to the archive, not onto this preset.
        stale = (await db.execute(sa_select(PtzPreset).where(PtzPreset.camera_id == camera.id,
                                                             PtzPreset.token == new_token))).scalar_one_or_none()
        if stale is not None:
            kept = ptz_view_changes.archive_row(stale, by, "its number was given to a new preset")
            if kept is not None:
                db.add(kept)
            await db.delete(stale)
            await db.commit()
    # VMS's record: where it is (read back from the camera) + what it looks like.
    try:
        position = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError:
        position = None
    jpeg = await _fresh_jpeg(camera.slug)
    row = await ptz_presets.upsert_saved(
        db, camera.id, new_token, body.name, position, by, ptz_presets.make_thumbnail(jpeg) if jpeg else None)
    review = None
    if before is not None and row.id == before.id and jpeg:
        night = ptz_track.is_night(jpeg)
        day_ref, night_ref, thumb = old_pictures
        old = (night_ref if night else day_ref) or day_ref or night_ref or thumb
        review = await ptz_view_changes.apply_new_view(
            row, jpeg, old=old, zoom_changed=ptz_view_changes.zoom_moved(old_zoom, (position or {}).get("zoom")),
            reason=f"re-saved by {by}" if by else "re-saved", by=by)
        await db.commit()
        await db.refresh(row)
        if row.analytics_config:
            try:
                await ptz_views.push_now(camera.id)
            except Exception as exc:                    # noqa: BLE001 — the reconcile loop applies it anyway
                log.warning("ptz.preset_resave_push_failed", slug=camera.slug, error=str(exc)[:200])
    await audit_svc.record(
        request, principal, "camera.config_changed",
        target=camera.slug,
        detail={"section": "ptz_preset", "action": "update" if body.token else "save", **result,
                **({"zones": review["state"], "zones_note": review["note"]} if review else {})},
    )
    log.info("ptz.preset_saved", slug=camera.slug, **result)
    return {"status": "saved", **result, "preset": ptz_presets.out(row)}


@router.delete("/{camera_id}/ptz/presets/{preset_token}", dependencies=_can_manage)
async def delete_preset(
    camera_id: uuid.UUID,
    preset_token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    from sqlalchemy import select as sa_select

    from ..services import ptz_view_changes, ptz_views
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    ip, port, user, password = target
    principal = await get_principal(request)
    row = (await db.execute(sa_select(PtzPreset).where(PtzPreset.camera_id == camera.id,
                                                       PtzPreset.token == preset_token))).scalar_one_or_none()
    if row is None or not row.missing_on_camera:        # a missing one is gone from the camera already: just forget it
        try:
            await onvif_ptz.remove_preset(ip, port, user, password, token, preset_token)
        except onvif_media.OnvifError as exc:
            raise _onvif_http(exc)
    archived = None
    had_rules = False
    if row is not None:
        # Its zones + rules + picture are kept (phase 3): copy them onto another preset, or Forget them.
        archived = ptz_view_changes.archive_row(row, ptz_control.holder_id(principal), "preset deleted")
        if archived is not None:
            db.add(archived)
        had_rules = ptz_views.has_rules(row.analytics_config)
        await db.delete(row)
    await db.commit()
    if had_rules:
        try:
            await ptz_views.push_now(camera.id)         # it may have been the camera's only preset with rules
        except Exception as exc:                        # noqa: BLE001 — the reconcile loop applies it anyway
            log.warning("ptz.preset_delete_push_failed", slug=camera.slug, error=str(exc)[:200])
    await audit_svc.record(
        request, principal, "camera.config_changed",
        target=camera.slug,
        detail={"section": "ptz_preset", "action": "delete", "preset_token": preset_token,
                **({"zones_archived": str(archived.id)} if archived is not None else {})},
    )
    log.info("ptz.preset_deleted", slug=camera.slug, token=preset_token)
    # Patrol steps keep pointing at the deleted preset's id: they are skipped (and logged), never re-bound to the
    # next preset the camera gives this token (extensions/ptz_patrols/bind.py).
    return {"status": "deleted", "preset_token": preset_token,
            "archived": ptz_view_changes.archive_out(archived) if archived is not None else None}


# ─── Home ─────────────────────────────────────────────────────────────────────

@router.post("/{camera_id}/ptz/home", dependencies=_can_ptz)
async def goto_home(
    camera_id: uuid.UUID,
    body: PtzGotoBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    camera, target, token, caps = await _ptz_target(camera_id, db)
    if not (caps.get("home") or {}).get("supported"):
        raise HTTPException(409, detail="Camera reports no home position")
    await _lease(camera, request, via="home")
    ip, port, user, password = target
    try:
        result = await onvif_ptz.goto_home(
            ip, port, user, password, token,
            body.speed if body.speed is not None else settings.ptz_default_speed,
        )
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    # Every "Go to home" teaches VMS where home is (the camera won't say): once it stops there, recorded — so it
    # reads "⌂ Home" instead of "Off preset", and "20° left of Home" works without a separate Learn Home.
    asyncio.create_task(_note_home_on_arrival(camera.id, camera.slug))
    return result


@router.put("/{camera_id}/ptz/home", dependencies=_can_manage)
async def set_home(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Make the current position the camera's home. Configuration, and the one
    PTZ write an operator can make that silently changes what a patrol or an
    idle-return does later — so it is audited."""
    camera, target, token, _caps = await _ptz_target(camera_id, db)
    ip, port, user, password = target
    try:
        result = await onvif_ptz.set_home(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    # The camera keeps its home to itself (ONVIF can't read it back) — VMS remembers where it is, so views can be
    # aimed relative to it ("20° left of Home").
    try:
        here = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError:
        here = None
    await _remember_home(camera, db, here, await get_principal(request))
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed",
        target=camera.slug, detail={"section": "ptz_home", "action": "set"},
    )
    log.info("ptz.home_set", slug=camera.slug)
    return result


# ─── Home as a reference, aiming by degrees ───────────────────────────────────

async def _remember_home(camera: Camera, db: AsyncSession, here: Optional[dict], principal) -> Optional[dict]:
    if not here or here.get("pan") is None or here.get("tilt") is None:
        return None
    rec = {"pan": here["pan"], "tilt": here["tilt"], "zoom": here.get("zoom"),
           "at": datetime.now(timezone.utc).isoformat(), "by": ptz_control.holder_id(principal)}
    camera.camera_metadata = {**(camera.camera_metadata or {}), "ptz_home": rec}   # new dict: JSONB change tracked
    await db.commit()
    return rec


async def _note_home_on_arrival(camera_id: uuid.UUID, slug: str) -> None:
    """After GotoHomePosition: wait for the head to stop and record where — unless something else moved it in
    the meantime (then the place it stopped isn't home, and nothing is recorded)."""
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    try:
        resolved = await ptz_control._resolve_target(slug)   # noqa: SLF001
        if resolved is None:
            return
        ip, port, user, password, profile = resolved
        t0 = time.monotonic()
        await asyncio.sleep(1.5)
        here = None
        while time.monotonic() - t0 < 25:
            here = await onvif_ptz.get_status(ip, port, user, password, profile)
            if here and "MOVING" not in {str(here.get("moving_pan_tilt") or "").upper(),
                                         str(here.get("moving_zoom") or "").upper()}:
                break
            await asyncio.sleep(1.0)
        hint = await ptz_control.last_mover(slug)
        if not here or (hint and hint.get("cause") != "home"):
            return
        async with AsyncSessionLocal() as db:
            cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one()
            rec = {"pan": here.get("pan"), "tilt": here.get("tilt"), "zoom": here.get("zoom"),
                   "at": datetime.now(timezone.utc).isoformat(), "by": "go-to-home"}
            if rec["pan"] is None or rec["tilt"] is None:
                return
            cam.camera_metadata = {**(cam.camera_metadata or {}), "ptz_home": rec}
            await db.commit()
    except Exception as exc:                            # noqa: BLE001 — learning home is a nicety
        log.info("ptz.home_note_failed", slug=slug, error=str(exc)[:200])


async def _home_ref(camera: Camera, db: AsyncSession) -> Optional[dict]:
    """Where Home points: what VMS recorded at "Set current as home" / "Learn Home", else a VMS preset marked
    Home. None = unknown (a home set on the camera before VMS kept track, never learned)."""
    rec = (camera.camera_metadata or {}).get("ptz_home")
    if isinstance(rec, dict) and rec.get("pan") is not None and rec.get("tilt") is not None:
        return rec
    from sqlalchemy import select
    row = (await db.execute(select(PtzPreset).where(PtzPreset.camera_id == camera.id, PtzPreset.is_home.is_(True))
                            .limit(1))).scalar_one_or_none()
    if row is not None and row.pan is not None and row.tilt is not None:
        return {"pan": row.pan, "tilt": row.tilt, "zoom": row.zoom, "preset": row.token}
    return None


@router.post("/{camera_id}/ptz/home/learn", dependencies=_can_manage)
async def learn_home(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Send the camera to its own home and remember where that is — for homes set before VMS kept track.
    MOVES THE CAMERA (to home, where it stays)."""
    camera, target, token, caps = await _ptz_target(camera_id, db)
    if not (caps.get("home") or {}).get("supported"):
        raise HTTPException(409, detail="Camera reports no home position")
    await _lease(camera, request, via="home")
    ip, port, user, password = target
    try:
        await onvif_ptz.goto_home(ip, port, user, password, token, 1.0)
        here = None
        t0 = time.monotonic()
        await asyncio.sleep(1.0)
        while time.monotonic() - t0 < 20:               # until the head reports it has stopped
            here = await onvif_ptz.get_status(ip, port, user, password, token)
            if not here or "MOVING" not in {str(here.get("moving_pan_tilt") or "").upper(),
                                            str(here.get("moving_zoom") or "").upper()}:
                break
            await asyncio.sleep(0.7)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    rec = await _remember_home(camera, db, here, await get_principal(request))
    if rec is None:
        raise HTTPException(502, detail="The camera went home but didn't report where that is")
    return {"status": "learned", "home": rec}


@router.post("/{camera_id}/ptz/aim", dependencies=_can_ptz)
async def ptz_aim(
    camera_id: uuid.UUID,
    body: PtzAimBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Turn by degrees: a step from where it points, or a view relative to Home (services/ptz_calibrate)."""
    camera, target, token, caps = await _ptz_target(camera_id, db)
    if not ptz_control.supports(caps, "absolute", "pan_tilt"):
        raise HTTPException(409, detail="This camera can't be sent to a position (no absolute pan/tilt)")
    model = ptz_calibrate.angle_model(camera.ptz_calibration)
    if model is None:
        raise HTTPException(409, detail="Degrees need a calibration with measured angles — "
                                        "PTZ → Calibration → Calibrate again")
    home = await _home_ref(camera, db) if body.origin == "home" else None
    ip, port, user, password = target
    try:
        status = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    try:
        aim = ptz_calibrate.aim_target(model, caps, status or {}, home, body.right, body.up, body.origin,
                                       body.up_from_level)
    except ValueError as exc:
        hint = " — PTZ → Control → Learn Home" if body.origin == "home" else ""
        raise HTTPException(409, detail=f"{exc}{hint}")
    await _lease(camera, request, via="aim")
    try:
        await onvif_ptz.absolute_move(ip, port, user, password, token, pan=aim["pan"], tilt=aim["tilt"], zoom=None,
                                      speed=body.speed if body.speed is not None else settings.ptz_default_speed)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    return {"status": "moving", **aim}


# ─── Click-to-centre / drag-to-zoom ───────────────────────────────────────────

@router.post("/{camera_id}/ptz/centre", dependencies=_can_ptz)
async def ptz_centre(
    camera_id: uuid.UUID,
    body: PtzCentreBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Centre the picture on a clicked point (and zoom in on a drawn box), using this camera's measured field of
    view at its current zoom (services/ptz_calibrate). Uncalibrated cameras keep the old fixed-gain nudge and
    say so (`calibrated: false`) so the UI can offer Calibrate."""
    camera, target, token, caps = await _ptz_target(camera_id, db)
    # A newer click replaces the check of an older one — before anything is planned, so a correction still on its
    # way can't land on top of this move (or skew the position this plan starts from).
    old = _centre_checks.pop(camera.slug, None)
    if old and not old.done():
        old.cancel()
    ip, port, user, password = target
    try:
        status = await onvif_ptz.get_status(ip, port, user, password, token)
    except onvif_media.OnvifError:
        status = None
    plan = ptz_calibrate.plan_centre(camera.ptz_calibration, status, body.x, body.y, body.zoom_factor, caps)
    await _lease(camera, request, via="centre")
    speed = body.speed if body.speed is not None else settings.ptz_default_speed
    try:
        if plan["mode"] == "absolute":
            await onvif_ptz.absolute_move(ip, port, user, password, token, pan=plan["pan"], tilt=plan["tilt"],
                                          zoom=plan.get("zoom"), speed=speed)
        else:
            if not ptz_control.supports(caps, "relative", "pan_tilt"):
                raise HTTPException(409, detail="Camera does not support relative pan/tilt")
            zoom = plan.get("zoom") if ptz_control.supports(caps, "relative", "zoom") else None
            await onvif_ptz.relative_move(ip, port, user, password, token, pan=plan["pan"], tilt=plan["tilt"],
                                          zoom=zoom or None, speed=speed)
    except onvif_media.OnvifError as exc:
        raise _onvif_http(exc)
    checking = False
    before = _decode_frame(body.frame) if (body.frame and plan.get("calibrated") and not body.zoom_factor) else None
    if before:
        principal = await get_principal(request)
        _centre_checks[camera.slug] = asyncio.create_task(
            _check_centre(camera.id, camera.slug, before, body.x, body.y, status, ptz_control.holder_id(principal)))
        checking = True
    return {"status": "moving", "checking": checking, **plan}


# ─── After-click check ────────────────────────────────────────────────────────

_centre_checks: dict[str, asyncio.Task] = {}


def _decode_frame(b64: str) -> Optional[bytes]:
    import base64
    try:
        raw = base64.b64decode(b64.split(",", 1)[-1], validate=False)
    except Exception:                                   # noqa: BLE001 — a bad frame just means no check
        return None
    return raw if raw[:2] == b"\xff\xd8" else None      # JPEG only


async def _check_centre(camera_id: uuid.UUID, slug: str, before: bytes, x: float, y: float,
                        status_before: Optional[dict], holder: str) -> None:
    """Where did the click land? One correction if it missed; what it learned is saved on the calibration.
    Runs after the reply (the camera starts moving at once); never fails anything — it logs and gives up."""
    import json
    from sqlalchemy import select

    from .. import redis_client
    from ..db import AsyncSessionLocal
    from ..services import snapshot as snapshot_svc

    try:
        resolved = await ptz_control._resolve_target(slug)   # noqa: SLF001
        if resolved is None:
            return
        ip, port, user, password, profile = resolved
        async with AsyncSessionLocal() as db:
            cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one()
            cal, caps = cam.ptz_calibration, cam.ptz_capabilities or {}
        if not cal:
            return

        async def still_ours() -> bool:
            # Someone else (or a patrol, or a calibration) took the camera since the click: hands off.
            return (await ptz_control.current_holder(slug)) in (None, holder)

        async def status():
            return await onvif_ptz.get_status(ip, port, user, password, profile)

        async def move_abs(pan=None, tilt=None, zoom=None):
            if not await still_ours():
                raise RuntimeError("camera taken by someone else")
            await ptz_control.note_mover(slug, "operator", holder)
            await onvif_ptz.absolute_move(ip, port, user, password, profile, pan=pan, tilt=tilt, zoom=zoom,
                                          speed=1.0)

        async def move_rel(pan=None, tilt=None):
            if not await still_ours():
                raise RuntimeError("camera taken by someone else")
            await ptz_control.note_mover(slug, "operator", holder)
            await onvif_ptz.relative_move(ip, port, user, password, profile, pan=pan, tilt=tilt, zoom=None,
                                          speed=1.0)

        async def frame():
            return await snapshot_svc.decode_snapshot(settings.internal_rtsp_url(slug))   # RTSP: where the head IS

        res = await ptz_calibrate.verify_centre(ptz_calibrate.Hw(status, move_abs, move_rel, frame), cal, caps,
                                                before, x, y, status_before)
        learned = res.pop("learned", None)
        if learned:
            async with AsyncSessionLocal() as db:
                cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one()
                # Only onto the calibration it was measured against — a recalibration meanwhile starts fresh.
                if cam.ptz_calibration and cam.ptz_calibration.get("measured_at") == cal.get("measured_at"):
                    cam.ptz_calibration = {**cam.ptz_calibration, "learned": learned["learned"]}
                    await db.commit()
        r = await redis_client.get_redis()
        await r.set(f"ptz:centre_check:{slug}", json.dumps({**res, "at": datetime.now(timezone.utc).isoformat()}),
                    ex=3600)
        log.info("ptz.centre_checked", slug=slug, **{k: v for k, v in res.items() if k != "learned"})
    except asyncio.CancelledError:
        raise
    except Exception as exc:                            # noqa: BLE001 — a check is a nicety
        log.info("ptz.centre_check_failed", slug=slug, error=str(exc)[:200])
    finally:
        if _centre_checks.get(slug) is asyncio.current_task():
            _centre_checks.pop(slug, None)


# ─── Where it points + history ────────────────────────────────────────────────

@router.get("/ptz/positions")
async def ptz_positions_current(
    request: Request,
    ids: str = Query("", description="Comma list of camera ids"),
) -> dict[str, Any]:
    """Every listed camera's current position state (services/ptz_track) — one call for the whole live grid."""
    wanted = [i.strip() for i in ids.split(",") if i.strip()][:200]
    cur = await ptz_track.current_for(wanted)
    if not await _sees_history(request):
        cur = {k: _anonymous(v) for k, v in cur.items()}
    return {"positions": cur}


@router.get("/{camera_id}/ptz/position")
async def ptz_position(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    camera = await _get_camera_or_404(camera_id, db)
    cur = (await ptz_track.current_for([str(camera.id)])).get(str(camera.id))
    if not await _sees_history(request):
        cur = _anonymous(cur)
    # `view` = the preset the camera is CONFIRMED at (picture-checked, services/ptz_track) — what per-preset AI
    # follows; {"state": "none", "reason": …} while moving, off preset or still being checked.
    return {"camera_id": str(camera.id), "position": cur, "view": await ptz_track.current_view(camera.id)}


def _csv_list(v: Optional[str]) -> Optional[list[str]]:
    vals = [x.strip() for x in (v or "").split(",") if x.strip()]
    return vals or None


async def _camera_names(db: AsyncSession, ids: Optional[list] = None) -> dict[str, str]:
    from sqlalchemy import select
    q = select(Camera.id, Camera.name)
    if ids:
        q = q.where(Camera.id.in_(ids))
    return {str(i): n for i, n in (await db.execute(q)).all()}


async def _history_response(db: AsyncSession, camera_ids, since, until, state, cause, preset, actor, offset, limit,
                            fmt: str):
    from fastapi.responses import Response
    if fmt == "csv":
        rows, total = await ptz_track.history(db, camera_ids=camera_ids, since=since, until=until,
                                              states=_csv_list(state), causes=_csv_list(cause), preset_token=preset,
                                              actor=actor, offset=0, limit=50000)
        names = await _camera_names(db, camera_ids)
        body = ptz_track.to_csv([ptz_track.row_out(r, names) for r in rows])
        return Response(body, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="ptz-position-history.csv"'})
    rows, total = await ptz_track.history(db, camera_ids=camera_ids, since=since, until=until,
                                          states=_csv_list(state), causes=_csv_list(cause), preset_token=preset,
                                          actor=actor, offset=offset, limit=limit)
    names = await _camera_names(db, list({r.camera_id for r in rows}) or None) if rows else {}
    return {"items": [ptz_track.row_out(r, names) for r in rows], "total": total, "offset": offset, "limit": limit}


@router.get("/{camera_id}/ptz/positions", dependencies=_can_history)
async def ptz_position_history(
    camera_id: uuid.UUID,
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    state: Optional[str] = Query(None, description="Comma list: at_preset,off_preset,moving,unknown"),
    cause: Optional[str] = Query(None, description="Comma list: operator,preset,home,patrol,calibration,camera,unknown"),
    preset: Optional[str] = Query(None, description="A preset token"),
    actor: Optional[str] = Query(None, description="Who (substring)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=2000),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncSession = Depends(get_db),
):
    """Change rows, newest first, filtered and paged (`total` = all matching); `format=csv` = every match."""
    camera = await _get_camera_or_404(camera_id, db)
    return await _history_response(db, [camera.id], since, until, state, cause, preset, actor, offset, limit, format)


@router.get("/{camera_id}/ptz/position-at", dependencies=_can_history)
async def ptz_position_at(
    camera_id: uuid.UUID,
    at: datetime = Query(..., description="The moment to look up"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Where was this camera pointing at `at`? = the newest change at or before it, and since when."""
    camera = await _get_camera_or_404(camera_id, db)
    row = await ptz_track.position_at(db, camera.id, at)
    return {"at": at.isoformat(), "position": ptz_track.row_out(row) if row else None}


@router.get("/{camera_id}/ptz/positions/summary", dependencies=_can_history)
async def ptz_position_summary(
    camera_id: uuid.UUID,
    since: datetime = Query(...),
    until: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Share of the period spent at each preset / off preset / moving — was it watching what it should?"""
    camera = await _get_camera_or_404(camera_id, db)
    end = until or datetime.now(timezone.utc)
    return {"since": since.isoformat(), "until": end.isoformat(),
            "split": await ptz_track.summary(db, camera.id, since, end)}


@router.get("/ptz/history", dependencies=_can_history)
async def ptz_history_all(
    camera_ids: Optional[str] = Query(None, description="Comma list of camera ids (default: every camera)"),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    state: Optional[str] = Query(None),
    cause: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=2000),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncSession = Depends(get_db),
):
    """Position changes across cameras — "what were the PTZ cameras doing between 02:00 and 02:30?"."""
    ids = None
    if camera_ids:
        try:
            ids = [uuid.UUID(x) for x in _csv_list(camera_ids) or []]
        except ValueError:
            raise HTTPException(422, detail="camera_ids must be camera UUIDs")
    return await _history_response(db, ids, since, until, state, cause, None, actor, offset, limit, format)


@router.get("/ptz/overview")
async def ptz_overview(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Every PTZ camera on one page: where it points now, presets, calibration, last move. (Patrol state comes
    from the patrols extension's own /ptz/patrols/status — the core does not know patrols exist.)"""
    from sqlalchemy import func, select
    cams = (await db.execute(select(Camera).order_by(Camera.name))).scalars().all()
    ptz = [c for c in cams if (c.ptz_capabilities or {}).get("supported") is True]
    counts = dict((await db.execute(select(PtzPreset.camera_id, func.count()).where(
        PtzPreset.camera_id.in_([c.id for c in ptz]), PtzPreset.missing_on_camera.is_(False))
        .group_by(PtzPreset.camera_id))).all()) if ptz else {}
    missing = dict((await db.execute(select(PtzPreset.camera_id, func.count()).where(
        PtzPreset.camera_id.in_([c.id for c in ptz]), PtzPreset.missing_on_camera.is_(True))
        .group_by(PtzPreset.camera_id))).all()) if ptz else {}
    current = await ptz_track.current_for([str(c.id) for c in ptz])
    # The calibration column: each camera's run state (queued / running + step / done / failed + why), and its
    # place in the queue — what the page polls to show bulk calibration live.
    import json as _json
    from .. import redis_client
    rc = await redis_client.get_redis()
    runs = await rc.mget([_calib_key(c.slug) for c in ptz]) if ptz else []
    run_of = {c.id: (_json.loads(x) if x else None) for c, x in zip(ptz, runs)}
    order = {i["camera_id"]: n + 1 for n, i in enumerate(await _queue_items())}
    if not await _sees_history(request):
        current = {k: _anonymous(v) for k, v in current.items()}
    return {"cameras": [{
        "id": str(c.id), "name": c.name, "slug": c.slug, "enabled": c.enabled, "health_status": c.health_status,
        "position": current.get(str(c.id)), "presets": int(counts.get(c.id, 0)),
        "presets_missing": int(missing.get(c.id, 0)),
        "calibrated": bool(c.ptz_calibration), "calibrated_at": (c.ptz_calibration or {}).get("measured_at"),
        "calibration_angles": bool((c.ptz_calibration or {}).get("geometry")),
        "calibration_levels": len((c.ptz_calibration or {}).get("points") or []),
        "calibration_run": {k: v for k, v in (run_of.get(c.id) or {}).items() if k != "notes"} or None,
        "queue_position": order.get(str(c.id)),
        "max_presets": ((c.ptz_capabilities or {}).get("presets") or {}).get("max"),
    } for c in ptz]}


@router.put("/{camera_id}/ptz/presets/order", dependencies=_can_manage)
async def order_presets(
    camera_id: uuid.UUID,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """{"tokens": [...]} in the wanted order (drag to reorder) — also the order of the live view's recall
    buttons. Tokens not listed keep their place after the listed ones."""
    camera = await _get_camera_or_404(camera_id, db)
    tokens = [str(t) for t in (body or {}).get("tokens") or []][:1000]
    rows = await ptz_presets.rows(db, camera.id)
    pos = {t: i for i, t in enumerate(tokens)}
    for i, r in enumerate(sorted(rows, key=lambda r: (pos.get(r.token, len(tokens) + r.sort_order), r.name))):
        r.sort_order = i + 1
    await db.commit()
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "ptz_preset", "action": "reorder"})
    return {"presets": [ptz_presets.out(p) for p in await ptz_presets.rows(db, camera.id)]}


# ─── Calibration ──────────────────────────────────────────────────────────────

_CALIB_HOLDER = "system:calibration"
_calibrations: dict[str, asyncio.Task] = {}


def _calib_key(slug: str) -> str:
    return f"ptz:calib:{slug}"


async def _calib_state(slug: str, **fields: Any) -> None:
    import json
    from .. import redis_client
    r = await redis_client.get_redis()
    raw = await r.get(_calib_key(slug))
    cur = json.loads(raw) if raw else {}
    cur.update(fields)
    await r.set(_calib_key(slug), json.dumps(cur), ex=3600)


async def _hold(slug: str) -> None:
    """Hold the control lease for the calibration (operators get the usual 409; a patrol pauses, naming it)."""
    from .. import redis_client
    r = await redis_client.get_redis()
    await r.set(f"ptz:lock:{slug}", _CALIB_HOLDER, ex=max(15, settings.ptz_lock_ttl))
    await ptz_control.note_mover(slug, "calibration", None)


async def _run_calibration(camera_id: uuid.UUID, slug: str, by: str) -> None:
    from sqlalchemy import select

    from .. import redis_client
    from ..db import AsyncSessionLocal
    from ..services import snapshot as snapshot_svc

    resolved = await ptz_control._resolve_target(slug)   # noqa: SLF001
    if resolved is None:
        await _calib_state(slug, state="failed", error="no usable ONVIF address or credentials")
        return
    ip, port, user, password, profile = resolved
    async with AsyncSessionLocal() as db:
        caps = (await db.execute(select(Camera.ptz_capabilities).where(Camera.id == camera_id))).scalar_one() or {}
    # Fresh capabilities first: the movement LIMITS (caps["ranges"]) keep the test moves inside what the camera
    # accepts — a probe from before ranges were recorded has none, and i-PRO refuses an out-of-range move.
    try:
        fresh = await onvif_ptz.probe(ip, port, user, password)
        if fresh.get("supported"):
            fresh["probed_at"] = datetime.now(timezone.utc).isoformat()
            caps = fresh
            async with AsyncSessionLocal() as db:
                cam_row = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one()
                cam_row.ptz_capabilities = fresh
                await db.commit()
    except Exception as exc:                            # noqa: BLE001 — calibrate with what we had
        log.info("ptz.calibration_reprobe_failed", slug=slug, error=str(exc)[:200])
    notes: list = []

    async def status():
        await _hold(slug)
        return await onvif_ptz.get_status(ip, port, user, password, profile)

    async def move_abs(pan=None, tilt=None, zoom=None):
        await _hold(slug)
        if pan is None and tilt is None and zoom is None:
            return
        await onvif_ptz.absolute_move(ip, port, user, password, profile, pan=pan, tilt=tilt, zoom=zoom, speed=1.0)

    async def move_rel(pan=None, tilt=None):
        await _hold(slug)
        await onvif_ptz.relative_move(ip, port, user, password, profile, pan=pan, tilt=tilt, zoom=None, speed=1.0)

    async def frame():
        # RTSP only (no slug → decode_snapshot skips the HLS live edge): HLS trails the head by 2-4 s, and a
        # calibration frame must show where the head IS (alpha i-PRO, 2026-10-26 — see ptz_calibrate's docstring).
        return await snapshot_svc.decode_snapshot(settings.internal_rtsp_url(slug))

    async def progress(step: str):
        await _calib_state(slug, step=step)

    try:
        await _calib_state(slug, state="running", step="starting", error=None,
                           started_at=datetime.now(timezone.utc).isoformat(), by=by)
        doc = await ptz_calibrate.run(ptz_calibrate.Hw(status, move_abs, move_rel, frame, progress=progress), caps,
                                      notes=notes)
        doc["by"] = by
        async with AsyncSessionLocal() as db:
            cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one()
            cam.ptz_calibration = doc
            await db.commit()
        await _calib_state(slug, state="done", step=None, notes=notes[-40:],
                           finished_at=datetime.now(timezone.utc).isoformat())
        await audit_svc.log_event(action="camera.config_changed", actor_type="user", actor=by, target=slug,
                                  detail={"section": "ptz_calibration", "points": len(doc["points"]),
                                          "space": doc["space"]})
        log.info("ptz.calibrated", slug=slug, points=doc["points"])
    except asyncio.CancelledError:
        await _calib_state(slug, state="failed", error="cancelled")
        raise
    except Exception as exc:                            # noqa: BLE001 — the UI shows the reason
        log.warning("ptz.calibration_failed", slug=slug, error=str(exc)[:300], notes=notes[-10:])
        await _calib_state(slug, state="failed", error=str(exc)[:600], notes=notes[-40:])
    finally:
        r = await redis_client.get_redis()
        if await r.get(f"ptz:lock:{slug}") == _CALIB_HOLDER:
            await r.delete(f"ptz:lock:{slug}")
        _calibrations.pop(slug, None)


# ─── History retention (admin) ────────────────────────────────────────────────

@router.get("/ptz/settings", dependencies=_can_history)
async def ptz_settings(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """How long movement history is kept, and how much room it takes now (so an admin can see it growing)."""
    from sqlalchemy import text
    sizes: dict[str, Optional[int]] = {}
    for table in ("ptz_position_log", "ptz_patrol_events"):
        try:
            sizes[table] = int((await db.execute(text("SELECT pg_total_relation_size(CAST(:t AS regclass))"),
                                               {"t": table})).scalar() or 0)
        except Exception:                               # noqa: BLE001 — table absent (extension off): unknown
            await db.rollback()
            sizes[table] = None
    return {"history": await ptz_track.retention(), "limits": ptz_track.RETENTION_LIMITS,
            "defaults": ptz_track.RETENTION_DEFAULTS, "bytes": sizes}


@router.put("/ptz/settings", dependencies=[Depends(require_role("admin"))])
async def put_ptz_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Admin: {"history": {"operator_days": 90, "patrol_days": 14}} (clamped to RETENTION_LIMITS). Shorter takes
    effect at the next hourly prune."""
    from ..models import PtzSettings
    history = ptz_track.clean_retention((body or {}).get("history"))
    row = await db.get(PtzSettings, 1)
    if row is None:
        row = PtzSettings(id=1)
        db.add(row)
    principal = await get_principal(request)
    row.history, row.updated_at, row.updated_by = history, datetime.now(timezone.utc), ptz_control.holder_id(principal)
    await db.commit()
    await audit_svc.record(request, principal, "camera.config_changed", target="ptz",
                           detail={"section": "ptz_history_retention", **history})
    return {"history": history}


# ─── Calibration queue: many cameras, one at a time ───────────────────────────
#
# Cameras → PTZ: tick cameras (or "all not calibrated" / "all without angles") → Calibrate. They run ONE AT A
# TIME — each moves its camera for a few minutes (it isn't watching its area meanwhile) and decodes many frames
# — and the list shows each one's state live: queued (with its place), running (which step), done, failed (why).
# The queue lives in Valkey so it outlives a restart; a runner task in this process works it, guarded by a lock
# so two workers never calibrate at once. A camera someone is steering when its turn comes is put back at the
# end once, then skipped with a note.

_QUEUE_KEY = "ptz:calib:queue"
_QUEUE_LOCK = "ptz:calib:queue_runner"
_queue_task: Optional[asyncio.Task] = None


async def _queue_items() -> list[dict]:
    import json
    from .. import redis_client
    r = await redis_client.get_redis()
    return [json.loads(x) for x in await r.lrange(_QUEUE_KEY, 0, -1)]


async def _queue_camera(camera_id: str) -> Optional[Camera]:
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Camera).where(Camera.id == uuid.UUID(camera_id)))).scalar_one_or_none()


async def _queue_runner() -> None:
    import json

    from .. import redis_client
    r = await redis_client.get_redis()
    if not await r.set(_QUEUE_LOCK, "1", nx=True, ex=900):
        return                                          # another worker is already working the queue
    try:
        while True:
            raw = await r.lpop(_QUEUE_KEY)
            if raw is None:
                return
            await r.expire(_QUEUE_LOCK, 900)
            item = json.loads(raw)
            cam = await _queue_camera(item["camera_id"])
            if cam is None or not ptz_control.is_capable(cam.ptz_capabilities or {}):
                continue
            holder = await ptz_control.current_holder(cam.slug)
            if holder and holder != _CALIB_HOLDER:
                if item.get("tries", 0) < 1:            # someone is steering it: try again at the end, once
                    await r.rpush(_QUEUE_KEY, json.dumps({**item, "tries": item.get("tries", 0) + 1}))
                    await _calib_state(cam.slug, state="queued", step=None, error=None,
                                       note=f"{holder} was controlling it — trying again at the end")
                else:
                    await _calib_state(cam.slug, state="failed",
                                       error=f"skipped — {holder} was controlling the camera both times")
                continue
            await _hold(cam.slug)
            await _calib_state(cam.slug, state="running", step="starting", error=None, note=None, by=item["by"],
                               started_at=datetime.now(timezone.utc).isoformat())
            task = asyncio.create_task(_run_calibration(cam.id, cam.slug, item["by"]))
            _calibrations[cam.slug] = task
            try:
                await task
            except asyncio.CancelledError:
                # "Stop" cancels the camera's own calibration: carry on with the next one. Only a cancel of the
                # runner itself (shutdown) ends the loop.
                if asyncio.current_task().cancelling():
                    raise
            # (_run_calibration records done / failed itself)
    finally:
        await r.delete(_QUEUE_LOCK)


def _kick_queue() -> None:
    global _queue_task
    if _queue_task is None or _queue_task.done():
        _queue_task = asyncio.create_task(_queue_runner())


@router.get("/ptz/calibration-queue", dependencies=_can_manage)
async def calibration_queue() -> dict[str, Any]:
    """The queue (in order) and the camera calibrating now. Also restarts a queue a restart left behind."""
    items = await _queue_items()
    if items:
        _kick_queue()
    running = next((slug for slug, t in _calibrations.items() if not t.done()), None)
    return {"queue": [i["camera_id"] for i in items], "running_slug": running}


@router.post("/ptz/calibration-queue", dependencies=_can_manage)
async def enqueue_calibrations(body: dict, request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Add cameras ({"camera_ids": [...]}) to the end of the queue — skipping any already queued or running."""
    import json
    from sqlalchemy import select

    from .. import redis_client
    ids = [str(i) for i in (body or {}).get("camera_ids") or []][:500]
    try:
        wanted = [uuid.UUID(i) for i in ids]
    except ValueError:
        raise HTTPException(422, detail="camera_ids must be camera UUIDs")
    cams = (await db.execute(select(Camera).where(Camera.id.in_(wanted)))).scalars().all() if wanted else []
    by_id = {c.id: c for c in cams}
    queued = {i["camera_id"] for i in await _queue_items()}
    running = {slug for slug, t in _calibrations.items() if not t.done()}
    by = ptz_control.holder_id(await get_principal(request))
    r = await redis_client.get_redis()
    added, skipped = [], []
    for cid in wanted:
        cam = by_id.get(cid)
        if cam is None or not ptz_control.is_capable(cam.ptz_capabilities or {}):
            skipped.append({"camera_id": str(cid), "why": "not a PTZ camera"})
            continue
        if str(cid) in queued or cam.slug in running:
            skipped.append({"camera_id": str(cid), "why": "already queued or calibrating"})
            continue
        await r.rpush(_QUEUE_KEY, json.dumps({"camera_id": str(cid), "by": by, "tries": 0}))
        await _calib_state(cam.slug, state="queued", step=None, error=None, note=None, by=by,
                           queued_at=datetime.now(timezone.utc).isoformat())
        added.append(str(cid))
    if added:
        await audit_svc.record(request, await get_principal(request), "camera.config_changed", target="ptz",
                               detail={"section": "ptz_calibration", "action": "queue", "cameras": len(added)})
        _kick_queue()
    return {"added": added, "skipped": skipped}


@router.delete("/ptz/calibration-queue", dependencies=_can_manage)
async def clear_calibration_queue(request: Request, db: AsyncSession = Depends(get_db),
                                  camera_id: Optional[uuid.UUID] = Query(None),
                                  include_running: bool = Query(False)) -> dict[str, Any]:
    """Take one camera (camera_id) or every camera off the queue; include_running also stops the one running
    (it puts its camera back where it was, as a finished calibration does)."""
    import json
    from sqlalchemy import select

    from .. import redis_client
    r = await redis_client.get_redis()
    removed = []
    for raw in await r.lrange(_QUEUE_KEY, 0, -1):
        item = json.loads(raw)
        if camera_id is None or item["camera_id"] == str(camera_id):
            await r.lrem(_QUEUE_KEY, 1, raw)
            removed.append(item["camera_id"])
    if removed:
        rows = (await db.execute(select(Camera.id, Camera.slug).where(
            Camera.id.in_([uuid.UUID(x) for x in removed])))).all()
        for _cid, slug in rows:
            await _calib_state(slug, state="cancelled", step=None, error=None, note="taken off the queue")
    stopped = None
    if include_running:
        for slug, t in list(_calibrations.items()):
            if t.done():
                continue
            if camera_id is not None:
                cam = await _get_camera_or_404(camera_id, db)
                if cam.slug != slug:
                    continue
            t.cancel()
            stopped = slug
    return {"removed": removed, "stopped": stopped}


@router.post("/{camera_id}/ptz/calibrate", dependencies=_can_manage)
async def start_calibration(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Measure this camera's field of view for click-to-centre. MOVES THE CAMERA for a few minutes (small pan/tilt
    steps at up to eight zoom levels, then a few tilts for the angles) and puts it back where it was.
    Configuration → `camera_manage`."""
    camera, _target, _token, _caps = await _ptz_target(camera_id, db)
    holder = await ptz_control.current_holder(camera.slug)
    if camera.slug in _calibrations or holder == _CALIB_HOLDER:
        raise HTTPException(409, detail="A calibration is already running on this camera")
    if holder:
        raise HTTPException(409, detail="Someone is controlling this camera right now — try when they are done")
    principal = await get_principal(request)
    by = ptz_control.holder_id(principal)
    await _hold(camera.slug)
    await _calib_state(camera.slug, state="running", step="starting", error=None, by=by,
                       started_at=datetime.now(timezone.utc).isoformat())
    _calibrations[camera.slug] = asyncio.create_task(_run_calibration(camera.id, camera.slug, by))
    return {"status": "running"}


@router.get("/{camera_id}/ptz/calibration")
async def get_calibration(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    import json
    from .. import redis_client
    camera = await _get_camera_or_404(camera_id, db)
    r = await redis_client.get_redis()
    raw = await r.get(_calib_key(camera.slug))
    check = await r.get(f"ptz:centre_check:{camera.slug}")
    return {"calibration": camera.ptz_calibration, "run": json.loads(raw) if raw else None,
            "last_check": json.loads(check) if check else None}


@router.delete("/{camera_id}/ptz/calibration", dependencies=_can_manage)
async def clear_calibration(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    camera = await _get_camera_or_404(camera_id, db)
    camera.ptz_calibration = None
    await db.commit()
    await audit_svc.record(request, await get_principal(request), "camera.config_changed", target=camera.slug,
                           detail={"section": "ptz_calibration", "action": "clear"})
    return {"status": "cleared"}
