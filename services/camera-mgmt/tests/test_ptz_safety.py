"""Unit tests for the PTZ safety invariants.

PTZ is the only camera command in this product that keeps running after its
response returns, which makes it the only one whose bugs are *physical*: a
camera aimed at a wall, and no footage of whatever it was watching. Every test
here is a regression test for a way that happens.

Three of them cover bugs that were in the first cut of this feature and were
caught by driving the real API, not by reading it:

  • test_failed_stop_still_releases_the_lease — a Stop that failed at the camera
    left the lease held, locking the camera to an operator who could not move it.
  • test_failed_stop_leaves_the_watchdog_armed — the router disarmed the watchdog
    BEFORE the Stop, so a Stop that then failed cancelled the only thing left
    that would have stopped a moving camera.
  • test_watchdog_retries_a_transient_failure — one failed Stop used to be the
    end of it.

These run without a camera and without Redis: the module's Redis client is
replaced with a dict-backed fake, because what is under test is the ORDERING
and the supersede rule, not redis-py.
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.security import Principal  # noqa: E402
from backend.services import ptz_control  # noqa: E402
from backend.services.onvif_media import OnvifError  # noqa: E402


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeRedis:
    """The operations ptz_control uses. `ex` is ignored: every test here
    asserts on explicit supersede/delete/compare-and-delete, never on
    wall-clock expiry. `eval` implements only the compare-and-delete the
    module ships — the script text is not interpreted."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    async def expire(self, key, ttl):
        return True

    async def eval(self, script, numkeys, key, value, *args):
        # ptz_control's only script is delete-if-value-unchanged.
        if self.store.get(key) == value:
            del self.store[key]
            return 1
        return 0

    async def scan_iter(self, match="*", count=100):
        import fnmatch
        for k in list(self.store):
            if fnmatch.fnmatch(k, match):
                yield k


TARGET = ("10.0.0.1", 80, "user", "pass")
ALICE = Principal("alice", frozenset({"operator"}), "user")
BOB = Principal("bob", frozenset({"operator"}), "user")
ROOT_USER = Principal("root", frozenset({"admin"}), "user")


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()

    async def get_redis():
        return fake

    monkeypatch.setattr(ptz_control.redis_client, "get_redis", get_redis)
    return fake


class StopRecorder:
    """Records Stop calls instead of dialling a camera. `fail_first(n)` makes
    the first n attempts raise, which is how the retry tests model a network
    blip (n small) versus an unplugged camera (n large)."""

    def __init__(self):
        self.calls: list = []
        self._fail_first = 0

    def fail_first(self, n: int) -> None:
        self._fail_first = n

    async def stop(self, ip, port, user, password, token):
        self.calls.append((ip, token))
        if len(self.calls) <= self._fail_first:
            raise OnvifError("camera unreachable")
        return {}

    def __len__(self) -> int:
        return len(self.calls)

    def __eq__(self, other) -> bool:
        return self.calls == other


@pytest.fixture
def stops(monkeypatch):
    rec = StopRecorder()
    monkeypatch.setattr(ptz_control.onvif_ptz, "stop", rec.stop)
    # Real retry spacing is 1s; tests assert on the COUNT and the ordering, not
    # on wall-clock, so shrink it rather than sleeping through three seconds.
    monkeypatch.setattr(ptz_control, "_WATCHDOG_RETRY_SEC", 0.02)
    return rec


# ── Control lease ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_acquire_starts_a_session_and_a_refresh_does_not(redis):
    """The return value is what decides whether an audit row is written. If a
    refresh reported True, one held drag would write an audit row per heartbeat
    and bury the trail it exists to make readable."""
    assert await ptz_control.acquire("cam", ALICE) is True
    assert await ptz_control.acquire("cam", ALICE) is False
    assert await ptz_control.acquire("cam", ALICE) is False


@pytest.mark.asyncio
async def test_second_operator_is_refused_while_someone_holds_the_camera(redis):
    await ptz_control.acquire("cam", ALICE)
    with pytest.raises(HTTPException) as exc:
        await ptz_control.acquire("cam", BOB)
    assert exc.value.status_code == 409
    assert await ptz_control.current_holder("cam") == "alice"


@pytest.mark.asyncio
async def test_admin_preempts_the_lease(redis):
    """Priority-lite: the whole hierarchy is 'admins win'. Enough for a
    single-site appliance, and it must not silently stop working."""
    await ptz_control.acquire("cam", ALICE)
    assert await ptz_control.acquire("cam", ROOT_USER) is True
    assert await ptz_control.current_holder("cam") == "root"


@pytest.mark.asyncio
async def test_a_non_holder_cannot_release_someone_elses_lease(redis):
    await ptz_control.acquire("cam", ALICE)
    await ptz_control.release("cam", BOB)
    assert await ptz_control.current_holder("cam") == "alice"


# ── Deadman watchdog ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watchdog_stops_a_move_the_client_never_ended(redis, stops):
    """The whole reason the watchdog exists: the tab died mid-drag."""
    await ptz_control.arm("cam", TARGET, "P1", 0.05)
    await asyncio.sleep(0.2)
    assert len(stops) == 1


@pytest.mark.asyncio
async def test_a_newer_command_supersedes_an_older_watchdog(redis, stops):
    """The multi-worker case. Each uvicorn worker arms its own watchdog, so
    without the Redis command-id check a watchdog from worker A would cut a
    live move that worker B had just re-issued — roughly whenever the load
    balancer alternates, which is to say constantly."""
    await ptz_control.arm("cam", TARGET, "P1", 0.15)
    await asyncio.sleep(0.05)
    await ptz_control.arm("cam", TARGET, "P1", 0.15)   # heartbeat / other worker
    await asyncio.sleep(0.15)
    assert stops == [], "the stale watchdog must not stop a live move"
    await asyncio.sleep(0.1)
    assert len(stops) == 1, "the current watchdog must still fire"


@pytest.mark.asyncio
async def test_explicit_disarm_cancels_the_watchdog(redis, stops):
    await ptz_control.arm("cam", TARGET, "P1", 0.05)
    await ptz_control.disarm("cam")
    await asyncio.sleep(0.2)
    assert stops == []


@pytest.mark.asyncio
async def test_watchdog_retries_a_transient_failure(redis, stops):
    """Regression: one failed Stop used to be the end of it, which turned a
    one-packet network blip into a camera that kept panning."""
    stops.fail_first(2)
    await ptz_control.arm("cam", TARGET, "P1", 0.05)
    await asyncio.sleep(0.4)
    assert len(stops) == 3, "must retry until the camera confirms"


@pytest.mark.asyncio
async def test_watchdog_gives_up_on_a_dead_camera(redis, stops):
    """The other half: an unplugged camera must not spin a retry loop forever."""
    stops.fail_first(99)
    await ptz_control.arm("cam", TARGET, "P1", 0.05)
    await asyncio.sleep(0.5)
    assert len(stops) == ptz_control._WATCHDOG_STOP_ATTEMPTS


@pytest.mark.asyncio
async def test_clear_wipes_both_keys(redis):
    """A deleted camera must not leave a lease that a re-added camera of the
    same slug would inherit."""
    await ptz_control.acquire("cam", ALICE)
    await ptz_control.arm("cam", TARGET, "P1", 60)
    await ptz_control.clear("cam")
    assert redis.store == {}


@pytest.mark.asyncio
async def test_watchdog_failure_leaves_the_record_for_the_reaper(redis, stops):
    """Regression for the hole this section exists to close: the first cut
    deleted the record BEFORE attempting the stop, so a stop that then failed
    left a moving camera with no evidence anywhere that it needed stopping."""
    stops.fail_first(99)
    await ptz_control.arm("cam", TARGET, "P1", 0.05)
    await asyncio.sleep(0.5)
    assert len(stops) == ptz_control._WATCHDOG_STOP_ATTEMPTS
    assert ptz_control._cmd_key("cam") in redis.store, \
        "an unstopped move must stay visible to the reaper"


@pytest.mark.asyncio
async def test_confirmed_stop_retires_the_record(redis, stops):
    await ptz_control.arm("cam", TARGET, "P1", 0.05)
    await asyncio.sleep(0.3)
    assert len(stops) == 1
    assert ptz_control._cmd_key("cam") not in redis.store, \
        "a camera-confirmed stop must retire the record"


# ── Reaper: the guard that survives process death ────────────────────────────

@pytest.fixture
def resolver(monkeypatch):
    """Answer the reaper's Postgres lookup without a database."""
    async def resolve(slug):
        return ("10.0.0.1", 80, "user", "pass", "P_from_caps")
    monkeypatch.setattr(ptz_control, "_resolve_target", resolve)


def _orphan_record(redis, slug, *, age_past_deadline, profile_token="P1"):
    """Plant what a dead worker leaves behind: an in-flight record whose
    watchdog will never fire (we simply never create the asyncio task)."""
    import json, time
    raw = json.dumps({
        "id": "dead-worker-cmd",
        "deadline": time.time() - age_past_deadline,
        "profile_token": profile_token,
    })
    redis.store[ptz_control._cmd_key(slug)] = raw
    return raw


@pytest.mark.asyncio
async def test_reaper_stops_a_move_whose_worker_died(redis, stops, resolver):
    """THE scenario: kill -9 mid-drag. The record is past its deadline and no
    watchdog exists in any process. Exactly what test_watchdog_stops_a_move…
    proves for a dead CLIENT, this proves for a dead WORKER."""
    _orphan_record(redis, "cam", age_past_deadline=ptz_control._REAPER_GRACE_SEC + 1)
    r = await ptz_control.redis_client.get_redis()
    await ptz_control._reap_one(r, ptz_control._cmd_key("cam"), __import__("time").time())
    assert len(stops) == 1, "the reaper must stop the orphaned move"
    assert stops.calls[0][1] == "P1", "must address the profile token from the record"
    assert ptz_control._cmd_key("cam") not in redis.store


@pytest.mark.asyncio
async def test_reaper_leaves_a_live_move_alone(redis, stops, resolver):
    """A record inside its deadline+grace window belongs to a live watchdog
    (or a heartbeat about to renew it). The reaper cutting live moves would be
    the stale-watchdog bug reintroduced one layer up."""
    import json, time
    raw = json.dumps({"id": "live", "deadline": time.time() + 2.0, "profile_token": "P1"})
    redis.store[ptz_control._cmd_key("cam")] = raw
    r = await ptz_control.redis_client.get_redis()
    await ptz_control._reap_one(r, ptz_control._cmd_key("cam"), time.time())
    assert stops == [], "the reaper must not touch an in-flight move"
    assert ptz_control._cmd_key("cam") in redis.store


@pytest.mark.asyncio
async def test_reaper_keeps_retrying_an_unreachable_camera(redis, stops, resolver):
    """A failed stop leaves the record so the next sweep tries again; the
    record's own TTL is what eventually bounds this."""
    stops.fail_first(1)
    _orphan_record(redis, "cam", age_past_deadline=ptz_control._REAPER_GRACE_SEC + 1)
    r = await ptz_control.redis_client.get_redis()
    import time
    await ptz_control._reap_one(r, ptz_control._cmd_key("cam"), time.time())
    assert len(stops) == 1
    assert ptz_control._cmd_key("cam") in redis.store, "must survive for the next sweep"
    await ptz_control._reap_one(r, ptz_control._cmd_key("cam"), time.time())
    assert len(stops) == 2, "the next sweep retries"
    assert ptz_control._cmd_key("cam") not in redis.store, "…and retires it on success"


@pytest.mark.asyncio
async def test_reaper_drops_a_record_it_cannot_act_on(redis, stops, monkeypatch):
    """A record for a deleted camera (or one whose credentials no longer
    decrypt) can never be stopped from here — it must be dropped with an error
    log, not re-logged every sweep for a minute."""
    async def resolve(slug):
        return None
    monkeypatch.setattr(ptz_control, "_resolve_target", resolve)
    _orphan_record(redis, "cam", age_past_deadline=ptz_control._REAPER_GRACE_SEC + 1)
    r = await ptz_control.redis_client.get_redis()
    import time
    await ptz_control._reap_one(r, ptz_control._cmd_key("cam"), time.time())
    assert stops == []
    assert ptz_control._cmd_key("cam") not in redis.store


@pytest.mark.asyncio
async def test_reaper_drops_an_unreadable_record_without_dialling(redis, stops, resolver):
    redis.store[ptz_control._cmd_key("cam")] = "not-json-a-legacy-bare-id"
    r = await ptz_control.redis_client.get_redis()
    import time
    await ptz_control._reap_one(r, ptz_control._cmd_key("cam"), time.time())
    assert stops == []
    assert ptz_control._cmd_key("cam") not in redis.store


# ── The product rule: PTZ only on PTZ cameras ────────────────────────────────

@pytest.mark.parametrize("caps,expected", [
    (None, False),                       # never probed is NOT "no PTZ"
    ({}, False),
    ({"supported": False}, False),
    ({"supported": True}, True),
])
def test_is_capable_distinguishes_never_probed_from_fixed(caps, expected):
    assert ptz_control.is_capable(caps) is expected


def test_supports_refuses_a_move_kind_the_camera_never_advertised():
    caps = {"supported": True,
            "continuous": {"pan_tilt": True, "zoom": False},
            "relative": {"pan_tilt": False, "zoom": False}}
    assert ptz_control.supports(caps, "continuous", "pan_tilt") is True
    assert ptz_control.supports(caps, "continuous", "zoom") is False
    assert ptz_control.supports(caps, "relative", "pan_tilt") is False
    assert ptz_control.supports(caps, "absolute", "pan_tilt") is False
    assert ptz_control.supports(None, "continuous", "pan_tilt") is False
