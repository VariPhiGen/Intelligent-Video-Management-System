#!/usr/bin/env python3
"""replay — recorded NVR segments as deterministic stand-in cameras.

WHY NOT REUSE CameraSampler. It paces by wall clock (`next_due`) but calls
`grab()` flat out, which self-paces against RTSP because grab() blocks until the
next packet arrives — roughly 40 ms at 25 fps. Against a FILE grab() returns
immediately, so the same loop races through a 60-second segment in a few seconds
and burns a whole core. Pointing the production sampler at a file would measure
the decoder's top speed, not a camera.

WHY NOT LIVE RTSP EITHER, which is what this replaces. Scaling a live test past
a handful of cameras means many subscribers on the same MediaMTX path, and it
starts refusing them: a 32-camera step connected 23, which is a property of the
relay rather than of Smart Search. The previous run also could not be repeated,
because the scene changed between steps — the quiet-hours run recorded almost no
detections and so measured decode with the inference switched off by accident.

SO: each logical camera decodes its own copy of a fixed segment, paced to the
source frame rate, looping at end of file. That reproduces what actually costs
money — a continuous demux of every frame to sample one of them — while being
repeatable to the frame, independent of the network, and unable to disturb
production. Segments are opened READ-ONLY and never written.

DETERMINISM IS THE POINT. The same segment list, the same frame rate, the same
duration, the same warm-up, on every run and at every camera count. What varies
between steps is the camera count and nothing else.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("smartsearch.replay")

SEGMENT_DB = os.environ.get("NVR_SEGMENT_DB", "/data/nvr/segments.db")

#: Measured on this site's recordings: packets/duration gave 24.9-25.0 fps on
#: every camera. The containers' declared r_frame_rate lies (cam1 and cam3 claim
#: 100/1, cam5 50/1), so it is not read from the file — a wrong value here would
#: change the decode load, which is the main thing being measured.
DEFAULT_SOURCE_FPS = 25.0

QUIET = "quiet"
BUSY = "busy"


@dataclass
class Segment:
    camera: str
    path: str
    start_epoch: float
    duration: float
    size_bytes: int

    def to_dict(self) -> dict:
        return {"camera": self.camera, "file": os.path.basename(self.path),
                "start_utc": datetime.fromtimestamp(
                    self.start_epoch, tz=timezone.utc).isoformat(),
                "duration_s": round(self.duration, 1),
                "size_mb": round(self.size_bytes / 1e6, 1)}


def _local_hour(epoch: float, tz_offset_hours: float) -> int:
    return datetime.fromtimestamp(
        epoch + tz_offset_hours * 3600, tz=timezone.utc).hour


def select_segments(kind: str, count: int, *, hours: tuple[int, int],
                    tz_offset_hours: float = 5.5,
                    min_size_bytes: int = 2_000_000,
                    cameras: tuple[str, ...] = (),
                    exclude: tuple[str, ...] = (),
                    db: str = SEGMENT_DB) -> list[Segment]:
    """Fixed, reproducible source footage for one workload.

    Chosen by local-time window rather than by querying the search index: the
    index records what the CURRENT detector found, so selecting on it would
    make the benchmark's input depend on the thing being benchmarked. A clock
    window is a property of the recording alone.

    Sorted deterministically and taken from the front, so two runs with the
    same arguments replay exactly the same files.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT camera, filepath, start_epoch, duration, file_size "
            "FROM segments ORDER BY camera, start_epoch").fetchall()
    finally:
        con.close()

    lo, hi = hours
    out: list[Segment] = []
    for camera, path, start, dur, size in rows:
        if cameras and camera not in cameras:
            continue
        if camera in exclude:
            continue
        if not (lo <= _local_hour(start, tz_offset_hours) < hi):
            continue
        # A tiny segment is a truncated one — the recorder was starting or
        # stopping. Replaying it would loop every couple of seconds.
        if not size or size < min_size_bytes or not os.path.exists(path):
            continue
        out.append(Segment(camera, path, start, dur or 60.0, size))

    # Round-robin across cameras so a 4-camera run is not four copies of one
    # scene: the decode cost depends on resolution and bitrate, which differ
    # per camera (measured 0.6 to 4.3 Mbps here).
    by_cam: dict[str, list[Segment]] = {}
    for s in out:
        by_cam.setdefault(s.camera, []).append(s)
    ordered: list[Segment] = []
    i = 0
    while len(ordered) < count and any(len(v) > i for v in by_cam.values()):
        for cam in sorted(by_cam):
            if len(ordered) >= count:
                break
            if len(by_cam[cam]) > i:
                ordered.append(by_cam[cam][i])
        i += 1
    return ordered[:count]


class ReplaySampler:
    """One logical camera: a segment file, decoded at real time, sampled at N fps.

    Mimics CameraSampler's OBSERVABLE behaviour — `slug`, `state`,
    `frames_sampled`, and calling `on_frame(slug, frame, ts)` — so the pipeline
    under test cannot tell the difference. What it does not mimic is the
    reconnect backoff, which has nothing to do with capacity.

    PACING IS THE WHOLE TRICK. Every frame is grabbed, as a real camera forces,
    but the loop sleeps to keep pace with the source frame rate instead of
    running as fast as the disk allows. Only one frame per sample interval is
    retrieved and decoded to an array, which is exactly the production split
    between cheap `grab()` and expensive `retrieve()`.
    """

    def __init__(self, slug: str, segment: Segment, sample_fps: float,
                 on_frame: Callable, on_reconnect: Optional[Callable] = None,
                 source_fps: float = DEFAULT_SOURCE_FPS) -> None:
        self.slug = slug
        self.segment = segment
        self._interval = 1.0 / sample_fps if sample_fps > 0 else 1.0
        self._on_frame = on_frame
        self._on_reconnect = on_reconnect
        self._source_fps = source_fps
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.state = "CONNECTING"
        self.frames_sampled = 0
        self.frames_grabbed = 0
        self.loops = 0
        self.last_error: Optional[str] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"replay-{self.slug}",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        import cv2

        started = time.monotonic()
        grabbed_total = 0
        next_due = 0.0
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.segment.path)
            if not cap.isOpened():
                self.state = "FAILED"
                self.last_error = f"cannot open {self.segment.path}"
                log.error("replay %s: %s", self.slug, self.last_error)
                return
            self.state = "SAMPLING"
            if self._on_reconnect is not None and self.loops:
                try:
                    self._on_reconnect(self.slug)
                except Exception:                                  # noqa: BLE001
                    pass
            try:
                while not self._stop.is_set():
                    if not cap.grab():
                        break                       # end of file: loop below
                    grabbed_total += 1
                    self.frames_grabbed = grabbed_total

                    # Hold the source frame rate. Without this the file is
                    # consumed at decode speed and the measurement becomes
                    # "how fast can this box demux", not "can it keep up with
                    # cameras".
                    target = started + grabbed_total / self._source_fps
                    slack = target - time.monotonic()
                    if slack > 0:
                        if self._stop.wait(slack):
                            return

                    now = time.time()
                    if now < next_due:
                        continue                   # grabbed and discarded
                    ok, frame = cap.retrieve()
                    if not ok or frame is None or frame.size == 0:
                        continue
                    next_due = now + self._interval
                    self.frames_sampled += 1
                    try:
                        self._on_frame(self.slug, frame, now)
                    except Exception as exc:                       # noqa: BLE001
                        log.warning("replay %s handler failed: %s", self.slug, exc)
            finally:
                cap.release()
            self.loops += 1

    def snapshot(self) -> dict:
        return {"slug": self.slug, "state": self.state,
                "source": os.path.basename(self.segment.path),
                "frames_sampled": self.frames_sampled,
                "frames_grabbed": self.frames_grabbed, "loops": self.loops,
                "last_error": self.last_error}


def build_cameras(kind: str, count: int, sample_fps: float, on_frame,
                  on_reconnect=None, **kw) -> tuple[list[ReplaySampler], list[Segment]]:
    """`count` logical cameras drawn from the chosen workload's segments.

    When more cameras are asked for than there are distinct segments, the list
    REPEATS — deliberately. Each logical camera still runs its own decoder and
    its own motion gate, so the per-camera cost is real; only the pixels are
    shared. That is what makes a 40-camera step possible on a site with five
    physical cameras, and it is honest as long as the report says so.
    """
    segments = select_segments(kind, count, **kw)
    if not segments:
        return [], []
    cams = []
    for i in range(count):
        seg = segments[i % len(segments)]
        cams.append(ReplaySampler(f"replay-{i:03d}", seg, sample_fps,
                                  on_frame, on_reconnect))
    return cams, segments
