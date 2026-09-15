"""encoders.py — transcode encoder selection: NVENC when the GPU path works,
libx264 otherwise.

Why a probe and not detection: "GPU exists" ≠ "NVENC works". The container
needs the NVIDIA runtime to have injected libnvidia-encode, the driver must
match, and the encoder must actually open a session — any of which can fail
while nvidia-smi looks healthy. So the gate is an actual encode: two black
frames through h264_nvenc at startup. Deliberately stdlib-only (no fastapi
import) so tests can exercise the selection logic without the API's deps.

Input side uses plain `-hwaccel cuda` WITHOUT `-hwaccel_output_format cuda`:
ffmpeg then decodes on NVDEC when it can and silently falls back to software
decode when it can't (e.g. an exotic profile) — a built-in safety net. Frames
land in system memory; `format=yuv420p` normalises both 8-bit NV12 and 10-bit
P010 sources to what h264_nvenc + HTML5 <video> accept.
"""
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 20  # seconds; first CUDA context on a cold driver can be slow

# Output args for the transcode path. libx264 mirrors the historical CPU path;
# nvenc keeps visually-comparable quality (cq 26 ≈ crf 23 ballpark) at a small
# fraction of the CPU cost — the encode itself runs on the GPU's NVENC block.
#
# `-fps_mode passthrough` is load-bearing, not tidiness. Some cameras declare a
# container tick rate far above the frames they actually send: measured on this
# fleet, exit-gate-hvte reports r_frame_rate=100/1 while delivering 25 fps
# (1499 frames per 60 s segment). ffmpeg's default is to conform the output to
# that declared rate, so it was **duplicating every frame 4x** and encoding 5999
# frames where 1499 exist — 11.8 s and 47 MB per segment instead of 3.2 s and
# 34 MB. Passthrough keeps the source's own frame timing: a 3.7x speedup on that
# camera, and a no-op on cameras whose declared and actual rates already agree
# (entry-g98a and cabin-u9tn measured byte-identical either way).
_FPS_MODE = ["-fps_mode", "passthrough"]

LIBX264_ARGS = [
    *_FPS_MODE,
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "23",
    "-pix_fmt", "yuv420p",
    "-an",
]
NVENC_ARGS = [
    *_FPS_MODE,
    "-vf", "format=yuv420p",
    "-c:v", "h264_nvenc",
    "-preset", "p4",
    "-cq", "26",
    "-an",
]
NVENC_INPUT_ARGS = ["-hwaccel", "cuda"]


def probe_nvenc(ffmpeg_bin: str = "ffmpeg") -> bool:
    """True when h264_nvenc can actually encode in THIS process's environment.

    Honors NVR_DISABLE_NVENC=1 as a kill switch (troubleshooting / forcing the
    CPU path). Failure of any kind — missing encoder, missing driver libs, no
    device, timeout — is False, never an exception: the CPU path is always the
    safe landing.
    """
    if os.environ.get("NVR_DISABLE_NVENC", "").strip() in ("1", "true", "yes"):
        logger.info("NVENC disabled by NVR_DISABLE_NVENC")
        return False
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=black:s=256x144:d=0.2",
        "-frames:v", "2", "-c:v", "h264_nvenc", "-f", "null", "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("NVENC probe errored (%s) — using libx264", e)
        return False
    if out.returncode == 0:
        logger.info("NVENC probe OK — GPU transcode (h264_nvenc) enabled")
        return True
    logger.info(
        "NVENC probe failed (rc=%s: %s) — using libx264",
        out.returncode, out.stderr.decode(errors="replace").strip()[:200],
    )
    return False


def transcode_args(nvenc_ok: bool) -> tuple[list[str], list[str]]:
    """(input_args, output_args) for a transcode-to-H.264 extraction."""
    if nvenc_ok:
        return list(NVENC_INPUT_ARGS), list(NVENC_ARGS)
    return [], list(LIBX264_ARGS)


def is_nvenc_cmd(cmd: list[str]) -> bool:
    """Whether an assembled ffmpeg command uses the NVENC path (for fallback)."""
    return "h264_nvenc" in cmd


def to_cpu_fallback(cmd: list[str]) -> list[str]:
    """Rewrite an NVENC extraction command to the libx264 CPU path.

    Used for the per-job fallback: a transient NVENC failure (driver hiccup,
    exhausted encode sessions on consumer GPUs) shouldn't fail the clip — it
    should cost CPU instead. Strips the `-hwaccel cuda` input pair and swaps
    the NVENC output block for the libx264 one.
    """
    out: list[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "-hwaccel" and i + 1 < len(cmd):
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    # Replace the output block. NVENC_ARGS appear contiguously in our commands.
    for j in range(len(out) - len(NVENC_ARGS) + 1):
        if out[j:j + len(NVENC_ARGS)] == NVENC_ARGS:
            return out[:j] + LIBX264_ARGS + out[j + len(NVENC_ARGS):]
    return out
