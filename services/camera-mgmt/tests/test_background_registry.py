"""services/background — the registration point that lets an extension run a loop.

Core-suite test: it must pass with no extension present, so it registers its own
throwaway loops and restores the registry afterwards.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import background  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(background, "_REGISTERED", {})
    monkeypatch.setattr(background, "_TASKS", [])


async def test_registered_loops_start_and_stop():
    ran = asyncio.Event()
    cancelled = asyncio.Event()

    async def loop():
        ran.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    background.register_loop("t_loop", loop, leader_only=False)
    background.register_loop("t_loop", loop, leader_only=False)      # idempotent per name
    assert background.registered() == ["t_loop"]
    assert background.start_all() == ["t_loop"]
    await asyncio.wait_for(ran.wait(), 1)
    await background.stop_all(timeout=1)
    assert cancelled.is_set() and background._TASKS == []


async def test_leader_only_loops_go_through_run_as_leader(monkeypatch):
    seen = []

    async def fake_leader(name, factory):
        seen.append(name)

    monkeypatch.setattr(background.leader, "run_as_leader", fake_leader)

    async def loop():
        return None

    background.register_loop("t_leader", loop)
    background.start_all()
    await asyncio.sleep(0)
    await background.stop_all(timeout=1)
    assert seen == ["t_leader"]


def test_background_names_no_extension():
    src = (ROOT / "backend" / "services" / "background.py").read_text(encoding="utf-8")
    assert "extensions." not in src and "ptz_patrols" not in src
