"""Bulk calibration (Cameras → PTZ → Calibrate selected): the queue runs ONE camera at a time, in order, skips a
camera someone is steering (once back to the end, then a note), carries on after a Stop, and never runs twice."""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.routers import ptz as ptz_router  # noqa: E402


class ListRedis:
    """Just what the queue uses: strings with NX/EX, lists, mget."""

    def __init__(self):
        self.kv: dict = {}
        self.lists: dict = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)

    async def expire(self, k, s):
        return True

    async def exists(self, *ks):
        return sum(1 for k in ks if k in self.kv)

    async def rpush(self, k, v):
        self.lists.setdefault(k, []).append(v)

    async def lpop(self, k):
        lst = self.lists.get(k) or []
        return lst.pop(0) if lst else None

    async def lrange(self, k, a, b):
        return list(self.lists.get(k) or [])

    async def lrem(self, k, n, v):
        lst = self.lists.get(k) or []
        if v in lst:
            lst.remove(v)


@pytest.fixture
def world(monkeypatch):
    fake = ListRedis()
    from backend import redis_client

    async def get_redis():
        return fake
    monkeypatch.setattr(redis_client, "get_redis", get_redis)
    cams = {str(uuid.uuid4()): SimpleNamespace(id=None, slug=f"cam{i}", ptz_capabilities={"supported": True})
            for i in range(3)}
    for cid, c in cams.items():
        c.id = uuid.UUID(cid)

    async def queue_camera(cid):
        return cams.get(cid)
    monkeypatch.setattr(ptz_router, "_queue_camera", queue_camera)
    monkeypatch.setattr(ptz_router.ptz_control, "is_capable", lambda caps: bool(caps.get("supported")))
    holders: dict = {}

    async def current_holder(slug):
        return holders.get(slug)
    monkeypatch.setattr(ptz_router.ptz_control, "current_holder", current_holder)

    async def hold(slug):
        return None
    monkeypatch.setattr(ptz_router, "_hold", hold)
    ran: list = []
    live = {"now": 0, "most": 0}

    async def run_calibration(camera_id, slug, by):
        live["now"] += 1
        live["most"] = max(live["most"], live["now"])
        ran.append(slug)
        await asyncio.sleep(0.01)
        live["now"] -= 1
        await ptz_router._calib_state(slug, state="done")
    monkeypatch.setattr(ptz_router, "_run_calibration", run_calibration)
    ptz_router._calibrations.clear()
    return SimpleNamespace(redis=fake, cams=cams, holders=holders, ran=ran, live=live)


def _enqueue(world, ids):
    for cid in ids:
        world.redis.lists.setdefault(ptz_router._QUEUE_KEY, []).append(json.dumps({"camera_id": cid, "by": "admin", "tries": 0}))


def _state(world, slug):
    return json.loads(world.redis.kv[ptz_router._calib_key(slug)])


def test_cameras_calibrate_one_at_a_time_in_order(world):
    ids = list(world.cams)
    _enqueue(world, ids)
    asyncio.run(ptz_router._queue_runner())
    assert world.ran == [world.cams[i].slug for i in ids]
    assert world.live["most"] == 1                               # never two at once
    assert all(_state(world, c.slug)["state"] == "done" for c in world.cams.values())
    assert ptz_router._QUEUE_LOCK not in world.redis.kv          # lock released when the queue is empty


def test_a_camera_someone_is_steering_goes_to_the_end_once_then_is_skipped(world):
    ids = list(world.cams)
    busy = world.cams[ids[0]].slug
    world.holders[busy] = "ramesh"
    _enqueue(world, ids)
    asyncio.run(ptz_router._queue_runner())
    assert busy not in world.ran and len(world.ran) == 2
    st = _state(world, busy)
    assert st["state"] == "failed" and "ramesh" in st["error"]

    world.ran.clear()
    world.holders[busy] = None                                   # free by the time it comes round again
    _enqueue(world, [ids[0], ids[1]])
    world.holders[busy] = "ramesh"

    async def go():
        task = asyncio.create_task(ptz_router._queue_runner())
        await asyncio.sleep(0)
        world.holders[busy] = None                               # lets go while the other one runs
        await task
    asyncio.run(go())
    assert world.ran == [world.cams[ids[1]].slug, busy]          # retried at the end and done


def test_a_second_runner_does_not_start_while_one_is_working(world):
    world.redis.kv[ptz_router._QUEUE_LOCK] = "1"
    _enqueue(world, list(world.cams))
    asyncio.run(ptz_router._queue_runner())
    assert world.ran == []                                        # the other worker owns the queue


def test_stopping_the_running_camera_moves_on_to_the_next(world, monkeypatch):
    ids = list(world.cams)

    async def stuck(camera_id, slug, by):
        world.ran.append(slug)
        await asyncio.sleep(10)
    monkeypatch.setattr(ptz_router, "_run_calibration", stuck)

    async def go():
        task = asyncio.create_task(ptz_router._queue_runner())
        for _ in range(3):
            await asyncio.sleep(0.01)
            for t in list(ptz_router._calibrations.values()):
                if not t.done():
                    t.cancel()                                     # "Stop" on the running camera
        await asyncio.wait_for(task, 2)
    _enqueue(world, ids)
    asyncio.run(go())
    assert world.ran == [world.cams[i].slug for i in ids]          # each started, none held the queue up
