"""Bad camera PTS must not turn a minute of video into hours of coverage."""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder.stream_worker import StreamWorker
from storage.index import SegmentIndex


def setup_segment(tmp_path, elapsed=60):
    index = SegmentIndex(tmp_path / "index.db")
    worker = StreamWorker("cam", "rtsp://localhost/cam", tmp_path, index)
    worker.segment_dir.mkdir()
    start = int(time.time()) - elapsed - 10
    stamp = datetime.fromtimestamp(start).strftime("%Y%m%d_%H%M%S")
    path = worker.segment_dir / f"cam_{stamp}.ts"
    path.write_bytes(b"recording preserved")
    os.utime(path, (start + elapsed, start + elapsed))
    return worker, index, path, start


@pytest.mark.parametrize("duration", [43482.823544, float("nan"), float("inf")])
def test_csv_rejects_bad_clock_and_next_segment_can_be_indexed(tmp_path, duration):
    worker, index, path, start = setup_segment(tmp_path)
    worker._index_segment_line(f"{path.name},0,{duration}")
    assert index.find_segments("cam", start, start + 50000) == []
    assert path.read_bytes() == b"recording preserved"
    worker._index_segment_line(f"{path.name},0,60")
    assert index.find_segments("cam", start, start + 50000)[0].duration == 60


def test_reconciliation_cannot_reintroduce_timestamp_jump(tmp_path, monkeypatch):
    worker, index, path, start = setup_segment(tmp_path)
    monkeypatch.setattr(worker, "_probe_duration", lambda _: 43482.823544)
    assert worker.reconcile() == 0
    assert index.find_segments("cam", start, start + 50000) == []
    assert path.exists()


@pytest.mark.parametrize("elapsed,duration", [(60, 60), (60, 121.7), (600, 601)])
def test_normal_long_gop_and_genuinely_long_files_remain_available(
    tmp_path, elapsed, duration, monkeypatch,
):
    worker, index, path, start = setup_segment(tmp_path, elapsed)
    monkeypatch.setattr(worker, "_probe_duration", lambda _: duration)
    assert worker.reconcile() == 1
    assert index.find_segments("cam", start, start + 50000)[0].duration == duration


def test_future_mtime_cannot_legitimize_future_coverage(tmp_path):
    worker, _, path, start = setup_segment(tmp_path)
    os.utime(path, (start + 50000, start + 50000))
    assert not worker._valid_segment_duration(path, start, 43482.823544)
