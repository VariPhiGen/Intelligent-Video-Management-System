"""Whole frames for the Recent-detections feed (migrations/005).

The feed shows the scene with the object's box drawn over it, so a row may now
point at a second file beside its crop. What is pinned here is everything that
file's life depends on:

  * ONE FILE PER FRAME. Several objects found in one frame are several rows and
    one picture.
  * NOTHING FOR A DEDUPLICATED OBSERVATION. It has no row, so its frame would be
    a picture of a person that nothing points at.
  * EVERY DELETION PATH TAKES IT. Expiry, erasure and the orphan sweep all knew
    about crops; a frame they did not know about would outlive an erasure, or
    be swept as an orphan every day.
  * FRAMES AGE OUT BEFORE ROWS, by day directory.

The pool is faked, as in test_camera_retention: what regresses is which files
are touched, not the SQL.

Run: python3 -m pytest tests -q
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.config import AppConfig                               # noqa: E402
from index.frame_boxes import clean                              # noqa: E402
from index.pipeline import IngestPipeline, ObservationInput      # noqa: E402
from index.retention import RetentionThread                      # noqa: E402
from index.store import Store                                    # noqa: E402
from index.writer import CropWriter                              # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 20 + b"\xff\xd9"
TS = 1_757_600_000.25


def touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ── writing ──────────────────────────────────────────────────────────────────
def test_a_frame_is_written_under_frames_by_camera_and_day(tmp_path):
    w = CropWriter(str(tmp_path))
    path = w.save_frame("cam-a", TS, JPEG)
    utc_day = datetime.fromtimestamp(TS, tz=timezone.utc).strftime("%Y-%m-%d")
    assert path == str(tmp_path / "frames" / "cam-a" / utc_day / f"{round(TS * 1000)}.jpg")
    assert Path(path).read_bytes() == JPEG, "the bytes were re-encoded or altered"


def test_two_objects_in_one_frame_share_one_file(tmp_path):
    w = CropWriter(str(tmp_path))
    a = w.save_frame("cam-a", TS, JPEG)
    b = w.save_frame("cam-a", TS, b"\xff\xd8 a different encode")
    assert a == b
    assert Path(a).read_bytes() == JPEG, "the second object rewrote the frame"
    assert [p.name for p in (tmp_path / "frames").rglob("*") if p.is_file()] == [Path(a).name], \
        "a temporary file was left behind"


def test_a_slug_that_could_leave_the_frames_directory_is_refused(tmp_path):
    w = CropWriter(str(tmp_path))
    for slug in ("", ".", "..", "a/b", "a\\b"):
        assert w.save_frame(slug, TS, JPEG) is None, slug
    assert not list(tmp_path.rglob("*.jpg"))


# ── ingest ───────────────────────────────────────────────────────────────────
class FakeEmbedder:
    """Hands back the vectors it was given, in order, one per crop."""

    def __init__(self, vectors) -> None:
        self._v = [np.asarray(v, dtype=np.float32) / np.linalg.norm(v) for v in vectors]

    def embed_images(self, crops):
        out, self._v = self._v[:len(crops)], self._v[len(crops):]
        return out


class RowStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write_batch(self, domain, rows):
        self.rows.extend(rows)
        return len(rows)


def obs(ts: float = TS, frame: bytes | None = JPEG,
        boxes: list | None = None) -> ObservationInput:
    return ObservationInput(
        slug="cam-a", crop=Image.new("RGB", (20, 40), (128, 128, 128)), ts=ts,
        domain="person", confidence=0.9, label="person", plate=None,
        plate_confidence=None, bbox=(0.1, 0.1, 0.2, 0.4),
        tracker_id="cam-a:n.0:1", frame_jpeg=frame, frame_boxes=boxes)


def pipeline(tmp_path, vectors):
    store = RowStore()
    return IngestPipeline(AppConfig(), FakeEmbedder(vectors), store,
                          CropWriter(str(tmp_path))), store


def test_every_row_from_one_frame_points_at_the_same_file(tmp_path):
    p, store = pipeline(tmp_path, [[1, 0, 0], [0, 1, 0]])
    assert p.ingest_observations([obs(), obs()]) == 2
    a, b = store.rows
    assert a["frame_path"] and a["frame_path"] == b["frame_path"]
    assert len(list((tmp_path / "frames").rglob("*.jpg"))) == 1


def test_a_deduplicated_observation_leaves_no_frame(tmp_path):
    p, store = pipeline(tmp_path, [[1, 0, 0], [1, 0, 0]])
    p.ingest_observations([obs(ts=TS)])
    p.ingest_observations([obs(ts=TS + 2)])           # same look, inside the window
    assert len(store.rows) == 1
    names = [f.name for f in (tmp_path / "frames").rglob("*.jpg")]
    assert names == [f"{round(TS * 1000)}.jpg"], "a frame was kept for a dropped row"


def test_an_observation_without_a_frame_is_indexed_as_before(tmp_path):
    """An older producer sends none. The row is written; it just has no frame."""
    p, store = pipeline(tmp_path, [[1, 0, 0]])
    assert p.ingest_observations([obs(frame=None)]) == 1
    assert store.rows[0]["frame_path"] is None
    assert store.rows[0]["crop_path"]


# ── deletion ─────────────────────────────────────────────────────────────────
class Cur:
    """DELETE hands back every row of its table once; SELECT lists them."""

    def __init__(self, pool) -> None:
        self._pool, self._rows = pool, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        table = "search_persons" if "search_persons" in flat else "search_vehicles"
        if flat.startswith("DELETE"):
            self._rows, self._pool.tables[table] = self._pool.tables[table], []
        else:
            self._rows = list(self._pool.tables[table])

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class Conn:
    def __init__(self, pool) -> None:
        self._pool = pool

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, **_kw):
        return Cur(self._pool)


class Pool:
    def __init__(self, persons=(), vehicles=()) -> None:
        self.tables = {"search_persons": list(persons), "search_vehicles": list(vehicles)}

    def connection(self):
        return Conn(self)


def store_over(pool) -> Store:
    s = Store("postgresql://unused")
    s._get_pool = lambda: pool                  # noqa: SLF001 — the seam under test
    return s


def test_expiry_takes_the_frame_with_the_row(tmp_path):
    crop = touch(tmp_path / "person" / "cam-a" / "d" / "c.jpg")
    frame = touch(tmp_path / "frames" / "cam-a" / "d" / "1.jpg")
    out = store_over(Pool(persons=[(str(crop), str(frame))])).expire_once()
    assert out["error"] is None
    assert out["rows_deleted"] == 1 and out["frames_unlinked"] == 1
    assert not crop.exists() and not frame.exists()


def test_a_row_from_before_frames_expires_as_it_always_did(tmp_path):
    crop = touch(tmp_path / "c.jpg")
    out = store_over(Pool(vehicles=[(str(crop), None)])).expire_once()
    assert out["error"] is None
    assert out["rows_deleted"] == 1 and out["frames_unlinked"] == 0


def test_erasure_takes_the_whole_scene_too(tmp_path):
    """The frame shows the person being erased, and anyone beside them. Leaving
    it would destroy the crop of a person while keeping a picture of them —
    and report the erasure complete."""
    shared = touch(tmp_path / "frames" / "cam-a" / "d" / "1.jpg")
    c1 = touch(tmp_path / "c1.jpg")
    c2 = touch(tmp_path / "c2.jpg")
    store = store_over(Pool(persons=[(str(c1), str(shared)), (str(c2), str(shared))]))
    now = datetime.now(timezone.utc)
    out = store.erase_range("cam-a", now - timedelta(hours=1), now)
    assert out["persons_deleted"] == 2
    # Shared by both rows: the second unlink finds it gone, which is the goal
    # rather than a failure.
    assert out["frames_unlinked"] == 1 and out["frames_failed"] == 0
    assert not shared.exists()


def test_the_orphan_sweep_keeps_frames_that_rows_point_at(tmp_path):
    """Frames live under the crop root. If the sweep only knew crop_path, it
    would delete every frame older than its grace period, once a day."""
    crop = touch(tmp_path / "person" / "cam-a" / "d" / "c.jpg")
    frame = touch(tmp_path / "frames" / "cam-a" / "d" / "1.jpg")
    orphan = touch(tmp_path / "frames" / "cam-a" / "d" / "2.jpg")
    old = time.time() - 7200
    for f in (crop, frame, orphan):
        os.utime(f, (old, old))
    out = store_over(Pool(persons=[(str(crop), str(frame))])).sweep_orphans(tmp_path)
    assert out["error"] is None
    assert crop.exists() and frame.exists(), "a referenced frame was swept"
    assert not orphan.exists() and out["orphans_removed"] == 1


# ── ageing out ───────────────────────────────────────────────────────────────
def test_frames_older_than_the_window_go_by_day(tmp_path):
    root = tmp_path / "frames"
    old = touch(root / "cam-a" / day(9) / "1.jpg")
    touch(root / "cam-a" / day(9) / "2.jpg.part")          # a crashed write
    recent = touch(root / "cam-a" / day(1) / "3.jpg")
    today = touch(root / "cam-b" / day(0) / "4.jpg")
    out = Store("postgresql://unused").expire_frames(root, 7)
    assert out == {"frame_days_removed": 1, "frames_removed": 2, "error": None}
    assert not old.parent.exists()
    assert recent.exists() and today.exists()


def test_a_directory_that_is_not_a_day_is_left_alone(tmp_path):
    stray = touch(tmp_path / "frames" / "cam-a" / "notes" / "x.jpg")
    Store("postgresql://unused").expire_frames(tmp_path / "frames", 7)
    assert stray.exists()


def test_zero_keeps_frames_as_long_as_their_rows(tmp_path):
    old = touch(tmp_path / "frames" / "cam-a" / day(90) / "1.jpg")
    Store("postgresql://unused").expire_frames(tmp_path / "frames", 0)
    assert old.exists()


def test_no_frames_directory_yet_is_not_an_error(tmp_path):
    out = Store("postgresql://unused").expire_frames(tmp_path / "frames", 7)
    assert out["error"] is None and out["frame_days_removed"] == 0


class FrameStore:
    def __init__(self) -> None:
        self.calls: list = []

    def expire_once(self):
        return {"rows_deleted": 0, "crops_unlinked": 0, "crops_failed": 0, "error": None}

    def expire_frames(self, root, days):
        self.calls.append((root, days))
        return {"frame_days_removed": 0, "frames_removed": 0, "error": None}


def test_every_retention_pass_ages_out_frames(tmp_path):
    s = FrameStore()
    r = RetentionThread(s, interval_seconds=10, frames_root=str(tmp_path),
                        frame_retention_days=7)
    r.sweep_now()
    r.sweep_now()
    assert s.calls == [(str(tmp_path), 7)] * 2
    snap = r.snapshot()
    assert snap["frame_retention_days"] == 7
    assert "frames" in snap["last"]


def test_frame_retention_zero_never_touches_frames(tmp_path):
    s = FrameStore()
    r = RetentionThread(s, interval_seconds=10, frames_root=str(tmp_path),
                        frame_retention_days=0)
    r.sweep_now()
    assert s.calls == []
    assert r.snapshot()["frame_retention_days"] is None


def test_the_shipped_config_keeps_frames_seven_days(monkeypatch):
    # As deployed: config.py refuses any other frame source, since detection
    # moved to the analytics service.
    monkeypatch.setenv("SEARCH_FRAME_SOURCE", "none")
    monkeypatch.delenv("SEARCH_FRAME_RETENTION_DAYS", raising=False)
    here = Path(__file__).resolve().parents[1]
    assert AppConfig.from_yaml(str(here / "config.yaml")).store.frame_retention_days == 7


# ── the frame's box list ────────────────────────────────────────────────────
def box(**kw) -> dict:
    b = {"bbox": [0.1, 0.2, 0.3, 0.4], "label": "person", "domain": "person",
         "tracker_id": "cam-a:n.0:1", "confidence": 0.9, "recorded": True}
    b.update(kw)
    return b


def test_a_box_list_arrives_intact():
    assert clean([box()]) == [box()]


def test_coordinates_are_clamped_to_the_picture():
    """A detector box can run a pixel past the edge. Clamped, not dropped: the
    object is real, it is just touching the border of the frame."""
    assert clean([box(bbox=[-0.2, 0.0, 0.5, 1.4])])[0]["bbox"] == [0.0, 0.0, 0.5, 1.0]


def test_anything_that_would_not_draw_is_dropped():
    assert clean([box(bbox=[0.5, 0.5, 0.2, 0.9])]) is None       # inverted
    assert clean([box(bbox=[0.1, 0.1, 0.1, 0.4])]) is None       # no width
    assert clean([box(bbox=[0.1, 0.1, 0.2])]) is None            # short
    assert clean([box(bbox=[0.1, None, 0.2, 0.4])]) is None      # not numbers
    assert clean([box(bbox="nonsense")]) is None
    assert clean(["not a box"]) is None
    assert clean(None) is None
    assert clean([]) is None


def test_an_unknown_domain_reads_as_a_vehicle():
    """Colour only: the feed has two, and the producer does not choose them."""
    assert clean([box(domain="aircraft")])[0]["domain"] == "vehicles"


def test_the_list_and_its_text_are_bounded():
    """It is stored on every row of a frame and drawn in a browser, so its size
    is this service's guarantee rather than the producer's."""
    out = clean([box(label="x" * 200, tracker_id="y" * 500)])
    assert len(out[0]["label"]) == 40 and len(out[0]["tracker_id"]) == 128
    assert len(clean([box()] * 100)) == 40


def test_every_row_of_a_frame_carries_the_whole_frames_boxes(tmp_path):
    """So a card survives its siblings: when dedup drops the row for one of two
    people, the surviving row still knows both were there."""
    p, store = pipeline(tmp_path, [[1, 0, 0], [0, 1, 0]])
    boxes = [box(), box(tracker_id="cam-a:n.0:2", recorded=False)]
    assert p.ingest_observations([obs(boxes=boxes), obs(boxes=boxes)]) == 2
    assert [r["frame_boxes"] for r in store.rows] == [boxes, boxes]


def test_a_row_without_a_frame_carries_no_boxes(tmp_path):
    p, store = pipeline(tmp_path, [[1, 0, 0]])
    p.ingest_observations([obs(frame=None, boxes=[box()])])
    assert store.rows[0]["frame_boxes"] is None
