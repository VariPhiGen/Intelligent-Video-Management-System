"""Snapshot extraction: segment choice at boundaries, the JPEG pixel format,
and the concurrency bound.

All three are regressions waiting to happen, and two of them already had:

  • An exact segment boundary matched BOTH neighbours, and the earlier one won,
    so ffmpeg was told to seek to the very end of a segment and wrote nothing.
    Systematic rather than rare — a dataset sampling at an interval that shares
    a factor with the segment duration hits it on every single sample.

  • ffmpeg 7's mjpeg encoder refuses the limited-range YUV that cameras record
    ("Non full-range YUV is non-standard"), so every snapshot 500'd. Nothing
    pinned ffmpeg, and `python:3.x-slim` moved from bookworm (ffmpeg 5) to
    trixie (ffmpeg 7), so this arrives on an image rebuild with no code change
    to blame it on.

Run (from services/nvr): python -m pytest tests -q
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import server  # noqa: E402

SLUG = "gate-1"
T0 = 1_700_000_000.0
DURATION = 60.0


def _seg(k: int, directory: Path):
    # A real file on disk: get_snapshot filters segments by os.path.exists.
    path = directory / f"seg{k}.ts"
    path.write_bytes(b"\x47" * 188)
    return SimpleNamespace(
        camera=SLUG, start_epoch=T0 + k * DURATION, duration=DURATION,
        end_epoch=T0 + (k + 1) * DURATION,
        filepath=str(path), file_size=188,
    )


class _Index:
    """Ten consecutive 60 s segments, inclusive overlap like the real index."""
    def __init__(self, directory: Path, n=10):
        self._segs = [_seg(k, directory) for k in range(n)]
    def get_cameras(self): return [SLUG]
    def get_recording_range(self, cam):
        return (self._segs[0].start_epoch, self._segs[-1].end_epoch)
    def find_segments(self, cam, frm, to):
        return [s for s in self._segs if s.start_epoch <= to and s.end_epoch >= frm]
    def total_size(self, cam=None): return 1024


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Run get_snapshot without ffmpeg, capturing the command it would run."""
    segdir = tmp_path / "segments"
    segdir.mkdir()
    monkeypatch.setattr(server, "_index", _Index(segdir))
    monkeypatch.setattr(server, "_clips_path", tmp_path)
    monkeypatch.setattr(server, "_snapshot_semaphore", None)
    seen = {}

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = list(cmd)
        # The endpoint checks the output file exists and is non-empty.
        Path(cmd[-1]).write_bytes(b"\xff\xd8\xffFAKEJPEG")

        class _P:
            returncode = 0
            async def communicate(self): return (b"", b"")
        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _run(ts_epoch: float, captured):
    from fastapi import BackgroundTasks
    asyncio.run(server.get_snapshot(BackgroundTasks(), camera=SLUG,
                                    timestamp=_iso(ts_epoch), quality=85))
    cmd = captured["cmd"]
    return cmd[cmd.index("-i") + 1], float(cmd[cmd.index("-ss") + 1])


# ── segment choice ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("k", [1, 2, 3, 5, 9])
def test_exact_boundary_picks_the_later_segment(k, captured):
    """On a boundary both neighbours match inclusively. Choosing the earlier one
    seeks to its end, where there is no frame."""
    path, offset = _run(T0 + k * DURATION, captured)
    assert path.endswith(f"seg{k}.ts"), "picked the segment that ENDS here, not the one that starts"
    assert offset == 0.0


def test_midway_through_a_segment_is_unaffected(captured):
    path, offset = _run(T0 + DURATION + 30.0, captured)
    assert path.endswith("seg1.ts")
    assert offset == pytest.approx(30.0)


def test_offset_never_lands_at_or_past_the_segment_end(captured):
    """The last segment has no later neighbour to prefer, so the offset is
    nudged inside rather than being allowed to sit on the end."""
    last_instant = T0 + 10 * DURATION
    path, offset = _run(last_instant, captured)
    assert path.endswith("seg9.ts")
    assert 0.0 <= offset < DURATION, f"offset {offset} would extract nothing"


def test_the_nudge_clears_a_whole_frame_interval(captured):
    """Output seeking lands on the first frame AT OR AFTER the offset, so a
    backoff smaller than one frame interval still finds nothing. Half a second
    covers sources down to 2 fps."""
    _, offset = _run(T0 + 10 * DURATION, captured)
    assert DURATION - offset >= 0.5


# ── pixel format ──────────────────────────────────────────────────────────────

def test_snapshot_forces_full_range_jpeg_pixel_format(captured):
    """ffmpeg 7's mjpeg encoder rejects limited-range YUV outright. Naming
    yuvj420p works identically on ffmpeg 5 and 7."""
    _run(T0 + 90.0, captured)
    cmd = captured["cmd"]
    assert "-pix_fmt" in cmd, "no pixel format: this 500s on ffmpeg 7"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuvj420p"


def test_output_seeking_is_preserved(captured):
    """`-ss` must stay AFTER `-i`. Input seeking is faster but returns silently
    wrong frames on MPEG-TS, which is unacceptable for annotation images."""
    _run(T0 + 90.0, captured)
    cmd = captured["cmd"]
    assert cmd.index("-i") < cmd.index("-ss")


# ── concurrency bound ─────────────────────────────────────────────────────────

def test_snapshot_semaphore_is_separate_from_the_clip_semaphore(monkeypatch):
    """Sharing would make /clip 503 for the length of a dataset run, because
    get_clip rejects rather than queues when its semaphore is locked."""
    clip = asyncio.Semaphore(4)
    snap = asyncio.Semaphore(2)
    monkeypatch.setattr(server, "_clip_semaphore", clip)
    monkeypatch.setattr(server, "_snapshot_semaphore", snap)
    assert server._snapshot_semaphore is not server._clip_semaphore

    # Saturating snapshot extraction must leave every clip slot free — that is
    # the whole reason these are two semaphores and not one.
    async def hold():
        async with server._snapshot_slot():
            async with server._snapshot_slot():
                return clip.locked(), clip._value

    locked, free = asyncio.run(hold())
    assert not locked and free == 4, "a saturated snapshot cap ate clip slots"
    assert server._snapshot_semaphore_limit >= 1


def test_snapshot_slot_queues_rather_than_rejecting():
    """A batch sampler wants back-pressure, not a 503 it has to retry."""
    async def scenario():
        server._snapshot_semaphore = asyncio.Semaphore(1)
        order = []

        async def worker(tag):
            async with server._snapshot_slot():
                order.append(f"enter:{tag}")
                await asyncio.sleep(0.02)
                order.append(f"exit:{tag}")

        await asyncio.gather(worker("a"), worker("b"))
        return order

    try:
        order = asyncio.run(scenario())
    finally:
        server._snapshot_semaphore = None
    # Serialised, and crucially nobody was refused.
    assert order in (["enter:a","exit:a","enter:b","exit:b"],
                     ["enter:b","exit:b","enter:a","exit:a"])
