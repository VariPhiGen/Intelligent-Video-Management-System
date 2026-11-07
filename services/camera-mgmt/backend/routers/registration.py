"""registration.py — calibrating a camera against a site plan.

Placement answers "where is this camera"; registration answers "what ground
does it cover". The difference is a homography fitted from landmark pairs an
operator clicks in the Map tab, and it is the prerequisite for drawing a
detection region once on the plan and projecting it into every camera that sees
it (plan Section 22.7) rather than redrawing it per camera.

Mounted separately from cameras.py for the same reason ptz.py is: that file is
already 2k lines, and this is its own concern with its own failure modes.

**The client fits, this service judges.** The least-squares solve lives in the
browser — it needs a numeric dependency this service deliberately does not
carry, and the operator gets the reprojection error updating live as they place
the fourth pin instead of after a round trip. None of that is trusted here. On
arrival the stored pairs are re-measured against the submitted matrix and the
transform is checked for the degeneracies that fit a set of pairs perfectly and
still describe an unusable view: collapsed onto a line, mirrored because frame
and plan points were entered the wrong way round, or blown up because the
landmarks were nearly collinear. See services/homography for the list and why
each one is there.

**Registration is configuration, so it sits on `camera_manage`,** the same
capability as saving a preset or a home position — not on `ptz_control`, which
is the operational act of moving a camera. An operator who can aim a camera
does not thereby get to redefine what the map believes it covers.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import (
    Camera,
    CameraRegistration,
    CameraRegistrationBody,
    CameraRegistrationResponse,
    LandmarkPair,
    REGISTRATION_MAX_RMS,
    Sitemap,
)
from ..security import get_principal
from ..services import audit as audit_svc
from ..services import homography as hg
from ..services.policy import require_capability
# Camera lookup owns the 404 wording the SPA deep-links on; imported rather
# than re-implemented so the two surfaces cannot drift. cameras.py does not
# import this module, so there is no cycle.
from .cameras import _get_camera_or_404

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/cameras", tags=["registration"])
plan_router = APIRouter(prefix="/sitemaps", tags=["registration"])

_can_manage = [Depends(require_capability("camera_manage"))]


def _to_response(row: CameraRegistration, *, scale_m: float | None) -> CameraRegistrationResponse:
    """Shape one stored row for the Map tab.

    `scale_m` is metres per plan unit, or None on an uncalibrated plan. The
    metre figure is left None rather than defaulted to 0 in that case: a zero
    would read as a perfect fit on the very plans where accuracy is least
    verifiable.
    """
    return CameraRegistrationResponse(
        id=row.id,
        camera_id=row.camera_id,
        sitemap_id=row.sitemap_id,
        preset_token=row.preset_token,
        homography=row.homography,
        landmark_pairs=[LandmarkPair(**pair) for pair in row.landmark_pairs],
        reprojection_error=row.reprojection_error,
        reprojection_error_m=(
            row.reprojection_error * scale_m if scale_m is not None else None
        ),
        footprint=row.footprint,
        calibrated_at=row.calibrated_at,
        calibrated_by=row.calibrated_by,
    )


async def _sitemap_or_404(sitemap_id: int, db: AsyncSession) -> Sitemap:
    """The plan being registered against. A 404 here means the operator's map
    was deleted mid-calibration, which is worth saying plainly rather than
    failing later on a foreign key."""
    plan = (await db.execute(select(Sitemap).where(Sitemap.id == sitemap_id))).scalars().first()
    if plan is None:
        raise HTTPException(404, "Sitemap not found")
    return plan


@router.get("/{camera_id}/registration", response_model=list[CameraRegistrationResponse])
async def list_camera_registrations(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[CameraRegistrationResponse]:
    """Every plan and view this camera is calibrated against.

    A list rather than a single object even today, when a fixed camera has at
    most one: a PTZ gains a row per calibrated preset, and a site with two
    plans (a ground floor and a yard) can legitimately register one camera
    against both.
    """
    camera = await _get_camera_or_404(camera_id, db)
    rows = (await db.execute(
        select(CameraRegistration)
        .where(CameraRegistration.camera_id == camera.id)
        .order_by(CameraRegistration.sitemap_id, CameraRegistration.preset_token)
    )).scalars().all()

    # Scale is per plan, so resolve each plan once rather than per row.
    scales: dict[int, float | None] = {}
    for row in rows:
        if row.sitemap_id not in scales:
            plan = (await db.execute(
                select(Sitemap).where(Sitemap.id == row.sitemap_id)
            )).scalars().first()
            scales[row.sitemap_id] = hg.plan_scale_metres(plan.calibration if plan else None)
    return [_to_response(row, scale_m=scales[row.sitemap_id]) for row in rows]


@plan_router.get("/{sitemap_id}/registrations", response_model=list[CameraRegistrationResponse])
async def list_plan_registrations(
    sitemap_id: int,
    db: AsyncSession = Depends(get_db),
) -> list[CameraRegistrationResponse]:
    """Every camera registered on this plan — the Map tab's coverage layer.

    One query for the whole plan, because the alternative is a request per
    camera dot on every map render.
    """
    plan = await _sitemap_or_404(sitemap_id, db)
    scale_m = hg.plan_scale_metres(plan.calibration)
    rows = (await db.execute(
        select(CameraRegistration)
        .where(CameraRegistration.sitemap_id == sitemap_id)
        .order_by(CameraRegistration.camera_id, CameraRegistration.preset_token)
    )).scalars().all()
    return [_to_response(row, scale_m=scale_m) for row in rows]


@router.put("/{camera_id}/registration", response_model=CameraRegistrationResponse,
            dependencies=_can_manage)
async def upsert_camera_registration(
    camera_id: uuid.UUID,
    body: CameraRegistrationBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraRegistrationResponse:
    """Create or replace this camera's registration for one plan and view.

    Idempotent on (camera, plan, preset): recalibrating replaces the row rather
    than accumulating history, because a superseded homography has no consumer
    — zones are re-projected from whichever is current, and keeping the old one
    would only offer a way to project them from a view nobody is looking
    through any more.

    Validation order is deliberate. The cheap identity checks (does the plan
    exist, does this camera actually have the PTZ preset being claimed) run
    before the geometry, so an operator who picked the wrong plan is told that
    instead of being told their landmarks disagree.
    """
    camera = await _get_camera_or_404(camera_id, db)
    plan = await _sitemap_or_404(body.sitemap_id, db)

    # A preset token on a camera that cannot move is an orphan: nothing will
    # ever recall that view, so the registration could never become active.
    # Refused rather than stored, since a stored one looks like working
    # coverage on the map.
    if body.preset_token:
        ptz_supported = (camera.ptz_capabilities or {}).get("supported") is True
        if not ptz_supported:
            raise HTTPException(
                409,
                "This camera has no PTZ support, so it has no presets to register against. "
                "Leave the preset empty to register its fixed view.",
            )

    pairs = [(p.fx, p.fy, p.mx, p.my) for p in body.landmark_pairs]
    try:
        rms, footprint = hg.validate(body.homography, pairs, max_rms=REGISTRATION_MAX_RMS)
    except hg.HomographyError as exc:
        # 422 with the operator-facing reason: these messages are written for
        # the person placing pins and are surfaced in the calibration card.
        raise HTTPException(422, str(exc)) from exc

    principal = await get_principal(request)
    actor = getattr(principal, "username", None) or getattr(principal, "sub", None)

    existing = (await db.execute(
        select(CameraRegistration).where(
            CameraRegistration.camera_id == camera.id,
            CameraRegistration.sitemap_id == body.sitemap_id,
            CameraRegistration.preset_token == body.preset_token,
        )
    )).scalars().first()

    if existing is None:
        existing = CameraRegistration(
            camera_id=camera.id,
            sitemap_id=body.sitemap_id,
            preset_token=body.preset_token,
        )
        db.add(existing)
    existing.homography = body.homography
    existing.landmark_pairs = [p.model_dump() for p in body.landmark_pairs]
    existing.reprojection_error = rms
    existing.footprint = [[x, y] for x, y in footprint]
    existing.calibrated_at = datetime.now(timezone.utc)
    existing.calibrated_by = actor

    await db.commit()
    await db.refresh(existing)

    scale_m = hg.plan_scale_metres(plan.calibration)
    log.info(
        "camera.registered", slug=camera.slug, sitemap_id=body.sitemap_id,
        preset=body.preset_token or "(fixed)", pairs=len(pairs),
        rms=round(rms, 5), rms_m=round(rms * scale_m, 3) if scale_m else None,
    )
    await audit_svc.record(
        request, principal, "camera.registered", target=camera.slug,
        detail={
            "sitemap_id": body.sitemap_id,
            "preset_token": body.preset_token,
            "landmark_count": len(pairs),
            "reprojection_error": round(rms, 6),
        },
    )
    return _to_response(existing, scale_m=scale_m)


@router.delete("/{camera_id}/registration/{registration_id}", status_code=204,
               dependencies=_can_manage)
async def delete_camera_registration(
    camera_id: uuid.UUID,
    registration_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Drop one registration.

    Scoped by camera as well as id so a mistyped id cannot delete another
    camera's calibration — the id alone is enough for the query, and that is
    exactly why it is not enough for the route.

    Zones already projected from this registration are not touched: they live
    in the camera's own `analytics_config` and remain valid for as long as the
    camera has not moved. Removing the registration removes the ability to
    re-project them, which is the honest consequence, not the ability to keep
    detecting.
    """
    camera = await _get_camera_or_404(camera_id, db)
    row = (await db.execute(
        select(CameraRegistration).where(
            CameraRegistration.id == registration_id,
            CameraRegistration.camera_id == camera.id,
        )
    )).scalars().first()
    if row is None:
        raise HTTPException(404, "Registration not found for this camera")

    sitemap_id, preset_token = row.sitemap_id, row.preset_token
    await db.execute(delete(CameraRegistration).where(CameraRegistration.id == registration_id))
    await db.commit()

    log.info("camera.registration_removed", slug=camera.slug, sitemap_id=sitemap_id,
             preset=preset_token or "(fixed)")
    await audit_svc.record(
        request, await get_principal(request), "camera.registration_removed",
        target=camera.slug,
        detail={"sitemap_id": sitemap_id, "preset_token": preset_token},
    )
    # Explicit empty response: FastAPI refuses a 204 route that declares a body,
    # and a `-> None` annotation counts as one. Same shape as the other 204
    # deletes in this service (datasets.py, discovery.py).
    return Response(status_code=204)
