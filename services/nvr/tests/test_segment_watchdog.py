"""Unit tests for the segment-roll watchdog (recorder/stream_worker.py).

Root cause it guards against: a flaky/corrupt RTSP source keeps bytes flowing
(so ffmpeg's -timeout never fires) but the segment muxer stops advancing PTS,
so ffmpeg pours hours into ONE ever-growing .ts and never writes a new
completed line to segments.csv. The indexer watermark freezes, playback sees
no new footage, yet the worker thread + ffmpeg process are both "alive" — so
neither the liveness restart (engine.py) nor the staleness alarm ever recovers
it. Only a manual VMS restart did, until this watchdog.

The watchdog must fire on that state (open segment old AND still growing) and
must NOT fire on a healthy fresh segment or on a genuinely-offline camera
(open segment old but NOT growing — left to -timeout/backoff).

Run: python -m pytest services/nvr/tests/test_segment_watchdog.py -q
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder.stream_worker import StreamWorker  # noqa: E402

DUR = 60  # segment_duration used across these tests


# ---- _is_wedged: the pure predicate ----------------------------------------

def test_healthy_fresh_segment_is_not_wedged():
    # opened 10s ago, written 1s ago — normal in-progress segment
    assert StreamWorker._is_wedged(open_for=10, since_write=1, segment_duration=DUR) is False


def test_growing_but_not_rolling_is_wedged():
    # open 200s (should have rolled at 60s) and still being written -> the bug
    assert StreamWorker._is_wedged(open_for=200, since_write=2, segment_duration=DUR) is True


def test_old_but_not_growing_is_not_wedged():
    # open 200s but untouched for 120s -> stream stopped, not a wedge.
    # -timeout/backoff owns this; restarting here would thrash an offline cam.
    assert StreamWorker._is_wedged(open_for=200, since_write=120, segment_duration=DUR) is False


def test_roll_threshold_is_exclusive_boundary():
    # exactly 3x duration is NOT yet wedged; just past it is
    assert StreamWorker._is_wedged(open_for=3 * DUR, since_write=1, segment_duration=DUR) is False
    assert StreamWorker._is_wedged(open_for=3 * DUR + 1, since_write=1, segment_duration=DUR) is True


def test_none_inputs_are_not_wedged():
    assert StreamWorker._is_wedged(open_for=None, since_write=None, segment_duration=DUR) is False
    assert StreamWorker._is_wedged(open_for=200, since_write=None, segment_duration=DUR) is False


# ---- newest_segment_stat: reads real files ---------------------------------

def _make_segment(dirpath: Path, start_ago_s: float, mtime_ago_s: float) -> Path:
    """Create a .ts whose filename encodes a start `start_ago_s` in the past
    (local time, matching -strftime) and whose mtime is `mtime_ago_s` ago."""
    now = time.time()
    start_dt = datetime.fromtimestamp(now - start_ago_s)  # local, like ffmpeg
    name = f"cam1_{start_dt.strftime('%Y%m%d_%H%M%S')}.ts"
    p = dirpath / name
    p.write_bytes(b"\x00" * 16)
    os.utime(p, (now - mtime_ago_s, now - mtime_ago_s))
    return p


def _worker(tmp_path) -> StreamWorker:
    return StreamWorker(
        camera_name="cam1", rtsp_url="rtsp://x/y",
        storage_path=tmp_path, index=None, segment_duration=DUR,
    )


def test_newest_segment_stat_empty_dir_returns_none(tmp_path):
    w = _worker(tmp_path)
    (tmp_path / "cam1").mkdir()
    assert w.newest_segment_stat() is None


def test_newest_segment_stat_picks_latest_and_reports_age(tmp_path):
    w = _worker(tmp_path)
    d = tmp_path / "cam1"; d.mkdir()
    _make_segment(d, start_ago_s=300, mtime_ago_s=250)   # older, done
    _make_segment(d, start_ago_s=200, mtime_ago_s=2)     # newest, growing
    stat = w.newest_segment_stat()
    assert stat is not None
    open_for, since_write = stat
    assert 195 <= open_for <= 210      # ~200s open
    assert since_write <= 10           # written seconds ago


# ---- is_segment_stalled: only while ffmpeg is supposed to be running --------

class _FakeProc:
    """Stand-in for a live ffmpeg subprocess (poll() None == running)."""
    def poll(self):
        return None


def test_stalled_true_when_ffmpeg_alive_and_segment_wedged(tmp_path):
    w = _worker(tmp_path)
    d = tmp_path / "cam1"; d.mkdir()
    _make_segment(d, start_ago_s=200, mtime_ago_s=2)
    w._running = True
    w._process = _FakeProc()
    assert w.is_segment_stalled() is True


def test_not_stalled_when_process_absent(tmp_path):
    # in backoff between ffmpeg runs -> _process is None -> never a wedge
    w = _worker(tmp_path)
    d = tmp_path / "cam1"; d.mkdir()
    _make_segment(d, start_ago_s=200, mtime_ago_s=2)
    w._running = True
    w._process = None
    assert w.is_segment_stalled() is False
