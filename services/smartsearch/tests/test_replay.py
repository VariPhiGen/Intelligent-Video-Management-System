"""The capacity harness's replay source: deterministic, paced, read-only.

WHAT THESE PROTECT. A capacity number is only worth having if the workload that
produced it can be reproduced, so the properties under test are the ones that
make a run repeatable: the same segments chosen every time, a fixed sample rate,
and a decode that runs at the source frame rate rather than as fast as the disk
allows. The last one is not a detail — the production sampler self-paces against
RTSP because grab() blocks, and against a FILE it does not, so an unpaced replay
would measure the decoder's top speed and call it a camera.

Run: python3 -m pytest tests -q   (from services/smartsearch)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

from replay import (BUSY, QUIET, ReplaySampler, Segment,  # noqa: E402
                    build_cameras, select_segments)


def _index(tmp_path, rows) -> str:
    """A stand-in NVR segment index. Same schema the recorder writes."""
    db = str(tmp_path / "segments.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE segments (camera TEXT, filepath TEXT, "
                "start_epoch REAL, duration REAL, file_size INTEGER)")
    con.executemany("INSERT INTO segments VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def _epoch_at_local_hour(hour: int, tz_offset_hours: float = 5.5) -> float:
    # 2026-09-04T00:00:00Z plus the wanted LOCAL hour, minus the offset.
    return 1788480000.0 + (hour - tz_offset_hours) * 3600


# ── choosing footage ─────────────────────────────────────────────────────────
def test_selection_is_deterministic(tmp_path):
    """Two runs with the same arguments must replay the same files, or the
    capacity numbers from different steps are not comparable."""
    files = []
    rows = []
    for cam in ("camA", "camB"):
        for i in range(6):
            f = tmp_path / f"{cam}_{i}.ts"
            f.write_bytes(b"x" * 3_000_000)
            files.append(f)
            rows.append((cam, str(f), _epoch_at_local_hour(15) + i * 60,
                         60.0, 3_000_000))
    db = _index(tmp_path, rows)
    a = select_segments(BUSY, 6, hours=(15, 16), db=db)
    b = select_segments(BUSY, 6, hours=(15, 16), db=db)
    assert [x.path for x in a] == [x.path for x in b]
    assert len(a) == 6


def test_selection_spreads_across_cameras(tmp_path):
    """Decode cost depends on resolution and bitrate, which differ per camera
    (0.6 to 4.3 Mbps measured here). Four copies of one scene would not be a
    four-camera workload."""
    rows = []
    for cam in ("camA", "camB", "camC"):
        for i in range(4):
            f = tmp_path / f"{cam}_{i}.ts"
            f.write_bytes(b"x" * 3_000_000)
            rows.append((cam, str(f), _epoch_at_local_hour(15) + i * 60,
                         60.0, 3_000_000))
    got = select_segments(BUSY, 3, hours=(15, 16), db=_index(tmp_path, rows))
    assert {s.camera for s in got} == {"camA", "camB", "camC"}


def test_truncated_segments_are_skipped(tmp_path):
    """A tiny segment is one the recorder was starting or stopping on.
    Replaying it would loop every couple of seconds and distort the load."""
    small = tmp_path / "camA_small.ts"
    small.write_bytes(b"x" * 1000)
    big = tmp_path / "camA_big.ts"
    big.write_bytes(b"x" * 3_000_000)
    rows = [("camA", str(small), _epoch_at_local_hour(15), 60.0, 1000),
            ("camA", str(big), _epoch_at_local_hour(15) + 60, 60.0, 3_000_000)]
    got = select_segments(BUSY, 5, hours=(15, 16), db=_index(tmp_path, rows))
    assert [s.path for s in got] == [str(big)]


def test_a_missing_file_is_skipped(tmp_path):
    """The index outlives the files retention deletes."""
    rows = [("camA", str(tmp_path / "gone.ts"), _epoch_at_local_hour(15),
             60.0, 3_000_000)]
    assert select_segments(BUSY, 5, hours=(15, 16),
                           db=_index(tmp_path, rows)) == []


def test_the_hour_window_selects_the_workload(tmp_path):
    """Busy and quiet are different clock windows over the same recordings —
    a property of the footage, not of what the detector once found in it."""
    rows = []
    for hour in (2, 15):
        f = tmp_path / f"h{hour}.ts"
        f.write_bytes(b"x" * 3_000_000)
        rows.append((f"cam{hour}", str(f), _epoch_at_local_hour(hour),
                     60.0, 3_000_000))
    db = _index(tmp_path, rows)
    assert [s.camera for s in select_segments(BUSY, 5, hours=(15, 16), db=db)] \
        == ["cam15"]
    assert [s.camera for s in select_segments(QUIET, 5, hours=(1, 5), db=db)] \
        == ["cam2"]


# ── scaling to more cameras than segments ────────────────────────────────────
def test_cameras_repeat_segments_when_asked_for_more(tmp_path):
    """Forty logical cameras on a five-camera site. Each still runs its own
    decoder and motion gate, so the per-camera cost is real; only the pixels
    are shared, and the report has to say so."""
    f = tmp_path / "camA_0.ts"
    f.write_bytes(b"x" * 3_000_000)
    rows = [("camA", str(f), _epoch_at_local_hour(15), 60.0, 3_000_000)]
    cams, segs = build_cameras(BUSY, 8, 1.0, on_frame=lambda *a: None,
                               hours=(15, 16), db=_index(tmp_path, rows))
    assert len(cams) == 8
    assert len(segs) == 1
    assert len({c.slug for c in cams}) == 8, "logical cameras must be distinct"
    assert all(c.segment.path == str(f) for c in cams)


def test_no_segments_yields_no_cameras(tmp_path):
    """Reported as INVALID by the caller rather than as a capacity limit."""
    cams, segs = build_cameras(BUSY, 4, 1.0, on_frame=lambda *a: None,
                               hours=(15, 16), db=_index(tmp_path, []))
    assert cams == [] and segs == []


# ── pacing ───────────────────────────────────────────────────────────────────
class _FakeCap:
    """A file that never ends, so pacing can be measured without real video."""
    def __init__(self, *_a, **_kw):
        self.grabs = 0

    def isOpened(self):
        return True

    def grab(self):
        self.grabs += 1
        return True

    def retrieve(self):
        import numpy as np
        return True, np.zeros((4, 4, 3), dtype=np.uint8)

    def release(self):
        pass


def test_replay_is_paced_to_the_source_frame_rate(monkeypatch):
    """THE REASON THIS CLASS EXISTS. CameraSampler self-paces against RTSP
    because grab() blocks for the next packet; against a file it returns
    instantly, so an unpaced loop consumes a 60 s segment in seconds and
    measures the decoder's top speed instead of a camera."""
    import cv2
    cap = _FakeCap()
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **kw: cap)

    seg = Segment("camA", "/tmp/x.ts", 0.0, 60.0, 3_000_000)
    s = ReplaySampler("r0", seg, sample_fps=1.0, on_frame=lambda *a: None,
                      source_fps=50.0)
    s.start()
    time.sleep(1.0)
    s.stop(join_timeout=3)

    # 50 fps for ~1 s. Generous bounds: this asserts that pacing HAPPENS, not
    # that the scheduler is precise.
    assert 10 <= cap.grabs <= 140, f"not paced: {cap.grabs} grabs in 1s"


def test_only_one_frame_per_interval_is_decoded(monkeypatch):
    """grab() is cheap and retrieve() is where the work is — the production
    split. A replay that decoded every frame would overstate the load."""
    import cv2
    cap = _FakeCap()
    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **kw: cap)

    got: list = []
    seg = Segment("camA", "/tmp/x.ts", 0.0, 60.0, 3_000_000)
    s = ReplaySampler("r0", seg, sample_fps=2.0,
                      on_frame=lambda slug, f, ts: got.append(ts),
                      source_fps=50.0)
    s.start()
    time.sleep(1.2)
    s.stop(join_timeout=3)

    assert 1 <= len(got) <= 5, f"expected ~2 samples in 1.2s, got {len(got)}"
    assert cap.grabs > len(got) * 3, "should grab far more than it retrieves"


def test_a_sampler_that_cannot_open_reports_rather_than_spins(monkeypatch):
    import cv2

    class _Dead(_FakeCap):
        def isOpened(self):
            return False

    monkeypatch.setattr(cv2, "VideoCapture", lambda *a, **kw: _Dead())
    seg = Segment("camA", "/tmp/missing.ts", 0.0, 60.0, 3_000_000)
    s = ReplaySampler("r0", seg, 1.0, on_frame=lambda *a: None)
    s.start()
    time.sleep(0.3)
    s.stop(join_timeout=3)
    assert s.state == "FAILED"
    assert s.last_error and "cannot open" in s.last_error


def test_snapshot_names_the_source_footage(tmp_path):
    """Which recordings produced a number is part of the number."""
    seg = Segment("camA", "/data/nvr/camA/camA_20260904_150020.ts",
                  1788500000.0, 60.0, 28_000_000)
    s = ReplaySampler("r0", seg, 1.0, on_frame=lambda *a: None)
    snap = s.snapshot()
    assert snap["source"] == "camA_20260904_150020.ts"
    assert seg.to_dict()["camera"] == "camA"
    assert seg.to_dict()["size_mb"] == 28.0
