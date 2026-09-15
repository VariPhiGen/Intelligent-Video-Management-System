# ===========================================================================
# GENERATED COPY - DO NOT EDIT.
#
# Source: services/motion/detector/motion_core.py
# Sync:   python3 scripts/sync_shared.py   (--check verifies, and CI should)
#
# Edits here are silently destroyed on the next sync. More to the point, they
# would recreate the exact duplication this replaced: two motion
# implementations that agreed until one of them was tuned. If this file needs
# to change, change the source and re-run the sync.
# ===========================================================================
"""motion_core.py — THE motion implementation for the whole VMS.

THE ONLY HAND-WRITTEN COPY. It lives in the motion service because that is
where someone looks for motion detection, and because this service always
ships: the frame broker is profile-gated and off by default, so an algorithm
that lived there would sit in a container most deployments never start.

Two other services need it and neither can import it — each builds from its
own directory as its Docker context, and a consumer must not depend on this
service at runtime. So scripts/sync_motion.py copies this file to:

    services/frames/broker/motion.py             (the broker)
    services/smartsearch/index/motion.py         (the gate)

Both copies carry a DO-NOT-EDIT banner and each service's own suite replays a
golden fixture, so a copy that drifts fails in its own container.

There used to be two implementations: this file's predecessor produced a scalar
for operator alerting, and the SmartSearch gate produced spatial regions for
gating YOLO. They shared four OpenCV calls and diverged after that, which meant
a tuning change had to be made twice and could drift.

This module replaces both. ONE differencing pass produces BOTH outputs:

    resize -> grayscale -> blur -> absdiff
         |
         +-- THRESH_TOZERO(noise_floor) -> nonzero/size  ->  scalar fraction
         |                                                   (Motion service:
         |                                                    window, state
         |                                                    machine, events)
         |
         +-- THRESH_BINARY(threshold) -> open -> dilate
                 -> contours -> merge -> pad -> full-frame boxes
                                                          ->  regions
                                                              (SmartSearch:
                                                               YOLO gating)

WHY BOTH OUTPUTS AND NOT ONE. They are not interchangeable and the difference
is measured, not stylistic. The scalar is a whole-frame changed-pixel fraction:
a person at the end of a corridor moves a handful of pixels and never crosses
it. Per-contour area does see them — services/smartsearch/config.yaml records
the test, where a whole-frame-style threshold lost 1 of 8 people and the
contour threshold found all 8. So the scalar cannot gate YOLO, and contours are
pointlessly expensive for a yes/no alert. Both are cheap once the difference
is computed, which is the entire argument for computing it once.

NOTHING HERE DECIDES ANYTHING. No confirmation window, no stickiness, no alert
state, no skip verdict acted upon. This module measures; the Motion service
decides whether a measurement is an alert, and SmartSearch decides whether it
is worth running a detector. Keeping that boundary is what let the two
consumers stay independent of each other.

EACH CONSUMER'S VOCABULARY LIVES AT THE BOTTOM, not in a separate adapter file
in each service. `preprocess_frame` / `compute_pixel_feature` are the two
stateless helpers this service's worker calls; `MotionGate` is the per-camera
object SmartSearch's pipeline constructs, with the /health key names its API
already reports. Folding them in is what keeps the count at one file per
service instead of a copy plus a shim.

PARITY WAS THE CONTRACT, and it is now a fixture. The region half reproduced
the SmartSearch gate exactly over 430 live and synthetic frames before that
gate was replaced; what it produced on 220 of them is frozen in
tests/golden_motion.json, which each service replays. A change here that would
alter an operator's events or the crops sent to the detector fails there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

Box = tuple[int, int, int, int]          # x1, y1, x2, y2 in FULL-frame coords

#: Built once. This runs on every sampled frame of every camera, and cv2
#: rebuilds the structuring element on each call otherwise.
_SPECKLE_KERNEL = np.ones((3, 3), np.uint8)

#: Verdicts. Strings rather than an enum because they cross a process boundary
#: as JSON and land in /health, where a name is worth more than an int.
NONE = "none"                # nothing moved; a consumer may skip work entirely
FULL_FRAME = "full_frame"    # scene change, or regions not worth cropping
REGIONS = "regions"          # crop these


@dataclass
class MotionResult:
    """One frame's motion, for every consumer at once."""
    #: Whole-frame changed-pixel fraction after the noise floor. The Motion
    #: service's input; comparable to its sensitivity presets.
    fraction: float
    #: None -> nothing moved. [] -> use the whole frame. [...] -> crop these.
    #: Full-frame pixel coordinates, already padded.
    regions: Optional[list[Box]]
    verdict: str
    reason: str
    #: False on the first frame after a connect or reconnect, when there is no
    #: previous frame to difference against. Consumers must not trust
    #: `regions` on such a frame — see the note in analyse().
    baseline_valid: bool = True

    def to_dict(self) -> dict:
        return {"fraction": round(self.fraction, 6),
                "regions": [list(r) for r in self.regions] if self.regions is not None else None,
                "verdict": self.verdict, "reason": self.reason,
                "baseline_valid": self.baseline_valid}


@dataclass
class MotionParams:
    """Both consumers' tuning, in one place — which is the point of the module.

    The region values are the SmartSearch gate's measured operating point and
    must not drift from it without re-running that measurement. The scalar
    values are the Motion service's.
    """
    scale_width: int = 320
    blur_kernel: int = 21
    #: The Motion service resizes to a FIXED 320x180; the region path resizes
    #: to 320 wide with the aspect PRESERVED. For 16:9 cameras those are the
    #: same operation and one pass serves both. For anything else they are not
    #: — measured on a 704x576 camera the fractions differ by up to 0.0034 —
    #: so the scalar gets its own fixed-size pass and operator events stay
    #: bit-for-bit what they were.
    #:
    #: FREE ON 16:9 CAMERAS: the second pass is skipped when the two
    #: geometries coincide, which is the common case. None = always share the
    #: pass and accept the small divergence.
    scalar_size: Optional[tuple[int, int]] = (320, 180)
    # ── region half (was smartsearch/index/motion.py) ────────────────────────
    threshold: int = 18
    min_area_fraction: float = 0.0002
    despeckle_iterations: int = 1
    dilate_iterations: int = 2
    max_regions: int = 3
    region_padding: float = 0.15
    full_frame_fraction: float = 0.35
    scene_change_fraction: float = 0.55
    # ── scalar half (was motion/detector/algorithm.py) ───────────────────────
    #: Sub-threshold differences are zeroed before counting, so sensor noise
    #: does not inflate the fraction. This is the Motion service's floor and it
    #: is NOT redundant here the way it would be against a binary threshold.
    noise_floor: int = 10


def preprocess(frame: np.ndarray, width: int, height: int,
               blur_kernel: int = 21) -> np.ndarray:
    """Resize -> greyscale -> blur. The analysis frame both halves difference.

    EXPORTED because the Motion service's stateless helpers are this, and were
    a second copy of it until they were replaced by a call. The 21x21 kernel is
    large enough to suppress CMOS sensor noise at 320x180 without blurring away
    real motion; it is a tuned value, not an arbitrary one.
    """
    return cv2.GaussianBlur(
        cv2.cvtColor(cv2.resize(frame, (width, height)), cv2.COLOR_BGR2GRAY),
        (blur_kernel, blur_kernel), 0)


def changed_fraction(prev: np.ndarray, curr: np.ndarray,
                     noise_floor: int) -> float:
    """Fraction of pixels that changed by more than the noise floor, 0..1.

    THE NUMBER THE MOTION SERVICE DECIDES ON. Its sensitivity presets were
    fitted against exactly this, so the operations and their order are load
    bearing: absolute difference, zero anything at or below the floor, count
    what is left. THRESH_TOZERO rather than THRESH_BINARY because the floor
    removes noise without also discarding how much a surviving pixel changed.
    """
    return _fraction_from_diff(cv2.absdiff(prev, curr), noise_floor)


def _fraction_from_diff(diff: np.ndarray, noise_floor: int) -> float:
    """The half of changed_fraction that works on an ALREADY computed
    difference. analyse() reuses one absdiff for both halves whenever the two
    geometries coincide, and must not reach that number by a second route."""
    _, floored = cv2.threshold(diff, noise_floor, 255, cv2.THRESH_TOZERO)
    return float(np.count_nonzero(floored)) / float(floored.size)


class CanonicalMotion:
    """One per camera. Holds the previous analysis frame and nothing else.

    Not thread-safe; each camera worker owns its own instance, the same
    arrangement both predecessors used.
    """

    def __init__(self, params: Optional[MotionParams] = None) -> None:
        self.p = params or MotionParams()
        self._prev: Optional[np.ndarray] = None
        #: Separate baseline, used only when the scalar runs on its own
        #: geometry. Stays None whenever the two passes coincide.
        self._prev_scalar: Optional[np.ndarray] = None
        self.frames_seen = 0
        self.frames_none = 0
        self.frames_full = 0
        self.frames_regions = 0
        self.frames_scene_change = 0
        self.regions_emitted = 0

    def reset(self) -> None:
        """Drop the baseline. Call on reconnect.

        The first frame of a new connection differs from the last frame of the
        old one by however long the outage lasted, which is not motion.
        """
        self._prev = None
        self._prev_scalar = None

    # ── the single pass ──────────────────────────────────────────────────────
    def analyse(self, frame: np.ndarray) -> MotionResult:
        p = self.p
        self.frames_seen += 1
        h, w = frame.shape[:2]
        scale = p.scale_width / float(w)
        small = preprocess(frame, p.scale_width, max(1, int(h * scale)),
                           p.blur_kernel)

        if self._prev is None:
            # No baseline. Report the whole frame rather than "nothing moved":
            # a camera that comes up with someone already in shot must not stay
            # invisible until they happen to move. baseline_valid says why, so
            # a consumer can treat it as "unknown" rather than as evidence.
            self._prev = small
            self._prev_scalar = self._scalar_frame(frame, small)
            self.frames_full += 1
            return MotionResult(0.0, [], FULL_FRAME,
                                "first frame after connect", baseline_valid=False)

        diff = cv2.absdiff(self._prev, small)

        # ── scalar half ──────────────────────────────────────────────────────
        # On its own geometry when that differs from the region path's, so the
        # number handed to the Motion service is the one its sensitivity
        # presets were fitted against. Same pass whenever they coincide.
        scalar_now = self._scalar_frame(frame, small)
        if scalar_now is None:
            source_diff = diff
        elif (self._prev_scalar is not None
                and self._prev_scalar.shape == scalar_now.shape):
            source_diff = cv2.absdiff(self._prev_scalar, scalar_now)
        else:
            source_diff = np.zeros_like(scalar_now)
        self._prev_scalar = scalar_now
        fraction = _fraction_from_diff(source_diff, p.noise_floor)

        self._prev = small

        # ── region half ──────────────────────────────────────────────────────
        _, mask = cv2.threshold(diff, p.threshold, 255, cv2.THRESH_BINARY)
        # Despeckle BEFORE dilating. Isolated sensor pixels that clear the
        # threshold become real regions once dilated, and a frame full of them
        # trips max_regions into a full-frame pass — so speckle costs the exact
        # inference the region path exists to avoid. Measured on a synthetic
        # mask of 400 speckle pixels plus one 4x9 distant figure: 252 regions
        # passing min_area before, 1 after, and the figure survived both.
        if p.despeckle_iterations > 0:
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _SPECKLE_KERNEL,
                                    iterations=p.despeckle_iterations)
        mask = cv2.dilate(mask, None, iterations=p.dilate_iterations)

        changed = float(np.count_nonzero(mask)) / mask.size
        if changed >= p.scene_change_fraction:
            # Lights, IR cut, auto-exposure, camera moved. Everything "moved",
            # so regions are meaningless — and the baseline above is already
            # the new scene.
            self.frames_full += 1
            self.frames_scene_change += 1
            return MotionResult(fraction, [], FULL_FRAME, "scene change")

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        min_area = p.min_area_fraction * mask.size
        boxes: list[Box] = []
        for c in contours:
            if cv2.contourArea(c) < min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            boxes.append((x, y, x + bw, y + bh))

        if not boxes:
            self.frames_none += 1
            return MotionResult(fraction, None, NONE, "no motion")

        boxes = merge_boxes(boxes)
        inv = 1.0 / scale
        full: list[Box] = []
        for (x1, y1, x2, y2) in boxes:
            bx1, by1 = int(x1 * inv), int(y1 * inv)
            bx2, by2 = int(x2 * inv), int(y2 * inv)
            pad_x = int((bx2 - bx1) * p.region_padding)
            pad_y = int((by2 - by1) * p.region_padding)
            full.append((max(0, bx1 - pad_x), max(0, by1 - pad_y),
                         min(w, bx2 + pad_x), min(h, by2 + pad_y)))

        covered = sum((b[2] - b[0]) * (b[3] - b[1]) for b in full) / float(w * h)
        if len(full) > p.max_regions or covered >= p.full_frame_fraction:
            # More crops than the frame is worth, or they nearly cover it. One
            # inference beats N — the detector resizes any input to its own
            # resolution, so three crops cost three inferences.
            self.frames_full += 1
            return MotionResult(fraction, [], FULL_FRAME,
                                f"{len(full)} regions covering {covered:.0%}")

        self.frames_regions += 1
        self.regions_emitted += len(full)
        return MotionResult(fraction, full, REGIONS, "motion")

    def _scalar_frame(self, frame: np.ndarray,
                      shared: np.ndarray) -> Optional[np.ndarray]:
        """Preprocessed frame for the SCALAR, or None to reuse the shared one.

        None is the common case and the fast path: at 16:9 the region pass has
        already produced exactly the geometry the Motion service expects.
        """
        p = self.p
        if p.scalar_size is None:
            return None
        sw, sh = p.scalar_size
        if (p.scale_width, shared.shape[0]) == (sw, sh):
            return None
        return preprocess(frame, sw, sh, p.blur_kernel)

    def snapshot(self) -> dict:
        seen = self.frames_seen or 1
        return {
            "frames_seen": self.frames_seen,
            "frames_none": self.frames_none,
            "skip_rate": round(self.frames_none / seen, 4),
            "frames_full_frame": self.frames_full,
            "frames_regions": self.frames_regions,
            "frames_scene_change": self.frames_scene_change,
            "regions_emitted": self.regions_emitted,
        }


def merge_boxes(boxes: list[Box], passes: int = 3) -> list[Box]:
    """Union overlapping boxes so one object is not cropped twice."""
    for _ in range(passes):
        merged: list[Box] = []
        for b in boxes:
            for i, m in enumerate(merged):
                if b[0] <= m[2] and m[0] <= b[2] and b[1] <= m[3] and m[1] <= b[3]:
                    merged[i] = (min(m[0], b[0]), min(m[1], b[1]),
                                 max(m[2], b[2]), max(m[3], b[3]))
                    break
            else:
                merged.append(b)
        if len(merged) == len(boxes):
            return merged
        boxes = merged
    return boxes


# ═══════════════════════════════════════════════════════════════════════════
# CONSUMER ENTRY POINTS
#
# Each service calls the same measurement above through the names its own code
# already uses. These are NOT second implementations and must never become
# ones: every line below forwards. They live here rather than in a per-service
# adapter file so each service carries exactly one motion file.
# ═══════════════════════════════════════════════════════════════════════════

# ── the Motion service ─────────────────────────────────────────────────────
# Stateless, because all per-camera memory (previous frame, rolling window)
# lives in CameraState and every decision lives in worker.py. Pipeline per
# sample, unchanged from the implementation these replaced:
#
#   resize -> greyscale -> 21x21 blur -> absdiff vs previous sample
#   -> zero diffs at or below the noise floor -> fraction of what is left

def preprocess_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resize -> greyscale -> blur. Returns a single-channel analysis frame."""
    return preprocess(frame, width, height)


def compute_pixel_feature(
    prev: np.ndarray, curr: np.ndarray, noise_floor: int
) -> float:
    """Fraction of pixels that changed above the noise floor (0.0-1.0)."""
    return changed_fraction(prev, curr, noise_floor)


# ── SmartSearch ────────────────────────────────────────────────────────────

class MotionGate:
    """One per camera. Not thread-safe; each sampler thread owns its own.

    THE FALLBACK PATH. In broker mode the analysis arrives with the frame and
    this is never constructed; with SEARCH_FRAME_SOURCE=sampler the service
    decodes its own frames and gates them itself, so indexing survives the
    broker being stopped. Same measurement either way — that is the point —
    with no runtime dependency in either direction.

    WHAT THE GATE SAVES, AND WHAT IT DOES NOT. The saving is the GATE: no
    motion, no inference at all. Cropping to regions is NOT a second saving —
    the detector resizes any input to its own resolution, so three crops cost
    three inferences where one frame costs one. Cropping buys ACCURACY: a
    distant figure survives a crop and is lost in a full-frame downscale.
    """

    def __init__(self, *, scale_width: int = 320, threshold: int = 25,
                 min_area_fraction: float = 0.0008, dilate_iterations: int = 2,
                 max_regions: int = 3, region_padding: float = 0.15,
                 full_frame_fraction: float = 0.35,
                 scene_change_fraction: float = 0.55,
                 despeckle_iterations: int = 1) -> None:
        self._canon = CanonicalMotion(MotionParams(
            scale_width=scale_width, threshold=threshold,
            min_area_fraction=min_area_fraction,
            despeckle_iterations=despeckle_iterations,
            dilate_iterations=dilate_iterations, max_regions=max_regions,
            region_padding=region_padding,
            full_frame_fraction=full_frame_fraction,
            scene_change_fraction=scene_change_fraction,
            # THE SCALAR HALF SHARES THIS PATH'S SINGLE PASS. analyse() also
            # produces the whole-frame fraction the Motion service decides on,
            # and would compute it on that service's fixed 320x180 geometry
            # when that differs from the region path's aspect-preserving one —
            # a second resize and blur per frame, for a number nothing in
            # SmartSearch reads. None means share.
            #
            # Measured against the implementation this replaced, on the golden
            # frames: 0.926 -> 0.835 ms/frame, about 10% FASTER, with zero
            # region mismatches running the two side by side.
            scalar_size=None,
        ))

    def reset(self) -> None:
        """Drop the baseline — call after a reconnect. The first frame of a new
        connection differs from the last frame of the old one by however long
        the gap was, which is not motion."""
        self._canon.reset()

    def evaluate(self, frame: np.ndarray) -> MotionResult:
        return self._canon.analyse(frame)

    # ── counters ───────────────────────────────────────────────────────────
    # Read off the measurement under SMARTSEARCH's names. Keeping the names is
    # not cosmetic: they are /health keys an operator and our own dashboards
    # already read.
    @property
    def frames_seen(self) -> int:
        return self._canon.frames_seen

    @property
    def frames_skipped(self) -> int:
        return self._canon.frames_none

    @property
    def frames_full(self) -> int:
        return self._canon.frames_full

    @property
    def frames_regional(self) -> int:
        return self._canon.frames_regions

    @property
    def regions_emitted(self) -> int:
        return self._canon.regions_emitted

    @property
    def frames_scene_change(self) -> int:
        return self._canon.frames_scene_change

    def snapshot(self) -> dict:
        seen = self.frames_seen or 1
        return {
            "frames_seen": self.frames_seen,
            "frames_skipped": self.frames_skipped,
            "skip_rate": round(self.frames_skipped / seen, 4),
            "frames_full_frame": self.frames_full,
            "frames_regional": self.frames_regional,
            "regions_emitted": self.regions_emitted,
            "frames_scene_change": self.frames_scene_change,
        }
