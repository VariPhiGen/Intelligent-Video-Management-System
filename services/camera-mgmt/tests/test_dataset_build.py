"""Frame sampling: timestamp planning, gap tolerance, re-run cleanup, manifest.

The whole point of dataset_build taking an injected fetcher is that this runs
with no NVR, no ffmpeg and no recorded footage. Every behaviour the sampler
promises an operator is checked here, in particular the three that a naive
implementation gets wrong: midpoint sampling, counting gaps instead of aborting
on them, and clearing the previous run before writing a new one.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import dataset_build as db  # noqa: E402

START = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def recipe(**over):
    base = {
        "id": 42, "camera_slug": "gate-1",
        "window_start": START, "window_end": END,
        "target_frames": 10, "jpeg_quality": 85,
    }
    base.update(over)
    return base


def fetch_all(payload=b"JPEGDATA"):
    async def _f(slug, at_iso, quality):
        return payload
    return _f


# ── plan_timestamps ───────────────────────────────────────────────────────────

def test_timestamps_are_midpoints_not_endpoints():
    """The edges are exactly where the NVR is least likely to have a covering
    segment, so no sample may land on them."""
    ts = db.plan_timestamps(START, END, 4)
    assert len(ts) == 4
    assert ts[0] > START, "first frame must not sit on the window's start edge"
    assert ts[-1] < END, "last frame must not sit on the window's end edge"
    # 2h / 4 = 30 min slices; midpoints at 15, 45, 75, 105 minutes in.
    assert ts[0] == START + timedelta(minutes=15)
    assert ts[-1] == START + timedelta(minutes=105)


def test_timestamps_are_evenly_spaced():
    ts = db.plan_timestamps(START, END, 12)
    gaps = {(b - a).total_seconds() for a, b in zip(ts, ts[1:])}
    assert len(gaps) == 1, f"spacing drifted: {gaps}"
    assert gaps.pop() == pytest.approx(7200 / 12)


def test_single_frame_lands_at_the_window_centre():
    """N=1 is the case `start + k*span/(N-1)` divides by zero on."""
    assert db.plan_timestamps(START, END, 1) == [START + timedelta(hours=1)]


def test_spacing_matches_the_interval_the_ui_is_shown():
    """models.dataset_interval_seconds is what the operator reads; it must
    equal what the sampler actually does."""
    from backend.models import dataset_interval_seconds
    ts = db.plan_timestamps(START, END, 200)
    actual = (ts[1] - ts[0]).total_seconds()
    assert actual == pytest.approx(dataset_interval_seconds(START, END, 200))


# ── collect_frames: the happy path and the manifest ───────────────────────────

def test_collect_writes_files_and_manifest(tmp_path):
    res = asyncio.run(db.collect_frames(recipe(), str(tmp_path), fetch_all()))

    assert res["collected"] == 10 and res["missing"] == 0
    assert res["bytes_total"] == 10 * len(b"JPEGDATA")

    directory = Path(res["frames_dir"])
    assert directory.name == "42-gate-1"
    on_disk = sorted(p.name for p in directory.glob("frame_*.jpg"))
    assert len(on_disk) == 10
    assert [f["filename"] for f in res["frames"]] == on_disk, \
        "manifest must list exactly the files on disk, in capture order"

    first = res["frames"][0]
    assert first["filename"].startswith("frame_00000_")
    assert first["filename"].endswith(".jpg")
    assert len(first["sha256"]) == 64
    assert first["bytes"] == len(b"JPEGDATA")


def test_manifest_timestamps_use_z_not_offset(tmp_path):
    res = asyncio.run(db.collect_frames(recipe(), str(tmp_path), fetch_all()))
    for frame in res["frames"]:
        assert frame["timestamp"].endswith("Z")
        assert "+00:00" not in frame["timestamp"]


def test_filename_carries_the_capture_epoch(tmp_path):
    """A frame must stay meaningful after it leaves this system."""
    res = asyncio.run(db.collect_frames(recipe(target_frames=1), str(tmp_path),
                                        fetch_all()))
    name = res["frames"][0]["filename"]
    epoch_ms = int(name.removeprefix("frame_00000_").removesuffix(".jpg"))
    centre = START + timedelta(hours=1)
    assert epoch_ms == int(centre.timestamp() * 1000)


# ── collect_frames: gaps are counted, not fatal ───────────────────────────────

def test_gaps_are_counted_not_fatal(tmp_path):
    """Aborting on the first None would discard every frame that did extract."""
    async def half_missing(slug, at_iso, quality):
        return None if at_iso.endswith("Z") and "10:" in at_iso else b"JPEG"

    res = asyncio.run(db.collect_frames(recipe(), str(tmp_path), half_missing))
    assert res["collected"] > 0
    assert res["missing"] > 0
    assert res["collected"] + res["missing"] == 10
    assert len(res["frames"]) == res["collected"]


def test_a_raising_fetcher_costs_one_frame_not_the_run(tmp_path):
    async def flaky(slug, at_iso, quality):
        if at_iso.endswith("10:06:00Z"):
            raise RuntimeError("NVR blew up on this one frame")
        return b"JPEG"

    res = asyncio.run(db.collect_frames(recipe(), str(tmp_path), flaky))
    assert res["collected"] == 9 and res["missing"] == 1


def test_zero_frames_fails_with_an_actionable_message(tmp_path):
    async def nothing(slug, at_iso, quality):
        return None

    with pytest.raises(db.DatasetBuildError) as exc:
        asyncio.run(db.collect_frames(recipe(), str(tmp_path), nothing))
    message = str(exc.value)
    assert "retention" in message.lower()
    assert "gate-1" in message


def test_write_failure_counts_as_missing_rather_than_raising(tmp_path, monkeypatch):
    """Disk full mid-run must not lose the frames already written."""
    real = Path.write_bytes
    calls = {"n": 0}

    def flaky_write(self, data):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise OSError(28, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky_write)
    res = asyncio.run(db.collect_frames(recipe(), str(tmp_path), fetch_all()))
    assert res["collected"] == 5 and res["missing"] == 5


# ── collect_frames: a re-run starts clean ─────────────────────────────────────

def test_rerun_deletes_the_previous_runs_frames(tmp_path):
    """Re-collecting at a higher target must not interleave two runs.

    This is the check that catches a missing pre-run cleanup: without it the
    directory ends up holding 10 stale files plus 25 new ones while the
    manifest lists only 25.
    """
    first = asyncio.run(db.collect_frames(recipe(target_frames=10), str(tmp_path),
                                          fetch_all()))
    stale = sorted(p.name for p in Path(first["frames_dir"]).glob("frame_*.jpg"))
    assert len(stale) == 10

    second = asyncio.run(db.collect_frames(recipe(target_frames=25), str(tmp_path),
                                           fetch_all()))
    directory = Path(second["frames_dir"])
    on_disk = sorted(p.name for p in directory.glob("frame_*.jpg"))

    assert len(on_disk) == 25, "old run's frames were left behind"
    assert [f["filename"] for f in second["frames"]] == on_disk
    leftovers = [n for n in stale if n in on_disk and n not in
                 {f["filename"] for f in second["frames"]}]
    assert not leftovers


def test_cleanup_leaves_non_frame_files_alone(tmp_path):
    directory = Path(db.frames_dir_for(42, "gate-1", str(tmp_path)))
    directory.mkdir(parents=True)
    keep = directory / "operator-notes.txt"
    keep.write_text("do not delete me")

    asyncio.run(db.collect_frames(recipe(), str(tmp_path), fetch_all()))
    assert keep.exists()


# ── progress reporting ────────────────────────────────────────────────────────

def test_progress_reports_and_finishes_on_the_last_frame(tmp_path):
    seen = []

    async def progress(done, total):
        seen.append((done, total))

    asyncio.run(db.collect_frames(recipe(target_frames=25), str(tmp_path),
                                  fetch_all(), progress))
    assert seen, "progress was never reported"
    assert seen[-1] == (25, 25), "final progress must report the completed run"
    assert all(total == 25 for _, total in seen)


def test_a_failing_progress_callback_does_not_fail_the_run(tmp_path):
    async def broken(done, total):
        raise RuntimeError("the database went away")

    res = asyncio.run(db.collect_frames(recipe(), str(tmp_path), fetch_all(), broken))
    assert res["collected"] == 10


# ── slug safety and directory deletion ────────────────────────────────────────

def test_slug_is_made_filesystem_safe():
    assert db.safe_slug("gate-1") == "gate-1"
    assert db.safe_slug("../../etc/passwd") == ".._.._etc_passwd"
    # Only an EMPTY result falls back; "///" collapses to a single safe "_".
    assert db.safe_slug("") == "camera"
    assert db.safe_slug("///") == "_"


def test_dataset_directory_cannot_escape_the_root(tmp_path):
    """`.` and `-` survive sanitising, so ".." stays "..". The `{id}-` prefix
    is what makes that inert — the directory is literally named "42-..", not
    the parent of the root."""
    path = Path(db.frames_dir_for(42, "..", str(tmp_path)))
    assert path.name == "42-.."
    assert path.parent == tmp_path
    assert path.resolve().parent == tmp_path.resolve()


def test_delete_frames_dir_refuses_to_escape_the_root(tmp_path):
    root = tmp_path / "dataset-frames"
    root.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "evidence.zip").write_text("x")

    db.delete_frames_dir(str(outside), str(root))
    assert (outside / "evidence.zip").exists(), "deleted a directory outside the root"

    db.delete_frames_dir(str(root), str(root))
    assert root.exists(), "deleted the root itself"

    inside = root / "42-gate-1"
    inside.mkdir()
    (inside / "frame_00000_1.jpg").write_text("x")
    db.delete_frames_dir(str(inside), str(root))
    assert not inside.exists()


# ── detection mode: keep only frames containing a selected class ──────────────

def test_scan_timestamps_follow_interval_inside_window():
    ts = db.plan_scan_timestamps(START, START + timedelta(seconds=10), 2.0)
    assert len(ts) == 5
    assert ts[0] == START + timedelta(seconds=1)          # half an interval in
    assert all(START < t < START + timedelta(seconds=10) for t in ts)


def _detect_recipe(**over):
    kw = {"window_end": START + timedelta(seconds=20),
          "scan_interval_seconds": 2.0, "target_frames": 100}
    kw.update(over)
    return recipe(**kw)


def test_only_frames_with_a_detection_are_kept(tmp_path):
    """Frames where the model finds nothing are scanned but never written."""
    seen = []

    async def fetch(slug, at_iso, quality):
        return at_iso.encode()          # frame content = its own timestamp

    async def detector(jpeg):
        seen.append(jpeg)
        # person present in every other scan
        return [{"label": "person", "confidence": 0.9}] if len(seen) % 2 else []

    out = asyncio.run(db.collect_frames(
        _detect_recipe(), str(tmp_path), fetch, detector=detector, concurrency=1))
    assert out["scanned"] == 10
    assert out["collected"] == 5
    assert out["missing"] == 0, "no-detection frames are not 'missing'"
    assert len(list(Path(out["frames_dir"]).glob("frame_*.jpg"))) == 5
    assert all(f["detections"] for f in out["frames"])


def test_target_frames_caps_kept_frames_in_detection_mode(tmp_path):
    async def fetch(slug, at_iso, quality):
        return b"x"

    async def detector(jpeg):
        return [{"label": "person"}]

    out = asyncio.run(db.collect_frames(
        _detect_recipe(target_frames=3), str(tmp_path), fetch,
        detector=detector, concurrency=1))
    assert out["collected"] == 3
    assert out["scanned"] <= 4, "must stop scanning once the cap is reached"


def test_run_fails_clearly_when_nothing_is_detected(tmp_path):
    async def fetch(slug, at_iso, quality):
        return b"x"

    async def detector(jpeg):
        return []

    with pytest.raises(db.DatasetBuildError, match="none of the selected classes"):
        asyncio.run(db.collect_frames(
            _detect_recipe(), str(tmp_path), fetch, detector=detector))


def test_detector_failure_is_reported_not_silently_empty(tmp_path):
    async def fetch(slug, at_iso, quality):
        return b"x"

    async def detector(jpeg):
        raise RuntimeError("analytics down")

    with pytest.raises(db.DatasetBuildError, match="detection model failed"):
        asyncio.run(db.collect_frames(
            _detect_recipe(), str(tmp_path), fetch, detector=detector))
