"""substream.py — find a camera's low-resolution second stream, and judge it.

Discovery answers "what exists". Recording answers "what is worth paying for",
and those are different questions: a second stream costs a second ffmpeg, a
second relay path and ~10-25% more disk, so it is only worth recording when it
actually makes playback cheaper.

The acquisition ladder, in order:

  1. **ONVIF profile** — the second discovered profile, already sitting in
     `rtsp_candidates` for any camera found over ONVIF.
  2. **Vendor derivation** — flip `subtype=0` to `subtype=1` on a Dahua/CP-Plus
     manual URL. Convention, not contract, which is why it is probed.
  3. **Manual** — an operator pastes one in.

**A substream's codec is never assumed, always probed.** The multi-stream plan
recorded the GPU server's Dahua fleet as all-HEVC and concluded no `-c copy`
path existed. Probing this appliance's CP-Plus cameras found H.264 on two of
them, including one of the two cameras that currently force a transcode. Guessing
from the vendor would have missed the best result available.

The usefulness test, straight from the plan: a sub is worth recording if it is
EITHER browser-safe (H.264, so playback can stream-copy it) OR materially
smaller than the main (so the transcode is cheaper). A same-size HEVC sub is
recorded nowhere — it is pure cost.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Codecs a browser plays without transcoding — the (a) half of the test.
BROWSER_SAFE = frozenset({"h264", "avc1"})
# A sub must have at most this share of the main's pixels to be worth recording
# on size alone — the (b) half. 0.5 is deliberately loose: 704x576 against
# 1920x1080 is 0.20, and 1280x720 against 1920x1080 is 0.44, so both qualify
# while a "sub" that is really a second full-size stream does not.
MAX_PIXEL_RATIO = 0.5

_PROBE_TIMEOUT_S = 25.0
# Seconds of stream captured to measure a candidate's real bitrate. Cameras
# under-report or omit `bit_rate` in RTSP metadata, and the number decides how
# much disk this costs forever — so it is measured, not read.
_BITRATE_SAMPLE_S = 4


def _creds_of(url: str | None) -> str | None:
    m = re.match(r"^rtsps?://([^@/]+)@", url or "")
    return m.group(1) if m else None


def _with_creds(raw: str, creds: str | None) -> str:
    if not creds:
        return raw
    m = re.match(r"^(rtsps?://)(.*)$", raw)
    return f"{m.group(1)}{creds}@{m.group(2)}" if m else raw


async def probe_stream(url: str) -> dict[str, Any] | None:
    """Codec/resolution/fps of `url`'s video stream, or None if unreachable.

    Note the absence of `-timeout`: on ffmpeg's RTSP demuxer that option means
    "wait for an incoming connection", so passing it puts ffprobe into listen
    mode and every probe fails with "Unable to open RTSP for listening". The
    wall-clock bound is the asyncio timeout instead.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
            # Camera certificates are self-signed (services/tlsutil.py), so an
            # rtsps:// probe must not fail on chain/hostname verification —
            # the same flag rtsp_verify.py has carried since discovery shipped.
            # Without it every probe of a TLS camera returned None, which reads
            # as "this camera offers no substream": the feature was not
            # degraded on rtsps:// cameras, it was absent, and silently.
            # Inert for plain rtsp:// URLs.
            "-tls_verify", "0",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
            "-of", "json", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # ffprobe absent from the image (FileNotFoundError is an OSError), or
        # the box is out of process slots. This used to escape the endpoint as
        # an unhandled 500; a probe that cannot run has simply learned nothing,
        # which is what None means everywhere else here.
        log.error("substream.ffprobe_unavailable", error=str(exc))
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return None
    try:
        st = json.loads(stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        return None
    num, _, den = (st.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = round(float(num) / float(den or 1), 2) or None
    except (ValueError, ZeroDivisionError):
        fps = None
    return {
        "codec": (st.get("codec_name") or "").lower(),
        "width": st.get("width"),
        "height": st.get("height"),
        "fps": fps,
    }


async def measure_bitrate(url: str) -> float | None:
    """Mbps actually delivered, by capturing a few seconds and weighing it.

    Why measure rather than read `stream=bit_rate`: these cameras frequently
    report nothing, and the figure decides storage cost for the life of the
    recording. Measured on this fleet, entry-g98a's 720p profile delivers
    2.41 Mbps against its 1080p main's 1.16 — a "sub" that costs twice the
    stream it is meant to relieve.
    """
    out = os.path.join(tempfile.gettempdir(), f"br-{uuid.uuid4().hex}.ts")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            # Same self-signed-certificate reason as probe_stream. Without it a
            # candidate that probed fine would measure no bitrate on an rtsps://
            # camera and be ranked by pixel count instead.
            "-tls_verify", "0",
            "-i", url, "-t", str(_BITRATE_SAMPLE_S), "-c", "copy", "-f", "mpegts",
            "-y", out,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.communicate(),
                                   timeout=_BITRATE_SAMPLE_S + _PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill(); await proc.communicate(); return None
        if not os.path.exists(out):
            return None
        return round(os.path.getsize(out) * 8 / _BITRATE_SAMPLE_S / 1e6, 2)
    except OSError:
        return None
    finally:
        try:
            os.path.exists(out) and os.unlink(out)
        except OSError:
            pass


def candidate_urls(camera: Any) -> list[tuple[str, str]]:
    """[(source, url_raw)] worth probing as a sub, best-guess order.

    Credential-free, like `rtsp_candidates` — they are composed at use time.
    """
    out: list[tuple[str, str]] = []
    for c in (getattr(camera, "rtsp_candidates", None) or [])[1:]:
        raw = c.get("url_raw")
        if raw:
            out.append(("onvif", raw))
    if not out:
        main = re.sub(r"^(rtsps?://)[^@/]+@", r"\1", getattr(camera, "rtsp_url", "") or "")
        # Dahua/CP-Plus convention. `%26` because some stored URLs have the
        # ampersand escaped.
        for a, b in (("subtype=0", "subtype=1"), ("subtype%3D0", "subtype%3D1")):
            if a in main:
                out.append(("derived", main.replace(a, b)))
                break
    return out


def judge(main: dict[str, Any] | None, sub: dict[str, Any] | None) -> tuple[bool, str]:
    """(worth recording, why) for a probed sub against a probed main.

    **The main's codec is the gate.** A sub track exists for exactly one reason:
    to spare playback the cost of converting a stream the browser cannot decode.
    If the main is already browser-safe it is served by stream-copy at no cost,
    there is no conversion to remove, and a sub is a second copy of every frame
    on disk in exchange for nothing. Bandwidth to the client is a real but
    different problem, and not one worth doubling a camera's storage for by
    default.

    Only once the main is known to need converting does the sub's own codec
    matter: browser-safe removes the conversion outright, materially smaller
    makes it cheaper, anything else is not worth a second stream.
    """
    if not sub or not sub.get("width"):
        return False, "not reachable"

    main_codec = (main or {}).get("codec")
    if not main_codec:
        # Not knowing whether the main needs converting means not knowing
        # whether a sub helps. Default to not spending the disk.
        return False, "main stream not probed — cannot tell whether a sub would help"
    if main_codec in BROWSER_SAFE:
        return False, (f"the main stream is already {main_codec} and plays as-is — "
                       "a sub would add storage without removing any conversion")

    if sub["codec"] in BROWSER_SAFE:
        return True, f"browser-safe {sub['codec']} — playback can stream-copy it"
    if not main.get("width"):
        return False, "main resolution unknown, cannot compare size"
    main_px = main["width"] * main["height"]
    sub_px = sub["width"] * sub["height"]
    if main_px and sub_px / main_px <= MAX_PIXEL_RATIO:
        return True, (f"{main_px // max(sub_px, 1)}x fewer pixels than main "
                      f"— cheaper to transcode")
    return False, (f"{sub['codec']} at {sub_px / max(main_px, 1):.0%} of the "
                   "main's pixels — no cheaper to serve, not worth a second stream")


def _write_back(camera: Any, seen: dict[str, dict[str, Any]]) -> None:
    """Record what each probed profile turned out to be, on the camera itself.

    Discovery stored only `{token, profile, url_raw, verified}`; selection needs
    codec and resolution, and until now those were probed and thrown away for
    every profile except the winner.
    """
    cands = getattr(camera, "rtsp_candidates", None)
    if not cands:
        return
    stamp = datetime.now(timezone.utc).isoformat()
    out = []
    for c in cands:
        info = seen.get(c.get("url_raw"))
        out.append(c if not info else {
            **c,
            "codec": info.get("codec"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "bitrate_mbps": info.get("bitrate_mbps"),
            "usable_as_sub": info.get("usable_as_sub"),
            "reason": info.get("reason"),
            "probed_at": stamp,
        })
    camera.rtsp_candidates = out


# Why `resolve_detailed` returned nothing. The caller has to tell these apart:
# only one of them is a fact about the camera.
UNREACHABLE = "unreachable"        # the main stream did not answer — transient
NO_USABLE_SUB = "no_usable_sub"    # camera answered; nothing it offers is worth it
RESOLVED = "ok"


async def resolve(camera: Any) -> dict[str, Any] | None:
    """Probe the ladder and return a `sub_track` dict, or None if none is useful.

    Thin wrapper over `resolve_detailed` for callers that only act on success
    (registration's background resolve). Anything that OVERWRITES a stored
    sub_track must use `resolve_detailed` instead and check the reason — see
    the note there.
    """
    resolved, _reason = await resolve_detailed(camera)
    return resolved


async def resolve_detailed(camera: Any) -> tuple[dict[str, Any] | None, str]:
    """`resolve`, plus WHY it came back empty — RESOLVED / UNREACHABLE /
    NO_USABLE_SUB.

    The distinction exists because a `None` return was being written straight
    over a working configuration. A camera that is briefly unreachable — a
    reboot, a flapping PoE port, the few seconds after a network blip — probes
    as "no sub", and storing that erased the sub track's URL and its
    `recording_enabled` flag. The reconcile loops then read the camera as having
    no sub and tore the recording down within one interval. Silent, permanent
    (the stored config is gone), and triggered by the one button an operator
    presses when a camera looks unwell.

    UNREACHABLE is decided by the MAIN stream, not by the candidates: if the
    main did not answer, nothing was learned about this camera's substreams and
    no conclusion about them may be stored.

    `recording_enabled` is **always false** here. Resolution records what the
    camera offers; switching it on is a separate, explicit act, because it
    starts a second recording on a live appliance.

    **Side effect:** every probed stream is also written back onto
    `camera.rtsp_candidates`, so the camera ends up carrying what each of its
    profiles actually is — codec, resolution, fps, measured bitrate — not just
    the one that won. It costs nothing (the probes have already happened) and it
    is what lets a future view ask for a different rendition, or an operator see
    why a profile was passed over. The list is reassigned rather than mutated:
    SQLAlchemy does not track in-place JSONB edits, so a mutated element would
    silently never reach the database. Both callers commit the camera.
    """
    creds = _creds_of(getattr(camera, "rtsp_url", None))
    main_raw = re.sub(r"^(rtsps?://)[^@/]+@", r"\1", getattr(camera, "rtsp_url", "") or "")
    main = await probe_stream(_with_creds(main_raw, creds)) if main_raw else None

    main_bitrate = await measure_bitrate(_with_creds(main_raw, creds)) if main_raw else None

    # What each probe found, keyed by the credential-free URL, so the results can
    # be folded back onto rtsp_candidates at the end.
    seen: dict[str, dict[str, Any]] = {}
    if main and main_raw:
        seen[main_raw] = {**main, "bitrate_mbps": main_bitrate}

    best: tuple[tuple[int, float], dict, str, str] | None = None
    for source, raw in candidate_urls(camera):
        probed = await probe_stream(_with_creds(raw, creds))
        ok, why = judge(main, probed)
        if ok:
            probed["bitrate_mbps"] = await measure_bitrate(_with_creds(raw, creds))
        if probed:
            seen[raw] = {**probed, "usable_as_sub": ok, "reason": why}
        log.info("substream.probe", slug=getattr(camera, "slug", None), source=source,
                 codec=(probed or {}).get("codec"), width=(probed or {}).get("width"),
                 height=(probed or {}).get("height"),
                 bitrate=(probed or {}).get("bitrate_mbps"), usable=ok, reason=why)
        if not ok:
            continue
        # Rank: a stream-copyable sub beats any transcoded one, because it
        # removes the conversion entirely rather than making it cheaper.
        #
        # Within a class, take the CHEAPEST — measured bitrate, falling back to
        # pixel count when the measurement failed. Every candidate in a class
        # costs the server the same to serve, so the only thing separating them
        # is the disk they consume forever, and a sub exists to be cheap.
        #
        # This rule replaced "among free candidates take the biggest picture",
        # which was wrong and measurably so: entry-g98a offers H.264 720p at
        # 2.41 Mbps and H.264 704x576 at 1.24 Mbps, both zero-transcode, against
        # a 1.16 Mbps HEVC main. Preferring the bigger picture chose the one
        # costing twice as much — 23.7 GB/day, 356 GB over its 15-day retention —
        # for a scrub thumbnail nobody asked to be sharper.
        px = (probed["width"] or 0) * (probed["height"] or 0)
        copyable = probed["codec"] in BROWSER_SAFE
        cost = probed.get("bitrate_mbps")
        rank = (2 if copyable else 1, -(cost if cost is not None else px / 1e6))
        if best is None or rank > best[0]:
            best = (rank, probed, source, raw)

    _write_back(camera, seen)

    if best is None:
        return None, (NO_USABLE_SUB if main else UNREACHABLE)
    _rank, probed, source, raw = best
    return {
        "url_raw": raw,
        "codec": probed["codec"],
        "width": probed["width"],
        "height": probed["height"],
        "fps": probed["fps"],
        # What this costs, and what it is being compared against. Stored so the
        # UI can state the storage bill in GB/day instead of hand-waving a
        # percentage that is wrong by 10x on the cameras that matter.
        "bitrate_mbps": probed.get("bitrate_mbps"),
        "main_bitrate_mbps": main_bitrate,
        "source": source,
        "verified": True,
        "recording_enabled": False,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }, RESOLVED


async def resolve_in_background(camera_id) -> None:
    """Probe and store a camera's sub track without holding up registration.

    The plan calls for resolving the sub *at registration*, and it has to be
    detached: probing opens each candidate stream in turn and was measured at up
    to 30 s on an ONVIF camera with four profiles. Doing that inline would make
    adding a camera feel broken.

    Stores only — `recording_enabled` stays false, so a newly registered camera
    never silently starts writing a second stream. It arrives with its options
    known, which is the point: an operator opening the Recording tab sees what
    the camera offers instead of an unexplained "check" button.

    Swallows everything. A camera that is unreachable, slow or simply has no
    second stream must still register successfully.
    """
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera

    try:
        async with AsyncSessionLocal() as db:
            camera = (await db.execute(
                select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
            if camera is None or camera.sub_track is not None:
                return
            resolved = await resolve(camera)
            if resolved is None:
                return
            # Re-read: probing took seconds, and an operator may have resolved
            # or edited it in the meantime. Theirs wins.
            camera = (await db.execute(
                select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
            if camera is None or camera.sub_track is not None:
                return
            camera.sub_track = resolved
            await db.commit()
            log.info("substream.auto_resolved", slug=camera.slug,
                     codec=resolved["codec"],
                     resolution=f"{resolved['width']}x{resolved['height']}")
    except Exception as exc:  # noqa: BLE001 — must never affect registration
        log.warning("substream.auto_resolve_failed", camera_id=str(camera_id),
                    error=str(exc))
