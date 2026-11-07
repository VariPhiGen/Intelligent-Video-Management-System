"""timeline.py — person / vehicle / motion presence for the playback timeline.

WHAT THIS IS. The analytics tracker knows, for every object it follows, when it
came into view and when it left. This module stores that as bounded SEGMENTS
(`timeline_presence`, migration 043) and serves them back to the playback
timeline as merged bars: "someone was in view 10:02:13–10:02:47".

WHAT IT IS NOT. An event. Events (services/ai_events.py) are what an operator's
configured activity DECIDED; presence is where the footage has something in it.
The two are drawn side by side on the timeline and never mixed in storage.

WHO MAY WRITE. Only the analytics service (X-Internal-Key), and only for a kind
the camera is actually indexing: `search_indexing` on and the domain ticked in
AI Config. A producer running ahead of a configuration change (indexing just
switched off) is told so per segment and the rest of its batch still lands —
the same posture as event ingest.

READS MERGE AT PIXEL RESOLUTION. The browser says how many pixels wide its
timeline is; segments of one kind closer together than ~2 px are one bar. That
bounds the response by the screen, not by how busy the camera was — a 7-day
view of a busy gate returns a few hundred bars, not a few hundred thousand
segments. Each bar carries how many segments it absorbed, so the UI can show
density without a second query.

MOTION IS A PRESENCE TOO (migration 044): the motion service reports the runs
of confirmed motion it saw, for cameras with motion detection on — the stretches
of footage in which something moved. Its incident log is not used: an
incident's length is the re-arm cooldown, not the motion.

NEXT / PREVIOUS (`adjacent`) answers "where is the next marker" across all the
footage still kept, not only the window the browser has loaded.

RETENTION AND ERASURE follow the footage. `expires_at` is stamped from the
camera's footage retention at insert and swept on the events' cadence; a subject
erasure or a whole-camera purge deletes the matching rows at once, because a
marker over deleted footage is a claim nobody can check.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence, Union

import structlog
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import String, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Camera, TimelinePresence
from . import retention_units
from .detection_engine import presence_source_ok

log = structlog.get_logger(__name__)

# ── Contract constants ────────────────────────────────────────────────────────

KINDS = ("person", "vehicle", "motion")

#: Presence kind → the AI Config search domain that switches it on. "vehicles"
#: is plural on the search contract (types.ts, analytics detector.py).
DOMAIN_OF = {"person": "person", "vehicle": "vehicles"}

#: The longest one segment may be. Mirrors the table's CHECK, and is what lets
#: an overlap read be an index range on started_at (see presence()).
MAX_SEGMENT = timedelta(minutes=10)

INGEST_MAX_BATCH = 500
FUTURE_SKEW = timedelta(minutes=5)

#: The widest window one read may ask for — the timeline zooms out to 7 days.
MAX_WINDOW = timedelta(days=8)
#: Never merge tighter than this, however far in the browser zooms: two
#: sightings a second apart are one presence, not two.
MIN_MERGE_GAP_S = 3.0
#: Bars per response, all kinds together. Merging at 2 px keeps a real
#: response far below this; hitting it means the caller sent an absurd `px`.
MAX_BARS = 5000

_ID_NAMESPACE = uuid.UUID("b1f5b0a4-3d0e-4f7e-9d4a-2a6e1c9a7f31")

REJECT_REASONS = (
    "unknown_camera",
    "kind_not_indexed",       # the camera is not indexing this kind (AI Config)
    "ended_before_started",
    "too_long",               # longer than MAX_SEGMENT: the producer must cut it
    "in_future",
    "outside_retention",
    "wrong_source",           # not the engine that owns this camera (detection_engine)
)


class PresenceIn(BaseModel):
    """One segment of one tracked object, as the analytics service reports it.

    `sensor_id` is the camera slug, the same name the event ingest uses.
    """

    id: Optional[uuid.UUID] = None
    sensor_id: str = Field(min_length=1, max_length=255)
    kind: Literal["person", "vehicle", "motion"]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    peak_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    track_id: Optional[Union[int, str]] = None
    label: Optional[str] = Field(default=None, max_length=32)
    source: Literal["cpu", "deepstream", "motion"] = "cpu"


class IngestBody(BaseModel):
    segments: list[PresenceIn] = Field(min_length=1, max_length=INGEST_MAX_BATCH)


# ── Ingest ────────────────────────────────────────────────────────────────────

def indexed_kinds(camera: Any) -> set[str]:
    """Presence kinds this camera is configured to produce.

    Person / vehicle: Smart Search indexing on with the domain ticked —
    `search_domains` means nothing while `search_indexing` is off, the same
    reading the Config page and the search proxy give it. Motion: the camera's
    motion detection switch.
    """
    out: set[str] = set()
    if getattr(camera, "motion_detection", False):
        out.add("motion")
    if getattr(camera, "search_indexing", False):
        domains = set(getattr(camera, "search_domains", None) or [])
        out |= {k for k, d in DOMAIN_OF.items() if d in domains}
    return out


def derived_id(slug: str, kind: str, track_id: Optional[str], started_at: datetime) -> uuid.UUID:
    key = "|".join([slug, kind, track_id or "",
                    started_at.astimezone(timezone.utc).isoformat()])
    return uuid.uuid5(_ID_NAMESPACE, key)


def prepare(items: Iterable[PresenceIn], cameras_by_slug: Mapping[str, Any], *,
            now: datetime) -> tuple[list[dict], list[dict]]:
    """Validate a batch. Returns (rows to store, rejections), per segment."""
    rows: list[dict] = []
    rejected: list[dict] = []

    def reject(index: int, reason: str, seg: PresenceIn) -> None:
        rejected.append({"index": index, "reason": reason,
                         "sensor_id": seg.sensor_id, "kind": seg.kind})

    for i, seg in enumerate(items):
        camera = cameras_by_slug.get(seg.sensor_id)
        if camera is None:
            reject(i, "unknown_camera", seg)
            continue
        if seg.kind not in indexed_kinds(camera):
            reject(i, "kind_not_indexed", seg)
            continue
        # One engine per camera: CPU analytics or DeepStream, never both, so a
        # person is never counted twice (services/detection_engine.py).
        if not presence_source_ok(camera, seg.kind, seg.source):
            reject(i, "wrong_source", seg)
            continue
        if seg.ended_at < seg.started_at:
            reject(i, "ended_before_started", seg)
            continue
        if seg.ended_at - seg.started_at > MAX_SEGMENT:
            reject(i, "too_long", seg)
            continue
        if seg.started_at > now + FUTURE_SKEW:
            reject(i, "in_future", seg)
            continue
        # Presence describes footage, so it goes when the footage does — the
        # rule ai_events stamps with, in the same unit.
        retention = retention_units.effective_retention_minutes(
            getattr(camera, "retention_minutes", None))
        expires_at = seg.ended_at + timedelta(minutes=retention)
        if expires_at <= now:
            reject(i, "outside_retention", seg)
            continue
        track = None if seg.track_id is None else str(seg.track_id)[:64]
        rows.append({
            "id": seg.id or derived_id(seg.sensor_id, seg.kind, track, seg.started_at),
            "camera_id": camera.id,
            "kind": seg.kind,
            "started_at": seg.started_at,
            "ended_at": seg.ended_at,
            "peak_confidence": seg.peak_confidence,
            "track_id": track,
            "label": seg.label,
            "source": seg.source,
            "expires_at": expires_at,
        })
    return rows, rejected


async def record(db: AsyncSession, rows: Sequence[dict]) -> dict:
    """Store prepared rows. Idempotent: a segment is immutable once stored."""
    if not rows:
        return {"inserted": 0, "duplicates": 0}
    table = TimelinePresence.__table__
    stmt = (pg_insert(table).values(list(rows))
            .on_conflict_do_nothing(index_elements=[table.c.id])
            .returning(table.c.id))
    inserted = len((await db.execute(stmt)).all())
    await db.commit()
    return {"inserted": inserted, "duplicates": len(rows) - inserted}


# ── Reading ───────────────────────────────────────────────────────────────────

def merge_gap_seconds(frm: datetime, to: datetime, px: int) -> float:
    """Two segments this close are one bar at the caller's resolution (~2 px)."""
    per_px = (to - frm).total_seconds() / max(1, px)
    return max(MIN_MERGE_GAP_S, 2.0 * per_px)


# Gaps and islands, per kind: a segment starts a new bar when it begins more
# than :gap seconds after every earlier segment of its kind has ended. The
# started_at lower bound is what MAX_SEGMENT buys — nothing that began before
# (from - MAX_SEGMENT) can overlap the window, so the scan is an index range.
_PRESENCE_SQL = text("""
WITH r AS (
    SELECT kind, started_at AS s, ended_at AS e
      FROM timeline_presence
     WHERE camera_id = :camera_id
       AND kind = ANY(:kinds)
       AND started_at >= :lower
       AND started_at < :to
       AND ended_at > :frm
       AND expires_at > now()
), o AS (
    SELECT kind, s, e,
           CASE WHEN s > max(e) OVER (PARTITION BY kind ORDER BY s, e
                                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                         + make_interval(secs => :gap)
                THEN 1 ELSE 0 END AS brk
      FROM r
), g AS (
    SELECT kind, s, e,
           sum(brk) OVER (PARTITION BY kind ORDER BY s, e ROWS UNBOUNDED PRECEDING) AS grp
      FROM o
)
SELECT kind, min(s) AS s, max(e) AS e, count(*) AS n
  FROM g
 GROUP BY kind, grp
 ORDER BY kind, min(s)
 LIMIT :cap
""").bindparams(bindparam("kinds", type_=ARRAY(String)))


async def presence(db: AsyncSession, camera_id: uuid.UUID, frm: datetime, to: datetime,
                   kinds: Sequence[str], px: int) -> dict:
    """Merged presence bars for one camera and window.

    Epoch seconds, `[start, end, segments]` per bar — compact, because a busy
    week is still a few thousand of them.
    """
    gap = merge_gap_seconds(frm, to, px)
    rows = (await db.execute(_PRESENCE_SQL, {
        "camera_id": camera_id, "kinds": list(kinds),
        "lower": frm - MAX_SEGMENT, "frm": frm, "to": to,
        "gap": gap, "cap": MAX_BARS + 1,
    })).mappings().all()
    truncated = len(rows) > MAX_BARS
    out: dict[str, list] = {k: [] for k in kinds}
    for r in rows[:MAX_BARS]:
        out[r["kind"]].append([round(r["s"].timestamp(), 1), round(r["e"].timestamp(), 1),
                               int(r["n"])])
    return {"merge_gap_s": round(gap, 1), "kinds": out, "truncated": truncated}


# ── Next / previous ───────────────────────────────────────────────────────────

#: Where a jump lands, before the marker — the SPA's own pre-rolls, so a
#: server answer and a locally found one put the playhead in the same place.
PRESENCE_PRE_ROLL = timedelta(seconds=3)
#: Presence closer than this to an earlier segment of its kind is the same bar
#: continuing (a long stay is cut into segments), not a new appearance.
_ISLAND_GAP = timedelta(seconds=3)

# The first segment whose SEEK time is past :at and which starts an island —
# no earlier segment of its kind reaches to within the island gap of it. The
# NOT EXISTS is bounded to MAX_SEGMENT back, so both halves are index ranges.
_NEXT_PRESENCE = text("""
SELECT p.kind, p.started_at, p.ended_at
  FROM timeline_presence p
 WHERE p.camera_id = :cid AND p.kind = ANY(:kinds) AND p.expires_at > now()
   AND p.started_at > :bound
   AND NOT EXISTS (
       SELECT 1 FROM timeline_presence q
        WHERE q.camera_id = p.camera_id AND q.kind = p.kind
          AND q.started_at < p.started_at
          AND q.started_at >= p.started_at - CAST(:max_seg AS interval)
          AND q.ended_at >= p.started_at - CAST(:island AS interval))
 ORDER BY p.started_at ASC
 LIMIT 1
""").bindparams(bindparam("kinds", type_=ARRAY(String)))

_PREV_PRESENCE = text("""
SELECT p.kind, p.started_at, p.ended_at
  FROM timeline_presence p
 WHERE p.camera_id = :cid AND p.kind = ANY(:kinds) AND p.expires_at > now()
   AND p.started_at < :bound
   AND NOT EXISTS (
       SELECT 1 FROM timeline_presence q
        WHERE q.camera_id = p.camera_id AND q.kind = p.kind
          AND q.started_at < p.started_at
          AND q.started_at >= p.started_at - CAST(:max_seg AS interval)
          AND q.ended_at >= p.started_at - CAST(:island AS interval))
 ORDER BY p.started_at DESC
 LIMIT 1
""").bindparams(bindparam("kinds", type_=ARRAY(String)))

_NEXT_EVENT = text("""
SELECT activity, started_at, ended_at FROM analytics_events
 WHERE camera_id = :cid AND expires_at > now() AND started_at > :bound
   AND NOT (activity = ANY(:hidden))
 ORDER BY started_at ASC, id ASC LIMIT 1
""").bindparams(bindparam("hidden", type_=ARRAY(String)))

_PREV_EVENT = text("""
SELECT activity, started_at, ended_at FROM analytics_events
 WHERE camera_id = :cid AND expires_at > now() AND started_at < :bound
   AND NOT (activity = ANY(:hidden))
 ORDER BY started_at DESC, id DESC LIMIT 1
""").bindparams(bindparam("hidden", type_=ARRAY(String)))


async def adjacent(db: AsyncSession, camera_id: uuid.UUID, at: datetime, direction: str,
                   kinds: Sequence[str], events: bool,
                   hidden_activities: Sequence[str] = ()) -> Optional[dict]:
    """The nearest marker whose seek point is strictly after (or before) `at`.

    Compared on the SEEK point, pre-roll included, exactly as the SPA compares
    the marks it has loaded — so pressing "next" from where the last jump
    landed moves on instead of finding the same marker again. Backwards keeps
    a 3 s margin, like a media player's "previous" once a track has started.
    """
    from .ai_events import PLAYBACK_PRE_ROLL_S

    fwd = direction == "next"
    margin = timedelta(seconds=1) if fwd else timedelta(seconds=-3)
    found: list[dict] = []
    if kinds:
        sql = _NEXT_PRESENCE if fwd else _PREV_PRESENCE
        r = (await db.execute(sql, {
            "cid": camera_id, "kinds": list(kinds),
            "bound": at + margin + PRESENCE_PRE_ROLL,
            "max_seg": MAX_SEGMENT, "island": _ISLAND_GAP,
        })).mappings().first()
        if r is not None:
            found.append({"row": r["kind"], "activity": None, "start": r["started_at"],
                          "end": r["ended_at"], "seek": r["started_at"] - PRESENCE_PRE_ROLL})
    if events:
        pre = timedelta(seconds=PLAYBACK_PRE_ROLL_S)
        sql = _NEXT_EVENT if fwd else _PREV_EVENT
        r = (await db.execute(sql, {
            "cid": camera_id, "bound": at + margin + pre, "hidden": list(hidden_activities),
        })).mappings().first()
        if r is not None:
            found.append({"row": "event", "activity": r["activity"], "start": r["started_at"],
                          "end": r["ended_at"], "seek": r["started_at"] - pre})
    if not found:
        return None
    best = min(found, key=lambda f: f["seek"]) if fwd else max(found, key=lambda f: f["seek"])
    return {"row": best["row"], "activity": best["activity"],
            "start": best["start"].timestamp(),
            "end": best["end"].timestamp() if best["end"] is not None else None,
            "seek_at": best["seek"].timestamp()}


# ── Retention and erasure ─────────────────────────────────────────────────────

_PURGE_CHUNK = 5000


async def purge_expired(db: AsyncSession) -> int:
    total = 0
    while True:
        res = await db.execute(text(
            "DELETE FROM timeline_presence WHERE id IN ("
            " SELECT id FROM timeline_presence WHERE expires_at < now() LIMIT :n)"
        ), {"n": _PURGE_CHUNK})
        await db.commit()
        total += res.rowcount or 0
        if (res.rowcount or 0) < _PURGE_CHUNK:
            return total


async def retention_loop() -> None:
    from ..db import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await purge_expired(db)
            if n:
                log.info("timeline.retention.purged", rows=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed sweep retries next period
            log.warning("timeline.retention.failed", error=str(exc))
        await asyncio.sleep(settings.analytics_events_sweep_seconds)


async def erase(db: AsyncSession, camera_id: uuid.UUID,
                frm: Optional[datetime] = None, to: Optional[datetime] = None) -> int:
    """Delete presence overlapping [frm, to) — or all of it, for a whole-camera purge."""
    sql = "DELETE FROM timeline_presence WHERE camera_id = :cid"
    params: dict[str, Any] = {"cid": camera_id}
    if frm is not None:
        sql += " AND ended_at > :frm"
        params["frm"] = frm
    if to is not None:
        sql += " AND started_at < :to"
        params["to"] = to
    res = await db.execute(text(sql), params)
    await db.commit()
    return res.rowcount or 0


async def erase_by_slug(slug: str, frm: Optional[datetime] = None,
                        to: Optional[datetime] = None) -> Optional[int]:
    """`erase` for callers holding a slug and no session (subject erasure, the purge proxy).

    Returns rows deleted, 0 for an unknown camera (nothing of it is stored),
    and None on failure — an erasure must never read as done when it was not.
    """
    from sqlalchemy import select

    from ..db import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            cid = (await db.execute(select(Camera.id).where(Camera.slug == slug))).scalar()
            if cid is None:
                return 0
            return await erase(db, cid, frm, to)
    except Exception as exc:  # noqa: BLE001 — reported to the caller as None
        log.error("timeline.erase.failed", camera=slug, error=str(exc))
        return None
