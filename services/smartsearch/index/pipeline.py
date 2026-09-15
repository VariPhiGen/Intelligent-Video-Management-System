"""pipeline.py — everything below the crop.

WHAT THIS SERVICE DOES NOW. It takes finished observations — a crop and the
metadata describing it — embeds them with CLIP, deduplicates them by
appearance, writes the crop to disk and the row to the index. That is the whole
job, and it is what the service's name says.

WHAT MOVED OUT, AND WHY. Detection, plate reading, tracking and the indexing
policy live in services/analytics now. They needed a detector, an OCR model and
a plate localiser; this needs an encoder and a database. Splitting them means
neither image carries the other's weights, and a new AI feature lands in
analytics without touching the index.

Observations arrive on POST /observations. There is NO frame source here, no
decoder, no queue and no worker thread: nothing to pull and nothing to schedule,
because the caller has already decided what is worth recording.

BACKPRESSURE LIVES AT THE CALLER. Embedding happens on the request thread, so a
slow encoder slows the response, and the analytics sink's own bounded queue
absorbs it. That is real backpressure — one queue, on the producing side, where
it can be seen — rather than two queues quietly filling at both ends.

EMBEDDING BEFORE DEDUPLICATING IS NOT WASTE: the dedup decision IS the
embedding comparison.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from PIL import Image

from .config import AppConfig
from .dedup import Deduplicator
from .domains import DOMAINS
from .embedder import ClipEmbedder
from .store import Store
from .writer import CropWriter

log = logging.getLogger("smartsearch.pipeline")


@dataclass
class ObservationInput:
    """One accepted observation, as the half below the crop needs it.

    The crop is a PIL image in RGB — already cut, already sized. Sending the
    frame instead would put 6 MB on the wire where ~200 KB will do, and would
    make this side repeat the bbox arithmetic that produced it.
    """
    slug: str
    crop: "Image.Image"
    ts: float
    domain: str
    confidence: float
    label: str
    plate: Optional[str]
    plate_confidence: Optional[float]
    #: normalised 0-1 (x1, y1, x2, y2)
    bbox: tuple
    tracker_id: Optional[str]
    #: The whole frame this crop was cut from, as JPEG, for the detections
    #: feed. Optional: an older producer sends none and the row is frameless.
    frame_jpeg: Optional[bytes] = None
    #: Every object that frame held — box, label, tracker id — so the feed can
    #: draw all of them and not just this observation's own. Drawing only.
    frame_boxes: Optional[list] = None
    #: Faces arrive WITH their vector. SFace embeds a 112x112 aligned face and
    #: the producer has the raw frame; this service only has the stored crop,
    #: whose JPEG quality moves an embedding by as much as identity does (see
    #: index/domains.py). None means "embed it here", which is what the CLIP
    #: domains do and what every existing caller still sends.
    embedding: Optional[list] = None
    #: Face width in source pixels, for the quality floor. Meaningless for the
    #: other domains, which is why it rides the observation rather than being
    #: derived from bbox here — the crop has been resized by then.
    face_width_px: Optional[int] = None
    #: WHICH model produced `embedding`. Stored per row so a model swap is a
    #: re-index that can be measured (`select embedding_model, count(*)`)
    #: rather than a silent mixture of two vector spaces in one column.
    embedding_model: Optional[str] = None


class IngestPipeline:
    def __init__(self, config: AppConfig, embedder: ClipEmbedder,
                 store: Store, writer: CropWriter) -> None:
        self._cfg = config
        self._embedder = embedder
        self._store = store
        self._writer = writer
        # One deduplicator per (camera, domain): a person and a car in the same
        # frame are never the same object, and comparing them wastes the
        # comparison and risks merging two things that are not alike.
        self._dedup: dict[tuple[str, str], Deduplicator] = {}
        #: Which domains each camera contributes. Analytics filters on these
        #: too; kept here because /cameras reports them and because a domain
        #: this camera does not contribute must never reach the store.
        self._domains: dict[str, tuple[str, ...]] = {}

        self.crops_embedded = 0
        self.crops_deduped = 0
        self.rows_written = 0
        self.observations_received = 0
        self.observations_rejected = 0
        self.last_error: Optional[str] = None

    # ── camera bookkeeping ──────────────────────────────────────────────────
    def set_domains(self, slug: str, domains: tuple[str, ...]) -> None:
        self._domains[slug] = tuple(domains)

    def forget_domains(self, slug: str) -> None:
        self._domains.pop(slug, None)
        # Drop this camera's dedup windows too, or a re-added camera would be
        # compared against clusters from before it went away.
        for key in [k for k in self._dedup if k[0] == slug]:
            self._dedup.pop(key, None)

    def domains_for(self, slug: str) -> tuple[str, ...]:
        return self._domains.get(slug, ("person", "vehicles"))

    # ── below the crop: embed, deduplicate, store ───────────────────────────
    def ingest_observations(self, items: "list[ObservationInput]") -> int:
        """Embed, deduplicate and store. Returns rows written.

        ONE IMPLEMENTATION, TWO CALLERS. _process reaches it in-process; the
        analytics service reaches it over HTTP. Splitting detection into its
        own service must not split THIS, or the two paths would drift in
        exactly the way two motion implementations already did once.

        Everything above the crop — what was detected, which track it belongs
        to, whether the policy wanted it, what the plate says — is decided by
        the caller. This half knows only pixels and metadata.

        EMBEDDING BEFORE DEDUPLICATING IS NOT WASTE: the dedup decision IS the
        embedding comparison.
        """
        self.observations_received += len(items)
        # A domain this camera does not contribute must never reach the store,
        # even if the caller sends it. Analytics filters too; this is the
        # backstop, because the store is the thing an operator later trusts.
        kept = [i for i in items if i.domain in self.domains_for(i.slug)]
        self.observations_rejected += len(items) - len(kept)
        items = kept
        if not items:
            return 0
        # Two kinds of observation now. A CLIP domain hands over pixels and
        # this service embeds them; a face hands over a vector computed where
        # the raw frame was. Embedding a face here with CLIP would put a
        # 512-dim vector in a 128-dim column — it would fail at the INSERT,
        # which is the right failure but a late one, so they are separated
        # here by what the registry says rather than by the domain name.
        supplied = [i for i in items if i.embedding is not None]
        to_embed = [i for i in items if i.embedding is None]
        self.crops_embedded += len(to_embed)
        vectors_by_id = {id(i): np.asarray(i.embedding, dtype="float32")
                         for i in supplied}
        if to_embed:
            for item, vec in zip(to_embed, self._embedder.embed_images(
                    [i.crop for i in to_embed])):
                vectors_by_id[id(item)] = vec
        vectors = [vectors_by_id[id(i)] for i in items]

        pending: dict[str, list[dict]] = defaultdict(list)
        for item, vec in zip(items, vectors):
            dedup = self._dedup.setdefault(
                (item.slug, item.domain),
                Deduplicator(threshold=self._cfg.ingest.dedup_cosine_threshold,
                             window_seconds=self._cfg.ingest.dedup_window_seconds),
            )
            if not dedup.observe(vec, item.ts, item.confidence):
                self.crops_deduped += 1
                continue
            path = self._writer.save(item.domain, item.slug, item.crop, item.ts)
            if path is None:
                continue                                  # disk problem, counted
            # After the crop, and only for a row that is actually written: a
            # deduplicated observation has no row, so its frame would be an
            # image of a person that nothing points at. Several objects in one
            # frame share one file — see CropWriter.save_frame.
            # And only for a domain whose rows keep a frame. A face row has no
            # frame_path column, so a frame written for it would be a picture
            # of a person nothing references — see Domain.frames.
            keeps_frame = bool(item.frame_jpeg) and getattr(
                DOMAINS.get(item.domain), "frames", False)
            frame_path = (self._writer.save_frame(item.slug, item.ts, item.frame_jpeg)
                          if keeps_frame else None)
            pending[item.domain].append({
                "embedding": str(vec.tolist()),
                "camera": item.slug,
                "ts": datetime.fromtimestamp(item.ts, tz=timezone.utc),
                "confidence": item.confidence,
                # The detector's own class name. For vehicles this IS the
                # vehicle type (car / truck / bus / motorcycle).
                "vehicle_type": item.label,
                "plate": item.plate,
                "plate_confidence": item.plate_confidence,
                # Normalised 0-1, the same convention the zone config uses.
                "bbox": list(item.bbox),
                "crop_path": path,
                # The whole scene, for the feed. None from older producers.
                "frame_path": frame_path,
                # And what was in it. Identical on every row of one frame, so a
                # card survives its siblings being deduplicated away.
                "frame_boxes": item.frame_boxes if frame_path else None,
                # Faces only; None on every other domain, and write_batch turns
                # a missing extra into NULL rather than shifting the columns.
                "face_width_px": item.face_width_px,
                "embedding_model": item.embedding_model,
                # Scoped id — see tracking.ObjectTracker._new_id. Stored so an
                # operator can later ask for every observation of one object.
                "tracker_id": item.tracker_id,
            })

        written = 0
        for domain, rows in pending.items():
            written += self._store.write_batch(domain, rows)
        self.rows_written += written
        return written

    # ── observability ────────────────────────────────────────────────────────

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            # THE FUNNEL, IN ORDER, SO THE QUANTITIES CANNOT BE CONFUSED:
            #   observations_received  what analytics decided was worth keeping
            #   crops_embedded         CLIP forward passes actually paid for
            #   crops_deduped          rejected afterwards on appearance
            #   rows_written           what a search can return
            "observations_received": self.observations_received,
            "crops_embedded": self.crops_embedded,
            "crops_deduped": self.crops_deduped,
            "rows_written": self.rows_written,
            "dedup": {
                "cosine_threshold": self._cfg.ingest.dedup_cosine_threshold,
                "window_seconds": self._cfg.ingest.dedup_window_seconds,
                "live_windows": len(self._dedup),
                "live_clusters": sum(d.live_clusters
                                     for d in self._dedup.values()),
            },
            "cameras_known": len(self._domains),
            "last_error": self.last_error,
        }
