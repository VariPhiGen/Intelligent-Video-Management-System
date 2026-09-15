"""Unit tests for the NVENC/libx264 encoder selection (api/encoders.py).

Stdlib-only module by design, so these run without the API's dependencies.
The probe is exercised against stand-in binaries (`true`/`false`) — the real
encode probe is verified on GPU hardware, not here.

Run: python -m pytest services/nvr/tests -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import encoders  # noqa: E402


def test_cpu_default_args():
    pre, out = encoders.transcode_args(False)
    assert pre == []
    assert out == encoders.LIBX264_ARGS
    assert "libx264" in out and "h264_nvenc" not in out


def test_nvenc_args():
    pre, out = encoders.transcode_args(True)
    assert pre == ["-hwaccel", "cuda"]
    assert "h264_nvenc" in out
    # browser-safe pixel format normalisation must be present (10-bit sources)
    assert "format=yuv420p" in out


def test_probe_success_and_failure_via_stub_binaries():
    assert encoders.probe_nvenc("true") is True     # exits 0 → probe passes
    assert encoders.probe_nvenc("false") is False   # exits 1 → probe fails
    assert encoders.probe_nvenc("/nonexistent/ffmpeg") is False  # OSError → False


def test_probe_kill_switch(monkeypatch):
    monkeypatch.setenv("NVR_DISABLE_NVENC", "1")
    assert encoders.probe_nvenc("true") is False


def test_is_nvenc_cmd():
    _, out = encoders.transcode_args(True)
    assert encoders.is_nvenc_cmd(["ffmpeg", *out]) is True
    assert encoders.is_nvenc_cmd(["ffmpeg", *encoders.LIBX264_ARGS]) is False


def test_to_cpu_fallback_rewrites_full_command():
    pre, out = encoders.transcode_args(True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           *pre, "-ss", "1.000", "-i", "/seg/a.ts",
           "-t", "20.000", *out,
           "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
           "-y", "/out/clip.mp4"]
    fb = encoders.to_cpu_fallback(cmd)
    assert "-hwaccel" not in fb and "cuda" not in fb
    assert "h264_nvenc" not in fb and "libx264" in fb
    # input/output file placement survives the rewrite
    assert fb[fb.index("-i") + 1] == "/seg/a.ts" and fb[-1] == "/out/clip.mp4"
    # non-encoder args untouched
    assert "-movflags" in fb and "-t" in fb


# ── frame-rate conforming ──────────────────────────────────────────────────
#
# Regression guard for a 3.7x slowdown found on this fleet: exit-gate-hvte
# declares r_frame_rate=100/1 but sends 25 fps, and ffmpeg's default conforming
# duplicated every frame 4x — 5999 encoded frames per 60 s segment instead of
# 1499. Both encoder paths must keep the source's own frame timing.

def test_both_paths_pass_through_source_frame_timing():
    for nvenc_ok in (True, False):
        _, out = encoders.transcode_args(nvenc_ok)
        assert "-fps_mode" in out, f"nvenc_ok={nvenc_ok}"
        assert out[out.index("-fps_mode") + 1] == "passthrough"


def test_cpu_fallback_keeps_frame_timing():
    """The per-job NVENC→libx264 rewrite must not drop the fps mode with it."""
    cmd = ["ffmpeg", "-hwaccel", "cuda", "-i", "in.ts",
           *encoders.NVENC_ARGS, "out.mp4"]
    fallback = encoders.to_cpu_fallback(cmd)
    assert "h264_nvenc" not in fallback and "libx264" in fallback
    assert fallback[fallback.index("-fps_mode") + 1] == "passthrough"
