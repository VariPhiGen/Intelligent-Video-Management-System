"""The index must not outlive the footage it describes (index/coverage.py).

Age-based expiry cannot see this drift. The recorder and the indexer are
separate services that restart independently, so a skew, a camera re-added under
a new slug, or an NVR erasure that did not reach here leaves detections standing
over dead air — a hit in Smart Search or on the AI Analytics dashboard whose
playback 404s, which reads to an operator exactly like a bug.

This runs UNATTENDED on the retention timer, which is what most of these tests
are really about. A destructive job on a timer has to be wrong in the safe
direction:

  * unknown is not absent — an unreachable recorder must skip, never erase;
  * a plausible-but-wrong answer (rebuilt segments.db) must trip the brake;
  * one camera's refusal must not become a half-applied plan.

The NVR is faked at the HTTP seam and the store at the query seam. What is
pinned is which windows are chosen and when the sweep refuses to act.

Run: python3 -m pytest tests -q
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index import coverage as cov  # noqa: E402

HOUR = 3600.0
T0 = 1_788_000_000.0          # arbitrary fixed epoch; no clock reads in tests
#: Far enough past the fixtures that the recency grace never clips them, except
#: where a test is specifically about the grace.
NOW = T0 + 30 * 24 * HOUR


class FakeStore:
    """Answers the three queries coverage.py asks, and records erasures."""

    def __init__(self, spans, rows_in_window=None):
        self._spans = spans                      # cam -> (lo, hi, n)
        self._rows = rows_in_window              # callable(cam, start, end) -> int
        self.erased: list[tuple] = []

    def fetch(self, sql, params=None):
        params = params or {}
        if "min(ts)" in sql:
            col = "sensor_id" if "search_persons" in sql else "camera_id"
            if "search_vehicles" in sql:
                return []                        # persons-only, keeps sums simple
            out = []
            for cam, (lo, hi, n) in self._spans.items():
                if params.get("camera") in (None, cam):
                    out.append({"cam": cam, "lo": lo, "hi": hi, "n": n})
            assert col                            # silence lint; shape asserted above
            return out
        if "count(*)" in sql:
            if "search_vehicles" in sql:
                return [{"n": 0}]
            start = params["s"].timestamp()
            end = params["e"].timestamp()
            n = self._rows(params["c"], start, end) if self._rows else 0
            return [{"n": n}]
        raise AssertionError(f"unexpected query: {sql}")

    def erase_range(self, camera, start, end):
        n = self._rows(camera, start.timestamp(), end.timestamp()) if self._rows else 0
        self.erased.append((camera, start.timestamp(), end.timestamp()))
        return {"persons_deleted": n, "vehicles_deleted": 0,
                "crops_unlinked": n, "crops_failed": 0}


def fake_nvr(monkeypatch, answer):
    """`answer` is a dict, or None to mean the recorder could not be asked."""
    monkeypatch.setattr(cov, "coverage_for",
                        lambda url, camera, lo, hi, timeout=30.0: answer)


# ── choosing windows ─────────────────────────────────────────────────────────

def test_a_gap_inside_the_recorded_range_is_unrecorded():
    windows = list(cov.unrecorded_windows(
        {"earliest": T0, "latest": T0 + 4 * HOUR,
         "gaps": [{"start": T0 + HOUR, "end": T0 + 2 * HOUR}]},
        T0, T0 + 4 * HOUR))
    assert [w[2] for w in windows] == ["recording gap"]
    # Trimmed by the edge tolerance on both sides, never widened.
    assert windows[0][0] == T0 + HOUR + cov.EDGE_TOLERANCE_SECONDS
    assert windows[0][1] == T0 + 2 * HOUR - cov.EDGE_TOLERANCE_SECONDS


def test_rows_before_the_first_segment_are_unrecorded():
    """/coverage reports these as territory rather than as a gap, so they have
    to be derived from `earliest`. This is the camera re-added under a new slug,
    whose old footage is gone — the case a gaps-only reader misses entirely."""
    windows = list(cov.unrecorded_windows(
        {"earliest": T0 + 2 * HOUR, "latest": T0 + 4 * HOUR, "gaps": []},
        T0, T0 + 4 * HOUR))
    assert [w[2] for w in windows] == ["before earliest segment"]
    assert windows[0][0] == T0


def test_rows_after_the_last_segment_are_unrecorded():
    """The indexer outliving the recorder: detection kept writing after
    recording stopped."""
    windows = list(cov.unrecorded_windows(
        {"earliest": T0, "latest": T0 + 2 * HOUR, "gaps": []},
        T0, T0 + 4 * HOUR))
    assert [w[2] for w in windows] == ["after latest segment"]
    assert windows[0][1] == T0 + 4 * HOUR


def test_a_camera_with_no_segments_at_all_is_entirely_unrecorded():
    windows = list(cov.unrecorded_windows(
        {"earliest": None, "latest": None, "gaps": []}, T0, T0 + HOUR))
    assert windows == [(T0, T0 + HOUR, "no footage at all")]


def test_a_fully_covered_camera_yields_nothing():
    assert list(cov.unrecorded_windows(
        {"earliest": T0 - HOUR, "latest": T0 + 5 * HOUR, "gaps": []},
        T0, T0 + 4 * HOUR)) == []


def test_overlapping_windows_are_merged_so_rows_are_not_counted_twice():
    """A live camera's trailing gap arrives both as a reported gap and as
    "after latest segment". Erasing twice is harmless; counting twice reports
    more rows than the index holds."""
    merged = cov.merge_windows([(T0, T0 + 2 * HOUR, "recording gap"),
                                (T0 + HOUR, T0 + 3 * HOUR, "after latest segment")])
    assert len(merged) == 1
    assert merged[0][0] == T0 and merged[0][1] == T0 + 3 * HOUR
    assert "recording gap" in merged[0][2] and "after latest" in merged[0][2]


# ── refusing to act ──────────────────────────────────────────────────────────

def test_an_unreachable_recorder_erases_nothing(monkeypatch):
    """THE PROPERTY THAT MATTERS MOST. "Cannot ask" must never be read as "no
    footage exists" — that reading erases a camera whole, unattended."""
    store = FakeStore({"cam-a": (T0, T0 + HOUR, 100)}, lambda *a: 100)
    fake_nvr(monkeypatch, None)
    res = cov.purge_unrecorded(store, "http://nvr", now=NOW)
    assert store.erased == []
    assert res["rows_erased"] == 0
    assert res["skipped"] == {"cam-a": "coverage unknown"}


def test_a_sweep_that_would_take_most_of_a_camera_refuses(monkeypatch):
    """A rebuilt or truncated segments.db answers successfully and reports
    almost nothing recorded. That is indistinguishable from genuinely lost
    footage, and only one of the two is recoverable."""
    store = FakeStore({"cam-a": (T0, T0 + 4 * HOUR, 100)}, lambda *a: 90)
    fake_nvr(monkeypatch, {"earliest": T0 + 4 * HOUR, "latest": T0 + 5 * HOUR,
                           "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", now=NOW)
    assert store.erased == []
    assert "refusing" in res["skipped"]["cam-a"]


def test_force_overrides_the_brake(monkeypatch):
    """The operator who has looked at the recorder and means it. Deliberately
    unavailable to the unattended sweep, which always passes force=False."""
    store = FakeStore({"cam-a": (T0, T0 + 4 * HOUR, 100)}, lambda *a: 90)
    fake_nvr(monkeypatch, {"earliest": T0 + 4 * HOUR, "latest": T0 + 5 * HOUR,
                           "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", force=True, now=NOW)
    assert res["skipped"] == {}
    assert res["rows_erased"] == 90


def test_a_normal_sweep_under_the_brake_erases(monkeypatch):
    store = FakeStore({"cam-a": (T0, T0 + 4 * HOUR, 100)}, lambda *a: 10)
    fake_nvr(monkeypatch, {"earliest": T0, "latest": T0 + 3 * HOUR, "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", now=NOW)
    assert res["rows_erased"] == 10
    assert len(store.erased) == 1


def test_dry_run_counts_without_erasing(monkeypatch):
    store = FakeStore({"cam-a": (T0, T0 + 4 * HOUR, 100)}, lambda *a: 10)
    fake_nvr(monkeypatch, {"earliest": T0, "latest": T0 + 3 * HOUR, "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", dry_run=True, now=NOW)
    assert res["rows_erased"] == 10 and store.erased == []


def test_one_cameras_refusal_does_not_stop_the_others(monkeypatch):
    """A skip is per camera. A sweep that aborted on the first unknown would
    leave every later camera un-reconciled for a whole day."""
    store = FakeStore({"cam-a": (T0, T0 + 4 * HOUR, 100),
                       "cam-b": (T0, T0 + 4 * HOUR, 100)}, lambda *a: 10)
    calls = {"n": 0}

    def answer(url, camera, lo, hi, timeout=30.0):
        calls["n"] += 1
        return None if camera == "cam-a" else {"earliest": T0,
                                               "latest": T0 + 3 * HOUR, "gaps": []}
    monkeypatch.setattr(cov, "coverage_for", answer)
    res = cov.purge_unrecorded(store, "http://nvr", now=NOW)
    assert res["skipped"] == {"cam-a": "coverage unknown"}
    assert [c[0] for c in store.erased] == ["cam-b"]


def test_a_store_that_raises_is_reported_not_raised(monkeypatch):
    """This runs on the sweep thread. A thread that dies takes the only bound on
    index growth with it and says nothing."""
    class Exploding(FakeStore):
        def fetch(self, sql, params=None):
            raise RuntimeError("database exploded")
    res = cov.purge_unrecorded(Exploding({}), "http://nvr", now=NOW)
    assert res["error"] is not None
    assert res["rows_erased"] == 0


# ── the in-progress segment is not a gap ─────────────────────────────────────

def test_rows_inside_the_recency_grace_are_never_judged(monkeypatch):
    """THE BUG THIS GRACE EXISTS FOR, caught by the first live sweep.

    ffmpeg has not finalised the segment being written, so it is not in
    segments.db and `latest` lags wall-clock by up to one segment duration. The
    NVR suppresses that trailing window from `gaps` deliberately; "after latest
    segment" is derived from `latest` itself, so without the grace an hourly
    sweep deletes the most recent minute of detections on every pass — footage
    that exists and is being recorded right now."""
    now = T0 + 4 * HOUR
    store = FakeStore({"cam-a": (T0, now - 30, 100)}, lambda *a: 10)
    fake_nvr(monkeypatch, {"earliest": T0, "latest": now - 60, "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", now=now)
    assert store.erased == [], "erased footage inside the in-progress segment"
    assert res["rows_erased"] == 0


def test_a_camera_whose_rows_are_all_recent_is_simply_not_judged(monkeypatch):
    """Not a refusal — there is nothing old enough to have an answer about."""
    now = T0 + HOUR
    store = FakeStore({"cam-a": (now - 60, now - 10, 5)}, lambda *a: 5)
    fake_nvr(monkeypatch, {"earliest": None, "latest": None, "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", now=now)
    assert store.erased == [] and res["skipped"] == {}


def test_older_rows_are_still_judged_once_past_the_grace(monkeypatch):
    """The grace delays the decision, it does not cancel it: a genuinely
    unrecorded row is caught on a later pass."""
    now = T0 + 10 * HOUR
    store = FakeStore({"cam-a": (T0, T0 + 2 * HOUR, 100)}, lambda *a: 10)
    fake_nvr(monkeypatch, {"earliest": T0 + 3 * HOUR, "latest": T0 + 4 * HOUR,
                           "gaps": []})
    res = cov.purge_unrecorded(store, "http://nvr", now=now, force=True)
    assert res["rows_erased"] == 10
