"""leader.py — run a background loop in ONE worker, not in every worker.

WHY THIS EXISTS. The API is served by `uvicorn --workers 4`, and every worker
runs the lifespan, so every background reconcile loop was running four times
concurrently: registry→relay, →NVR, →motion, →Smart Search, →DeepStream, the
camera health monitor and the Keycloak audit poll. Four copies of each.

That was mostly invisible because those loops issue idempotent instructions —
four workers telling the NVR to record the same camera is wasteful, not wrong.
It stopped being invisible on 2026-08-31, when a Smart Search domain change was
healed with a remove-then-add pair: two workers interleaved into add / remove /
add and left a camera registered but not being sampled. Three separate workers
were observed logging the same drift in the same millisecond.

Idempotence is still the first line of defence and the upsert that fixed that
case stays. This is the second: one holder does the work, the rest wait.

FAIL-OPEN, DELIBERATELY. If Valkey is unreachable the loop runs anyway, in every
worker — back to the previous behaviour. The alternative is that a cache outage
silently stops recording reconciliation, camera health and audit collection,
which is far worse than doing them more than once.

The lock is a plain SET NX PX with a token, renewed at a third of its TTL and
released with a compare-and-delete so a worker cannot delete a lock that has
already expired and been taken by someone else.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from typing import Awaitable, Callable, Optional

import structlog

from ..redis_client import get_redis

log = structlog.get_logger(__name__)

#: Long enough that a worker briefly blocked does not lose the lock, short
#: enough that a crashed worker's loop restarts elsewhere promptly.
LOCK_TTL_SEC = 45
RENEW_EVERY_SEC = LOCK_TTL_SEC / 3
#: How often a follower re-checks whether the leader has gone.
POLL_EVERY_SEC = 10

#: Release only if we still hold it. Without the compare, a worker whose lock
#: had already expired would delete the new holder's lock on shutdown.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1])
else return 0 end
"""
_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2])
else return 0 end
"""

_IDENTITY = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def run_as_leader(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Run ``factory()`` for as long as this worker holds the lock for ``name``.

    Never returns except by cancellation: on losing the lock it stops the inner
    loop and goes back to waiting, so leadership moves on its own when a worker
    dies.
    """
    key = f"lock:loop:{name}"
    inner: Optional[asyncio.Task] = None
    holding = False
    try:
        while True:
            try:
                r = await get_redis()
                got = await r.set(key, _IDENTITY, nx=True, px=LOCK_TTL_SEC * 1000)
                if got:
                    if not holding:
                        holding = True
                        log.info("leader.acquired", loop=name, who=_IDENTITY)
                        inner = asyncio.create_task(factory(), name=f"{name}_leader")
                else:
                    renewed = await r.eval(_RENEW, 1, key, _IDENTITY, str(LOCK_TTL_SEC * 1000))
                    if renewed:
                        if not holding:
                            # Our own lock from a previous iteration.
                            holding = True
                            inner = asyncio.create_task(factory(), name=f"{name}_leader")
                    elif holding:
                        # Lost it — a stall long enough for the TTL to lapse.
                        log.warning("leader.lost", loop=name)
                        holding = False
                        await _stop(inner)
                        inner = None
            except Exception as exc:                     # noqa: BLE001
                # FAIL-OPEN: no cache, no election. Run rather than stop.
                if not holding:
                    log.warning("leader.unavailable_running_anyway", loop=name,
                                error=str(exc))
                    holding = True
                    inner = asyncio.create_task(factory(), name=f"{name}_unlocked")

            if inner is not None and inner.done():
                # The loop itself exited; surface why and let the next tick
                # restart it rather than leaving the lock held by a dead loop.
                exc = inner.exception() if not inner.cancelled() else None
                log.warning("leader.loop_exited", loop=name, error=str(exc) if exc else None)
                inner = None
                holding = False

            await asyncio.sleep(RENEW_EVERY_SEC if holding else POLL_EVERY_SEC)
    except asyncio.CancelledError:
        await _stop(inner)
        with contextlib.suppress(Exception):
            r = await get_redis()
            await r.eval(_RELEASE, 1, key, _IDENTITY)
        raise


async def _stop(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=5.0)
