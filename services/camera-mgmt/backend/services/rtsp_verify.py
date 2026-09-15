"""rtsp_verify.py — confirm an RTSP URL is actually playable, via ffprobe.

Mirrors the FFmpeg subprocess approach used by the main backend's snapshot
endpoint (backend/routers/cameras.py).  Returns True only if ffprobe finds a video
stream within the timeout.

`resolve_rtsp` adds the scheme-correcting retry: cameras that serve RTSP over
TLS still advertise plain rtsp:// through ONVIF, so the URL a scan stores can
never play until it's switched to rtsps://.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import structlog

from ..config import settings
from ..urlutil import host_of, to_rtsps
from . import tlsutil

log = structlog.get_logger(__name__)

_DEFAULT_RTSP_PORT = 554


async def verify_rtsp(url: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-rtsp_transport", "tcp",
            # Camera certificates are self-signed (see services/tlsutil.py), so
            # an rtsps:// probe must not fail on chain/hostname verification.
            # Inert for plain rtsp:// URLs.
            "-tls_verify", "0",
            "-timeout", "5000000",            # socket timeout (µs)
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=0",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.error("rtsp_verify.ffprobe_missing")
        return False

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.discovery_rtsp_timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False

    ok = proc.returncode == 0 and b"codec_name" in stdout
    return ok


async def resolve_rtsp(url: str) -> tuple[bool, str]:
    """Verify `url`, retrying over TLS when a plain rtsp:// URL won't play.

    Returns ``(playable, url)`` where the URL is the scheme that actually
    worked — callers should store *that*, not what they passed in.  On total
    failure the input URL comes back unchanged.

    The TLS retry is gated on a real handshake against the same host:port
    (milliseconds) rather than spent speculatively, so an unreachable or
    genuinely dead camera still costs one ffprobe timeout instead of two.
    """
    if await verify_rtsp(url):
        return True, url

    alt = to_rtsps(url)
    host = host_of(url)
    if alt is None or not host:
        return False, url

    port = urlparse(url).port or _DEFAULT_RTSP_PORT
    if await tlsutil.cert_fingerprint(host, port) is None:
        return False, url  # port doesn't speak TLS — nothing to retry

    if await verify_rtsp(alt):
        log.info("rtsp_verify.tls_scheme_corrected", host=host, port=port)
        return True, alt
    return False, url
