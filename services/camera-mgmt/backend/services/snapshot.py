"""snapshot.py — one JPEG from a camera's relay path, shared by everyone who asks.

TWO CALLERS, ONE FRAME. The config page's thumbnail / mask editor
(GET /cameras/{id}/snapshot) and the time-lapse capture loop
(services/timelapse.py) both want "the camera's picture now". They must get it
the same way, and that way has two rules this module exists to keep in one place:

  * THE RELAY, NEVER THE CAMERA (the single-pull rule). Every consumer reads
    MediaMTX's copy of the stream; a second RTSP session against a cheap camera
    is what knocks it over. `settings.internal_rtsp_url(slug)` is the API's own
    route to the relay.
  * ONE DECODE PER CAMERA AT A TIME. A Valkey lock serialises decodes and a
    10-second cache hands the result to everyone who arrived during it — so a
    time-lapse tick landing while an operator has the mask editor open costs
    nothing extra, and neither of them waits on a second FFmpeg.

Moved out of routers/cameras.py unchanged (2026-10, time-lapse) — the passes,
the flat-frame threshold and the timeouts are the measured values they were.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .. import redis_client
from ..config import settings


class SnapshotBusy(Exception):
    """Someone else held the decode for longer than a caller is willing to wait."""


# A grey/green "frame" is a real decode of a P-frame whose reference picture was
# never received — it happens whenever FFmpeg joins the relay mid-GOP. Such a
# frame is near-uniform, so it compresses to almost nothing: a genuine 720p+
# camera frame at q:v 4 is tens of KB, a flat one is 1–3 KB. Anything under this
# is treated as a miss and retried with a different frame-selection strategy.
_MIN_PLAUSIBLE_JPEG_BYTES = 8_000

_SNAPSHOT_TIMEOUT_SEC = 12.0
_SNAPSHOT_WAIT_SEC = 10.0        # how long a caller waits on someone else's decode


# Two frame-selection strategies, tried in order. Each is (input options, output
# options) — FFmpeg needs decoder flags before -i, filters after it.
_SNAPSHOT_PASSES: list[tuple[list[str], list[str]]] = [
    # Keyframes only. An IDR is self-contained, so this is the one strategy that
    # cannot return a grey frame. Costs up to one GOP of wait — measured at
    # 1.9–3.0 s on the cameras here, well inside the timeout.
    (["-skip_frame", "nokey"], []),
    # Fallback for a stream we never see a keyframe on within the timeout: decode
    # normally and discard the first 15 pictures. Partial-grey is possible (P
    # deltas painted over a grey reference) but a partial frame is still enough
    # to place a mask or zone against.
    ([], ["-vf", "select=gte(n\\,15)"]),
]


async def grab_frame(local_url: str, pre_input: list[str], post_input: list[str]) -> bytes | None:
    """Run one FFmpeg frame-grab. Returns JPEG bytes, or None if it produced nothing."""
    # asyncio.create_subprocess_exec avoids blocking the event loop. We read from
    # the local RTSP relay rather than the raw camera URL so this works even if
    # the camera requires VPN access.
    # -rtsp_transport ONLY for rtsp://. The same helper now also grabs from
    # MediaMTX's HLS, and handing an HTTP input an RTSP-only option makes
    # ffmpeg complain about an unused option on some builds.
    transport = ["-rtsp_transport", "tcp"] if local_url.startswith("rtsp://") else []
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        *transport,
        *pre_input,
        "-i", local_url,
        *post_input,
        "-an",
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-q:v", "4",             # JPEG quality 1-31, lower = better
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SNAPSHOT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0 or not stdout:
        return None
    return stdout


async def decode_snapshot(local_url: str, slug: str | None = None) -> bytes | None:
    """Decode one JPEG, escalating to the next pass if the frame looks flat.

    HLS IS TRIED FIRST (when `slug` is given), AND IT IS WORTH ROUGHLY FIVE
    TIMES THE SPEED. Dialling RTSP for a single frame pays a connection that is
    thrown away immediately: measured on this appliance, DESCRIBE/SETUP/PLAY
    alone is ~1.15 s, and then the keyframe-only pass waits up to one GOP on
    top — 2.3–3.0 s in total for one picture. MediaMTX already holds the stream
    open and is already cutting it into 2 s segments, so reading the live edge
    of its playlist skips the handshake and the keyframe wait entirely:
    0.41–0.61 s for a full 1920x1080 frame, verified against the RTSP path at
    the same moment.

    `-live_start_index -1` is load-bearing. ffmpeg's HLS demuxer defaults to
    three segments back from the live edge, which would hand the operator a
    picture six seconds stale; -1 starts at the last segment, so the frame is
    at most one segment (2 s) behind — less than the RTSP path's own latency,
    and well inside the 10 s shared cache the time-lapse loop already accepts.

    RTSP REMAINS THE FALLBACK and is unchanged. HLS is absent for a camera
    MediaMTX has not published yet, and the playlist can lag a stream that has
    only just come up, so a thin or missing HLS answer must cost nothing.

    What is deliberately NOT done: `-fflags nobuffer -flags low_delay`. It cuts
    HLS to 0.27 s but returns a 19 KB partial picture — the same grey-frame
    failure the RTSP fallback pass exists to mop up. Speed is not worth a
    broken frame here.

    (HLS-first came from the `manual` branch's routers/cameras.py; it lands here
    because this module is now the one place a frame is grabbed.)
    """
    best: bytes | None = None

    if slug:
        hls_url = f"{settings.mediamtx_hls_url.rstrip('/')}/{slug}/index.m3u8"
        jpeg = await grab_frame(hls_url, ["-live_start_index", "-1"], [])
        if jpeg and len(jpeg) >= _MIN_PLAUSIBLE_JPEG_BYTES:
            return jpeg
        best = jpeg or best

    for pre_input, post_input in _SNAPSHOT_PASSES:
        jpeg = await grab_frame(local_url, pre_input, post_input)
        if jpeg and len(jpeg) >= _MIN_PLAUSIBLE_JPEG_BYTES:
            return jpeg
        best = jpeg or best
    return best  # a flat frame still beats no frame at all


async def shared_snapshot(slug: str, wait_s: float = None) -> Optional[bytes]:
    """The camera's current frame: cached if someone decoded it in the last
    ~10 s, otherwise decoded now under the per-camera lock.

    Returns None when the stream produced nothing (offline, wrong codec).
    Raises SnapshotBusy when another decode held the lock past `wait_s` —
    the HTTP caller turns that into a 503, the time-lapse loop just retries on
    its next tick.
    """
    wait = _SNAPSHOT_WAIT_SEC if wait_s is None else wait_s
    cached = await redis_client.get_cached_snapshot(slug)
    if cached:
        return cached

    if not await redis_client.acquire_snapshot_lock(slug):
        # Someone else is decoding — poll for their result instead of failing.
        deadline = asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            cached = await redis_client.get_cached_snapshot(slug)
            if cached:
                return cached
        raise SnapshotBusy(slug)

    # Pull via the API's own route to the relay — differs from the user-facing
    # URL under the bridge overlay (mediamtx is another container there).
    local_url = settings.internal_rtsp_url(slug)
    try:
        jpeg = await decode_snapshot(local_url, slug)
        if jpeg:
            await redis_client.set_cached_snapshot(slug, jpeg)
        return jpeg
    finally:
        # Release explicitly: the TTL is only a crash backstop. Holding it for
        # its full window was what starved every follow-up request.
        await redis_client.release_snapshot_lock(slug)
