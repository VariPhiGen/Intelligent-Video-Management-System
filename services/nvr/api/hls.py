"""hls.py — HLS VOD playback over the recorded MPEG-TS segments.

Why this exists
---------------
The `/clip` endpoint builds a fresh MP4 with ffmpeg on *every seek*: the player
asks for a 120 s window, the NVR transcodes HEVC→H.264 into a uuid-named file,
and the browser downloads the whole thing before showing frame one. Measured on
this appliance that is ~54 s of CPU for one clip — the "playback takes forever"
complaint and the 503 "Clip extraction at capacity" cascade are the same bug
seen from two sides.

But the recorder already writes what HLS wants: `recorder/stream_worker.py`
stores 60-second **MPEG-TS** segments with `-c copy`. MPEG-TS *is* the HLS
segment format. So playback becomes a playlist over files that already exist,
the player fetches only the 60 s it is about to show, and seeking is an HTTP
GET instead of an ffmpeg invocation.

Three delivery modes, chosen per camera from the recorded codec and what the
client can decode:

  ts     H.264 source            → the on-disk file, byte for byte. No ffmpeg.
  fmp4   HEVC + HEVC-capable UA  → TS→fMP4 remux, `-c copy`. Measured 0.06 s
                                   per 60 s segment on this box (~900x cheaper
                                   than transcoding the equivalent clip).
  h264   HEVC + everything else  → per-segment transcode, the old cost but
                                   cached and reused across seeks instead of
                                   redone on every one.

HEVC must be carried in fMP4, not TS: Apple's HLS authoring spec requires it,
hls.js only demuxes AVC from TS, and the `hvc1` tag (not ffmpeg's default
`hev1`) is what Safari will actually play.

Cache correctness
-----------------
Artifacts are keyed on the source segment's (size, mtime), so anything that
rewrites a segment — grooming rewrites them keyframe-only — lands on a
different key and can never serve stale footage. Serving also re-checks that
the source still exists, so footage erased under a DSR stops being reachable
through the cache immediately, without needing an invalidation hook at every
delete site.

Timestamps
----------
The recorder runs with `-reset_timestamps 1`, so every segment restarts its PTS
at ~0 — verified on this appliance (three consecutive segments all report
start_time=1.4). In HLS terms that makes *every* segment boundary a
discontinuity, so the playlist emits EXT-X-DISCONTINUITY before each segment
and an EXT-X-PROGRAM-DATE-TIME on all of them; the player then maps media time
to wall clock from the tags rather than from accumulated durations.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Recording-name suffix for a camera's low-resolution sub track. camera-mgmt
# creates it (models.sub_recording_name); the NVR only ever sees it as another
# name in the index, which is exactly why retention, grooming and the size cap
# picked it up with no change.
SUB_SUFFIX = "_sub"

MODE_TS = "ts"
MODE_FMP4 = "fmp4"
MODE_H264 = "h264"
MODES = (MODE_TS, MODE_FMP4, MODE_H264)

# Codecs a browser plays without transcoding. Mirrors server._BROWSER_SAFE_CODECS;
# kept local so this module has no import back into the API surface.
_BROWSER_SAFE = frozenset({"h264", "avc1"})

# Segment stems are `{camera}_YYYYMMDD_HHMMSS` (recorder's -strftime pattern).
# Anchored and character-limited so a crafted stem can't escape the cache dir.
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}_\d{8}_\d{6}$")
_SIG_RE = re.compile(r"^[0-9a-f]{8}$")

# A gap this small between one segment's end and the next one's start is muxer
# rounding, not a recording gap. Larger gaps get their own PROGRAM-DATE-TIME so
# the player's wall clock doesn't drift across the hole.
_GAP_TOLERANCE_S = 0.5

_REMUX_TIMEOUT_S = 30.0
_TRANSCODE_BASE_TIMEOUT_S = 60.0
_MAX_TIMEOUT_S = 300.0


class HlsError(Exception):
    """Materialising a cached artifact failed (ffmpeg error or timeout)."""


def choose_mode(codec: str, client_hevc: bool) -> str:
    """Delivery mode for a camera whose segments are `codec`.

    `client_hevc` is the browser's own answer to "can you decode HEVC" — the
    server cannot infer it from the User-Agent with any accuracy, so the SPA
    probes MediaSource.isTypeSupported and passes the result.

    An unknown codec (probe failed) is treated as not-browser-safe and
    transcoded, matching `_video_output_args`' existing safe default.
    """
    if codec in _BROWSER_SAFE:
        return MODE_TS
    if codec == "hevc" and client_hevc:
        return MODE_FMP4
    return MODE_H264


# What each delivery mode costs to serve, cheapest first. Used to rank a
# camera's tracks against each other.
_MODE_COST = {MODE_TS: 0, MODE_FMP4: 1, MODE_H264: 2}


def rank_track(mode: str, width: int | None, height: int | None) -> tuple[int, int]:
    """Sort key for a candidate track: cheaper to serve sorts first.

    Mode dominates pixels, deliberately. A stream-copyable main beats a
    transcoded sub however small the sub is — copying 1080p costs nothing and
    transcoding 352x288 still costs an ffmpeg. Within a mode, fewer pixels win:
    among two transcodes take the cheaper, and among two copies... the smaller
    one is also the cheaper to ship, which is what a scrubbing operator wants.
    """
    return (_MODE_COST.get(mode, 99), (width or 0) * (height or 0))


def covered_seconds(segments, from_epoch: float, to_epoch: float) -> float:
    """How much of [from_epoch, to_epoch] these segments actually cover.

    Overlaps are merged, and each segment is clipped to the window, so this is
    real coverage rather than a sum of durations — two copies of the same minute
    count once, and a segment straddling the start counts only its inside part.

    Track selection needs this because "has a segment in the window" and "covers
    the window" are different claims. A sub track switched on at 14:00 has one
    segment inside a 13:00-15:00 request and is the cheaper track, so ranking on
    cost alone hands back a playlist that starts at 14:00 — and the main's full
    first hour reads to an operator as a gap in recording, which is the one thing
    a playlist must never invent.
    """
    if to_epoch <= from_epoch:
        return 0.0
    spans = sorted(
        (max(s.start_epoch, from_epoch), min(s.start_epoch + s.duration, to_epoch))
        for s in segments
    )
    total = 0.0
    cur_start = cur_end = None
    for start, end in spans:
        if end <= start:
            continue                      # segment lies wholly outside the window
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def select_track(candidates: list[tuple]) -> tuple:
    """Pick the track to serve from ``[(rank, covered_seconds, payload), ...]``.

    Coverage decides, cost breaks the tie. Ranking on cost alone picked the
    cheapest track holding ANY segment in the window, so a sub switched on at
    14:00 won a 13:00-15:00 request and the main's complete first hour came
    back as a recording gap — a playlist inventing a hole in footage that
    exists, which is the worst thing a recorder can tell an operator.

    The tie is a tolerance rather than equality: the two tracks are written by
    separate ffmpeg processes and their segment boundaries never line up to the
    second, so demanding identical coverage would reject the sub on rounding
    alone and quietly disable the cheap-track optimisation everywhere. Within
    1% (minimum 1 s) of the best coverage counts as the same footage.

    Never rejects the last candidate: coverage ranks what there is, it does not
    withhold it. A thin playlist beats a 404 when thin is all that was recorded.
    """
    best_cov = max(c[1] for c in candidates)
    tol = max(1.0, 0.01 * best_cov)
    finalists = [c for c in candidates if c[1] >= best_cov - tol]
    finalists.sort(key=lambda c: c[0])
    return finalists[0]


def segment_stem(filepath: str) -> str:
    """`/data/nvr/cam/cam_20260904_173256.ts` → `cam_20260904_173256`."""
    return Path(filepath).stem


def _iso(epoch: float) -> str:
    """RFC 3339 with milliseconds — the form EXT-X-PROGRAM-DATE-TIME wants."""
    return (datetime.fromtimestamp(epoch, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def source_signature(filepath: str) -> str:
    """Short digest of (size, mtime) — the cache key's staleness guard.

    Grooming rewrites a segment in place; keying on content-identity means the
    rewritten segment simply misses the cache instead of serving the old video.
    """
    try:
        st = os.stat(filepath)
    except OSError:
        return "00000000"
    return hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:8]


def build_playlist(segments: list, mode: str, base: str) -> str:
    """Render an HLS VOD media playlist for `segments`.

    `base` is the URL prefix the client should resolve segment names against,
    already including the mode (e.g. `/api/nvr/hls/cam-1/fmp4/`). Callers pass
    a client-visible prefix because the NVR sits behind camera-mgmt's
    authenticated proxy and does not know its own external mount point.
    """
    fmp4 = mode in (MODE_FMP4, MODE_H264)
    target = max(1, math.ceil(max((s.duration for s in segments), default=1)))
    out = [
        "#EXTM3U",
        # v7 is the floor for EXT-X-MAP (fMP4); v3 for float EXTINF durations.
        f"#EXT-X-VERSION:{7 if fmp4 else 3}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    prev_end: float | None = None
    for seg in segments:
        stem = segment_stem(seg.filepath)
        sig = source_signature(seg.filepath)
        # Every segment restarts its PTS (see module docstring), so each one
        # opens a new discontinuity sequence. The first needs no marker.
        if prev_end is not None:
            out.append("#EXT-X-DISCONTINUITY")
        if fmp4:
            # Re-declared per segment rather than hoisted: each cached artifact
            # carries its own init, so a camera that changed resolution or
            # profile mid-history still plays instead of decoding garbage.
            out.append(f'#EXT-X-MAP:URI="{base}{stem}.{sig}.init.mp4"')
        out.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso(seg.start_epoch)}")
        out.append(f"#EXTINF:{seg.duration:.3f},")
        out.append(f"{base}{stem}.{sig}." + ("m4s" if fmp4 else "ts"))
        prev_end = seg.end_epoch
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def parse_artifact(name: str) -> tuple[str, str, str]:
    """`cam_20260904_173256.a1b2c3d4.m4s` → (stem, sig, kind).

    Raises ValueError on anything that isn't one of the three shapes this
    module emits — the only path-ish input the segment route accepts.
    """
    for suffix, kind in ((".init.mp4", "init"), (".m4s", "m4s"), (".ts", "ts")):
        if name.endswith(suffix):
            head = name[: -len(suffix)]
            break
    else:
        raise ValueError(f"unrecognised artifact name: {name!r}")
    stem, _, sig = head.rpartition(".")
    if not _STEM_RE.match(stem) or not _SIG_RE.match(sig):
        raise ValueError(f"unrecognised artifact name: {name!r}")
    return stem, sig, kind


class HlsCache:
    """On-disk cache of remuxed / transcoded segment artifacts.

    One instance per process, created in `create_app`. `transcode_args` is
    injected (rather than imported) so the encoder ladder stays owned by
    api/encoders.py and this module stays testable without it; `slot` is the
    clip semaphore's context manager — the transcode mode is the only path here
    that burns CPU and it must queue behind the same cap as /clip.
    """

    def __init__(self, root: Path, transcode_args, slot, ffmpeg: str = "ffmpeg",
                 cpu_transcode_args=None):
        self._root = Path(root)
        self._transcode_args = transcode_args
        # Per-job CPU fallback, mirroring what /clip has always done: a
        # transient NVENC failure (driver hiccup, exhausted encode sessions on
        # a consumer GPU) should cost CPU, not fail the segment and stall the
        # player. The startup probe stays authoritative for the default — one
        # bad job does not flip the mode. Without this, an NVENC blip on the
        # HLS path is a hard 500 where the clip path would have degraded.
        self._cpu_transcode_args = cpu_transcode_args
        self._slot = slot
        self._ffmpeg = ffmpeg
        # Per-key locks: two players seeking to the same segment must not run
        # two ffmpegs. Keyed by cache stem, pruned when the last waiter leaves.
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def camera_dir(self, camera: str) -> Path:
        return self._root / camera

    def artifact_path(self, camera: str, mode: str, stem: str, sig: str,
                      kind: str) -> Path:
        ext = {"init": "init.mp4", "m4s": "m4s", "ts": "ts"}[kind]
        return self.camera_dir(camera) / f"{stem}.{mode}.{sig}.{ext}"

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get(self, camera: str, mode: str, stem: str, sig: str, kind: str,
                  source: str, duration: float) -> Path:
        """Path to the requested artifact, building it if it isn't cached.

        `source` is the on-disk .ts; the caller has already confirmed it is the
        segment the index knows about and that `sig` still matches it.
        """
        want = self.artifact_path(camera, mode, stem, sig, kind)
        if want.exists():
            # Touch so the retention sweep ages the cache by last use, not by
            # when it was first built — a segment being scrubbed over and over
            # should outlive one nobody has opened since.
            try:
                os.utime(want, None)
            except OSError:
                pass
            return want

        key = f"{camera}/{mode}/{stem}.{sig}"
        lock = await self._lock_for(key)
        try:
            async with lock:
                if want.exists():                  # built while we waited
                    return want
                await self._materialise(camera, mode, stem, sig, source, duration)
        finally:
            # Outside `async with`, `locked()` is False unless another request
            # already took it — so this prunes the map without stranding a
            # waiter on a lock nobody else can see.
            async with self._locks_guard:
                if not lock.locked() and self._locks.get(key) is lock:
                    del self._locks[key]
        if not want.exists():
            raise HlsError(f"artifact missing after build: {want.name}")
        return want

    async def _materialise(self, camera: str, mode: str, stem: str, sig: str,
                           source: str, duration: float) -> None:
        """Run ffmpeg once, producing init.mp4 + .m4s for this segment.

        Written into a scratch directory and moved into place, so a killed or
        timed-out ffmpeg never leaves a half-file that a later request would
        happily serve as a cache hit.
        """
        out_dir = self.camera_dir(camera)
        out_dir.mkdir(parents=True, exist_ok=True)

        if mode == MODE_FMP4:
            attempts = [([], ["-c", "copy", "-tag:v", "hvc1"])]
            timeout = _REMUX_TIMEOUT_S
        elif mode == MODE_H264:
            attempts = [self._transcode_args()]
            if self._cpu_transcode_args is not None:
                cpu = self._cpu_transcode_args()
                if cpu != attempts[0]:
                    attempts.append(cpu)
            timeout = min(_TRANSCODE_BASE_TIMEOUT_S + duration, _MAX_TIMEOUT_S)
        else:
            raise HlsError(f"mode {mode!r} is served from disk, not built")

        with tempfile.TemporaryDirectory(dir=str(out_dir)) as scratch:
            def build(pre_args, vid_args):
                return [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error",
                    *pre_args,
                    "-i", source,
                    *vid_args,
                    "-an",
                    "-f", "hls",
                    "-hls_segment_type", "fmp4",
                    "-hls_fmp4_init_filename", "init.mp4",
                    "-hls_list_size", "0",
                    # One output segment per input segment: the boundaries are
                    # already where the recorder put them, and re-splitting here
                    # would desynchronise the playlist's durations from the index.
                    "-hls_time", "86400",
                    # The %d is not optional: ffmpeg's hls muxer rejects a segment
                    # template with no number pattern. -hls_time above guarantees
                    # there is only ever a seg0.
                    "-hls_segment_filename", os.path.join(scratch, "seg%d.m4s"),
                    os.path.join(scratch, "out.m3u8"),
                ]

            for i, (pre_args, vid_args) in enumerate(attempts):
                cmd = build(pre_args, vid_args)
                try:
                    if mode == MODE_H264:
                        async with self._slot():
                            await self._run(cmd, timeout)
                    else:
                        await self._run(cmd, timeout)
                    break
                except HlsError as e:
                    if i == len(attempts) - 1:
                        raise
                    logger.warning(
                        "HLS hardware transcode failed for %s (%s) — retrying on CPU",
                        stem, e)
                    # A partial write from the failed attempt would otherwise be
                    # picked up as this attempt's output.
                    for leftover in Path(scratch).glob("*"):
                        leftover.unlink(missing_ok=True)

            init_src = Path(scratch) / "init.mp4"
            seg_src = Path(scratch) / "seg0.m4s"
            if not init_src.exists() or not seg_src.exists():
                raise HlsError("ffmpeg produced no fMP4 output")
            # Media first, init second: the playlist sends the player to the
            # init file first, so publishing it last means a reader can never
            # see an init whose media isn't there yet.
            os.replace(seg_src, self.artifact_path(camera, mode, stem, sig, "m4s"))
            os.replace(init_src, self.artifact_path(camera, mode, stem, sig, "init"))

    async def _run(self, cmd: list[str], timeout: float) -> None:
        logger.debug("HLS materialise: %s", " ".join(cmd))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise HlsError(f"could not start ffmpeg: {e}") from e
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise HlsError(f"ffmpeg timed out after {timeout:g}s")
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:300]
            raise HlsError(f"ffmpeg failed (rc={proc.returncode}): {detail}")

    def stats(self) -> dict:
        """Size and shape of the artifact cache, for /health.

        The cache is what makes re-seeking free, so "is it working" is a real
        operational question — an empty cache on a busy appliance means every
        seek is paying full price and something (a too-short TTL, the disk-floor
        sweep, grooming churning signatures) is throwing the work away.
        """
        cameras = files = 0
        total = 0
        by_mode: dict[str, int] = {}
        if self._root.is_dir():
            for cam_dir in self._root.iterdir():
                if not cam_dir.is_dir():
                    continue
                cameras += 1
                for f in cam_dir.iterdir():
                    try:
                        if not f.is_file():
                            continue
                        total += f.stat().st_size
                        files += 1
                    except OSError:
                        continue
                    # `<stem>.<mode>.<sig>.<ext>` — the mode is the segment
                    # after the recorder's `_HHMMSS`.
                    parts = f.name.split(".")
                    if len(parts) >= 3:
                        by_mode[parts[1]] = by_mode.get(parts[1], 0) + 1
        return {"cameras": cameras, "artifacts": files,
                "bytes": total, "by_mode": by_mode}

    def purge_camera(self, camera: str) -> int:
        """Drop a camera's whole cache. Returns bytes reclaimed.

        Called when its footage is purged or the camera is removed. Serving
        already refuses artifacts whose source segment is gone, so this is
        about reclaiming space promptly, not about correctness.
        """
        d = self.camera_dir(camera)
        if not d.is_dir():
            return 0
        freed = 0
        for f in d.iterdir():
            try:
                freed += f.stat().st_size if f.is_file() else 0
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)
        return freed
