"""heatmaps.py — heat sessions and their pictures: ingest, reads, the runtime switch.

WHERE HEATMAPS COME FROM. The `Heatmap_overlay` activity runs on the CPU
Activity Engine in the analytics service, on the detections it already makes
for every other activity. It accumulates where its chosen classes were seen,
renders an overlay on its snapshot interval, and POSTs each one here. Nothing
in this module looks at a pixel: it stores what analytics decided and serves it
back to the Heatmap page.

THREE STATES, AND WHY "STOPPED" IS TWO OF THEM.

    active  — accumulating now.
    frozen  — an operator switched this camera OFF. Analytics still holds the
              matrix, so switching back ON can RESUME it. The last picture is
              what the page shows, and it stays exactly as it was.
    closed  — the schedule window ended, or the operator chose a new session
              over resuming. Nothing can be added to it again.

WHO OWNS WHAT. The AI Config schedule (`active_days` / `active_hours` on the
activity) says when a heatmap is ALLOWED to run; it lives in
`cameras.analytics_config` and this module never writes it. The runtime switch
says whether an operator wants it running now; it lives in
`heatmap_camera_state` and is pushed to analytics, which holds it in memory
only — so the reconcile loop re-asserts it, exactly as it re-asserts camera
configuration.

SESSION IDS ARE MINTED BY ANALYTICS, like `analytics_events.id`: only analytics
knows when heat actually began accumulating, and a client-minted id makes a
retried upload update one row instead of writing a second. The unique index on
(session_id, kind, captured_at) is the other half of that promise.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import detection_engine, object_store, retention_units
from ..models import (Camera, CameraStage, HeatmapArtifact, HeatmapCameraState,
                      HeatmapSession)

log = structlog.get_logger(__name__)

#: The activity this module serves. One key, not a family: a second heat-like
#: activity would bring its own semantics and deserves its own decision.
ACTIVITY = "Heatmap_overlay"

ACTIVE, FROZEN, CLOSED = "active", "frozen", "closed"

#: WHAT IS KEPT. No archive of pictures: the history of a heatmap is its
#: 5-minute heat blocks (services/heatmap_chunks.py), which any range is redrawn
#: from, so a stored picture of every minute or half-hour is wallpaper nobody
#: reads.
#:
#:   live      the session's newest picture. ONE per session, replaced every
#:             upload, bytes kept in the row.
#:   snapshot  LEGACY. Analytics used to send its per-minute picture under this
#:             kind and every one was archived to the object store for the
#:             camera's whole retention (~1,440 a day per camera). An upload of
#:             this kind is now stored as `live`; old rows age out by expires_at.
#:   final     the one picture the session ended on. One per session.
LIVE, SNAPSHOT, FINAL = "live", "snapshot", "final"
ARCHIVED_KINDS = (FINAL,)

#: Rows deleted per statement by the retention sweep, so no lock is held long.
_PURGE_CHUNK = 500

#: How long an `active` session may go without an upload before the sweep calls
#: it interrupted. Analytics uploads once per snapshot interval for as long as a
#: camera accumulates, so silence this long means the accumulation is gone: the
#: service restarted, the camera was removed, the stream never came back.
#: Deliberately 30x the snapshot interval and not 2x — a brief outage must not
#: close a session that is about to carry straight on. `frozen` is exempt; see
#: close_stale_sessions().
STALE_ACTIVE_S = 1800


def _aware(value: Any) -> Optional[datetime]:
    """Epoch seconds (what analytics sends) or a datetime, as UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(value), timezone.utc)


class Rejected(Exception):
    """The upload cannot be stored, and repeating it will not help."""


class WrongEngine(Rejected):
    """The producer is not the engine this appliance runs.

    A subclass, so every existing caller of `store()` still handles it, but
    distinguished because it is not the same kind of no: the message is well
    formed and would be stored verbatim the moment the operator switches the
    engine. The ingest answers 409 for it and 422 for everything else, which is
    the line the observation door already draws for `deepstream_not_engine`.
    """


#: Who a message is from when it does not say. The CPU Activity Engine's sink
#: has never sent `source` (services/analytics/analytics/heatmap_sink.py) and
#: predates this gate, so its messages are attributed to it rather than refused
#: -- the gate must not break the producer that works today. Every other
#: producer names itself, which means a pipeline that FORGETS the field is shut
#: out rather than waved through: the default fails closed for everyone but the
#: one caller it exists for.
DEFAULT_SOURCE = "cpu"


# ── ingest ───────────────────────────────────────────────────────────────────

async def store(db: AsyncSession, meta: Mapping[str, Any],
                image: Optional[bytes]) -> dict:
    """Store one artifact or lifecycle change from the analytics service.

    Idempotent in both halves: the session is an upsert on its id, and the
    artifact's (session, kind, captured_at) is unique, so a retry after a
    timeout updates rather than duplicates.
    """
    slug = str(meta.get("camera") or "").strip()
    raw_id = str(meta.get("session_id") or "").strip()
    kind = str(meta.get("kind") or LIVE).strip()
    if kind == SNAPSHOT:
        kind = LIVE                      # never archived any more; see the kinds above
    source = str(meta.get("source") or DEFAULT_SOURCE).strip().lower()
    if not slug or not raw_id:
        raise Rejected("camera and session_id are required")
    try:
        session_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise Rejected("session_id must be a uuid") from exc

    camera = (await db.execute(
        select(Camera).where(Camera.slug == slug,
                             Camera.stage == CameraStage.REGISTERED.value)
    )).scalars().first()
    if camera is None:
        raise Rejected(f"unknown camera {slug!r}")

    # ONE ENGINE PER CAMERA. Checked here, before the upsert below: a refused
    # producer must not leave a session row behind, because that row is what
    # `_close_superseded` and the stale sweep then reason about.
    if not detection_engine.heatmap_source_ok(camera, source):
        raise WrongEngine(
            f"{source!r} may not write heatmaps: the engine is "
            f"{detection_engine.engine()!r}")

    started_at = _aware(meta.get("session_started_at")) or datetime.now(timezone.utc)
    status = str(meta.get("status") or ACTIVE)
    if status not in (ACTIVE, FROZEN, CLOSED):
        raise Rejected(f"unknown status {status!r}")
    # A "closed" notice for a session the operator abandoned carries no picture.
    ended_at = _aware(meta.get("session_ended_at"))

    table = HeatmapSession.__table__
    stmt = pg_insert(table).values(
        id=session_id, camera_id=camera.id, activity=ACTIVITY, started_at=started_at,
        ended_at=ended_at, status=status, close_reason=meta.get("close_reason"),
        params=dict(meta.get("params") or {}), points=int(meta.get("points") or 0),
        frames=int(meta.get("frames") or 0), updated_at=func.now(),
    )
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.id],
        set_={"status": stmt.excluded.status, "ended_at": stmt.excluded.ended_at,
              "close_reason": stmt.excluded.close_reason,
              "points": stmt.excluded.points, "frames": stmt.excluded.frames,
              "params": stmt.excluded.params, "updated_at": func.now()},
    ))

    # A session id we have not seen before means the PREVIOUS accumulation on
    # this camera is over, whatever the row still says: analytics only mints a
    # new id once it no longer holds the old one, so that session can never be
    # resumed, closed or updated by anything again. Closing it here is the
    # event-driven half; close_stale_sessions() catches the cameras that never
    # come back to send a new id at all.
    await _close_superseded(db, camera.id)

    stored_artifact = False
    if image:
        if kind not in (LIVE, SNAPSHOT, FINAL):
            raise Rejected(f"unknown kind {kind!r}")
        captured_at = _aware(meta.get("captured_at")) or datetime.now(timezone.utc)
        retention = retention_units.effective_retention_minutes(
            getattr(camera, "retention_minutes", None))

        # WHERE THE BYTES GO. An archived picture (snapshot/final) is written to
        # the object store and the row keeps only its key; `live` keeps its
        # bytes in the row. See services/object_store.py for why the live one is
        # the exception. With no store configured everything falls back to the
        # row, which is exactly how this worked before the store existed.
        object_key = None
        row_image = image
        if kind in ARCHIVED_KINDS and object_store.is_configured():
            # Written BEFORE the row, so a row never references an object that
            # is not there. The reverse order would put a key in the database
            # and then lose the picture if the store refused.
            object_key = object_store.put(
                object_store.heatmap_key(camera.slug, session_id, captured_at, kind),
                image, content_type="image/jpeg")
            row_image = None

        art = HeatmapArtifact.__table__
        ins = pg_insert(art).values(
            session_id=session_id, camera_id=camera.id, kind=kind,
            captured_at=captured_at, period_start=started_at, period_end=captured_at,
            width=int(meta.get("width") or 0), height=int(meta.get("height") or 0),
            content_type="image/jpeg", image=row_image, object_key=object_key,
            points=int(meta.get("points") or 0), peak=float(meta.get("peak") or 0.0),
            expires_at=captured_at + timedelta(minutes=retention),
        )
        if kind == LIVE:
            # ONE LIVE ROW PER SESSION, replaced in place. The archived kinds
            # key on (session, kind, captured_at) because each capture is its
            # own record; a live picture is a single moving value, and keying it
            # on captured_at too would insert a fresh row every 30 s — ~2,880 a
            # day per camera, none of which anyone reads.
            stmt = ins.on_conflict_do_update(
                index_elements=[art.c.session_id],
                # A LITERAL, not `art.c.kind == LIVE`. That form binds a
                # parameter, and Postgres cannot infer a partial index from
                # `WHERE kind = $15` -- it answers "no unique or exclusion
                # constraint matching the ON CONFLICT specification" and every
                # live update after the first one 500s. The predicate has to
                # match migration 047's index text exactly.
                index_where=text(f"kind = '{LIVE}'"),
                set_={"image": ins.excluded.image, "object_key": ins.excluded.object_key,
                      "captured_at": ins.excluded.captured_at,
                      "period_end": ins.excluded.period_end,
                      "width": ins.excluded.width, "height": ins.excluded.height,
                      "points": ins.excluded.points, "peak": ins.excluded.peak,
                      "expires_at": ins.excluded.expires_at},
            )
        else:
            stmt = ins.on_conflict_do_update(
                index_elements=[art.c.session_id, art.c.kind, art.c.captured_at],
                set_={"image": ins.excluded.image, "object_key": ins.excluded.object_key,
                      "points": ins.excluded.points, "peak": ins.excluded.peak,
                      "period_end": ins.excluded.period_end},
            )
        await db.execute(stmt)
        stored_artifact = True
    await db.commit()
    return {"stored": True, "session_id": str(session_id), "artifact": stored_artifact,
            "kind": kind}


# ── the runtime switch ───────────────────────────────────────────────────────

async def runtime_state(db: AsyncSession, camera_id: uuid.UUID) -> bool:
    row = await db.get(HeatmapCameraState, camera_id)
    return True if row is None else bool(row.runtime_enabled)


async def set_runtime(db: AsyncSession, camera: Camera, enabled: bool, *,
                      mode: str = "resume", actor: Optional[str] = None) -> dict:
    """Record the operator's switch, then tell analytics. Never rewrites the schedule."""
    table = HeatmapCameraState.__table__
    stmt = pg_insert(table).values(camera_id=camera.id, runtime_enabled=bool(enabled),
                                   updated_at=func.now(), updated_by=actor)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.camera_id],
        set_={"runtime_enabled": stmt.excluded.runtime_enabled,
              "updated_at": func.now(), "updated_by": stmt.excluded.updated_by},
    ))

    # The local half of the state machine: freezing and closing are visible in
    # the page immediately, not at the next snapshot a minute later.
    now = datetime.now(timezone.utc)
    open_session = await _open_session(db, camera.id)
    if open_session is not None:
        if not enabled and open_session.status == ACTIVE:
            open_session.status, open_session.ended_at = FROZEN, now
            open_session.close_reason = "operator"
        elif enabled and open_session.status == FROZEN:
            if mode == "new":
                open_session.status, open_session.close_reason = CLOSED, "new session"
                open_session.ended_at = open_session.ended_at or now
            else:
                open_session.status, open_session.ended_at = ACTIVE, None
                open_session.close_reason = None
        open_session.updated_at = now
    await db.commit()

    from . import analytics_client
    await analytics_client.push_heatmap(camera.slug, enabled, mode=mode)
    return {"camera": camera.slug, "runtime_enabled": bool(enabled), "mode": mode}


async def _close_superseded(db: AsyncSession, camera_id: uuid.UUID) -> int:
    """Leave one open session per camera: the newest. Close the rest as interrupted.

    `ended_at` becomes the last moment a closed session can be PROVED to have
    been accumulating — its newest picture — falling back to when it was last
    updated, because inventing "now" would claim heat it never gathered.

    Stated as "all but the newest" rather than "older than the upload that just
    arrived" so it is direction-agnostic: a late retry of a stale upload closes
    ITSELF rather than the session that is really running.

    A `frozen` row is closed too when something newer exists, and deliberately:
    if a new session id has arrived, the matrix behind that freeze died with the
    process that held it, so offering "resume" would be a lie. A frozen session
    that IS the newest is left alone — that is an operator's OFF, waiting to be
    resumed, and it is silent by design.
    """
    res = await db.execute(text(
        "UPDATE heatmap_sessions s SET status = 'closed', close_reason = 'interrupted',"
        " ended_at = COALESCE(s.ended_at,"
        "   (SELECT max(a.captured_at) FROM heatmap_artifacts a WHERE a.session_id = s.id),"
        "   s.updated_at),"
        " updated_at = now()"
        " WHERE s.camera_id = :cam AND s.status <> 'closed'"
        "   AND s.started_at < (SELECT max(x.started_at) FROM heatmap_sessions x"
        "                        WHERE x.camera_id = :cam AND x.status <> 'closed')"
    ), {"cam": str(camera_id)})
    return res.rowcount or 0


async def _open_session(db: AsyncSession, camera_id: uuid.UUID) -> Optional[HeatmapSession]:
    return (await db.execute(
        select(HeatmapSession)
        .where(HeatmapSession.camera_id == camera_id, HeatmapSession.status != CLOSED)
        .order_by(HeatmapSession.started_at.desc()).limit(1)
    )).scalars().first()


async def desired_runtime(db: AsyncSession) -> dict[str, bool]:
    """Every registered camera's switch, for the reconcile loop to re-assert.

    Only cameras that actually have the activity configured: pushing a switch
    for a camera analytics has never heard of would create state for nothing.
    """
    rows = (await db.execute(
        select(Camera.slug, Camera.analytics_config, HeatmapCameraState.runtime_enabled)
        .select_from(Camera)
        .outerjoin(HeatmapCameraState, HeatmapCameraState.camera_id == Camera.id)
        .where(Camera.stage == CameraStage.REGISTERED.value, Camera.enabled.is_(True))
    )).all()
    out: dict[str, bool] = {}
    for slug, config, enabled in rows:
        if configured(config):
            out[slug] = True if enabled is None else bool(enabled)
    return out


def configured(analytics_config: Optional[Mapping]) -> bool:
    """Does this camera's AI Config include the heatmap activity?"""
    if not isinstance(analytics_config, Mapping):
        return False
    return any(isinstance(a, Mapping) and str(a.get("type")) == ACTIVITY
               for a in (analytics_config.get("activities") or []))


# ── reads ────────────────────────────────────────────────────────────────────

async def overview(db: AsyncSession) -> list[dict]:
    """One row per camera that has the heatmap configured: state and its latest picture."""
    cameras = (await db.execute(
        select(Camera).where(Camera.stage == CameraStage.REGISTERED.value)
        .order_by(Camera.name)
    )).scalars().all()
    wanted = [c for c in cameras if configured(c.analytics_config)]
    if not wanted:
        return []
    ids = [c.id for c in wanted]

    switches = dict((await db.execute(
        select(HeatmapCameraState.camera_id, HeatmapCameraState.runtime_enabled)
        .where(HeatmapCameraState.camera_id.in_(ids))
    )).all())
    sessions = {s.camera_id: s for s in (await db.execute(
        select(HeatmapSession).where(HeatmapSession.camera_id.in_(ids))
        .order_by(HeatmapSession.camera_id, HeatmapSession.started_at.desc())
        .distinct(HeatmapSession.camera_id)
    )).scalars().all()}
    latest = {a.camera_id: a for a in (await db.execute(
        select(HeatmapArtifact.id, HeatmapArtifact.camera_id, HeatmapArtifact.session_id,
               HeatmapArtifact.kind, HeatmapArtifact.captured_at,
               HeatmapArtifact.period_start, HeatmapArtifact.width, HeatmapArtifact.height)
        .where(HeatmapArtifact.camera_id.in_(ids))
        .order_by(HeatmapArtifact.camera_id, HeatmapArtifact.captured_at.desc())
        .distinct(HeatmapArtifact.camera_id)
    )).all()}

    out = []
    for cam in wanted:
        session = sessions.get(cam.id)
        art = latest.get(cam.id)
        out.append({
            "camera_id": str(cam.id),
            "slug": cam.slug,
            "name": cam.name,
            "camera_enabled": bool(cam.enabled),
            "health": cam.health_status,
            "runtime_enabled": bool(switches.get(cam.id, True)),
            "schedule": schedule_of(cam.analytics_config),
            "session": _session_out(session),
            "latest_artifact": _artifact_out(art),
        })
    return out


def _session_out(session: Optional[HeatmapSession]) -> Optional[dict]:
    if session is None:
        return None
    return {"id": str(session.id), "status": session.status,
            "started_at": session.started_at, "ended_at": session.ended_at,
            "close_reason": session.close_reason, "points": session.points,
            "frames": session.frames, "params": session.params or {}}


def _artifact_out(art) -> Optional[dict]:
    if art is None:
        return None
    return {"id": art.id, "session_id": str(art.session_id), "kind": art.kind,
            "captured_at": art.captured_at, "period_start": art.period_start,
            "width": art.width, "height": art.height}


def schedule_of(analytics_config: Optional[Mapping]) -> Optional[dict]:
    """The activity's configured window, so the page can say when it will run."""
    if not isinstance(analytics_config, Mapping):
        return None
    for entry in analytics_config.get("activities") or []:
        if not isinstance(entry, Mapping) or str(entry.get("type")) != ACTIVITY:
            continue
        params = entry.get("params") or {}
        return {"active_days": params.get("active_days"),
                "active_hours": params.get("active_hours")}
    return None


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC instants a LOCAL calendar day begins and ends.

    An operator picking "20 Sep" means the day they lived through, not
    00:00-24:00 UTC. On this appliance those differ by the TZ offset, so asking
    the database for a UTC day would return the wrong five and a half hours.
    Same local clock the AI Config schedule is evaluated on.
    """
    local_midnight = datetime(day.year, day.month, day.day).astimezone()
    return (local_midnight.astimezone(timezone.utc),
            (local_midnight + timedelta(days=1)).astimezone(timezone.utc))


async def session_or_none(db: AsyncSession, session_id: uuid.UUID) -> Optional[HeatmapSession]:
    """One session by id, or None."""
    return (await db.execute(
        select(HeatmapSession).where(HeatmapSession.id == session_id)
    )).scalars().first()


def session_out(session: Optional[HeatmapSession]) -> Optional[dict]:
    """A session as the API shapes it. Public because the historical view needs
    the same shape the overview already returns."""
    return _session_out(session)


async def artifacts_for_session(db: AsyncSession, session_id: uuid.UUID) -> list[dict]:
    """Every picture of one session, oldest first — the historical timeline.

    ALL of them, not the newest: this is what draws 09:00, 09:30, 10:00 ... and
    the final beside them. `sessions_for` deliberately returns one artifact per
    session for the overview card; this is its opposite and they are not
    interchangeable.
    """
    rows = (await db.execute(
        select(HeatmapArtifact.id, HeatmapArtifact.session_id, HeatmapArtifact.kind,
               HeatmapArtifact.captured_at, HeatmapArtifact.period_start,
               HeatmapArtifact.width, HeatmapArtifact.height)
        .where(HeatmapArtifact.session_id == session_id,
               HeatmapArtifact.expires_at > func.now())
        .order_by(HeatmapArtifact.captured_at.asc())
    )).all()
    return [_artifact_out(a) for a in rows]


async def sessions_for(db: AsyncSession, camera_id: uuid.UUID, limit: int = 20,
                       day: Optional[date] = None) -> list[dict]:
    """A camera's sessions, newest first, each with its latest picture.

    `day` narrows to one LOCAL calendar day, which is what the historical page
    asks for. Without it this is the recent-sessions list the overview uses.
    """
    where = [HeatmapSession.camera_id == camera_id]
    if day is not None:
        frm, to = local_day_bounds(day)
        # A session belongs to the day it STARTED on. One running across
        # midnight is listed under the day it opened, which is how an operator
        # thinks of "last night's session".
        where += [HeatmapSession.started_at >= frm, HeatmapSession.started_at < to]
    rows = (await db.execute(
        select(HeatmapSession).where(*where)
        .order_by(HeatmapSession.started_at.desc()).limit(limit)
    )).scalars().all()
    if not rows:
        return []
    arts = (await db.execute(
        select(HeatmapArtifact.id, HeatmapArtifact.session_id, HeatmapArtifact.kind,
               HeatmapArtifact.captured_at, HeatmapArtifact.period_start,
               HeatmapArtifact.width, HeatmapArtifact.height)
        .where(HeatmapArtifact.session_id.in_([r.id for r in rows]))
        .order_by(HeatmapArtifact.session_id, HeatmapArtifact.captured_at.desc())
        .distinct(HeatmapArtifact.session_id)
    )).all()
    by_session = {a.session_id: a for a in arts}
    return [{**_session_out(r), "latest_artifact": _artifact_out(by_session.get(r.id))}
            for r in rows]


async def artifact(db: AsyncSession, artifact_id: int) -> Optional[HeatmapArtifact]:
    return (await db.execute(
        select(HeatmapArtifact).where(HeatmapArtifact.id == artifact_id,
                                      HeatmapArtifact.expires_at > func.now())
    )).scalars().first()


# ── retention ────────────────────────────────────────────────────────────────

async def close_stale_sessions(db: AsyncSession, stale_s: int = STALE_ACTIVE_S) -> int:
    """Close `active` sessions that nothing has touched for `stale_s` seconds.

    The other half of _supersede_older(): a camera that never comes back never
    sends a new session id, so nothing would ever close its row and the page
    would keep showing an accumulation that stopped days ago.

    `frozen` is EXEMPT, and that is the whole reason this filters on status
    rather than on silence alone: an operator who switches a camera off in the
    afternoon must still be offered "resume" the next morning, and a frozen
    session is silent by definition.
    """
    res = await db.execute(text(
        "UPDATE heatmap_sessions s SET status = 'closed', close_reason = 'interrupted',"
        " ended_at = COALESCE(s.ended_at,"
        "   (SELECT max(a.captured_at) FROM heatmap_artifacts a WHERE a.session_id = s.id),"
        "   s.updated_at),"
        " updated_at = now()"
        " WHERE s.status = 'active'"
        "   AND s.updated_at < now() - (:stale * interval '1 second')"
    ), {"stale": float(stale_s)})
    await db.commit()
    return res.rowcount or 0


async def purge_expired(db: AsyncSession) -> int:
    """Delete pictures past their own deadline, in chunks so no lock is held long."""
    stale = await close_stale_sessions(db)
    if stale:
        log.info("heatmaps.retention.sessions_closed", rows=stale, reason="interrupted")
    total = 0
    while True:
        # THE KEYS FIRST, then the objects, then the rows. A bulk DELETE alone
        # would drop the only record of which objects existed, and the store
        # would accumulate them for ever with nothing left to name them — the
        # database and the bucket would diverge silently and permanently.
        keys = (await db.execute(text(
            "SELECT id, object_key FROM heatmap_artifacts"
            " WHERE expires_at < now() LIMIT :n"
        ), {"n": _PURGE_CHUNK})).all()
        if not keys:
            break
        doomed_ids = [row[0] for row in keys]
        doomed_objects = [row[1] for row in keys if row[1]]

        # Objects BEFORE rows. If this half fails the rows stay, so the next
        # sweep finds them again and retries; the alternative order leaks an
        # object the moment a delete fails. delete_many never raises for one
        # bad key, so a single unreachable object cannot stall retention.
        if doomed_objects and object_store.is_configured():
            removed = object_store.delete_many(doomed_objects)
            if removed != len(doomed_objects):
                log.warning("heatmaps.retention.objects_partial",
                            wanted=len(doomed_objects), removed=removed)

        res = await db.execute(text(
            "DELETE FROM heatmap_artifacts WHERE id = ANY(:ids)"
        ), {"ids": doomed_ids})
        await db.commit()
        total += res.rowcount or 0
        if len(keys) < _PURGE_CHUNK:
            break
    # A session whose pictures have all expired is a row nobody can look at.
    res = await db.execute(text(
        "DELETE FROM heatmap_sessions s WHERE s.status = 'closed'"
        " AND NOT EXISTS (SELECT 1 FROM heatmap_artifacts a WHERE a.session_id = s.id)"
    ))
    await db.commit()
    return total


async def retention_loop() -> None:
    from ..db import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await purge_expired(db)
            if n:
                log.info("heatmaps.retention.purged", rows=n)
            from . import heatmap_chunks
            async with AsyncSessionLocal() as db:
                c = await heatmap_chunks.purge_expired(db)
            if c:
                log.info("heatmaps.retention.chunks_purged", rows=c)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed sweep retries next period
            log.warning("heatmaps.retention.failed", error=str(exc))
        await asyncio.sleep(settings.analytics_events_sweep_seconds)
