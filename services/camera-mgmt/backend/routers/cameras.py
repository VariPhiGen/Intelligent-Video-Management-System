"""
cameras.py — Camera CRUD, bulk upload, health, and snapshot endpoints

Concurrency safety:
  PostgreSQL advisory locks (pg_try_advisory_xact_lock) guard slug generation
  so that concurrent POST /cameras or bulk uploads cannot produce duplicate
  slugs even if multiple uvicorn workers race.  The lock key is derived from
  the hash of the slug string.

  For UPDATE operations that change rtsp_url the relay path is patched
  atomically: DB update first, then MediaMTX patch.  If the MediaMTX call
  fails, the DB change has already committed (the new URL will be picked up by
  the health monitor's next reconnect cycle anyway).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..crypto import decrypt, encrypt
from ..db import get_db
from ..models import (
    ActivityTypesBody,
    AnalyticsActivityType,
    AnalyticsConfigBody,
    BulkRowResult,
    BulkUploadResponse,
    Camera,
    CameraCreate,
    CameraHealthResponse,
    CameraResponse,
    CameraStage,
    CameraStatusEvent,
    CameraUpdate,
    CameraUptimeResponse,
    EncoderUpdateBody,
    HealthStatus,
    ImagingUpdateBody,
    MasksBody,
    OnvifCredentialsBody,
    OsdUpdateBody,
    RecordingScheduleBody,
    SubTrackBody,
    LAWFUL_BASES,
    STQC_STATUSES,
    SystemHealthResponse,
    UptimeInterval,
    generate_slug,
    sub_recording_name,
    validate_lawful_basis,
    validate_stqc_status,
)
from .. import redis_client
from ..security import get_principal, require_role
from ..urlutil import host_of
from ..services import audit as audit_svc
from ..services.audit import actor_of
from ..services import health as health_svc
from ..services import motion_client, nvr_client, onvif_device, onvif_media, relay
from ..services import substream, tracks
from ..services import smartsearch_sync
from ..services import schedule as schedule_svc
from ..services.deepstream import deepstream_config
from ..services.deepstream_client import sync_now
from ..services import analytics_client
from ..services import activity_catalog
from ..services.policy import require_capability

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/cameras", tags=["cameras"])

# Camera lifecycle + per-camera configuration is gated by the grantable
# 'camera_manage' capability, so an admin can delegate it (e.g. to a supervisor
# covering a shift) under Administration → Roles & permissions without a code
# change. Two operations stay behind the fixed admin role: deleting a camera
# (it can purge recorded footage) and editing the global detection-type
# vocabulary (system-wide, not per-camera). Reads inherit the router's
# authenticated-principal guard from main.py.
_can_manage = [Depends(require_capability("camera_manage"))]
_admin_only = [Depends(require_role("admin"))]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_response(camera: Camera) -> CameraResponse:
    return CameraResponse.from_orm(camera, settings.local_rtsp_url(camera.slug))


async def _get_camera_or_404(camera_id: uuid.UUID, db: AsyncSession) -> Camera:
    # Registered rows only: pre-registration (discovery staging) rows share the
    # table but are not cameras yet — they are managed via /api/discovery.
    result = await db.execute(
        select(Camera).where(
            Camera.id == camera_id,
            Camera.stage == CameraStage.REGISTERED.value,
        )
    )
    camera = result.scalar_one_or_none()
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return camera


async def _sync_recording_tracks(camera: Camera, *, restart: bool = False) -> None:
    """Bring the NVR's workers for EVERY one of this camera's tracks into line.

    Recording state is per track, and `tracks_for` is the single definition of
    what a camera's tracks are (services/tracks.py) — so anything that changes
    how a camera records has to fan out through it rather than acting on the
    bare slug. A caller that touches only `camera.slug` leaves `<slug>_sub`
    running on its previous settings.

    That is not a cosmetic drift for privacy masks. The mask endpoint restarts
    the worker precisely because masks are burned in at record time and the
    reconcile loop cannot heal them: reconcile only ADDS tracks it finds
    missing, and a sub that is already recording is never missing, so it kept
    recording unmasked indefinitely. Playback then ranks the sub as the cheaper
    track — so the redacted region was preferentially served from the one copy
    that does not redact it.

    ``restart`` removes each worker before re-adding it, which is what makes a
    mask change take effect (~2-5 s gap per track). Without it this only starts
    or stops workers to match the camera's desired state.

    Best-effort, like every other NVR call here: the reconcile loop heals a
    miss on its next pass.
    """
    if not settings.nvr_sync_enabled:
        return
    should_record = (
        camera.enabled
        and camera.recording
        and schedule_svc.is_active(camera.recording_schedule)
    )
    sub_name = sub_recording_name(camera.slug)
    # Every name this camera could be recording under, not just the ones it
    # should be: a sub switched off still has a worker to stop.
    wanted = {t.recording_name for t in tracks.tracks_for(camera)} if should_record else set()
    for name in (camera.slug, sub_name):
        if name in wanted:
            if restart:
                await nvr_client.remove_camera(name)
            is_sub = name == sub_name
            await nvr_client.add_camera(
                name,
                retention_days=(tracks.sub_retention_days(camera, camera.retention_days)
                                if is_sub else camera.retention_days),
                masks=camera.privacy_masks,
                # 0 = never groom the sub. Grooming rewrites footage
                # keyframe-only, which is exactly what a scrub track must not
                # become.
                #
                # The main is normalised `or None` on purpose. 0 now means
                # "never groom" everywhere, but for a MAIN camera it has always
                # meant "no override" — PUT /cameras/{id} stores
                # `body.groom_after_days or None`, so no main row should hold 0.
                # This makes that explicit rather than trusting it: a stray 0
                # (a hand-written UPDATE, a future create path) would otherwise
                # silently exempt a camera from grooming under the new meaning.
                # Per-camera "never groom" for a main is a product decision, not
                # something a data accident should grant.
                groom_after_days=0 if is_sub else (camera.groom_after_days or None),
            )
        else:
            await nvr_client.remove_camera(name)


async def _teardown_recording_tracks(slug: str, *, purge: bool) -> bool:
    """Stop recording every track of a DELETED camera, optionally purging its
    footage. Returns whether the purge fully succeeded (True when not purging).

    Takes a slug, not a Camera, because the row is already gone by the time this
    runs — which is also why nothing here consults the camera's state. Both
    tracks are torn down unconditionally, and every call treats "not found" as
    success, so a camera that never had a sub costs one extra 404.

    Gating the sub on `sub_is_recordable(camera)` was wrong in both directions.
    A sub switched off minutes ago still has a live worker no later pass would
    find (deletion is the one path the reconcile loops cannot heal — the row
    that described the sub is gone). And footage recorded while the sub WAS on
    outlives the flag: purging only the main reported erasure as complete while
    a full low-res copy of exactly that footage stayed on disk, unreachable
    through the UI and with no camera row left to erase it through. For a DPDP
    erasure that is the difference between deleted and merely hidden.
    """
    if not settings.nvr_sync_enabled:
        return True
    sub_name = sub_recording_name(slug)
    # Stop the workers first so no new segment lands mid-purge.
    await nvr_client.remove_camera(slug)
    await nvr_client.remove_camera(sub_name)
    if not purge:
        return True
    # A purge that half-succeeded must not be reported as a purge: the audit
    # line it feeds is how an erasure request is answered, and it has to be
    # true about both tracks. Both are attempted before judging — a failed main
    # is no reason to leave the sub's copy behind.
    main_ok = await nvr_client.purge_recordings(slug)
    sub_ok = await nvr_client.purge_recordings(sub_name)
    if not (main_ok and sub_ok):
        log.error("delete_camera.purge_incomplete", slug=slug,
                  main_purged=main_ok, sub_purged=sub_ok)
    return main_ok and sub_ok


async def _ensure_unique_slug(slug: str, db: AsyncSession) -> None:
    """
    Use a PostgreSQL advisory lock to prevent two workers from inserting the
    same slug simultaneously.  The lock is held for the duration of the
    surrounding transaction.
    """
    lock_key = hash(slug) & 0x7FFFFFFF  # positive 32-bit int
    row = await db.execute(text(f"SELECT pg_try_advisory_xact_lock({lock_key})"))
    if not row.scalar():
        raise HTTPException(
            status_code=409,
            detail=f"Concurrent slug reservation conflict for '{slug}'; retry",
        )
    exists = await db.execute(select(Camera.id).where(Camera.slug == slug))
    if exists.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"Slug '{slug}' already in use")


# ─── POST /cameras ────────────────────────────────────────────────────────────

@router.post("", response_model=CameraResponse, status_code=201, dependencies=_can_manage)
async def create_camera(
    body: CameraCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    slug = body.slug or generate_slug(body.name)
    # DPDP: a registered camera must carry a valid lawful basis + purpose.
    lawful_basis, purpose = validate_lawful_basis(body.lawful_basis, body.purpose)
    # STQC posture is optional here by design — it defaults to "unknown" rather
    # than blocking registration (migration 023). Only stamp the attestation
    # when the caller actually asserted something: "unknown" is the absence of
    # an attestation, so recording a verifier against it would be a lie.
    principal = await get_principal(request)
    stqc_status = validate_stqc_status(body.stqc_status)
    stqc_attested = stqc_status != "unknown"
    # Same invariant the update path enforces: certificate details only exist
    # under "certified", so a camera can never register carrying evidence of a
    # certificate its own status doesn't claim.
    stqc_certified = stqc_status == "certified"

    try:
        async with db.begin():
            await _ensure_unique_slug(slug, db)
            camera = Camera(
                name=body.name,
                slug=slug,
                rtsp_url=body.rtsp_url,
                sensor_id=body.sensor_id,
                enabled=body.enabled,
                recording=body.recording,
                stage=CameraStage.REGISTERED.value,
                ip=host_of(body.rtsp_url),
                camera_metadata=body.metadata,
                lawful_basis=lawful_basis,
                purpose=purpose,
                notice_posted=body.notice_posted,
                stqc_status=stqc_status,
                stqc_certificate_no=body.stqc_certificate_no if stqc_certified else None,
                stqc_valid_until=body.stqc_valid_until if stqc_certified else None,
                stqc_verified_at=datetime.now(timezone.utc) if stqc_attested else None,
                stqc_verified_by=actor_of(principal)[1] if stqc_attested else None,
            )
            db.add(camera)
            await db.flush()
            await db.refresh(camera)
    except IntegrityError as exc:
        if "ix_cameras_rtsp_url" in str(exc.orig):
            raise HTTPException(status_code=409, detail="A camera with this RTSP URL already exists")
        raise HTTPException(status_code=409, detail="Camera could not be created due to a conflict")

    # Register in MediaMTX — outside the DB transaction so a MediaMTX failure
    # doesn't roll back the DB record.  The health monitor will re-register on
    # next poll if this call fails.
    if body.enabled:
        try:
            await relay.add_path(slug, body.rtsp_url)
        except Exception as exc:
            log.warning("create_camera.relay_failed", slug=slug, error=str(exc))
        # Start recording via the relay URL (best-effort; the reconcile loop
        # heals any miss — see services/nvr_client.py).
        if settings.nvr_sync_enabled and camera.recording:
            await nvr_client.add_camera(slug, masks=camera.privacy_masks)
        # Find out what second stream this camera offers, in the background —
        # probing takes seconds and must not delay creating the camera. Stores
        # only; it never starts recording on its own.
        asyncio.create_task(substream.resolve_in_background(camera.id))
        # Fast-path status: don't leave the UI on "unknown" until the next
        # 30s monitor poll — verify within seconds.
        asyncio.create_task(health_svc.check_camera_soon(camera.id))

    log.info("create_camera.ok", slug=slug)
    await audit_svc.record(
        request, await get_principal(request), "camera.created",
        target=slug, detail={"name": body.name},
    )
    return _build_response(camera)


# ─── GET /cameras ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[CameraResponse])
async def list_cameras(
    enabled: Optional[bool] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[CameraResponse]:
    stmt = (
        select(Camera)
        .where(Camera.stage == CameraStage.REGISTERED.value)
        .order_by(Camera.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if enabled is not None:
        stmt = stmt.where(Camera.enabled.is_(enabled))
    result = await db.execute(stmt)
    cameras = result.scalars().all()
    responses = [_build_response(c) for c in cameras]

    # Enrich with true uptime: MediaMTX's readyTime = when the stream last
    # became ready. One relay call for the whole page; best-effort (the list
    # must render even with MediaMTX down).
    #
    # The normalisation lives in relay.ready_since_of() because GET
    # /cameras/{id} answers the same field from a different relay call, and two
    # copies of this rule is how the two endpoints came to disagree.
    try:
        ready_at: dict[str, Optional[str]] = {}
        for p in await relay.list_active_paths():
            ready_at[p.get("name", "")] = relay.ready_since_of(p)
        for r in responses:
            r.ready_since = ready_at.get(r.slug)
    except Exception as exc:
        log.debug("list_cameras.uptime_enrich_failed", error=str(exc))

    return responses


# ─── GET /cameras/uptime/summary ─────────────────────────────────────────────
# One call for the whole inventory's "Uptime 7d" column — computing this
# per-camera via GET /{id}/uptime would be N requests per table refresh.
# NOTE: must be registered BEFORE /{camera_id} or the path would be parsed
# as a UUID and 422.

@router.get("/uptime/summary")
async def uptime_summary(
    hours: int = Query(default=168, ge=1, le=2208, description="Window (default 7 days)"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-camera uptime %% over the trailing window, computed like
    GET /{id}/uptime (monitored time only; None = never monitored in window)."""
    to_dt = datetime.now(tz=timezone.utc)
    from_dt = to_dt - timedelta(hours=hours)

    cam_ids = [
        r[0] for r in (
            await db.execute(
                select(Camera.id).where(Camera.stage == CameraStage.REGISTERED.value)
            )
        ).all()
    ]
    if not cam_ids:
        return {"hours": hours, "cameras": {}}

    # Seed status per camera: the last transition at/before the window start.
    seeds = dict(
        (
            await db.execute(
                select(CameraStatusEvent.camera_id, CameraStatusEvent.status)
                .where(
                    CameraStatusEvent.camera_id.in_(cam_ids),
                    CameraStatusEvent.changed_at <= from_dt,
                )
                .order_by(
                    CameraStatusEvent.camera_id,
                    CameraStatusEvent.changed_at.desc(),
                )
                .distinct(CameraStatusEvent.camera_id)
            )
        ).all()
    )
    events = (
        await db.execute(
            select(
                CameraStatusEvent.camera_id,
                CameraStatusEvent.status,
                CameraStatusEvent.changed_at,
            )
            .where(
                CameraStatusEvent.camera_id.in_(cam_ids),
                CameraStatusEvent.changed_at > from_dt,
                CameraStatusEvent.changed_at <= to_dt,
            )
            .order_by(CameraStatusEvent.camera_id, CameraStatusEvent.changed_at.asc())
        )
    ).all()

    per_cam: dict[uuid.UUID, list] = {cid: [] for cid in cam_ids}
    for cid, status, at in events:
        per_cam[cid].append((status, at))

    # Per-camera: overall uptime %% plus a bucketed series (one bucket per day
    # by default) that the inventory renders as a sparkline.
    buckets = max(1, min(hours // 24 or 1, 30))
    bucket_len = (to_dt - from_dt) / buckets

    out: dict[str, Any] = {}
    for cid in cam_ids:
        up = down = 0.0
        b_up = [0.0] * buckets
        b_mon = [0.0] * buckets

        def _accumulate(status: str, start: datetime, end: datetime) -> None:
            nonlocal up, down
            dur = (end - start).total_seconds()
            if dur <= 0:
                return
            is_up = status in _UP_STATUSES
            is_down = status in _DOWN_STATUSES
            if is_up:
                up += dur
            elif is_down:
                down += dur
            else:
                return
            # Spread the interval across the buckets it overlaps.
            i = int((start - from_dt) / bucket_len)
            cursor = start
            while cursor < end and i < buckets:
                b_end = from_dt + bucket_len * (i + 1)
                seg = (min(end, b_end) - cursor).total_seconds()
                b_mon[i] += seg
                if is_up:
                    b_up[i] += seg
                cursor = min(end, b_end)
                i += 1

        cur_status = seeds.get(cid, HealthStatus.UNKNOWN.value)
        cur_start = from_dt
        for status, at in per_cam[cid] + [(None, to_dt)]:
            _accumulate(cur_status, cur_start, at)
            cur_status, cur_start = status, at

        monitored = up + down
        out[str(cid)] = {
            "pct": round(up / monitored * 100, 1) if monitored > 0 else None,
            "series": [
                round(b_up[i] / b_mon[i] * 100, 1) if b_mon[i] > 0 else None
                for i in range(buckets)
            ],
        }

    return {"hours": hours, "buckets": buckets, "cameras": out}


# ─── GET /cameras/{id} ────────────────────────────────────────────────────────

@router.get("/{camera_id}", response_model=CameraResponse)
async def get_camera(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    camera = await _get_camera_or_404(camera_id, db)
    response = _build_response(camera)

    # SAME FIELD, SAME MEANING AS THE LIST. `ready_since` is documented on
    # CameraResponse as the camera's true "up since", with no hint that it is
    # list-only — but only list_cameras ever filled it in, so this endpoint
    # reported every streaming camera as having no ready time. Nothing read it
    # here (the SPA's useCameras() drives off the list), which is exactly why it
    # could sit wrong indefinitely.
    #
    # get_path_status, not list_active_paths: one camera needs one path, and
    # the list endpoint is paginated over every path in the relay.
    #
    # Best-effort, like the list. A detail view must still render with MediaMTX
    # down — this endpoint answered without touching the relay at all until
    # now, so letting it start failing on a relay outage would trade a null
    # field for an unavailable page.
    try:
        response.ready_since = (
            await relay.get_path_status(camera.slug)).get("ready_time")
    except Exception as exc:  # noqa: BLE001 — uptime is not worth a 5xx
        log.debug("get_camera.uptime_enrich_failed", slug=camera.slug,
                  error=str(exc))

    return response


def _search_state(camera: Camera) -> dict[str, Any]:
    """What a camera is collecting for Smart Search, as the audit trail records
    it. Indexing off with domains still stored means nothing is collected, so
    both halves are kept rather than folding them into one list."""
    return {"indexing": bool(camera.search_indexing),
            "domains": sorted(camera.search_domains or [])}


# ─── PUT /cameras/{id} ────────────────────────────────────────────────────────

@router.put("/{camera_id}", response_model=CameraResponse, dependencies=_can_manage)
async def update_camera(
    camera_id: uuid.UUID,
    body: CameraUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    # Resolved up front rather than at the audit call below: an STQC attestation
    # is stamped onto the row inside the transaction and needs the actor there.
    principal = await get_principal(request)
    try:
        async with db.begin():
            camera = await _get_camera_or_404(camera_id, db)
            rtsp_changed = body.rtsp_url is not None and body.rtsp_url != camera.rtsp_url
            enabled_changed = body.enabled is not None and body.enabled != camera.enabled
            recording_changed = body.recording is not None and body.recording != camera.recording
            motion_changed = (
                (body.motion_detection is not None
                 and body.motion_detection != camera.motion_detection)
                or (body.motion_sensitivity is not None
                    and body.motion_sensitivity != camera.motion_sensitivity)
            )
            search_changed = (
                (body.search_indexing is not None
                 and body.search_indexing != camera.search_indexing)
                or (body.search_domains is not None
                    and sorted(body.search_domains) != sorted(camera.search_domains or []))
            )
            # Taken before the assignments below overwrite it: the audit entry
            # has to say what collection was switched on or off, not only that
            # something under "search" moved.
            search_before = _search_state(camera)
            # 0 in the payload means "reset to appliance default" → stored NULL.
            new_retention = (body.retention_days or None) if body.retention_days is not None else None
            retention_changed = (
                body.retention_days is not None and new_retention != camera.retention_days
            )
            new_groom = (body.groom_after_days or None) if body.groom_after_days is not None else None
            groom_changed = (
                body.groom_after_days is not None and new_groom != camera.groom_after_days
            )

            if body.name is not None:
                camera.name = body.name
            if body.rtsp_url is not None:
                camera.rtsp_url = body.rtsp_url
                camera.ip = host_of(body.rtsp_url)  # keep the scan-dedupe key current
            if body.sensor_id is not None:
                camera.sensor_id = body.sensor_id
            if body.enabled is not None:
                camera.enabled = body.enabled
            if body.recording is not None:
                camera.recording = body.recording
            if body.motion_detection is not None:
                camera.motion_detection = body.motion_detection
            if body.motion_sensitivity is not None:
                camera.motion_sensitivity = body.motion_sensitivity
            if body.search_indexing is not None:
                camera.search_indexing = body.search_indexing
            if body.search_domains is not None:
                # Deduplicated and ordered so the stored value is comparable and
                # the immediate push below does not fire on a reordering.
                camera.search_domains = sorted(set(body.search_domains))
            if retention_changed:
                camera.retention_days = new_retention
            if body.retention_justification is not None:
                camera.retention_justification = body.retention_justification or None
            if body.lawful_basis is not None:
                lb = body.lawful_basis.strip()
                if lb and lb not in LAWFUL_BASES:
                    raise HTTPException(422, detail="Invalid lawful basis — one of: " + ", ".join(LAWFUL_BASES))
                camera.lawful_basis = lb or None
            if body.purpose is not None:
                camera.purpose = body.purpose.strip() or None
            if body.notice_posted is not None:
                camera.notice_posted = body.notice_posted
            # STQC: only a status change is an attestation, so only that restamps
            # verified_at/_by. Correcting a typo in the certificate number must
            # not make it look freshly verified. Moving to "unknown" clears the
            # attestation entirely — it is the retraction of one.
            if body.stqc_status is not None:
                new_stqc = validate_stqc_status(body.stqc_status)
                if new_stqc != camera.stqc_status:
                    camera.stqc_status = new_stqc
                    if new_stqc == "unknown":
                        camera.stqc_verified_at = None
                        camera.stqc_verified_by = None
                    else:
                        camera.stqc_verified_at = datetime.now(timezone.utc)
                        camera.stqc_verified_by = actor_of(principal)[1]
            if body.stqc_certificate_no is not None:
                camera.stqc_certificate_no = body.stqc_certificate_no.strip() or None
            if body.stqc_valid_until is not None:
                camera.stqc_valid_until = body.stqc_valid_until
            # Certificate details are meaningless — and actively misleading in the
            # posture report — under any status but "certified". An "unverified"
            # camera still showing a certificate number reads as evidence of
            # compliance nobody asserted, so the invariant is enforced here rather
            # than left to callers: the UI clears these fields too, but the API
            # must not depend on it.
            if camera.stqc_status != "certified":
                camera.stqc_certificate_no = None
                camera.stqc_valid_until = None
            if groom_changed:
                camera.groom_after_days = new_groom
            if body.metadata is not None:
                camera.camera_metadata = body.metadata

            await db.flush()
            await db.refresh(camera)
    except IntegrityError as exc:
        if "ix_cameras_rtsp_url" in str(exc.orig):
            raise HTTPException(status_code=409, detail="A camera with this RTSP URL already exists")
        raise HTTPException(status_code=409, detail="Camera could not be updated due to a conflict")

    slug = camera.slug

    # Sync relay state with the new config
    try:
        if not camera.enabled and enabled_changed:
            await relay.remove_path(slug)
            await redis_client.delete_stream_keys(slug)
        elif camera.enabled and enabled_changed:
            await relay.add_path(slug, camera.rtsp_url)
        elif camera.enabled and rtsp_changed:
            await relay.patch_path(slug, camera.rtsp_url)
            # The sub's URL is COMPOSED from the main's credentials at use time
            # (tracks.sub_track_url), so a password rotation or a host change
            # invalidates it too. Repointing only the main left the sub's relay
            # path authenticating with the old credentials — it stops pulling,
            # the path still exists, and nothing reports a problem.
            sub_url = tracks.sub_track_url(camera)
            if sub_url and tracks.sub_is_recordable(camera):
                await relay.patch_path(sub_recording_name(slug), sub_url)
    except Exception as exc:
        log.warning("update_camera.relay_sync_failed", slug=slug, error=str(exc))

    # Recording runs iff the camera is enabled AND its recording flag is on
    # AND its schedule (if any) is in-window right now — the reconcile loop
    # flips it at window edges. (The NVR records the relay URL, which is
    # slug-based — an rtsp_url change needs no NVR action.)
    if settings.nvr_sync_enabled and (enabled_changed or recording_changed):
        # Stored masks travel with the add, so a camera masked while disabled
        # records through the masked (re-encode) path on enable — the reconcile
        # loop won't heal this (the add puts the camera in the NVR's current
        # set, so it's excluded from reconcile's enabled-minus-current re-add).
        # Per track, so an enabled camera's sub starts with it instead of
        # waiting out a reconcile interval.
        await _sync_recording_tracks(camera)
    if settings.nvr_sync_enabled and retention_changed:
        # The NVR's add endpoint 409s on a live camera without touching
        # retention, so push changes through the dedicated setter. Applies to
        # non-recording cameras too — their already-recorded footage keeps
        # aging out and must follow the new policy.
        await nvr_client.set_retention(slug, camera.retention_days)
    if settings.nvr_sync_enabled and groom_changed:
        # Same in-place push for the groom-after override (None clears it).
        # `or None` for the same reason as in _sync_recording_tracks: 0 on a
        # main camera means "no override", never "never groom".
        await nvr_client.set_groom(slug, camera.groom_after_days or None)

    # Motion analysis runs iff the camera is enabled AND opted in. A restart
    # (remove+add) also covers sensitivity changes, which need a fresh worker.
    if settings.motion_sync_enabled and (motion_changed or enabled_changed):
        if camera.enabled and camera.motion_detection:
            await motion_client.restart_camera(slug, camera.motion_sensitivity)
        else:
            await motion_client.remove_camera(slug)

    # Smart Search indexes a camera iff it is enabled AND not opted out. Pushed
    # immediately rather than waiting for the reconcile sweep, so an operator who
    # turns indexing off sees it stop rather than continuing for another minute.
    #
    # `retention_changed` is in the trigger for a stronger reason than latency.
    # The index stamps each row's deletion deadline from this camera's retention
    # and RESTAMPS the existing ones when it changes, so a shortened policy that
    # only reached the NVR would delete the footage on the new clock while the
    # crops cut from it stayed searchable on the old one — the appliance
    # reporting a retention it is not keeping for the derived personal data.
    if settings.smartsearch_sync_enabled and smartsearch_sync.is_configured() \
            and (search_changed or enabled_changed or retention_changed):
        if camera.enabled and camera.search_indexing and camera.search_domains:
            # Re-add rather than add: the index holds the domain set per camera,
            # so a domain change needs the entry replaced, not merely present.
            await smartsearch_sync.restart_camera(
                slug, list(camera.search_domains), camera.retention_days)
        else:
            # No domains selected is the same as off, and is stored as such
            # rather than as a camera that is indexed but contributes nothing.
            await smartsearch_sync.remove_camera(slug)

    # Keep the uptime history truthful across enable/disable: a disabled
    # camera must not linger as "connected" (the poller skips it), and time
    # while disabled is excluded from uptime math rather than counted as down.
    if enabled_changed:
        await health_svc.set_status(
            slug,
            HealthStatus.UNKNOWN.value if camera.enabled else HealthStatus.DISABLED.value,
        )

    # Fast-path status after enable or URL change (see create_camera).
    if camera.enabled and (enabled_changed or rtsp_changed):
        asyncio.create_task(health_svc.check_camera_soon(camera.id))

    # Unconditional: the projected envelope carries the camera's name, slug and
    # enabled flag, and a full sweep is a no-op when none of them moved — cheaper
    # than tracking which fields feed the pipeline and getting it wrong later.
    await sync_now(f"camera.updated:{slug}")

    log.info("update_camera.ok", slug=slug)
    # Retention is a distinct, compliance-relevant event; everything else is a
    # config change. Record them separately (and skip config_changed when the
    # only change was retention) to keep the trail readable.
    changed = [
        field for field, did in [
            ("name", body.name is not None),
            ("rtsp_url", rtsp_changed),
            ("enabled", enabled_changed),
            ("recording", recording_changed),
            ("motion", motion_changed),
            # Smart Search opt-in is a data-COLLECTION decision, so its absence
            # here was the one gap that mattered: an operator stopping indexing
            # for a data-protection reason left no trace, in either direction,
            # from either the AI Config tab or the Recording tab's one-click
            # stop.
            # `search_changed` already covers search_indexing and search_domains
            # and is computed above for the index push.
            ("search", search_changed),
            ("groom_after_days", groom_changed),
            ("metadata", body.metadata is not None),
            ("sensor_id", body.sensor_id is not None),
            ("lawful_basis", body.lawful_basis is not None),
            ("purpose", body.purpose is not None),
            ("notice_posted", body.notice_posted is not None),
            ("stqc_status", body.stqc_status is not None),
            ("stqc_certificate_no", body.stqc_certificate_no is not None),
            ("stqc_valid_until", body.stqc_valid_until is not None),
        ] if did
    ]
    if changed:
        detail: dict[str, Any] = {"changed": changed}
        if search_changed:
            detail["search"] = {"before": search_before, "after": _search_state(camera)}
        await audit_svc.record(
            request, principal, "camera.config_changed",
            target=slug, detail=detail,
        )
    if retention_changed:
        await audit_svc.record(
            request, principal, "retention.changed",
            target=slug,
            detail={"retention_days": new_retention,
                    "justification": camera.retention_justification},
        )
    return _build_response(camera)


# ─── DELETE /cameras/{id} ─────────────────────────────────────────────────────

@router.delete("/{camera_id}", status_code=204, dependencies=_admin_only)
async def delete_camera(
    camera_id: uuid.UUID,
    request: Request,
    purge_recordings: bool = False,
    db: AsyncSession = Depends(get_db),
) -> Response:
    async with db.begin():
        camera = await _get_camera_or_404(camera_id, db)
        slug = camera.slug
        await db.delete(camera)

    # Both tracks are torn down and purged UNCONDITIONALLY, without asking
    # whether the sub is switched on right now. Deletion is the one path the
    # reconcile loops cannot heal — once the row is gone nothing remembers this
    # camera ever had a sub — and "is the sub recording today" is the wrong
    # question to gate either half on:
    #
    #   • teardown: a sub switched off minutes ago can still have a live worker
    #     or relay path that no later pass would ever find.
    #   • purge: footage recorded while the sub WAS on outlives the flag. Gating
    #     the purge on the flag reported erasure as complete while a full
    #     low-res copy of exactly that footage stayed on disk — unreachable
    #     through the UI, with no camera row left to erase it through. For a
    #     DPDP erasure that is the difference between deleted and hidden.
    #
    # Every call below treats "not found" as success, so doing this for a camera
    # that never had a sub costs one 404 and changes nothing.
    sub_name = sub_recording_name(slug)
    try:
        await relay.remove_path(slug)
        await relay.remove_path(sub_name)
        await redis_client.delete_stream_keys(slug)
    except Exception as exc:
        log.warning("delete_camera.relay_cleanup_failed", slug=slug, error=str(exc))

    purged_ok = await _teardown_recording_tracks(slug, purge=purge_recordings)
    if settings.motion_sync_enabled:
        await motion_client.remove_camera(slug)

    # Drops the camera's config file and detaches it from the running
    # pipeline. NOTE: the pipeline restarts its container on any removal, so
    # analysis on every camera pauses for a few seconds here — its behaviour,
    # not something this call adds, but the reason removals are never inferred
    # by the background loop (see services/deepstream_client.reconcile).
    await sync_now(f"camera.deleted:{slug}")

    log.info("delete_camera.ok", slug=slug, purged=purge_recordings,
             purge_complete=purged_ok)
    await audit_svc.record(
        request, await get_principal(request), "camera.deleted",
        target=slug, detail={"purged_recordings": purge_recordings,
                             # False = the NVR refused or was unreachable for at
                             # least one track and footage may remain. Recorded
                             # rather than raised: the camera row IS gone, and
                             # claiming the delete failed would be its own lie.
                             "purge_complete": purged_ok},
    )
    return Response(status_code=204)


# ─── POST /cameras/{id}/reconnect ─────────────────────────────────────────────

@router.post("/{camera_id}/reconnect", status_code=200, dependencies=_can_manage)
async def reconnect_camera(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    camera = await _get_camera_or_404(camera_id, db)
    if not camera.enabled:
        raise HTTPException(status_code=409, detail="Camera is disabled")
    await relay.force_reconnect(camera.slug, camera.rtsp_url)
    await redis_client.reset_reconnect_count(camera.slug)
    await redis_client.clear_backoff(camera.slug)
    # Fast-path status so the UI reflects the reconnect within seconds.
    asyncio.create_task(health_svc.check_camera_soon(camera.id))
    return {"status": "reconnect_triggered", "slug": camera.slug}


# ─── GET /cameras/{id}/health ─────────────────────────────────────────────────

@router.get("/{camera_id}/health", response_model=CameraHealthResponse)
async def camera_health(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> CameraHealthResponse:
    camera = await _get_camera_or_404(camera_id, db)
    slug = camera.slug

    # Try Redis cache first (updated every HEALTH_POLL_INTERVAL seconds)
    cached = await redis_client.get_stream_health(slug)
    reconnect_count = await redis_client.get_reconnect_count(slug)

    if cached:
        return CameraHealthResponse(
            slug=slug,
            connected=cached.get("status") == "connected",
            status=cached.get("status", "unknown"),
            checked_at=cached.get("checked_at"),
            reconnect_count=reconnect_count,
            tracks=cached.get("tracks", []),
            source_type=cached.get("source_type"),
        )

    # Cache miss — query MediaMTX directly
    status_data = await relay.get_path_status(slug)
    return CameraHealthResponse(
        slug=slug,
        connected=status_data.get("connected", False),
        status="connected" if status_data.get("connected") else "disconnected",
        checked_at=None,
        reconnect_count=reconnect_count,
        tracks=status_data.get("tracks", []),
        source_type=status_data.get("source_type"),
    )


# ─── GET /cameras/{id}/uptime ─────────────────────────────────────────────────

_UP_STATUSES = {HealthStatus.CONNECTED.value}
_DOWN_STATUSES = {HealthStatus.DISCONNECTED.value, HealthStatus.ERROR.value}

def _status_bucket(status: str) -> str:
    """Collapse the five HealthStatus values into the report's three states:
    Up (connected), Down (disconnected + error — both are observed failures),
    No data (unknown + disabled — time we weren't monitoring)."""
    if status in _UP_STATUSES:
        return "Up"
    if status in _DOWN_STATUSES:
        return "Down"
    return "No data"


def _fmt_local_dt(dt: datetime) -> str:
    """Server-local wall-clock time (respects TZ env) — what the report reader expects."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _uptime_xlsx(camera: Camera, rpt: CameraUptimeResponse) -> Response:
    """Excel workbook for one camera's uptime window: Summary + Intervals sheets.

    Same shape as the NVR's /report/uptime export so the two reports read
    alike, but sourced from relay-connectivity events rather than segments on
    disk. Deliberately simpler than the on-screen timeline: intervals are
    collapsed to Up / Down / No data (consecutive same-state spans merged) —
    the audience is facilities/vendors, not operators debugging the relay.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(500, detail="openpyxl is not installed on the server")

    bold = Font(bold=True)
    fills = {
        "Up": PatternFill("solid", start_color="C6EFCE"),       # light green
        "Down": PatternFill("solid", start_color="FFC7CE"),     # light red
        "No data": PatternFill("solid", start_color="E7E6E6"),  # gray
    }

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Camera", "Slug", "Window Start", "Window End", "Uptime %",
               "Outages", "Up Time", "Down Time", "No Data",
               "Up Seconds", "Down Seconds"])
    for c in ws[1]:
        c.font = bold
    ws.append([
        camera.name or "", camera.slug or "",
        _fmt_local_dt(rpt.from_), _fmt_local_dt(rpt.to),
        rpt.uptime_pct if rpt.uptime_pct is not None else "—",
        rpt.outages,
        _fmt_dur(rpt.up_seconds), _fmt_dur(rpt.down_seconds),
        _fmt_dur(rpt.unknown_seconds),
        rpt.up_seconds, rpt.down_seconds,
    ])

    # Merge consecutive intervals that land in the same bucket (a
    # disconnected→error flap is ONE Down row in the report). Intervals are
    # contiguous by construction, so bucket equality is the only test needed.
    merged: list[list] = []  # [bucket, start, end]
    for iv in rpt.intervals:
        bucket = _status_bucket(iv.status)
        if merged and merged[-1][0] == bucket:
            merged[-1][2] = iv.end
        else:
            merged.append([bucket, iv.start, iv.end])

    ws2 = wb.create_sheet("Intervals")
    ws2.append(["Status", "Start", "End", "Duration", "Duration (s)"])
    for c in ws2[1]:
        c.font = bold
    for bucket, start, end in merged:
        dur = (end - start).total_seconds()
        ws2.append([
            bucket,
            _fmt_local_dt(start), _fmt_local_dt(end),
            _fmt_dur(dur), round(dur, 1),
        ])
        ws2.cell(row=ws2.max_row, column=1).fill = fills[bucket]

    for sheet in (ws, ws2):
        for col_idx in range(1, sheet.max_column + 1):
            width = max(
                (len(str(c.value)) for c in sheet[get_column_letter(col_idx)]
                 if c.value is not None), default=8)
            sheet.column_dimensions[get_column_letter(col_idx)].width = min(width + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    name = camera.slug or str(camera.id)[:8]
    stamp_from = rpt.from_.astimezone().strftime("%Y%m%d_%H%M%S")
    stamp_to = rpt.to.astimezone().strftime("%Y%m%d_%H%M%S")
    filename = f"uptime_{name}_{stamp_from}_to_{stamp_to}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{camera_id}/uptime", response_model=CameraUptimeResponse)
async def camera_uptime(
    camera_id: uuid.UUID,
    hours: int = Query(default=24, ge=1, le=2208, description="Window length (≤ 92 days)"),
    to: Optional[datetime] = Query(default=None, description="Window end; default now (UTC)"),
    from_: Optional[datetime] = Query(
        default=None, alias="from",
        description="Explicit window start; overrides hours (max 92-day window)",
    ),
    format: Literal["json", "xlsx"] = Query(default="json", description="json response or xlsx download"),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Status timeline reconstructed from camera_status_events.

    The window is 'from'→'to' when 'from' is given (custom export ranges),
    otherwise the trailing 'hours' before 'to'. It is seeded with the last
    transition at/before its start (or 'unknown' when history doesn't reach
    back that far), then each event in range opens a new interval. Uptime %
    counts monitored time only — unknown/disabled spans are excluded from the
    denominator.

    format=xlsx returns the same data as an Excel download (Summary +
    Intervals), simplified to three states: Up / Down / No data.
    """
    camera = await _get_camera_or_404(camera_id, db)

    to_dt = to or datetime.now(tz=timezone.utc)
    if to_dt.tzinfo is None:
        to_dt = to_dt.replace(tzinfo=timezone.utc)
    if from_ is not None:
        from_dt = from_ if from_.tzinfo else from_.replace(tzinfo=timezone.utc)
        if from_dt >= to_dt:
            raise HTTPException(status_code=400, detail="'from' must be earlier than 'to'")
        if to_dt - from_dt > timedelta(days=92):
            raise HTTPException(status_code=400, detail="Window cannot exceed 92 days")
    else:
        from_dt = to_dt - timedelta(hours=hours)

    seed = (
        await db.execute(
            select(CameraStatusEvent.status)
            .where(
                CameraStatusEvent.camera_id == camera.id,
                CameraStatusEvent.changed_at <= from_dt,
            )
            .order_by(CameraStatusEvent.changed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    events = (
        await db.execute(
            select(CameraStatusEvent.status, CameraStatusEvent.changed_at)
            .where(
                CameraStatusEvent.camera_id == camera.id,
                CameraStatusEvent.changed_at > from_dt,
                CameraStatusEvent.changed_at <= to_dt,
            )
            .order_by(CameraStatusEvent.changed_at.asc())
        )
    ).all()

    intervals: list[UptimeInterval] = []
    cur_status = seed or HealthStatus.UNKNOWN.value
    cur_start = from_dt
    for status, changed_at in events:
        if status == cur_status:
            continue
        intervals.append(UptimeInterval(status=cur_status, start=cur_start, end=changed_at))
        cur_status, cur_start = status, changed_at
    intervals.append(UptimeInterval(status=cur_status, start=cur_start, end=to_dt))

    up = down = unknown = 0.0
    outages = 0
    in_outage = False
    for iv in intervals:
        dur = (iv.end - iv.start).total_seconds()
        if iv.status in _UP_STATUSES:
            up += dur
            in_outage = False
        elif iv.status in _DOWN_STATUSES:
            down += dur
            if not in_outage:
                outages += 1
            in_outage = True
        else:
            unknown += dur
            in_outage = False

    monitored = up + down
    rpt = CameraUptimeResponse(
        camera_id=camera.id,
        slug=camera.slug,
        from_=from_dt,
        to=to_dt,
        uptime_pct=round(up / monitored * 100, 2) if monitored > 0 else None,
        outages=outages,
        up_seconds=round(up, 1),
        down_seconds=round(down, 1),
        unknown_seconds=round(unknown, 1),
        intervals=intervals,
    )
    if format == "xlsx":
        # Excel export is policy-gated (Roles & permissions → report export);
        # the JSON timeline stays open to any authenticated principal.
        if request is not None:
            from ..security import get_principal
            from ..services import policy as policy_svc
            await policy_svc.require(await get_principal(request), "export_reports")
        return _uptime_xlsx(camera, rpt)
    return rpt


# ─── GET /cameras/{id}/snapshot ──────────────────────────────────────────────

# A grey/green "frame" is a real decode of a P-frame whose reference picture was
# never received — it happens whenever FFmpeg joins the relay mid-GOP. Such a
# frame is near-uniform, so it compresses to almost nothing: a genuine 720p+
# camera frame at q:v 4 is tens of KB, a flat one is 1–3 KB. Anything under this
# is treated as a miss and retried with a different frame-selection strategy.
_MIN_PLAUSIBLE_JPEG_BYTES = 8_000

_SNAPSHOT_TIMEOUT_SEC = 12.0
_SNAPSHOT_WAIT_SEC = 10.0        # how long a caller waits on someone else's decode


# Two frame-selection strategies, tried in order. Each is (input options, output
# options) — FFmpeg needs decoder flags before -i, filters after it.
_SNAPSHOT_PASSES: list[tuple[list[str], list[str]]] = [
    # Keyframes only. An IDR is self-contained, so this is the one strategy that
    # cannot return a grey frame. Costs up to one GOP of wait — measured at
    # 1.9–3.0 s on the cameras here, well inside the timeout.
    (["-skip_frame", "nokey"], []),
    # Fallback for a stream we never see a keyframe on within the timeout: decode
    # normally and discard the first 15 pictures. Partial-grey is possible (P
    # deltas painted over a grey reference) but a partial frame is still enough
    # to place a mask or zone against.
    ([], ["-vf", "select=gte(n\\,15)"]),
]


async def _grab_frame(local_url: str, pre_input: list[str], post_input: list[str]) -> bytes | None:
    """Run one FFmpeg frame-grab. Returns JPEG bytes, or None if it produced nothing."""
    # asyncio.create_subprocess_exec avoids blocking the event loop. We read from
    # the local RTSP relay rather than the raw camera URL so this works even if
    # the camera requires VPN access.
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-rtsp_transport", "tcp",
        *pre_input,
        "-i", local_url,
        *post_input,
        "-an",
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-q:v", "4",             # JPEG quality 1-31, lower = better
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SNAPSHOT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0 or not stdout:
        return None
    return stdout


async def _decode_snapshot(local_url: str) -> bytes | None:
    """Decode one JPEG, escalating to the next pass if the frame looks flat."""
    best: bytes | None = None
    for pre_input, post_input in _SNAPSHOT_PASSES:
        jpeg = await _grab_frame(local_url, pre_input, post_input)
        if jpeg and len(jpeg) >= _MIN_PLAUSIBLE_JPEG_BYTES:
            return jpeg
        best = jpeg or best
    return best  # a flat frame still beats no frame at all


def _jpeg_response(jpeg: bytes) -> Response:
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": f"private, max-age={redis_client.SNAPSHOT_CACHE_TTL_SEC}"},
    )


@router.get("/{camera_id}/snapshot")
async def camera_snapshot(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Extract a single JPEG frame from the local RTSP stream via FFmpeg.

    One decode per camera at a time. Concurrent callers do NOT get a 429 — they
    wait for the in-flight decode and are served the same frame from a short
    Redis cache. That matters because the drawing editors (privacy masks,
    analytics zones) open right after the config page has already pulled a
    thumbnail for the same camera; rejecting them left the operator drawing on a
    blank rectangle.
    """
    camera = await _get_camera_or_404(camera_id, db)
    slug = camera.slug

    if not camera.enabled:
        raise HTTPException(status_code=409, detail="Camera is disabled")

    cached = await redis_client.get_cached_snapshot(slug)
    if cached:
        return _jpeg_response(cached)

    if not await redis_client.acquire_snapshot_lock(slug):
        # Someone else is decoding — poll for their result instead of failing.
        deadline = asyncio.get_running_loop().time() + _SNAPSHOT_WAIT_SEC
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            cached = await redis_client.get_cached_snapshot(slug)
            if cached:
                return _jpeg_response(cached)
        raise HTTPException(status_code=503, detail="Snapshot is taking too long — try again")

    # Pull via the API's own route to the relay — differs from the user-facing
    # URL under the bridge overlay (mediamtx is another container there).
    local_url = settings.internal_rtsp_url(slug)

    try:
        jpeg = await _decode_snapshot(local_url)
        if not jpeg:
            raise HTTPException(status_code=503, detail="Could not extract frame — stream may be offline")
        await redis_client.set_cached_snapshot(slug, jpeg)
        return _jpeg_response(jpeg)
    finally:
        # Release explicitly: the TTL is only a crash backstop. Holding it for
        # its full window was what starved every follow-up request.
        await redis_client.release_snapshot_lock(slug)


# ── Privacy masks (burned into recordings by the NVR) ────────────────────────

@router.post("/{camera_id}/sub-track/resolve", response_model=CameraResponse,
             dependencies=_admin_only)
async def resolve_sub_track(
    camera_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    """Probe the camera for a useful low-resolution second stream and store it.

    Probing only — **nothing starts recording here**. A sub track costs a second
    ffmpeg, a second relay path and ~10-25% more disk, so switching it on is a
    separate, explicit act (`PUT .../sub-track`).

    The stream is judged, not just found: it is stored when it is either
    browser-safe (playback can stream-copy it, removing the transcode entirely)
    or materially smaller than the main (making the transcode cheaper). A
    same-size sub is no cheaper to serve and is rejected. Codec is always
    probed, never inferred from the vendor — on this fleet that turned up H.264
    substreams behind HEVC mains, which a vendor guess would have missed.

    Takes a few seconds per candidate stream: it opens each one.
    """
    camera = await _get_camera_or_404(camera_id, db)
    was_recording = bool((camera.sub_track or {}).get("recording_enabled"))
    resolved, reason = await substream.resolve_detailed(camera)

    # An unreachable camera teaches us NOTHING about its substreams, so it may
    # not overwrite what we already know about them. Storing the empty result
    # erased the sub's URL and its recording flag, and the reconcile loops then
    # tore the recording down within one interval — from a button an operator
    # presses precisely when a camera looks unwell, which is when it is most
    # likely to be briefly unreachable.
    if reason == substream.UNREACHABLE and camera.sub_track:
        log.warning("sub_track.resolve_unreachable_kept_config", slug=camera.slug)
        await audit_svc.record(
            request, await get_principal(request), "camera.sub_track_resolved",
            target=camera.slug, detail={"found": False, "reason": reason,
                                        "stored_config_kept": True},
        )
        raise HTTPException(
            status_code=503,
            detail="Could not reach this camera's main stream, so its sub track "
                   "could not be re-checked. The existing sub-track "
                   "configuration has been left untouched — try again once the "
                   "camera is back.",
        )

    prior = camera.sub_track or {}
    # Carry the operator's choices across the re-probe — see
    # tracks.carry_operator_settings for which fields those are and why the list
    # lives there rather than here.
    resolved = tracks.carry_operator_settings(prior, resolved)
    # Reached only when the camera ANSWERED. Clearing a stored sub here is a
    # real finding — this camera no longer offers a substream worth recording —
    # so it is applied, but never quietly: a recording that stops because of it
    # is called out in the audit line rather than left for someone to notice.
    cleared_recording = bool(was_recording and not resolved)
    camera.sub_track = resolved
    await db.commit()
    await db.refresh(camera)
    if cleared_recording:
        log.warning("sub_track.recording_cleared_by_resolve", slug=camera.slug,
                    reason=reason)
        await _sync_recording_tracks(camera)
    elif resolved and was_recording and resolved.get("url_raw") != prior.get("url_raw"):
        # The probe found a DIFFERENT profile and the sub is recording it right
        # now. Three things point at the old stream and none of them notice on
        # their own:
        #   • the relay path still pulls the previous URL (add_path treats an
        #     existing path as success without repointing it),
        #   • the NVR worker is mid-recording on a source about to change codec
        #     underneath it, which stream-copy handles badly, and
        #   • the NVR's codec cache still answers with the old codec, so
        #     playback would serve e.g. raw HEVC through the H.264 path — a
        #     black player with nothing logged.
        # Repoint, drop the stale codec answer, then restart the worker so the
        # new profile starts at a clean segment boundary.
        sub_name = sub_recording_name(camera.slug)
        log.info("sub_track.profile_swapped", slug=camera.slug,
                 codec=resolved.get("codec"), was=prior.get("codec"))
        try:
            new_url = tracks.sub_track_url(camera)
            if new_url:
                await relay.ensure_path(sub_name, new_url)
        except Exception as exc:  # noqa: BLE001 — reconcile heals within one interval
            log.warning("sub_track.relay_repoint_failed", slug=camera.slug,
                        error=str(exc))
        await nvr_client.invalidate_codec_cache(sub_name)
        await _sync_recording_tracks(camera, restart=True)
    await audit_svc.record(
        request, await get_principal(request), "camera.sub_track_resolved",
        target=camera.slug,
        detail={"found": bool(resolved), "reason": reason,
                **({"recording_stopped": True} if cleared_recording else {}),
                **({"codec": resolved["codec"],
                    "resolution": f"{resolved['width']}x{resolved['height']}",
                    "source": resolved["source"]} if resolved else {})},
    )
    return CameraResponse.from_orm(camera, settings.local_rtsp_url(camera.slug))


@router.put("/{camera_id}/sub-track", response_model=CameraResponse,
            dependencies=_admin_only)
async def set_sub_track_recording(
    camera_id: uuid.UUID,
    body: SubTrackBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    """Start or stop recording the camera's sub track.

    Enabling adds a `<slug>_sub` relay path and NVR recording worker; disabling
    tears both down.

    The sub keeps its **own, much shorter retention** (`retention_days` here, or
    the appliance default, never longer than the main). It exists to make
    scrubbing recent footage fast, while the main is what evidence is drawn
    from — so keeping both for the same period doubles the cost of the feature
    long past the point it buys anything. Once the sub expires, playback of
    older footage falls back to the main on its own. It is also never groomed:
    grooming rewrites footage keyframe-only, which is exactly what a scrub track
    must not be.

    It does inherit the privacy masks, and that is not optional — a mask that
    redacts the main must redact the sub or it publishes what the mask hides.

    Existing footage is untouched either way: turning the sub off stops new
    segments, it does not delete recorded ones, and playback keeps using them
    for the times they cover.
    """
    camera = await _get_camera_or_404(camera_id, db)
    if not camera.sub_track:
        raise HTTPException(
            status_code=409,
            detail="No sub track resolved for this camera — POST "
                   f"/cameras/{camera_id}/sub-track/resolve first",
        )
    # Replace the dict rather than mutating: SQLAlchemy does not track in-place
    # JSONB edits, so a mutated key silently never reaches the database.
    patch: dict = {"recording_enabled": body.recording_enabled}
    if body.retention_days is not None:
        patch["retention_days"] = body.retention_days
    camera.sub_track = {**camera.sub_track, **patch}
    await db.commit()
    await db.refresh(camera)

    slug, sub_name = camera.slug, sub_recording_name(camera.slug)
    try:
        if body.recording_enabled:
            url = tracks.sub_track_url(camera)
            if url:
                # ensure_path, not add_path: switching the sub off leaves the
                # relay path behind on a later re-enable, and add_path would
                # report the stale path as success and keep pulling the old
                # profile (or, after a password change, nothing at all).
                await relay.ensure_path(sub_name, url)
                if settings.nvr_sync_enabled and camera.recording and camera.enabled:
                    # The sub keeps its own short retention and is never groomed
                    # — see the reconcile loop in services/nvr_client.py for why.
                    keep = tracks.sub_retention_days(camera, camera.retention_days)
                    await nvr_client.add_camera(
                        sub_name, retention_days=keep,
                        masks=camera.privacy_masks, groom_after_days=0)
                    # add_camera is a no-op on a track that is ALREADY recording
                    # (the NVR answers 409), and reconcile only ever adds tracks
                    # it finds missing — so neither path would apply a changed
                    # retention to a running sub. These do, and are idempotent.
                    await nvr_client.set_retention(sub_name, keep)
                    await nvr_client.set_groom(sub_name, 0)
        else:
            if settings.nvr_sync_enabled:
                await nvr_client.remove_camera(sub_name)
            await relay.remove_path(sub_name)
    except Exception as exc:  # noqa: BLE001 — reconcile heals within one interval
        log.warning("sub_track.sync_failed", slug=slug,
                    enabled=body.recording_enabled, error=str(exc))

    await audit_svc.record(
        request, await get_principal(request), "camera.sub_track_recording_changed",
        target=slug, detail={"recording_enabled": body.recording_enabled},
    )
    return CameraResponse.from_orm(camera, settings.local_rtsp_url(camera.slug))


@router.put("/{camera_id}/masks", response_model=CameraResponse, dependencies=_can_manage)
async def update_privacy_masks(
    camera_id: uuid.UUID,
    body: MasksBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    """Replace the camera's privacy-mask polygons (normalized 0–1 coords).

    Masks are enforced by the NVR: a camera WITH masks records through a
    decode → overlay → re-encode pipeline (CPU cost on the NVR box), one
    WITHOUT masks keeps the zero-CPU stream-copy path. The recording worker is
    restarted here so the change takes effect within seconds — expect a brief
    (~2–5 s) recording gap on save. Live view (relay) is NOT masked; already
    recorded footage keeps whatever was burned in at the time.
    """
    camera = await _get_camera_or_404(camera_id, db)
    camera.privacy_masks = body.masks
    await db.commit()
    await db.refresh(camera)

    # Restart the NVR worker for EVERY track with the new mask set (idempotent,
    # best-effort). Schedule-gated: outside the recording window there is no
    # worker to restart, and starting one here would record off-schedule.
    #
    # The sub track is restarted too, and it has to be here: the reconcile loop
    # only re-adds tracks the NVR is MISSING, so an already-recording sub was
    # never revisited and kept burning in the old mask set — permanently, and
    # invisibly, since nothing downstream reports which masks a worker started
    # with. Playback prefers the sub when it is the cheaper track, so the
    # unmasked copy was the one an operator was most likely to be served.
    await _sync_recording_tracks(camera, restart=True)

    log.info("masks.updated", slug=camera.slug, count=len(body.masks))
    await audit_svc.record(
        request, await get_principal(request), "camera.masks_changed",
        target=camera.slug, detail={"mask_count": len(body.masks)},
    )
    return _build_response(camera)


# ── CMM analytics config (authored here; pulled by the DeepStream pipeline) ──

def _analytics_contract(camera: Camera) -> dict:
    """The canonical JSON the external pipeline consumes. Camera identity is
    joined in fresh (not stored) so it always tracks the registry; zones and
    activities come straight from the stored analytics_config."""
    cfg = camera.analytics_config or {}
    return {
        "sensor_id": camera.slug,
        "name": camera.name,
        "uri": settings.local_rtsp_url(camera.slug),  # relay URL — pull once from the relay
        "enabled": camera.enabled,
        "regions": cfg.get("regions", {}),
        "activities": cfg.get("activities", []),
    }


@router.put("/{camera_id}/analytics", response_model=CameraResponse, dependencies=_can_manage)
async def update_analytics_config(
    camera_id: uuid.UUID,
    body: AnalyticsConfigBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    """Replace the camera's CMM analytics config (zones + activity mapping).

    Validated and stored here; EXECUTED by the CPU Activity Engine in the
    analytics service, which receives this stored config verbatim (pushed
    below and re-asserted by analytics_client's reconcile loop). DeepStream is
    no longer the execution path: its projection only runs when
    DEEPSTREAM_PROJECTION_ENABLED is set, and the GET endpoints below remain for
    it. No NVR/motion-service involvement.
    """
    camera = await _get_camera_or_404(camera_id, db)
    # Every activity's type must be a key in the admin-managed catalog (validated
    # here, not in the pydantic model, because the catalog lives in the DB).
    catalog = {t.key: t for t in (await db.execute(select(AnalyticsActivityType))).scalars().all()}
    unknown = sorted({a.type for a in body.activities if a.type not in catalog})
    if unknown:
        raise HTTPException(422, f"Unknown activity type(s): {', '.join(unknown)}")
    # The registry's rules: only a runnable activity can be added, each takes
    # the zones its definition allows, and every setting must be a value its
    # code accepts.
    problems = activity_catalog.validate_activities(
        catalog, camera.analytics_config or {}, body.activities, body.regions)
    if problems:
        raise HTTPException(422, "; ".join(problems))
    camera.analytics_config = body.model_dump()
    await db.commit()
    await db.refresh(camera)
    log.info("analytics.updated", slug=camera.slug,
             regions=len(body.regions), activities=len(body.activities))
    # Hand the saved config to the CPU Activity Engine now, so the change is
    # live while the operator is still on the page rather than at the next
    # reconcile pass. Best-effort: the save above already succeeded either way.
    await analytics_client.push_camera(
        camera.slug, camera.enabled, camera.search_indexing,
        camera.search_domains, camera.analytics_config)
    # DeepStream projection — a no-op unless DEEPSTREAM_PROJECTION_ENABLED is
    # set. Kept so that integration can be switched back on without code.
    await sync_now(f"analytics.updated:{camera.slug}")
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed",
        target=camera.slug,
        detail={"section": "analytics", "regions": len(body.regions), "activities": len(body.activities)},
    )
    return _build_response(camera)


@router.get("/analytics/types")
async def list_activity_types(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """The activity-type catalog: the CPU registry's definitions (status, zone
    rule, settings) with the administrator's label, colour and order."""
    result = await db.execute(
        select(AnalyticsActivityType).order_by(AnalyticsActivityType.sort_order)
    )
    return [_type_out(t) for t in result.scalars()]


def _type_out(t: AnalyticsActivityType) -> dict:
    return {"key": t.key, "label": t.label, "color": t.color,
            "params_schema": t.params_schema or [], "status": t.status,
            "zone_rule": t.zone_rule, "description": t.description,
            "definition_version": t.definition_version}


@router.put("/analytics/types", dependencies=_admin_only)
async def replace_activity_types(
    body: ActivityTypesBody,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Relabel, recolour and reorder the catalog (admin).

    Which activity types exist, and their settings, come from the CPU activity
    registry (services/activity_catalog.py) — so this cannot add a type, cannot
    delete one, and ignores any `params_schema` it is sent. A key that is not
    already in the catalog is refused rather than created. Types the payload
    omits keep their label and colour and follow the listed ones in their
    current order.
    """
    rows = {t.key: t for t in (await db.execute(select(AnalyticsActivityType))).scalars().all()}
    unknown = [t.key for t in body.types if t.key not in rows]
    if unknown:
        raise HTTPException(
            422, f"Activity types come from the analytics engine; not in the catalog: {', '.join(unknown)}")
    listed = {t.key for t in body.types}
    for i, t in enumerate(body.types):
        rows[t.key].label = t.label
        rows[t.key].color = t.color
        rows[t.key].sort_order = i
    rest = sorted((r for k, r in rows.items() if k not in listed), key=lambda r: r.sort_order)
    for j, r in enumerate(rest, start=len(body.types)):
        r.sort_order = j
    await db.commit()
    log.info("analytics.types.relabelled", count=len(body.types))
    return [_type_out(r) for r in sorted(rows.values(), key=lambda r: r.sort_order)]


@router.get("/{camera_id}/analytics/config")
async def get_analytics_config(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The emitted pipeline contract for one camera (pull endpoint)."""
    camera = await _get_camera_or_404(camera_id, db)
    return _analytics_contract(camera)


@router.get("/analytics/config")
async def list_analytics_configs(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Bulk pull: the contract for every enabled camera that has a non-empty
    analytics_config. The pipeline reads this on startup / periodic refresh."""
    result = await db.execute(
        select(Camera).where(
            Camera.stage == CameraStage.REGISTERED.value,
            Camera.enabled.is_(True),
        )
    )
    out: list[dict] = []
    for camera in result.scalars():
        cfg = camera.analytics_config or {}
        if cfg.get("regions") or cfg.get("activities"):
            out.append(_analytics_contract(camera))
    return out


# ── DeepStream adapter (translation only; /analytics/config stays the VMS's
# own authored contract and source of truth) ──

def _require_pixel_dims(coords: str, width: Optional[int], height: Optional[int]) -> None:
    if coords == "pixels" and (width is None or height is None):
        raise HTTPException(
            400,
            "coords=pixels needs width and height — the VMS does not store the pipeline's frame size",
        )
    # The inverse mistake is just as dangerous and silent: coords=normalized
    # (the default) with a width/height supplied anyway. Those dims are simply
    # ignored today, so a caller who sends them expecting pixel output instead
    # gets normalized floats with a 200 — and a pipeline that reads those as
    # pixels collapses every zone to the top-left corner. Reject it instead.
    if coords != "pixels" and (width is not None or height is not None):
        raise HTTPException(
            400,
            "width/height only apply with coords=pixels — normalized output was requested",
        )


@router.get("/{camera_id}/analytics/deepstream")
async def get_deepstream_analytics_config(
    camera_id: uuid.UUID,
    coords: Literal["normalized", "pixels"] = "normalized",
    width: Optional[int] = Query(default=None, gt=0, le=10000),
    height: Optional[int] = Query(default=None, gt=0, le=10000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The DeepStream pipeline envelope for one camera (pull endpoint),
    translated from the VMS's own analytics config."""
    # Validate query params before touching the DB, so a bad `coords=pixels`
    # request on an unknown camera id 400s rather than 404s.
    _require_pixel_dims(coords, width, height)
    camera = await _get_camera_or_404(camera_id, db)
    return deepstream_config(
        camera,
        width=width if coords == "pixels" else None,
        height=height if coords == "pixels" else None,
    )


@router.get("/analytics/deepstream")
async def list_deepstream_analytics_configs(
    coords: Literal["normalized", "pixels"] = "normalized",
    width: Optional[int] = Query(default=None, gt=0, le=10000),
    height: Optional[int] = Query(default=None, gt=0, le=10000),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Bulk pull: the DeepStream envelope for every enabled camera that has
    a non-empty analytics_config."""
    _require_pixel_dims(coords, width, height)
    result = await db.execute(
        select(Camera).where(
            Camera.stage == CameraStage.REGISTERED.value,
            Camera.enabled.is_(True),
        )
    )
    out: list[dict] = []
    for camera in result.scalars():
        cfg = camera.analytics_config or {}
        if cfg.get("regions") or cfg.get("activities"):
            out.append(deepstream_config(
                camera,
                width=width if coords == "pixels" else None,
                height=height if coords == "pixels" else None,
            ))
    return out


# ── Recording schedule (Milestone-style calendar; enforced by the NVR sync) ──

@router.put("/{camera_id}/schedule", response_model=CameraResponse, dependencies=_can_manage)
async def update_recording_schedule(
    camera_id: uuid.UUID,
    body: RecordingScheduleBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CameraResponse:
    """Set or clear (schedule=null → record 24/7) the camera's recording calendar.

    weekly rules pick ISO weekdays (0=Mon), monthly rules pick days of the
    month; each rule carries a start–end time range in SERVER-LOCAL time
    (end <= start wraps past midnight). Applied immediately, then enforced at
    window edges by the reconcile loop every NVR_SYNC_INTERVAL (default 60 s).
    Only recording is scheduled — live view and health monitoring continue 24/7.
    """
    camera = await _get_camera_or_404(camera_id, db)
    camera.recording_schedule = (
        body.schedule.model_dump() if body.schedule is not None else None
    )
    await db.commit()
    await db.refresh(camera)

    # Apply now rather than waiting for the next reconcile tick (idempotent,
    # best-effort — the loop heals a miss).
    await _sync_recording_tracks(camera)

    log.info(
        "schedule.updated",
        slug=camera.slug,
        mode=(camera.recording_schedule or {}).get("mode", "always"),
    )
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed",
        target=camera.slug,
        detail={"section": "schedule", "mode": (camera.recording_schedule or {}).get("mode", "always")},
    )
    return _build_response(camera)


# ── ONVIF stream settings (bitrate / fps / resolution / GOP) ─────────────────
# Works for cameras onboarded through discovery (or manual add with creds):
# their IP, ONVIF port, and Fernet-encrypted credentials live on the camera row.

def _onvif_target_or_409(camera: Camera) -> tuple[str, int, str, str]:
    if not (camera.ip and camera.onvif_port and camera.enc_password):
        raise HTTPException(
            status_code=409,
            detail="No ONVIF connection details for this camera — enter its "
                   "credentials in the Maintenance tab (ONVIF credentials)",
        )
    user = decrypt(camera.enc_username)
    password = decrypt(camera.enc_password)
    if not user or password is None:
        raise HTTPException(
            status_code=409,
            detail="Stored camera credentials cannot be decrypted "
                   "(DISCOVERY_SECRET_KEY changed?) — re-enter them in the "
                   "Maintenance tab (ONVIF credentials)",
        )
    return camera.ip, camera.onvif_port, user, password


@router.put("/{camera_id}/onvif-credentials", dependencies=_can_manage)
async def set_onvif_credentials(
    camera_id: uuid.UUID,
    body: OnvifCredentialsBody,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """(Re-)enter a registered camera's ONVIF credentials in place.

    This is the recovery path when stored credentials are missing or no longer
    decrypt (e.g. DISCOVERY_SECRET_KEY rotated), and the bootstrap for cameras
    added manually/by bulk import that never had ONVIF details. Credentials are
    verified against the device (GetDeviceInformation) BEFORE being stored, so
    a typo can't clobber working state.
    """
    camera = await _get_camera_or_404(camera_id, db)
    if not camera.ip:
        raise HTTPException(
            status_code=409,
            detail="Camera has no IP on record — edit its RTSP URL first so the "
                   "device address is known",
        )
    port = body.onvif_port or camera.onvif_port
    if not port:
        raise HTTPException(
            status_code=422,
            detail="No ONVIF port known for this camera — specify one (80 is the most common)",
        )

    try:
        info = await onvif_device.verify_device(camera.ip, port, body.username, body.password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))

    camera.enc_username = encrypt(body.username)
    camera.enc_password = encrypt(body.password)
    camera.onvif_port = port
    camera.vendor = info.get("vendor") or camera.vendor
    camera.model = info.get("model") or camera.model
    camera.firmware = info.get("firmware") or camera.firmware
    await db.commit()

    log.info("onvif_credentials.updated", slug=camera.slug, port=port,
             vendor=info.get("vendor"), model=info.get("model"))
    return {"status": "verified", **info, "onvif_port": port}


@router.get("/{camera_id}/encoder")
async def get_encoder_settings(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Current video encoder configuration per media profile (Main/Sub stream),
    plus the camera-reported option ranges to build the edit form from."""
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    try:
        profiles = await onvif_media.get_encoder_settings(ip, port, user, password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    return {"camera_id": str(camera.id), "slug": camera.slug, "profiles": profiles}


@router.put("/{camera_id}/encoder/{config_token}", dependencies=_can_manage)
async def update_encoder_settings(
    camera_id: uuid.UUID,
    config_token: str,
    body: EncoderUpdateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Apply stream-setting changes to one encoder configuration on the camera.

    Most cameras restart the affected RTSP stream when settings change, so the
    relay path is force-reconnected afterwards and a fast health check follows.
    """
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No changes provided")

    try:
        await onvif_media.set_encoder_settings(ip, port, user, password, config_token, changes)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    log.info("encoder.update.ok", slug=camera.slug, token=config_token, changes=changes)

    # The camera usually drops the stream to apply encoder changes — cycle the
    # relay path now instead of waiting out a health-monitor backoff window.
    if camera.enabled:
        try:
            await relay.force_reconnect(camera.slug, camera.rtsp_url)
        except Exception as exc:  # noqa: BLE001 — monitor heals it on next poll
            log.warning("encoder.update.reconnect_failed", slug=camera.slug, error=str(exc))
        asyncio.create_task(health_svc.check_camera_soon(camera.id))

    # Best-effort read-back so the UI can show what the camera actually kept
    # (vendors clamp out-of-range values silently instead of erroring).
    profile = None
    try:
        for p in await onvif_media.get_encoder_settings(ip, port, user, password):
            if p.get("config_token") == config_token:
                profile = p
                break
    except onvif_media.OnvifError:
        pass
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed",
        target=camera.slug, detail={"section": "encoder", "token": config_token, "changes": changes},
    )
    return {"status": "applied", "profile": profile}


# ── ONVIF imaging (brightness / day-night / WDR / exposure / WB) ─────────────

@router.get("/{camera_id}/imaging")
async def get_imaging_settings(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Current imaging settings + camera-reported option ranges, one block per
    video source (almost always exactly one)."""
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    try:
        sources = await onvif_device.get_imaging(ip, port, user, password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    return {"camera_id": str(camera.id), "sources": sources}


@router.put("/{camera_id}/imaging/{source_token}", dependencies=_can_manage)
async def update_imaging_settings(
    camera_id: uuid.UUID,
    source_token: str,
    body: ImagingUpdateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Apply imaging changes. Unlike encoder changes the stream keeps running —
    no relay reconnect needed; the picture just updates."""
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No changes provided")
    try:
        await onvif_device.set_imaging(ip, port, user, password, source_token, changes)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    log.info("imaging.update.ok", slug=camera.slug, source=source_token, changes=changes)

    source = None
    try:  # read-back: cameras clamp silently
        for s in await onvif_device.get_imaging(ip, port, user, password):
            if s.get("source_token") == source_token:
                source = s
                break
    except onvif_media.OnvifError:
        pass
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed",
        target=camera.slug, detail={"section": "imaging", "source": source_token, "changes": changes},
    )
    return {"status": "applied", "source": source}


# ── ONVIF OSD (burned-in text / timestamp overlays) ──────────────────────────

@router.get("/{camera_id}/osd")
async def get_osd_settings(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    try:
        osds = await onvif_device.get_osds(ip, port, user, password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    return {"camera_id": str(camera.id), "osds": osds}


@router.put("/{camera_id}/osd/{osd_token}", dependencies=_can_manage)
async def update_osd_settings(
    camera_id: uuid.UUID,
    osd_token: str,
    body: OsdUpdateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No changes provided")
    try:
        await onvif_device.set_osd(ip, port, user, password, osd_token, changes)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    log.info("osd.update.ok", slug=camera.slug, token=osd_token, changes=changes)

    osd = None
    try:
        for o in await onvif_device.get_osds(ip, port, user, password):
            if o.get("token") == osd_token:
                osd = o
                break
    except onvif_media.OnvifError:
        pass
    await audit_svc.record(
        request, await get_principal(request), "camera.config_changed",
        target=camera.slug, detail={"section": "osd", "token": osd_token, "changes": changes},
    )
    return {"status": "applied", "osd": osd}


# ── ONVIF maintenance: reboot + clock sync ────────────────────────────────────

@router.post("/{camera_id}/onvif-reboot", dependencies=_can_manage)
async def reboot_camera(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Remote power-cycle via ONVIF SystemReboot. The stream drops for the
    camera's boot time (typically 30–90 s); the health monitor's backoff
    reconnect brings the relay back automatically."""
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    try:
        result = await onvif_device.reboot(ip, port, user, password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    log.warning("camera.reboot.requested", slug=camera.slug, ip=ip)
    return result


@router.post("/{camera_id}/sync-time", dependencies=_can_manage)
async def sync_camera_time(
    camera_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Set the camera's UTC clock to this server's time (timezone/DST on the
    camera are preserved). Fixes OSD timestamp drift and the WS-Security
    clock-skew auth failures that plague ONVIF."""
    camera = await _get_camera_or_404(camera_id, db)
    ip, port, user, password = _onvif_target_or_409(camera)
    try:
        result = await onvif_device.sync_time(ip, port, user, password)
    except onvif_media.OnvifError as exc:
        raise HTTPException(status_code=409 if exc.auth else 502, detail=str(exc))
    log.info("camera.time_synced", slug=camera.slug,
             skew_before=result.get("skew_before_seconds"))
    return result


# ── Bulk upload shared logic ──────────────────────────────────────────────────

async def _process_bulk(
    raw_rows: list[dict[str, Any]],
    dry_run: bool,
    db: AsyncSession,
    default_lawful_basis: Optional[str] = None,
    default_purpose: Optional[str] = None,
    default_notice_posted: bool = False,
) -> BulkUploadResponse:
    """
    Shared implementation for both bulk upload endpoints.

    Flow:
      1. Validate every row via Pydantic AND the DPDP lawful-basis rule — a
         bulk-registered camera is a registered camera, so it carries the same
         "lawful basis + purpose required" invariant as POST /cameras. A row may
         supply its own values; otherwise the batch-level defaults
         (default_lawful_basis / default_purpose) apply. A row with neither is a
         per-row error, not a silently non-compliant registration.
      2. If dry_run=True, return the validation report without touching the DB
      3. If any row has a validation error, return all errors without any insertion
      4. Otherwise insert all rows atomically and register them in MediaMTX
    """

    if not raw_rows:
        raise HTTPException(status_code=422, detail="No rows provided")

    if len(raw_rows) > 5000:
        raise HTTPException(status_code=422, detail="Maximum 5000 cameras per bulk upload")

    # ── Step 1: Validate all rows ─────────────────────────────────────────────
    results: list[BulkRowResult] = []
    validated: list[CameraCreate] = []
    seen_slugs: set[str] = set()
    has_errors = False

    for i, row in enumerate(raw_rows, start=1):
        try:
            item = CameraCreate(**row)
            slug = item.slug or generate_slug(item.name)
            # Check for within-batch slug duplicate
            if slug in seen_slugs:
                slug = generate_slug(item.name)  # re-roll once
            seen_slugs.add(slug)
            item.slug = slug
            # DPDP: a bulk-registered camera must carry a valid lawful basis +
            # purpose, exactly like POST /cameras. The row's own values win;
            # otherwise the batch-level defaults for this import apply. Neither
            # present → a clear per-row error (validate_lawful_basis raises 422),
            # never a silently non-compliant registered camera.
            item.lawful_basis, item.purpose = validate_lawful_basis(
                item.lawful_basis or default_lawful_basis,
                item.purpose or default_purpose,
            )
            item.notice_posted = item.notice_posted or default_notice_posted
            validated.append(item)
            results.append(BulkRowResult(row=i, name=item.name, success=True, slug=slug))
        except HTTPException as exc:
            has_errors = True
            name = row.get("name") if isinstance(row, dict) else None
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            results.append(BulkRowResult(row=i, name=name, success=False, error=detail))
        except Exception as exc:
            has_errors = True
            name = row.get("name") if isinstance(row, dict) else None
            results.append(BulkRowResult(row=i, name=name, success=False, error=str(exc)))

    total = len(raw_rows)
    error_count = sum(1 for r in results if not r.success)
    success_count = total - error_count

    if dry_run or has_errors:
        return BulkUploadResponse(
            total=total,
            success_count=success_count if not has_errors else 0,
            error_count=error_count,
            dry_run=dry_run or has_errors,
            results=results,
        )

    # ── Step 2: Atomic insert ─────────────────────────────────────────────────
    inserted_cameras: list[Camera] = []
    try:
        async with db.begin():
            for item in validated:
                cam = Camera(
                    name=item.name,
                    slug=item.slug,
                    rtsp_url=item.rtsp_url,
                    sensor_id=item.sensor_id,
                    enabled=item.enabled,
                    recording=item.recording,
                    stage=CameraStage.REGISTERED.value,
                    ip=host_of(item.rtsp_url),
                    camera_metadata=item.metadata,
                    # Persist the (now-validated) compliance fields — previously
                    # dropped entirely, which is how bulk-imported cameras ended
                    # up registered with no lawful basis or purpose.
                    lawful_basis=item.lawful_basis,
                    purpose=item.purpose,
                    notice_posted=item.notice_posted,
                )
                db.add(cam)
                inserted_cameras.append(cam)
            await db.flush()
            for cam in inserted_cameras:
                await db.refresh(cam)
    except IntegrityError as exc:
        log.error("bulk_upload.integrity_error", error=str(exc))
        raise HTTPException(
            status_code=409,
            detail="One or more slugs conflicted with existing records; retry — slugs are auto-generated with random suffixes so this is transient",
        )

    # ── Step 3: Register in MediaMTX ─────────────────────────────────────────
    enabled_cams = [c for c in inserted_cameras if c.enabled]
    if enabled_cams:
        await relay.register_cameras_bulk([
            {"slug": c.slug, "rtsp_url": c.rtsp_url, "sub_track": c.sub_track}
            for c in enabled_cams
        ])
        # Start recording each (best-effort; reconcile loop heals misses).
        if settings.nvr_sync_enabled:
            for c in enabled_cams:
                if c.recording:
                    await nvr_client.add_camera(c.slug, masks=c.privacy_masks)

    log.info("bulk_upload.ok", inserted=len(inserted_cameras))
    for i, cam in enumerate(inserted_cameras):
        results[i].slug = cam.slug

    return BulkUploadResponse(
        total=total,
        success_count=success_count,
        error_count=0,
        dry_run=False,
        results=results,
    )


# ─── POST /cameras/bulk  (JSON body) ──────────────────────────────────────────

class BulkJsonBody(BaseModel):
    cameras: list[dict[str, Any]]


@router.post("/bulk", response_model=BulkUploadResponse, status_code=200, dependencies=_can_manage)
async def bulk_upload_json(
    body: BulkJsonBody,
    dry_run: bool = Query(default=False, description="Validate without inserting"),
    lawful_basis: Optional[str] = Query(default=None, description="DPDP lawful basis applied to rows that omit their own"),
    purpose: Optional[str] = Query(default=None, description="DPDP purpose applied to rows that omit their own"),
    notice_posted: bool = Query(default=False, description="Mark notice/signage posted for the batch"),
    db: AsyncSession = Depends(get_db),
) -> BulkUploadResponse:
    """Bulk-register cameras from a JSON array body."""
    return await _process_bulk(body.cameras, dry_run, db, lawful_basis, purpose, notice_posted)


# ─── POST /cameras/bulk/csv  (CSV file upload OR parsed rows as JSON) ─────────
# The browser frontend parses CSV client-side and sends the rows as a JSON body;
# direct API callers may also POST a raw CSV file using multipart form-data.

@router.post("/bulk/csv", response_model=BulkUploadResponse, status_code=200, dependencies=_can_manage)
async def bulk_upload_csv(
    body: Optional[BulkJsonBody] = None,
    file: Optional[UploadFile] = File(default=None, description="CSV file: name,rtsp_url,slug"),
    dry_run: bool = Query(default=False, description="Validate without inserting"),
    lawful_basis: Optional[str] = Query(default=None, description="DPDP lawful basis applied to rows that omit their own"),
    purpose: Optional[str] = Query(default=None, description="DPDP purpose applied to rows that omit their own"),
    notice_posted: bool = Query(default=False, description="Mark notice/signage posted for the batch"),
    db: AsyncSession = Depends(get_db),
) -> BulkUploadResponse:
    """Bulk-register cameras from a CSV file upload or pre-parsed JSON rows."""
    if file is not None:
        content = await file.read()
        text_data = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text_data))
        raw_rows = [{k.strip(): v.strip() for k, v in row.items()} for row in reader]
    elif body is not None:
        raw_rows = body.cameras
    else:
        raise HTTPException(status_code=422, detail="Provide a CSV file or JSON body")
    return await _process_bulk(raw_rows, dry_run, db, lawful_basis, purpose, notice_posted)
