"""Cross-worker rule state: the version counter and the kill switch.

This service runs `uvicorn --workers 4` (Dockerfile.backend, docker-compose).
A rule disabled in one worker that keeps firing in the other three is a
correctness bug for a feature that opens gates, so the arm state and the cache
version live in Valkey and every worker reads them.

The DIRECTION OF FAILURE is the point of these tests. services/leader.py is
fail-OPEN by design and says so in its own docstring: if Valkey is unreachable
its loops run in every worker, because a cache outage silently stopping
recording reconciliation is worse than doing it four times. That reasoning does
not transfer here. These are per-event safety gates, not idempotent loops, and
they fail CLOSED.

Run (from services/camera-mgmt): python -m pytest tests/test_rules_engine_state.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import StubRedis  # noqa: E402

from backend.services import rules_engine  # noqa: E402

pytestmark = pytest.mark.asyncio


class Flaky(StubRedis):
    """StubRedis that can be taken away mid-test, the way Valkey can be."""

    fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("valkey is down")

    async def get(self, key):
        self._check()
        return await super().get(key)

    async def set(self, key, value, **kw):
        self._check()
        return await super().set(key, value, **kw)

    async def incr(self, key):
        self._check()
        return await super().incr(key)


@pytest.fixture
def valkey(monkeypatch):
    """One shared in-memory Valkey. Two readers of it stand in for two uvicorn
    workers: they share the cache and nothing else."""
    r = Flaky()

    async def _get():
        return r

    monkeypatch.setattr(rules_engine, "get_redis", _get)
    return r


# ── The version counter ───────────────────────────────────────────────────────

async def test_bump_version_increments(valkey):
    assert await rules_engine.bump_version() == 1
    assert await rules_engine.bump_version() == 2


async def test_current_version_reads_zero_before_any_write(valkey):
    assert await rules_engine.current_version() == 0


async def test_current_version_is_none_when_valkey_is_gone(valkey):
    """None is not 'version 0'. It means UNCONFIRMED, and a caller holding
    cached rules must not actuate on them."""
    valkey.fail = True
    assert await rules_engine.current_version() is None


# ── The arm state ─────────────────────────────────────────────────────────────

async def test_default_state_is_armed(valkey):
    st = await rules_engine.arm_state()
    assert st.armed is True
    assert st.degraded is False


async def test_disarm_is_visible_to_a_second_reader(valkey):
    """The second read stands in for another uvicorn worker: it shares only
    Valkey, never memory."""
    await rules_engine.set_arm(False, by="alice", reason="gate flapping")
    st = await rules_engine.arm_state()
    assert st.armed is False
    assert st.by == "alice"
    assert st.reason == "gate flapping"
    assert st.at


async def test_rearm_restores(valkey):
    await rules_engine.set_arm(False, by="alice", reason="flapping")
    await rules_engine.set_arm(True, by="bob", reason="fixed the limit switch")
    st = await rules_engine.arm_state()
    assert st.armed is True
    assert st.by == "bob"


async def test_disarm_bumps_the_version_so_workers_notice(valkey):
    """Without the bump a worker would keep its cached rules — and its cached
    idea that it is armed — until something else happened to invalidate them."""
    before = await rules_engine.bump_version()
    await rules_engine.set_arm(False, by="alice", reason="flapping")
    assert int(valkey.store["rules:version"]) > before


async def test_a_corrupt_arm_record_is_treated_as_not_armed(valkey):
    """Garbage in the key must not read as 'armed'. Unparseable is unknown,
    and unknown does not actuate."""
    valkey.store["rules:arm"] = "{not json"
    st = await rules_engine.arm_state()
    assert st.armed is False
    assert st.degraded is True


# ── Fail closed ───────────────────────────────────────────────────────────────

async def test_valkey_outage_reports_degraded_and_refuses_actuation(valkey):
    """FAIL CLOSED, in deliberate contrast to leader.py's fail-open loops."""
    valkey.fail = True
    st = await rules_engine.arm_state()
    assert st.degraded is True
    assert await rules_engine.may_actuate() is False


async def test_may_actuate_is_false_when_disarmed(valkey):
    await rules_engine.set_arm(False, by="alice", reason="flapping")
    assert await rules_engine.may_actuate() is False


async def test_may_actuate_is_true_only_when_armed_and_confirmed(valkey):
    assert await rules_engine.may_actuate() is True
