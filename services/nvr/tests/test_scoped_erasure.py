"""Unit tests for the DSR scoped-erasure selection logic (storage/index.py).

The API handler partitions select_overlapping() rows into fully-inside
(deletable) vs boundary-overlap (preserved) — these tests pin that predicate
and the unlink-then-delete_by_ids bookkeeping it relies on.

Run: python -m pytest services/nvr/tests -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.index import Segment, SegmentIndex  # noqa: E402


def make_index(tmp_path, camera="cam1"):
    idx = SegmentIndex(str(tmp_path / "segments.db"))
    # Three back-to-back 60s segments: [0,60) [60,120) [120,180)
    for start in (0.0, 60.0, 120.0):
        idx.add_segment(Segment(
            camera=camera,
            start_epoch=start,
            duration=60.0,
            filepath=f"/data/nvr/{camera}/{camera}_{int(start)}.ts",
            file_size=1000,
        ))
    return idx


def split_inside(rows, from_epoch, to_epoch):
    """Mirror of the handler's partition in api/server.py."""
    inside = [r for r in rows if r[1] >= from_epoch and (r[1] + r[2]) <= to_epoch]
    partial = [r for r in rows if not (r[1] >= from_epoch and (r[1] + r[2]) <= to_epoch)]
    return inside, partial


def test_overlap_returns_boundary_segments(tmp_path):
    idx = make_index(tmp_path)
    rows = idx.select_overlapping("cam1", 55.0, 125.0)
    assert [r[1] for r in rows] == [0.0, 60.0, 120.0]


def test_only_fully_inside_is_deletable(tmp_path):
    idx = make_index(tmp_path)
    rows = idx.select_overlapping("cam1", 55.0, 125.0)
    inside, partial = split_inside(rows, 55.0, 125.0)
    assert [r[1] for r in inside] == [60.0]        # the middle segment only
    assert [r[1] for r in partial] == [0.0, 120.0]  # boundary overlaps preserved


def test_exact_window_is_inside(tmp_path):
    idx = make_index(tmp_path)
    rows = idx.select_overlapping("cam1", 60.0, 120.0)
    inside, _ = split_inside(rows, 60.0, 120.0)
    assert [r[1] for r in inside] == [60.0]


def test_window_covering_everything(tmp_path):
    idx = make_index(tmp_path)
    rows = idx.select_overlapping("cam1", 0.0, 180.0)
    inside, partial = split_inside(rows, 0.0, 180.0)
    assert len(inside) == 3 and not partial


def test_other_camera_untouched(tmp_path):
    idx = make_index(tmp_path)
    idx.add_segment(Segment("cam2", 60.0, 60.0, "/data/nvr/cam2/x.ts", 1000))
    assert [r[1] for r in idx.select_overlapping("cam1", 0.0, 180.0)] == [0.0, 60.0, 120.0]


def test_delete_by_ids_removes_only_selected(tmp_path):
    idx = make_index(tmp_path)
    rows = idx.select_overlapping("cam1", 55.0, 125.0)
    inside, _ = split_inside(rows, 55.0, 125.0)
    idx.delete_by_ids([r[0] for r in inside])
    remaining = idx.find_segments("cam1", 0.0, 180.0)
    assert sorted(s.start_epoch for s in remaining) == [0.0, 120.0]


def test_empty_window_no_match(tmp_path):
    idx = make_index(tmp_path)
    assert idx.select_overlapping("cam1", 500.0, 600.0) == []
