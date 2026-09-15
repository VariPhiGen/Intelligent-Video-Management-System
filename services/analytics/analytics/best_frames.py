"""best_frames.py — keep the best few looks at a tracked object, for ANPR.

WHY THIS EXISTS. Plate reading currently runs on whichever frame happened to
survive the indexing gate, which is arbitrary: the surviving frame is not
necessarily the one where the plate is legible. A vehicle approaching a camera
produces a sequence of steadily better looks, and the one worth reading is
usually not the first. With a tracker we can hold the best few and read those.

RESOLUTION IS THE WHOLE POINT, so crops are stored at full source resolution.
A plate is small; downscaling to save memory would defeat the feature. The
budget is enforced by holding FEWER crops, never smaller ones — and there is a
hard byte ceiling, because a busy forecourt would otherwise grow this without
limit.

WHAT THIS DOES NOT DO. It does not defer the row. Plate results are still read
eagerly and written with the observation that triggered them; the only change
is WHICH pixels get read. Deferring to track end would need either a nullable
follow-up UPDATE or a delayed insert, and both change the store contract — see
the note in pipeline.py. Choosing the best-so-far frame is the part that is
free of schema consequences, so it is the part implemented here.

FUTURE FACE WORK fits the same shape: a person track's best crops by size and
sharpness are exactly what a face detector would want, and nothing here is
vehicle-specific except where the pipeline chooses to call it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

#: Sharpness is measured on a downscale, not the full crop. Laplacian variance
#: is O(pixels) and this runs per detection per frame; at this width the
#: ranking is unchanged and the cost is negligible.
_SHARPNESS_WIDTH = 128


@dataclass
class Candidate:
    """One stored look at an object, at source resolution."""
    crop: np.ndarray          # BGR, exactly as decoded
    ts: float
    score: float
    confidence: float
    area: float
    sharpness: float

    @property
    def nbytes(self) -> int:
        return int(self.crop.nbytes)


def sharpness(crop: np.ndarray) -> float:
    """Laplacian variance on a fixed-width downscale. Higher is sharper.

    A motion-blurred plate and a sharp one can have identical size and
    detector confidence, so without this the ranking would happily pick the
    blurred one.
    """
    if crop.size == 0:
        return 0.0
    h, w = crop.shape[:2]
    if w > _SHARPNESS_WIDTH:
        scale = _SHARPNESS_WIDTH / float(w)
        crop = cv2.resize(crop, (_SHARPNESS_WIDTH, max(1, int(h * scale))),
                          interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def score_crop(crop: np.ndarray, confidence: float) -> tuple[float, float, float]:
    """(score, area, sharpness) for one candidate.

    Area enters as a square root: plate legibility scales with linear pixel
    count across the plate, not with area, so a 4x larger box is worth about 2x
    not 4x. Sharpness is compressed the same way for the same reason — it is a
    tie-breaker between comparable looks, not the dominant term.
    """
    if crop.size == 0:
        return (-1.0, 0.0, 0.0)
    h, w = crop.shape[:2]
    area = float(w * h)
    sharp = sharpness(crop)
    return (float(confidence) * (area ** 0.5) * (1.0 + sharp) ** 0.25, area, sharp)


class BestFrameBuffer:
    """Bounded best-N candidates per track, with a hard byte ceiling.

    Two limits on purpose. `per_track` keeps a single long-lived vehicle from
    hoarding, and `max_bytes` keeps a forecourt full of them from growing the
    process — the second is the one that matters, because the first scales with
    traffic.
    """

    def __init__(self, *, per_track: int = 3, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.per_track = max(1, per_track)
        self.max_bytes = max_bytes
        self._by_track: dict[str, list[Candidate]] = {}
        self._bytes = 0
        self.offered = 0
        self.accepted = 0
        self.evicted_for_budget = 0

    def offer(self, track_id: str, crop: np.ndarray, confidence: float,
              ts: float) -> None:
        """Consider a crop for this track's shortlist. Copies only if kept."""
        self.offered += 1
        score, area, sharp = score_crop(crop, confidence)
        if score < 0:
            return
        held = self._by_track.setdefault(track_id, [])
        if len(held) >= self.per_track and score <= held[-1].score:
            return                                   # not better than the worst
        cand = Candidate(crop=crop.copy(), ts=ts, score=score,
                         confidence=float(confidence), area=area, sharpness=sharp)
        held.append(cand)
        self._bytes += cand.nbytes
        held.sort(key=lambda c: c.score, reverse=True)
        while len(held) > self.per_track:
            self._bytes -= held.pop().nbytes
        self.accepted += 1
        self._enforce_budget()

    def best(self, track_id: str) -> Optional[Candidate]:
        held = self._by_track.get(track_id)
        return held[0] if held else None

    def candidates(self, track_id: str) -> list[Candidate]:
        return list(self._by_track.get(track_id, ()))

    def forget(self, track_ids) -> None:
        for tid in track_ids:
            for cand in self._by_track.pop(tid, ()):
                self._bytes -= cand.nbytes
        self._bytes = max(0, self._bytes)

    def _enforce_budget(self) -> None:
        """Drop the weakest candidates globally until inside the ceiling.

        Weakest-first rather than oldest-first: the buffer exists to hold the
        BEST looks, so under pressure the thing to give up is the worst one
        anywhere, not the oldest one somewhere.
        """
        if self._bytes <= self.max_bytes:
            return
        ranked = sorted(
            ((c.score, tid, i) for tid, held in self._by_track.items()
             for i, c in enumerate(held)),
            key=lambda r: r[0])
        for _score, tid, _i in ranked:
            if self._bytes <= self.max_bytes:
                break
            held = self._by_track.get(tid)
            if not held:
                continue
            self._bytes -= held.pop().nbytes
            self.evicted_for_budget += 1
            if not held:
                self._by_track.pop(tid, None)

    def snapshot(self) -> dict:
        return {
            "tracks_held": len(self._by_track),
            "candidates_held": sum(len(v) for v in self._by_track.values()),
            "bytes_held": self._bytes,
            "max_bytes": self.max_bytes,
            "per_track": self.per_track,
            "offered": self.offered,
            "accepted": self.accepted,
            "evicted_for_budget": self.evicted_for_budget,
        }
