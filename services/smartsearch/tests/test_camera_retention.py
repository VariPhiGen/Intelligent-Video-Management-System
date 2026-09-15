"""Unit tests for per-camera retention (index/store.py).

The index stamps `expires_at` once, at insert, from a single appliance-wide
`retention_days`. That was wrong in one specific and consequential way: a camera
whose RECORDING retention is two days had thirty days of crops cut from it left
searchable — the index outliving the footage it describes, and the appliance
reporting a deletion deadline for that camera which it was not keeping for the
personal data derived from it.

What is pinned here is the part that is easy to get subtly wrong:

  * RESTAMPING. Stamping only new rows is a no-op for the case operators
    actually hit, because retention is shortened after the data exists.
  * IDEMPOTENCE. The registry re-asserts every camera on every reconcile pass
    (four uvicorn workers, one sweep interval), so "unchanged" must not issue an
    UPDATE over the whole index each time.
  * NEVER RAISING. This runs inside camera registration; a briefly-down search
    database must not make a camera unregisterable.

The pool is faked. The SQL is one UPDATE covered against a real database
elsewhere; what regresses is which calls happen and when.

Run: python3 -m pytest tests -q
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.domains import ALL_TABLES  # noqa: E402
from index.store import Store  # noqa: E402


class FakeCursor:
    """Drains like the real UPDATE does.

    The restamp is a batched loop that terminates when a batch matches nothing,
    so a fake returning a constant rowcount would spin forever. `pending` is the
    number of rows still carrying the wrong deadline; each execute takes up to
    one batch off it and reports what it took, which is exactly the signal the
    loop reads.
    """

    def __init__(self, pool, raises=False):
        self._pool = pool
        self._rec = pool.sql
        self.rowcount = 0
        self._raises = raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._raises:
            raise psycopg.OperationalError("connection refused")
        flat = " ".join(sql.split())
        self._rec.append((flat, params))
        if flat.startswith("UPDATE"):
            # Per table, as the real thing is: each has its own set of rows
            # still carrying the wrong deadline, and its own loop draining them.
            table = flat.split()[1]
            took = min(self._pool.batch, self._pool.pending.get(table, 0))
            self._pool.pending[table] = self._pool.pending.get(table, 0) - took
            self.rowcount = took
        else:
            self.rowcount = 0

    def executemany(self, sql, params):
        self._rec.append((" ".join(sql.split()), list(params)))


class FakeConn:
    def __init__(self, pool, raises=False):
        self._pool, self._raises = pool, raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, **_kw):
        return FakeCursor(self._pool, self._raises)


class FakePool:
    # From the registry, not a literal: a domain added without its table
    # appearing here would make this fake disagree with the store it stands in
    # for, and the disagreement would look like a passing test.
    TABLES = ALL_TABLES

    def __init__(self, rowcount=0, raises=False, batch=5_000):
        self.sql: list = []
        self.pending = {t: rowcount for t in self.TABLES}
        self.batch = batch
        self._raises = raises

    def seed(self, rowcount):
        self.pending = {t: rowcount for t in self.TABLES}

    def connection(self):
        return FakeConn(self, self._raises)


def make_store(rowcount=0, raises=False, retention_days=30):
    """`rowcount` is per table: every registered domain's table starts with that
    many rows carrying the wrong deadline."""
    store = Store("postgresql://unused", retention_days=retention_days)
    pool = FakePool(rowcount, raises, batch=Store.RESTAMP_BATCH)
    store._get_pool = lambda: pool          # noqa: SLF001 — the seam under test
    return store, pool


def test_unset_camera_follows_the_appliance_default():
    store, _ = make_store(retention_days=30)
    assert store.retention_for("cam-a") == timedelta(days=30)


def test_camera_retention_overrides_the_default_for_that_camera_only():
    store, _ = make_store(retention_days=30)
    store.set_camera_retention("cam-a", 2)
    assert store.retention_for("cam-a") == timedelta(days=2)
    assert store.retention_for("cam-b") == timedelta(days=30)


def test_shortening_retention_restamps_the_rows_already_indexed():
    """The whole point. Stamping new rows alone leaves a month of crops behind
    on a camera just dropped to two days."""
    store, pool = make_store(rowcount=7)
    out = store.set_camera_retention("cam-a", 2)
    assert out["changed"] is True
    assert out["error"] is None
    # Both domain tables restamped, each scoped to this camera.
    updates = [s for s, _ in pool.sql if s.startswith("UPDATE")]
    assert any("search_persons" in s and "sensor_id = %(cam)s" in s for s in updates)
    assert any("search_vehicles" in s and "camera_id = %(cam)s" in s for s in updates)
    # Every registered domain, not just the two that existed first: a camera's
    # shortened retention has to bind everything derived from it.
    for table in ALL_TABLES:
        assert any(table in s for s in updates), f"{table} was never restamped"
    assert out["rows_restamped"] == 7 * len(ALL_TABLES)   # summed across domains
    for _sql, params in pool.sql:
        assert params["cam"] == "cam-a"
        assert params["iv"] == timedelta(days=2)


def test_lengthening_retention_restamps_too():
    """Not one-way: a policy set wrongly and corrected upward has to move the
    existing deadlines as well, or the correction silently applies to new rows
    only."""
    store, pool = make_store(rowcount=3)
    store.set_camera_retention("cam-a", 2)
    pool.sql.clear()
    pool.seed(3)
    out = store.set_camera_retention("cam-a", 90)
    assert out["changed"] is True
    assert out["rows_restamped"] == 3 * len(ALL_TABLES)
    assert all(p["iv"] == timedelta(days=90) for _s, p in pool.sql)


def test_a_large_restamp_is_done_in_batches():
    """Every other bulk mutation in the store is bounded (ERASE_BATCH,
    EXPIRE_BATCH) so it cannot hold one transaction — and the row locks under it
    — open across millions of rows while ingest for that camera queues behind.
    A retention change is rare and never urgent; it has no claim on the write
    path that the erase and expiry sweeps do not have."""
    store, pool = make_store()
    per_table = Store.RESTAMP_BATCH * 2 + 17
    pool.seed(per_table)
    out = store.set_camera_retention("cam-a", 2)
    assert out["rows_restamped"] == per_table * len(ALL_TABLES)
    updates = [s for s, _ in pool.sql if s.startswith("UPDATE")]
    assert all(f"LIMIT {Store.RESTAMP_BATCH}" in s for s in updates)
    # Per table: three batches to drain, plus the empty one that ends the loop.
    # Counted from the registry so that adding a domain cannot leave a table
    # unrestamped while this still reads as green.
    assert len(updates) == 4 * len(ALL_TABLES)


def test_reasserting_the_same_retention_touches_nothing():
    """Every reconcile pass re-asserts every camera. If this were not a no-op it
    would be an UPDATE over the entire index once per sync interval, from each
    of four workers."""
    store, pool = make_store(rowcount=5)
    store.set_camera_retention("cam-a", 2)
    pool.sql.clear()
    out = store.set_camera_retention("cam-a", 2)
    assert out["changed"] is False
    assert out["rows_restamped"] == 0
    assert pool.sql == []


def test_clearing_retention_returns_the_camera_to_the_default_and_restamps():
    store, pool = make_store(rowcount=4, retention_days=30)
    store.set_camera_retention("cam-a", 2)
    pool.sql.clear()
    out = store.set_camera_retention("cam-a", None)
    assert store.retention_for("cam-a") == timedelta(days=30)
    assert out["changed"] is True
    assert all(p["iv"] == timedelta(days=30) for _s, p in pool.sql)


def test_zero_days_is_treated_as_unset_not_as_delete_everything():
    """0 is the registry's "reset to appliance default" sentinel (see the
    retention_days handling in routers/cameras.py). Reading it as a literal zero
    day would expire every crop from that camera on the next sweep."""
    store, _ = make_store(retention_days=30)
    store.set_camera_retention("cam-a", 0)
    assert store.retention_for("cam-a") == timedelta(days=30)


def test_a_database_failure_is_reported_not_raised():
    """This runs inside camera registration. A camera that cannot be registered
    because the search database blinked is worse than a stale deadline, and the
    next reconcile pass re-asserts it anyway."""
    store, _ = make_store(raises=True)
    out = store.set_camera_retention("cam-a", 2)
    assert out["error"] is not None
    assert out["rows_restamped"] == 0
    # Adopted in memory regardless, so new rows get the right stamp even while
    # the restamp of old ones is still failing.
    assert store.retention_for("cam-a") == timedelta(days=2)


def test_write_batch_stamps_expiry_from_the_camera_not_the_default():
    store, pool = make_store(retention_days=30)
    store.set_camera_retention("cam-a", 2)
    pool.sql.clear()
    ts = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    store.write_batch("person", [{
        "embedding": [0.0], "camera": "cam-a", "ts": ts,
        "crop_path": "/tmp/a.jpg",
    }])
    (_sql, params), = [r for r in pool.sql if r[0].startswith("INSERT")]
    expires_at = params[0][6]
    assert expires_at == ts + timedelta(days=2)


def test_write_batch_falls_back_to_the_default_for_an_unknown_camera():
    store, pool = make_store(retention_days=30)
    ts = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    store.write_batch("person", [{
        "embedding": [0.0], "camera": "cam-unknown", "ts": ts,
        "crop_path": "/tmp/a.jpg",
    }])
    (_sql, params), = [r for r in pool.sql if r[0].startswith("INSERT")]
    assert params[0][6] == ts + timedelta(days=30)
