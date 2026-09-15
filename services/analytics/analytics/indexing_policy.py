"""indexing_policy.py — does this observation of a known object earn a row?

THE SEPARATION THIS MODULE EXISTS FOR. index/tracking.py answers "is this the
same physical object?". This answers "even though it is the same object, is
this worth another CLIP embedding, another crop on disk and another row?".
Those are different questions with different failure modes, and the old
temporal gate answered them with one number — which is why it re-indexed a
motionless person every 10 s: its window was simultaneously the identity
timeout and the re-index interval.

THE ANCHOR IS THE POINT. Everything here is measured against the observation
that was LAST INDEXED, not the one last seen. The temporal gate could not do
this: its entries followed their object, so displacement was always measured
from the previous frame and a steadily walking person was re-indexed on window
expiry rather than on distance travelled — a limitation its own test suite
documented rather than fixed. A tracker separates "where is it now" from
"where was it when we recorded it", and that is the main gain of the redesign.

WHAT THIS IS NOT. It is not deduplication. index/dedup.py still runs after the
CLIP encoder and still rejects near-identical vectors at cosine 0.85; it is the
only backstop for this layer's mistakes, because geometry cannot tell that two
tracks are the same person returning. The two layers fail differently, which is
why both are worth their cost.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .tracking import Track

#: Reasons an observation was indexed. Strings rather than an enum because they
#: land in /health counters and in logs, where a name is worth more than an int.
FIRST_SIGHT = "first_sight"
DISPLACEMENT = "displacement"
SCALE_CHANGE = "scale_change"
HEARTBEAT = "heartbeat"
QUALITY_GAIN = "quality_gain"
SUPPRESSION_OFF = "suppression_disabled"


@dataclass
class _Anchor:
    """What was true the last time this track produced a row."""
    ts: float
    cx: float
    by: float
    h: float
    confidence: float
    area: float


class IndexingPolicy:
    """One instance per pipeline; anchors keyed by track id.

    Holds no identity state of its own — it is handed a Track and asked a
    question. That is what lets suppression be turned off wholesale without the
    tracker noticing.
    """

    def __init__(self, *, suppression_enabled: bool = True,
                 displacement_heights: float = 1.5,
                 scale_change_ratio: float = 1.5,
                 scale_change_persist: int = 2,
                 heartbeat_seconds: float = 60.0,
                 quality_confidence_gain: float = 0.15,
                 min_interval_seconds: float = 0.0) -> None:
        self.suppression_enabled = suppression_enabled
        self.displacement_heights = displacement_heights
        self.scale_change_ratio = max(1.0 + 1e-6, scale_change_ratio)
        self.scale_change_persist = max(1, int(scale_change_persist))
        self.heartbeat_seconds = heartbeat_seconds
        self.quality_confidence_gain = quality_confidence_gain
        #: The floor between two records of the SAME track, whatever the
        #: reason. See decide(); 0 turns it off and leaves the change-driven
        #: rules to decide alone.
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))

        self._anchors: dict[str, _Anchor] = {}
        #: Consecutive observations for which the scale condition has held, per
        #: track. Reset the moment it does not hold, and whenever the anchor
        #: moves — a new anchor is a new comparison.
        self._scale_pending: dict[str, int] = {}
        self.indexed = 0
        self.suppressed = 0
        self.by_reason: dict[str, int] = {}

    # ── the decision ─────────────────────────────────────────────────────────
    def decide(self, track: Track, detection, ts: float) -> Optional[str]:
        """The reason to index this observation, or None to suppress it."""
        if not self.suppression_enabled:
            return SUPPRESSION_OFF

        anchor = self._anchors.get(track.track_id)
        if anchor is None:
            # Never indexed. The forensic baseline, and it is never skipped:
            # an object that produced no row at all is a person no search can
            # return, which is the one failure this product cannot afford.
            return FIRST_SIGHT

        x1, y1, x2, y2 = detection.xyxy
        cx = float(x1 + x2) / 2.0
        by = float(y2)
        h = max(1.0, float(y2 - y1))
        area = max(1.0, float(x2 - x1) * h)

        # 2. Moved far enough from where it was RECORDED to be a new look at it.
        scale = max(1e-6, (h + anchor.h) / 2.0)
        moved_far = (math.hypot(cx - anchor.cx, by - anchor.by) / scale
                     >= self.displacement_heights)

        # 3. Approached or receded — BUT ONLY IF IT PERSISTS.
        #
        # The counter is updated HERE, before any rule can return, so it stays
        # correct even on frames where displacement fires first. Doing it
        # inside the `if` below would make persistence depend on which rule
        # happened to match, which is not what it means.
        ratio = max(h, anchor.h) / max(1e-6, min(h, anchor.h))
        if ratio >= self.scale_change_ratio:
            held = self._scale_pending.get(track.track_id, 0) + 1
            self._scale_pending[track.track_id] = held
        else:
            # Did not hold. A one-frame spike is a bad box, not a moved object.
            held = 0
            self._scale_pending.pop(track.track_id, None)

        # THE INTERVAL FLOOR: one record per track per interval, whatever
        # changed. After the scale counter, so "persists" still means held on
        # consecutive frames; before every rule that could re-record, so a
        # person walking toward the camera — who trips displacement AND scale
        # AND quality within seconds — still produces one row per interval.
        #
        # WHY IT EXISTS. At 5 FPS each change-driven rule fires on its own
        # evidence, and one person reached Smart Search five times in ten
        # seconds (tracker 3721, cam3, 2026-09-11 11:34:17-27): five CLIP
        # passes and five cards in the feed for one walk past a camera. The
        # operator's rule is simpler and this is it — first sight, then at most
        # one look per interval while the object stays.
        if (self.min_interval_seconds > 0
                and (ts - anchor.ts) < self.min_interval_seconds):
            return None

        if moved_far:
            return DISPLACEMENT
        if held >= self.scale_change_persist:
            return SCALE_CHANGE

        # 4. Still here. Bounds worst-case absence from search for something
        #    that never moves. A product decision more than a technical one.
        if self.heartbeat_seconds > 0 and (ts - anchor.ts) >= self.heartbeat_seconds:
            return HEARTBEAT

        # 5. A better look at the same object: more confident AND no smaller.
        #    Both halves matter — confidence alone would re-index a jittering
        #    detector, and area alone would re-index anything drifting closer.
        if (detection.confidence >= anchor.confidence + self.quality_confidence_gain
                and area >= anchor.area):
            return QUALITY_GAIN

        return None

    def record(self, track: Track, detection, ts: float, reason: str) -> None:
        """Move the anchor. Call only for observations actually indexed."""
        x1, y1, x2, y2 = detection.xyxy
        h = max(1.0, float(y2 - y1))
        self._anchors[track.track_id] = _Anchor(
            ts=ts, cx=float(x1 + x2) / 2.0, by=float(y2), h=h,
            confidence=float(detection.confidence),
            area=max(1.0, float(x2 - x1) * h),
        )
        # The anchor moved, so any part-completed scale change was measured
        # against a reference that no longer exists.
        self._scale_pending.pop(track.track_id, None)
        self.indexed += 1
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def suppress(self) -> None:
        self.suppressed += 1

    # ── lifecycle ────────────────────────────────────────────────────────────
    def forget(self, track_ids) -> None:
        """Drop anchors for retired tracks.

        Unbounded growth here would be a slow leak that only shows on a busy
        site after days, so the tracker reports what it expired and this prunes
        against that rather than guessing with a timer.
        """
        for tid in track_ids:
            self._anchors.pop(tid, None)
            self._scale_pending.pop(tid, None)

    def snapshot(self) -> dict:
        total = self.indexed + self.suppressed
        return {
            "suppression_enabled": self.suppression_enabled,
            "displacement_heights": self.displacement_heights,
            "scale_change_ratio": self.scale_change_ratio,
            "scale_change_persist": self.scale_change_persist,
            "heartbeat_seconds": self.heartbeat_seconds,
            "min_interval_seconds": self.min_interval_seconds,
            "observations_indexed": self.indexed,
            "observations_suppressed": self.suppressed,
            "suppression_rate": (
                round(self.suppressed / total, 4) if total else 0.0
            ),
            "indexed_by_reason": dict(self.by_reason),
            "tracked_anchors": len(self._anchors),
        }
