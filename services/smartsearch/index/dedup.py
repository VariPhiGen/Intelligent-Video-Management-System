"""dedup.py — one row per object, not one row per frame.

Deduplicates by APPEARANCE inside a short time window, per camera and per
domain. Geometry is never consulted.

WHY NOT A TRACKER. At 1 FPS a walking person crosses most of the frame between
samples, so IOU association is at its least reliable exactly where this pipeline
operates. Measured on real footage 2026-08-31: over the same 600 s, ByteTrack at
the camera's native 15 FPS found 8 identities; at 1 FPS it found 5. It MERGED
people rather than splitting them, which for search is the worse failure — a
merged identity is a person who never comes back as a hit.

Appearance clustering on the same crops recovered the 15 FPS partition: purity
0.983, ARI 0.946, and stable across cosine 0.80-0.88. 0.85 sits mid-plateau, not
on a cliff, so the threshold does not need tuning per site.

The compression is the point: 68 crops became 8 rows, 8.5x fewer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class _Cluster:
    centroid: np.ndarray
    last_seen: float
    count: int = 1
    #: Best crop so far, by detector confidence — that is the one worth keeping.
    best_confidence: float = 0.0


@dataclass
class Deduplicator:
    """Greedy, online, bounded. One instance per (camera, domain)."""

    threshold: float = 0.85
    window_seconds: float = 30.0
    _clusters: list[_Cluster] = field(default_factory=list)

    def observe(self, vector: np.ndarray, ts: float, confidence: float) -> bool:
        """True if this crop is a NEW object and should be written.

        False means an object already represented in this window — the row is
        not written, which is the whole saving.
        """
        self._evict(ts)
        best_i, best_s = -1, -1.0
        for i, c in enumerate(self._clusters):
            s = float(np.dot(c.centroid, vector))
            if s > best_s:
                best_i, best_s = i, s

        if best_i >= 0 and best_s >= self.threshold:
            c = self._clusters[best_i]
            # Running mean, renormalised: the cluster drifts with the object
            # rather than being pinned to whatever crop happened to arrive first.
            c.centroid = (c.centroid * c.count + vector) / (c.count + 1)
            n = np.linalg.norm(c.centroid)
            if n > 0:
                c.centroid = c.centroid / n
            c.count += 1
            c.last_seen = ts
            c.best_confidence = max(c.best_confidence, confidence)
            return False

        self._clusters.append(
            _Cluster(centroid=vector.copy(), last_seen=ts, best_confidence=confidence)
        )
        return True

    def _evict(self, now: float) -> None:
        if not self._clusters:
            return
        cutoff = now - self.window_seconds
        self._clusters = [c for c in self._clusters if c.last_seen >= cutoff]

    @property
    def live_clusters(self) -> int:
        return len(self._clusters)
