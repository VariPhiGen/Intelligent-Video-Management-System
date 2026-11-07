"""Reconcile the NVR's annotation files with this database's index.

Writing the file before the index row is what makes an annotation recoverable,
and this module is the half that recovers it. Every field a row needs is in the
sidecar, so an orphan file is a complete record waiting for an index entry.

IDEMPOTENT BY CONSTRUCTION: the sidecar's `id` IS the row's primary key, so a
second pass over the same file finds the row already there. That is the whole
reason the NVR's id is reused rather than a new one minted here — without it a
retried pass would duplicate training data, and duplicated training data is
invisible until it skews a model.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from ..config import settings
from ..db import AsyncSessionLocal
from ..models import Annotation, LabelClass
from . import nvr_client, qa_sink

log = structlog.get_logger(__name__)

# Annotations drawn before the taxonomy existed carry no class. Discarding them
# would lose frames nobody agreed to lose, and guessing a class would be worse
# — a wrong label is more expensive than no label. They are adopted against an
# inert holder instead: retired, so nothing new can be drawn as it, and visible,
# so an operator can see exactly what predates the taxonomy.
UNCLASSIFIED_KEY = "unclassified"

_INTERVAL_SECONDS = 3600


async def _ensure_unclassified(db) -> None:
    """Create the inert holder once, on first need — never at migration time.

    A migration that seeded it would put a class on every appliance, including
    the ones that have no legacy annotations and would then show a class nobody
    created.
    """
    if await db.get(LabelClass, UNCLASSIFIED_KEY) is None:
        db.add(LabelClass(
            key=UNCLASSIFIED_KEY, label="Unclassified", color="#808080",
            sort_order=9999, active=False,
            description="Annotations drawn before the taxonomy existed. "
                        "Retired: cannot be chosen for new work."))
        await db.flush()


async def adopt_orphans(db) -> int:
    """Index every annotation file that has no row. Returns how many."""
    sidecars = await nvr_client.list_annotations()
    if sidecars is None:
        # None and [] are different answers. "Could not look" must not be
        # recorded as "nothing to adopt".
        log.warning("annotation.adopt.nvr_unreachable")
        return 0
    if not sidecars:
        return 0

    ids = set()
    for s in sidecars:
        try:
            ids.add(uuid.UUID(s["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    known = set((await db.execute(
        select(Annotation.id).where(Annotation.id.in_(ids))
    )).scalars().all()) if ids else set()

    # Read once for the whole pass rather than per sidecar: the destination
    # cannot change halfway through a batch, and a thousand orphans should not
    # mean a thousand identical reads of one singleton row.
    sink = await qa_sink.get_config(db)
    if not qa_sink.is_publishable(sink):
        sink = None

    adopted = 0
    for s in sidecars:
        try:
            aid = uuid.UUID(s["id"])
        except (KeyError, TypeError, ValueError):
            log.warning("annotation.adopt.bad_sidecar", sidecar=str(s)[:200])
            continue
        if aid in known:
            continue

        class_key = s.get("class_key")
        if not class_key:
            await _ensure_unclassified(db)
            class_key = UNCLASSIFIED_KEY
        elif await db.get(LabelClass, class_key) is None:
            # The FK should make this impossible. Adoption is still not the
            # place to lose a frame over it: park it on the holder and say so.
            log.warning("annotation.adopt.unknown_class", class_key=class_key,
                        annotation_id=s["id"])
            await _ensure_unclassified(db)
            class_key = UNCLASSIFIED_KEY

        try:
            captured = datetime.fromtimestamp(
                float(s["timestamp_epoch"]), tz=timezone.utc)
            bbox, frame = s["bbox"], s["frame"]
            row = Annotation(
                id=aid, camera_slug=s["camera"], class_key=class_key,
                captured_at=captured,
                bbox_x=float(bbox["x"]), bbox_y=float(bbox["y"]),
                bbox_w=float(bbox["width"]), bbox_h=float(bbox["height"]),
                frame_width=int(frame["width"]),
                frame_height=int(frame["height"]),
                image_path=s["image_path"],
                expires_at=captured + timedelta(
                    minutes=settings.annotation_ttl_minutes),
                created_at=datetime.now(timezone.utc))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("annotation.adopt.bad_sidecar", annotation_id=s.get("id"),
                        error=str(exc))
            continue
        db.add(row)
        # An adopted annotation is one whose row write failed on the way in.
        # Without this it would be indexed and then never published: the create
        # path writes the annotation and its outbox entry in ONE transaction,
        # so a failure there loses both, and only the sidecar survives to be
        # recovered. Recovering the index while silently dropping the publish
        # would make the recovery look complete and leave the operator's box
        # out of the review queue for good.
        if sink is not None:
            await qa_sink.enqueue(db, row, sink)
        known.add(aid)
        adopted += 1

    if adopted:
        await db.commit()
        log.info("annotation.adopt.done", adopted=adopted)
    return adopted


async def adoption_loop() -> None:
    """Run adoption at startup and hourly thereafter."""
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await adopt_orphans(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("annotation.adopt.failed", error=str(exc))
        await asyncio.sleep(_INTERVAL_SECONDS)


async def purge_expired(db) -> int:
    """Delete annotations whose deadline has passed. Returns how many.

    FILE FIRST, ROW SECOND — the same ordering as create, and sharper here:
    dropping the row while the file survives hands it straight back to
    `adopt_orphans`, which would re-index exactly what this sweep is trying to
    remove. A file the NVR could not delete keeps its row so the next pass
    retries it, and one stuck file does not block the rest of the batch.

    `expires_at IS NULL` means dataset-attached: a named owner and a deletable
    parent, so its removal is the dataset's business, not this sweep's.
    """
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Annotation)
        .where(Annotation.expires_at.is_not(None),
               Annotation.expires_at < now)
        .limit(500))).scalars().all()

    # The mirrored copies go first, for the same reason the NVR file does: the
    # outbox row that records WHICH object belongs to this annotation is
    # removed by cascade the moment the row is deleted. Afterwards there is
    # nothing left to look the key up from, and a still of an identifiable
    # person would sit in a public bucket past the deadline that was supposed
    # to remove it.
    await qa_sink.forget_images(db, [row.id for row in rows])

    removed = 0
    for row in rows:
        if not await nvr_client.delete_annotation(row.id.hex):
            log.warning("annotation.retention.file_delete_failed",
                        annotation_id=row.id.hex)
            continue
        await db.delete(row)
        removed += 1

    if removed:
        await db.commit()
        log.info("annotation.retention.swept", removed=removed)
    return removed


async def retention_loop() -> None:
    """Hourly sweep, matching presence_retention's cadence."""
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await purge_expired(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("annotation.retention.failed", error=str(exc))
        await asyncio.sleep(_INTERVAL_SECONDS)


async def erase_range(db, slug: str, from_dt: datetime,
                      to_dt: datetime) -> int | None:
    """Erase every annotation of `slug` captured inside the window.

    Returns the count, or None if any file could not be deleted — None is what
    stops `fulfil_dsr` marking the request fulfilled. Returning a count on a
    partial sweep would close the register on an erasure that did not happen,
    which is the defect this function exists to fix.

    Rows whose file could not be deleted are KEPT, so a retry finds them again.
    """
    rows = (await db.execute(
        select(Annotation).where(
            Annotation.camera_slug == slug,
            Annotation.captured_at >= from_dt,
            Annotation.captured_at <= to_dt))).scalars().all()

    # Same ordering as purge_expired, and it matters more here: this is an
    # erasure request, and a frame left in a public bucket is the one copy an
    # auditor is most likely to find.
    stuck_images = await qa_sink.forget_images(db, [row.id for row in rows])

    deleted, failed = 0, stuck_images
    for row in rows:
        if not await nvr_client.delete_annotation(row.id.hex):
            failed += 1
            continue
        await db.delete(row)
        deleted += 1
    if deleted:
        await db.commit()
    if failed:
        log.warning("annotation.erase.partial", slug=slug, failed=failed,
                    deleted=deleted)
        return None
    return deleted
