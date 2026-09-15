"""HLS VOD playback over recorded segments (api/hls.py).

Covers the three things that make this path safe to expose: the mode ladder,
the URL parser that is the only path-ish input the segment route accepts, and
the cache's content-keyed invalidation. The remux test shells out to the real
ffmpeg — the whole claim of the fmp4 mode is that ffmpeg produces a playable
init+m4s pair from a recorded segment, and a mocked ffmpeg would prove nothing.
"""
import asyncio
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import hls  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@dataclass
class FakeSeg:
    camera: str
    start_epoch: float
    duration: float
    filepath: str

    @property
    def end_epoch(self) -> float:
        return self.start_epoch + self.duration


def _seg(tmp_path, name, start, dur=60.0, body=b"x"):
    p = tmp_path / f"{name}.ts"
    p.write_bytes(body)
    return FakeSeg("cam", start, dur, str(p))


# ── mode ladder ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("codec,client_hevc,expected", [
    ("h264", False, hls.MODE_TS),
    ("h264", True, hls.MODE_TS),      # copy beats remux even for an HEVC client
    ("hevc", True, hls.MODE_FMP4),
    ("hevc", False, hls.MODE_H264),
    ("", False, hls.MODE_H264),       # probe failed → transcode, the safe side
    ("mjpeg", True, hls.MODE_H264),
])
def test_choose_mode(codec, client_hevc, expected):
    assert hls.choose_mode(codec, client_hevc) == expected


# ── artifact-name parsing (the only path-ish input the route takes) ────────

def test_parse_artifact_roundtrip():
    assert hls.parse_artifact("cam-1_20260904_173256.a1b2c3d4.m4s") == (
        "cam-1_20260904_173256", "a1b2c3d4", "m4s")
    assert hls.parse_artifact("cam-1_20260904_173256.a1b2c3d4.init.mp4") == (
        "cam-1_20260904_173256", "a1b2c3d4", "init")
    assert hls.parse_artifact("cam-1_20260904_173256.a1b2c3d4.ts") == (
        "cam-1_20260904_173256", "a1b2c3d4", "ts")


@pytest.mark.parametrize("name", [
    "../../etc/passwd",
    "..%2Fx.a1b2c3d4.ts",
    "cam/../other_20260904_173256.a1b2c3d4.ts",
    "cam_20260904_173256.ZZZZZZZZ.ts",      # signature isn't lowercase hex
    "cam_20260904_173256.a1b2c3d4.mkv",     # not an emitted kind
    "cam_2026_173256.a1b2c3d4.ts",          # stem doesn't match the recorder's
    "_leading_20260904_173256.a1b2c3d4.ts",
    "cam_20260904_173256.ts",               # no signature at all
])
def test_parse_artifact_rejects(name):
    with pytest.raises(ValueError):
        hls.parse_artifact(name)


# ── playlist rendering ─────────────────────────────────────────────────────

def test_playlist_ts_mode_is_plain_and_relative(tmp_path):
    segs = [_seg(tmp_path, "cam_20260904_173156", 1000.0),
            _seg(tmp_path, "cam_20260904_173256", 1060.0)]
    pl = hls.build_playlist(segs, hls.MODE_TS, "ts/")
    assert "#EXT-X-VERSION:3" in pl
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in pl
    assert "#EXT-X-TARGETDURATION:60" in pl
    assert pl.rstrip().endswith("#EXT-X-ENDLIST")
    assert "#EXT-X-MAP" not in pl              # TS carries no init segment
    # Relative URIs: the NVR never learns its external mount point.
    assert "\nts/cam_20260904_173156." in pl
    assert "/api/" not in pl


def test_playlist_marks_every_boundary_discontinuous(tmp_path):
    """The recorder runs -reset_timestamps, so each segment restarts at PTS 0.

    Without a DISCONTINUITY per boundary the player would stack segments on a
    monotonic timeline that the media doesn't have.
    """
    segs = [_seg(tmp_path, "cam_20260904_173156", 1000.0),
            _seg(tmp_path, "cam_20260904_173256", 1060.0),
            _seg(tmp_path, "cam_20260904_173356", 1120.0)]
    pl = hls.build_playlist(segs, hls.MODE_TS, "ts/")
    # One fewer than the segment count — the first opens the playlist.
    assert pl.count("#EXT-X-DISCONTINUITY") == 2
    assert pl.count("#EXT-X-PROGRAM-DATE-TIME:") == 3


def test_playlist_fmp4_mode_maps_init_per_segment(tmp_path):
    segs = [_seg(tmp_path, "cam_20260904_173156", 1000.0),
            _seg(tmp_path, "cam_20260904_173256", 1060.0)]
    pl = hls.build_playlist(segs, hls.MODE_FMP4, "fmp4/")
    assert "#EXT-X-VERSION:7" in pl
    assert pl.count("#EXT-X-MAP:URI=") == 2
    assert ".init.mp4" in pl and ".m4s" in pl


def test_playlist_program_date_time_tracks_wall_clock(tmp_path):
    segs = [_seg(tmp_path, "cam_20260904_173156", 1757000000.0)]
    pl = hls.build_playlist(segs, hls.MODE_TS, "ts/")
    assert "#EXT-X-PROGRAM-DATE-TIME:2025-09-04T" in pl


def test_playlist_signature_is_in_the_uri(tmp_path):
    """A rebuilt segment must land on a different URI, not silently reuse."""
    seg = _seg(tmp_path, "cam_20260904_173156", 1000.0, body=b"first")
    before = hls.build_playlist([seg], hls.MODE_FMP4, "fmp4/")
    Path(seg.filepath).write_bytes(b"rewritten-by-grooming")
    os.utime(seg.filepath, (2_000_000, 2_000_000))
    after = hls.build_playlist([seg], hls.MODE_FMP4, "fmp4/")
    assert before != after


def test_source_signature_survives_a_missing_file(tmp_path):
    assert hls.source_signature(str(tmp_path / "gone.ts")) == "00000000"


# ── cache ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _no_slot():
    yield


def _make_ts(path: Path, seconds: int = 1) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=black:s=128x96:d={seconds}:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "5",
         "-f", "mpegts", "-y", str(path)],
        check=True,
    )


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_remux_produces_a_playable_init_and_media_pair(tmp_path):
    src = tmp_path / "cam_20260904_173156.ts"
    _make_ts(src)
    cache = hls.HlsCache(tmp_path / "cache", transcode_args=lambda: ([], []),
                         slot=_no_slot)
    sig = hls.source_signature(str(src))

    async def go():
        media = await cache.get("cam", hls.MODE_FMP4, src.stem, sig, "m4s",
                                str(src), 1.0)
        init = await cache.get("cam", hls.MODE_FMP4, src.stem, sig, "init",
                               str(src), 1.0)
        return media, init

    media, init = asyncio.run(go())
    assert media.exists() and init.exists()
    # init + media concatenated is a complete fMP4 — the shape the player sees
    # after following EXT-X-MAP.
    joined = tmp_path / "joined.mp4"
    joined.write_bytes(init.read_bytes() + media.read_bytes())
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(joined)],
        capture_output=True, text=True, check=True,
    )
    assert "h264" in out.stdout


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_concurrent_requests_build_once(tmp_path):
    """Two players seeking to the same segment must not race two ffmpegs."""
    src = tmp_path / "cam_20260904_173156.ts"
    _make_ts(src)
    calls = []
    cache = hls.HlsCache(tmp_path / "cache", transcode_args=lambda: ([], []),
                         slot=_no_slot)
    real = cache._materialise

    async def counting(*a, **kw):
        calls.append(a)
        return await real(*a, **kw)

    cache._materialise = counting
    sig = hls.source_signature(str(src))

    async def go():
        return await asyncio.gather(*[
            cache.get("cam", hls.MODE_FMP4, src.stem, sig, "m4s", str(src), 1.0)
            for _ in range(5)
        ])

    paths = asyncio.run(go())
    assert len({str(p) for p in paths}) == 1
    assert len(calls) == 1
    # The per-key lock is released and forgotten, not leaked per request.
    assert cache._locks == {}


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_failed_build_leaves_no_half_artifact(tmp_path):
    src = tmp_path / "cam_20260904_173156.ts"
    src.write_bytes(b"not a transport stream")
    cache = hls.HlsCache(tmp_path / "cache", transcode_args=lambda: ([], []),
                         slot=_no_slot)
    sig = hls.source_signature(str(src))
    with pytest.raises(hls.HlsError):
        asyncio.run(cache.get("cam", hls.MODE_FMP4, src.stem, sig, "m4s",
                              str(src), 1.0))
    leftovers = list((tmp_path / "cache" / "cam").glob("*"))
    assert leftovers == [], f"scratch or partial output left behind: {leftovers}"


def test_purge_camera_reclaims_and_reports(tmp_path):
    cache = hls.HlsCache(tmp_path / "cache", transcode_args=lambda: ([], []),
                         slot=_no_slot)
    d = cache.camera_dir("cam")
    d.mkdir(parents=True)
    (d / "a.fmp4.deadbeef.m4s").write_bytes(b"0" * 100)
    (d / "a.fmp4.deadbeef.init.mp4").write_bytes(b"0" * 50)
    assert cache.purge_camera("cam") == 150
    assert not d.exists()
    assert cache.purge_camera("never-existed") == 0


def test_ts_mode_is_never_materialised(tmp_path):
    """ts mode is served straight off disk; asking the cache to build it is a bug."""
    cache = hls.HlsCache(tmp_path / "cache", transcode_args=lambda: ([], []),
                         slot=_no_slot)
    with pytest.raises(hls.HlsError):
        asyncio.run(cache._materialise("cam", hls.MODE_TS, "cam_20260904_173156",
                                       "deadbeef", str(tmp_path / "x.ts"), 1.0))


# ── per-job CPU fallback ───────────────────────────────────────────────────
#
# /clip has always degraded a failed NVENC job to libx264 rather than failing
# the request (server.py). The HLS path shipped without that, which made a
# transient NVENC blip a hard 500 mid-playlist instead of a slower segment.

@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_hardware_failure_degrades_to_cpu(tmp_path):
    src = tmp_path / "cam_20260904_173156.ts"
    _make_ts(src)
    # A "hardware" encoder that does not exist, and a CPU path that does.
    cache = hls.HlsCache(
        tmp_path / "cache",
        transcode_args=lambda: ([], ["-c:v", "h264_nonexistent_encoder"]),
        cpu_transcode_args=lambda: ([], ["-c:v", "libx264", "-preset", "ultrafast"]),
        slot=_no_slot,
    )
    sig = hls.source_signature(str(src))
    path = asyncio.run(cache.get("cam", hls.MODE_H264, src.stem, sig, "m4s",
                                 str(src), 1.0))
    assert path.exists() and path.stat().st_size > 0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_both_encoders_failing_still_raises(tmp_path):
    src = tmp_path / "cam_20260904_173156.ts"
    _make_ts(src)
    cache = hls.HlsCache(
        tmp_path / "cache",
        transcode_args=lambda: ([], ["-c:v", "h264_nonexistent_encoder"]),
        cpu_transcode_args=lambda: ([], ["-c:v", "also_nonexistent"]),
        slot=_no_slot,
    )
    sig = hls.source_signature(str(src))
    with pytest.raises(hls.HlsError):
        asyncio.run(cache.get("cam", hls.MODE_H264, src.stem, sig, "m4s",
                              str(src), 1.0))
    assert list((tmp_path / "cache" / "cam").glob("*")) == []


# ── extraction concurrency sizing ──────────────────────────────────────────
#
# A fixed cap of 4 is right for a hardware encoder and wrong for a slow CPU:
# measured, four concurrent libx264 jobs on a 4-vCPU-class box fall under
# realtime, which stalls playback mid-segment rather than merely starting slow.

def _slots(configured, nvenc_ok, cores, monkeypatch):
    from api import server
    monkeypatch.setattr(server, "_available_cores", lambda: cores)
    return server._size_extraction_slots(configured, nvenc_ok)


def test_hardware_encoder_keeps_the_historical_cap(monkeypatch):
    for cores in (2, 4, 24):
        slots, why = _slots(None, True, cores, monkeypatch)
        assert slots == 4 and "nvenc" in why


@pytest.mark.parametrize("cores,expected", [
    (1, 1), (2, 1), (4, 1),      # never 0, and never 4 on a small box
    (8, 2), (12, 3),
    (16, 4), (64, 4),            # capped — more slots stop helping
])
def test_cpu_encoder_scales_with_cores(cores, expected, monkeypatch):
    slots, why = _slots(None, False, cores, monkeypatch)
    assert slots == expected
    assert f"{cores} core" in why


def test_explicit_configuration_always_wins(monkeypatch):
    # Even a 1-core CPU box honours an operator who has measured their hardware.
    assert _slots(8, False, 1, monkeypatch) == (8, "configured")
    assert _slots(1, True, 64, monkeypatch) == (1, "configured")
    # ...but never a nonsensical zero.
    assert _slots(0, False, 8, monkeypatch)[0] == 1


def test_available_cores_respects_a_cgroup_quota(tmp_path, monkeypatch):
    """A --cpus=2 container must not be sized as though it had the host's cores.

    This is the whole point: os.cpu_count() answers 24 in that container, and
    sizing off it would hand a 2-core box four concurrent libx264 jobs.
    """
    from api import server
    (tmp_path / "cpu.max").write_text("200000 100000")     # --cpus=2
    real = server.Path
    monkeypatch.setattr(server, "Path",
                        lambda p: tmp_path / "cpu.max" if p == "/sys/fs/cgroup/cpu.max" else real(p))
    monkeypatch.setattr(server.os, "cpu_count", lambda: 24)
    monkeypatch.setattr(server.os, "sched_getaffinity", lambda _: set(range(24)))
    assert server._available_cores() == 2


def test_available_cores_respects_cpu_affinity(tmp_path, monkeypatch):
    """taskset / --cpuset-cpus, with no quota set."""
    from api import server
    real = server.Path
    monkeypatch.setattr(server, "Path",
                        lambda p: tmp_path / "missing" if str(p).startswith("/sys/fs/cgroup") else real(p))
    monkeypatch.setattr(server.os, "cpu_count", lambda: 24)
    monkeypatch.setattr(server.os, "sched_getaffinity", lambda _: {0, 1, 2, 3})
    assert server._available_cores() == 4


def test_available_cores_never_returns_zero(tmp_path, monkeypatch):
    from api import server
    (tmp_path / "cpu.max").write_text("50000 100000")      # --cpus=0.5
    real = server.Path
    monkeypatch.setattr(server, "Path",
                        lambda p: tmp_path / "cpu.max" if p == "/sys/fs/cgroup/cpu.max" else real(p))
    monkeypatch.setattr(server.os, "sched_getaffinity", lambda _: {0})
    assert server._available_cores() == 1


# ── track ranking (main vs sub) ────────────────────────────────────────────
#
# A camera may have a low-res sub recorded alongside its main. Which one
# playback serves is a cost question, and mode dominates pixels: copying a
# 1080p main is free, transcoding a 352x288 sub is not.

@pytest.mark.parametrize("a,b,cheaper", [
    # copy beats transcode, however small the transcode's source
    ((hls.MODE_TS, 1920, 1080), (hls.MODE_H264, 352, 288), "a"),
    # ...and beats a remux too
    ((hls.MODE_TS, 1920, 1080), (hls.MODE_FMP4, 704, 576), "a"),
    # remux beats transcode
    ((hls.MODE_FMP4, 1920, 1080), (hls.MODE_H264, 352, 288), "a"),
    # within one mode, fewer pixels win
    ((hls.MODE_H264, 352, 288), (hls.MODE_H264, 1920, 1080), "a"),
    ((hls.MODE_TS, 704, 576), (hls.MODE_TS, 1920, 1080), "a"),
])
def test_rank_track_prefers_the_cheaper_service(a, b, cheaper):
    ra, rb = hls.rank_track(*a), hls.rank_track(*b)
    assert (ra < rb) is (cheaper == "a")


def test_rank_track_survives_an_unprobed_resolution():
    """A probe failure must not crash selection, just sort last within its mode."""
    assert hls.rank_track(hls.MODE_TS, None, None) < hls.rank_track(hls.MODE_H264, 1, 1)


def test_this_fleet_would_pick_the_sub():
    """The real measurements from this appliance, as a selection decision."""
    # entry-g98a: HEVC 1080p main, H.264 720p sub -> sub, and it is free.
    main = hls.rank_track(hls.choose_mode("hevc", False), 1920, 1080)
    sub = hls.rank_track(hls.choose_mode("h264", False), 1280, 720)
    assert sub < main
    # exit-gate: HEVC 1080p main, HEVC 352x288 sub -> sub, transcode but tiny.
    main = hls.rank_track(hls.choose_mode("hevc", False), 1920, 1080)
    sub = hls.rank_track(hls.choose_mode("hevc", False), 352, 288)
    assert sub < main
    # cabin-u9tn: H.264 1440p main, HEVC 720p sub -> MAIN, because copying the
    # big stream beats transcoding the small one.
    main = hls.rank_track(hls.choose_mode("h264", False), 2560, 1440)
    sub = hls.rank_track(hls.choose_mode("hevc", False), 1280, 720)
    assert main < sub
