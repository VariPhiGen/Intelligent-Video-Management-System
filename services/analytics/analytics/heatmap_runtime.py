"""heatmap_runtime.py — one heat session per camera: when it accumulates, when it renders.

The activity (`activities/Heatmap_overlay.py`) decides WHICH points are heat.
This decides WHETHER a camera is accumulating at all, WHICH session the heat
belongs to, and WHEN a picture is made. It is separate so the lifecycle can be
tested without frames, a detector or a tracker.

THREE THINGS GATE ACCUMULATION, and they are not the same thing:

  * the AI Config SCHEDULE (`active_days` / `active_hours` on the activity) —
    when heatmap processing is ALLOWED to run. Owned by the activity's spec.
  * the RUNTIME switch — an operator's per-camera ON/OFF from the Heatmap page,
    pushed here by camera-mgmt. It never rewrites the schedule.
  * a camera being CONFIGURED for the activity at all.

A session is one continuous accumulation. It ends when the schedule window
closes (`closed`) or an operator switches the camera off (`frozen`); the matrix
behind a frozen session is KEPT in memory, so "resume" continues it and costs
nothing while off. Sessions are identified here, not in the database: analytics
is the only thing that knows when heat actually started accumulating, and a
locally minted id survives an API that is briefly unreachable.

WHAT SURVIVES A RESTART, and what does not. Session metadata and every rendered
artifact live in Postgres, written by camera-mgmt as they are produced, so the
page keeps showing the last overlay across any restart. The float matrix itself
is memory only: on restart the session resumes and accumulates from empty, and
the last artifact stays on screen until the next snapshot replaces it. Keeping
the matrix would mean writing ~130 KB per camera per minute into the database
(a 480x270 float32 matrix, PNG-compressed as uint16) — 190 MB per camera per
day of write traffic to survive an event that happens on deploys. If a
restart-recovery requirement appears, `state()`/`restore()` below are the seam:
they move a matrix in and out without touching anything else.

OFF COSTS NOTHING. `observe()` returns immediately for a camera that is off or
out of window: no accumulation, no render, no encode, no upload.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import io
import json

import cv2
import numpy as np

from .heatmap import (BLUR_PX, CEILING, FRAME_ALPHA, JPEG_QUALITY, OUTPUT_WIDTH, SCALE,
                      HeatAccumulator, Snapshot)

log = logging.getLogger("analytics.heatmap")

#: How often a session renders a picture. NOT the accumulation window: heat
#: keeps accumulating across snapshots for the whole session.
SNAPSHOT_INTERVAL_S = 60.0

#: STORAGE BLOCKS. Every CHUNK_S the heat ADDED in that span is handed to the
#: chunk sink as a numpy array (the VMS keeps it in MinIO). A block is an
#: increment, not a running total, so the heat between any two times is the sum
#: of the blocks between them. Blocks end on the local clock's 5-minute marks,
#: and early when heat stops being added (schedule end, operator off, settings
#: or frame size changed), so a block never mixes two kinds of heat.
CHUNK_S = 300.0
#: A camera frame goes with a block at most this often. The VMS keeps ONE frame
#: per camera and overwrites it, so this is how fresh the background of every
#: heatmap (today's and any past range) is -- not how many frames are kept.
BACKGROUND_EVERY_S = CHUNK_S
BACKGROUND_QUALITY = 80


def next_mark(ts: float, step: float = CHUNK_S) -> float:
    """The next `step` boundary after `ts` on the LOCAL clock."""
    off = time.localtime(ts).tm_gmtoff
    return ts - ((ts + off) % step) + step


def encode_frame(frame, width: int = OUTPUT_WIDTH, quality: int = BACKGROUND_QUALITY) -> Optional[bytes]:
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    h, w = frame.shape[:2]
    if width and w > width:
        frame = cv2.resize(frame, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


def npz_bytes(**arrays) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


#: Why a session stopped. `frozen` keeps its matrix, `closed` does not.
ACTIVE, FROZEN, CLOSED = "active", "frozen", "closed"


@dataclass(frozen=True)
class HeatmapSettings:
    """Render/encode knobs. Not operator settings: the catalog exposes only
    what the supplied implementation exposed (classes, intensity, radius)."""

    snapshot_interval_s: float = SNAPSHOT_INTERVAL_S
    scale: int = SCALE
    blur_px: int = BLUR_PX
    frame_alpha: float = FRAME_ALPHA
    ceiling: float = CEILING
    output_width: int = OUTPUT_WIDTH
    jpeg_quality: int = JPEG_QUALITY


@dataclass
class Session:
    """One accumulation, from the moment heat first landed to the moment it stopped."""

    id: str
    camera: str
    started_at: float
    status: str = ACTIVE
    ended_at: Optional[float] = None
    close_reason: Optional[str] = None
    last_snapshot_at: Optional[float] = None
    accumulator: Optional[HeatAccumulator] = None
    params: dict = field(default_factory=dict)

    def wire(self) -> dict:
        acc = self.accumulator
        return {"session_id": self.id, "camera": self.camera, "started_at": self.started_at,
                "ended_at": self.ended_at, "status": self.status,
                "close_reason": self.close_reason, "last_snapshot_at": self.last_snapshot_at,
                "points": acc.points if acc else 0, "frames": acc.frames if acc else 0,
                "peak": round(acc.peak, 2) if acc else 0.0, "params": dict(self.params)}


@dataclass
class CameraHeat:
    """What the runtime holds for one camera. One per configured camera."""

    camera: str
    #: The operator's switch. Off means: do nothing at all for this camera.
    enabled: bool = True
    session: Optional[Session] = None
    #: True while the schedule window was open on the last frame we saw.
    in_window: bool = False
    #: The newest frame, kept ONLY so a freeze renders the scene as it is at
    #: the moment the operator froze it rather than up to a snapshot old. One
    #: frame per accumulating camera; dropped the moment it stops.
    last_frame: object = None
    #: The storage block being filled: when it started, and when it is due.
    chunk_start: Optional[float] = None
    chunk_due: Optional[float] = None
    last_background_at: Optional[float] = None


class HeatmapRuntime:
    """Every camera's heat sessions. One instance per analytics process."""

    def __init__(self, settings: Optional[HeatmapSettings] = None,
                 publish: Optional[Callable[[str, Session, Snapshot], None]] = None,
                 clock: Callable[[], float] = time.time,
                 chunks: Optional[Callable[[dict, bytes, Optional[bytes]], None]] = None) -> None:
        self.settings = settings or HeatmapSettings()
        self._publish = publish
        self._chunks = chunks
        self.chunks_emitted = 0
        self._clock = clock
        self._cameras: dict[str, CameraHeat] = {}
        self._lock = threading.Lock()
        self.snapshots_rendered = 0
        self.publish_failures = 0

    # ── the control plane: camera-mgmt pushes these ──────────────────────────
    def set_enabled(self, camera: str, enabled: bool, *, mode: str = "resume",
                    reason: Optional[str] = None, now: Optional[float] = None) -> dict:
        """Turn a camera's heatmap on or off. `mode` applies when turning ON:
        "resume" keeps the frozen session, "new" closes it and starts fresh."""
        now = self._clock() if now is None else now
        with self._lock:
            heat = self._cameras.setdefault(camera, CameraHeat(camera))
            was = heat.enabled
            heat.enabled = bool(enabled)
            if was and not enabled:
                self._finalize(heat, FROZEN, reason or "operator", now, keep_matrix=True)
            elif enabled and not was and mode == "new":
                self._discard(heat, now)
            return self._status(heat)

    def configured(self, cameras: Iterable[str]) -> None:
        """The set of cameras with the activity configured; forget the rest."""
        wanted = set(cameras)
        with self._lock:
            for camera in [c for c in self._cameras if c not in wanted]:
                self._flush_chunk(self._cameras.pop(camera), self._clock())

    def forget(self, camera: str) -> None:
        with self._lock:
            heat = self._cameras.pop(camera, None)
            if heat is not None:
                self._flush_chunk(heat, self._clock())

    def reset(self, camera: str) -> None:
        """The stream was interrupted. The heat is still the same place's heat,
        so the session continues; only the frame geometry may change."""

    # ── the data plane: the activity calls this, every frame ─────────────────
    def observe(self, camera: str, ts: float, points, frame, *, width: int, height: int,
                radius_px: float, intensity: float, in_window: bool,
                params: Optional[dict] = None) -> None:
        with self._lock:
            heat = self._cameras.setdefault(camera, CameraHeat(camera))
            if not heat.enabled:
                heat.in_window = in_window
                return                                        # OFF: nothing, no CPU
            if not in_window:
                if heat.in_window:
                    self._finalize(heat, CLOSED, "schedule ended", ts, keep_matrix=False)
                heat.in_window = False
                return
            heat.in_window = True
            heat.last_frame = frame
            session = heat.session
            if session is not None and session.status == FROZEN:
                # Switched back on and resumed: the frozen matrix carries on.
                # A session the operator chose NOT to resume was let go when
                # they chose "new", so a frozen one here always means resume.
                session.status, session.ended_at, session.close_reason = ACTIVE, None, None
            elif session is None or session.status != ACTIVE:
                session = heat.session = Session(id=uuid.uuid4().hex, camera=camera,
                                                 started_at=ts, params=dict(params or {}))
            if heat.chunk_due is not None and ts >= heat.chunk_due:
                self._flush_chunk(heat, heat.chunk_due)        # the block ends on its mark
            if session.accumulator is None or not session.accumulator.matches(
                    width, height, radius_px, intensity):
                self._flush_chunk(heat, ts)                     # never mix two geometries
                session.accumulator = HeatAccumulator(width, height, radius_px=radius_px,
                                                      intensity=intensity,
                                                      scale=self.settings.scale)
                session.params = dict(params or {})
            if heat.chunk_start is None:
                heat.chunk_start, heat.chunk_due = ts, next_mark(ts)
            session.accumulator.observe(points)
            due = (session.last_snapshot_at is None
                   or ts - session.last_snapshot_at >= self.settings.snapshot_interval_s)
            if due:
                # "live": the VMS keeps one per session and replaces it, so a
                # picture a minute never becomes an archive of them.
                self._render(session, frame, ts, kind="live")

    # ── rendering ────────────────────────────────────────────────────────────
    def _render(self, session: Session, frame, ts: float, *, kind: str,
                publish: bool = True) -> Optional[Snapshot]:
        acc = session.accumulator
        if acc is None or frame is None:
            return None
        snap = acc.snapshot(frame, alpha=self.settings.frame_alpha,
                            blur_px=self.settings.blur_px, ceiling=self.settings.ceiling,
                            output_width=self.settings.output_width,
                            quality=self.settings.jpeg_quality, captured_at=ts)
        if snap is None:
            return None
        session.last_snapshot_at = ts
        self.snapshots_rendered += 1
        if publish:
            self._emit(kind, session, snap)
        return snap

    def _emit(self, kind: str, session: Session, snap: Optional[Snapshot]) -> None:
        """Hand one artifact or lifecycle change to the sink. Never raises:
        a heatmap that cannot be stored must not stop the activity worker."""
        if self._publish is None:
            return
        try:
            self._publish(kind, session, snap)
        except Exception as exc:                              # noqa: BLE001
            self.publish_failures += 1
            log.warning("heatmap %s for %s not published: %s", kind, session.camera, exc)

    def _finalize(self, heat: CameraHeat, status: str, reason: str, ts: float,
                  *, keep_matrix: bool) -> None:
        """Stop the running session and make its last picture.

        Rendered from the newest frame, so a freeze shows the scene at the
        moment it froze rather than up to a snapshot interval earlier. If the
        camera has gone quiet there is no frame to use and the session's last
        snapshot stays the picture on screen.
        """
        session = heat.session
        if session is None or session.status != ACTIVE:
            heat.last_frame = None
            return
        self._flush_chunk(heat, ts)
        final = self._render(session, heat.last_frame, ts, kind="final", publish=False)
        session.status, session.ended_at, session.close_reason = status, ts, reason
        self._emit("final", session, final)
        heat.last_frame = None
        if not keep_matrix:
            session.accumulator = None
            heat.session = None

    def _discard(self, heat: CameraHeat, ts: float) -> None:
        """The operator chose a NEW session over resuming the frozen one."""
        session = heat.session
        if session is None:
            return
        session.status, session.close_reason = CLOSED, "new session"
        session.ended_at = session.ended_at or ts
        session.accumulator = None
        heat.session = None
        self._emit("closed", session, None)

    # ── storage blocks ───────────────────────────────────────────────────────
    def _flush_chunk(self, heat: CameraHeat, end: float) -> None:
        """Hand the block being filled to the chunk sink and start none. Caller
        holds the lock. Never raises: a block that cannot be stored must not
        stop the activity worker."""
        start, heat.chunk_start, heat.chunk_due = heat.chunk_start, None, None
        session = heat.session
        acc = session.accumulator if session is not None else None
        if start is None or acc is None:
            return
        delta, points = acc.take_delta()
        if self._chunks is None:
            return
        try:
            background = None
            if (heat.last_background_at is None
                    or end - heat.last_background_at >= BACKGROUND_EVERY_S):
                background = encode_frame(heat.last_frame, self.settings.output_width)
                if background is not None:
                    heat.last_background_at = end
            meta = {"camera": heat.camera, "session_id": session.id,
                    "period_start": start, "period_end": max(end, start),
                    "frame_width": acc.frame_width, "frame_height": acc.frame_height,
                    "scale": acc.scale, "points": points,
                    "peak": round(float(delta.max()), 3) if points else 0.0,
                    "params": dict(session.params or {})}
            self._chunks(meta, npz_bytes(heat=delta), background)
            self.chunks_emitted += 1
        except Exception as exc:                              # noqa: BLE001
            self.publish_failures += 1
            log.warning("heatmap block for %s not published: %s", heat.camera, exc)

    def partial(self, camera: str) -> Optional[bytes]:
        """The block being filled right now, with the newest camera frame, as one
        .npz: `heat` (float32), `background` (JPEG bytes as uint8, may be empty)
        and `meta` (JSON). None when this camera is not accumulating."""
        with self._lock:
            heat = self._cameras.get(camera)
            session = heat.session if heat is not None else None
            acc = session.accumulator if session is not None else None
            if heat is None or acc is None or heat.chunk_start is None:
                return None
            delta, frame = acc.delta.copy(), heat.last_frame
            meta = {"camera": camera, "period_start": heat.chunk_start, "period_end": self._clock(),
                    "frame_width": acc.frame_width, "frame_height": acc.frame_height,
                    "scale": acc.scale, "points": acc.delta_points}
        jpeg = encode_frame(frame, self.settings.output_width) or b""
        return npz_bytes(heat=delta, background=np.frombuffer(jpeg, np.uint8),
                         meta=np.array(json.dumps(meta)))

    # ── what a restart would need, if it is ever asked for ───────────────────
    def state(self, camera: str) -> Optional[np.ndarray]:
        """The live matrix, for a persistence layer that does not exist yet."""
        heat = self._cameras.get(camera)
        session = heat.session if heat else None
        return None if session is None or session.accumulator is None else session.accumulator.matrix

    # ── observability ────────────────────────────────────────────────────────
    def _status(self, heat: CameraHeat) -> dict:
        out = {"camera": heat.camera, "enabled": heat.enabled, "in_window": heat.in_window,
               "session": heat.session.wire() if heat.session else None}
        return out

    def status(self, camera: str) -> Optional[dict]:
        with self._lock:
            heat = self._cameras.get(camera)
            return self._status(heat) if heat else None

    def snapshot(self) -> dict:
        with self._lock:
            return {"cameras": {c: self._status(h) for c, h in self._cameras.items()},
                    "snapshots_rendered": self.snapshots_rendered,
                    "chunks_emitted": self.chunks_emitted, "chunk_seconds": CHUNK_S,
                    "publish_failures": self.publish_failures,
                    "snapshot_interval_s": self.settings.snapshot_interval_s}


#: The process-wide runtime. The engine builds it; the activity finds it here,
#: because an Activity is constructed with (camera, spec) and nothing else.
_RUNTIME: Optional[HeatmapRuntime] = None


def install(runtime: Optional[HeatmapRuntime]) -> Optional[HeatmapRuntime]:
    global _RUNTIME
    _RUNTIME = runtime
    return runtime


def runtime() -> Optional[HeatmapRuntime]:
    return _RUNTIME
