"""stream_codec.py — keep every camera on a VIDEO stream, never a JPEG one.

Live view is HLS, and HLS carries H.264/H.265 only. A camera registered on its
MJPEG profile records (the recorder stores whatever arrives) but can never play
live: MediaMTX refuses to build an HLS muxer for it, every tile 404s and reads
"Signal lost", and ffmpeg's re-stamp hop logs `Picture size 0x0 is invalid`
forever. Found live on KEC A5000 (2026-10-21): four Panasonic/i-PRO WV-X6533L,
whose FIRST ONVIF profile is JPEG — and the add wizard defaults to the first
profile, and `rtsp_verify` only asks "does it play?", which MJPEG does.

Two places use this:

* **Registration** (`routers/discovery.add_to_relay`) — the chosen profile is
  probed; a JPEG one is swapped for the best video profile the camera offers
  before the camera is stored.
* **Repair** (`repair_loop`, leader-only) — cameras ALREADY on a JPEG stream are
  moved to their best video profile by themselves, so an operator doesn't have
  to edit each one. Only when the camera answers and a video profile is found
  AND plays; otherwise nothing changes. Every switch is audited.

The choice is always probed, never assumed from a profile name: vendors name
profiles freely ("Profile_1", "JPEG", "stream1") and the codec is the only
thing that matters here.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import structlog

from . import substream

log = structlog.get_logger(__name__)

JPEG_CODECS = frozenset({"mjpeg", "jpeg", "mjpg", "jpg"})
# Ranked: H.264 plays everywhere (browsers stream-copy it); H.265 plays live in
# Safari and in Chrome/Edge with hardware HEVC, and is still far better than a
# stream that can never play.
_VIDEO_RANK = {"h264": 2, "avc1": 2, "hevc": 1, "h265": 1}

# Tried only when the camera's ONVIF profiles hold no video stream at all (some
# firmwares expose JPEG over ONVIF and video only on a vendor path). Probed like
# everything else, so a wrong guess simply doesn't play.
_VENDOR_VIDEO_PATHS = (
    "/MediaInput/h264/stream_1", "/MediaInput/h265/stream_1",   # Panasonic / i-PRO
    "/MediaInput/h264", "/MediaInput/h265",
)

REPAIR_EVERY_S = 30 * 60
# A camera the repair could not fix (unreachable, or offers no video stream) is
# not probed again for this long — each probe opens RTSP sessions on the camera.
_RETRY_AFTER_S = 6 * 3600
_last_tried: dict[str, float] = {}


def is_jpeg(codec: Optional[str]) -> bool:
    return (codec or "").lower() in JPEG_CODECS


def _strip_creds(url: str) -> str:
    return re.sub(r"^(rtsps?://)[^@/]+@", r"\1", url or "")


def _host_port(url: str) -> tuple[str, int]:
    m = re.match(r"^rtsps?://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", url or "")
    return (m.group(1), int(m.group(2) or 554)) if m else ("", 554)


async def best_video(camera: Any, exclude: Optional[str] = None) -> Optional[tuple[int, str, dict[str, Any]]]:
    """(candidate index or -1 for a vendor path, credential-free URL, probe) of the best VIDEO stream this camera
    offers — H.264 over H.265, then the most pixels. None when nothing answers with video."""
    creds = substream._creds_of(getattr(camera, "rtsp_url", None))
    seen: list[tuple[tuple[int, int], int, str, dict[str, Any]]] = []
    cands = list(getattr(camera, "rtsp_candidates", None) or [])
    for i, c in enumerate(cands):
        raw = str((c or {}).get("url_raw") or "")
        if not raw or raw == exclude:
            continue
        # A codec already learned (substream._write_back) saves a probe — but a JPEG verdict is final, a video one
        # is re-checked below since we're about to point the camera at it.
        known = (c or {}).get("codec")
        if known and is_jpeg(known):
            continue
        info = await substream.probe_stream(substream._with_creds(raw, creds))
        if info and info.get("codec") in _VIDEO_RANK:
            seen.append(((_VIDEO_RANK[info["codec"]], int(info.get("width") or 0) * int(info.get("height") or 0)), i, raw, info))
    if not seen:
        host, port = _host_port(getattr(camera, "rtsp_url", "") or "")
        scheme = "rtsps" if str(getattr(camera, "rtsp_url", "")).startswith("rtsps://") else "rtsp"
        for path in _VENDOR_VIDEO_PATHS if host else ():
            raw = f"{scheme}://{host}:{port}{path}"
            if raw == exclude:
                continue
            info = await substream.probe_stream(substream._with_creds(raw, creds))
            if info and info.get("codec") in _VIDEO_RANK:
                seen.append(((_VIDEO_RANK[info["codec"]], int(info.get("width") or 0) * int(info.get("height") or 0)), -1, raw, info))
                break                                   # first vendor path that plays is the right one
    if not seen:
        return None
    seen.sort(key=lambda s: s[0], reverse=True)
    _rank, idx, raw, info = seen[0]
    return idx, raw, info


async def video_url_for(camera: Any, full_url: str) -> tuple[str, Optional[dict[str, Any]]]:
    """At registration: `full_url` (with credentials) if it is video — else the best video stream's URL, with the
    same credentials. Returns (url, probe of the stream switched to | None when nothing changed)."""
    info = await substream.probe_stream(full_url)
    if not info or not is_jpeg(info.get("codec")):
        return full_url, None
    best = await best_video(camera, exclude=_strip_creds(full_url))
    if best is None:
        log.warning("stream_codec.jpeg_only", ip=getattr(camera, "ip", None))
        return full_url, None
    _idx, raw, vinfo = best
    log.info("stream_codec.switched_at_add", ip=getattr(camera, "ip", None), codec=vinfo.get("codec"),
             resolution=f"{vinfo.get('width')}x{vinfo.get('height')}")
    return substream._with_creds(raw, substream._creds_of(full_url)), vinfo


def drop_jpeg_sub(camera: Any) -> bool:
    """A stored sub track that is JPEG can't play live either (the grid asks for <slug>_sub). Drop it — unless it is
    being RECORDED: stopping a recording is the operator's call, so that one is only logged. True when dropped."""
    sub = getattr(camera, "sub_track", None)
    if not isinstance(sub, dict) or not is_jpeg(sub.get("codec")):
        return False
    if sub.get("recording_enabled"):
        log.warning("stream_codec.jpeg_sub_recorded", slug=getattr(camera, "slug", None))
        return False
    camera.sub_track = None
    return True


async def repair_camera(camera: Any) -> Optional[dict[str, Any]]:
    """If `camera` streams JPEG and offers video, point it at the video stream (in place, not committed). Returns
    {from_codec, to, codec, width, height} when switched, None otherwise."""
    url = getattr(camera, "rtsp_url", None) or ""
    if not url:
        return None
    main = await substream.probe_stream(url)
    if not main or not is_jpeg(main.get("codec")):
        return None                                     # video already, or unreachable now: nothing learned
    best = await best_video(camera, exclude=_strip_creds(url))
    if best is None:
        return None
    _idx, raw, info = best
    camera.rtsp_url = substream._with_creds(raw, substream._creds_of(url))
    return {"from_codec": main.get("codec"), "to": raw, "codec": info.get("codec"),
            "width": info.get("width"), "height": info.get("height")}


async def repair_all() -> int:
    """One pass over enabled cameras: move the ones on a JPEG stream to video. Returns how many were switched."""
    import time

    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera
    from . import audit as audit_svc
    from . import relay

    switched = 0
    async with AsyncSessionLocal() as db:
        cams = (await db.execute(select(Camera).where(Camera.enabled.is_(True)))).scalars().all()
        for cam in cams:
            if drop_jpeg_sub(cam):
                await db.commit()
                try:
                    from ..models import sub_recording_name
                    await relay.remove_path(sub_recording_name(cam.slug))
                except Exception as exc:                # noqa: BLE001 — the relay sync loop reconciles it
                    log.warning("stream_codec.sub_remove_failed", slug=cam.slug, error=str(exc))
                await audit_svc.record(None, None, "camera.sub_track_dropped", target=cam.slug,
                                       detail={"reason": "JPEG sub stream can't play live"})
            if time.time() - _last_tried.get(cam.slug, 0) < _RETRY_AFTER_S:
                continue
            try:
                change = await repair_camera(cam)
            except Exception as exc:                    # noqa: BLE001 — one camera must not stop the pass
                log.warning("stream_codec.repair_failed", slug=cam.slug, error=str(exc))
                change = None
            if change is None:
                _last_tried[cam.slug] = time.time()
                continue
            await db.commit()
            try:
                await relay.patch_path(cam.slug, cam.rtsp_url)
            except Exception as exc:                    # noqa: BLE001 — the relay sync loop re-applies it
                log.warning("stream_codec.relay_patch_failed", slug=cam.slug, error=str(exc))
            await audit_svc.record(None, None, "camera.stream_switched", target=cam.slug,
                                   detail={"reason": f"{change['from_codec']} can't play live",
                                           "to": change["to"], "codec": change["codec"],
                                           "resolution": f"{change['width']}x{change['height']}"})
            log.info("stream_codec.switched", slug=cam.slug, to_codec=change["codec"],
                     resolution=f"{change['width']}x{change['height']}")
            switched += 1
    return switched


async def repair_loop() -> None:
    """Leader-only: a pass at start-up, then every REPAIR_EVERY_S."""
    while True:
        try:
            await repair_all()
        except Exception as exc:                        # noqa: BLE001 — keep the loop alive
            log.warning("stream_codec.pass_failed", error=str(exc))
        await asyncio.sleep(REPAIR_EVERY_S)
