"""Unit tests for the retention sweep (index/retention.py).

`retention_days` stamps `expires_at` on every row at insert, and until the
thread existed nothing ever acted on it: no cron entry, no sidecar, no thread —
so the index and crop directory grew without bound on a service whose own plan
named storage as the binding constraint. It had not bitten only because the
index was younger than its own retention window.

What is pinned here is therefore mostly *liveness*: that the thread keeps
sweeping when a sweep fails, that shutdown does not wait an interval, and that
/health can tell "no sweep has run yet" from "a sweep ran and found nothing" —
the two states that look identical if `last` is initialised to zeroes.

The store is faked. Sweeping is one SQL DELETE plus unlink, covered against a
real database elsewhere; the thread's behaviour around it is what regresses.

Run: python3 -m pytest tests -q
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.retention import RetentionThread  # noqa: E402


class FakeStore:
    """Counts sweeps; can be told to fail, either softly or by raising."""

    def __init__(self, raises=False, error=None):
        self.calls = 0
        self.raises = raises
        self.error = error
        self.swept = threading.Event()

    def expire_once(self):
        self.calls += 1
        self.swept.set()
        if self.raises:
            raise RuntimeError("database exploded")
        return {"rows_deleted": 3, "crops_unlinked": 3,
                "crops_failed": 0, "error": self.error}


def test_sweep_runs_on_the_timer_and_records_the_result():
    store = FakeStore()
    r = RetentionThread(store, interval_seconds=0.05, initial_delay_seconds=0.01)
    r.start()
    assert store.swept.wait(timeout=5), "sweep never ran"
    r.stop()
    assert store.calls >= 1
    snap = r.snapshot()
    assert snap["enabled"] is True
    assert snap["sweeps"] >= 1
    assert snap["last"]["rows_deleted"] == 3


def test_a_raising_sweep_does_not_kill_the_thread():
    """The failure that would matter most and say the least.

    A dead sweep thread removes the only bound on index growth, and nothing in
    the product changes — searches still work, /health still says ok. So the
    loop has to survive an exception and try again on the next tick.
    """
    store = FakeStore(raises=True)
    r = RetentionThread(store, interval_seconds=0.02, initial_delay_seconds=0.01)
    r.start()
    deadline = time.time() + 5
    while store.calls < 3 and time.time() < deadline:
        time.sleep(0.01)
    r.stop()
    assert store.calls >= 3, f"thread stopped after {store.calls} sweep(s)"


def test_a_soft_error_is_carried_into_health_not_swallowed():
    """expire_once reports failure in-band; the snapshot must not lose it."""
    store = FakeStore(error="connection refused")
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=0.01)
    r.sweep_now()
    assert r.snapshot()["last"]["error"] == "connection refused"


def test_zero_interval_disables_the_sweep():
    """A supported choice — an operator running the script from cron instead."""
    store = FakeStore()
    r = RetentionThread(store, interval_seconds=0)
    assert r.enabled is False
    r.start()
    time.sleep(0.05)
    assert store.calls == 0
    assert r.snapshot()["enabled"] is False
    assert r.snapshot()["interval_seconds"] is None


def test_health_distinguishes_never_swept_from_swept_nothing():
    """`last: None` vs `last: {rows_deleted: 0}`.

    If `last` were initialised to zeroes, a thread that has never run once would
    be indistinguishable from one that ran and found nothing to delete — and the
    first is the broken state this whole change exists to prevent.
    """
    store = FakeStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99)
    assert r.snapshot()["last"] is None
    assert r.snapshot()["sweeps"] == 0
    r.sweep_now()
    assert r.snapshot()["last"] is not None


def test_stop_does_not_wait_out_the_interval():
    """A container stop must not block for an hour on the sleeping thread.

    This is why the loop uses Event.wait() rather than time.sleep(); with sleep
    the join below would time out and the test would take the full interval.
    """
    store = FakeStore()
    r = RetentionThread(store, interval_seconds=3600, initial_delay_seconds=0.01)
    r.start()
    assert store.swept.wait(timeout=5)
    began = time.time()
    r.stop(timeout=5)
    assert time.time() - began < 2, "stop() waited on the interval"


def test_start_is_idempotent():
    """Two starts must not leave two threads sweeping the same table."""
    store = FakeStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99)
    r.start()
    first = r._thread
    r.start()
    assert r._thread is first
    r.stop()


@pytest.mark.parametrize("interval", [-1, 0])
def test_non_positive_intervals_are_all_disabled(interval):
    assert RetentionThread(FakeStore(), interval_seconds=interval).enabled is False


# ── orphan sweep scheduling ──────────────────────────────────────────────────

class OrphanStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.orphan_calls = 0
        self.orphan_root = None

    def sweep_orphans(self, crop_root, grace_seconds=None):
        self.orphan_calls += 1
        self.orphan_root = crop_root
        return {"orphans_removed": 2, "skipped_recent": 1, "failed": 0, "error": None}


def test_orphans_are_swept_every_nth_pass_not_every_pass():
    """Walking the whole crop tree hourly would be waste; never is growth."""
    store = OrphanStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99,
                        crop_root="/crops", orphan_every_n=3)
    for _ in range(2):
        r.sweep_now()
    assert store.orphan_calls == 0
    r.sweep_now()                       # third pass
    assert store.orphan_calls == 1
    assert store.orphan_root == "/crops"
    assert r.snapshot()["last"]["orphans"]["orphans_removed"] == 2


def test_orphan_sweep_off_without_a_crop_root():
    """The thread is constructible without one; it must then simply not sweep."""
    store = OrphanStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99,
                        orphan_every_n=1)
    assert r.orphan_sweep_enabled is False
    r.sweep_now()
    assert store.orphan_calls == 0
    assert r.snapshot()["orphan_sweep_every_n"] is None


def test_orphan_sweep_can_be_disabled_with_a_crop_root_present():
    store = OrphanStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99,
                        crop_root="/crops", orphan_every_n=0)
    assert r.orphan_sweep_enabled is False
    for _ in range(5):
        r.sweep_now()
    assert store.orphan_calls == 0


def test_expiry_still_reported_on_a_pass_that_also_sweeps_orphans():
    """The orphan result is added to the expiry result, never replaces it."""
    store = OrphanStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99,
                        crop_root="/crops", orphan_every_n=1)
    out = r.sweep_now()
    assert out["rows_deleted"] == 3          # expiry survived
    assert out["orphans"]["orphans_removed"] == 2


# ── coverage sweep scheduling ────────────────────────────────────────────────
#
# The third thing this thread sweeps, and the one that was a script first. Age
# expiry cannot see it: the recorder and the indexer restart independently, so
# detections accumulate over dead air and their playback 404s. Left to a script,
# the drift was corrected only when someone remembered it existed. What the
# behaviour of the sweep ITSELF is lives in test_coverage_sweep.py; this is
# about when the thread calls it, and — more importantly — when it must not.

@pytest.fixture
def coverage_calls(monkeypatch):
    from index import coverage as coverage_mod
    calls: list[dict] = []

    def fake(store, nvr_url, camera=None, dry_run=False, force=False):
        calls.append({"url": nvr_url, "force": force})
        return {"cameras_checked": 1, "rows_erased": 4, "crops_unlinked": 4,
                "crops_failed": 0, "skipped": {}, "plans": [], "error": None}

    monkeypatch.setattr(coverage_mod, "purge_unrecorded", fake)
    return calls


def test_the_first_pass_sweeps_coverage(coverage_calls):
    """A restart is when this drift is created, so waiting for the 24th pass
    guarantees a day of hits whose playback 404s."""
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="http://nvr:8009", coverage_every_n=3)
    r.sweep_now()
    assert len(coverage_calls) == 1
    assert coverage_calls[0]["url"] == "http://nvr:8009"
    assert r.snapshot()["last"]["coverage"]["rows_erased"] == 4


def test_coverage_is_then_swept_every_nth_pass_not_every_pass(coverage_calls):
    """An HTTP round trip per camera, correcting drift that appears at restarts
    rather than continuously. Hourly would be waste; never is a permanent index
    of footage that no longer exists."""
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="http://nvr:8009", coverage_every_n=3)
    for _ in range(3):
        r.sweep_now()
    assert len(coverage_calls) == 1     # the first, then two quiet passes
    r.sweep_now()                       # third pass since the last success
    assert len(coverage_calls) == 2


def test_a_recorder_that_could_not_be_asked_is_retried_next_pass(monkeypatch):
    """A boot where the NVR is not up yet must not stand down for a full cycle.
    "Could not ask" is not "asked and found nothing" — the same distinction
    coverage.py draws to decide whether it may delete at all."""
    from index import coverage as coverage_mod
    calls: list[dict] = []
    state = {"unknown": True}

    def fake(store, nvr_url, camera=None, dry_run=False, force=False):
        calls.append({"url": nvr_url})
        skipped = {"cam-a": "coverage unknown"} if state["unknown"] else {}
        return {"cameras_checked": 1, "rows_erased": 0, "crops_unlinked": 0,
                "crops_failed": 0, "skipped": skipped, "plans": [], "error": None}

    monkeypatch.setattr(coverage_mod, "purge_unrecorded", fake)
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="http://nvr:8009", coverage_every_n=99)
    r.sweep_now()
    r.sweep_now()
    assert len(calls) == 2, "an unreachable recorder should be retried"
    state["unknown"] = False
    r.sweep_now()
    assert len(calls) == 3
    r.sweep_now()                       # now converged; stands down until Nth
    assert len(calls) == 3


def test_the_safety_brake_is_not_retried_hourly(monkeypatch):
    """A refusal by the brake IS a decision that was reached. Repeating it every
    pass would log the same complaint 24 times a day."""
    from index import coverage as coverage_mod
    calls: list[dict] = []

    def fake(store, nvr_url, camera=None, dry_run=False, force=False):
        calls.append({"url": nvr_url})
        return {"cameras_checked": 1, "rows_erased": 0, "crops_unlinked": 0,
                "crops_failed": 0, "plans": [], "error": None,
                "skipped": {"cam-a": "would erase 90/100 rows — refusing"}}

    monkeypatch.setattr(coverage_mod, "purge_unrecorded", fake)
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="http://nvr:8009", coverage_every_n=99)
    for _ in range(4):
        r.sweep_now()
    assert len(calls) == 1


def test_the_unattended_sweep_never_forces(coverage_calls):
    """The safety brake exists FOR this caller. An operator who has looked at
    the recorder and means it runs the script with --force; a timer must not be
    able to make that call on its own."""
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="http://nvr:8009", coverage_every_n=1)
    r.sweep_now()
    assert coverage_calls[0]["force"] is False


def test_coverage_sweep_off_without_an_nvr_url(coverage_calls):
    """Coverage is the only thing that makes deleting by it safe, so "no
    recorder configured" has to mean "do not sweep" — never "nothing is
    recorded", which would erase the whole index."""
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        coverage_every_n=1)
    assert r.coverage_sweep_enabled is False
    r.sweep_now()
    assert coverage_calls == []
    assert r.snapshot()["coverage_sweep_every_n"] is None


def test_an_empty_nvr_url_is_the_same_as_none(coverage_calls):
    """Compose passes the variable through whether or not it is set, so the
    disabled case arrives as "" far more often than as None."""
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="   ", coverage_every_n=1)
    assert r.coverage_sweep_enabled is False
    r.sweep_now()
    assert coverage_calls == []


def test_coverage_sweep_can_be_disabled_with_an_nvr_url_present(coverage_calls):
    r = RetentionThread(FakeStore(), interval_seconds=10, initial_delay_seconds=99,
                        nvr_url="http://nvr:8009", coverage_every_n=0)
    assert r.coverage_sweep_enabled is False
    for _ in range(5):
        r.sweep_now()
    assert coverage_calls == []


def test_expiry_and_orphans_survive_a_pass_that_also_sweeps_coverage(coverage_calls):
    """Three results compose into one pass; none replaces another."""
    store = OrphanStore()
    r = RetentionThread(store, interval_seconds=10, initial_delay_seconds=99,
                        crop_root="/crops", orphan_every_n=1,
                        nvr_url="http://nvr:8009", coverage_every_n=1)
    out = r.sweep_now()
    assert out["rows_deleted"] == 3                     # expiry
    assert out["coverage"]["rows_erased"] == 4          # coverage
    assert out["orphans"]["orphans_removed"] == 2       # orphans
