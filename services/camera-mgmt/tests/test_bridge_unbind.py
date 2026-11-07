"""Unbinding: the order of the checks, without a database.

What the database does (the row goes, the wall's state goes, `last_seen` and
the peripheral stay) is proved against real rows in test_bridge_db.py; that
file skips without a scratch database, so the part that is pure ordering is
pinned here as well: a follow-up still owed is refused BEFORE anything is
deleted, and a device with no binding blanks nothing.

Run (from services/camera-mgmt): python -m pytest tests/test_bridge_unbind.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import bridge  # noqa: E402

_aio = pytest.mark.asyncio


class FakeDB:
    """Answers the three statements unbind() issues, and records their order."""

    def __init__(self, *, owed: int = 0, bound: bool = True):
        self.owed = owed
        self.bound = bound
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, statement, *a, **kw):
        sql = str(statement)
        if sql.startswith("SELECT count"):
            self.statements.append("count")
            return SimpleNamespace(scalar=lambda: self.owed)
        if sql.startswith("DELETE FROM peripheral_bindings"):
            self.statements.append("delete")
            return SimpleNamespace(rowcount=1 if self.bound else 0)
        if sql.startswith("UPDATE peripherals"):
            self.statements.append("blank")
            return SimpleNamespace(rowcount=1)
        raise AssertionError(f"unexpected statement: {sql}")

    async def commit(self):
        self.commits += 1


@_aio
async def test_unbinding_counts_then_deletes_then_blanks_the_wall():
    db = FakeDB()
    assert await bridge.unbind(db, 8) is True
    assert db.statements == ["count", "delete", "blank"]
    assert db.commits == 1


@_aio
async def test_a_follow_up_still_owed_is_refused_before_anything_is_touched():
    db = FakeDB(owed=2)
    with pytest.raises(bridge.FollowUpOwed) as exc:
        await bridge.unbind(db, 8)
    assert exc.value.owed == 2
    assert db.statements == ["count"]
    assert db.commits == 0


@_aio
async def test_a_device_with_no_binding_blanks_nothing():
    """False, not an exception — and the wall is left alone: with no binding
    there was never a writer, so there is nothing to retract."""
    db = FakeDB(bound=False)
    assert await bridge.unbind(db, 8) is False
    assert db.statements == ["count", "delete"]
    assert db.commits == 0
