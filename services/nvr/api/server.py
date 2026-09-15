"""
REST API for clip extraction and system status.

Design decisions:
- FastAPI: async support, automatic OpenAPI docs, excellent validation.
- Clip extraction uses `ffmpeg -c copy` (remux, no transcode) for speed.
  A 20-second clip from MPEG-TS segments takes <1 second to extract.
- Output format is fragmented MP4 (`-movflags +frag_keyframe+empty_moov+
  default_base_moof`): the init moov is written up front AND media is emitted
  in self-contained fragments, so playback starts immediately AND a clip whose
  ffmpeg was interrupted (killed under load, or a truncated/lossy source
  segment) is still playable up to the last complete fragment — instead of a
  0-byte-moov file that hangs the player ("moov atom not found").
- Clips are written to a temp directory and served via FileResponse.
  A retention worker deletes clips after clip_ttl_minutes.
- Concurrency: each clip request spawns one ffmpeg process. On a typical
  server, 10-20 concurrent extractions are feasible (I/O-bound, not CPU).
  For higher concurrency, add a semaphore or queue.

Edge cases handled:
- Camera not found → 404
- Timestamp outside recorded range → 404 with available range info
- Timestamp in a gap between segments → returns best-effort clip with
  whatever footage is available in the window, and warns in headers
- Clip extraction failure → 500 with ffmpeg stderr
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from api import encoders, hls
from storage import capacity
from storage.index import SegmentIndex

logger = logging.getLogger(__name__)

# NVENC availability, probed once in create_app (an actual test encode — see
# api/encoders.py). False until proven; the CPU path is always the fallback.
_nvenc_ok: bool = False

# Module-level references set by create_app()
_index: SegmentIndex | None = None
_clips_path: Path | None = None
_hls_cache: hls.HlsCache | None = None
_annotations_path: Path | None = None
_engine = None  # RecordingEngine, set after creation
# Live, restart-surviving global size cap (StorageSettings). Read fresh on every
# /storage call so a UI edit is reflected immediately.
_storage_settings = None  # storage.settings.StorageSettings, set by create_app
# Footage volume, used to bound the size cap by real free space rather than by
# an arbitrary constant. None = unknown, and the bound is then not enforced.
_storage_path: Path | None = None
# Storage-pressure evaluator (storage/alerts.py). None = not wired.
_alerter = None
_segment_duration: float = 60.0
# Appliance groom-after default (days; 0 = disabled) — surfaced in /storage so
# the UI can show the effective policy for cameras without an override.
_groom_default_days: int = 0

# Cap annotation upload size. Annotations are full-frame JPEGs at source
# resolution (typically 0.3–0.8 MB at 1080p, q=92). 10 MB is generous and
# also defends against accidental wrong-file uploads.
_MAX_ANNOTATION_BYTES = 10 * 1024 * 1024
_ANNOTATION_ALLOWED_MIME = {"image/jpeg", "image/jpg"}

# Per-camera video codec cache. Browsers (notably Chrome on Linux) can't play
# HEVC/H.265 in MP4, so for HEVC sources we transcode the clip to H.264.
#
# "Codec is fixed by camera config, so a one-shot ffprobe per camera is enough"
# stopped being true when sub tracks arrived. Re-probing a camera can swap
# `<slug>_sub` onto a different profile — H.264 to HEVC — under the same
# recording name. A cache with no expiry then serves raw HEVC through the `ts`
# path as though it were H.264, and the player shows black with no error, for
# as long as the process lives.
#
# So entries carry the time they were probed and are re-checked after
# _CODEC_CACHE_TTL. The TTL is the backstop, not the mechanism: camera-mgmt
# drops an entry explicitly (DELETE /cameras/{camera}/codec-cache) the moment it
# changes a track's source, and the TTL only bounds how wrong this can get when
# something changes a stream WITHOUT telling us. One ffprobe per camera per
# five minutes is nothing against a black player nobody can explain.
_CODEC_CACHE_TTL = 300.0
_codec_cache: dict[str, tuple[str, int | None, int | None, float]] = {}

# Semaphore that caps simultaneous ffmpeg/ffprobe processes spawned by clip
# extraction. HEVC → H.264 transcoding is CPU-bound (libx264); without a cap,
# N concurrent requests spawn N libx264 processes that saturate all cores and
# starve the continuous RTSP recording workers.
#
# SIZED TO THE ACTIVE ENCODER, not fixed. A hardware encoder puts the work on a
# dedicated ASIC, so the CPU is not the constraint and the historical 4 stands.
# libx264 is a different machine entirely, and the difference is the difference
# between "playback starts slowly" and "playback stalls mid-segment": a segment
# must transcode faster than it plays or the player runs dry.
#
# Measured on this appliance (60 s HEVC 1080p segment, libx264 veryfast,
# taskset-pinned), per-segment throughput against realtime:
#
#     4 cores, 1 concurrent :  6.08 s   9.86x realtime
#     4 cores, 4 concurrent : 21.46 s   2.80x realtime
#     2 cores, 4 concurrent : 38.59 s   1.55x realtime
#
# Those cores are fast. The reference 4-vCPU appliance in the multi-stream plan
# measured 2.24x realtime for a single transcode, against 9.86x here — i.e.
# ~4.4x slower per core. Scaling by that factor, a single transcode there lands
# at 2.24x (which is exactly the plan's own measured number, so the factor is
# self-consistent), two land near 1.1x, and four land at ~0.64x — under realtime,
# where playback stalls.
#
# Hence one CPU transcode per 4 cores: it keeps every job above 2x realtime even
# on that slow reference hardware, and it degrades to 1 rather than 0 on a tiny
# box. An explicit recording.max_concurrent_extractions always wins — this is a
# default for operators who have not measured their own hardware, not a policy.
_clip_semaphore: asyncio.Semaphore | None = None
_clip_semaphore_limit: int = 4
# How the limit was arrived at, surfaced in /health so a silently small cap is
# diagnosable rather than mysterious.
_clip_semaphore_basis: str = "default"

# Slots when the encode is offloaded to hardware; the CPU is not the constraint.
_HW_EXTRACTION_SLOTS = 4
# Cores per concurrent CPU transcode (see the reasoning above).
_CORES_PER_CPU_EXTRACTION = 4


def _available_cores() -> int:
    """Cores this process may actually use — not what the host happens to have.

    `os.cpu_count()` reports the machine, and the NVR runs in a container. On a
    box started with `--cpus=2` it still answers 24, which would size the
    extraction cap for hardware that isn't there — the precise deployment this
    sizing exists to protect. So consult, and take the smallest of:

      * the cgroup CPU quota (`--cpus`), v2 then v1;
      * the CPU affinity mask (`--cpuset-cpus`);
      * the machine's core count, as the last resort.
    """
    limits = []
    try:  # cgroup v2
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max" and float(period) > 0:
            limits.append(int(float(quota) / float(period)))
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            limits.append(quota // period)
    except (OSError, ValueError):
        pass
    try:  # cpuset / taskset
        limits.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    limits.append(os.cpu_count() or 1)
    return max(1, min(n for n in limits if n and n > 0))


def _size_extraction_slots(configured: int | None, nvenc_ok: bool) -> tuple[int, str]:
    """(slots, why). `configured` is an explicit operator value, or None."""
    if configured is not None:
        return max(1, int(configured)), "configured"
    if nvenc_ok:
        return _HW_EXTRACTION_SLOTS, "hardware encoder (nvenc)"
    cores = _available_cores()
    slots = max(1, min(cores // _CORES_PER_CPU_EXTRACTION, _HW_EXTRACTION_SLOTS))
    return slots, f"cpu encoder, {cores} core(s)"

# Longest clip /clip will extract, in seconds. Configured via
# create_app(max_clip_seconds=...) / recording.max_clip_seconds.
#
# This used to be `le=1800` on `before` and `after` — a 1-hour ceiling, chosen
# for the AI-pipeline windows the parameters were added for ("30s before for
# event context"). It also silently governed EVIDENCE EXPORT, whose windows are
# set by a case and not by a pipeline: a 3-hour export asked for before=after=
# 5400, FastAPI rejected it 422 before any code ran, and camera-mgmt reported
# "no recording in that window, or the NVR was unreachable" — sending the
# operator to hunt for missing footage that was there all along.
#
# The ceiling below is only a sanity bound for the query parameters; the real,
# configurable limit is checked in the body so that config governs it and the
# caller is told the actual number.
_max_clip_seconds: float = 6 * 3600.0
_CLIP_SECONDS_CEILING = 86400.0

# Hard ceiling on a single ffmpeg extraction, whatever the clip length implies.
# One extraction holds one of _clip_semaphore_limit slots; a wedged process must
# not hold one indefinitely.
_MAX_EXTRACTION_SECONDS = 3600.0

# Codecs that all major browsers can play in MP4 with no transcoding.
_BROWSER_SAFE_CODECS = {"h264"}

# Camera names become directory names under /data/nvr/<name>/. Constrain to
# safe characters so an attacker (or a typo) can't traverse paths or break
# the segment-filename regex (`{camera}_YYYYMMDD_HHMMSS.ts`).
_CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_camera_name(name: str) -> None:
    """Raise HTTPException(400) if `name` isn't a safe camera identifier."""
    if not _CAMERA_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Invalid camera name",
                "detail": (
                    "Must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ "
                    "(letters, digits, '_' or '-'; 1–64 chars; must not "
                    "start with '_' or '-')."
                ),
            },
        )


def create_app(index: SegmentIndex, clips_path: str | Path,
               engine=None, max_concurrent_extractions: int | None = None,
               lifespan=None,
               annotations_path: str | Path | None = None,
               storage_settings=None,
               storage_path: str | Path | None = None,
               alerter=None,
               segment_duration: float = 60.0,
               groom_default_days: int = 0,
               max_clip_seconds: float = 6 * 3600.0) -> FastAPI:
    global _index, _clips_path, _annotations_path, _engine, _hls_cache
    global _clip_semaphore, _clip_semaphore_limit, _clip_semaphore_basis
    global _max_clip_seconds
    global _storage_settings, _segment_duration, _groom_default_days
    global _storage_path, _alerter
    _index = index
    _clips_path = Path(clips_path)
    _clips_path.mkdir(parents=True, exist_ok=True)
    if annotations_path is None:
        # Default to a sibling of clips_path so retention can find it later.
        annotations_path = Path(clips_path).parent / "annotations"
    _annotations_path = Path(annotations_path)
    _annotations_path.mkdir(parents=True, exist_ok=True)
    _engine = engine
    _max_clip_seconds = float(max_clip_seconds)
    _storage_settings = storage_settings
    # Falls back to the clips dir's parent, which is where main.py puts clips —
    # same volume, so the free-space probe is right even if the caller omits it.
    _storage_path = Path(storage_path) if storage_path else _clips_path.parent
    _alerter = alerter
    _segment_duration = float(segment_duration)
    _groom_default_days = int(groom_default_days or 0)

    # Probe NVENC once at startup (a real test encode). When the GPU overlay
    # granted this container video capability, HEVC→H.264 clip transcodes run
    # on the GPU's encoder block instead of libx264 — on a shared appliance
    # the difference is "playback works" vs "4 cores pinned per seek".
    global _nvenc_ok
    _nvenc_ok = encoders.probe_nvenc()

    # Sized after the probe, because the answer depends on it.
    _clip_semaphore_limit, _clip_semaphore_basis = _size_extraction_slots(
        max_concurrent_extractions, _nvenc_ok)
    _clip_semaphore = asyncio.Semaphore(_clip_semaphore_limit)
    logger.info("Extraction concurrency: %d slot(s) — %s",
                _clip_semaphore_limit, _clip_semaphore_basis)

    # HLS artifact cache. Lives under the clips dir so it shares the volume the
    # retention worker already watches, and is handed the encoder ladder and
    # the extraction slot rather than importing either — see api/hls.py.
    _hls_cache = hls.HlsCache(
        _clips_path / "hls",
        transcode_args=lambda: encoders.transcode_args(_nvenc_ok),
        # Same per-job degradation /clip does: one bad NVENC job costs CPU
        # rather than failing the segment.
        cpu_transcode_args=lambda: encoders.transcode_args(False),
        slot=_extraction_slot,
    )

    app = FastAPI(
        title="Smart NVR API",
        description="REST API for AI-pipeline clip extraction from continuous RTSP recordings",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Allow the shared "common" frontend (served from one NVR box) to call the
    # other NVR backends cross-origin. Internal tool on a trusted LAN, so the
    # origin allowlist is permissive; tighten to specific origins if exposed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-NVR-Coverage", "X-NVR-HLS-Mode", "X-NVR-Source-Codec"],
    )

    app.add_api_route("/clip", get_clip, methods=["GET"])
    app.add_api_route("/hls/{camera}/index.m3u8", hls_playlist, methods=["GET"])
    app.add_api_route("/hls/{camera}/{mode}/{name}", hls_segment, methods=["GET"])
    app.add_api_route("/snapshot", get_snapshot, methods=["GET"])
    app.add_api_route("/cameras", list_cameras, methods=["GET"])
    app.add_api_route("/cameras/{camera}/range", get_camera_range, methods=["GET"])
    app.add_api_route("/cameras/{camera}/recordings", purge_recordings, methods=["DELETE"])
    app.add_api_route("/cameras/{camera}/recordings/range", erase_recordings_range, methods=["DELETE"])
    app.add_api_route("/cameras/{camera}/retention", set_camera_retention, methods=["PUT"])
    app.add_api_route("/cameras/{camera}/groom", set_camera_groom, methods=["PUT"])
    app.add_api_route("/cameras/{camera}/codec-cache", invalidate_codec_cache,
                      methods=["DELETE"])
    app.add_api_route("/cameras/{camera}", add_camera, methods=["POST"])
    app.add_api_route("/cameras/{camera}", remove_camera, methods=["DELETE"])
    app.add_api_route("/health", health_check, methods=["GET"])
    app.add_api_route("/storage", storage_stats, methods=["GET"])
    app.add_api_route("/storage/limit", set_storage_limit, methods=["PUT"])
    app.add_api_route("/annotations", save_annotation, methods=["POST"])
    app.add_api_route("/coverage", coverage, methods=["GET"])
    app.add_api_route("/report/uptime", uptime_report, methods=["GET"])

    # No UI here: the NVR is headless. The product UI is the unified SPA at
    # the repo root (frontend/), served by the camera-mgmt API, which reaches
    # this service through its authenticated /api/nvr proxy.

    return app


async def get_clip(
    camera: str = Query(..., description="Camera name"),
    timestamp: str = Query(..., description="ISO 8601 timestamp (e.g. 2026-03-17T14:32:10Z)"),
    before: float = Query(10.0, description="Seconds before timestamp",
                          ge=0, le=_CLIP_SECONDS_CEILING),
    after: float = Query(10.0, description="Seconds after timestamp",
                         ge=0, le=_CLIP_SECONDS_CEILING),
    format: str = Query("mp4", description="Output format", enum=["mp4"]),
):
    """Extract a video clip centered on the given timestamp.

    Default: 10 seconds before + 10 seconds after = 20-second clip.
    The `before` and `after` parameters allow AI pipelines to request
    custom windows (e.g., 30s before for event context analysis).
    """
    # Parse timestamp
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        ts_epoch = ts.timestamp()
    except ValueError:
        raise HTTPException(400, f"Invalid timestamp format: {timestamp}. Use ISO 8601.")

    # Validate camera exists in index
    cameras = _index.get_cameras()
    if camera not in cameras:
        raise HTTPException(404, {
            "error": f"Camera '{camera}' not found",
            "available_cameras": cameras,
        })

    # Compute clip window
    clip_start = ts_epoch - before
    clip_end = ts_epoch + after
    clip_duration = before + after

    # Reject zero-length windows up-front. ffmpeg would fail noisily otherwise.
    if clip_duration <= 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Clip duration must be greater than 0",
                "detail": "Set 'before' and/or 'after' to a positive value.",
            },
        )

    # Say the limit and the request, so a caller can act on the answer. A bare
    # 422 on one parameter told an evidence export nothing it could use.
    if clip_duration > _max_clip_seconds:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Requested clip is longer than this NVR will extract",
                "requested_seconds": round(clip_duration, 1),
                "max_seconds": round(_max_clip_seconds, 1),
                "detail": (
                    f"Asked for {clip_duration / 3600:.2f} h; the limit is "
                    f"{_max_clip_seconds / 3600:.2f} h. Split the window, or "
                    "raise recording.max_clip_seconds."
                ),
            },
        )

    # Check recording range
    rec_range = _index.get_recording_range(camera)
    if rec_range is None:
        raise HTTPException(404, {
            "error": f"No recordings found for camera '{camera}'",
        })

    earliest, latest = rec_range
    if clip_end < earliest or clip_start > latest:
        raise HTTPException(404, {
            "error": "Requested timestamp is outside recorded range",
            "recorded_range": {
                "earliest": datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat(),
                "latest": datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(),
            },
            "requested": timestamp,
        })

    # Find overlapping segments
    segments = _index.find_segments(camera, clip_start, clip_end)
    if not segments:
        raise HTTPException(404, {
            "error": "No segments cover the requested time window (gap in recording)",
            "requested_window": {
                "start": datetime.fromtimestamp(clip_start, tz=timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(clip_end, tz=timezone.utc).isoformat(),
            },
        })

    # Verify segment files exist
    segments = [s for s in segments if os.path.exists(s.filepath)]
    if not segments:
        raise HTTPException(500, "Segment files missing from disk")

    # Build ffmpeg command for clip extraction
    clip_filename = f"{camera}_{int(ts_epoch)}_{uuid.uuid4().hex[:8]}.mp4"
    clip_path = _clips_path / clip_filename

    # Enforce extraction concurrency limit. libx264 transcoding is CPU-bound;
    # reject immediately (503) rather than queuing — queued requests would all
    # complete slowly and still saturate the CPU.
    if _clip_semaphore is not None and _clip_semaphore.locked():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Clip extraction at capacity",
                "detail": (
                    f"All {_clip_semaphore_limit} extraction slots are busy. "
                    "Retry in a few seconds."
                ),
            },
            headers={"Retry-After": "5"},
        )

    try:
        async with _clip_semaphore:
            await _extract_clip(segments, clip_start, clip_duration, str(clip_path))
    except ClipExtractionError as e:
        raise HTTPException(500, {
            "error": "Clip extraction failed",
            "detail": str(e),
        })

    if not clip_path.exists() or clip_path.stat().st_size == 0:
        raise HTTPException(500, "Clip extraction produced empty file")

    # Check if there were gaps in coverage
    headers = {}
    coverage = _compute_coverage(segments, clip_start, clip_end)
    if coverage < 1.0:
        headers["X-NVR-Coverage"] = f"{coverage:.2f}"
        headers["X-NVR-Warning"] = "Partial coverage: recording gap in requested window"

    return FileResponse(
        path=str(clip_path),
        media_type="video/mp4",
        filename=clip_filename,
        headers=headers,
    )


@asynccontextmanager
async def _extraction_slot():
    """Hold one of the extraction semaphore's slots for the duration of a body.

    `/clip` *rejects* with 503 when the semaphore is full: a queued clip would
    finish slowly and still saturate the CPU. The HLS transcode path queues
    instead, deliberately — the player asks for one segment at a time and is
    already buffering the one before it, so waiting for a slot degrades to a
    pause, while a 503 mid-playlist stops playback outright.
    """
    if _clip_semaphore is None:
        yield
        return
    async with _clip_semaphore:
        yield


async def hls_playlist(
    camera: str,
    from_ts: str = Query(..., alias="from", description="ISO 8601 window start"),
    to_ts: str = Query(..., alias="to", description="ISO 8601 window end"),
    hevc: bool = Query(False, description="Client can decode HEVC (MSE probe)"),
):
    """HLS VOD playlist over the recorded segments in [from, to].

    No ffmpeg runs here — this is an index query rendered as m3u8. The player
    then fetches only the segments it actually reaches, which is the whole
    point: seeking stops costing a transcode.

    Segment URIs are *relative* (`<mode>/<name>`), so they resolve against
    whatever path the playlist was served from. The NVR sits behind
    camera-mgmt's authenticated proxy and has no idea what its external mount
    point is; relative URIs mean it never needs to.
    """
    _validate_camera_name(camera)
    if _index is None:
        raise HTTPException(500, "Index not available")
    try:
        from_epoch = datetime.fromisoformat(from_ts.replace("Z", "+00:00")).timestamp()
        to_epoch = datetime.fromisoformat(to_ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise HTTPException(
            400, f"Invalid timestamp format: use ISO 8601 (got from={from_ts!r} to={to_ts!r})")
    if to_epoch <= from_epoch:
        raise HTTPException(400, "'from' must be earlier than 'to'")
    # Same ceiling as /clip: one playlist is one bounded ask for footage, and
    # an unbounded window would render tens of thousands of segment lines.
    if (to_epoch - from_epoch) > _max_clip_seconds:
        raise HTTPException(400, {
            "error": "Requested window exceeds the playlist limit",
            "requested_seconds": round(to_epoch - from_epoch, 1),
            "max_seconds": round(_max_clip_seconds, 1),
        })

    cameras = _index.get_cameras()
    if camera not in cameras:
        raise HTTPException(404, {
            "error": f"Camera '{camera}' not found",
            "available_cameras": cameras,
        })

    # ── track selection ──────────────────────────────────────────────────
    #
    # A camera may have a low-resolution sub track recorded alongside its main,
    # under `<slug>_sub`. Both are just names in the index here, so this ranks
    # whichever exist and serves the best one.
    #
    # "The track exists" is NOT "the track has footage at this time", and that
    # is a hard contract rather than a hope: a sub enabled last week has no
    # footage from last month, and recording gaps happen on either track
    # independently. So each candidate is checked for actual segments in the
    # requested window and skipped if it has none — which is also why the sub
    # can be switched on at any time without breaking playback of older footage.
    #
    # Coverage is measured, not merely tested for. Ranking on cost alone picks
    # the cheapest track holding ANY segment in the window, so a sub switched on
    # at 14:00 wins a 13:00-15:00 request and the main's complete first hour is
    # served as a gap. Coverage decides first and cost breaks the tie, which
    # keeps the cheap-track optimisation for the case it was written for — both
    # tracks covering the window — without ever trading footage for it.
    candidates = []
    for track in (f"{camera}{hls.SUB_SUFFIX}", camera):
        segs = [s for s in _index.find_segments(track, from_epoch, to_epoch)
                if os.path.exists(s.filepath)]
        if not segs:
            continue
        codec, width, height = await _get_stream_info(track, segs[0].filepath)
        mode = hls.choose_mode(codec, hevc)
        covered = hls.covered_seconds(segs, from_epoch, to_epoch)
        candidates.append((hls.rank_track(mode, width, height), covered,
                           (track, segs, mode, codec)))

    if not candidates:
        rng = _index.get_recording_range(camera)
        raise HTTPException(404, {
            "error": "No segments cover the requested time window (gap in recording)",
            "requested_window": {
                "start": _epoch_to_iso(from_epoch),
                "end": _epoch_to_iso(to_epoch),
            },
            "recorded_range": {
                "earliest": _epoch_to_iso(rng[0]) if rng else None,
                "latest": _epoch_to_iso(rng[1]) if rng else None,
            },
        })

    _rank, covered, (track, segments, mode, codec) = hls.select_track(candidates)

    # Segment URIs resolve against the playlist's own path. When the chosen
    # track is not the camera itself, step up and across to its own path — the
    # segment route is per recording name, and `<slug>_sub`'s segments live
    # under `<slug>_sub`.
    base = f"{mode}/" if track == camera else f"../{track}/{mode}/"
    body = hls.build_playlist(segments, mode, base)
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={
            # Which of the three delivery paths this camera landed on, so a
            # silent fall to the expensive one (h264 = per-segment transcode)
            # is visible in the UI and in a HAR, not a mystery.
            "X-NVR-HLS-Mode": mode,
            "X-NVR-Source-Codec": codec or "unknown",
            # Which track is actually being served — "the sub is enabled" and
            # "the sub is what you are watching" are different claims, and only
            # this one is checkable.
            "X-NVR-Track": "sub" if track != camera else "main",
            # How much of the requested window the served track actually covers.
            # A playlist shorter than the ask is now always a real recording gap
            # rather than a track-selection artefact, and this is what says so.
            "X-NVR-Track-Coverage": f"{covered:.1f}/{to_epoch - from_epoch:.1f}s",
            # Footage is groomed and erased underneath us; a stale playlist
            # points at segments that are gone.
            "Cache-Control": "no-store",
        },
    )


async def hls_segment(camera: str, mode: str, name: str):
    """Serve one playlist artifact: a raw .ts, or a cached init.mp4 / .m4s.

    The index row is re-checked on every request rather than trusting the
    playlist. That is what makes erasure real: a DSR deletes the row and the
    file, and the next fetch of an already-built artifact 404s instead of
    replaying footage from cache.
    """
    _validate_camera_name(camera)
    if mode not in hls.MODES:
        raise HTTPException(404, f"Unknown HLS mode: {mode!r}")
    if _index is None or _hls_cache is None:
        raise HTTPException(500, "Index not available")
    try:
        stem, sig, kind = hls.parse_artifact(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # The stem carries its own camera prefix; refuse a request that pairs one
    # camera's path with another's segment.
    if not stem.startswith(f"{camera}_"):
        raise HTTPException(404, "Segment does not belong to this camera")

    seg = _index.find_by_filename(camera, f"{stem}.ts")
    if seg is None or not os.path.exists(seg.filepath):
        raise HTTPException(404, "Segment is no longer available")
    if hls.source_signature(seg.filepath) != sig:
        # The source was rewritten since the playlist was built — grooming
        # turns a segment keyframe-only in place. 409 rather than 404 so the
        # client knows to re-fetch the playlist instead of giving up.
        raise HTTPException(409, "Segment changed on disk — reload the playlist")

    headers = {"Cache-Control": "no-store"}
    if mode == hls.MODE_TS:
        if kind != "ts":
            raise HTTPException(404, "Not available in ts mode")
        # H.264 in MPEG-TS: the recorded file is already the HLS segment, so
        # this is a plain file read. No ffmpeg, no semaphore, no cache.
        return FileResponse(seg.filepath, media_type="video/mp2t", headers=headers)

    if kind == "ts":
        raise HTTPException(404, f"Not available in {mode} mode")
    try:
        path = await _hls_cache.get(camera, mode, stem, sig, kind,
                                    seg.filepath, seg.duration)
    except hls.HlsError as e:
        logger.warning("HLS %s build failed for %s: %s", mode, stem, e)
        raise HTTPException(500, {"error": "Segment preparation failed",
                                  "detail": str(e)})
    return FileResponse(path, media_type="video/mp4", headers=headers)


async def get_snapshot(
    background_tasks: BackgroundTasks,
    camera: str = Query(..., description="Camera name"),
    timestamp: str = Query(..., description="ISO 8601 timestamp (e.g. 2026-03-17T14:32:10Z)"),
    quality: int = Query(85, description="JPEG quality (1–95)", ge=1, le=95),
):
    """Extract a single JPEG frame from the recording at the given timestamp."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        ts_epoch = ts.timestamp()
    except ValueError:
        raise HTTPException(400, f"Invalid timestamp format: {timestamp}. Use ISO 8601.")

    cameras = _index.get_cameras()
    if camera not in cameras:
        raise HTTPException(404, {
            "error": f"Camera '{camera}' not found",
            "available_cameras": cameras,
        })

    rec_range = _index.get_recording_range(camera)
    if rec_range is None:
        raise HTTPException(404, {"error": f"No recordings found for camera '{camera}'"})

    earliest, latest = rec_range
    if ts_epoch < earliest or ts_epoch > latest:
        raise HTTPException(404, {
            "error": "Requested timestamp is outside recorded range",
            "recorded_range": {
                "earliest": datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat(),
                "latest": datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(),
            },
            "requested": timestamp,
        })

    # Find the segment covering the exact timestamp
    segments = _index.find_segments(camera, ts_epoch, ts_epoch)
    if not segments:
        # Widen by half a segment duration to catch boundary timestamps
        segments = _index.find_segments(camera, ts_epoch - 60, ts_epoch + 60)
    segments = [s for s in segments if os.path.exists(s.filepath)]
    if not segments:
        raise HTTPException(404, {
            "error": "No segment covers the requested timestamp (gap in recording)"
        })

    # Pick the segment whose window contains the timestamp, or the closest one
    segment = next((s for s in segments if s.start_epoch <= ts_epoch <= s.end_epoch), segments[0])
    seek_offset = max(0.0, ts_epoch - segment.start_epoch)

    # ffmpeg -q:v uses 1 (best) – 31 (worst); map quality 1-95 → ffmpeg 31-1
    ffmpeg_q = max(1, int(round(31 - (quality / 95) * 30)))

    snap_path = _clips_path / f"{camera}_{int(ts_epoch)}_{uuid.uuid4().hex[:8]}.jpg"
    # `-ss` AFTER `-i` — output seeking — is deliberate, and the obvious
    # optimisation is wrong here.
    #
    # It costs real time: ffmpeg decodes the segment from its first frame up to
    # the offset and discards all of it, so a snapshot late in a segment is slow
    # in proportion. Measured on exit-gate, 0.18 s at 1 s in against 2.58 s at
    # 55 s in.
    #
    # Moving `-ss` before `-i` makes that flat (~0.12 s) and was tried. It is
    # not safe on MPEG-TS: the container carries no seek index, so ffmpeg
    # estimates the position from bitrate and lands approximately. On
    # corner-001-u3z2 (keyframes at 1.5/18.2/34.9/51.5 s) the coarse+exact
    # two-stage form returned the *same* frame for targets 40 s, 45 s and 50 s,
    # and produced nothing at all past 53 s. Silently wrong frames, on images
    # used for evidence annotation.
    #
    # So the decode cost stays. It is bounded by segment_duration and paid once
    # per snapshot, and correctness is not negotiable for these frames.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", segment.filepath,
        "-ss", f"{seek_offset:.3f}",
        "-frames:v", "1",
        "-q:v", str(ffmpeg_q),
        "-y",
        str(snap_path),
    ]
    logger.info("Extracting snapshot: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

    if proc.returncode != 0:
        raise HTTPException(500, {
            "error": "Snapshot extraction failed",
            "detail": stderr.decode(),
        })

    if not snap_path.exists() or snap_path.stat().st_size == 0:
        raise HTTPException(500, "Snapshot extraction produced empty file")

    background_tasks.add_task(snap_path.unlink, missing_ok=True)
    return FileResponse(
        path=str(snap_path),
        media_type="image/jpeg",
        filename=snap_path.name,
    )


async def list_cameras():
    """List all cameras with recording status.

    The set is the UNION of the segment index and the RUNNING RECORDERS, and
    the union is the point. Built from the index alone, a camera that was just
    added did not appear until its first segment closed and was indexed —
    60-90 s during which the recorder was demonstrably writing it to disk while
    this endpoint said the NVR had no such camera.

    That is not cosmetic: camera-mgmt's reconcile treats "absent from this
    list" as "needs adding" and re-POSTs every cycle, getting 409 each time and
    logging `nvr.sync.add.ok` — so the loop could not converge, and a genuine
    add failure was indistinguishable from a camera that simply had not been
    indexed yet.
    """
    engine_status = _engine.get_status() if _engine else {}
    recording_now = set(_engine._workers) if _engine else set()
    cameras = sorted(set(_index.get_cameras()) | recording_now)
    result = []
    for cam in cameras:
        rec_range = _index.get_recording_range(cam)
        info = {"name": cam}
        if _engine and cam in _engine._workers:
            info["rtsp_url"] = _engine._workers[cam].rtsp_url
        if rec_range:
            info["earliest"] = datetime.fromtimestamp(
                rec_range[0], tz=timezone.utc).isoformat()
            info["latest"] = datetime.fromtimestamp(
                rec_range[1], tz=timezone.utc).isoformat()
            info["storage_bytes"] = _index.total_size(cam)
        cam_status = engine_status.get(cam)
        if cam_status is not None:
            info["recording"] = bool(cam_status.get("alive"))
            info["last_indexed_at"] = _epoch_to_iso(
                cam_status.get("last_indexed_at")
            )
            info["index_lag_seconds"] = cam_status.get("index_lag_seconds")
            info["segments_indexed"] = cam_status.get("segments_indexed")
            info["current_backoff_seconds"] = cam_status.get("current_backoff")
        result.append(info)
    return {"cameras": result}


def _epoch_to_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


async def get_camera_range(camera: str):
    """Get the available recording time range for a specific camera."""
    rec_range = _index.get_recording_range(camera)
    if rec_range is None:
        raise HTTPException(404, f"No recordings for camera '{camera}'")
    return {
        "camera": camera,
        "earliest": datetime.fromtimestamp(rec_range[0], tz=timezone.utc).isoformat(),
        "latest": datetime.fromtimestamp(rec_range[1], tz=timezone.utc).isoformat(),
        "storage_bytes": _index.total_size(camera),
    }


async def add_camera(camera: str, rtsp_url: str = Query(...),
                     retention_days: int = Query(30),
                     masks: str = Query("", description="Privacy masks: JSON list of normalized polygons"),
                     groom_after_days: int | None = Query(None, ge=0, le=3650)):
    """Add a new camera at runtime. With ``masks`` the worker burns the
    polygons into every recorded segment (overlay + re-encode).

    ``groom_after_days`` overrides the appliance groom default: omit to inherit
    it, 0 to never groom this camera, >0 for a threshold in days. 0 was rejected
    outright (``ge=1``), which left "never groom" unsayable — so a sub track,
    which must not be groomed, silently inherited the default and was rewritten
    keyframe-only."""
    _validate_camera_name(camera)
    if _engine is None:
        raise HTTPException(500, "Recording engine not available")
    from recorder.masking import parse_masks
    try:
        _engine.add_camera(camera, rtsp_url, retention_days,
                           masks=parse_masks(masks),
                           groom_after_days=groom_after_days)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"status": "ok", "camera": camera}


async def invalidate_codec_cache(camera: str):
    """Drop a recording name's cached codec/resolution, so the next playlist
    re-probes it.

    Called by camera-mgmt whenever it changes what a recording name is actually
    carrying — a sub track re-resolved onto a different profile, most of all.
    That swap keeps the recording name and changes the codec underneath it, and
    a cached H.264 answer then serves raw HEVC through the stream-copy path: the
    player goes black with nothing logged anywhere. Idempotent; a name that was
    never cached is a 200, because "there is nothing stale here" is the outcome
    the caller wanted.
    """
    _validate_camera_name(camera)
    existed = _codec_cache.pop(camera, None) is not None
    logger.info("codec cache invalidated for '%s' (had entry: %s)", camera, existed)
    return {"status": "ok", "camera": camera, "had_entry": existed}


async def set_camera_retention(camera: str,
                               retention_days: int = Query(..., ge=1, le=3650)):
    """Update a camera's retention_days in place — no worker restart, no 409
    for an already-recording camera (unlike POST /cameras/{camera})."""
    _validate_camera_name(camera)
    if _engine is None:
        raise HTTPException(500, "Recording engine not available")
    _engine.set_retention(camera, retention_days)
    return {"status": "ok", "camera": camera, "retention_days": retention_days}


async def set_camera_groom(camera: str,
                           groom_after_days: int | None = Query(None, ge=0, le=3650)):
    """Update a camera's groom-after override in place. No worker restart.

    Omit the parameter to clear the override back to the appliance default; 0
    means never groom this camera; >0 is a threshold in days. Omission is what
    clears, because 0 has to stay available to say "never" — see
    RecordingEngine.set_groom_after."""
    _validate_camera_name(camera)
    if _engine is None:
        raise HTTPException(500, "Recording engine not available")
    _engine.set_groom_after(camera, groom_after_days)
    return {"status": "ok", "camera": camera,
            "groom_after_days": groom_after_days,
            "policy": ("default" if groom_after_days is None
                       else "never" if groom_after_days == 0 else "days")}


async def remove_camera(camera: str):
    """Stop recording and remove a camera."""
    _validate_camera_name(camera)
    if _engine is None:
        raise HTTPException(500, "Recording engine not available")
    try:
        _engine.remove_camera(camera)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if _hls_cache is not None:
        _hls_cache.purge_camera(camera)
    return {"status": "ok", "camera": camera}


async def purge_recordings(camera: str):
    """Permanently delete ALL recorded footage for a camera.

    Removes every indexed segment (files + index rows). The camera itself is
    untouched — if a worker is still recording, new segments keep arriving
    from now on. Intended for decommissioned cameras whose footage should not
    wait out retention. Irreversible.
    """
    _validate_camera_name(camera)
    if _index is None:
        raise HTTPException(500, "Index not available")
    # "Everything before now+1h" == the camera's entire indexed history.
    paths = _index.delete_before(camera, time.time() + 3600)
    if not paths:
        raise HTTPException(404, f"No recordings for camera '{camera}'")
    deleted = 0
    freed = 0
    cam_dir: Path | None = None
    for p in paths:
        f = Path(p)
        cam_dir = cam_dir or f.parent
        try:
            freed += f.stat().st_size if f.is_file() else 0
            f.unlink(missing_ok=True)
            deleted += 1
        except OSError as e:
            logger.warning("purge: could not delete %s: %s", p, e)
    # Tidy the (now likely empty) camera directory if nothing is recording
    # into it — leave it alone when the segment list or fresh files remain.
    if cam_dir and cam_dir.is_dir():
        try:
            leftovers = [x for x in cam_dir.iterdir() if x.suffix == ".ts"]
            if not leftovers:
                for x in cam_dir.iterdir():
                    if x.name == "segments.csv":
                        x.unlink(missing_ok=True)
                if not any(cam_dir.iterdir()):
                    cam_dir.rmdir()
        except OSError:
            pass
    # The HLS routes already refuse artifacts whose index row is gone, so
    # this is about reclaiming the space now rather than at the next sweep.
    if _hls_cache is not None:
        freed += _hls_cache.purge_camera(camera)
    logger.info("purge: camera %s — deleted %d segment(s), freed %.2f GB",
                camera, deleted, freed / (1024 ** 3))
    return {"status": "ok", "camera": camera,
            "segments_deleted": deleted, "bytes_freed": freed}


async def erase_recordings_range(
    camera: str,
    from_ts: str = Query(..., alias="from", description="ISO 8601 window start"),
    to_ts: str = Query(..., alias="to", description="ISO 8601 window end"),
):
    """Permanently delete segments that lie FULLY inside [from, to].

    DPDP data-principal erasure (DSR fulfilment). Deliberately narrow: one
    explicit camera + time window per call, no wildcards. Segments that only
    partially overlap the window are preserved and reported back so the
    operator can widen the request instead of us silently over-deleting
    footage that extends outside the erasure scope. Returns the deleted
    segment list so the caller (camera-mgmt) can embed it in the
    evidence.erased audit payload. Irreversible.
    """
    _validate_camera_name(camera)
    if _index is None:
        raise HTTPException(500, "Index not available")
    try:
        from_epoch = datetime.fromisoformat(from_ts.replace("Z", "+00:00")).timestamp()
        to_epoch = datetime.fromisoformat(to_ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise HTTPException(400, f"Invalid timestamp format: use ISO 8601 (got from={from_ts!r} to={to_ts!r})")
    if from_epoch >= to_epoch:
        raise HTTPException(400, "'from' must be earlier than 'to'")
    if camera not in _index.get_cameras():
        raise HTTPException(404, {
            "error": f"Camera '{camera}' not found",
            "available_cameras": _index.get_cameras(),
        })

    def _iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    rows = _index.select_overlapping(camera, from_epoch, to_epoch)
    inside = [r for r in rows
              if r[1] >= from_epoch and (r[1] + r[2]) <= to_epoch]
    partial = [r for r in rows
               if not (r[1] >= from_epoch and (r[1] + r[2]) <= to_epoch)]

    # Unlink first, then drop only the rows whose unlink succeeded — same
    # DB↔FS fault ordering as the retention size cap.
    deleted_ids: list[int] = []
    deleted_segments: list[dict] = []
    bytes_freed = 0
    for seg_id, start, duration, path, size in inside:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("erase: could not delete %s: %s", path, e)
            continue
        deleted_ids.append(seg_id)
        bytes_freed += size
        deleted_segments.append({
            "filepath": path,
            "start": _iso(start),
            "end": _iso(start + duration),
            "bytes": size,
        })
    _index.delete_by_ids(deleted_ids)

    logger.info("erase: camera %s [%s → %s] — deleted %d segment(s), "
                "preserved %d boundary overlap(s), freed %.2f GB",
                camera, from_ts, to_ts, len(deleted_segments), len(partial),
                bytes_freed / (1024 ** 3))
    return {
        "status": "ok",
        "camera": camera,
        "from": from_ts,
        "to": to_ts,
        "segments_deleted": len(deleted_segments),
        "bytes_freed": bytes_freed,
        "deleted": deleted_segments,
        "skipped_partial_overlap": [
            {"filepath": p, "start": _iso(s), "end": _iso(s + d)}
            for _, s, d, p, _sz in partial
        ],
    }


async def health_check():
    """System health check.

    Per-camera fields exposed for external monitoring (Prometheus, uptime
    checks, etc.):
      - alive: worker thread is running
      - last_indexed_at: ISO timestamp of last successfully indexed segment
      - index_lag_seconds: seconds since last indexed segment (None if never)
      - segments_indexed: lifetime count for this worker instance
      - current_backoff_seconds: current reconnect backoff (base if healthy)
    """
    raw = _engine.get_status() if _engine else {}
    disk_state = raw.pop("__disk__", {"state": "unknown"})
    cameras = {}
    overall_healthy = True
    for name, m in raw.items():
        cameras[name] = {
            "alive": m.get("alive", False),
            "last_indexed_at": _epoch_to_iso(m.get("last_indexed_at")),
            "index_lag_seconds": m.get("index_lag_seconds"),
            "segments_indexed": m.get("segments_indexed", 0),
            "current_backoff_seconds": m.get("current_backoff"),
        }
        if not m.get("alive", False):
            overall_healthy = False
    if disk_state.get("state") == "critical":
        overall_healthy = False
    slots_available = (
        _clip_semaphore._value if _clip_semaphore is not None else _clip_semaphore_limit
    )
    return {
        "status": "ok" if overall_healthy else "degraded",
        "cameras": cameras,
        "total_storage_bytes": _index.total_size(),
        "disk": disk_state,
        "clip_extraction": {
            "slots_total": _clip_semaphore_limit,
            # Why that number, and which encoder is actually in use. A silent
            # degrade to CPU shrinks the cap; without this it looks like a
            # mystery slowdown.
            "slots_basis": _clip_semaphore_basis,
            "encoder": "nvenc" if _nvenc_ok else "cpu",
            "slots_available": slots_available,
            "slots_in_use": _clip_semaphore_limit - slots_available,
        },
        # What the HLS artifact cache is holding. An empty cache on a busy
        # appliance means every seek is paying full price.
        "hls_cache": _hls_cache.stats() if _hls_cache is not None else {},
    }


async def coverage(
    camera: str = Query(..., description="Camera name"),
    from_: str = Query(..., alias="from",
                       description="ISO 8601 window start"),
    to: str = Query(..., description="ISO 8601 window end"),
    min_gap_seconds: float = Query(
        2.0, ge=0.0, le=60.0,
        description="Segments closer than this are merged; sub-second ffmpeg "
                    "boundary jitter shouldn't show as a gap.",
    ),
):
    """Return recording gaps for a camera within [from, to].

    Gaps are restricted to the intersection of the requested window with the
    camera's actual recorded range — periods before the camera ever recorded
    or beyond its latest segment are not 'gaps', just unrecorded territory.
    """
    try:
        from_epoch = datetime.fromisoformat(
            from_.replace("Z", "+00:00")).timestamp()
        to_epoch = datetime.fromisoformat(
            to.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise HTTPException(400, "Invalid timestamp format. Use ISO 8601.")
    if to_epoch <= from_epoch:
        raise HTTPException(400, "'to' must be later than 'from'")

    if camera not in _index.get_cameras():
        raise HTTPException(404, {
            "error": f"Camera '{camera}' not found",
        })

    rec_range = _index.get_recording_range(camera)
    if rec_range is None:
        return {
            "camera": camera, "from": from_, "to": to,
            "earliest": None, "latest": None,
            "segment_count": 0, "gaps": [],
        }
    earliest, latest = rec_range

    segs = _index.find_segments(camera, from_epoch, to_epoch)
    merged = _merge_segments(segs, from_epoch, to_epoch, min_gap_seconds)

    # Gap window: from camera's earliest up to "now" if the engine still
    # considers this camera live (so an offline-but-expected camera shows red
    # from its last segment to now). For decommissioned cameras (not in the
    # current config), stop at latest.
    camera_is_live = (
        _engine is not None
        and camera in getattr(_engine, "_workers", {})
    )
    live_end = time.time() if camera_is_live else latest
    gap_start = max(from_epoch, earliest)
    gap_end = min(to_epoch, max(latest, live_end))
    gaps: list[dict] = []
    cursor = gap_start
    for ms, me in merged:
        if me <= gap_start or ms >= gap_end:
            continue
        ms_c = max(ms, gap_start)
        me_c = min(me, gap_end)
        if ms_c > cursor:
            gaps.append({"start": cursor, "end": ms_c})
        cursor = max(cursor, me_c)
    if cursor < gap_end:
        # Healthy cameras always have a small trailing gap: the in-progress
        # segment isn't indexed until ffmpeg finalises it, so `latest` lags
        # wall-clock by up to one segment_duration. Suppress that window for
        # live cameras to avoid a phantom red strip glued to the right edge.
        trailing_width = gap_end - cursor
        if not camera_is_live or trailing_width > _segment_duration:
            gaps.append({"start": cursor, "end": gap_end})

    return {
        "camera": camera,
        "from": from_,
        "to": to,
        "earliest": earliest,
        "latest": latest,
        "segment_count": len(segs),
        "gaps": gaps,
    }


def _merge_segments(segs: list, from_epoch: float, to_epoch: float,
                    min_gap_seconds: float) -> list[list[float]]:
    """Merge segments closer than min_gap_seconds, clipped to the window.

    Returns [[start, end], ...] covered intervals in ascending order.
    Shared by /coverage (gap rendering) and /report/uptime (up/down report)
    so both agree on what counts as a gap.
    """
    merged: list[list[float]] = []
    for s in segs:
        s_start = max(s.start_epoch, from_epoch)
        s_end = min(s.end_epoch, to_epoch)
        if s_end <= s_start:
            continue
        if merged and s_start - merged[-1][1] <= min_gap_seconds:
            merged[-1][1] = max(merged[-1][1], s_end)
        else:
            merged.append([s_start, s_end])
    return merged


def _fmt_local(epoch: float) -> str:
    """Epoch → 'YYYY-MM-DD HH:MM:SS' in the server's local timezone."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(seconds: float) -> str:
    """Seconds → 'HH:MM:SS' (hours not wrapped, e.g. '26:03:12')."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _camera_up_down_events(camera: str, from_epoch: float,
                           win_end: float, min_gap_seconds: float) -> dict:
    """Compute UP/DOWN intervals for one camera within [from_epoch, win_end].

    UP = interval covered by recorded segments (recording running), DOWN =
    everything else in the window. A live camera's in-progress segment isn't
    indexed until ffmpeg finalises it, so for live cameras a trailing hole of
    up to one segment_duration before `win_end` is counted as UP — same
    reasoning as /coverage's phantom-gap suppression.
    """
    segs = _index.find_segments(camera, from_epoch, win_end)
    merged = _merge_segments(segs, from_epoch, win_end, min_gap_seconds)

    camera_is_live = (
        _engine is not None
        and camera in getattr(_engine, "_workers", {})
    )
    if camera_is_live:
        # Bridge the not-yet-indexed in-progress segment to "now".
        last_end = merged[-1][1] if merged else None
        if last_end is None:
            rec_range = _index.get_recording_range(camera)
            last_end = rec_range[1] if rec_range else None
        if (last_end is not None
                and 0 < win_end - last_end <= _segment_duration + min_gap_seconds
                and last_end >= from_epoch - (_segment_duration + min_gap_seconds)):
            if merged and merged[-1][1] >= from_epoch:
                merged[-1][1] = win_end
            else:
                merged.append([max(from_epoch, last_end), win_end])

    # Walk the window, alternating DOWN (uncovered) / UP (covered).
    events: list[dict] = []
    cursor = from_epoch
    for m_start, m_end in merged:
        if m_start > cursor:
            events.append({"state": "DOWN", "start": cursor, "end": m_start})
        events.append({"state": "UP", "start": max(cursor, m_start), "end": m_end})
        cursor = m_end
    if cursor < win_end:
        events.append({"state": "DOWN", "start": cursor, "end": win_end})

    up_seconds = sum(e["end"] - e["start"] for e in events if e["state"] == "UP")
    window_seconds = win_end - from_epoch
    down_seconds = window_seconds - up_seconds
    return {
        "camera": camera,
        "events": events,
        "up_seconds": up_seconds,
        "down_seconds": down_seconds,
        "uptime_pct": (100.0 * up_seconds / window_seconds) if window_seconds > 0 else 0.0,
        "outages": sum(1 for e in events if e["state"] == "DOWN"),
    }


async def uptime_report(
    from_: str = Query(..., alias="from", description="ISO 8601 window start"),
    to: str = Query(..., description="ISO 8601 window end (clamped to now)"),
    cameras: str | None = Query(
        None, description="Comma-separated camera names; omit for all cameras"),
    format: str = Query("xlsx", enum=["xlsx", "json"],
                        description="xlsx download or json"),
    min_gap_seconds: float = Query(2.0, ge=0.0, le=60.0),
):
    """Per-camera up/down (recording/not-recording) report for a time window.

    "Up" means segments exist on disk for that interval — i.e. the camera was
    reachable and the recorder was writing. Returns an Excel workbook with a
    Summary sheet (per-camera totals) and an Events sheet (every UP/DOWN
    interval with start, end, duration), or the same data as JSON.
    """
    try:
        from_epoch = datetime.fromisoformat(from_.replace("Z", "+00:00")).timestamp()
        to_epoch = datetime.fromisoformat(to.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise HTTPException(400, "Invalid timestamp format. Use ISO 8601.")

    win_end = min(to_epoch, time.time())   # can't report the future
    if win_end <= from_epoch:
        raise HTTPException(400, "'to' must be later than 'from' (and not entirely in the future)")

    # Known cameras = anything indexed + anything currently configured, so a
    # configured-but-never-recorded camera reports as 100% down (that IS the
    # information the user wants) instead of 404ing.
    known = set(_index.get_cameras()) | set(getattr(_engine, "_workers", {}) or {})
    if cameras:
        requested = [c.strip() for c in cameras.split(",") if c.strip()]
        unknown = [c for c in requested if c not in known]
        if unknown:
            raise HTTPException(404, {
                "error": f"Unknown camera(s): {', '.join(unknown)}",
                "available_cameras": sorted(known),
            })
    else:
        requested = sorted(known)
    if not requested:
        raise HTTPException(404, {"error": "No cameras configured or recorded"})

    reports = [
        _camera_up_down_events(cam, from_epoch, win_end, min_gap_seconds)
        for cam in requested
    ]

    if format == "json":
        return {
            "from": from_, "to": to,
            "window_end_used": datetime.fromtimestamp(
                win_end, tz=timezone.utc).isoformat(),
            "cameras": [
                {**r, "events": [
                    {**e, "duration_seconds": round(e["end"] - e["start"], 1)}
                    for e in r["events"]
                ]} for r in reports
            ],
        }

    # ── xlsx ──
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(500, {
            "error": "openpyxl is not installed on the server",
            "detail": "pip install openpyxl (or rebuild the Docker image).",
        })
    import io

    wb = Workbook()
    bold = Font(bold=True)
    fill_down = PatternFill("solid", start_color="FFC7CE")   # light red
    fill_up   = PatternFill("solid", start_color="C6EFCE")   # light green

    # Sheet 1: Summary
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Camera", "Window Start", "Window End", "Up Time", "Down Time",
               "Uptime %", "Outages", "Up Seconds", "Down Seconds"])
    for c in ws[1]:
        c.font = bold
    for r in reports:
        ws.append([
            r["camera"], _fmt_local(from_epoch), _fmt_local(win_end),
            _fmt_duration(r["up_seconds"]), _fmt_duration(r["down_seconds"]),
            round(r["uptime_pct"], 2), r["outages"],
            round(r["up_seconds"], 1), round(r["down_seconds"], 1),
        ])

    # Sheet 2: Events (chronological per camera)
    ws2 = wb.create_sheet("Events")
    ws2.append(["Camera", "State", "Start", "End", "Duration", "Duration (s)"])
    for c in ws2[1]:
        c.font = bold
    for r in reports:
        for e in r["events"]:
            ws2.append([
                r["camera"], e["state"],
                _fmt_local(e["start"]), _fmt_local(e["end"]),
                _fmt_duration(e["end"] - e["start"]),
                round(e["end"] - e["start"], 1),
            ])
            state_cell = ws2.cell(row=ws2.max_row, column=2)
            state_cell.fill = fill_down if e["state"] == "DOWN" else fill_up

    for sheet in (ws, ws2):
        for col_idx in range(1, sheet.max_column + 1):
            width = max(
                (len(str(c.value)) for c in sheet[get_column_letter(col_idx)]
                 if c.value is not None), default=8)
            sheet.column_dimensions[get_column_letter(col_idx)].width = min(width + 2, 40)

    buf = io.BytesIO()
    wb.save(buf)
    stamp_from = datetime.fromtimestamp(from_epoch).strftime("%Y%m%d_%H%M%S")
    stamp_to = datetime.fromtimestamp(win_end).strftime("%Y%m%d_%H%M%S")
    filename = f"uptime_{stamp_from}_to_{stamp_to}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def storage_stats():
    """Storage usage statistics."""
    cameras = _index.get_cameras()
    per_camera = {}
    for cam in cameras:
        size = _index.total_size(cam)
        normal, cold = _index.size_split(cam)
        normal_range, cold_range = _index.range_split(cam)
        per_camera[cam] = {
            "bytes": size,
            "gb": round(size / (1024**3), 2),
            # Full-quality (recent) vs groomed keyframe-only (aged-out) footage.
            "normal_bytes": normal,
            "cold_bytes": cold,
            # [earliest_start, latest_end] epochs per tier, null when empty.
            "normal_range": normal_range,
            "cold_range": cold_range,
        }
    total = _index.total_size()
    total_normal, total_cold = _index.size_split()
    total_normal_range, total_cold_range = _index.range_split()
    limit = _storage_settings.max_storage_bytes if _storage_settings else None
    # Free space is measured live, not cached: the operator opens this screen to
    # decide a cap, and a stale headroom figure is exactly the one that lets them
    # set a cap the volume cannot honour.
    head = capacity.probe(_storage_path, footage_bytes=total)
    result = {
        "total_bytes": total,
        "total_gb": round(total / (1024**3), 2),
        "normal_bytes": total_normal,
        "cold_bytes": total_cold,
        "normal_range": total_normal_range,
        "cold_range": total_cold_range,
        # Appliance groom-after default (days; 0 = grooming disabled).
        "groom_after_days_default": _groom_default_days,
        "limit_bytes": limit,
        "limit_gb": (round(limit / (1024**3), 2) if limit else None),
        "usage_pct": (round(100 * total / limit, 2) if limit else None),
        # The largest cap this volume can honour, and the disk it was measured
        # on. Both null when the probe failed — the UI then falls back to the
        # absolute bound, matching set_storage_limit's fail-open behaviour.
        "max_limit_bytes": (head.max_cap_bytes if head else None),
        "max_limit_gb": (head.max_cap_gb if head else None),
        "disk": (head.as_dict() if head else None),
        # Storage pressure: one state plus the reasons for it. The disk half was
        # already visible in three places; the CAP half was visible nowhere, and
        # a binding cap silently shortens retention rather than breaking
        # anything. Null when alerting is not wired.
        "pressure": (_alerter.snapshot() if _alerter else None),
        "per_camera": per_camera,
    }
    return result


async def set_storage_limit(gb: float | None = Query(None, ge=0, le=1_000_000)):
    """Set the global storage size cap (GB). 0 or omitted = uncapped (age-based
    retention only). Persisted so it survives a restart; takes effect on the
    retention loop's next pass. Lowering it below current usage deletes the
    oldest footage across ALL cameras — the caller is expected to confirm.

    Rejected with 400 when the cap exceeds what the storage volume can honour
    (see storage/capacity.py). A larger cap is not a larger limit: the disk-floor
    evictor reclaims space at DISK_FLOOR_FREE_PCT regardless of the cap, so the
    number would be recorded, displayed, and never enforced. The ceiling counts
    footage already on disk, so it measures the volume rather than this instant's
    leftover free space — but it is not a promise that an existing cap can always
    be re-saved: a volume whose non-footage data has grown into the reserve gets
    a ceiling below its current cap, and must be lowered. That is the intended
    answer, since such a cap is already being overridden by the disk floor.
    """
    if _storage_settings is None:
        raise HTTPException(500, "Storage settings not available")
    limit_bytes = int(gb * (1024 ** 3)) if gb else None

    if limit_bytes:
        head = capacity.probe(_storage_path, footage_bytes=_index.total_size())
        # head is None when the volume could not be measured. Fail open: a stat
        # failure must not be the thing that stops an operator setting a cap.
        if head is not None and limit_bytes > head.max_cap_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Storage cap of {capacity.humanize(limit_bytes)} is larger than "
                    f"{head.path} can hold. Maximum is {head.max_cap_gb} GB — "
                    f"{capacity.humanize(head.free_bytes)} free plus "
                    f"{capacity.humanize(head.footage_bytes)} already recorded, "
                    f"keeping {capacity.RESERVE_FREE_PCT:g}% of the disk free for "
                    f"clips and the segment index."
                ),
            )

    _storage_settings.set_max_storage_bytes(limit_bytes)
    return {"status": "ok", "limit_gb": gb or None, "limit_bytes": limit_bytes}


async def save_annotation(
    camera: str = Form(..., description="Camera name (must already be configured)"),
    timestamp: str = Form(..., description="ISO 8601 wall-clock time of the frame"),
    bbox_x: float = Form(..., ge=0, description="Left edge of bbox in source-video pixels"),
    bbox_y: float = Form(..., ge=0, description="Top edge of bbox in source-video pixels"),
    bbox_w: float = Form(..., gt=0, description="Width of bbox in source-video pixels"),
    bbox_h: float = Form(..., gt=0, description="Height of bbox in source-video pixels"),
    frame_width: int = Form(..., gt=0, description="Source frame width in pixels"),
    frame_height: int = Form(..., gt=0, description="Source frame height in pixels"),
    image: UploadFile = File(..., description="Full-frame JPEG at source resolution"),
):
    """Save a user-drawn bounding-box annotation along with the source frame.

    Storage layout:
      {storage_path}/annotations/{camera}/{ts_epoch}_{uuid}.jpg
      {storage_path}/annotations/{camera}/{ts_epoch}_{uuid}.json

    The JSON sidecar carries the bbox (in *source* pixel coordinates, not
    display pixels) plus the frame dimensions and timestamp — exactly the
    inputs an offline Re-ID / search indexer needs. Keeping the image as a
    full frame (not a pre-cropped patch) lets a future indexer re-crop or
    re-process at higher quality without losing context.
    """
    _validate_camera_name(camera)

    if _annotations_path is None or _index is None:
        raise HTTPException(500, "Annotation storage is not configured")

    # Camera must be known to the system. We accept any configured camera,
    # not just ones with indexed segments — annotations may be saved for a
    # camera that just came online and hasn't completed a segment yet.
    known_cameras = set(_index.get_cameras())
    if _engine is not None:
        known_cameras.update(_engine._workers.keys())
    if camera not in known_cameras:
        raise HTTPException(404, {
            "error": f"Camera '{camera}' not found",
            "available_cameras": sorted(known_cameras),
        })

    # Timestamp parsing
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        ts_epoch = ts.timestamp()
    except ValueError:
        raise HTTPException(400, f"Invalid timestamp format: {timestamp}. Use ISO 8601.")

    # Bbox must lie within the declared frame. We allow sub-pixel floats so
    # the UI doesn't have to round, but the saved metadata is rounded to
    # int pixel coordinates (what every CV library expects).
    if bbox_x + bbox_w > frame_width + 0.5 or bbox_y + bbox_h > frame_height + 0.5:
        raise HTTPException(400, {
            "error": "Bbox extends outside frame bounds",
            "bbox": {"x": bbox_x, "y": bbox_y, "w": bbox_w, "h": bbox_h},
            "frame": {"width": frame_width, "height": frame_height},
        })

    # Image type guard. We don't sniff magic bytes here — the front-end
    # always uploads a freshly encoded JPEG via canvas.toBlob, and the size
    # cap (below) bounds the worst case if the header is wrong.
    if image.content_type not in _ANNOTATION_ALLOWED_MIME:
        raise HTTPException(415, {
            "error": "Unsupported image type",
            "got": image.content_type,
            "allowed": sorted(_ANNOTATION_ALLOWED_MIME),
        })

    # Read with size cap. Reading-then-checking is fine for 10 MB; for much
    # larger caps you'd want a streaming guard.
    image_bytes = await image.read()
    if len(image_bytes) == 0:
        raise HTTPException(400, "Image upload was empty")
    if len(image_bytes) > _MAX_ANNOTATION_BYTES:
        raise HTTPException(413, {
            "error": "Image too large",
            "size_bytes": len(image_bytes),
            "max_bytes": _MAX_ANNOTATION_BYTES,
        })

    # Persist. mkdir per-camera once on first write per camera.
    cam_dir = _annotations_path / camera
    cam_dir.mkdir(parents=True, exist_ok=True)

    annotation_id = uuid.uuid4().hex
    base_name = f"{int(ts_epoch)}_{annotation_id}"
    image_path = cam_dir / f"{base_name}.jpg"
    meta_path  = cam_dir / f"{base_name}.json"

    # Atomic-ish write: write to a tmp sibling and rename, so a partial
    # write can't be picked up as a complete annotation by a future indexer.
    tmp_image = image_path.with_suffix(".jpg.tmp")
    tmp_meta  = meta_path.with_suffix(".json.tmp")

    try:
        tmp_image.write_bytes(image_bytes)
        metadata = {
            "id": annotation_id,
            "camera": camera,
            "timestamp": timestamp,
            "timestamp_epoch": ts_epoch,
            "bbox": {
                "x": round(bbox_x, 2),
                "y": round(bbox_y, 2),
                "width":  round(bbox_w, 2),
                "height": round(bbox_h, 2),
            },
            "frame": {"width": frame_width, "height": frame_height},
            "image_path": str(image_path),
            "image_bytes": len(image_bytes),
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        tmp_meta.write_text(json.dumps(metadata, indent=2))
        tmp_image.rename(image_path)
        tmp_meta.rename(meta_path)
    except OSError:
        for p in (tmp_image, tmp_meta, image_path, meta_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        logger.exception("Failed to persist annotation for camera '%s'", camera)
        raise HTTPException(500, "Failed to save annotation to disk")

    logger.info(
        "Annotation saved: camera=%s ts=%s bbox=(%.0f,%.0f,%.0fx%.0f) id=%s",
        camera, timestamp, bbox_x, bbox_y, bbox_w, bbox_h, annotation_id,
    )
    return metadata


class ClipExtractionError(Exception):
    pass


async def _get_video_codec(camera: str, sample_filepath: str) -> str:
    """Codec only — the shape `_video_output_args` has always wanted."""
    return (await _get_stream_info(camera, sample_filepath))[0]


async def _get_stream_info(camera: str, sample_filepath: str) -> tuple[str, int | None, int | None]:
    """Return the video codec name (lowercase) for this camera's segments.

    Cached per-camera with a TTL and an explicit invalidation hook — a track's
    codec can change under a stable recording name (see _codec_cache). Falls
    back to empty string on probe failure (caller treats unknown as
    non-browser-safe → transcodes to H.264, which is the safe choice).

    Uses asyncio.create_subprocess_exec so the event loop is never blocked,
    even on the first (uncached) call after restart.
    """
    cached = _codec_cache.get(camera)
    if cached is not None and (time.time() - cached[3]) < _CODEC_CACHE_TTL:
        return cached[0], cached[1], cached[2]
    codec, width, height = "", None, None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "default=nw=1",
            sample_filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise OSError("ffprobe timed out")
        # MPEG-TS containers can list the same stream under multiple programs,
        # so each key repeats. Take the first value of each.
        for line in stdout.decode().splitlines():
            key, _, value = line.strip().partition("=")
            value = value.strip()
            if not value:
                continue
            if key == "codec_name" and not codec:
                codec = value.lower()
            elif key == "width" and width is None:
                width = int(value) if value.isdigit() else None
            elif key == "height" and height is None:
                height = int(value) if value.isdigit() else None
    except (OSError, ValueError) as e:
        logger.warning("Stream probe failed for camera '%s': %s", camera, e)
    if codec:
        # Single-key dict assignment is atomic under the GIL; no lock needed.
        _codec_cache[camera] = (codec, width, height, time.time())
    elif cached is not None:
        # The probe failed and we hold an expired entry. Keep serving it rather
        # than falling back to "unknown" — a stale codec is a better guess than
        # none, and "unknown" forces a transcode of footage that may not need
        # one. The next request retries.
        return cached[0], cached[1], cached[2]
    return codec, width, height


async def _video_output_args(camera: str, sample_filepath: str) -> tuple[list[str], list[str]]:
    """(pre-input args, output args) for the video stream: copy if browser-safe,
    else transcode to H.264 — h264_nvenc when the startup probe proved the GPU
    path, libx264 otherwise (see api/encoders.py). The pre-input args carry
    `-hwaccel cuda` on the NVENC path; they must precede `-i` in the command.
    """
    codec = await _get_video_codec(camera, sample_filepath)
    if codec in _BROWSER_SAFE_CODECS:
        return [], ["-c", "copy"]
    # HEVC (or unknown) → transcode to H.264 for browser compatibility.
    return encoders.transcode_args(_nvenc_ok)


async def _extract_clip(segments: list, clip_start: float, clip_duration: float,
                        output_path: str) -> None:
    """Extract a clip from one or more MPEG-TS segments using ffmpeg.

    Strategy:
    - If single segment: direct seek + trim from the segment file.
    - If multiple segments: use the concat *demuxer* (not the concat protocol)
      to join them. The demuxer properly handles PTS discontinuities that arise
      from segments recorded with -reset_timestamps 1.

    Output codec is chosen by `_video_output_args` — H.264 sources are remuxed
    (`-c copy`, fast); HEVC and unknown codecs are transcoded to H.264 so the
    resulting MP4 plays in browsers that lack HEVC support.
    """
    first_seg_start = segments[0].start_epoch
    seek_offset = max(0, clip_start - first_seg_start)
    pre_input_args, video_args = await _video_output_args(
        segments[0].camera, segments[0].filepath
    )
    # Transcode is slower than remux; give it a longer wall-clock budget.
    #
    # SCALED, not constant. This was `30 if remux else 60` — a budget sized for
    # the 20-second default clip and applied unchanged to every length. It was
    # survivable only because /clip capped windows at an hour; the moment a
    # 3-hour evidence export became possible, a fixed 30 s became the next wall,
    # and a timeout reads to the caller like a broken NVR rather than a budget.
    # Measured on this appliance: a 1-hour remux takes ~0.4 s, so 0.05x duration
    # is ~400x margin. Transcode is bounded near real time (NVENC is far
    # faster), and both are capped so a wedged ffmpeg cannot hold a slot for
    # ever.
    if video_args[0] == "-c":            # remux (stream copy)
        timeout = min(30 + 0.05 * clip_duration, _MAX_EXTRACTION_SECONDS)
    else:                                 # transcode
        timeout = min(60 + 1.00 * clip_duration, _MAX_EXTRACTION_SECONDS)

    concat_path: str | None = None
    duration_args = ["-t", f"{clip_duration:.3f}"]
    if len(segments) == 1:
        input_args = [
            "-ss", f"{seek_offset:.3f}",
            "-i", segments[0].filepath,
        ]
    elif video_args[0] == "-c":
        # STREAM COPY ACROSS FILES: the window is bounded by the concat
        # demuxer's own `inpoint`/`outpoint`, not by `-ss`/`-t` around it.
        #
        # The recorder writes every segment with `-reset_timestamps 1`, so the
        # joined timeline restarts at each file boundary. With `-c copy` nothing
        # is decoded to re-time it, and `-t` stops honouring the requested
        # length once the cut crosses a boundary: the rest of the next file is
        # appended. Measured on this appliance (cam2, H.264): 30 s requests that
        # crossed a boundary came back 77.4 s and 77.6 s long, a 40 s request
        # 87.0 s — while in/out points on the same files gave 40.6 s and 39.2 s,
        # still a 0.3 s remux. Re-encoded clips decode every frame and were
        # already exact, so they keep the `-ss`/`-t` path below.
        first_start = await _file_start_time(segments[0].filepath)
        last_start = await _file_start_time(segments[-1].filepath)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            f.write(_bounded_concat_list(segments, clip_start, clip_start + clip_duration,
                                         first_start, last_start))
            concat_path = f.name
        input_args = ["-f", "concat", "-safe", "0", "-i", concat_path]
        duration_args = []
    else:
        # concat demuxer handles PTS discontinuities from -reset_timestamps 1.
        concat_list = "\n".join(f"file '{s.filepath}'" for s in segments)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            f.write(concat_list)
            concat_path = f.name
        input_args = [
            "-f", "concat", "-safe", "0",
            "-ss", f"{seek_offset:.3f}",
            "-i", concat_path,
        ]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        *pre_input_args,
        *input_args,
        *duration_args,
        *video_args,
        # Fragmented MP4: init moov up front + self-contained fragments, so an
        # interrupted/killed ffmpeg (or a truncated lossy source) still yields a
        # playable file up to the last complete fragment — vs +faststart, which
        # writes the moov only on clean close and leaves corrupt "moov atom not
        # found" clips when the source stutters (packet loss / feed drops).
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-y",
        output_path,
    ]
    logger.info("Extracting clip: %s", " ".join(cmd))

    try:
        try:
            await _run_ffmpeg(cmd, timeout)
        except ClipExtractionError as e:
            if not encoders.is_nvenc_cmd(cmd):
                raise
            # Per-job CPU fallback: a transient NVENC failure (driver hiccup,
            # exhausted encode sessions on consumer GPUs) should cost CPU, not
            # fail the clip. The startup probe stays authoritative for the
            # default — one bad job doesn't flip the mode.
            logger.warning("NVENC extraction failed (%s) — retrying on libx264", e)
            await _run_ffmpeg(encoders.to_cpu_fallback(cmd), timeout)
    finally:
        if concat_path is not None:
            try:
                os.unlink(concat_path)
            except OSError:
                pass


def _bounded_concat_list(segments: list, clip_start: float, clip_end: float,
                         first_start_time: float = 0.0, last_start_time: float = 0.0) -> str:
    """A concat-demuxer list that begins at `clip_start` and ends at `clip_end`.

    `inpoint` goes on the first file and `outpoint` on the last, each in that
    file's own timestamps: the file's `start_time` plus how far into it the
    wall-clock bound falls. Files in between are taken whole.
    """
    lines: list[str] = []
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        lines.append(f"file '{seg.filepath}'")
        if i == 0:
            lines.append(f"inpoint {first_start_time + max(0.0, clip_start - seg.start_epoch):.3f}")
        if i == last:
            lines.append(f"outpoint {last_start_time + max(0.0, clip_end - seg.start_epoch):.3f}")
    return "\n".join(lines) + "\n"


async def _file_start_time(filepath: str) -> float:
    """The first timestamp in a recorded file, in seconds (0.0 if unreadable)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=start_time",
            "-of", "csv=p=0", filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return float(out.decode().strip() or 0.0)
    except (OSError, ValueError, asyncio.TimeoutError):
        return 0.0


async def _run_ffmpeg(cmd: list[str], timeout: float) -> None:
    """Run one ffmpeg invocation; raise ClipExtractionError on failure/timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ClipExtractionError(f"ffmpeg timed out after {timeout}s")
    if proc.returncode != 0:
        raise ClipExtractionError(
            f"ffmpeg exited with code {proc.returncode}: "
            f"{stderr.decode(errors='replace')}"
        )


def _compute_coverage(segments: list, start: float, end: float) -> float:
    """Compute fraction of [start, end] covered by segments. 1.0 = full coverage.

    A 1-second tolerance is applied before returning: ffmpeg segment durations
    are stored as float PTS differences (e.g. 59.9997 s instead of 60.0 s),
    so a perfectly covered window can produce 0.9999… and fire a spurious
    "recording gap" header. Snapping to 1.0 when we’re within 1 s of full
    coverage eliminates that false positive without masking real gaps.
    """
    if end <= start:
        return 1.0
    total_window = end - start
    covered = 0.0
    for seg in segments:
        seg_start = max(seg.start_epoch, start)
        seg_end = min(seg.end_epoch, end)
        if seg_end > seg_start:
            covered += seg_end - seg_start
    # Snap to 1.0 when within 1 s of full coverage (float PTS drift tolerance).
    if covered + 1.0 >= total_window:
        return 1.0
    return covered / total_window
