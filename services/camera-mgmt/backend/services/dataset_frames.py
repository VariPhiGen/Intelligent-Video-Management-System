"""Erase collected dataset frames that fall inside an erasure window.

The same defect as annotations, at larger scale: Data Collection writes a
durable pile of stills under `datasets.frames_dir` that nothing expires and,
until now, nothing erased. A 20 000-frame dataset was 20 000 unerasable images
of whoever walked past.

THE MANIFEST IS THE INDEX. `datasets.frames` holds one
{filename, timestamp, sha256, bytes} per image, so the frames inside a window
are selectable without walking the directory — and rewriting the manifest is
what keeps the export honest afterwards, because the export reads the manifest
rather than the directory.

A dataset emptied by an erasure is KEPT, with an empty manifest. The recipe is
still valid and re-collectable, and silently deleting an operator's dataset row
during somebody else's erasure request would be a surprise nobody asked for.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy import select

from ..models import Dataset

log = structlog.get_logger(__name__)


def _within(stamp: str, from_dt: datetime, to_dt: datetime) -> bool:
    """Is this manifest entry inside the window?

    An entry we cannot date cannot be proven OUTSIDE the window, so it is
    treated as inside. Over-erasing working material is recoverable by
    re-collecting; under-erasing is a false fulfilment, which is not.
    """
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=from_dt.tzinfo)
    return from_dt <= when <= to_dt


async def erase_range(db, slug: str, from_dt: datetime,
                      to_dt: datetime) -> int | None:
    """Unlink dataset frames of `slug` inside the window. None on failure."""
    rows = (await db.execute(
        select(Dataset).where(Dataset.camera_slug == slug))).scalars().all()

    deleted, failed, touched = 0, 0, False
    for ds in rows:
        if not ds.frames or not ds.frames_dir:
            continue
        keep, drop = [], []
        for frame in ds.frames:
            (drop if _within(frame.get("timestamp", ""), from_dt, to_dt)
             else keep).append(frame)
        if not drop:
            continue

        directory = Path(ds.frames_dir)
        for frame in drop:
            try:
                (directory / frame["filename"]).unlink(missing_ok=True)
                deleted += 1
            except (OSError, KeyError) as exc:
                log.warning("dataset.erase.unlink_failed", dataset_id=ds.id,
                            filename=frame.get("filename"), error=str(exc))
                failed += 1

        # Reassigned rather than mutated: JSONB columns only mark themselves
        # dirty on assignment, so an in-place edit would be silently dropped.
        ds.frames = keep
        ds.frames_collected = len(keep)
        ds.bytes_total = sum(int(f.get("bytes", 0)) for f in keep)
        touched = True

    if touched:
        await db.commit()
    if failed:
        log.warning("dataset.erase.partial", slug=slug, failed=failed,
                    deleted=deleted)
        return None
    return deleted
