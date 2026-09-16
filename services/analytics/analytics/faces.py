"""faces.py — find a face inside a person crop, align it, and embed it here.

WHY THE EMBEDDING IS COMPUTED IN THIS SERVICE, which is not where the other
domains are embedded. Person and vehicle crops travel to Smart Search as pixels
and are embedded there by CLIP. Faces cannot afford that trip. Measured on this
appliance's own corpus 2026-09-14:

    same person, two photographs      median cosine 0.387
    different people                  median cosine 0.117
    THE SAME FACE, re-encoded at q60  median cosine 0.875, worst 0.539

The identity signal is a ~0.27 band and JPEG compression alone moves an
embedding by up to 0.46. Smart Search only ever sees the stored quality-82 crop,
so embedding there would spend most of the margin on the encoder. This service
has the decoded frame, so the vector is computed before any of that is lost and
travels with the observation.

WHAT RUNS. YuNet (cv2.FaceDetectorYN, ~230 KB) finds faces and their five
landmarks; SFace (cv2.FaceRecognizerSF, ~37 MB) aligns to 112x112 and embeds to
128 dimensions. Both ship inside the `opencv-python-headless` already pinned
here — the only new artefacts are two model files in the shared models volume.

THE MODEL IS CHOSEN BY NAME FROM A REGISTRY, and its name travels with every
observation. This service produces vectors that another service stores and
searches, so the two have to agree on the model and not merely on the width:
two 128-dim face models share no vector space, and an upgrade that reaches one
service first would otherwise write vectors that INSERT cleanly, rank
plausibly, and mean nothing. The index checks the name and refuses a mismatch.
Mirrors index/faces.py in smartsearch — separate deployables, so the registry
is duplicated deliberately rather than shared through an import that would
couple their release cycles.

A SECOND MODEL OVER EVERY PERSON, exactly like the plate reader is a second
model over every vehicle, and loaded on the same terms: only when some camera
asks for the `face` domain, released when none do.

WHAT IS STORED IS WHAT THE MODEL SAW. The crop posted to Smart Search is the
ALIGNED 112x112 face, not a prettier padded rectangle, because that is the image
the vector describes. A gallery showing one thing while the index matched
another is how a search result becomes impossible to argue with.

NIGHT COSTS NOTHING AND RETURNS NOTHING, so it is not paid for. These models
are trained on colour; measured over this appliance's own crops, near-greyscale
(IR-illuminated) crops produced **0 faces out of 99** against 11.1% on colour
crops. Running the detector on them is spend with a measured zero return, and
worse, it makes a camera that is working correctly look like a broken model.
The colour check is three lines on a downsample and it runs before the detector.

On THIS site that saves about 4% of calls, which is nothing — the saving is for
the deployment this rule is really about: an outdoor camera on IR from dusk to
dawn, where it is most of the night's crops.

A SIZE FLOOR ON THE CROP WAS MEASURED AND REJECTED. A person crop narrower than
~67px cannot hold a 40px face except at a face/crop ratio above 0.6, which the
survey puts beyond its 95th percentile. Gating there would skip 15.1% of
detector calls and lose 7 of 1,639 usable faces. At ~5 ms a call and this site's
volume, 15.1% is about 4.5 seconds of CPU a day — not worth 0.4% of the faces.
The cheap gate is not always the right one, and the way to know is to price it.

THE SIZE FLOOR IS THE FEATURE'S HONESTY. Measured over 7,516 person crops from
this site, only ~8% carry a face 40px or wider and ~5% reach 64px; below 40px
SFace is embedding an upscale of pixels nobody captured, and retrieval rank-1
fell from 40% (64-111px) to 24% (<40px). Faces under the floor are not stored —
a row that cannot answer a query is cost without benefit, and under
data-protection law it is biometric data collected for nothing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

from . import face_weights

log = logging.getLogger("analytics.faces")


@dataclass(frozen=True)
class FaceModel:
    """A candidate embedder: name, width, and where it loads from."""
    name: str
    dim: int
    weights: str
    note: str


#: The registry. One proven entry, matching smartsearch's — a second one is
#: added to BOTH, and then the index needs a migration and a re-index before it
#: can store what this produces.
FACE_MODELS: dict[str, FaceModel] = {
    "sface-2021dec": FaceModel(
        name="sface-2021dec", dim=128,
        weights="/models/face_recognition_sface_2021dec.onnx",
        note="OpenCV Zoo SFace, 112x112 aligned BGR in, 128-d out",
    ),
}

DEFAULT_FACE_MODEL = "sface-2021dec"


@dataclass(frozen=True)
class DetectedFace:
    """One face, ready to become an observation."""
    aligned: np.ndarray            # BGR 112x112 — what SFace embedded
    embedding: np.ndarray          # L2-normalised, 128-dim
    score: float                   # YuNet confidence
    width_px: int                  # face width in SOURCE pixels
    #: Face box in the coordinates of the crop it was found in: x1, y1, x2, y2.
    box: tuple[int, int, int, int]


class FaceReader:
    """Detect + align + embed. Inactive when the models are not installed."""

    #: Mean per-pixel channel spread below which a crop is treated as
    #: greyscale. Measured: IR crops on this appliance sit near 0 and colour
    #: crops well above 3. Not 0, because JPEG chroma noise puts a true
    #: greyscale frame slightly above it.
    COLOUR_SPREAD_MIN = 3.0

    def __init__(self, detector, recogniser, *, model: FaceModel,
                 score_threshold: float, min_width_px: int,
                 max_per_crop: int, require_colour: bool = True) -> None:
        self._det = detector
        self._rec = recogniser
        #: Written into every observation this reader produces. The index
        #: rejects an observation whose model is not the one it runs.
        self.model = model.name
        self.dim = model.dim
        self.score_threshold = float(score_threshold)
        self.min_width_px = int(min_width_px)
        self.max_per_crop = max(1, int(max_per_crop))
        self.require_colour = bool(require_colour)
        self.calls = 0
        self.faces_found = 0
        self.rejected_small = 0
        #: Crops skipped before the detector ran because they carry no colour.
        #: Counted, not silent: "we did not look" and "we looked and found
        #: nothing" are different facts about a camera.
        self.skipped_greyscale = 0
        #: Crops too small to hold a face at all, skipped before the detector.
        #: Counted for the same reason as the greyscale skip, and for one more:
        #: it is what makes the reader's own arithmetic close. Every crop handed
        #: to an ACTIVE reader lands in exactly one of `calls`,
        #: `skipped_greyscale` or `skipped_too_small`, so a caller comparing its
        #: own "crops searched" against these can tell a silent drop from a
        #: deliberate skip. Without it the three numbers simply did not add up
        #: and nothing on /health explained the difference.
        self.skipped_too_small = 0

    @property
    def active(self) -> bool:
        return self._det is not None and self._rec is not None

    def read(self, crop: np.ndarray) -> list[DetectedFace]:
        """Faces in one person crop, best first. Never raises.

        A failure here must cost the face, not the observation: the person row
        is the product's baseline and it is already earned by the time this
        runs.
        """
        if not self.active:
            # Not counted: the call site gates on `active`, and a reader with no
            # models has no arithmetic to keep.
            return []
        if (crop is None or crop.size == 0
                or crop.shape[0] < 20 or crop.shape[1] < 20):
            # Smaller than YuNet's smallest anchor. Asking anyway costs a
            # resize and an exception on some builds. A degenerate crop — None,
            # or zero-sized from a collapsed box — is the same fact and is
            # counted with it rather than vanishing.
            self.skipped_too_small += 1
            return []
        h, w = crop.shape[:2]
        if self.require_colour and not self._has_colour(crop):
            self.skipped_greyscale += 1
            return []
        self.calls += 1
        try:
            self._det.setInputSize((w, h))
            _, raw = self._det.detect(crop)
        except Exception as exc:                                  # noqa: BLE001
            log.debug("face detection failed: %s", exc)
            return []
        if raw is None or len(raw) == 0:
            return []

        out: list[DetectedFace] = []
        for row in sorted(raw, key=lambda r: float(r[-1]), reverse=True):
            if len(out) >= self.max_per_crop:
                break
            score = float(row[-1])
            if score < self.score_threshold:
                continue
            width = int(row[2])
            if width < self.min_width_px:
                self.rejected_small += 1
                continue
            try:
                aligned = self._rec.alignCrop(crop, row)
                vec = np.asarray(self._rec.feature(aligned), dtype="float32").ravel()
            except Exception as exc:                              # noqa: BLE001
                log.debug("face alignment/embedding failed: %s", exc)
                continue
            norm = float(np.linalg.norm(vec))
            if norm == 0.0:
                continue
            x, y, bw, bh = (int(row[0]), int(row[1]), width, int(row[3]))
            out.append(DetectedFace(aligned=aligned, embedding=vec / norm,
                                    score=score, width_px=width,
                                    box=(x, y, x + bw, y + bh)))
        self.faces_found += len(out)
        return out

    @classmethod
    def _has_colour(cls, crop: np.ndarray) -> bool:
        """Is there chroma in this crop, or is it an IR frame?

        Measured on a DOWNSAMPLE — a 32-pixel-wide thumbnail answers the same
        question as the full crop and costs microseconds, which matters because
        this runs in front of every face call rather than instead of one.
        """
        if crop.ndim != 3 or crop.shape[2] < 3:
            return False
        small = crop[::max(1, crop.shape[0] // 32), ::max(1, crop.shape[1] // 32)]
        b = small[:, :, 0].astype(np.int16)
        g = small[:, :, 1].astype(np.int16)
        r = small[:, :, 2].astype(np.int16)
        spread = float(np.mean(np.abs(b - g) + np.abs(g - r)) / 2)
        return spread >= cls.COLOUR_SPREAD_MIN

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "model": self.model,
            "dim": self.dim,
            "score_threshold": self.score_threshold,
            "min_width_px": self.min_width_px,
            "calls": self.calls,
            "faces_found": self.faces_found,
            # Counted separately from "found nothing": a site whose faces are
            # all too small is a placement problem, not a model problem, and
            # the two are indistinguishable in a single miss counter.
            "rejected_too_small": self.rejected_small,
            # Night, in a counter. A camera on IR reports skips rather than a
            # flat zero, so "this camera sees no faces" and "this camera was
            # dark" stay distinguishable in /health.
            "skipped_greyscale": self.skipped_greyscale,
            # Completes the arithmetic: calls + skipped_greyscale +
            # skipped_too_small is every crop this reader was given. A gap
            # between that total and the pipeline's own person_crops_searched
            # is then a real drop, not a counter that was never written.
            "skipped_too_small": self.skipped_too_small,
        }


def build(cfg, device: Optional[str] = None) -> Optional[FaceReader]:
    """Load the detector and the configured embedder. None if unavailable.

    NEVER RAISES, for plates.py's reason: a missing face model costs face
    indexing, not detection. `device` is accepted and ignored — both models run
    through OpenCV's own DNN backend on CPU, which is where this measured 5-7 ms
    per person crop; there is no CUDA path to choose between.
    """
    spec = FACE_MODELS.get(cfg.model)
    if spec is None:
        log.warning("unknown face model %r; registered: %s — the face domain "
                    "will index nothing", cfg.model, sorted(FACE_MODELS))
        return None
    # FETCH BEFORE ASKING WHETHER THEY ARE THERE — see face_weights.py. This
    # runs inside the warm-up thread, off the engine lock, which is where the
    # plate models already download; the docstring's "never blocks" contract is
    # about add_camera, not about this.
    face_weights.ensure(cfg.detector_weights, cfg.recogniser_weights or spec.weights)
    for path in (cfg.detector_weights, cfg.recogniser_weights or spec.weights):
        if not path or not os.path.isfile(path):
            log.warning("face models not available (%s missing) — the face "
                        "domain will index nothing", path or "<unset>")
            return None
    try:
        import cv2
        det = cv2.FaceDetectorYN.create(cfg.detector_weights, "", (320, 320),
                                        score_threshold=float(cfg.score_threshold),
                                        nms_threshold=0.3, top_k=20)
        rec = cv2.FaceRecognizerSF.create(cfg.recogniser_weights, "")
    except Exception as exc:                                      # noqa: BLE001
        log.warning("face models failed to load: %s", exc)
        return None
    log.info("face reader ready (model=%s dim=%d score>=%.2f min_width=%dpx "
             "max_per_crop=%d)", spec.name, spec.dim, cfg.score_threshold,
             cfg.min_width_px, cfg.max_per_crop)
    return FaceReader(det, rec, model=spec, score_threshold=cfg.score_threshold,
                      min_width_px=cfg.min_width_px,
                      max_per_crop=cfg.max_per_crop,
                      require_colour=cfg.require_colour)
