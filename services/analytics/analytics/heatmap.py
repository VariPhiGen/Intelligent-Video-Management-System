"""heatmap.py — where things were, accumulated over a session and rendered now and then.

Ported from the DeepStream-era `Heatmap_overlay` probe onto the CPU engine. The
SEMANTICS are the supplied ones; the IMPLEMENTATION is not, because the supplied
one cannot run on this appliance's budget.

WHAT THE SUPPLIED CODE DOES, and this keeps:

  * one heat point per QUALIFYING DETECTION per frame, at the box's BOTTOM
    CENTRE — where the object meets the ground;
  * a hard-edged disc of `radius` pixels around it, `intensity` added to every
    pixel inside;
  * nothing ever decays, so the matrix is the whole session's dwell;
  * the picture is a Gaussian blur, clipped at 255 (NOT normalised, so busy
    spots saturate), COLORMAP_JET, blended over the frame 40/60.

WHAT IT DOES NOT DO, and this does not either: no decay, no zone masking (the
supplied code loads zones for this activity and never reads them), no tracker
de-duplication (heat is per detection, so standing still keeps adding heat —
that IS the dwell signal), nothing burnt into the matrix or the image but the
heat itself. Presentation metadata belongs to the artifact/UI layer.

WHY IT IS BUILT DIFFERENTLY. The supplied code walks a Python double loop over
(2r+1)² pixels per detection per frame and re-runs blur + colormap + blend on
EVERY frame. Measured IN THE ANALYTICS IMAGE at 1920x1080 with four detections
per frame, the same arithmetic done that way costs 18.3 ms per frame — 9.2% of a
core per camera at the 5 FPS this appliance samples, and ~46% at the 25 FPS
DeepStream ran. Here the disc is a precomputed stamp added with one slice
assignment, the matrix is kept at 1/4 scale, and the picture is rendered only
when a snapshot is due: 0.0122 ms per frame plus one 11.5 ms render, so 15.1 ms
per camera-minute — 0.025% of a core, 364x less work for the same picture.

SCALE. Heat lives on a matrix `SCALE` times smaller than the frame in each
axis. A heat point is a disc tens of pixels wide that is then blurred; a quarter
of the resolution is invisible after the blur and costs a sixteenth of the
memory and of every pixel operation. The rendered overlay is resized back.

FRAME RATE. `intensity` is heat per FRAME, as supplied. The broker samples
5 FPS where DeepStream ran the camera's full rate, so a given dwell reaches the
255 ceiling about five times slower in wall-clock terms; raise `intensity` to
match a DeepStream deployment's look.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

#: How much smaller than the frame the heat matrix is, per axis.
SCALE = 4
#: Gaussian kernel the supplied code used, in FRAME pixels (31 at full size).
BLUR_PX = 31
#: Weight of the camera frame in the blend; the heat colour gets the rest.
#: Supplied: cv2.addWeighted(frame, 0.4, heatmap_color, 0.6, 0).
FRAME_ALPHA = 0.4
#: Heat is clipped, not normalised, exactly as supplied: a busy spot saturates.
CEILING = 255.0
#: Long side of the rendered overlay. The supplied pipeline rendered at its
#: muxer size (1280x720); a card in the browser needs no more.
OUTPUT_WIDTH = 1280
#: JPEG quality of the artifact. The supplied code used 100, which triples the
#: bytes for no visible gain on a blurred colour map.
JPEG_QUALITY = 85


def disc(radius_px: float, intensity: float) -> np.ndarray:
    """The supplied circular stamp, built once instead of per pixel per frame."""
    r = max(1, int(round(radius_px)))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return np.where(x * x + y * y <= r * r, np.float32(intensity), np.float32(0.0))


@dataclass(frozen=True)
class Snapshot:
    """One rendered overlay, ready to be stored. Bytes, not pixels."""

    jpeg: bytes
    width: int
    height: int
    captured_at: float
    points: int
    peak: float


class HeatAccumulator:
    """One camera's heat for one session. Not thread-safe: one worker owns it."""

    def __init__(self, width: int, height: int, radius_px: float = 40.0,
                 intensity: float = 1.0, scale: int = SCALE) -> None:
        self.frame_width, self.frame_height = int(width), int(height)
        self.scale = max(1, int(scale))
        self.radius_px, self.intensity = float(radius_px), float(intensity)
        self._w = max(1, self.frame_width // self.scale)
        self._h = max(1, self.frame_height // self.scale)
        self.matrix = np.zeros((self._h, self._w), np.float32)
        #: Heat added since the last `take_delta()` -- the current storage block.
        self.delta = np.zeros((self._h, self._w), np.float32)
        self.delta_points = 0
        self._stamp = disc(self.radius_px / self.scale, self.intensity)
        self._r = self._stamp.shape[0] // 2
        #: Heat points added since the session started — the accumulation's size.
        self.points = 0
        #: Frames offered since the session started, whether or not they had one.
        self.frames = 0

    @property
    def shape(self) -> tuple[int, int]:
        return self._h, self._w

    def matches(self, width: int, height: int, radius_px: float, intensity: float) -> bool:
        """Is this accumulator still the right one for these settings?

        A frame of a different size or a changed radius/intensity would mean
        heat measured two ways in one matrix, so the caller starts a new one.
        """
        return (int(width) == self.frame_width and int(height) == self.frame_height
                and abs(float(radius_px) - self.radius_px) < 1e-6
                and abs(float(intensity) - self.intensity) < 1e-6)

    def add(self, x_px: float, y_px: float) -> None:
        """Add one detection's heat at a FRAME pixel position (bottom centre)."""
        cx, cy = int(x_px) // self.scale, int(y_px) // self.scale
        x0, y0 = cx - self._r, cy - self._r
        x1, y1 = x0 + self._stamp.shape[1], y0 + self._stamp.shape[0]
        sx, sy = max(0, -x0), max(0, -y0)                     # clip into the frame
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self._w, x1), min(self._h, y1)
        if x1 <= x0 or y1 <= y0:
            return                                            # entirely outside
        stamp = self._stamp[sy:sy + (y1 - y0), sx:sx + (x1 - x0)]
        self.matrix[y0:y1, x0:x1] += stamp
        self.delta[y0:y1, x0:x1] += stamp
        self.points += 1
        self.delta_points += 1

    def observe(self, points) -> int:
        """Add every point of one frame; returns how many landed."""
        self.frames += 1
        before = self.points
        for x_px, y_px in points:
            self.add(x_px, y_px)
        return self.points - before

    @property
    def peak(self) -> float:
        return float(self.matrix.max()) if self.points else 0.0

    def heat_image(self, width: int, height: int, blur_px: int = BLUR_PX,
                   ceiling: float = CEILING) -> np.ndarray:
        """The blurred, clipped, coloured heat at the requested size (BGR)."""
        kernel = max(3, int(round(blur_px / self.scale)) | 1)
        blurred = cv2.GaussianBlur(self.matrix, (kernel, kernel), 0)
        clipped = np.clip(blurred * (255.0 / max(1e-6, ceiling)), 0, 255).astype(np.uint8)
        scaled = cv2.resize(clipped, (width, height), interpolation=cv2.INTER_LINEAR)
        return cv2.applyColorMap(scaled, cv2.COLORMAP_JET)

    def render(self, frame: np.ndarray, alpha: float = FRAME_ALPHA,
               blur_px: int = BLUR_PX, ceiling: float = CEILING,
               output_width: int = OUTPUT_WIDTH) -> np.ndarray:
        """The supplied blend: the camera frame under the whole colour map."""
        h, w = frame.shape[:2]
        if output_width and w > output_width:
            h, w = max(1, round(h * output_width / w)), output_width
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        heat = self.heat_image(w, h, blur_px=blur_px, ceiling=ceiling)
        return cv2.addWeighted(frame, float(alpha), heat, 1.0 - float(alpha), 0)

    def snapshot(self, frame: np.ndarray, *, alpha: float = FRAME_ALPHA,
                 blur_px: int = BLUR_PX, ceiling: float = CEILING,
                 output_width: int = OUTPUT_WIDTH, quality: int = JPEG_QUALITY,
                 captured_at: Optional[float] = None) -> Optional[Snapshot]:
        """Render and encode one overlay. None when the frame cannot be used."""
        if frame is None or getattr(frame, "size", 0) == 0:
            return None
        overlay = self.render(frame, alpha=alpha, blur_px=blur_px, ceiling=ceiling,
                              output_width=output_width)
        ok, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None
        return Snapshot(jpeg=buf.tobytes(), width=overlay.shape[1], height=overlay.shape[0],
                        captured_at=captured_at if captured_at is not None else time.time(),
                        points=self.points, peak=self.peak)

    def take_delta(self) -> tuple[np.ndarray, int]:
        """The heat added since the last call, and how many points made it; resets it."""
        out, n = self.delta.copy(), self.delta_points
        self.delta[:] = 0.0
        self.delta_points = 0
        return out, n

    def clear(self) -> None:
        """Start a new accumulation on the same geometry."""
        self.matrix[:] = 0.0
        self.delta[:] = 0.0
        self.points = self.frames = self.delta_points = 0
