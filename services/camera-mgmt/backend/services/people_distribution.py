"""people_distribution.py — zone occupancy: ingest, the live reading, the day.

WHAT THIS IS FOR. "How many people are in each zone right now", and "how did
that change through the day". Both come from periodic samples the pipeline
posts; neither can be answered by the activity's existing exit records, which by
definition only exist once a person has already left.

WHAT IT DELIBERATELY IS NOT. Not events, not clips, not an object store. A
sample is a handful of integers, so it lives in Postgres and is read back as a
series. Nothing here writes to MinIO and nothing here creates an AnalyticsEvent
-- see migration 048 for why levels and occurrences are kept apart.

ONE ENGINE, as everywhere else: the ingest admits only the engine that owns the
camera (detection_engine.py), so CPU analytics and the pipeline cannot both
report occupancy for one camera and double it.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Mapping, Optional

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (Camera, CameraStage, PeopleDistributionHour,
                      PeopleDistributionSample)
from . import detection_engine, retention_units

log = structlog.get_logger(__name__)

ACTIVITY = "people_distribution"

#: A sample older than this is not interesting: occupancy is read live or by
#: day, and nobody asks what a zone held six weeks ago. Overridden per camera by
#: the same footage retention the heatmap and events use, so a camera keeping
#: 90 days of video keeps 90 days of counts.
DEFAULT_RETENTION_DAYS = 30

#: Anything older than this is "not live". Two sampling intervals plus slack:
#: one missed post must not blank the panel, but a dead pipeline must not go on
#: claiming people are present.
LIVE_STALE_SECONDS = 90


class Rejected(Exception):
    """The sample cannot be stored, and repeating it will not help."""


class WrongEngine(Rejected):
    """The producer is not the engine this appliance runs. Answers 409."""


def _aware(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(value), timezone.utc)


def clean_zones(raw: Any) -> list[dict]:
    """Normalise the zones a producer sent.

    Defensive because this is a door: a zone with no name, a count that is not a
    number, or a payload that is not a list at all must not reach the database
    and must not raise 500 either.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("zone_name") or item.get("zone_id") or "").strip()
        if not name:
            continue
        try:
            count = max(0, int(item.get("current_count") or 0))
        except (TypeError, ValueError):
            continue
        def _num(key):
            try:
                return round(float(item.get(key) or 0.0), 1)
            except (TypeError, ValueError):
                return 0.0
        raw_ids = item.get("tracker_ids")
        ids = sorted({str(t) for t in raw_ids if str(t).strip()}) \
            if isinstance(raw_ids, (list, tuple)) else []
        out.append({
            "zone_id": str(item.get("zone_id") or name),
            "zone_name": name,
            "current_count": count,
            # WHO is present, kept out of the stored sample (see store) but used
            # to union this hour's distinct people.
            "tracker_ids": ids,
            "dwell_seconds_max": _num("dwell_seconds_max"),
            "dwell_seconds_avg": _num("dwell_seconds_avg"),
        })
    out.sort(key=lambda z: z["zone_name"])
    return out


def hour_start(moment: datetime) -> datetime:
    """The clock hour `moment` falls in, floored on the LOCAL clock.

    FIXED BUCKETS, NOT ROLLING WINDOWS. An activity switched on at 06:30 feeds
    06:00-07:00; it does not create a 06:30-07:30 bucket. Local because the
    operator reads their own clock, and because `active_hours` are evaluated on
    it too -- the stored value is UTC, only the flooring is local.
    """
    local = moment.astimezone()
    return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def _merge_hour(db: AsyncSession, camera, captured_at: datetime,
                      zones: list[dict], expires_at) -> int:
    """Union this sample's tracker ids into its clock hour. Returns zones touched.

    A UNION, NOT AN INCREMENT. The pipeline reports every 10 s, so a person who
    stays 40 minutes is in ~240 samples; adding counts would claim 240 people.
    Unioning ids means they count once -- and it makes a duplicate or retried
    sample a no-op, because a set absorbs what it already holds.

    The union happens in SQL rather than read-modify-write in Python so two
    samples for the same hour cannot interleave and lose one of their ids.
    """
    bucket = hour_start(captured_at)
    table = PeopleDistributionHour.__table__
    touched = 0
    for zone in zones:
        ids = zone.get("tracker_ids") or []
        if not ids:
            # A zone that is empty right now still had whoever was in it
            # earlier this hour; writing an empty set must not clear them.
            continue
        stmt = pg_insert(table).values(
            camera_id=camera.id, hour_start=bucket, zone_name=zone["zone_name"],
            tracker_ids=ids, expires_at=expires_at)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=[table.c.camera_id, table.c.hour_start,
                            table.c.zone_name],
            set_={
                "tracker_ids": text(
                    "(SELECT COALESCE(jsonb_agg(DISTINCT e), '[]'::jsonb) "
                    " FROM jsonb_array_elements("
                    "   people_distribution_hours.tracker_ids"
                    "   || excluded.tracker_ids) AS e)"),
                "updated_at": func.now(),
                "expires_at": stmt.excluded.expires_at,
            },
        ))
        touched += 1
    return touched


async def store(db: AsyncSession, meta: Mapping[str, Any]) -> dict:
    """Record one occupancy sample.

    Idempotent on (camera, captured_at): a retried post updates the row rather
    than adding a second reading for the same instant.
    """
    slug = str(meta.get("camera") or "").strip()
    if not slug:
        raise Rejected("camera is required")
    source = str(meta.get("source") or "cpu").strip().lower()

    camera = (await db.execute(
        select(Camera).where(Camera.slug == slug,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()
    if camera is None:
        raise Rejected(f"unknown camera {slug!r}")

    # One engine per camera, checked before anything is written -- the same
    # rule and the same reason as the heatmap and presence ingests.
    if source != detection_engine.engine():
        raise WrongEngine(
            f"{source!r} may not report occupancy: the engine is "
            f"{detection_engine.engine()!r}")

    captured_at = _aware(meta.get("captured_at")) or datetime.now(timezone.utc)
    zones = clean_zones(meta.get("zones"))
    # Trust the zones over the producer's own total: they are what the page
    # displays, and a total that disagrees with its parts is worse than either.
    total = sum(z["current_count"] for z in zones)

    minutes = retention_units.effective_retention_minutes(
        getattr(camera, "retention_minutes", None))
    expires_at = captured_at + (timedelta(minutes=minutes) if minutes
                                else timedelta(days=DEFAULT_RETENTION_DAYS))

    # The live row keeps the COUNTS; the identities go to the hourly sets and
    # are not kept per sample. A live panel never needs to know who, and storing
    # ids 6 times a minute per camera for ever would be a lot of nobody's
    # business.
    stored_zones = [{k: v for k, v in z.items() if k != "tracker_ids"}
                    for z in zones]
    table = PeopleDistributionSample.__table__
    stmt = pg_insert(table).values(
        camera_id=camera.id, captured_at=captured_at, total_count=total,
        zones=stored_zones, source=source, expires_at=expires_at)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.camera_id, table.c.captured_at],
        set_={"total_count": stmt.excluded.total_count,
              "zones": stmt.excluded.zones,
              "source": stmt.excluded.source,
              "expires_at": stmt.excluded.expires_at},
    ))
    hours = await _merge_hour(db, camera, captured_at, zones, expires_at)
    await db.commit()
    return {"stored": True, "camera": slug, "zones": len(zones),
            "total_count": total, "hour_zones_merged": hours,
            "hour_start": hour_start(captured_at).isoformat()}


def sample_out(row, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    age = (now - row.captured_at).total_seconds()
    return {
        "captured_at": row.captured_at.isoformat(),
        "age_seconds": round(age, 1),
        "total_count": row.total_count,
        "zones": list(row.zones or []),
        "source": row.source,
        # Said by the API rather than inferred in the browser, so every client
        # draws the same line between "now" and "last known".
        "live": age <= LIVE_STALE_SECONDS,
    }


async def latest_for(db: AsyncSession, camera_ids: list[uuid.UUID]) -> dict:
    """Each camera's most recent sample, keyed by camera id."""
    if not camera_ids:
        return {}
    t = PeopleDistributionSample.__table__
    newest = (
        select(t.c.camera_id, func.max(t.c.captured_at).label("captured_at"))
        .where(t.c.camera_id.in_(camera_ids))
        .group_by(t.c.camera_id)
        .subquery()
    )
    rows = (await db.execute(
        select(PeopleDistributionSample).join(
            newest,
            (PeopleDistributionSample.camera_id == newest.c.camera_id)
            & (PeopleDistributionSample.captured_at == newest.c.captured_at))
    )).scalars().all()
    return {r.camera_id: r for r in rows}


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """A calendar day on the APPLIANCE's clock, as UTC.

    The same rule the heatmap history uses: an operator asking for "26 Sep"
    means their day, not a UTC one.
    """
    start = datetime.combine(day, dtime.min).astimezone()
    end = datetime.combine(day, dtime.max).astimezone()
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def series_for(db: AsyncSession, camera_id: uuid.UUID, day: date,
                     limit: int = 2000) -> list[dict]:
    """One camera's samples for one local day, oldest first."""
    lo, hi = local_day_bounds(day)
    rows = (await db.execute(
        select(PeopleDistributionSample)
        .where(PeopleDistributionSample.camera_id == camera_id,
               PeopleDistributionSample.captured_at >= lo,
               PeopleDistributionSample.captured_at <= hi)
        .order_by(PeopleDistributionSample.captured_at)
        .limit(limit)
    )).scalars().all()
    return [sample_out(r) for r in rows]


#: Chart intervals the People Distribution page offers, in minutes.
BUCKET_MINUTES = (15, 30, 60, 180, 360, 720, 1440)


async def buckets_for(db: AsyncSession, camera_id: uuid.UUID, day: date,
                      minutes: int, now: Optional[datetime] = None) -> list[dict]:
    """One local day cut into fixed `minutes` buckets from local midnight, each
    with the occupancy and dwell its samples saw. Read from every sample (not the
    capped `series`), so a 24-hour day at a 10-second cadence is all counted.

    Per bucket:
      people_avg / people_peak  -- mean and highest total in the zones at once
      dwell_avg_s               -- mean time the people present had been there,
                                   averaged over samples that had anyone
      dwell_max_s               -- the longest single stay seen in the bucket
    A bucket with no samples has `samples: 0` and nulls, so a chart can tell
    "nobody there" (0) from "not reporting" (gap). Buckets that start after
    `now` are left out, so today ends at the present.
    """
    lo, hi = local_day_bounds(day)
    t = PeopleDistributionSample.__table__
    rows = (await db.execute(
        select(t.c.captured_at, t.c.total_count, t.c.zones)
        .where(t.c.camera_id == camera_id, t.c.captured_at >= lo, t.c.captured_at <= hi)
        .order_by(t.c.captured_at)
    )).all()
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(day, dtime.min).astimezone()
    size = timedelta(minutes=minutes)
    n = max(1, (24 * 60) // minutes)
    acc = [{"n": 0, "sum": 0, "peak": 0, "dsum": 0.0, "dn": 0, "dmax": 0.0}
           for _ in range(n)]
    for at, total, zones in rows:
        i = int((at - start) // size)
        if not 0 <= i < n:
            continue
        a = acc[i]
        a["n"] += 1
        a["sum"] += total or 0
        a["peak"] = max(a["peak"], total or 0)
        people = sum(int(z.get("current_count") or 0) for z in zones or [])
        if people:
            weighted = sum(float(z.get("dwell_seconds_avg") or 0) * int(z.get("current_count") or 0)
                           for z in zones or [])
            a["dsum"] += weighted / people
            a["dn"] += 1
            a["dmax"] = max([a["dmax"]] + [float(z.get("dwell_seconds_max") or 0)
                                           for z in zones or []])
    out = []
    for i, a in enumerate(acc):
        b0 = start + i * size
        if b0 > now:
            break
        b1 = b0 + size
        out.append({
            "start": b0.astimezone(timezone.utc).isoformat(),
            "label": f"{b0:%H:%M}",
            "end_label": "24:00" if b1.date() != b0.date() else f"{b1:%H:%M}",
            "samples": a["n"],
            "people_avg": round(a["sum"] / a["n"], 2) if a["n"] else None,
            "people_peak": a["peak"] if a["n"] else None,
            "dwell_avg_s": round(a["dsum"] / a["dn"], 1) if a["dn"] else (0.0 if a["n"] else None),
            "dwell_max_s": round(a["dmax"], 1) if a["n"] else None,
        })
    return out


def day_summary(series: list[dict]) -> dict:
    """Peak and average per zone across a day's samples.

    Computed here rather than in the browser so every client agrees, and so a
    long day is not shipped twice (once to draw, once to total).
    """
    if not series:
        return {"samples": 0, "peak_total": 0, "zones": []}
    per_zone: dict[str, dict] = {}
    for s in series:
        for z in s["zones"]:
            acc = per_zone.setdefault(z["zone_name"],
                                      {"zone_name": z["zone_name"],
                                       "peak_count": 0, "_sum": 0, "_n": 0})
            acc["peak_count"] = max(acc["peak_count"], z["current_count"])
            acc["_sum"] += z["current_count"]
            acc["_n"] += 1
    zones = []
    for acc in per_zone.values():
        n = acc.pop("_n") or 1
        total = acc.pop("_sum")
        acc["avg_count"] = round(total / n, 1)
        zones.append(acc)
    zones.sort(key=lambda z: z["zone_name"])
    return {
        "samples": len(series),
        "peak_total": max(s["total_count"] for s in series),
        "zones": zones,
    }


async def hours_for(db: AsyncSession, camera_id: uuid.UUID,
                    day: date) -> list[dict]:
    """Every clock hour of one local day, with each zone's distinct-person count.

    The count is the SIZE OF THE SET, computed here rather than stored, so it
    can never disagree with the ids it came from.

    `total_people` per hour is the size of the UNION across zones, not the sum:
    a person who walks from Zone 1 to Zone 2 is one person on this camera, and
    summing would count them twice. The per-zone numbers and the total are
    therefore answering two different questions, and both are labelled as such
    on the page.
    """
    lo, hi = local_day_bounds(day)
    rows = (await db.execute(
        select(PeopleDistributionHour)
        .where(PeopleDistributionHour.camera_id == camera_id,
               PeopleDistributionHour.hour_start >= lo,
               PeopleDistributionHour.hour_start <= hi)
        .order_by(PeopleDistributionHour.hour_start,
                  PeopleDistributionHour.zone_name)
    )).scalars().all()

    buckets: dict[datetime, dict] = {}
    for r in rows:
        ids = set(r.tracker_ids or [])
        b = buckets.setdefault(r.hour_start, {"zones": [], "_all": set()})
        b["zones"].append({"zone_name": r.zone_name, "unique_people": len(ids)})
        b["_all"] |= ids

    out = []
    for start in sorted(buckets):
        b = buckets[start]
        local = start.astimezone()
        out.append({
            "hour_start": start.isoformat(),
            "hour_label": f"{local:%H:%M}–{local.replace(hour=(local.hour + 1) % 24):%H:%M}",
            "zones": b["zones"],
            # Distinct people ANYWHERE on this camera in the hour. Deliberately
            # not the sum of the zone numbers.
            "total_people": len(b["_all"]),
        })
    return out


async def purge_expired(db: AsyncSession, now: Optional[datetime] = None) -> int:
    """Drop samples past their deadline. An indexed DELETE, like the others."""
    now = now or datetime.now(timezone.utc)
    removed = 0
    for model in (PeopleDistributionSample, PeopleDistributionHour):
        t = model.__table__
        result = await db.execute(
            delete(t).where(t.c.expires_at.isnot(None), t.c.expires_at <= now))
        removed += result.rowcount or 0
    await db.commit()
    if removed:
        log.info("people_distribution.purged", rows=removed)
    return removed
