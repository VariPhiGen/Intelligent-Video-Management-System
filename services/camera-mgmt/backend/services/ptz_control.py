"""ptz_control.py — who may move a camera, who holds it, and what stops a move.

The policy half of PTZ. onvif_ptz.py speaks the protocol; this module owns the
two problems that protocol layer deliberately does not:

**1. Nothing else in this product keeps running after the response.**
A ContinuousMove is a latch: the camera pans until something stops it. The
browser sends stop on mouse-up, but mouse-up is not guaranteed — the tab
crashes, the laptop sleeps, the Wi-Fi drops mid-drag. Then a camera sits at its
pan limit, aimed at a wall, and the footage of whatever it was watching does
not exist. Three guards, because each one alone has a hole the next one covers:

  • the ONVIF ``Timeout`` on the move itself — the camera stops on its own.
    Hole: a large minority of firmwares ignore or reject the field.
  • a server-side watchdog — an asyncio task that issues Stop after the same
    interval unless a newer command has superseded it. Holes: it lives in one
    uvicorn worker while the next command may land in a different one, and it
    dies with its process.
  • a leader-elected reaper — every armed move leaves a durable record in
    Redis (``ptz:cmd:<slug>``: id, deadline, profile token) that only a
    camera-confirmed stop retires. The reaper sweeps for records past their
    deadline whose watchdog never came back — kill -9 mid-drag, an OOM, a
    deploy restart — resolves the camera from Postgres, and issues the Stop
    the dead worker never could. Hole: none left server-side; if the reaper
    cannot reach the camera either, the device's own Timeout is what remains,
    and the error log says exactly that.

The worker-handoff hole closes through the record's id: the watchdog stops the
camera only if the id it armed is *still* the current one when it wakes. A move
re-issued via another worker replaces the record, so the older worker's
watchdog wakes, sees a newer id, and does nothing. Without this, four workers
means a stale watchdog cutting a live move roughly whenever the load balancer
alternates. The process-death hole closes through the record's lifetime: it
deliberately outlives the process that wrote it.

**2. A PTZ head is shared hardware with one position.**
Two operators dragging the same camera produce a fight neither can win, and the
audit trail cannot say who aimed it. Genetec and Milestone solve this with
numbered priority levels; the honest small version is a short exclusive lease:
first mover holds control, refreshed by each command, expiring
``ptz_lock_ttl`` seconds after they stop. Everyone else gets a 409 naming the
holder. Admins preempt — that is the entire priority hierarchy, and it is
enough for a single-site appliance.

A control session is audited once, when the lease is first taken. Auditing
every tick would bury the trail under a hundred rows per drag while telling a
reader nothing the session row does not.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any, Optional

import structlog
from fastapi import HTTPException

from .. import redis_client
from ..config import settings
from ..security import Principal
from . import onvif_ptz

log = structlog.get_logger(__name__)


def _lock_key(slug: str) -> str:
    return f"ptz:lock:{slug}"


def _cmd_key(slug: str) -> str:
    return f"ptz:cmd:{slug}"


def holder_id(principal: Optional[Principal]) -> str:
    """Stable identity for the lease. Service and dev principals share one id —
    they are machine callers, not competing humans."""
    if principal is None:
        return "anonymous"
    if principal.kind == "user":
        return principal.subject
    return f"{principal.kind}:{principal.subject}"


async def acquire(slug: str, principal: Optional[Principal]) -> bool:
    """Take or refresh the control lease. Returns True when this call STARTED a
    session (first acquisition), so the caller can audit exactly once.

    Raises 409 when someone else holds it. Admins preempt instead.
    """
    me = holder_id(principal)
    ttl = settings.ptz_lock_ttl
    r = await redis_client.get_redis()
    key = _lock_key(slug)

    if await r.set(key, me, ex=ttl, nx=True):
        return True

    current = await r.get(key)
    if current == me:
        await r.expire(key, ttl)   # refresh — same operator still driving
        return False

    is_admin = principal is not None and principal.has_any(frozenset({"admin"}))
    if is_admin:
        await r.set(key, me, ex=ttl)
        log.info("ptz.lock_preempted", slug=slug, by=me, was=current)
        return True

    raise HTTPException(
        status_code=409,
        detail="Another operator is controlling this camera right now — "
               "PTZ frees up a few seconds after they stop.",
    )


async def release(slug: str, principal: Optional[Principal]) -> None:
    """Drop the lease if we still hold it. Best-effort: the TTL is the real
    guarantee, this just hands the camera over sooner."""
    r = await redis_client.get_redis()
    key = _lock_key(slug)
    if await r.get(key) == holder_id(principal):
        await r.delete(key)


async def current_holder(slug: str) -> Optional[str]:
    r = await redis_client.get_redis()
    return await r.get(_lock_key(slug))


# ── Who moved it (for the position history, services/ptz_track) ──────────────
#
# The tracker only SEES where the head ends up; it can't see why. Every path that commands a move leaves a short
# hint here — the operator's pad (via the router's lease), a preset recall, go-home, a patrol step, a calibration
# — and the tracker attributes the next change it sees to the newest hint. A change with no fresh hint is the
# camera moving by itself (its own web UI or tour, a reboot) and is logged as "camera".

MOVER_TTL_SEC = 90


def _mover_key(slug: str) -> str:
    return f"ptz:mover:{slug}"


async def note_mover(slug: str, cause: str, actor: Optional[str] = None) -> None:
    import json
    import time as _time
    try:
        r = await redis_client.get_redis()
        await r.set(_mover_key(slug), json.dumps({"cause": cause, "actor": actor, "at": _time.time()}),
                    ex=MOVER_TTL_SEC)
    except Exception as exc:                            # noqa: BLE001 — a hint must never block a move
        log.debug("ptz.note_mover_failed", slug=slug, error=str(exc))


async def last_mover(slug: str) -> Optional[dict]:
    import json
    r = await redis_client.get_redis()
    raw = await r.get(_mover_key(slug))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# ── Deadman watchdog ─────────────────────────────────────────────────────────

_watchdogs: set[asyncio.Task] = set()


# The watchdog is the last thing standing between a dropped client and a camera
# that pans until someone walks to it, so a single failed Stop is not where it
# gives up. Three attempts over a few seconds covers the realistic cause — a
# transient network blip on the same LAN — without turning a genuinely
# unplugged camera into an endless retry loop.
_WATCHDOG_STOP_ATTEMPTS = 3
_WATCHDOG_RETRY_SEC = 1.0

# The reaper (below) is the guard behind THAT guard, so its parameters are
# safety limits, not tuning knobs, and deliberately not settings:
#
#   _REAPER_GRACE_SEC   how long past a move's deadline the in-process watchdog
#                       keeps first claim. Covers its full retry budget
#                       (3 × 1 s) with slack, so the reaper never races a
#                       watchdog that is alive and already stopping the camera.
#   _REAPER_WINDOW_SEC  how long an orphaned record stays visible — i.e. how
#                       long the reaper keeps retrying a camera it cannot
#                       reach before conceding. Bounds the retry loop the same
#                       way _WATCHDOG_STOP_ATTEMPTS bounds the watchdog's.
#   _REAPER_INTERVAL    sweep cadence. An orphan is stopped within
#                       grace + interval ≈ 11 s of its deadline in the worst
#                       case, which is the number to quote when someone asks
#                       how long a camera can run away after a worker dies.
_REAPER_GRACE_SEC = 6.0
_REAPER_WINDOW_SEC = 60
_REAPER_INTERVAL = 5.0

# Delete-only-if-unchanged, same shape as leader.py's release script. The
# watchdog and the reaper both finish by deleting the record they acted on,
# and both must not delete a NEWER record that a live move wrote in between —
# plain DEL here is how a redundant cleanup disarms someone else's protection.
_CAD = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1])
else return 0 end
"""


async def _cad_delete(r: Any, key: str, value: str) -> None:
    with contextlib.suppress(Exception):
        await r.eval(_CAD, 1, key, value)


def _cmd_record(cmd_id: str, timeout_s: float, profile_token: Optional[str]) -> str:
    """The in-flight-move record. This is a JSON document rather than a bare
    id because it has TWO readers with different lifetimes: the in-process
    watchdog (which knows everything already and only checks the id) and the
    reaper in whichever worker holds the leader lock AFTER this process died
    (which knows nothing and needs the deadline and profile token from the
    record itself; the address and credentials it re-resolves from Postgres —
    credentials never transit Redis)."""
    return json.dumps({
        "id": cmd_id,
        "deadline": time.time() + timeout_s,
        "profile_token": profile_token,
    })


def _parse_record(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) and "id" in rec else None
    except (ValueError, TypeError):
        return None


async def _watchdog(slug: str, cmd_id: str, raw_record: str, delay: float,
                    target: tuple[str, int, str, str], profile_token: Optional[str]) -> None:
    await asyncio.sleep(delay)
    try:
        r = await redis_client.get_redis()
        # Superseded by a newer command (possibly in another worker) → not ours
        # to stop. Already stopped explicitly → the key is gone.
        rec = _parse_record(await r.get(_cmd_key(slug)))
        if rec is None or rec.get("id") != cmd_id:
            return
    except Exception as exc:  # noqa: BLE001 — a failed watchdog must not crash the worker
        log.error("ptz.watchdog_check_failed", slug=slug, error=str(exc))
        return

    ip, port, user, password = target
    for attempt in range(1, _WATCHDOG_STOP_ATTEMPTS + 1):
        try:
            await onvif_ptz.stop(ip, port, user, password, profile_token)
            log.warning("ptz.watchdog_stopped", slug=slug, attempt=attempt,
                        detail="continuous move was never stopped by the client")
            # Retire the record only now that the camera has CONFIRMED the
            # stop, and only if it is still ours — deleting before the stop
            # (as the first cut did) erases the reaper's evidence exactly when
            # the stop then fails.
            await _cad_delete(r, _cmd_key(slug), raw_record)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == _WATCHDOG_STOP_ATTEMPTS:
                # Out of local attempts. The record stays: the reaper picks it
                # up from whichever worker holds the leader lock and keeps
                # trying for the rest of the record's window.
                log.error(
                    "ptz.watchdog_stop_failed", slug=slug, attempts=attempt, error=str(exc),
                    detail="camera may still be moving — handing off to the reaper",
                )
                return
            await asyncio.sleep(_WATCHDOG_RETRY_SEC)


async def arm(slug: str, target: tuple[str, int, str, str],
              profile_token: Optional[str], timeout_s: float) -> str:
    """Stamp this move as current and schedule its stop. Call AFTER the move
    succeeded — arming a watchdog for a move the camera rejected would issue a
    pointless Stop."""
    cmd_id = uuid.uuid4().hex
    raw = _cmd_record(cmd_id, timeout_s, profile_token)
    r = await redis_client.get_redis()
    # The TTL is the reaper's retry window, NOT the move timeout: the record
    # must outlive the process that wrote it, or a dead worker's move expires
    # out of Redis before anyone else can see it needed stopping.
    await r.set(_cmd_key(slug), raw, ex=int(timeout_s) + _REAPER_WINDOW_SEC)

    task = asyncio.create_task(
        _watchdog(slug, cmd_id, raw, timeout_s, target, profile_token)
    )
    _watchdogs.add(task)
    task.add_done_callback(_watchdogs.discard)
    return cmd_id


async def disarm(slug: str) -> None:
    """Clear the in-flight record — the client stopped the move itself and the
    camera confirmed, so any pending watchdog should stand down.

    Unconditional delete, knowingly: between the camera confirming this stop
    and this delete there is a sub-millisecond window in which another
    operator's full move request (lease, gate, SOAP round trip) would have to
    complete for this to erase a live record — and even then that move's next
    heartbeat re-arms within a second and its ONVIF Timeout still stands.
    """
    r = await redis_client.get_redis()
    await r.delete(_cmd_key(slug))


async def clear(slug: str) -> None:
    """Forget all PTZ state for a camera (deletion / rename)."""
    r = await redis_client.get_redis()
    await r.delete(_lock_key(slug), _cmd_key(slug), _warm_key(slug))


# ── Keep-warm: warm every worker's session while a camera is in play ─────────
#
# The ONVIF session pool (services/onvif_ptz) is process-local, and the API
# runs uvicorn --workers 4. So a session built by the worker that served one
# command does nothing for the other three, and the next command — a heartbeat,
# a stop — lands on a cold worker and pays the ~1 s build again. That is the
# "movement starts late", and the repeated `cold: true` on both lines of a
# single drag, seen on real hardware.
#
# The fix is not to share the session (a live zeep client cannot cross a
# process) but to warm all four ahead of the command. Two shared signals in
# Redis say a camera is in play:
#
#   ptz:lock:<slug>   the control lease — set for the seconds an operator is
#                     actively driving (services above).
#   ptz:warm:<slug>   a short-lived "someone just opened this camera" marker,
#                     set by GET /ptz/status (the pad's mount-time warm-up) so
#                     all workers are warm BEFORE the first press, not after it.
#
# A per-worker loop (NOT leader-elected — each worker must warm its OWN pool)
# reads both, resolves each camera, and touches its session. Bounded work: it
# only ever acts on cameras with a live lease or a fresh marker, which is a
# handful at most, and touching a warm session is free.

_WARM_TTL_SEC = 20             # a status poll (every ~3 s from the tab) refreshes it
_KEEPWARM_INTERVAL = 4.0
_KEEPWARM_MAX = 8              # cap cameras warmed per tick — a sanity bound, not a real limit


def _warm_key(slug: str) -> str:
    return f"ptz:warm:{slug}"


async def mark_warm(slug: str) -> None:
    """Flag a camera as in-play so every worker's keep-warm loop holds a live
    session for it. Called from the pad's mount-time status read, so the first
    real command lands on an already-warm worker."""
    r = await redis_client.get_redis()
    await r.set(_warm_key(slug), "1", ex=_WARM_TTL_SEC)


def _slug_from_key(key: str) -> str:
    # ptz:lock:<slug> / ptz:warm:<slug> — slug may contain nothing exotic, but
    # split on the two known prefixes rather than assuming no colons downstream.
    for prefix in ("ptz:lock:", "ptz:warm:"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key.rsplit(":", 1)[-1]


async def keepwarm_loop() -> None:
    """Keep this worker's ONVIF sessions warm for cameras in active control.

    Runs in every worker. Each tick: gather the cameras that are leased or
    freshly opened, resolve each, and touch this worker's session (building it
    once if cold). A camera that stops being controlled loses its lease and
    marker, this loop stops touching it, and its session idles out normally.
    """
    from . import onvif_ptz

    while True:
        try:
            r = await redis_client.get_redis()
            slugs: set[str] = set()
            for pattern in ("ptz:lock:*", "ptz:warm:*"):
                async for key in r.scan_iter(match=pattern, count=100):
                    slugs.add(_slug_from_key(key))
                    if len(slugs) >= _KEEPWARM_MAX:
                        break
            for slug in list(slugs)[:_KEEPWARM_MAX]:
                target = await _resolve_target(slug)
                if target is None:
                    continue
                ip, port, user, password, _token = target
                try:
                    await onvif_ptz.warm(ip, port, user, password)
                except Exception as exc:  # noqa: BLE001 — a camera we cannot warm is not fatal
                    log.debug("ptz.keepwarm_failed", slug=slug, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive any tick
            log.warning("ptz.keepwarm_tick_failed", error=str(exc))
        await asyncio.sleep(_KEEPWARM_INTERVAL)


# ── Reaper: the watchdog for dead watchdogs ──────────────────────────────────

async def _resolve_target(slug: str) -> Optional[tuple[str, int, str, str, Optional[str]]]:
    """(ip, port, user, password, profile_token-from-caps) for a slug, or None.

    The reaper's own resolver rather than the router's `_onvif_target_or_409`
    because this path has no request to answer: a camera that has been deleted
    or whose credentials no longer decrypt is a reason to drop the record and
    log, not to raise HTTP errors inside a background loop.
    """
    from ..crypto import decrypt
    from ..db import AsyncSessionLocal
    from ..models import Camera, onvif_host
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        camera = (
            await db.execute(select(Camera).where(Camera.slug == slug))
        ).scalar_one_or_none()
    if camera is None or not (camera.ip and camera.onvif_port and camera.enc_password):
        return None
    user = decrypt(camera.enc_username)
    password = decrypt(camera.enc_password)
    if not user or password is None:
        return None
    caps = camera.ptz_capabilities or {}
    return onvif_host(camera.ip, camera.onvif_use_https), camera.onvif_port, user, password, caps.get("profile_token")


async def _reap_one(r: Any, key: str, now: float) -> None:
    raw = await r.get(key)
    if raw is None:
        return  # expired or retired between scan and read
    slug = key.split(":", 2)[2] if key.count(":") >= 2 else key
    rec = _parse_record(raw)
    if rec is None:
        # Unreadable record (a leftover from an older build). It cannot be
        # acted on, and leaving it would re-log every sweep for a minute.
        log.warning("ptz.reaper_dropped_unreadable", slug=slug)
        await _cad_delete(r, key, raw)
        return

    # Inside its window: a live move being heart-beaten, or a fresh deadline
    # whose in-process watchdog gets first claim. Not ours yet.
    if now < float(rec.get("deadline", 0)) + _REAPER_GRACE_SEC:
        return

    resolved = await _resolve_target(slug)
    if resolved is None:
        # Camera gone or credentials unusable — nothing CAN stop it from here.
        log.error("ptz.reaper_unresolvable", slug=slug,
                  detail="in-flight PTZ record for a camera that cannot be addressed")
        await _cad_delete(r, key, raw)
        return

    ip, port, user, password, cap_token = resolved
    token = rec.get("profile_token") or cap_token
    try:
        await onvif_ptz.stop(ip, port, user, password, token)
    except Exception as exc:  # noqa: BLE001 — keep the record; retry next sweep
        log.warning("ptz.reaper_stop_failed", slug=slug, error=str(exc),
                    detail="will retry until the record's window expires")
        return
    log.warning("ptz.reaper_stopped", slug=slug,
                detail="stopped a move whose worker died before its watchdog fired")
    await _cad_delete(r, key, raw)


async def reaper_loop() -> None:
    """Sweep for in-flight moves whose watchdog never came back.

    The in-process watchdog covers a client that vanished; this covers the
    process the WATCHDOG lived in vanishing — kill -9 mid-drag, an OOM, a
    deploy restart. Runs leader-elected in one worker (see main.py), and its
    first sweep runs immediately, which is what catches moves orphaned by a
    full-appliance restart. On a quiet system the scan matches nothing and
    costs one SCAN per interval.
    """
    while True:
        try:
            r = await redis_client.get_redis()
            now = time.time()
            async for key in r.scan_iter(match="ptz:cmd:*", count=100):
                await _reap_one(r, key, now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must outlive any sweep
            log.warning("ptz.reaper_sweep_failed", error=str(exc))
        await asyncio.sleep(_REAPER_INTERVAL)


# ── Registration-time probe ──────────────────────────────────────────────────

async def probe_in_background(camera_id) -> None:
    """Probe and cache a camera's PTZ capabilities without holding up
    registration. Same shape and reasoning as substream.resolve_in_background,
    fired from the same place (discovery._register).

    This exists because of a chicken-and-egg shipped in the first cut: the
    probe ran on credential (re-)entry and on first use of a /ptz endpoint —
    but discovery stores credentials at registration (so credential entry never
    happens), and the UI gates every /ptz call on ptz_capable already being
    true (so first use never happens). A discovery-onboarded PTZ dome sat at
    NULL forever, and the UI honestly reported "not checked yet" with no way to
    check. The UI now self-heals that state too (lib/ptz.ts ensureChecked);
    this probe just means a freshly onboarded camera arrives with the answer
    already cached, the way it arrives with its sub-track options known.

    Stores only on a definite answer, never overwrites, swallows everything —
    a camera that is unreachable or mid-edit must still register successfully,
    and NULL simply remains "ask again later".
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from ..crypto import decrypt
    from ..db import AsyncSessionLocal
    from ..models import Camera, onvif_host

    try:
        async with AsyncSessionLocal() as db:
            camera = (await db.execute(
                select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
            if camera is None or camera.ptz_capabilities is not None:
                return
            if not (camera.ip and camera.onvif_port and camera.enc_password):
                return  # nothing to ask — a manual rtsp:// add
            user = decrypt(camera.enc_username)
            password = decrypt(camera.enc_password)
            if not user or password is None:
                return
            caps = await onvif_ptz.probe(
                onvif_host(camera.ip, camera.onvif_use_https), camera.onvif_port, user, password)
            caps["probed_at"] = datetime.now(timezone.utc).isoformat()
            # Re-read: the probe took seconds and an operator may have beaten
            # us to it via the UI. Theirs wins.
            camera = (await db.execute(
                select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
            if camera is None or camera.ptz_capabilities is not None:
                return
            camera.ptz_capabilities = caps
            await db.commit()
            log.info("ptz.registration_probe", slug=camera.slug,
                     supported=caps.get("supported"), reason=caps.get("reason"))
    except Exception as exc:  # noqa: BLE001 — never let a probe fail a registration
        log.info("ptz.registration_probe_skipped", camera_id=str(camera_id),
                 error=str(exc))


# ── Capability cache ─────────────────────────────────────────────────────────

def is_capable(caps: Optional[dict[str, Any]]) -> bool:
    """PTZ-capable per the stored probe. `None` means never probed — NOT
    'no PTZ'. The two are different answers and the UI says so."""
    return bool(caps) and caps.get("supported") is True


def profile_token(caps: Optional[dict[str, Any]]) -> Optional[str]:
    return (caps or {}).get("profile_token")


def supports(caps: Optional[dict[str, Any]], kind: str, axis: str) -> bool:
    """kind: continuous|relative|absolute — axis: pan_tilt|zoom."""
    return bool(((caps or {}).get(kind) or {}).get(axis))
