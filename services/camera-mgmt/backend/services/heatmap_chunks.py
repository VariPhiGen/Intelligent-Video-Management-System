"""heatmap_chunks.py — 5-minute heat arrays: ingest, and the heatmap of any time range.

WHERE THEY COME FROM. The analytics service accumulates heat per camera (the
`Heatmap_overlay` activity) and, every 5 minutes on the local clock, hands over
the heat ADDED in those 5 minutes as a float32 numpy array (.npz), with the
newest camera frame to draw heatmaps over.

WHAT THIS MODULE DOES WITH THEM.
  * `store()`  -- the array goes to MinIO and a row indexes it. The frame is
    NOT archived: it overwrites the camera's one row in `heatmap_backgrounds`,
    so the previous frame is gone the moment the next one arrives.
  * `render()` -- the Heatmap page asks for "from A to B". Every block that
    overlaps the range is loaded and ADDED UP (that is the cumulative heat of
    the range), plus, when B is now, the block analytics is still filling, so
    the picture is up to the minute. The sum is rendered exactly as analytics
    renders: Gaussian blur, JET colour map, blended 40/60 over the CURRENT
    camera frame -- the live one when analytics is running, else the stored
    one -- whatever the range, so a past range shows past heat on today's scene. The colour scale is always RELATIVE: the hottest
    spot in this range is red, like ondemand-heatmap's NORM_MINMAX -- a long
    range does not saturate, but colours mean "compared with this range".

RETENTION. Each camera's Heatmap overlay activity carries two caps the operator
sets in AI Config: `retention_days` and `storage_mb`. The sweep deletes the
camera's OLDEST blocks past either one, objects first and rows second. The caps
are read on every sweep, so raising or lowering one takes effect at once.

GRANULARITY. A range is answered in whole blocks: a block counts if any of it is
inside the range, so the edges are accurate to 5 minutes. The summary says
exactly which period the blocks cover.
"""
from __future__ import annotations

import asyncio
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Camera, CameraStage, HeatmapBackground, HeatmapChunk
from . import analytics_client, detection_engine, object_store, retention_units

log = structlog.get_logger(__name__)

#: Rendering, identical to the analytics service (analytics/heatmap.py).
BLUR_PX = 31
FRAME_ALPHA = 0.4
OUTPUT_WIDTH = 1280
JPEG_QUALITY = 85

#: The longest range one request may add up: 31 days of 5-minute blocks.
MAX_RANGE = timedelta(days=31)
#: A range ending this close to now also takes the block analytics is filling.
LIVE_SLACK = timedelta(seconds=90)
#: Parallel object reads while summing.
_READERS = 16
_PURGE_CHUNK = 500

#: The storage caps when a camera's config does not set them. Must match the
#: activity's defaults (analytics/activities/Heatmap_overlay.py).
DEFAULT_RETENTION_DAYS = 30
DEFAULT_STORAGE_MB = 500
#: Blocks from before migration 079 measured per sweep (one HEAD each).
_MEASURE_CHUNK = 500


def caps(analytics_config: Optional[Mapping]) -> tuple[int, int]:
    """(retention_days, storage_mb) from a camera's Heatmap overlay settings.
    Anything missing or out of range falls back to the default, as analytics
    itself does with a bad setting."""
    params: Mapping = {}
    if isinstance(analytics_config, Mapping):
        for act in analytics_config.get("activities") or []:
            if isinstance(act, Mapping) and act.get("type") == detection_engine.HEATMAP_ACTIVITY:
                params = act.get("params") or {}
                break

    def num(key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = float(params.get(key))
        except (TypeError, ValueError):
            return default
        return int(value) if lo <= value <= hi and value == int(value) else default

    return (num("retention_days", DEFAULT_RETENTION_DAYS, 1, 3650),
            num("storage_mb", DEFAULT_STORAGE_MB, 10, 1_000_000))


class Rejected(Exception):
    """The upload cannot be stored, and repeating it will not help."""


class WrongEngine(Rejected):
    """The producer is not the engine this appliance runs."""


def _at(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError):
        return None


def load_heat(data: bytes):
    """The `heat` array of one .npz. Raises ValueError if it is not one."""
    import numpy as np
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        if "heat" not in z.files:
            raise ValueError("no 'heat' array")
        heat = z["heat"]
    if heat.ndim != 2 or heat.dtype.kind != "f":
        raise ValueError(f"heat must be a 2-D float array, got {heat.dtype} {heat.shape}")
    return heat.astype("float32", copy=False)


def chunk_key(slug: str, start: datetime) -> str:
    """heatmaps/{slug}/{YYYY-MM-DD}/chunks/{HHMMSS}.npz. Local date and time, like
    every other heatmap path, so a folder reads as the operator's own day."""
    when = start.astimezone()
    return f"{object_store.ROOT}/{slug}/{when:%Y-%m-%d}/chunks/{when:%H%M%S}.npz"


async def _camera_by_slug(db: AsyncSession, slug: str) -> Optional[Camera]:
    return (await db.execute(
        select(Camera).where(Camera.slug == slug,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()


async def store(db: AsyncSession, meta: Mapping[str, Any], heat: bytes,
                background: Optional[bytes]) -> dict:
    """Store one block. Idempotent on (camera, period_start): a retry overwrites."""
    slug = str(meta.get("camera") or "").strip()
    start, end = _at(meta.get("period_start")), _at(meta.get("period_end"))
    if not slug or start is None or end is None or end < start:
        raise Rejected("camera, period_start and period_end (>= start) are required")
    if not object_store.is_configured():
        raise Rejected("no object store is configured for heat blocks")
    camera = await _camera_by_slug(db, slug)
    if camera is None:
        raise Rejected(f"unknown camera {slug!r}")
    source = str(meta.get("source") or "cpu").strip().lower()
    if not detection_engine.heatmap_source_ok(camera, source):
        raise WrongEngine(f"{source!r} may not write heatmaps: the engine is "
                          f"{detection_engine.engine()!r}")
    try:
        shape = await asyncio.to_thread(lambda: load_heat(heat).shape)
    except Exception as exc:                                       # noqa: BLE001
        raise Rejected(f"heat is not a usable .npz: {exc}") from exc

    # Objects BEFORE the row, so a row never names an object that is not there.
    key = await asyncio.to_thread(object_store.put, chunk_key(slug, start), heat,
                                  "application/octet-stream")
    if background:
        # The frame is a picture of the scene, so it follows FOOTAGE retention.
        await _keep_background(db, camera, background, end,
                               retention_units.effective_retention_minutes(
                                   getattr(camera, "retention_minutes", None)))
    days, _ = caps(camera.analytics_config)
    table = HeatmapChunk.__table__
    stmt = pg_insert(table).values(
        camera_id=camera.id, period_start=start, period_end=end,
        frame_width=int(meta.get("frame_width") or shape[1]),
        frame_height=int(meta.get("frame_height") or shape[0]),
        scale=int(meta.get("scale") or 4), points=int(meta.get("points") or 0),
        peak=float(meta.get("peak") or 0.0), params=dict(meta.get("params") or {}),
        object_key=key, size_bytes=len(heat),
        expires_at=end + timedelta(days=days),
    )
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.camera_id, table.c.period_start],
        set_={"period_end": stmt.excluded.period_end, "points": stmt.excluded.points,
              "peak": stmt.excluded.peak, "object_key": stmt.excluded.object_key,
              "size_bytes": stmt.excluded.size_bytes,
              "expires_at": stmt.excluded.expires_at},
    ))
    await db.commit()
    return {"stored": True, "camera": slug, "object_key": key,
            "background": bool(background), "shape": list(shape)}


async def _keep_background(db: AsyncSession, camera: Camera, image: bytes,
                           captured_at: datetime, retention: int) -> None:
    """Make this the camera's one background frame, replacing the previous one.

    A retried or late block must not put an OLDER frame back over a newer one,
    so the row only moves forward in time."""
    table = HeatmapBackground.__table__
    stmt = pg_insert(table).values(
        camera_id=camera.id, image=image, captured_at=captured_at,
        expires_at=captured_at + timedelta(minutes=retention))
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.camera_id],
        set_={"image": stmt.excluded.image, "captured_at": stmt.excluded.captured_at,
              "expires_at": stmt.excluded.expires_at},
        where=table.c.captured_at <= stmt.excluded.captured_at))


# ── a range ──────────────────────────────────────────────────────────────────

async def _rows(db: AsyncSession, camera_id: uuid.UUID, start: datetime,
                end: datetime) -> list[HeatmapChunk]:
    return list((await db.execute(
        select(HeatmapChunk)
        .where(HeatmapChunk.camera_id == camera_id,
               HeatmapChunk.period_start < end, HeatmapChunk.period_end > start)
        .order_by(HeatmapChunk.period_start)
    )).scalars().all())


async def _stored_background(db: AsyncSession,
                             camera_id: uuid.UUID) -> tuple[Optional[bytes], Optional[datetime]]:
    """The camera's one stored frame and when it was taken, or (None, None)."""
    row = (await db.execute(
        select(HeatmapBackground.image, HeatmapBackground.captured_at)
        .where(HeatmapBackground.camera_id == camera_id)
    )).first()
    return (bytes(row[0]), row[1]) if row else (None, None)


async def _partial(slug: str, start: datetime, end: datetime) -> Optional[dict]:
    """The block analytics is still filling, if the range reaches it."""
    if end < datetime.now(timezone.utc) - LIVE_SLACK or not analytics_client.is_configured():
        return None
    try:
        resp = await analytics_client._get_client().get(
            f"{analytics_client._base()}/cameras/{slug}/heatmap/partial")
    except Exception as exc:                                       # noqa: BLE001
        log.info("heatmap.partial.unreachable", camera=slug, error=str(exc))
        return None
    if resp.status_code != 200:
        return None

    def decode(data: bytes) -> Optional[dict]:
        import numpy as np
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            return {"meta": meta, "heat": z["heat"].astype("float32"),
                    "background": z["background"].tobytes() or None}
    try:
        part = await asyncio.to_thread(decode, resp.content)
    except Exception as exc:                                       # noqa: BLE001
        log.warning("heatmap.partial.unreadable", camera=slug, error=str(exc))
        return None
    p_start = _at(part["meta"].get("period_start"))
    if p_start is None or p_start >= end:
        return None
    part["start"], part["end"] = p_start, _at(part["meta"].get("period_end")) or end
    return part


def _validate(start: datetime, end: datetime) -> None:
    if end <= start:
        raise Rejected("the end of the range must be after its start")
    if end - start > MAX_RANGE:
        raise Rejected(f"a range can be at most {MAX_RANGE.days} days")


async def summary(db: AsyncSession, camera: Camera, start: datetime, end: datetime) -> dict:
    """What a range holds, without loading any heat: cheap enough to poll."""
    _validate(start, end)
    rows = await _rows(db, camera.id, start, end)
    live = await _partial(camera.slug, start, end)
    covered = [(r.period_start, r.period_end) for r in rows]
    if live:
        covered.append((live["start"], live["end"]))
    bg_at = None
    if not (live and live["background"]):
        bg_at = (await db.execute(
            select(HeatmapBackground.captured_at)
            .where(HeatmapBackground.camera_id == camera.id)
        )).scalar()
    return {
        "camera_id": str(camera.id), "name": camera.name,
        "start": start.isoformat(), "end": end.isoformat(),
        "blocks": len(rows), "includes_live": bool(live),
        "points": sum(int(r.points) for r in rows) + (int(live["meta"].get("points") or 0) if live else 0),
        "covered_from": min(c[0] for c in covered).isoformat() if covered else None,
        "covered_to": max(c[1] for c in covered).isoformat() if covered else None,
        "background_at": ("live" if live and live["background"]
                          else bg_at.isoformat() if bg_at else None),
    }


async def render(db: AsyncSession, camera: Camera, start: datetime, end: datetime) -> Optional[bytes]:
    """The range's summed heat over the current frame, as a JPEG. None = nothing to draw."""
    _validate(start, end)
    rows = await _rows(db, camera.id, start, end)
    live = await _partial(camera.slug, start, end)
    bg_bytes = live["background"] if live and live["background"] else None
    if bg_bytes is None:
        bg_bytes, _ = await _stored_background(db, camera.id)
    if not rows and not live and bg_bytes is None:
        return None

    sem = asyncio.Semaphore(_READERS)

    async def read(row: HeatmapChunk):
        async with sem:
            data = await asyncio.to_thread(object_store.get, row.object_key)
        return (await asyncio.to_thread(load_heat, data)) if data else None

    heats = [h for h in await asyncio.gather(*(read(r) for r in rows)) if h is not None]
    if live:
        heats.append(live["heat"])
    newest = rows[-1] if rows else None
    scale = int(live["meta"].get("scale") or 4) if live else (newest.scale if newest else 4)
    frame_w = (int(live["meta"].get("frame_width") or 0) if live
               else (newest.frame_width if newest else 0)) or OUTPUT_WIDTH
    frame_h = (int(live["meta"].get("frame_height") or 0) if live
               else (newest.frame_height if newest else 0)) or round(OUTPUT_WIDTH * 9 / 16)
    return await asyncio.to_thread(_draw, heats, bg_bytes, scale, frame_w, frame_h)


def sum_heat(heats: list, empty_shape: tuple[int, int]):
    """The range's heat: every block added up. The newest block's grid is the
    canvas; a block from another frame size (the camera was reconfigured) is
    resampled onto it rather than dropped."""
    import cv2
    import numpy as np
    th, tw = heats[-1].shape if heats else empty_shape
    total = np.zeros((th, tw), np.float32)
    for h in heats:
        total += h if h.shape == (th, tw) else cv2.resize(h, (tw, th), interpolation=cv2.INTER_LINEAR)
    return total


def _draw(heats: list, bg_bytes: Optional[bytes], scale: int, frame_w: int, frame_h: int) -> bytes:
    import cv2
    import numpy as np

    total = sum_heat(heats, (max(1, frame_h // scale), max(1, frame_w // scale)))

    frame = None
    if bg_bytes:
        frame = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        frame = np.full((frame_h, frame_w, 3), 64, np.uint8)        # no frame stored yet
    h, w = frame.shape[:2]
    if w > OUTPUT_WIDTH:
        h, w = max(1, round(h * OUTPUT_WIDTH / w)), OUTPUT_WIDTH
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)

    kernel = max(3, int(round(BLUR_PX / max(1, scale))) | 1)
    blurred = cv2.GaussianBlur(total, (kernel, kernel), 0)
    # RELATIVE: the hottest point after the blur is red. Normalised AFTER the
    # blur, so one stray pixel cannot set the scale for the whole picture.
    top = float(blurred.max())
    clipped = np.clip(blurred * (255.0 / top), 0, 255).astype(np.uint8) if top > 0 \
        else np.zeros(blurred.shape, np.uint8)
    heat_img = cv2.applyColorMap(cv2.resize(clipped, (w, h), interpolation=cv2.INTER_LINEAR),
                                 cv2.COLORMAP_JET)
    overlay = cv2.addWeighted(frame, FRAME_ALPHA, heat_img, 1.0 - FRAME_ALPHA, 0)
    ok, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("could not encode the heatmap")
    return buf.tobytes()


# ── retention ────────────────────────────────────────────────────────────────

async def _delete_blocks(db: AsyncSession, where: str, params: dict) -> int:
    """Delete the blocks matching `where`, oldest first, in chunks: objects
    first, then rows, so a failed object delete is retried next sweep instead
    of leaving an object no row names."""
    total = 0
    while True:
        rows = (await db.execute(text(
            "SELECT id, object_key, background_key FROM heatmap_chunks"
            f" WHERE {where} ORDER BY period_end LIMIT :n"),
            {**params, "n": _PURGE_CHUNK})).all()
        if not rows:
            break
        keys = [k for r in rows for k in (r[1], r[2]) if k]
        if keys and object_store.is_configured():
            await asyncio.to_thread(object_store.delete_many, keys)
        res = await db.execute(text("DELETE FROM heatmap_chunks WHERE id = ANY(:ids)"),
                               {"ids": [r[0] for r in rows]})
        await db.commit()
        total += res.rowcount or 0
        if len(rows) < _PURGE_CHUNK:
            break
    return total


async def _measure(db: AsyncSession, camera_id: uuid.UUID) -> None:
    """Fill in `size_bytes` for blocks stored before it was recorded."""
    if not object_store.is_configured():
        return
    rows = (await db.execute(text(
        "SELECT id, object_key FROM heatmap_chunks"
        " WHERE camera_id = :c AND size_bytes IS NULL LIMIT :n"),
        {"c": camera_id, "n": _MEASURE_CHUNK})).all()
    for row_id, key in rows:
        size = await asyncio.to_thread(object_store.size, key)
        await db.execute(text("UPDATE heatmap_chunks SET size_bytes = :s WHERE id = :i"),
                         {"s": size or 0, "i": row_id})
    if rows:
        await db.commit()


async def enforce_caps(db: AsyncSession, camera_id: uuid.UUID,
                       analytics_config: Optional[Mapping],
                       now: Optional[datetime] = None) -> int:
    """Delete this camera's oldest blocks past its days cap, then past its
    storage cap. Returns how many were deleted."""
    days, mb = caps(analytics_config)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    deleted = await _delete_blocks(db, "camera_id = :c AND period_end < :cut",
                                   {"c": camera_id, "cut": cutoff})
    await _measure(db, camera_id)
    over = (await db.execute(text(
        "SELECT COALESCE(SUM(size_bytes), 0) FROM heatmap_chunks WHERE camera_id = :c"),
        {"c": camera_id})).scalar() - mb * 1024 * 1024
    if over > 0:
        # The oldest blocks whose sizes, added up, bring the camera under its
        # cap -- chosen ONCE, since the running total changes as rows go.
        ids = (await db.execute(text(
            "SELECT id FROM (SELECT id, SUM(COALESCE(size_bytes, 0))"
            " OVER (ORDER BY period_end, id) - COALESCE(size_bytes, 0) AS before"
            " FROM heatmap_chunks WHERE camera_id = :c) t WHERE before < :over"),
            {"c": camera_id, "over": over})).scalars().all()
        deleted += await _delete_blocks(db, "id = ANY(:ids)", {"ids": list(ids)})
    return deleted


async def purge_expired(db: AsyncSession) -> int:
    """Every camera's heat blocks past its caps (see `enforce_caps`), and any
    background frame nobody has refreshed for the camera's footage retention."""
    cameras = (await db.execute(
        select(Camera.id, Camera.analytics_config)
        .where(Camera.id.in_(select(HeatmapChunk.camera_id).distinct()))
    )).all()
    total = 0
    for camera_id, config in cameras:
        total += await enforce_caps(db, camera_id, config)
    res = await db.execute(text("DELETE FROM heatmap_backgrounds WHERE expires_at < now()"))
    await db.commit()
    return total + (res.rowcount or 0)


# ── what is available ────────────────────────────────────────────────────────

async def availability(db: AsyncSession, camera: Camera) -> dict:
    """Which local days have heat, from when, and how close the caps are.

    Days are taken from 15-minute buckets converted on the appliance clock, so
    a half-hour time zone (IST) never files 00:10 heat under the previous day."""
    buckets = (await db.execute(text(
        "SELECT DISTINCT to_timestamp(floor(extract(epoch FROM period_start) / 900) * 900)"
        " FROM heatmap_chunks WHERE camera_id = :c"), {"c": camera.id})).scalars().all()
    first, last, blocks, used = (await db.execute(text(
        "SELECT min(period_start), max(period_end), count(*), COALESCE(SUM(size_bytes), 0)"
        " FROM heatmap_chunks WHERE camera_id = :c"), {"c": camera.id})).one()
    days, mb = caps(camera.analytics_config)
    return {
        "camera_id": str(camera.id),
        "days": sorted({b.astimezone().date().isoformat() for b in buckets}),
        "first_at": first.isoformat() if first else None,
        "last_at": last.isoformat() if last else None,
        "blocks": int(blocks), "used_bytes": int(used),
        "retention_days": days, "storage_mb": mb,
    }
