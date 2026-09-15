"""frames.py — the images calibration measures and validates against.

ASPECT RATIO IS NOT A DETAIL HERE, IT IS THE MEASUREMENT. Measured 2026-09-04
on the same weights and the same backend pair: OpenVINO beat torch by 1.5x on a
1080x810 portrait frame and by 2.9x on a 405x1080 wide one. Benchmarking on a
square synthetic image would have reported the smaller number and understated
the winner on exactly the shape real cameras produce, which is 16:9.

So the source hierarchy is ordered by how much of production it reproduces, and
every result records WHICH source it used — a figure from synthetic input is
still a figure, but it is not the same claim as one from a live camera.

    live       a frame pulled from a registered camera through the relay.
               Real content, real resolution, real aspect, real compression.
    stored     a crop from this deployment's own crop directory. Real content,
               but a person- or vehicle-shaped crop, NOT a camera frame — so it
               is right for the encoder and the recogniser and WRONG for the
               detector, whose cost depends on full-frame geometry.
    synthetic  structured noise at production aspect ratios. Proves plumbing.
               It cannot prove accuracy, because agreement between two backends
               that both detect nothing is not agreement about anything.

THE SERVICE ALREADY HOLDS THE BEST SOURCE. Cameras arrive with relay URLs and
`index/sampler.py` opens them; calibration borrows the same mechanism rather
than inventing a second way to read a stream. It samples a handful of frames
and closes the capture — this is not a second consumer of the stream, it is a
brief one.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

log = logging.getLogger("analytics.frames")

LIVE = "live"
STORED = "stored"
SYNTHETIC = "synthetic"
ASSET = "asset"

#: Shapes real cameras deliver, as (width, height). 16:9 first because it is
#: what almost every IP camera produces; 4:3 for older and some fisheye models;
#: the portrait entry for corridor-mode installs, which are common in the
#: narrow spaces this product is often deployed in.
PRODUCTION_SHAPES: tuple[tuple[int, int], ...] = (
    (1920, 1080),      # 16:9 — the default for essentially every modern camera
    (1280, 720),       # 16:9 at a lower sub-stream resolution
    (1280, 960),       # 4:3
    (1080, 1920),      # 9:16, corridor mode
)


@dataclass
class FrameSet:
    """Images to measure with, and an honest account of where they came from."""
    frames: list[np.ndarray]
    source: str
    #: Human-readable provenance, carried into the profile.
    detail: str
    #: False for synthetic input. An accuracy verdict reached on synthetic
    #: frames is reported as insufficient evidence rather than as a pass.
    is_real: bool

    @property
    def shapes(self) -> list[tuple[int, int]]:
        return [(f.shape[1], f.shape[0]) for f in self.frames]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "detail": self.detail,
            "is_real": self.is_real,
            "count": len(self.frames),
            "shapes": [f"{w}x{h}" for w, h in self.shapes],
        }


def from_camera(rtsp_url: str, count: int = 8, timeout_ms: int = 5000,
                settle_frames: int = 5) -> Optional[FrameSet]:
    """Pull `count` frames from a relay stream, or None if it cannot be read.

    Uses the same TCP transport and timeouts as index/sampler.py. The first few
    frames are discarded: a freshly opened stream commonly delivers a keyframe
    plus partial data, and a torn frame measured as if it were a scene is a
    wasted measurement.
    """
    import cv2

    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    try:
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
        except Exception:                                          # noqa: BLE001
            pass
        if not cap.isOpened():
            return None

        for _ in range(settle_frames):
            cap.grab()

        frames: list[np.ndarray] = []
        deadline = time.monotonic() + (timeout_ms / 1000.0) * 3
        while len(frames) < count and time.monotonic() < deadline:
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                frames.append(frame)
        if not frames:
            return None
        return FrameSet(frames, LIVE, f"{len(frames)} frames from a live camera",
                        is_real=True)
    finally:
        cap.release()


def from_crop_dir(crop_dir: str, count: int = 200,
                  min_side: int = 24) -> Optional[FrameSet]:
    """Real crops this deployment already stored.

    RIGHT FOR SOME COMPONENTS AND WRONG FOR ONE. These are person and vehicle
    crops, so they are exactly what the encoder embeds and roughly what the
    recogniser sees — but they are not camera frames, and using them to measure
    the DETECTOR would benchmark it on geometry it never encounters.
    """
    import cv2

    if not os.path.isdir(crop_dir):
        return None
    picked: list[np.ndarray] = []
    for root, _dirs, files in os.walk(crop_dir):
        for name in files:
            if not name.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            img = cv2.imread(os.path.join(root, name))
            if img is None or min(img.shape[:2]) < min_side:
                continue
            picked.append(img)
            if len(picked) >= count:
                break
        if len(picked) >= count:
            break
    if not picked:
        return None
    return FrameSet(picked, STORED, f"{len(picked)} stored crops from {crop_dir}",
                    is_real=True)


def from_assets(count: int = 2) -> Optional[FrameSet]:
    """Ultralytics' bundled photographs.

    Real photographs containing real people and vehicles, which makes them far
    better than noise for proving that two backends agree — but they are not
    this site's cameras, so a result from them is evidence about the EXPORT,
    not about the deployment.
    """
    try:
        import cv2
        from ultralytics.utils import ASSETS
    except Exception:                                              # noqa: BLE001
        return None
    out: list[np.ndarray] = []
    for name in ("bus.jpg", "zidane.jpg"):
        path = os.path.join(str(ASSETS), name)
        if os.path.exists(path):
            img = cv2.imread(path)
            if img is not None:
                out.append(img)
        if len(out) >= count:
            break
    if not out:
        return None
    return FrameSet(out, ASSET, "ultralytics bundled photographs", is_real=True)


def synthetic(shapes: Sequence[tuple[int, int]] = PRODUCTION_SHAPES,
              seed: int = 0) -> FrameSet:
    """Structured noise at production aspect ratios. The last resort.

    Structured rather than uniform: uniform noise compresses and convolves
    unlike any real scene, and a detector handed it does so little work that
    the timing flatters every backend equally. Blocks with gradients at least
    exercise the same code paths at a comparable cost.

    It still cannot support an accuracy verdict. Two backends agreeing that
    there is nothing here have agreed about nothing.
    """
    rng = np.random.default_rng(seed)
    frames: list[np.ndarray] = []
    for w, h in shapes:
        base = rng.integers(40, 90, (h // 16 + 1, w // 16 + 1, 3), dtype=np.uint8)
        try:
            import cv2
            img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
        except Exception:                                          # noqa: BLE001
            img = np.repeat(np.repeat(base, 16, axis=0), 16, axis=1)[:h, :w]
        noise = rng.integers(0, 40, img.shape, dtype=np.uint8)
        frames.append(np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8))
    return FrameSet(frames, SYNTHETIC,
                    "structured noise at production aspect ratios", is_real=False)


def for_detector(camera_urls: Sequence[str] = (), count: int = 8) -> FrameSet:
    """Best available full-frame input, in order of how real it is.

    Never falls back to stored crops: a crop is not a camera frame, and the
    detector's cost is a function of full-frame geometry. Better an honest
    synthetic frame at 1920x1080 than a real crop at the wrong shape.
    """
    for url in camera_urls:
        try:
            got = from_camera(url, count=count)
        except Exception as exc:                                   # noqa: BLE001
            log.warning("could not sample %s for calibration: %s", url, exc)
            continue
        if got is not None:
            return got
    asset = from_assets()
    if asset is not None:
        return asset
    return synthetic()


def for_crops(crop_dir: str, count: int = 200) -> FrameSet:
    """Best available crop-shaped input, for the encoder and the recogniser."""
    stored = from_crop_dir(crop_dir, count=count)
    if stored is not None:
        return stored
    asset = from_assets()
    if asset is not None:
        return asset
    return synthetic(shapes=((128, 256), (192, 192)))
