"""Operator-drawn boxes: validate the class, write the file, then the row.

ORDERING IS THE WHOLE DESIGN. The NVR writes the JPEG and its JSON sidecar
first; Postgres gets the index row second. Both orders leave a window if the
second step fails, and this is the better window to leave:

  * file, no row  -> recoverable. The sidecar carries every field the row
                    needs, and `annotation_store.adopt_orphans` rebuilds it.
  * row, no file  -> a ghost the UI lists and cannot display. Not recoverable,
                    and it reads as corruption to whoever drew it.

So a failed row write does NOT delete the file. Losing an operator's frame to
tidy up a database error is the wrong trade — and the frame is unrepeatable,
because it was captured at pointer-down and the footage it came from may have
aged out since.

Validation runs before the NVR is called at all: the NVR does not own the
taxonomy and would faithfully record a class that does not exist.

This router also takes over the two annotation carve-outs that used to live
inline in routers/nvr.py — the capability override and the audit record. They
belong here, where the class and the row are visible.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import structlog
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     Request, Response, UploadFile, status)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import (Annotation, AnnotationExportBody,
                      AnnotationResponse, AnnotationUpdateBody, LabelClass)
from ..security import Principal
from ..services import audit as audit_svc
from ..services import annotation_export, nvr_client, qa_sink, s3_upload
from ..services import policy as policy_svc
from ..services.policy import require_capability

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])

# Drawing a box is an operator action, gated exactly as the Playback page it is
# drawn from. This is the carve-out routers/nvr.py used to make inline.
_DRAW = require_capability("playback_search")
# Managing the training pile is not. That is Data Collection's job, and it is
# off by default for operator and viewer.
_MANAGE = require_capability("data_collection")


@router.post("", response_model=AnnotationResponse,
             status_code=status.HTTP_201_CREATED)
async def create_annotation(
    request: Request,
    camera: str = Form(...),
    timestamp: str = Form(...),
    class_key: str = Form(..., max_length=64),
    bbox_x: float = Form(..., ge=0),
    bbox_y: float = Form(..., ge=0),
    bbox_w: float = Form(..., gt=0),
    bbox_h: float = Form(..., gt=0),
    frame_width: int = Form(..., gt=0),
    frame_height: int = Form(..., gt=0),
    dataset_id: Annotated[Optional[int], Form()] = None,
    image: UploadFile = File(...),
    principal: Principal = Depends(_DRAW),
    db: AsyncSession = Depends(get_db),
):
    cls = await db.get(LabelClass, class_key)
    if cls is None or not cls.active:
        raise HTTPException(
            422,
            f"'{class_key}' is not an active label class. Classes are managed "
            f"in Administration -> Label classes.")

    form = {"camera": camera, "timestamp": timestamp, "class_key": class_key,
            "bbox_x": str(bbox_x), "bbox_y": str(bbox_y),
            "bbox_w": str(bbox_w), "bbox_h": str(bbox_h),
            "frame_width": str(frame_width),
            "frame_height": str(frame_height)}
    payload = await nvr_client.save_annotation(
        form,
        {"image": (image.filename or "annotation.jpg",
                   await image.read(), "image/jpeg")})
    if payload is None:
        raise HTTPException(502, "The NVR could not store the annotation")

    captured = datetime.fromtimestamp(
        float(payload["timestamp_epoch"]), tz=timezone.utc)
    # A dataset-attached annotation does not expire: it has a named owner and a
    # parent whose deletion is the intended way to remove it. Everything else
    # is on a clock, so a still derived from footage cannot outlive its source
    # by accident.
    expires_at = None if dataset_id is not None else (
        captured + timedelta(minutes=settings.annotation_ttl_minutes))

    row = Annotation(
        id=uuid.UUID(payload["id"]), camera_slug=camera, class_key=class_key,
        captured_at=captured,
        bbox_x=bbox_x, bbox_y=bbox_y, bbox_w=bbox_w, bbox_h=bbox_h,
        frame_width=frame_width, frame_height=frame_height,
        image_path=payload["image_path"], dataset_id=dataset_id,
        expires_at=expires_at, created_by=principal.subject,
        created_at=datetime.now(timezone.utc))
    db.add(row)

    # Queue it for external review, in THIS transaction. The outbox row and the
    # annotation commit together or neither does, which is the only arrangement
    # where "it is in the index" and "it is queued to be published" cannot
    # disagree. Nothing is sent from here — see services/qa_sink.
    #
    # A sink that is off, or configured so incompletely that nothing could
    # arrive, is silent: drawing a box must behave identically on the
    # overwhelming majority of appliances that have no QA sink at all.
    sink = await qa_sink.get_config(db)
    published = qa_sink.is_publishable(sink)
    if published:
        await qa_sink.enqueue(db, row, sink)

    try:
        await db.commit()
    except Exception as exc:                                    # noqa: BLE001
        # The frame is on disk and STAYS there. adopt_orphans will index it on
        # its next pass; deleting it here would lose an unrepeatable capture.
        log.error("annotation.row_write_failed", annotation_id=payload["id"],
                  image_path=payload["image_path"], error=str(exc))
        raise HTTPException(
            500, "The frame was saved but could not be indexed. It will be "
                 "recovered automatically.") from exc

    await audit_svc.record(request, principal, "annotation.created",
                           target=camera,
                           detail={"class_key": class_key,
                                   "annotation_id": payload["id"],
                                   # Recorded on the CREATE record, not a
                                   # separate one: "this frame was queued to
                                   # leave the appliance" is a property of the
                                   # capture, and a reader reconstructing what
                                   # happened to it should not have to join two
                                   # audit entries to find out.
                                   "queued_for_qa": published})
    return AnnotationResponse.from_row(row)


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(annotation_id: uuid.UUID, request: Request,
                            principal: Principal = Depends(_MANAGE),
                            db: AsyncSession = Depends(get_db)):
    row = await db.get(Annotation, annotation_id)
    if row is None:
        raise HTTPException(404, "No such annotation")
    # File first, row second — the same rule as create, and sharper here:
    # dropping the row while the file survives hands it straight back to the
    # adoption pass, which would re-index what we just tried to remove.
    if not await nvr_client.delete_annotation(annotation_id.hex):
        raise HTTPException(502, "The NVR could not delete the annotation")
    # BEFORE the row goes. The outbox row holds the object key and dies with
    # the annotation by cascade, so once this row is deleted nothing on the
    # appliance knows which object in a public bucket to remove.
    await qa_sink.forget_images(db, [annotation_id])
    camera_slug, class_key = row.camera_slug, row.class_key
    await db.delete(row)
    await db.commit()
    await audit_svc.record(request, principal, "annotation.deleted",
                           target=camera_slug,
                           detail={"class_key": class_key,
                                   "annotation_id": annotation_id.hex})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{annotation_id}", response_model=AnnotationResponse)
async def update_annotation(annotation_id: uuid.UUID,
                            body: AnnotationUpdateBody, request: Request,
                            principal: Principal = Depends(_DRAW),
                            db: AsyncSession = Depends(get_db)):
    """Relabel an annotation, or decide to keep it past its deadline.

    TWO DIFFERENT AUTHORITIES IN ONE ENDPOINT, gated separately.

    Relabelling is an operator act — the same person who drew the box, fixing
    their own misclick — so it rides on `playback_search`, the capability that
    let them draw it. Making it harder than drawing would mean the only remedy
    for a wrong class is delete, and delete is irreversible here: the frame was
    captured at pointer-down and the footage behind it may have aged out.

    Clearing the expiry is not an operator act. It decides what this appliance
    keeps, it is what `posture.derived_stills` counts as
    `annotations_without_expiry`, and somebody has to answer for that number —
    so it additionally requires `data_collection`.
    """
    row = await db.get(Annotation, annotation_id)
    if row is None:
        raise HTTPException(404, "No such annotation")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "Nothing to change")

    detail: dict = {"annotation_id": annotation_id.hex}
    actions: list[str] = []

    if "class_key" in changes and changes["class_key"] is not None:
        cls = await db.get(LabelClass, changes["class_key"])
        if cls is None or not cls.active:
            raise HTTPException(
                422,
                f"'{changes['class_key']}' is not an active label class. "
                f"Classes are managed in Administration -> Label classes.")
        detail["from_class"] = row.class_key
        detail["to_class"] = cls.key
        row.class_key = cls.key
        actions.append("annotation.relabelled")

    if "keep" in changes and changes["keep"] is not None:
        await policy_svc.require(principal, "data_collection")
        if changes["keep"]:
            row.expires_at = None
            row.kept_reason = changes.get("kept_reason") or row.kept_reason
            detail["kept_reason"] = row.kept_reason
            actions.append("annotation.kept")
        else:
            # Released back onto the clock, measured from CAPTURE rather than
            # from now: the deadline is a property of when the frame was taken,
            # so releasing must not silently extend it.
            row.expires_at = row.captured_at + timedelta(
                minutes=settings.annotation_ttl_minutes)
            row.kept_reason = None
            actions.append("annotation.released")

    await db.commit()
    for action in actions:
        await audit_svc.record(request, principal, action,
                               target=row.camera_slug, detail=detail)
    return AnnotationResponse.from_row(row)


# ── Reading the pile back ───────────────────────────────────────────────────
# Without these, annotate is a one-way door: an operator can mark the thing
# that mattered and never find it again. `/stats` is declared before the
# id-shaped routes so the literal path can never be swallowed by a converter.


@router.get("/stats")
async def annotation_stats(principal: Principal = Depends(_MANAGE),
                           db: AsyncSession = Depends(get_db)):
    """Count per class, and per camera.

    BY_CLASS is the number that says which class is under-sampled, and it is the
    whole reason to have a taxonomy instead of free text — a grid of thumbnails
    cannot tell you what is missing.

    BY_CAMERA IS HERE RATHER THAN DERIVED FROM THE CAMERA REGISTRY, because
    those two sets are not the same and the difference is deliberate: migration
    047 keeps `camera_slug` out of a foreign key so an annotation outlives the
    camera row it was drawn on. A picker built from /cameras would therefore be
    wrong in both directions — listing cameras that have nothing to show, and
    omitting a decommissioned one whose training material is still here, which
    is the case that FK decision exists to protect. Counting the annotations
    themselves is the only answer that matches what a filter would return.
    """
    by_class_rows = await db.execute(
        select(Annotation.class_key, func.count())
        .group_by(Annotation.class_key))
    by_class = {key: n for key, n in by_class_rows.all()}
    by_camera_rows = await db.execute(
        select(Annotation.camera_slug, func.count())
        .group_by(Annotation.camera_slug)
        .order_by(Annotation.camera_slug))
    by_camera = {slug: n for slug, n in by_camera_rows.all()}
    return {"by_class": by_class, "by_camera": by_camera,
            "total": sum(by_class.values())}


@router.get("")
async def list_annotations(
    camera: Optional[str] = None,
    class_key: Optional[str] = None,
    dataset_id: Optional[int] = None,
    # Annotated rather than `= Query(...)`: a bare Query() default is an object,
    # not a value, so the handler could only ever be driven over HTTP. These are
    # real defaults, which is what lets the *_db suite call it directly.
    from_: Annotated[Optional[datetime], Query(alias="from")] = None,
    to: Annotated[Optional[datetime], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    principal: Principal = Depends(_MANAGE),
    db: AsyncSession = Depends(get_db),
):
    where = []
    if camera:
        where.append(Annotation.camera_slug == camera)
    if class_key:
        where.append(Annotation.class_key == class_key)
    if dataset_id is not None:
        where.append(Annotation.dataset_id == dataset_id)
    if from_:
        where.append(Annotation.captured_at >= from_)
    if to:
        where.append(Annotation.captured_at <= to)

    total = await db.scalar(
        select(func.count()).select_from(Annotation).where(*where))
    rows = (await db.execute(
        select(Annotation).where(*where)
        .order_by(Annotation.captured_at.desc())
        .limit(limit).offset(offset))).scalars().all()
    return {"annotations": [AnnotationResponse.from_row(r) for r in rows],
            "total": total or 0}


@router.get("/{annotation_id}/image")
async def annotation_image(annotation_id: uuid.UUID,
                           principal: Principal = Depends(_DRAW),
                           db: AsyncSession = Depends(get_db)):
    """Stream the frame through from the NVR.

    Gated on `playback_search` rather than `data_collection`: it is the frame
    the operator just drew on, and a save they cannot look at reads as lost.
    """
    row = await db.get(Annotation, annotation_id)
    if row is None:
        raise HTTPException(404, "No such annotation")
    blob = await nvr_client.fetch_annotation_image(annotation_id.hex)
    if blob is None:
        # A 502 rather than a broken <img>: the index says this frame exists,
        # so its absence is an NVR problem the operator should be told about.
        raise HTTPException(502, "The NVR could not return the frame")
    return Response(content=blob, media_type="image/jpeg")


@router.post("/export")
async def export_annotations(body: AnnotationExportBody, request: Request,
                             principal: Principal = Depends(_MANAGE),
                             db: AsyncSession = Depends(get_db)):
    """COCO for the selected annotations. Uploaded to S3 when a bucket is named.

    Label Studio is deliberately NOT a destination here. `import_frames` pushes
    frames into a project an operator owns, and the taxonomy reaches that
    project as the pasteable snippet from GET /api/label-classes/label-config.
    Pushing a label config from this side is precisely what labelstudio_client
    refuses to do, and for a good reason: the project's config is theirs.
    """
    doc = await annotation_export.build_coco(
        db, camera=body.camera, class_keys=body.class_keys,
        from_dt=body.from_, to_dt=body.to)

    s3_key = None
    if body.s3_bucket:
        s3_key = await s3_upload.upload_json(
            body.s3_bucket, body.s3_prefix or "", "annotations.coco.json", doc)

    await audit_svc.record(
        request, principal, "annotation.exported",
        target=body.camera or "all",
        detail={"annotations": len(doc["annotations"]),
                "categories": len(doc["categories"]),
                "s3_bucket": body.s3_bucket, "s3_key": s3_key})
    return {"coco": doc, "s3_key": s3_key}
