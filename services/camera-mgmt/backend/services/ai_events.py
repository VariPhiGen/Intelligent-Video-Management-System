"""ai_events.py — the canonical AI activity event: ingest, storage and reads.

WHERE EVENTS COME FROM. Activities run on the CPU Activity Engine in the
analytics service (`services/analytics/analytics/activities/`), configured per
camera from `Camera.analytics_config` as AI Config stores it and pushed there
by services/analytics_client.py. When an activity's own logic decides something
happened, the engine posts it to `POST /api/analytics/events` with
`source: "cpu"`. (The DeepStream pipeline could post here too if its projection
is switched back on; nothing here depends on which producer it is.)

Nothing here detects anything: this module is where decisions made elsewhere
land, and it refuses an event that does not trace back to an activity the
operator configured on that camera. That rule is what keeps a producer that
forwards raw detections from turning the Events page into a detection log.

STORAGE is `analytics_events` (migration 022), used the way that migration
committed to:

  * ids are minted by the PRODUCER, so a retried POST collapses. A producer
    that sends none gets a deterministic id from (camera, activity, zone,
    track, start) — a retry of the same event still lands on the same row.
  * `expires_at` is stamped at insert from the camera's footage retention. An
    event whose Open Playback link points at deleted footage is a broken
    promise, so the row goes when the footage does.
  * interval events may be posted OPEN (ended_at null) and posted again under
    the same id once they close. Closing an open event is the only update ever
    applied; every other field of a stored event is immutable.

READS are newest first by when the event HAPPENED (`started_at`), never by when
it arrived — a late delivery slots into its real place instead of jumping the
queue. Ties break on id, so a page boundary is stable.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence, Union

import structlog
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import and_, func, literal_column, select, text, true, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import AnalyticsActivityType, AnalyticsEvent, Camera, CameraStage
from .deepstream import _LINE_CROSSING_ACTIVITY, _lane_zone

log = structlog.get_logger(__name__)

# ── Contract constants ────────────────────────────────────────────────────────

#: The live ticker at the top of the Events tab. Enforced here, not only in the
#: browser, so no client can turn the "latest" feed into a bulk export.
TICKER_LIMIT = 10

#: Playback opens this far BEFORE the event starts, so the operator sees what
#: led up to it rather than landing mid-action, and the window runs this far
#: past its end (or its start, for a point event).
PLAYBACK_PRE_ROLL_S = 10
PLAYBACK_POST_ROLL_S = 20

#: Largest batch one POST may carry.
INGEST_MAX_BATCH = 500

#: How far ahead of this server's clock an event may claim to have started.
#: Beyond it the producer's clock is wrong, and storing the event would pin it
#: to the top of every newest-first list until real time caught up.
FUTURE_SKEW = timedelta(minutes=5)

#: Detector attributes are schemaless by design (migration 022), not unbounded.
ATTRIBUTES_MAX_BYTES = 8192

_ID_NAMESPACE = uuid.UUID("6f1c3c2e-6f0a-4c44-9a55-0e9a2c3b7d10")
_FALLBACK_COLOR = "#8a94a6"

REJECT_REASONS = (
    "unknown_camera",           # sensor_id is not a registered camera slug
    "activity_not_configured",  # the camera is not configured for this activity
    "ended_before_started",
    "in_future",
    "outside_retention",        # already older than the camera keeps footage
    "attributes_too_large",
)


# ── Ingest contract ───────────────────────────────────────────────────────────

class EventIn(BaseModel):
    """One activity event as the pipeline reports it.

    Identity uses the pipeline's own vocabulary so the producer needs no
    mapping: `sensor_id` is the VMS camera slug (the projected `<slug>.json`
    carries it as `sensor_id`), `activity` is the key under `activities_data`,
    and `zone` may be the zone key the pipeline was given (`zone1`, …) — it is
    translated back to the region's name here.
    """

    id: Optional[uuid.UUID] = None
    sensor_id: str = Field(min_length=1, max_length=255)
    activity: str = Field(min_length=1, max_length=64)
    zone: Optional[str] = Field(default=None, max_length=120)
    started_at: AwareDatetime
    ended_at: Optional[AwareDatetime] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    track_id: Optional[Union[int, str]] = None
    object_class: Optional[str] = Field(default=None, max_length=32)
    attributes: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = Field(default=None, max_length=128)
    source: Literal["cpu", "deepstream", "motion", "onvif", "manual"] = "deepstream"


class IngestBody(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=INGEST_MAX_BATCH)


# ── What a camera is configured to detect ─────────────────────────────────────

def _config(camera: Any) -> dict:
    cfg = getattr(camera, "analytics_config", None) or {}
    return cfg if isinstance(cfg, dict) else {}


def configured_activities(camera: Any) -> list[str]:
    """Activity keys configured on a camera, in the order the operator added them.

    Read from storage directly, so malformed entries (no `type`) are skipped
    rather than trusted — the same posture deepstream_config takes.
    """
    out: list[str] = []
    for act in _config(camera).get("activities") or []:
        key = act.get("type") if isinstance(act, dict) else None
        if key and key not in out:
            out.append(key)
    return out


def zone_name(camera: Any, activity: str, zone: Optional[str]) -> Optional[str]:
    """The operator's name for the region an event refers to.

    Accepts what a producer might reasonably send: the pipeline's positional
    key (`zone2`), a region id (`region_1`) or a region name (`Zone 1`). The
    positional key is numbered EXACTLY as services/deepstream.py numbers it —
    per activity, skipping regions that no longer exist and, for the
    line-crossing activity, anything that is not a usable tripwire — because
    that numbering is what the pipeline was handed. A test holds the two in
    step.

    A value that resolves to nothing is kept verbatim rather than dropped: the
    event is real even when the zones have been edited since the pipeline
    loaded them.
    """
    if not zone:
        return None
    regions: dict = _config(camera).get("regions") or {}
    if zone in regions:
        return regions[zone].get("name") or zone
    for region in regions.values():
        if region.get("name") == zone:
            return zone

    act = next((a for a in _config(camera).get("activities") or []
                if isinstance(a, dict) and a.get("type") == activity), None)
    if act is not None and zone.startswith("zone") and zone[4:].isdigit():
        wanted, n = int(zone[4:]), 0
        for rid in act.get("regions") or []:
            region = regions.get(rid)
            if region is None:
                continue
            if activity == _LINE_CROSSING_ACTIVITY:
                if region.get("kind") != "tripwire" or \
                        _lane_zone(region.get("points", []), region.get("direction")) is None:
                    continue
            n += 1
            if n == wanted:
                return region.get("name") or rid
    return zone


def derived_id(slug: str, activity: str, zone: Optional[str], track_id: Optional[str],
               started_at: datetime) -> uuid.UUID:
    """A stable id for a producer that did not mint one."""
    key = "|".join([slug, activity, zone or "", track_id or "",
                    started_at.astimezone(timezone.utc).isoformat()])
    return uuid.uuid5(_ID_NAMESPACE, key)


def prepare(
    items: Iterable[EventIn],
    cameras_by_slug: Mapping[str, Any],
    *,
    now: datetime,
    default_retention_days: int,
) -> tuple[list[dict], list[dict]]:
    """Validate a batch against the registry. Returns (rows to store, rejections).

    Per event rather than per batch: one camera removed mid-flight must not cost
    the other cameras' events in the same POST.
    """
    rows: list[dict] = []
    rejected: list[dict] = []

    def reject(index: int, reason: str, ev: EventIn) -> None:
        rejected.append({"index": index, "reason": reason,
                         "sensor_id": ev.sensor_id, "activity": ev.activity})

    for i, ev in enumerate(items):
        camera = cameras_by_slug.get(ev.sensor_id)
        if camera is None:
            reject(i, "unknown_camera", ev)
            continue
        if ev.activity not in configured_activities(camera):
            reject(i, "activity_not_configured", ev)
            continue
        if ev.ended_at is not None and ev.ended_at < ev.started_at:
            reject(i, "ended_before_started", ev)
            continue
        if ev.started_at > now + FUTURE_SKEW:
            reject(i, "in_future", ev)
            continue
        retention = getattr(camera, "retention_days", None) or default_retention_days
        expires_at = ev.started_at + timedelta(days=retention)
        if expires_at <= now:
            reject(i, "outside_retention", ev)
            continue
        if len(json.dumps(ev.attributes, default=str)) > ATTRIBUTES_MAX_BYTES:
            reject(i, "attributes_too_large", ev)
            continue

        track = None if ev.track_id is None else str(ev.track_id)[:64]
        zone = zone_name(camera, ev.activity, ev.zone)
        rows.append({
            "id": ev.id or derived_id(ev.sensor_id, ev.activity, ev.zone, track, ev.started_at),
            "camera_id": camera.id,
            "activity": ev.activity,
            "zone": zone[:120] if zone else None,
            "started_at": ev.started_at,
            "ended_at": ev.ended_at,
            "confidence": ev.confidence,
            "track_id": track,
            "object_class": ev.object_class,
            "attributes": ev.attributes,
            "dedupe_key": ev.dedupe_key,
            "source": ev.source,
            "expires_at": expires_at,
        })
    return rows, rejected


async def record(db: AsyncSession, rows: Sequence[dict]) -> dict[str, int]:
    """Store prepared rows. Idempotent; closing an open event is the one update.

    `xmax = 0` on the RETURNING row is PostgreSQL's tell for "this was an
    insert", which separates a new event from a close without a second read.
    No row back means the conflict matched and the update's WHERE declined it:
    a plain duplicate.
    """
    counts = {"inserted": 0, "closed": 0, "duplicates": 0}
    table = AnalyticsEvent.__table__
    for row in rows:
        stmt = pg_insert(table).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.id],
            set_={"ended_at": stmt.excluded.ended_at},
            where=and_(
                table.c.ended_at.is_(None),
                stmt.excluded.ended_at.isnot(None),
                stmt.excluded.ended_at >= table.c.started_at,
                # An id collision from a DIFFERENT event must not close this one.
                table.c.camera_id == stmt.excluded.camera_id,
                table.c.activity == stmt.excluded.activity,
            ),
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        got = (await db.execute(stmt)).first()
        if got is None:
            counts["duplicates"] += 1
        elif got.inserted:
            counts["inserted"] += 1
        else:
            counts["closed"] += 1
    await db.commit()
    return counts


# ── Reading ───────────────────────────────────────────────────────────────────

def humanize(key: str) -> str:
    words = key.replace("_", " ").replace("-", " ").split()
    text_ = " ".join(words)
    return text_[:1].upper() + text_[1:] if text_ else key


def activity_meta(key: str, catalog: Mapping[str, Any]) -> dict:
    """Label and colour for an activity key.

    The catalog is the source of the operator-facing name. A key it no longer
    holds (a type withdrawn after events were stored, or an un-catalogued type)
    still gets a readable label instead of disappearing from history.
    """
    entry = catalog.get(key)
    if entry is not None:
        return {"key": key, "label": entry["label"], "color": entry["color"]}
    return {"key": key, "label": humanize(key), "color": _FALLBACK_COLOR}


def playback_window(slug: str, started_at: datetime, ended_at: Optional[datetime]) -> dict:
    """Where Open Playback lands. Epoch seconds; `start` is what the link seeks to."""
    start = started_at - timedelta(seconds=PLAYBACK_PRE_ROLL_S)
    end = (ended_at or started_at) + timedelta(seconds=PLAYBACK_POST_ROLL_S)
    return {"camera": slug, "start": math.floor(start.timestamp()),
            "end": math.ceil(end.timestamp())}


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def shape(row: Mapping[str, Any], *, slug: str, name: Optional[str],
          catalog: Mapping[str, Any]) -> dict:
    """The one wire shape every read returns."""
    started, ended = row["started_at"], row["ended_at"]
    return {
        "id": str(row["id"]),
        "camera": {"id": str(row["camera_id"]), "slug": slug, "name": name or slug},
        "activity": activity_meta(row["activity"], catalog),
        "zone": row["zone"],
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "duration_s": round((ended - started).total_seconds(), 1) if ended else None,
        "confidence": row["confidence"],
        "track_id": row["track_id"],
        "object_class": row["object_class"],
        "attributes": row["attributes"] or {},
        "source": row["source"],
        "playback": playback_window(slug, started, ended),
    }


@dataclass(frozen=True)
class Cursor:
    """Keyset position: the last event of a page, as (started_at, id)."""

    started_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.started_at.astimezone(timezone.utc).isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> "Cursor":
        """Raises ValueError on anything that is not a cursor this module made."""
        try:
            padded = token + "=" * (-len(token) % 4)
            ts, ident = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
            started = datetime.fromisoformat(ts)
            if started.tzinfo is None:
                raise ValueError("naive")
            return cls(started, uuid.UUID(ident))
        except Exception as exc:  # noqa: BLE001 — every malformed form is one error
            raise ValueError("invalid cursor") from exc


@dataclass(frozen=True)
class EventFilter:
    camera_ids: Optional[Sequence[uuid.UUID]] = None
    activity: Optional[str] = None
    frm: Optional[datetime] = None      # inclusive, on started_at
    to: Optional[datetime] = None       # inclusive, on started_at
    before: Optional[Cursor] = None
    limit: int = 50


_EVENT_COLS = AnalyticsEvent.__table__.c


def events_query(f: EventFilter):
    """Newest-first events for the given filter, with the owning camera's names.

    Only registered cameras, and only rows still inside their retention — a row
    past `expires_at` is waiting for the sweep, and its footage is already gone.
    """
    q = (
        select(*_EVENT_COLS, Camera.slug.label("camera_slug"), Camera.name.label("camera_name"))
        .join(Camera, Camera.id == AnalyticsEvent.camera_id)
        .where(Camera.stage == CameraStage.REGISTERED.value,
               AnalyticsEvent.expires_at > func.now())
    )
    if f.camera_ids is not None:
        q = q.where(AnalyticsEvent.camera_id.in_(list(f.camera_ids)))
    if f.activity:
        q = q.where(AnalyticsEvent.activity == f.activity)
    if f.frm is not None:
        q = q.where(AnalyticsEvent.started_at >= f.frm)
    if f.to is not None:
        q = q.where(AnalyticsEvent.started_at <= f.to)
    if f.before is not None:
        q = q.where(tuple_(AnalyticsEvent.started_at, AnalyticsEvent.id)
                    < tuple_(f.before.started_at, f.before.id))
    return q.order_by(AnalyticsEvent.started_at.desc(), AnalyticsEvent.id.desc()).limit(f.limit)


async def load_catalog(db: AsyncSession) -> dict[str, dict]:
    result = await db.execute(select(AnalyticsActivityType))
    return {t.key: {"label": t.label, "color": t.color} for t in result.scalars().all()}


async def list_events(db: AsyncSession, f: EventFilter,
                      catalog: Mapping[str, Any]) -> tuple[list[dict], Optional[str]]:
    """One page, plus the cursor for the next one (None on the last page)."""
    probe = EventFilter(**{**f.__dict__, "limit": f.limit + 1})
    rows = (await db.execute(events_query(probe))).mappings().all()
    page = rows[: f.limit]
    events = [shape(r, slug=r["camera_slug"], name=r["camera_name"], catalog=catalog)
              for r in page]
    nxt = Cursor(page[-1]["started_at"], page[-1]["id"]).encode() \
        if len(rows) > f.limit and page else None
    return events, nxt


async def latest_per_camera(db: AsyncSession, camera_ids: Sequence[uuid.UUID],
                            per_camera: int) -> dict[uuid.UUID, list[Mapping[str, Any]]]:
    """The newest `per_camera` events of each camera, in one round trip.

    A LATERAL join rather than a window function: each camera's slice is an
    index walk on ix_events_camera_time that stops after `per_camera` rows,
    where ROW_NUMBER() would number every stored event of every camera first.
    """
    if not camera_ids:
        return {}
    cams = select(Camera.id.label("cid")).where(Camera.id.in_(list(camera_ids))).subquery("c")
    ev = (
        select(*_EVENT_COLS)
        .where(AnalyticsEvent.camera_id == cams.c.cid,
               AnalyticsEvent.expires_at > func.now())
        .order_by(AnalyticsEvent.started_at.desc(), AnalyticsEvent.id.desc())
        .limit(per_camera)
        .lateral("e")
    )
    q = select(ev).select_from(cams.join(ev, true()))
    out: dict[uuid.UUID, list[Mapping[str, Any]]] = {cid: [] for cid in camera_ids}
    for row in (await db.execute(q)).mappings().all():
        out.setdefault(row["camera_id"], []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: (r["started_at"], r["id"]), reverse=True)
    return out


def camera_cards(cameras: Sequence[Any], per_camera_rows: Mapping[uuid.UUID, Sequence[Mapping]],
                 catalog: Mapping[str, Any]) -> list[dict]:
    """One card per camera with at least one configured activity.

    A camera with activities and no events still gets its card — "configured
    and quiet" is a different fact from "not configured", and hiding the card
    would make the two look the same. Ordered by name, so cards stay put while
    the page refreshes instead of reshuffling on every new event.
    """
    cards = []
    for cam in cameras:
        keys = configured_activities(cam)
        if not keys:
            continue
        rows = per_camera_rows.get(cam.id, [])
        events = [shape(r, slug=cam.slug, name=cam.name, catalog=catalog) for r in rows]
        cards.append({
            "camera": {"id": str(cam.id), "slug": cam.slug, "name": cam.name or cam.slug,
                       "enabled": bool(cam.enabled)},
            "activities": [activity_meta(k, catalog) for k in keys],
            "events": events,
            "last_event_at": events[0]["started_at"] if events else None,
        })
    cards.sort(key=lambda c: ((c["camera"]["name"] or "").lower(), c["camera"]["slug"] or ""))
    return cards


async def overview(db: AsyncSession, *, per_camera: int) -> dict:
    """Everything the Events tab's main view draws, in one response."""
    cameras = (await db.execute(
        select(Camera).where(Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().all()
    catalog = await load_catalog(db)
    latest, _ = await list_events(db, EventFilter(limit=TICKER_LIMIT), catalog)
    configured = [c for c in cameras if configured_activities(c)]
    per = await latest_per_camera(db, [c.id for c in configured], per_camera)
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "ticker_limit": TICKER_LIMIT,
        "latest": latest[:TICKER_LIMIT],
        "cameras": camera_cards(configured, per, catalog),
    }


# ── Retention ─────────────────────────────────────────────────────────────────

_PURGE_CHUNK = 5000


async def purge_expired(db: AsyncSession) -> int:
    """Delete events past their own deadline, in chunks so no lock is held long."""
    total = 0
    while True:
        res = await db.execute(text(
            "DELETE FROM analytics_events WHERE id IN ("
            " SELECT id FROM analytics_events WHERE expires_at < now() LIMIT :n)"
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
                log.info("ai_events.retention.purged", rows=n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed sweep retries next period
            log.warning("ai_events.retention.failed", error=str(exc))
        await asyncio.sleep(settings.analytics_events_sweep_seconds)
