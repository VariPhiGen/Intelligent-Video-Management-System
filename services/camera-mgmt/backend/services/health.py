"""
health.py — Background health monitor with exponential-backoff reconnect

Architecture:
  - A single asyncio Task runs monitor_loop() for the lifetime of each worker.
  - Every HEALTH_POLL_INTERVAL seconds it fetches all enabled cameras from
    PostgreSQL and checks their live status against MediaMTX.
  - Health results are written to Redis (TTL 120 s) and to PostgreSQL.
  - If a stream is unhealthy and outside its backoff window, force_reconnect()
    is called, which cycles the MediaMTX path.

Backoff state is stored in Redis so all worker processes share the same view
of reconnect timing (preventing duplicate reconnects from different workers).

Concurrency note:
  Multiple uvicorn workers all run this loop.  Redis SET NX is used as a
  distributed lock for the reconnect step so only one worker performs the
  force-reconnect per backoff cycle.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import select, text

from ..config import settings
from ..db import AsyncSessionLocal
from ..models import Camera, CameraStage, CameraStatusEvent, HealthStatus
from .. import redis_client
from ..services import relay
from ..services import tracks

log = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
_DELAYS = settings.reconnect_delays   # e.g. [5, 10, 30, 60, 120, 300]
_RECONNECT_LOCK_TTL = 25              # seconds; slightly less than poll interval


def _next_delay(attempt: int) -> int:
    idx = min(attempt, len(_DELAYS) - 1)
    return _DELAYS[idx]


# ─────────────────────────────────────────────────────────────────────────────
# Playable-stream probe
# ─────────────────────────────────────────────────────────────────────────────
#
# MediaMTX's `ready` flag says a source is connected, not that anyone can watch
# it. Those come apart when the recorder's clock jumps backwards: the RTSP path
# stays ready and keeps taking bytes while the HLS muxer is dead, so the tile
# shows a green LIVE badge over a "Signal lost" overlay and every automatic
# remedy is a no-op, because by the only measure health.py had, the camera was
# fine. This probe closes that gap — it asks the question the browser asks.

async def _hls_playable(slug: str) -> bool:
    """True if MediaMTX will serve this camera's playlist to a browser.

    A hang and a 5xx both count as failure. Under MediaMTX 1.9.x a muxer with a
    broken clock never answers at all, so the timeout is the signal, not an
    incidental guard on it.
    """
    url = f"{settings.mediamtx_hls_url.rstrip('/')}/{slug}/index.m3u8"
    try:
        async with httpx.AsyncClient(timeout=settings.hls_probe_timeout) as client:
            resp = await client.get(url, follow_redirects=True)
        return resp.status_code == 200
    except httpx.RequestError:
        return False


async def _check_playable(camera: Camera) -> None:
    """Probe HLS for a connected camera and re-stamp it if it stays unplayable.

    Only reached when the RTSP path is ready, so a failure here is specifically
    "connected but unwatchable" rather than an offline camera.
    """
    slug = camera.slug
    if not settings.hls_probe_enabled:
        return

    if await _hls_playable(slug):
        await redis_client.reset_hls_fail_count(slug)
        return

    # Already re-stamped and still failing: the hop is not the answer for this
    # camera. Leave it in place and let the normal disconnect path handle it —
    # flipping back would only restore the state we know is worse.
    if await redis_client.get_restamp(slug):
        log.warning("health.hls_unplayable_while_restamped", slug=slug)
        return

    count = await redis_client.inc_hls_fail_count(slug)
    if count < settings.hls_fail_threshold:
        log.info("health.hls_unplayable", slug=slug, count=count,
                 threshold=settings.hls_fail_threshold)
        return

    # One worker performs the switch; the others see the Redis flag afterwards.
    r = await redis_client.get_redis()
    if not await r.set(f"stream:restamp_lock:{slug}", "1", ex=_RECONNECT_LOCK_TTL, nx=True):
        return

    log.warning("health.enabling_restamp", slug=slug, consecutive_failures=count)
    # Persist first, and best-effort. The Redis flag is what actually drives
    # path construction, so losing the durable copy costs only a re-detection
    # after a Redis flush. Doing it last and rolling back on failure was worse
    # than useless: it left the hop running and carrying video while Redis said
    # the camera was unflagged, so the next reconcile would have torn a working
    # stream back down to the shape known to be broken.
    try:
        await _persist_restamp_flag(slug, True)
    except Exception as exc:
        log.error("health.persist_restamp_failed", slug=slug, error=str(exc))

    try:
        await relay.set_restamp(slug, camera.rtsp_url, True)
        await redis_client.reset_hls_fail_count(slug)
    except Exception as exc:
        log.error("health.enable_restamp_failed", slug=slug, error=str(exc))
        await redis_client.set_restamp(slug, False)


async def _persist_restamp_flag(slug: str, enabled: bool) -> None:
    """Mirror the flag into camera_metadata so it survives a Redis flush.

    Redis holds the copy every worker reads; this one is for durability and so
    an operator can see which cameras are on a hop and why.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            # cast(... as jsonb), never `::jsonb`: SQLAlchemy's text() reads a
            # leading colon as a bind parameter, so a PostgreSQL `::` cast next
            # to one is ambiguous and `:val::jsonb` reached the driver unbound
            # ("syntax error at or near :"). The SQL-standard spelling has no
            # colons at all and is unambiguous.
            text(
                "UPDATE cameras SET camera_metadata = "
                "jsonb_set(coalesce(camera_metadata, cast('{}' as jsonb)), "
                "'{relay_restamp}', cast(:val as jsonb), true) WHERE slug = :slug"
            ),
            {
                "slug": slug,
                "val": (
                    '{"enabled": true, "reason": "hls_unplayable_while_connected"}'
                    if enabled else '{"enabled": false}'
                ),
            },
        )
        await session.commit()


async def restore_restamp_flags() -> None:
    """Re-seed the Redis flags from the database at worker startup.

    Without this a Redis restart would silently drop every camera back to a
    direct pull, and the fault only resurfaces at the next wrap — up to ~72
    minutes of a black tile before the detector earns the flag back.
    """
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT slug FROM cameras "
                "WHERE camera_metadata #>> '{relay_restamp,enabled}' = 'true' "
                "AND slug IS NOT NULL"
            )
        )
        slugs = [row[0] for row in rows]
    for slug in slugs:
        await redis_client.set_restamp(slug, True)
    if slugs:
        log.info("health.restamp_flags_restored", count=len(slugs), slugs=slugs)


# ─────────────────────────────────────────────────────────────────────────────
# Single-camera health check
# ─────────────────────────────────────────────────────────────────────────────

async def check_camera_soon(camera_id, delays: tuple = (3.0, 8.0, 20.0)) -> None:
    """Fast-path status verification right after add / enable / reconnect.

    The periodic monitor is up to HEALTH_POLL_INTERVAL (30s) away, and the
    browser refresh adds up to 10s more — so a freshly added camera reads
    "unknown"/"offline" for up to ~40-70s even though the stream came up in
    seconds. This one-shot rechecks at short delays and stops as soon as the
    camera reports connected. Spawn with asyncio.create_task; never raises.
    """
    for delay in delays:
        try:
            await asyncio.sleep(delay)
            async with AsyncSessionLocal() as db:
                cam = (
                    await db.execute(select(Camera).where(Camera.id == camera_id))
                ).scalar_one_or_none()
            if cam is None or not cam.enabled:
                return
            await check_camera(cam)
            async with AsyncSessionLocal() as db:
                status = (
                    await db.execute(
                        select(Camera.health_status).where(Camera.id == camera_id)
                    )
                ).scalar_one_or_none()
            if status == "connected":
                log.info("health.fast_check.connected", camera_id=str(camera_id),
                         after_sec=delay)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("health.fast_check_error", error=str(exc))


async def check_camera(camera: Camera) -> None:
    slug = camera.slug
    log_ctx = log.bind(slug=slug, camera_id=str(camera.id))

    status_data = await relay.get_path_status(slug)
    connected: bool = status_data.get("connected", False)
    registered: bool = status_data.get("registered", False)

    if connected:
        new_status = HealthStatus.CONNECTED.value
        await redis_client.set_stream_health(slug, new_status, {
            "source_type": status_data.get("source_type"),
            "tracks": status_data.get("tracks", []),
        })
        await redis_client.reset_reconnect_count(slug)
        await redis_client.clear_backoff(slug)
        await _update_db_health(slug, new_status, update_last_seen=True)
        # Connected is not the same as watchable — see _check_playable.
        await _check_playable(camera)
        log_ctx.debug("health.ok")
        return

    # Stream is down ────────────────────────────────────────────────────────
    new_status = HealthStatus.DISCONNECTED.value if registered else HealthStatus.ERROR.value
    await redis_client.set_stream_health(slug, new_status, {
        "source_type": None,
        "tracks": [],
        "registered_in_mediamtx": registered,
    })
    await _update_db_health(slug, new_status, update_last_seen=False)

    # Check backoff — use Redis NX lock to elect one worker for reconnect
    now = datetime.now(tz=timezone.utc)
    next_retry = await redis_client.get_backoff(slug)

    if next_retry is not None and now < next_retry:
        log_ctx.debug("health.backoff_pending", next_retry=next_retry.isoformat())
        return

    # Acquire reconnect lock (NX prevents races between workers)
    r = await redis_client.get_redis()
    lock_key = f"stream:reconnect_lock:{slug}"
    acquired = await r.set(lock_key, "1", ex=_RECONNECT_LOCK_TTL, nx=True)
    if not acquired:
        log_ctx.debug("health.reconnect_lock_held_by_peer")
        return

    # We hold the lock — perform reconnect
    attempt = await redis_client.inc_reconnect_count(slug)
    delay = _next_delay(attempt)
    next_backoff = now + timedelta(seconds=delay)
    await redis_client.set_backoff(slug, next_backoff)

    log_ctx.warning(
        "health.reconnecting",
        attempt=attempt,
        delay_sec=delay,
        next_retry=next_backoff.isoformat(),
    )

    try:
        if not registered:
            # Path disappeared from MediaMTX entirely — re-add from scratch
            await relay.add_path(slug, camera.rtsp_url)
        else:
            await relay.force_reconnect(slug, camera.rtsp_url)
    except Exception as exc:
        log_ctx.error("health.reconnect_failed", error=str(exc))


async def _update_db_health(
    slug: str,
    status: str,
    *,
    update_last_seen: bool,
) -> None:
    """Write the latest status; on an ACTUAL transition also append a row to
    camera_status_events (the uptime history).

    The UPDATE..FROM..FOR UPDATE returns the previous status atomically, so
    when several uvicorn workers poll the same camera concurrently only the
    one that observed the transition inserts an event — no Redis lock needed.
    """
    set_last_seen = ", last_seen_at = now()" if update_last_seen else ""
    stmt = text(
        f"""
        UPDATE cameras AS c
           SET health_status = :new_status{set_last_seen}
          FROM (SELECT id, health_status AS old_status
                  FROM cameras WHERE slug = :slug FOR UPDATE) AS o
         WHERE c.id = o.id
     RETURNING c.id, o.old_status
        """
    )
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(stmt, {"new_status": status, "slug": slug})).first()
            if row is not None and row.old_status != status:
                db.add(CameraStatusEvent(camera_id=row.id, status=status))
                log.info("health.status_changed", slug=slug,
                         old=row.old_status, new=status)
            await db.commit()
    except Exception as exc:
        log.warning("health.db_update_failed", slug=slug, error=str(exc))


async def set_status(slug: str, status: str) -> None:
    """Transition-aware status write for non-poll paths (enable/disable)."""
    await _update_db_health(slug, status, update_last_seen=False)


async def mark_all_unknown(
    reason: str,
    changed_at: Optional[datetime] = None,
    *,
    down_if_previously_up: bool = False,
) -> None:
    """Close the uptime history for every registered camera when monitoring
    stops, logging transition events. A Redis NX lock elects one worker; the
    conditional UPDATE makes stragglers no-ops.

    Called at service startup and shutdown so the span when nobody was
    monitoring isn't left inheriting the last known state (which would count
    service downtime as camera uptime). Normally the span is recorded as
    'unknown' (→ "No data", excluded from uptime %).

    ``down_if_previously_up`` switches the crash-recovery treatment: a camera
    that was 'connected' when the monitor died is recorded as 'disconnected'
    (→ "Down") instead of 'unknown', because a power cut / host-off / OOM
    genuinely stopped serving its stream. Cameras that were not live keep the
    'unknown' treatment. Only the crash-recovery caller passes this; graceful
    shutdown/restart gaps stay "No data".

    ``changed_at`` backdates the transition events — the crash-recovery path
    stamps them at the last heartbeat so an ungraceful death's whole dead
    window replays from the last proven-alive moment (see startup_mark_unknown).
    """
    try:
        r = await redis_client.get_redis()
        if not await r.set("stream:mark_unknown_lock", reason, ex=30, nx=True):
            return
        unknown = HealthStatus.UNKNOWN.value
        params: dict[str, Any] = {
            "unknown": unknown,
            "registered": CameraStage.REGISTERED.value,
        }
        if down_if_previously_up:
            params["connected"] = HealthStatus.CONNECTED.value
            params["disconnected"] = HealthStatus.DISCONNECTED.value
            new_status_sql = (
                "CASE WHEN health_status = :connected "
                "THEN :disconnected ELSE :unknown END"
            )
        else:
            new_status_sql = ":unknown"
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text(
                f"UPDATE cameras SET health_status = {new_status_sql} "
                "WHERE stage = :registered AND health_status <> :unknown "
                "RETURNING id, health_status AS new_status"
            ), params)).all()
            stamp = {"changed_at": changed_at} if changed_at else {}
            for row in rows:
                db.add(CameraStatusEvent(
                    camera_id=row.id, status=row.new_status, **stamp))
            await db.commit()
        if rows:
            down = sum(1 for row in rows
                       if row.new_status == HealthStatus.DISCONNECTED.value)
            log.info("health.marked_unknown", count=len(rows), down_count=down,
                     reason=reason,
                     backdated_to=changed_at.isoformat() if changed_at else None)
    except Exception as exc:
        log.warning("health.mark_unknown_failed", reason=reason, error=str(exc))


_HEARTBEAT_SQL = text(
    "INSERT INTO monitor_heartbeat (id, beat_at) VALUES (1, now()) "
    "ON CONFLICT (id) DO UPDATE SET beat_at = now()"
)


async def _beat() -> None:
    """Dead-man's switch: durably stamp 'the monitor was alive now'. One tiny
    upsert per poll cycle; failure is non-fatal — a missed beat can only make
    the crash-recovery window start earlier (safe direction), and if the DB is
    down, status events can't be written either."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(_HEARTBEAT_SQL)
            await db.commit()
    except Exception as exc:
        log.warning("health.heartbeat_failed", error=str(exc))


async def startup_mark_unknown() -> None:
    """Startup unknown-mark with ungraceful-death detection.

    A graceful shutdown already closed the uptime history via
    mark_all_unknown("shutdown"). But a power cut / OOM kill / hard hang
    never runs that hook — the last heartbeat then sits far in the past, and
    without correction the whole dead window would replay as each camera's
    last recorded status (usually 'connected'), inflating uptime while
    recordings show a gap. Detect that here and backdate the transition to the
    last proven-alive moment. With settings.crash_gap_marks_down (default on),
    cameras that were live are backdated to 'disconnected' (Down) rather than
    'unknown' (No data), since a host-off gap really did stop their streams.

    Fall-through to the plain startup mark when: no heartbeat row (first boot
    after upgrade), the gap is inside the grace window (normal restart), or
    the gap is negative (clock moved backwards — never write an event that
    ends before it starts).
    """
    grace = 3 * settings.health_poll_interval
    backdate: Optional[datetime] = None
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(text(
                "SELECT beat_at FROM monitor_heartbeat WHERE id = 1"
            ))).first()
        if row:
            gap = (datetime.now(timezone.utc) - row[0]).total_seconds()
            if gap > grace:
                backdate = row[0]
                log.warning("health.unmonitored_gap_detected",
                            unmonitored_seconds=round(gap),
                            backdated_to=row[0].isoformat())
    except Exception as exc:
        log.warning("health.heartbeat_read_failed", error=str(exc))
    if backdate:
        await mark_all_unknown("crash-recovery", changed_at=backdate,
                               down_if_previously_up=settings.crash_gap_marks_down)
    else:
        await mark_all_unknown("startup")


async def _prune_events() -> None:
    """Drop status events past retention. Redis NX lock (6h TTL) makes this a
    once-per-6h fleet-wide job even though every worker's loop calls it."""
    try:
        r = await redis_client.get_redis()
        if not await r.set("stream:events_prune_lock", "1", ex=6 * 3600, nx=True):
            return
        async with AsyncSessionLocal() as db:
            res = await db.execute(text(
                "DELETE FROM camera_status_events "
                "WHERE changed_at < now() - make_interval(days => :days)"
            ), {"days": settings.uptime_events_retention_days})
            await db.commit()
        if res.rowcount:
            log.info("health.events_pruned", deleted=res.rowcount,
                     retention_days=settings.uptime_events_retention_days)
    except Exception as exc:
        log.warning("health.events_prune_failed", error=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Main monitor loop
# ─────────────────────────────────────────────────────────────────────────────

async def monitor_loop() -> None:
    """
    Runs forever.  Called as an asyncio background Task from the FastAPI lifespan.
    Each iteration checks all enabled cameras; the interval is configured via
    HEALTH_POLL_INTERVAL (default 30 s).
    """
    log.info("health_monitor.started", interval_sec=settings.health_poll_interval)
    while True:
        try:
            await _beat()
            await _poll_all()
            await _prune_events()
        except asyncio.CancelledError:
            log.info("health_monitor.cancelled")
            return
        except Exception as exc:
            log.error("health_monitor.poll_error", error=str(exc))

        await asyncio.sleep(settings.health_poll_interval)


async def _poll_all() -> None:
    _registered = Camera.stage == CameraStage.REGISTERED.value
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Camera).where(Camera.enabled.is_(True), _registered)
            )
            cameras = result.scalars().all()
            # RELAY NAMES, not slugs and not RECORDING names. The sweep below
            # deletes every path it does not recognise, so the set it is given
            # has to be everything the relay may legitimately hold — and the
            # relay holds strictly more than the recorder does.
            #
            # This has now been wrong twice, in the same direction. First it
            # compared slugs, so a camera with a RECORDING sub lost `<slug>_sub`
            # within 30 s of it being switched on ("the sub never records").
            # Using recording_names() fixed that case and left the next one:
            # a sub that is merely RESOLVED is carried by the relay on demand
            # for the live view's H.265 -> H.264 fallback, but is not a
            # recording track — and `substream.resolve()` always stores
            # `recording_enabled: False`, so that is the default state of every
            # camera that has a sub at all. The sweep deleted the path ~17 ms
            # after each reconcile re-added it, once per poll, forever.
            #
            # tracks.relay_names() is the single definition of what the relay
            # may hold (services/tracks.py); recording_names() answers a
            # different question and must not be substituted here.
            known_names = tracks.relay_names(
                (await db.execute(
                    select(Camera.slug, Camera.rtsp_url, Camera.sub_track)
                    .where(_registered)
                )).all()
            )
    except Exception as exc:
        log.error("health_monitor.db_fetch_error", error=str(exc))
        return

    # Orphan cleanup: remove relay paths that belong to no registry camera.
    # A deleted camera's path can survive a failed remove_path and keep pulling
    # the stream — cameras often allow only 1-2 RTSP sessions, so the stale
    # path locks out the camera's re-added successor (seen in production).
    # remove_path treats 404 as success, so concurrent workers race safely.
    try:
        for p in await relay.list_active_paths():
            name = p.get("name")
            if name and name not in known_names:
                log.warning("health_monitor.orphan_path_removed", path=name)
                await relay.remove_path(name)
    except Exception as exc:
        log.warning("health_monitor.orphan_cleanup_error", error=str(exc))

    if not cameras:
        return

    # Run checks concurrently with bounded concurrency
    sem = asyncio.Semaphore(50)

    async def _bounded_check(cam: Camera) -> None:
        async with sem:
            try:
                await check_camera(cam)
            except Exception as exc:
                log.error("health_monitor.check_error", slug=cam.slug, error=str(exc))

    await asyncio.gather(*[_bounded_check(c) for c in cameras])
    log.debug("health_monitor.poll_done", checked=len(cameras))
