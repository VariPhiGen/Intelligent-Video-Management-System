"""background.py — a registration point for background loops the core does not name.

WHY THIS EXISTS. Every core loop is started by name in main.py's lifespan. An
optional extension (see backend/extensions) cannot be started that way without
the core naming it — which is the one thing the extension seam forbids. Until
this module, an extension could mount routes and declare capabilities but had
no way to run anything in the background, so a feature that needs a scheduler
(PTZ patrols were the first) could not be an extension at all.

The shape mirrors register_capability: an extension calls ``register_loop`` from
its ``register(app)`` — which runs at import time, before the lifespan — and the
lifespan starts whatever was registered, alongside its own loops, and cancels it
on shutdown with the same 5 s budget. Nothing here knows what it is starting.

``leader=True`` (the default) wraps the factory in leader.run_as_leader so the
loop runs in ONE uvicorn worker, like every reconcile loop in main.py; pass
False only for per-process work (the PTZ keep-warm is the core's example).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

import structlog

from . import leader

log = structlog.get_logger(__name__)

_REGISTERED: dict[str, tuple[Callable[[], Awaitable[None]], bool]] = {}
_TASKS: list[asyncio.Task] = []


def register_loop(name: str, factory: Callable[[], Awaitable[None]], *, leader_only: bool = True) -> None:
    """Declare a background loop to start with the app. Startup-only, idempotent per name."""
    if name in _REGISTERED:
        log.warning("background.loop_already_registered", loop=name)
        return
    _REGISTERED[name] = (factory, leader_only)
    log.info("background.loop_registered", loop=name, leader_only=leader_only)


def registered() -> list[str]:
    return sorted(_REGISTERED)


def start_all() -> list[str]:
    """Start every registered loop. Called once from the lifespan."""
    started: list[str] = []
    for name, (factory, leader_only) in _REGISTERED.items():
        coro = leader.run_as_leader(name, factory) if leader_only else factory()
        _TASKS.append(asyncio.create_task(coro, name=name))
        started.append(name)
    if started:
        log.info("background.loops_started", loops=started)
    return started


async def stop_all(timeout: Optional[float] = 5.0) -> None:
    """Cancel every task start_all() created; same budget as the core loops."""
    for task in list(_TASKS):
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    _TASKS.clear()
