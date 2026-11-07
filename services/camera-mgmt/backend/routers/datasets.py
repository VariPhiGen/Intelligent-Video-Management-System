# services/camera-mgmt/backend/routers/datasets.py
"""Data Collection — turn recorded footage into training images (migration 045).

A dataset is a recipe: "camera X, 10:00-12:00, 200 frames". Collecting
evaluates it by pulling that many evenly-spaced snapshots from the NVR;
exporting pushes the result to Label Studio for annotation and/or mirrors it to
S3 for keeping.

Every route is gated by the grantable `data_collection` capability, SERVER-SIDE.
Hiding the page would be a false promise: collection writes a durable pile of
stills derived from recorded footage and can push them to an external service,
so the routes themselves have to say no.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, Response,
    UploadFile,
)
from sqlalchemy import select

from ..config import settings
from ..db import AsyncSessionLocal, get_db
from ..models import (
    Camera, Dataset, DatasetCreateBody, DatasetExportBody, DatasetResponse,
    DatasetUpdateBody, PtzPreset, dataset_interval_seconds,
)
from ..security import Principal
from ..services import audit as audit_svc
from ..services import (
    analytics_client, dataset_build, labelstudio_client, nvr_client, ptz_dataset, s3_upload,
)
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/datasets", tags=["datasets"])

_COLLECT = require_capability("data_collection")

# How long a row may sit in `collecting` without progress before it is presumed
# dead. Collection runs as a background task inside an api worker, so a worker
# restart, a container bounce or an OOM kill leaves the row untouched and
# nothing would ever move it again. The progress heartbeat writes every 10
# frames, so ten minutes of silence means the run is gone, not slow — even at
# the ~1.3 s/frame the NVR actually costs, ten frames is under a minute.
_STALE_AFTER = timedelta(minutes=10)

# Frames are written by dataset_build as frame_{idx:05d}_{epoch_ms}.jpg and
# nothing else is ever served from a dataset directory. Matching the whole
# filename against the exact shape is a stronger guarantee than stripping "..":
# a name that is not this shape is not one of ours, whatever it is.
_FRAME_NAME = re.compile(r"^frame_\d{5}_\d+\.jpg$")

# Strong references to in-flight collection tasks. The event loop only holds
# WEAK references to tasks, so a task nobody keeps can be garbage-collected
# mid-run — the collection would simply stop, leaving the row in `collecting`
# until the reaper found it.
_RUNNING: set[asyncio.Task] = set()

# dataset_id -> its in-flight collection task, so an operator can stop one.
#
# IN-MEMORY, AND THAT IS ONLY SOUND BECAUSE THE API RUNS ONE WORKER. With
# several uvicorn workers a cancel could land on a process that does not hold
# the task and would silently do nothing; see the --workers note in
# docker-compose.yml. If the worker count ever rises, this has to move to a
# shared signal (a redis key the sampler polls) rather than a local dict.
_TASKS: dict[int, asyncio.Task] = {}

# Datasets an operator cancelled on purpose. The sampler raises CancelledError
# for both an operator cancel and an API shutdown, and the two deserve very
# different words in the UI, so the intent is recorded here before the task is
# cancelled and read back in the handler.
_CANCELLED: set[int] = set()


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reap_if_stale(db, row: Dataset) -> Dataset:
    """Flip a dead `collecting` row to `failed`.

    Self-healing on read rather than a background sweeper: there is no
    cross-worker scheduler here, and a row nobody looks at costs nothing.
    """
    if row.status != "collecting":
        return row
    updated = row.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    if updated is None or _now() - updated < _STALE_AFTER:
        return row
    row.status = "failed"
    row.build_error = (
        "Collection stopped unexpectedly — the API restarted while it was "
        "running. Nothing was lost; start the collection again."
    )
    row.updated_at = _now()
    await db.commit()
    log.warning("dataset.reaped_stale", dataset_id=row.id)
    return row


async def _get_or_404(db, dataset_id: int) -> Dataset:
    row = await db.get(Dataset, dataset_id)
    if row is None:
        raise HTTPException(404, f"Dataset {dataset_id} not found")
    return await _reap_if_stale(db, row)


def _frames_root() -> str:
    return settings.dataset_dir


def _frame_path(row: Dataset, filename: str) -> Path:
    """Resolve one frame inside the dataset's own directory, or 404/400.

    Two independent guards, because this serves bytes off disk to a browser:
    the filename must match the exact shape this system writes, AND the
    resolved path must still be inside the dataset's directory after symlinks.
    """
    if not _FRAME_NAME.match(filename or ""):
        raise HTTPException(400, "Not a valid frame name")
    if not row.frames_dir:
        raise HTTPException(404, "This dataset has no frames on disk")
    directory = Path(row.frames_dir).resolve()
    candidate = (directory / filename).resolve()
    if directory not in candidate.parents:
        raise HTTPException(400, "Not a valid frame name")
    if not candidate.is_file():
        raise HTTPException(404, "Frame not found")
    return candidate


# ── list / create ─────────────────────────────────────────────────────────────

@router.get("")
async def list_datasets(db=Depends(get_db),
                        principal: Principal = Depends(_COLLECT)) -> list[DatasetResponse]:
    rows = (await db.execute(
        select(Dataset).order_by(Dataset.created_at.desc())
    )).scalars().all()
    for row in rows:
        await _reap_if_stale(db, row)
    return [DatasetResponse.from_row(r) for r in rows]


@router.post("", status_code=201)
async def _check_presets(db, camera_slug: str, preset_ids: list) -> list[str]:
    """The chosen presets must be presets of THIS camera (a dataset moved to another camera can't keep the old
    camera's presets). Returns them as text, de-duplicated, in the order given."""
    ids = [str(x).strip() for x in (preset_ids or []) if str(x).strip()]
    if not ids:
        return []
    cam = (await db.execute(select(Camera).where(Camera.slug == camera_slug))).scalar_one_or_none()
    if cam is None or not cam.ptz_capable:
        raise HTTPException(422, "Only a PTZ camera can sample at presets.")
    known = {str(p.id) for p in (await db.execute(select(PtzPreset).where(PtzPreset.camera_id == cam.id))).scalars().all()}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(422, f"{len(unknown)} of the chosen presets aren't presets of {camera_slug} (deleted?). "
                                 "Pick them again.")
    return list(dict.fromkeys(ids))


async def create_dataset(body: DatasetCreateBody, request: Request,
                         db=Depends(get_db),
                         principal: Principal = Depends(_COLLECT)) -> DatasetResponse:
    now = _now()
    meta = await _validate_model_choice(body.model_id, body.target_classes)
    presets = await _check_presets(db, body.camera_slug.strip(), body.ptz_preset_ids)
    row = Dataset(
        ptz_preset_ids=presets or None,
        model_id=body.model_id or None,
        model_name=(meta or {}).get("name"),
        target_classes=body.target_classes if body.model_id else [],
        min_confidence=body.min_confidence if body.model_id else None,
        scan_interval_seconds=(body.scan_interval_seconds or 2.0) if body.model_id else None,
        created_at=now, updated_at=now,
        name=body.name.strip(), description=body.description,
        status="draft", camera_slug=body.camera_slug.strip(),
        window_start=body.window_start, window_end=body.window_end,
        target_frames=body.target_frames, jpeg_quality=body.jpeg_quality,
        frames=[], frames_collected=0, frames_missing=0,
        created_by=principal.subject,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info("dataset.created", dataset_id=row.id, slug=row.camera_slug,
             target=row.target_frames)
    return DatasetResponse.from_row(row)


# ── literal paths BEFORE /{id} ────────────────────────────────────────────────
#
# `id` is an int path parameter, so a route table that reaches /{id} first
# matches "coverage" against it and answers 422 "not a valid integer" for a
# route that exists. Literal segments must be declared above the parameterised
# ones; moving either of the next two below /{id} silently breaks it.

# ── detection models (held by the analytics service) ──────────────────────────

def _relay(exc: "analytics_client.AnalyticsModelError") -> HTTPException:
    return HTTPException(exc.status if 400 <= exc.status < 600 else 502, str(exc))


@router.get("/models")
async def list_models(principal: Principal = Depends(_COLLECT)) -> dict[str, Any]:
    try:
        return {"models": await analytics_client.list_models()}
    except analytics_client.AnalyticsModelError as exc:
        raise _relay(exc)


@router.post("/models", status_code=201)
async def upload_model(request: Request, file: UploadFile = File(...),
                       name: Optional[str] = Form(None),
                       principal: Principal = Depends(_COLLECT)) -> dict[str, Any]:
    """Upload a YOLO .pt/.onnx; analytics validates it and returns its classes."""
    try:
        meta = await analytics_client.upload_model(
            file.filename or "model", file.file, (name or "").strip() or None)
    except analytics_client.AnalyticsModelError as exc:
        raise _relay(exc)
    await audit_svc.record(
        request, principal, "dataset.model_uploaded", target=f"model:{meta.get('id')}",
        detail={"filename": file.filename, "classes": len(meta.get("classes", []))})
    return meta


@router.delete("/models/{model_id}", status_code=204)
async def delete_model(model_id: str, request: Request, db=Depends(get_db),
                       principal: Principal = Depends(_COLLECT)) -> Response:
    in_use = (await db.execute(
        select(Dataset.id).where(Dataset.model_id == model_id).limit(1))).first()
    if in_use:
        raise HTTPException(409, "A dataset uses this model; delete or change it first.")
    try:
        await analytics_client.delete_model(model_id)
    except analytics_client.AnalyticsModelError as exc:
        raise _relay(exc)
    await audit_svc.record(request, principal, "dataset.model_deleted",
                           target=f"model:{model_id}", detail={})
    return Response(status_code=204)


async def _validate_model_choice(model_id: Optional[str],
                                 classes: list[str]) -> Optional[dict]:
    """The model must exist and every chosen class must be one of its own."""
    if not model_id:
        return None
    try:
        meta = await analytics_client.get_model(model_id)
    except analytics_client.AnalyticsModelError as exc:
        raise _relay(exc)
    if meta is None:
        raise HTTPException(422, "Unknown detection model.")
    bad = [c for c in classes if c not in meta.get("classes", [])]
    if bad:
        raise HTTPException(422, f"Model has no class: {', '.join(bad)}")
    return meta


@router.get("/coverage")
async def coverage(
    camera_slug: str = Query(..., min_length=1),
    window_start: datetime = Query(...),
    window_end: datetime = Query(...),
    principal: Principal = Depends(_COLLECT),
) -> dict[str, Any]:
    """Does the NVR hold footage across this window?

    Advisory only — it warns before a doomed run rather than blocking one. The
    recorded range has gaps inside it that this cannot see, so "covered" means
    the window falls between the earliest and latest segments, not that every
    instant in it has footage.
    """
    if window_start >= window_end:
        raise HTTPException(422, "window_start must be earlier than window_end")

    rng = await nvr_client.camera_range(camera_slug)
    if rng is None:
        return {
            "known": False,
            "covered": False,
            "earliest": None, "latest": None,
            "message": (
                f"The NVR reports no recordings for '{camera_slug}'. It may "
                "never have recorded, or its footage may have aged out of "
                "retention."
            ),
        }

    earliest_iso, latest_iso = rng
    earliest = datetime.fromisoformat(earliest_iso.replace("Z", "+00:00"))
    latest = datetime.fromisoformat(latest_iso.replace("Z", "+00:00"))
    before = window_start < earliest
    after = window_end > latest
    covered = not before and not after

    if covered:
        message = "The NVR holds recordings across this window."
    elif window_end <= earliest or window_start >= latest:
        message = (
            "This window falls entirely outside the recorded range — the "
            "collection would produce no frames."
        )
    elif before and after:
        message = "Only the middle of this window is inside the recorded range."
    elif before:
        message = (
            "The start of this window is older than the earliest recording "
            "(retention has groomed it). Those frames will be missing."
        )
    else:
        message = (
            "The end of this window is later than the newest recording. Those "
            "frames will be missing."
        )

    return {
        "known": True, "covered": covered,
        "earliest": earliest_iso, "latest": latest_iso,
        "message": message,
    }


@router.get("/ptz-time")
async def ptz_time(
    camera_slug: str = Query(..., min_length=1),
    window_start: datetime = Query(...),
    window_end: datetime = Query(...),
    db=Depends(get_db),
    principal: Principal = Depends(_COLLECT),
) -> dict[str, Any]:
    """For the form: a PTZ camera's presets and how long it was AT each in the window — so "only at presets" is
    chosen with the numbers in front (a preset it never visited gives no frames). `source` says how it was told:
    "picture" (confirmed views) or "position" (footage from before the view history)."""
    if window_start >= window_end:
        raise HTTPException(422, "window_start must be earlier than window_end")
    cam = (await db.execute(select(Camera).where(Camera.slug == camera_slug))).scalar_one_or_none()
    if cam is None or not cam.ptz_capable:
        return {"ptz": False, "presets": [], "source": ""}
    presets = (await db.execute(select(PtzPreset).where(PtzPreset.camera_id == cam.id)
                                .order_by(PtzPreset.sort_order, PtzPreset.name))).scalars().all()
    spans, source = await ptz_dataset.load_intervals(db, cam.id, window_start, window_end, {str(p.id) for p in presets})
    seen = {a["preset_id"]: a for a in ptz_dataset.per_preset(spans)}
    return {"ptz": True, "source": source,
            "window_seconds": (window_end - window_start).total_seconds(),
            "presets": [{"preset_id": str(p.id), "token": p.token, "name": p.name or f"Preset {p.token}",
                         "seconds": round(seen.get(str(p.id), {}).get("seconds", 0.0)),
                         "visits": seen.get(str(p.id), {}).get("visits", 0)} for p in presets]}


@router.get("/labelstudio/config")
async def export_config(principal: Principal = Depends(_COLLECT)) -> dict[str, Any]:
    """What the import dialog needs to prefill itself.

    Returns the BUCKET and PREFIX defaults but never the AWS keys — those stay
    in the server environment. `s3_credentials` reports only whether an explicit
    key pair is set; false is not an error, because boto3 also resolves an
    instance role.
    """
    return {
        "configured": labelstudio_client.configured(),
        "url": settings.labelstudio_url.strip(),
        "default_project_id": settings.labelstudio_project_id,
        "s3_credentials": s3_upload.configured(),
        "s3_region": settings.aws_region.strip(),
        "default_bucket": settings.aws_s3_bucket.strip(),
        "default_prefix": settings.aws_s3_prefix.strip(),
    }


# ── read / update / delete ────────────────────────────────────────────────────

@router.get("/{dataset_id}")
async def get_dataset(dataset_id: int, db=Depends(get_db),
                      principal: Principal = Depends(_COLLECT)) -> DatasetResponse:
    return DatasetResponse.from_row(await _get_or_404(db, dataset_id))


# Fields that define what a collection run WOULD produce. Changing any of them
# invalidates a previous run's recorded outcome.
_BUILD_INPUTS = frozenset({
    "camera_slug", "window_start", "window_end", "target_frames", "jpeg_quality",
    "model_id", "target_classes", "min_confidence", "scan_interval_seconds", "ptz_preset_ids",
})


@router.patch("/{dataset_id}")
async def update_dataset(dataset_id: int, body: DatasetUpdateBody, db=Depends(get_db),
                         principal: Principal = Depends(_COLLECT)) -> DatasetResponse:
    row = await _get_or_404(db, dataset_id)
    if row.status == "collecting":
        raise HTTPException(409, "This dataset is collecting — wait for it to finish.")

    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()
    if "camera_slug" in data and data["camera_slug"]:
        data["camera_slug"] = data["camera_slug"].strip()

    # Validate the MERGED window, not the patch. Sending window_start alone must
    # not be able to invert a window whose other end is already stored.
    new_start = data.get("window_start", row.window_start)
    new_end = data.get("window_end", row.window_end)
    if new_start >= new_end:
        raise HTTPException(422, "window_start must be earlier than window_end")

    if "model_id" in data or "target_classes" in data:
        mid = data.get("model_id", row.model_id)
        classes = data.get("target_classes", row.target_classes) or []
        if mid and not classes:
            raise HTTPException(422, "Choose at least one class for the model to detect.")
        meta = await _validate_model_choice(mid, classes)
        data["model_name"] = (meta or {}).get("name")
        if not mid:
            data.update(model_id=None, target_classes=[], min_confidence=None,
                        scan_interval_seconds=None)
        elif not (data.get("scan_interval_seconds") or row.scan_interval_seconds):
            data["scan_interval_seconds"] = 2.0

    if "ptz_preset_ids" in data or ("camera_slug" in data and row.ptz_preset_ids):
        # Moving a dataset to another camera drops its presets unless new ones (of that camera) come with it.
        chosen = data.get("ptz_preset_ids") if "ptz_preset_ids" in data else []
        data["ptz_preset_ids"] = await _check_presets(db, data.get("camera_slug", row.camera_slug), chosen or []) or None

    for field, value in data.items():
        setattr(row, field, value)

    # A STORED OUTCOME DESCRIBES THE RUN THAT PRODUCED IT, not the dataset as it
    # is now. Change the camera and `build_error` still names the old one, so
    # the UI shows today's camera above yesterday's failure and the failure gets
    # read as belonging to a camera that never ran. Clear it, and let a failed
    # row return to draft so it can be collected again. A `ready` row keeps its
    # status: its frames are still on disk and still valid for what they are.
    if _BUILD_INPUTS.intersection(data):
        row.build_error = None
        if row.status == "failed":
            row.status = "draft"

    row.updated_at = _now()
    await db.commit()
    await db.refresh(row)
    return DatasetResponse.from_row(row)


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: int, request: Request, db=Depends(get_db),
                         principal: Principal = Depends(_COLLECT)) -> Response:
    row = await _get_or_404(db, dataset_id)
    if row.status == "collecting":
        raise HTTPException(409, "This dataset is collecting — wait for it to finish.")

    frames_dir, name, slug = row.frames_dir, row.name, row.camera_slug
    collected = row.frames_collected
    await db.delete(row)
    await db.commit()
    # After the commit: the row is the record, and orphaned bytes are a
    # recoverable annoyance where a deleted directory with a surviving row
    # would be a dataset that lies about having frames.
    dataset_build.delete_frames_dir(frames_dir, _frames_root())

    await audit_svc.record(
        request, principal, "dataset.deleted", target=f"dataset:{dataset_id}",
        detail={"name": name, "camera_slug": slug, "frames": collected},
    )
    return Response(status_code=204)


# ── collect ───────────────────────────────────────────────────────────────────

async def _run_collection(dataset_id: int) -> None:
    """Sample the window in the background and record the outcome.

    OPENS ITS OWN SESSION. The request-scoped session from get_db is closed the
    moment the 202 is sent, so touching it here fails on the first progress
    write. This is the single easiest way to get backgrounding wrong.
    """
    async with AsyncSessionLocal() as db:
        row = await db.get(Dataset, dataset_id)
        if row is None:
            return
        recipe = {
            "id": row.id, "camera_slug": row.camera_slug,
            "window_start": row.window_start, "window_end": row.window_end,
            "target_frames": row.target_frames, "jpeg_quality": row.jpeg_quality,
            "scan_interval_seconds": row.scan_interval_seconds,
        }
        model_id = row.model_id
        classes = list(row.target_classes or [])
        conf = row.min_confidence or 0.35
        # PTZ at chosen presets (phase 4): only the time the camera was at them, each frame named by its preset.
        if row.ptz_preset_ids:
            cam = (await db.execute(select(Camera).where(Camera.slug == row.camera_slug))).scalar_one_or_none()
            spans, source = (await ptz_dataset.load_intervals(db, cam.id, row.window_start, row.window_end,
                                                              {str(x) for x in row.ptz_preset_ids})
                             if cam is not None else ([], ""))
            if not spans:
                await _finish_failed(dataset_id, (
                    f"'{row.camera_slug}' was never at the chosen preset(s) between {row.window_start:%d %b %H:%M} and "
                    f"{row.window_end:%d %b %H:%M} UTC" + ("" if source else " — there is no position history for that time")
                    + ". Pick a window when it patrolled them, or another preset."))
                return
            recipe["plan"] = ([{**p, "view": source} for p in ptz_dataset.scan(spans, row.scan_interval_seconds or 2.0)]
                              if model_id else
                              [{**p, "view": source} for p in ptz_dataset.spread(spans, row.target_frames)])
            log.info("dataset.ptz_plan", dataset_id=dataset_id, intervals=len(spans), source=source,
                     seconds=round(ptz_dataset.total_seconds(spans)), instants=len(recipe["plan"]))

    detector = None
    if model_id:
        async def detector(jpeg: bytes) -> list:
            return await analytics_client.infer(model_id, jpeg, classes, conf)

    async def progress(done: int, total: int) -> None:
        # A short-lived session per heartbeat: holding one open for the whole
        # run would pin a pool connection for minutes at a time.
        async with AsyncSessionLocal() as beat:
            live = await beat.get(Dataset, dataset_id)
            if live is None or live.status != "collecting":
                return
            live.frames_collected = done
            live.updated_at = _now()
            await beat.commit()

    try:
        result = await dataset_build.collect_frames(
            recipe, _frames_root(), nvr_client.fetch_snapshot, progress,
            concurrency=settings.dataset_snapshot_concurrency,
            # Per-frame fetch stays the fallback; the batch path is the fast
            # one and degrades to it silently if the NVR has no /snapshot/batch
            # (older appliance) or the request fails.
            batch_fetcher=nvr_client.fetch_snapshots_batch,
            detector=detector)
    except dataset_build.DatasetBuildError as exc:
        await _finish_failed(dataset_id, str(exc))
        return
    except asyncio.CancelledError:
        # TWO VERY DIFFERENT EVENTS ARRIVE HERE. An operator pressing Cancel and
        # the API shutting down both cancel this task, and telling them apart
        # matters: one is a deliberate stop that should read as done, the other
        # is an interruption the operator has to restart. Frames already written
        # are kept either way.
        if dataset_id in _CANCELLED:
            _CANCELLED.discard(dataset_id)
            await _finish_cancelled(dataset_id)
        else:
            await _finish_failed(
                dataset_id,
                "Collection was cancelled because the API shut down. Start it again.")
        raise
    except Exception as exc:  # noqa: BLE001
        # An escaped exception in a background task is swallowed by the event
        # loop, which would strand the row in `collecting` until the reaper got
        # it ten minutes later. Record it now, with the real message.
        log.exception("dataset.collect_crashed", dataset_id=dataset_id)
        await _finish_failed(dataset_id, f"Collection failed unexpectedly: {exc}")
        return

    async with AsyncSessionLocal() as db:
        row = await db.get(Dataset, dataset_id)
        if row is None:
            return
        row.status = "ready"
        row.frames = result["frames"]
        row.frames_dir = result["frames_dir"]
        row.frames_collected = result["collected"]
        row.frames_missing = result["missing"]
        row.frames_scanned = result.get("scanned", 0)
        row.bytes_total = result["bytes_total"]
        row.build_error = None
        row.updated_at = _now()
        await db.commit()

    log.info("dataset.collect_finished", dataset_id=dataset_id,
             collected=result["collected"], missing=result["missing"])


async def _finish_cancelled(dataset_id: int) -> None:
    """Stopped on purpose. NOT `failed` — nothing went wrong, and a red row
    invites someone to go hunting for a fault that does not exist. Whatever was
    collected before the stop is kept and stays listed."""
    async with AsyncSessionLocal() as db:
        row = await db.get(Dataset, dataset_id)
        if row is None:
            return
        row.status = "cancelled"
        row.build_error = None
        row.updated_at = _now()
        await db.commit()
    log.info("dataset.collect_cancelled", dataset_id=dataset_id)


async def _finish_failed(dataset_id: int, message: str) -> None:
    async with AsyncSessionLocal() as db:
        row = await db.get(Dataset, dataset_id)
        if row is None:
            return
        row.status = "failed"
        row.build_error = message
        row.updated_at = _now()
        await db.commit()


@router.post("/{dataset_id}/collect", status_code=202)
async def collect(dataset_id: int, request: Request, db=Depends(get_db),
                  principal: Principal = Depends(_COLLECT)) -> DatasetResponse:
    """Start sampling. Returns 202 immediately; poll GET /{id} for the outcome.

    NOT SYNCHRONOUS, AND THE REASON IS MEASURED. The NVR's /snapshot decodes
    from the segment start on purpose (input seeking returns silently wrong
    frames on MPEG-TS), which costs 0.18 s one second into a segment and 2.58 s
    fifty-five seconds in — about 1.3 s on average. At the sampler's
    concurrency of 4 that is ~65 s for a 200-frame dataset and around two hours
    at the 20000 ceiling. Holding the request open for that would pin one of
    four uvicorn workers and depend on every proxy in front of us keeping the
    connection alive.

    The status moves draft -> collecting -> ready/failed, and `frames_collected`
    climbs while it runs, so the page can show real progress.
    """
    row = await _get_or_404(db, dataset_id)
    if row.status == "collecting":
        raise HTTPException(409, "This dataset is already collecting.")
    if row.model_id:
        span = (row.window_end - row.window_start).total_seconds()
        if span / (row.scan_interval_seconds or 2.0) > 20000:
            raise HTTPException(
                422, "The window holds more than 20000 scans at this interval; "
                     "raise the scan interval or shorten the window.")
        await _validate_model_choice(row.model_id, list(row.target_classes or []))

    row.status = "collecting"
    row.build_error = None
    row.frames_collected = 0
    row.frames_scanned = 0
    row.updated_at = _now()
    # COMMIT BEFORE STARTING THE WORK. A run that dies without this looks
    # untouched rather than interrupted, and the reaper has nothing to find.
    await db.commit()
    await db.refresh(row)

    interval = dataset_interval_seconds(
        row.window_start, row.window_end, row.target_frames)
    await audit_svc.record(
        request, principal, "dataset.collected", target=f"dataset:{dataset_id}",
        detail={
            "camera_slug": row.camera_slug,
            "window_start": row.window_start.isoformat(),
            "window_end": row.window_end.isoformat(),
            "target_frames": row.target_frames,
            "interval_seconds": interval,
        },
    )

    task = asyncio.create_task(_run_collection(dataset_id))
    # Hold a reference: the event loop only keeps weak references to tasks, so
    # an unreferenced one can be garbage-collected mid-run.
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    _TASKS[dataset_id] = task
    task.add_done_callback(lambda _t, i=dataset_id: _TASKS.pop(i, None))

    return DatasetResponse.from_row(row)


@router.post("/{dataset_id}/cancel", status_code=200)
async def cancel(dataset_id: int, request: Request, db=Depends(get_db),
                 principal: Principal = Depends(_COLLECT)) -> DatasetResponse:
    """Stop a running collection. Frames already collected are kept.

    The run is a background asyncio task, so this cancels the task and lets the
    sampler unwind at its next await — which is promptly, because it is nearly
    always waiting on a snapshot. The row lands in `cancelled`, not `failed`.
    """
    row = await _get_or_404(db, dataset_id)
    if row.status != "collecting":
        raise HTTPException(409, f"This dataset is not collecting (it is {row.status}).")

    task = _TASKS.get(dataset_id)
    # Mark the intent BEFORE cancelling: the handler reads this to decide
    # whether it was an operator or a shutdown, and the task can finish
    # unwinding before the next line of this function runs.
    _CANCELLED.add(dataset_id)
    if task is not None and not task.done():
        task.cancel()
    else:
        # No task here to cancel. Either it already finished, or this row was
        # stranded by a restart. Settle the row rather than leaving it
        # `collecting` for the reaper to mislabel ten minutes later.
        _CANCELLED.discard(dataset_id)
        await _finish_cancelled(dataset_id)

    await audit_svc.record(
        request, principal, "dataset.cancelled", target=f"dataset:{dataset_id}",
        detail={"frames_collected": row.frames_collected},
    )
    await db.refresh(row)
    return DatasetResponse.from_row(row)


# ── frames ────────────────────────────────────────────────────────────────────

@router.get("/{dataset_id}/frames")
async def list_frames(dataset_id: int, db=Depends(get_db),
                      principal: Principal = Depends(_COLLECT)) -> dict[str, Any]:
    row = await _get_or_404(db, dataset_id)
    return {
        "dataset_id": row.id,
        "frames_dir": row.frames_dir,
        "collected": row.frames_collected,
        "missing": row.frames_missing,
        "bytes_total": row.bytes_total,
        "interval_seconds": dataset_interval_seconds(
            row.window_start, row.window_end, row.target_frames),
        "frames": row.frames or [],
    }


@router.get("/{dataset_id}/frames/{filename}")
async def get_frame(dataset_id: int, filename: str, db=Depends(get_db),
                    principal: Principal = Depends(_COLLECT)) -> Response:
    """One frame's bytes, behind the same capability as everything else.

    Served through the API rather than a static mount precisely so it stays
    behind the Bearer token — which is why the SPA cannot use a plain <img src>
    and fetches these through AuthImg instead.
    """
    row = await _get_or_404(db, dataset_id)
    path = _frame_path(row, filename)
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{path.name}"',
        },
    )


@router.get("/{dataset_id}/s3/link")
async def s3_link(dataset_id: int, filename: str = Query(...), db=Depends(get_db),
                  principal: Principal = Depends(_COLLECT)) -> dict[str, str]:
    row = await _get_or_404(db, dataset_id)
    if not row.s3_bucket:
        raise HTTPException(409, "This dataset has not been mirrored to S3.")
    if not _FRAME_NAME.match(filename or ""):
        raise HTTPException(400, "Not a valid frame name")
    key = f"{row.s3_prefix or ''}{filename}"
    try:
        url = await s3_upload.presign(row.s3_bucket, key)
    except s3_upload.S3UploadError as exc:
        raise HTTPException(502, str(exc))
    return {"url": url, "key": key, "bucket": row.s3_bucket}


# ── export ────────────────────────────────────────────────────────────────────

@router.post("/{dataset_id}/export")
async def export(dataset_id: int, body: DatasetExportBody, request: Request,
                 db=Depends(get_db),
                 principal: Principal = Depends(_COLLECT)) -> dict[str, Any]:
    """Push to Label Studio and/or mirror to S3.

    THE TWO DESTINATIONS RUN CONCURRENTLY AND FAIL INDEPENDENTLY. They answer
    different questions — annotation versus keeping — so one failing must never
    cancel the other. asyncio.gather with return_exceptions=True is the whole
    mechanism: a half-done job whose surviving half is recorded beats losing
    both, and the operator re-runs only the failed side by unticking the other.
    """
    row = await _get_or_404(db, dataset_id)

    if not body.to_labelstudio and not body.to_s3:
        raise HTTPException(422, "Choose at least one destination.")
    if row.status != "ready" or not (row.frames or []):
        raise HTTPException(409, "Collect the frames before importing.")

    jobs: list[Any] = []
    order: list[str] = []

    project_id: Optional[int] = None
    if body.to_labelstudio:
        if not labelstudio_client.configured():
            raise HTTPException(503, (
                "Label Studio is not configured on this appliance. Set "
                "LABELSTUDIO_URL and LABELSTUDIO_API_KEY on the api service, "
                "or untick Label Studio to mirror to S3 only."
            ))
        # Most specific first: what this import asked for, then what this
        # dataset last used, then the appliance default.
        project_id = (body.project_id or row.labelstudio_project_id
                      or settings.labelstudio_project_id)
        if not project_id:
            raise HTTPException(422, (
                "Enter the Label Studio project id to import into — it is the "
                "number in the project's URL."
            ))
        jobs.append(labelstudio_client.import_frames(
            int(project_id), row.frames_dir, row.frames))
        order.append("labelstudio")

    bucket = prefix = None
    if body.to_s3:
        bucket = (body.s3_bucket or row.s3_bucket or settings.aws_s3_bucket or "").strip()
        raw_prefix = body.s3_prefix if body.s3_prefix is not None else (
            row.s3_prefix if row.s3_prefix is not None else settings.aws_s3_prefix)
        try:
            # Validated HERE, before anything starts, so a typo is a 422 the
            # operator can fix rather than a failure reported after a
            # multi-minute Label Studio upload has already run.
            bucket = s3_upload.validate_bucket(bucket)
            prefix = s3_upload.normalise_prefix(raw_prefix)
        except s3_upload.S3UploadError as exc:
            raise HTTPException(422, str(exc))
        jobs.append(s3_upload.upload_frames(
            bucket, prefix, row.frames_dir, row.frames))
        order.append("s3")

    results = await asyncio.gather(*jobs, return_exceptions=True)
    outcome: dict[str, Any] = {}
    now = _now()

    for name, result in zip(order, results):
        if isinstance(result, Exception):
            log.warning("dataset.export_failed", dataset_id=dataset_id,
                        destination=name, error=str(result))
            outcome[name] = {"ok": False, "error": str(result)}
            continue
        if name == "labelstudio":
            # Persist ONLY the half that succeeded.
            row.labelstudio_project_id = int(project_id)
            row.labelstudio_task_count = int(result["tasks"])
            row.labelstudio_exported_at = now
            outcome[name] = {"ok": True, "tasks": int(result["tasks"]),
                             "project_id": int(project_id),
                             "sent": result["sent"],
                             "missing_on_disk": result["missing_on_disk"],
                             "missing_examples": result["missing_examples"]}
        else:
            row.s3_bucket = result["bucket"]
            row.s3_prefix = result["prefix"]
            row.s3_uploaded_count = result["uploaded"]
            row.s3_uploaded_at = now
            outcome[name] = {"ok": True, "uploaded": result["uploaded"],
                             "skipped": result["skipped"],
                             "missing_on_disk": result.get("missing_on_disk", 0),
                             "bucket": result["bucket"], "prefix": result["prefix"],
                             "error": result.get("error")}

    row.updated_at = now
    await db.commit()
    await db.refresh(row)

    any_ok = any(v.get("ok") for v in outcome.values())
    await audit_svc.record(
        request, principal, "dataset.exported", target=f"dataset:{dataset_id}",
        detail={
            "camera_slug": row.camera_slug,
            "frames": len(row.frames or []),
            "destinations": {k: bool(v.get("ok")) for k, v in outcome.items()},
            "project_id": project_id,
            "s3_bucket": bucket,
        },
        outcome="success" if all(v.get("ok") for v in outcome.values()) else "failure",
    )

    if not any_ok:
        # Nothing landed anywhere. A 200 here would read as success in the UI.
        raise HTTPException(502, {
            "error": "The export failed.",
            "destinations": outcome,
        })

    return {"dataset": DatasetResponse.from_row(row).model_dump(mode="json"),
            "destinations": outcome}
