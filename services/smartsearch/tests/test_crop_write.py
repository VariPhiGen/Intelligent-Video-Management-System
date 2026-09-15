"""The crop write is atomic and durable, and the sweep can see when it wasn't.

WHAT THIS EXISTS FOR. Measured on the appliance 2026-09-14: 80 zero-byte crop
JPEGs written between 2026-09-01 and 09-10, in 15 bursts, every burst the last
thing written before the service stopped. Rows pointed at every one of them —
`save()` had returned a path and the row was committed — so this was not a
failed write being handled badly. Postgres fsyncs its commit and a closed JPEG
does not, so a power cut keeps the row and loses the pixels.

The invariant these tests pin is one sentence: **a path `save()` returns has its
bytes on disk.** Everything else here follows from it — the `.part` file, the
size check, the 404 on an empty crop, and the sweep counting what old damage is
left, since none of it had a symptom for nine days.

Run: python3 -m pytest tests/test_crop_write.py -q
"""
import os
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index.store import Store  # noqa: E402
from index.writer import PART_SUFFIX, CropWriter  # noqa: E402

TS = 1757404800.0          # 2026-09-09T08:00:00Z — inside the measured window


def crop(size=(40, 80)) -> Image.Image:
    return Image.new("RGB", size, (90, 120, 200))


# ── the write itself ────────────────────────────────────────────────────────

def test_returned_path_holds_a_complete_file(tmp_path):
    w = CropWriter(str(tmp_path))
    path = w.save("person", "cam-a", crop(), TS)
    assert path is not None
    p = Path(path)
    assert p.stat().st_size > 0
    assert Image.open(p).size == (40, 80)
    assert w.write_failures == 0


def test_the_final_name_only_appears_after_the_bytes(tmp_path, monkeypatch):
    """Atomicity, stated as the thing that matters rather than as 'os.replace is
    called': at the moment the crop takes its final name, it is already whole.

    A test that asserted `os.replace was called` would pass against a save that
    renamed an empty file, which is the bug."""
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["src_size"] = os.path.getsize(src)
        seen["dst_existed"] = os.path.exists(dst)
        return real_replace(src, dst)

    monkeypatch.setattr("index.writer.os.replace", spy)
    path = CropWriter(str(tmp_path)).save("person", "cam-a", crop(), TS)
    assert seen["dst_existed"] is False, "final name existed before the bytes did"
    assert seen["src_size"] > 0
    assert os.path.getsize(path) == seen["src_size"]


def test_fsync_is_on_by_default_and_can_be_turned_off(tmp_path, monkeypatch):
    """The durability half is a policy, so which way it is set has to be visible
    — a site that turns it off still gets atomicity, and must not silently get
    the fsync it was trying to avoid."""
    calls = []
    monkeypatch.setattr("index.writer.os.fsync", lambda fd: calls.append(fd))

    CropWriter(str(tmp_path)).save("person", "cam-a", crop(), TS)
    assert len(calls) == 1

    CropWriter(str(tmp_path), fsync=False).save("person", "cam-a", crop(), TS)
    assert len(calls) == 1, "fsync ran with fsync=False"


def test_an_encoder_that_writes_nothing_is_a_failure_not_a_path(tmp_path):
    """The exact shape of the production bug, reproduced from the inside: bytes
    never reach the file, nothing raises. The old writer returned the path and
    the caller wrote a row against it."""
    class Hollow(Image.Image):
        def save(self, fp, **kw):           # noqa: D401 — writes nothing, raises nothing
            return None

    w = CropWriter(str(tmp_path))
    assert w.save("person", "cam-a", Hollow(), TS) is None
    assert w.write_failures == 1
    assert list(Path(tmp_path).rglob("*.jpg")) == []


def test_a_write_that_dies_midway_leaves_no_crop_and_no_temp_file(tmp_path):
    """What a kill during encoding must look like afterwards: nothing at all.
    Not a short .jpg, and not a .part left to be counted as disk forever."""
    class Torn(Image.Image):
        def save(self, fp, **kw):
            fp.write(b"\xff\xd8\xff\xe0 half a JPEG")
            raise OSError("no space left on device")

    w = CropWriter(str(tmp_path))
    assert w.save("vehicles", "gate", Torn(), TS) is None
    assert w.write_failures == 1
    assert list(Path(tmp_path).rglob("*.jpg")) == []
    assert list(Path(tmp_path).rglob("*" + PART_SUFFIX)) == []


def test_two_writes_never_collide_and_both_survive(tmp_path):
    """The temp name is derived from the final one, so it has to be as unique.
    A single shared `.part` would make concurrent ingest overwrite itself."""
    w = CropWriter(str(tmp_path))
    a = w.save("person", "cam-a", crop((30, 60)), TS)
    b = w.save("person", "cam-a", crop((50, 90)), TS)
    assert a != b
    assert Image.open(a).size == (30, 60)
    assert Image.open(b).size == (50, 90)


# ── the sweep can see damage that predates the fix ──────────────────────────

class FakeCursor:
    def __init__(self, paths, frames=None):
        self._paths = paths
        self._frames = frames or {}
        self._rows = []

    def execute(self, sql, params=None):
        # Every table is queried; answer persons with everything and the rest
        # with nothing, which is what a person-only corpus looks like. Rows are
        # as wide as the SELECT, as Postgres's would be: person rows carry
        # (crop_path, frame_path) since whole frames landed.
        cols = sql.split("SELECT", 1)[1].split("FROM", 1)[0].count(",") + 1
        self._rows = ([(p, self._frames.get(p))[:cols] for p in self._paths]
                      if "search_persons" in sql else [])

    def __iter__(self):
        return iter(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, paths, frames=None):
        self._paths = paths
        self._frames = frames

    def cursor(self):
        return FakeCursor(self._paths, self._frames)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePool:
    def __init__(self, paths, frames=None):
        self._paths = paths
        self._frames = frames

    def connection(self):
        return FakeConn(self._paths, self._frames)


def store_over(paths, monkeypatch, frames=None):
    """`frames` maps a crop path to the frame_path its row carries."""
    s = Store("postgresql://unused/unused")
    monkeypatch.setattr(s, "_get_pool", lambda: FakePool(paths, frames))
    return s


def aged(p: Path, seconds: int = 7200) -> Path:
    old = time.time() - seconds
    os.utime(p, (old, old))
    return p


def test_sweep_counts_rows_whose_crop_is_empty_or_missing(tmp_path, monkeypatch):
    good = tmp_path / "good.jpg"
    good.write_bytes(b"\xff\xd8\xff")
    empty = tmp_path / "empty.jpg"
    empty.touch()                                  # the 80 files, exactly
    gone = tmp_path / "gone.jpg"                   # referenced, never created

    s = store_over([str(good), str(empty), str(gone)], monkeypatch)
    out = s.sweep_orphans(str(tmp_path))

    assert out["rows_empty_crop"] == 1
    assert out["rows_missing_crop"] == 1
    assert out["error"] is None
    # Reported, NOT repaired: deleting rows here is how an unmounted crop
    # volume would empty the index.
    assert empty.exists()
    assert out["orphans_removed"] == 0, "a referenced file is not an orphan"


def test_sweep_collects_stale_part_files_but_spares_writes_in_flight(tmp_path, monkeypatch):
    stale = tmp_path / ("abc.jpg" + PART_SUFFIX)
    stale.write_bytes(b"half")
    aged(stale)
    fresh = tmp_path / ("def.jpg" + PART_SUFFIX)
    fresh.write_bytes(b"half")

    s = store_over([], monkeypatch)
    out = s.sweep_orphans(str(tmp_path))

    assert out["partials_removed"] == 1
    assert not stale.exists()
    assert fresh.exists(), "swept a write that may still be in progress"


def test_a_part_file_is_never_mistaken_for_a_crop(tmp_path, monkeypatch):
    """The `*.jpg` glob must not reach `<uuid>.jpg.part` — if it did, an
    interrupted write would be counted as an orphaned crop and deleted on the
    spot, which is right by accident, or as a referenced one, which is not."""
    part = tmp_path / ("xyz.jpg" + PART_SUFFIX)
    part.write_bytes(b"half")
    aged(part)

    s = store_over([str(part)], monkeypatch)     # pretend a row points at it
    out = s.sweep_orphans(str(tmp_path))

    assert out["rows_empty_crop"] == 0
    assert out["orphans_removed"] == 0
    assert out["partials_removed"] == 1


def test_sweep_still_removes_real_orphans(tmp_path, monkeypatch):
    """The pre-existing job has to keep working — the counting above is added
    to the same pass, and a pass that stopped reclaiming disk would be a worse
    bug than the one being fixed."""
    orphan = tmp_path / "orphan.jpg"
    orphan.write_bytes(b"\xff\xd8\xff")
    aged(orphan)
    kept = tmp_path / "kept.jpg"
    kept.write_bytes(b"\xff\xd8\xff")

    s = store_over([str(kept)], monkeypatch)
    out = s.sweep_orphans(str(tmp_path))

    assert out["orphans_removed"] == 1
    assert not orphan.exists()
    assert kept.exists()


def test_a_frame_that_aged_out_is_not_a_dangling_crop(tmp_path, monkeypatch):
    """Frames are kept for days and rows for weeks, so a row whose frame_path
    points at nothing is the NORMAL state for most of a row's life. Counting it
    as a missing crop would raise the power-cut warning every day for a feature
    working exactly as designed — which is why the sweep keeps crops and frames
    in separate sets instead of one `referenced`."""
    crop_file = tmp_path / "crop.jpg"
    crop_file.write_bytes(b"\xff\xd8\xff")
    expired = tmp_path / "frames" / "cam-a" / "2026-09-01" / "1756684800000.jpg"

    s = store_over([str(crop_file)], monkeypatch, frames={str(crop_file): str(expired)})
    out = s.sweep_orphans(str(tmp_path))

    assert out["rows_missing_crop"] == 0
    assert out["rows_empty_crop"] == 0
    assert out["error"] is None


# ── frames get the crop's contract ──────────────────────────────────────────

def test_a_frame_is_written_as_durably_as_a_crop(tmp_path, monkeypatch):
    """A row references its frame exactly as it references its crop, so the
    frame gets the same contract: whole before it has its name, fsynced unless
    the site turned that off, and no temp file left behind."""
    calls = []
    monkeypatch.setattr("index.writer.os.fsync", lambda fd: calls.append(fd))

    path = CropWriter(str(tmp_path)).save_frame("cam-a", TS, b"\xff\xd8 frame")
    assert path is not None and Path(path).read_bytes() == b"\xff\xd8 frame"
    assert len(calls) == 1
    assert list(Path(tmp_path).rglob("*" + PART_SUFFIX)) == []

    CropWriter(str(tmp_path), fsync=False).save_frame("cam-b", TS, b"\xff\xd8 frame")
    assert len(calls) == 1, "fsync ran with fsync=False"


def test_a_torn_frame_write_leaves_no_frame_and_no_temp_file(tmp_path, monkeypatch):
    def full_disk(fd):
        raise OSError("no space left on device")

    monkeypatch.setattr("index.writer.os.fsync", full_disk)
    w = CropWriter(str(tmp_path))
    assert w.save_frame("cam-a", TS, b"\xff\xd8 frame") is None
    assert w.write_failures == 1
    assert list(Path(tmp_path).rglob("*.jpg")) == []
    assert list(Path(tmp_path).rglob("*" + PART_SUFFIX)) == []
