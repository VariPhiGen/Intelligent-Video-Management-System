"""
StreamWorker — manages a single ffmpeg process that records one RTSP stream
into fixed-duration MPEG-TS segments.

Design decisions:
- MPEG-TS segments (not MP4): TS is a streaming container; if ffmpeg or the
  stream dies mid-write, the partial segment is still valid up to the last
  complete packet. MP4 requires a finalized moov atom — a crash loses the
  entire segment. For continuous recording, TS resilience is essential.
- `-c copy` (no transcoding): Cameras output H.264; we remux into TS with
  zero CPU cost. One machine can record 40+ streams since it's I/O-bound.
- `-segment_time 60`: 60-second segments balance file count (1440/day/camera)
  against seek granularity. For the 20-second clip API, we need at most 2
  segments to span any request window.
- `-strftime 1`: Embeds wall-clock start time in the filename, giving us a
  filesystem-level index without any database dependency.
- `-segment_list <file>`: ffmpeg writes completed segment metadata
  (filename, start_pts, end_pts) to a CSV. We poll this file and use the
  filename as the watermark (NOT the line count): ffmpeg truncates the file
  on each restart, so a line-count watermark silently misses every new
  segment until the count exceeds the previous high.
- `-rtsp_transport tcp`: TCP is more reliable than UDP for most LAN setups.
  UDP can lose packets under congestion, causing artifacts.
- `-timeout 5000000` (5 s, microseconds): in ffmpeg 7.x's RTSP demuxer this
  serves as both the connect timeout AND the socket I/O timeout (it sets
  SO_RCVTIMEO/SO_SNDTIMEO on the TCP transport). So a half-open RTSP
  socket where packets stop flowing causes ffmpeg to exit within 5 s, and
  the watchdog/backoff loop takes over. Note: `-rw_timeout` and the older
  `-stimeout` are NOT accepted as input options for the RTSP demuxer in
  modern ffmpeg builds — they fail immediately with "Option not found".

Reconnection strategy:
  On ffmpeg exit (stream drop, network error), we wait current_backoff
  seconds (with jitter) and restart. The backoff doubles after each
  consecutive failure and resets to the base value as soon as the next
  successful segment is indexed.
"""

import logging
import os
import random
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from storage.index import Segment, SegmentIndex

logger = logging.getLogger(__name__)

# scheme://user:password@ with the password matched GREEDILY to the last '@'
# in the token, so raw '/' and '@' inside a hand-typed password stay masked
# (mirrors camera-mgmt's urlutil.redact_credentials — this service cannot
# import it). The relay URLs this NVR normally records are credential-free,
# but a hand-written cameras.yaml entry carries inline camera credentials
# (the example file shows exactly that shape).
_CRED_RE = re.compile(r"(\w+://[^:/@\s]*):(\S+)@")


def _redact_credentials(text: str) -> str:
    """`text` with URL-embedded passwords replaced by `***`, for logs.

    Applied to the ffmpeg command line AND to every relayed stderr line:
    ffmpeg embeds the full credentialed input URL in its connection errors and
    'Input #0' banner, so an unredacted stderr relay leaks the password once
    per reconnect attempt — densest exactly while an operator is debugging the
    camera that will not connect.
    """
    return _CRED_RE.sub(r"\1:***@", text)


class StreamWorker:
    """Manages ffmpeg recording for a single camera."""

    MAX_BACKOFF_SECONDS = 60.0
    JITTER_FRACTION = 0.25  # add up to 25% random jitter to each delay

    def __init__(self, camera_name: str, rtsp_url: str,
                 storage_path: Path, index: SegmentIndex,
                 segment_duration: int = 60,
                 rtsp_transport: str = "tcp",
                 reconnect_delay: int = 5,
                 ffmpeg_loglevel: str = "warning",
                 masks: list | None = None):
        self.camera = camera_name
        self.rtsp_url = rtsp_url
        self.segment_dir = storage_path / camera_name
        self.index = index
        self.segment_duration = segment_duration
        self.rtsp_transport = rtsp_transport
        self.base_reconnect_delay = float(reconnect_delay)
        self.ffmpeg_loglevel = ffmpeg_loglevel
        # Privacy masks (normalized polygons). Non-empty switches this worker
        # from the zero-CPU `-c copy` path to decode → overlay → re-encode:
        # the masked region is burned into every segment, so playback, clip
        # exports, and snapshots are all masked. CPU cost is per-camera and
        # opt-in — unmasked cameras are completely unaffected.
        self.masks = masks or []

        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False

        # Indexer watermark: lex-comparable filename (timestamp-encoded).
        # Resilient to CSV truncation on ffmpeg restart, unlike line-counting.
        self._last_indexed_filename: str = ""

        # Metrics exposed via engine.get_status() / /health endpoint.
        self._last_indexed_at: float | None = None
        self._segments_indexed: int = 0
        self._current_backoff: float = self.base_reconnect_delay

    def start(self) -> None:
        """Start the recording loop in a background thread."""
        if self._running:
            return
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name=f"rec-{self.camera}", daemon=True
        )
        self._thread.start()
        logger.info("Started recording worker for camera '%s'", self.camera)

    def stop(self) -> None:
        """Signal the worker to stop and wait for shutdown."""
        if not self._running:
            return
        logger.info("Stopping recording worker for camera '%s'", self.camera)
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._thread:
            self._thread.join(timeout=15)
        self._running = False

    @property
    def is_alive(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def last_indexed_at(self) -> float | None:
        return self._last_indexed_at

    @property
    def segments_indexed(self) -> int:
        return self._segments_indexed

    @property
    def current_backoff(self) -> float:
        return self._current_backoff

    def get_metrics(self) -> dict:
        """Snapshot of indexing health for /health and engine watchdog."""
        now = time.time()
        lag = (now - self._last_indexed_at) if self._last_indexed_at else None
        return {
            "alive": self.is_alive,
            "last_indexed_at": self._last_indexed_at,
            "index_lag_seconds": lag,
            "segments_indexed": self._segments_indexed,
            "current_backoff": self._current_backoff,
        }

    # ── Segment-roll watchdog ────────────────────────────────────────────────
    # A flaky/corrupt RTSP source can keep bytes flowing (so ffmpeg's -timeout
    # never fires) while the segment muxer stops advancing PTS: ffmpeg then
    # pours hours into ONE ever-growing .ts and never completes a segment, so
    # the CSV/indexer watermark freezes and playback sees no new footage. The
    # process and thread are both alive, so neither the liveness restart nor
    # the staleness alarm recovers it — previously only a manual restart did.
    # These helpers detect that exact state so the engine can restart the worker.
    WEDGE_ROLL_FACTOR = 3.0      # open segment older than N×duration = not rolling
    WEDGE_ACTIVE_FACTOR = 1.0    # ...but only if written within N×duration (growing)

    @staticmethod
    def _is_wedged(open_for: float | None, since_write: float | None,
                   segment_duration: int,
                   roll_factor: float = WEDGE_ROLL_FACTOR,
                   active_factor: float = WEDGE_ACTIVE_FACTOR) -> bool:
        """True when the newest segment has been open too long to still be
        rolling (``open_for`` > roll_factor×duration) AND is still being written
        (``since_write`` < active_factor×duration).

        The second half is what separates a wedged muxer (restart it) from an
        offline camera whose last segment is merely old and idle (leave that to
        -timeout/backoff — restarting it would thrash a genuinely-down camera).
        """
        if open_for is None or since_write is None:
            return False
        return (open_for > roll_factor * segment_duration
                and since_write < active_factor * segment_duration)

    def newest_segment_stat(self, now: float | None = None) -> tuple[float, float] | None:
        """``(open_for, since_write)`` seconds for the newest .ts in this
        camera's dir, or ``None`` if there are none / it can't be read.

        ``open_for`` is measured from the filename's encoded start time (the
        same wall-clock stamp ffmpeg's ``-strftime`` writes); ``since_write``
        from the file's mtime. Newest = lexically-greatest filename, which is
        chronological because names are timestamp-encoded. Uses a single
        ``scandir`` (names only) and stats just the winner, so it stays cheap
        on cameras with tens of thousands of segments.
        """
        if now is None:
            now = time.time()
        newest = None
        try:
            with os.scandir(self.segment_dir) as it:
                for entry in it:
                    n = entry.name
                    if n.endswith(".ts") and (newest is None or n > newest):
                        newest = n
        except OSError:
            return None
        if newest is None:
            return None
        start_epoch = self._parse_filename_time(newest)
        if start_epoch is None:
            return None
        try:
            mtime = (self.segment_dir / newest).stat().st_mtime
        except OSError:
            return None
        return (now - start_epoch, now - mtime)

    def is_segment_stalled(self, now: float | None = None) -> bool:
        """True when ffmpeg is running but wedged on a non-rolling segment.

        Only meaningful while a live ffmpeg is supposed to be producing
        segments — between reconnect attempts ``_process`` is ``None``, so this
        returns False and the backoff loop owns recovery.
        """
        # Capture the handle once — the worker thread may clear _process
        # (set it to None between _record() runs) concurrently with this read.
        proc = self._process
        if not self._running or proc is None or proc.poll() is not None:
            return False
        stat = self.newest_segment_stat(now=now)
        if stat is None:
            return False
        open_for, since_write = stat
        return self._is_wedged(open_for, since_write, self.segment_duration)

    def _run_loop(self) -> None:
        """Main loop: start ffmpeg, read segment list, restart on failure."""
        while not self._stop_event.is_set():
            try:
                self._record()
            except Exception:
                logger.exception("Unexpected error in recorder for '%s'",
                                 self.camera)
            if self._stop_event.is_set():
                break
            delay = self._compute_backoff_delay()
            logger.warning(
                "Camera '%s': ffmpeg exited, reconnecting in %.1fs "
                "(backoff base=%.1fs)",
                self.camera, delay, self._current_backoff,
            )
            self._stop_event.wait(delay)
            # Escalate backoff for the next failure cycle. Capped at MAX.
            # Reset to base happens in _index_segment_line on first
            # successful segment indexed after reconnection.
            self._current_backoff = min(
                self._current_backoff * 2.0, self.MAX_BACKOFF_SECONDS
            )

    def _compute_backoff_delay(self) -> float:
        jitter = random.uniform(0.0, self.JITTER_FRACTION * self._current_backoff)
        return self._current_backoff + jitter

    def _video_args(self) -> list[str]:
        """Video half of the ffmpeg command: stream-copy, or (with privacy
        masks) blur + re-encode.

        Masked path: the stream's native resolution is probed and the polygons
        are rasterized to a PNG at that exact size on EVERY ffmpeg start, so a
        camera-side resolution change self-heals on the next reconnect. A probe
        failure raises — the normal backoff/retry loop handles it (recording
        unmasked would be a silent privacy violation; never fall back to copy).

        The masked region is Gaussian-blurred (not blacked out): a full-frame
        blurred copy is composited back over the sharp frame using the mask's
        alpha channel as the stencil. Blur sigma scales with resolution so the
        strength looks the same at 720p or 4K. The PNG's fill colour is
        irrelevant now — only its alpha marks the region.
        """
        if not self.masks:
            return ["-c:v", "copy"]

        from recorder import masking

        width, height = masking.probe_resolution(self.rtsp_url, self.rtsp_transport)
        mask_png = masking.render_mask_png(
            self.masks, width, height, self.segment_dir / "privacy_mask.png"
        )
        # Resolution-relative, heavy blur (min 15 so small streams still smear).
        sigma = max(15, min(60, round(width / 50)))
        filtergraph = (
            "[0:v]split=2[base][reg];"
            f"[reg]gblur=sigma={sigma}:steps=3[blur];"
            "[1:v]alphaextract[stencil];"
            "[blur][stencil]alphamerge[fg];"
            # format=yuv420p: overlay can emit yuva420p, which libx264 rejects.
            "[base][fg]overlay=0:0:format=auto,format=yuv420p[v]"
        )
        return [
            "-i", str(mask_png),
            "-filter_complex", filtergraph,
            "-map", "[v]",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            # Keyframe at every segment boundary so masked segments cut as
            # cleanly as copied ones (copy relies on the camera's own GOPs).
            "-force_key_frames", f"expr:gte(t,n_forced*{self.segment_duration})",
        ]

    def _record(self) -> None:
        """Run one ffmpeg session until it exits or we're told to stop."""
        segment_pattern = str(
            self.segment_dir / f"{self.camera}_%Y%m%d_%H%M%S.ts"
        )
        segment_list = str(self.segment_dir / "segments.csv")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", self.ffmpeg_loglevel,
            # RTSP input options
            "-rtsp_transport", self.rtsp_transport,
            "-timeout", "5000000",       # connect + I/O timeout 5s (µs); kills hung streams
            "-i", self.rtsp_url,
            # Video: `-c copy` normally; overlay + re-encode when masks are set
            *self._video_args(),
            "-an",                       # drop audio — AI pipelines rarely need it
            "-f", "segment",
            "-segment_time", str(self.segment_duration),
            "-segment_format", "mpegts",
            "-segment_list", segment_list,
            "-segment_list_type", "csv",
            "-segment_list_size", "0",   # keep full list (we truncate on retention)
            "-strftime", "1",
            "-reset_timestamps", "1",
            # NO -break_non_keyframes. It let ffmpeg cut a segment mid-GOP to
            # hit the time target exactly, and a segment that starts mid-GOP
            # cannot be decoded from its first frame — which is visible now that
            # playback serves whole segments over HLS rather than seeking past
            # the start of one. Measured on this appliance: entry-g98a's
            # segments began 1.53 s after their own first keyframe, cabin-u9tn
            # 0.32-0.92 s and drifting, exit-gate 0.24 s. Recording live off the
            # relay with and without the flag, dropping it moved entry-g98a to
            # exactly 0.00 s.
            #
            # The cost is that segments now end on the first keyframe past the
            # target instead of on the target, so their length varies by up to
            # one GOP. Measured GOPs here are 2.0 s and 16.7 s, so a 60 s
            # segment becomes 60-77 s — well inside the segment-stall watchdog's
            # 3x threshold, and HLS carries each segment's real duration anyway.
            segment_pattern,
        ]

        logger.info("Camera '%s': starting ffmpeg: %s", self.camera,
                     _redact_credentials(" ".join(cmd)))

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Monitor stderr in a separate thread for logging
        stderr_thread = threading.Thread(
            target=self._log_stderr, daemon=True
        )
        stderr_thread.start()

        # Poll for new segment files by watching the CSV list
        self._watch_segment_list(segment_list)

        self._process.wait()
        self._process = None

    def _watch_segment_list(self, csv_path: str) -> None:
        """Watch the segment list CSV and index new segments as they complete.

        ffmpeg's segment_list CSV format: filename,start_pts,end_pts.

        Watermark strategy: we track the highest filename we've already
        indexed. On each poll, we re-parse the entire CSV and skip any line
        whose filename is <= the watermark. Filenames are timestamp-encoded
        (`{camera}_YYYYMMDD_HHMMSS.ts`) so lexical comparison is monotonic.

        Why not line count? ffmpeg reopens segments.csv with O_TRUNC on each
        invocation. After a reconnect, the new file has fewer lines than the
        previous high-water mark, so a line-count check never fires and the
        indexer silently stalls until the count climbs back.
        """
        while self._process and self._process.poll() is None:
            if self._stop_event.is_set():
                return
            try:
                if not os.path.exists(csv_path):
                    time.sleep(1)
                    continue
                with open(csv_path, "r") as f:
                    lines = f.readlines()
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    filename = stripped.split(",", 1)[0].strip()
                    if not filename:
                        continue
                    base = os.path.basename(filename)
                    if base <= self._last_indexed_filename:
                        continue
                    self._index_segment_line(stripped)
            except Exception:
                logger.exception("Error reading segment list for '%s'",
                                 self.camera)
            time.sleep(2)  # check every 2 seconds

    def _index_segment_line(self, line: str) -> None:
        """Parse a CSV line and add the segment to the index.

        On successful index, advances the filename watermark, updates
        metrics, and resets the reconnect backoff.
        """
        # Format: filename,start_pts,end_pts
        parts = line.split(",")
        if len(parts) < 3:
            logger.warning(
                "Camera '%s': dropping malformed CSV line (got %d fields): %r",
                self.camera, len(parts), line,
            )
            return
        filename = parts[0].strip()
        if not filename or not filename.endswith(".ts"):
            logger.warning(
                "Camera '%s': dropping CSV line with bad filename: %r",
                self.camera, filename,
            )
            return

        filepath = self.segment_dir / os.path.basename(filename) \
            if not os.path.isabs(filename) else Path(filename)

        if not filepath.exists():
            # Most likely a flush race: ffmpeg wrote the CSV row before the
            # .ts file was visible to us. Reconciliation will pick it up
            # later; do NOT advance the watermark, so we re-attempt it.
            logger.warning(
                "Camera '%s': CSV references missing file %s "
                "(flush race or deleted)",
                self.camera, filepath,
            )
            return

        # Parse wall-clock start time from filename: camera_YYYYMMDD_HHMMSS.ts
        start_epoch = self._parse_filename_time(filepath.name)
        if start_epoch is None:
            logger.warning(
                "Camera '%s': cannot parse timestamp from filename %s",
                self.camera, filepath.name,
            )
            return

        # Compute duration from PTS values
        try:
            start_pts = float(parts[1].strip())
            end_pts = float(parts[2].strip())
            duration = end_pts - start_pts
            if duration <= 0:
                # Fallback: probe the file
                duration = self._probe_duration(str(filepath))
        except (ValueError, IndexError):
            duration = self._probe_duration(str(filepath))

        if duration is None or duration <= 0:
            logger.warning(
                "Camera '%s': skipping segment with invalid duration: %s",
                self.camera, filepath,
            )
            return

        seg = Segment(
            camera=self.camera,
            start_epoch=start_epoch,
            duration=duration,
            filepath=str(filepath),
            file_size=filepath.stat().st_size,
        )
        self.index.add_segment(seg)
        self._mark_indexed(filepath.name)
        logger.debug("Indexed segment: %s (%.1fs, %.1f MB)",
                      filepath.name, duration,
                      seg.file_size / (1024 * 1024))

    def _mark_indexed(self, filename: str) -> None:
        """Update watermark + metrics + reset backoff on successful index."""
        if filename > self._last_indexed_filename:
            self._last_indexed_filename = filename
        self._last_indexed_at = time.time()
        self._segments_indexed += 1
        # Successful indexing means the stream is healthy — reset backoff.
        if self._current_backoff != self.base_reconnect_delay:
            logger.info(
                "Camera '%s': successful index after backoff, resetting "
                "delay to %.1fs",
                self.camera, self.base_reconnect_delay,
            )
            self._current_backoff = self.base_reconnect_delay

    def reconcile(self, lookback_seconds: int | None = None) -> int:
        """Filesystem-vs-index delta scan. Returns count of segments added.

        Looks at .ts files in this camera's segment_dir whose filename
        timestamp falls within the last `lookback_seconds`. For any file
        not already in the index, probes the duration and adds it.

        `lookback_seconds=None` disables the time cutoff and scans every
        .ts file in the directory. Use this when the bound is already
        enforced elsewhere (e.g. retention deletes anything older), or for
        recovery scenarios where the camera was offline longer than any
        fixed window. Callers that pass an int bound the scan to a window
        — useful to keep the glob cheap on cameras with deep history.

        Skips files modified within the last 5 seconds (likely still being
        written by ffmpeg). Safe to call concurrently with normal indexing
        — `INSERT OR REPLACE` makes add_segment idempotent.
        """
        if not self.segment_dir.is_dir():
            return 0

        now = time.time()
        cutoff = (now - lookback_seconds) if lookback_seconds is not None else 0.0
        try:
            indexed_paths = self.index.get_indexed_filepaths_since(
                self.camera, cutoff
            )
        except Exception:
            logger.exception(
                "Camera '%s': reconciliation failed to query index",
                self.camera,
            )
            return 0

        added = 0
        for ts_file in self.segment_dir.glob("*.ts"):
            try:
                if str(ts_file) in indexed_paths:
                    continue
                start_epoch = self._parse_filename_time(ts_file.name)
                if start_epoch is None:
                    continue
                if lookback_seconds is not None and start_epoch < cutoff:
                    continue
                stat = ts_file.stat()
                # Heuristic: if mtime updated in the last 5s, ffmpeg is
                # likely still writing — let the next pass pick it up.
                if now - stat.st_mtime < 5.0:
                    continue
                duration = self._probe_duration(str(ts_file))
                if duration is None or duration <= 0:
                    continue
                seg = Segment(
                    camera=self.camera,
                    start_epoch=start_epoch,
                    duration=duration,
                    filepath=str(ts_file),
                    file_size=stat.st_size,
                )
                self.index.add_segment(seg)
                self._mark_indexed(ts_file.name)
                added += 1
                logger.warning(
                    "Camera '%s': reconciliation indexed missing segment %s",
                    self.camera, ts_file.name,
                )
            except OSError as e:
                logger.warning(
                    "Camera '%s': reconciliation skipped %s: %s",
                    self.camera, ts_file.name, e,
                )
        return added

    def _parse_filename_time(self, filename: str) -> float | None:
        """Extract Unix epoch from segment filename like camera_20260317_143210.ts

        ffmpeg's ``-strftime 1`` stamps filenames in the process's LOCAL time
        (the TZ env var), so parse them the same way. Interpreting them as UTC
        shifted every segment by the UTC offset whenever TZ != UTC (e.g. +5:30
        under Asia/Kolkata) — timeline, playback and LIVE all drifted with it.
        Naive-datetime ``.astimezone()`` attaches the local zone, DST-safe.
        """
        match = re.search(r"(\d{8})_(\d{6})\.ts$", filename)
        if not match:
            return None
        try:
            dt = datetime.strptime(
                match.group(1) + match.group(2), "%Y%m%d%H%M%S"
            ).astimezone()
            return dt.timestamp()
        except ValueError:
            return None

    @staticmethod
    def _probe_duration(filepath: str) -> float | None:
        """Use ffprobe to get exact duration of a segment file."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    filepath,
                ],
                capture_output=True, text=True, timeout=10,
            )
            return float(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return None

    def _log_stderr(self) -> None:
        """Read and log ffmpeg stderr."""
        if not self._process or not self._process.stderr:
            return
        for line in self._process.stderr:
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                logger.debug("ffmpeg [%s]: %s", self.camera,
                             _redact_credentials(decoded))
