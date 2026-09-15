"""Clip length across recording-file boundaries (api/server.py `_extract_clip`).

The recorder writes segments with `-reset_timestamps 1`, so every file boundary
restarts the timeline. A stream-copied clip bounded with `-ss`/`-t` around the
concat demuxer kept going past the requested length once it crossed a boundary
— on the appliance, 30 s event clips from an H.264 camera came back ~77 s long.
Multi-file copy clips are now bounded with the demuxer's inpoint/outpoint.

The integration tests build real MPEG-TS segments with ffmpeg the way the
recorder does and are skipped where ffmpeg is not installed. A stream-copied
cut begins on the first keyframe at or after the requested start, so a copied
clip may be up to one GOP (2 s here) short — but it must never run on into the
next file.

Run: python -m pytest services/nvr/tests -q
"""
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import server  # noqa: E402
from storage.index import Segment  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg/ffprobe not installed")


# ── the concat list ────────────────────────────────────────────────────────

def seg(i, start, duration=60.0):
    return Segment(camera="cam", start_epoch=start, duration=duration, filepath=f"/rec/cam_{i}.ts", file_size=1)


def test_the_list_starts_in_the_first_file_and_stops_in_the_last():
    listing = server._bounded_concat_list([seg(0, 1000.0), seg(1, 1060.0)], 1050.0, 1090.0)
    assert listing == ("file '/rec/cam_0.ts'\ninpoint 50.000\n"
                       "file '/rec/cam_1.ts'\noutpoint 30.000\n")


def test_points_are_in_each_files_own_timestamps():
    listing = server._bounded_concat_list([seg(0, 1000.0), seg(1, 1060.0)], 1050.0, 1090.0,
                                          first_start_time=1.4, last_start_time=1.5)
    assert "inpoint 51.400" in listing and "outpoint 31.500" in listing


def test_files_in_between_are_taken_whole():
    listing = server._bounded_concat_list([seg(0, 0.0), seg(1, 60.0), seg(2, 120.0)], 30.0, 150.0)
    lines = listing.splitlines()
    assert lines == ["file '/rec/cam_0.ts'", "inpoint 30.000", "file '/rec/cam_1.ts'",
                     "file '/rec/cam_2.ts'", "outpoint 30.000"]


# ── real files ─────────────────────────────────────────────────────────────

def _duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    """Three 6 s H.264 MPEG-TS segments, keyframe every 2 s and no B-frames (as IP
    cameras usually send), recorded like the NVR does."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    d = tmp_path_factory.mktemp("rec")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-t", "18",
        "-c:v", "libx264", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0", "-bf", "0",
        "-pix_fmt", "yuv420p",
        "-f", "segment", "-segment_time", "6", "-segment_format", "mpegts", "-reset_timestamps", "1",
        str(d / "cam_%03d.ts"),
    ], check=True)
    files = sorted(d.glob("cam_*.ts"))
    base = 1_789_000_000.0
    segments, start = [], base
    for f in files:
        length = _duration(f)
        segments.append(Segment(camera="clipcam", start_epoch=start, duration=length,
                                filepath=str(f), file_size=f.stat().st_size))
        start += length
    return segments


def _extract(segments, clip_start, clip_duration, out, codec):
    server._codec_cache["clipcam"] = (codec, 320, 240, time.time())
    asyncio.run(server._extract_clip(segments, clip_start, clip_duration, str(out)))
    return _duration(out)


GOP = 2.0


@needs_ffmpeg
def test_a_copied_clip_across_a_boundary_is_the_length_asked_for(recording, tmp_path):
    first, second = recording[0], recording[1]
    clip_start = first.end_epoch - 3.0                       # 3 s before the boundary, 3 s after
    length = _extract([first, second], clip_start, 6.0, tmp_path / "copy.mp4", "h264")
    # Before the fix the rest of the second file came too: 3 s + 6 s = 9 s.
    assert 6.0 - GOP - 0.1 <= length <= 6.0 + 0.1, length


@needs_ffmpeg
def test_a_copied_clip_spanning_three_files_is_the_length_asked_for(recording, tmp_path):
    clip_start = recording[0].start_epoch + 4.0              # on a keyframe: exact
    length = _extract(recording, clip_start, 10.0, tmp_path / "three.mp4", "h264")
    assert 10.0 - 0.1 <= length <= 10.0 + 0.1, length


@needs_ffmpeg
def test_a_copied_clip_inside_one_file_is_unchanged(recording, tmp_path):
    length = _extract([recording[1]], recording[1].start_epoch + 1.0, 4.0, tmp_path / "one.mp4", "h264")
    assert 4.0 - GOP - 0.1 <= length <= 4.0 + 0.1, length


@needs_ffmpeg
def test_a_re_encoded_clip_across_a_boundary_stays_exact(recording, tmp_path):
    first, second = recording[0], recording[1]
    length = _extract([first, second], first.end_epoch - 3.0, 6.0, tmp_path / "exact.mp4", "hevc")
    assert 5.5 <= length <= 6.5, length
