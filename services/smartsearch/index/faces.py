"""faces.py — the face embedder seam, and the query side of face search.

WHY THERE IS AN INTERFACE HERE WITH ONE ENTRY IN THE REGISTRY. Same pattern as
embedder.py, and for a sharper version of the same reason. A face vector is only
comparable with the face vectors already stored, so the model is not a
configuration choice that can be revisited casually — it is the identity of the
index. But unlike CLIP, this one has a live reason to be revisited: measured on
this appliance 2026-09-14, SFace separates same-person from different-person
pairs at AUC 0.881 and rank-1 32%, and the faces here are small (median 59px).
If calibration on a real site says SFace is weak at that size, an ArcFace-class
model is the known answer.

So the seam is built for that swap: a backend owns its own alignment,
preprocessing and model; it reports `name` and `dim`; and the registry below is
what a new one is added to. Adding it is a registry entry and a re-index, not a
refactor of the callers — index/models.py holds the encoder, index/pipeline.py
never touches it (analytics embeds at ingest, off the raw frame) and
index/queries.py asks only for a vector.

THE THREE THINGS THAT MAKE A SWAP SAFE, all of which are enforced rather than
documented:

  1. `dim` is on the interface, checked against the schema at load. A 512-dim
     model against a vector(128) column is refused permanently, the way
     models.py refuses a mis-sized CLIP encoder — retrying only writes a wrong
     answer later.
  2. Every row records the model that produced it (`embedding_model`, migration
     006) and every query is scoped to the ACTIVE model. A half-finished
     re-index therefore returns fewer results, never wrong ones. This is the
     protection the CLIP domains do not have, and their own docstring says so.
  3. The producer sends its model name with each observation, so a fleet that
     is half-upgraded is rejected at the door with a message naming both
     models, rather than silently mixing two vector spaces in one column.

WHAT RUNS TODAY. YuNet (cv2.FaceDetectorYN, ~230 KB) detects and gives the five
landmarks; SFace (cv2.FaceRecognizerSF, ~37 MB) aligns to 112x112 and embeds to
128 dimensions. Both ship inside the pinned `opencv-python-headless`; the only
new artefacts are two model files in the shared volume.

THE SCORE THRESHOLD IS THE WHOLE FEATURE, so it is configuration. Measured over
7,516 stored person crops: at YuNet's 0.6 default, 30.7% of crops "contain a
face" — but sampled by eye, the 0.60-0.70 band is mostly the BACK of a head.
At 0.85, 36 of 36 sampled detections were real faces. At the zoo demo's 0.9 the
rate collapses to 2.6%.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np

from .backends import BY_INCUMBENT, ONNX, BackendSpec
from .domains import DOMAINS, FACE

log = logging.getLogger("smartsearch.faces")

#: The width the schema holds. Not a constant of its own: the registry IS the
#: authority on what a domain stores, so a model is checked against it.
FACE_DIM = DOMAINS[FACE].dims


class FaceEmbedder(Protocol):
    """Align a detected face and embed it.

    `name` and `dim` are both on the interface. `dim` because the column is
    vector(128) and a different width is an incompatible index rather than a
    setting; `name` because it is written with every row, so a re-index can be
    told apart from a mixed index by looking at the data instead of guessing.

    ALIGNMENT BELONGS TO THE BACKEND. It is not a shared pre-step: SFace's warp
    comes from cv2's own recogniser, and an ArcFace ONNX would bring its own
    template and normalisation. A shared aligner would silently hand each model
    the other's geometry, which is the failure that looks like "the new model is
    worse" rather than like a bug.
    """
    name: str
    dim: int

    def align(self, image_bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray: ...
    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray: ...

    @property
    def backend(self) -> BackendSpec: ...


@dataclass(frozen=True)
class FaceModel:
    """A candidate embedder: what it is called, how wide, and where it loads from."""
    name: str
    dim: int
    #: Default path in the shared models volume. Overridable per deployment.
    weights: str
    #: How it is executed today, for /health and for the calibration profile.
    representation: str
    note: str


#: THE CANDIDATE REGISTRY. One proven entry, which is a deliberate state rather
#: than an unfinished one — the same sentence embedder.py's docstring uses, and
#: here it comes with the measurement that would justify a second:
#:
#:   sface-2021dec   LFW 0.994, but 82% top-1 on nearest-neighbour search over
#:                   LFW crops, and rank-1 32% on THIS site's faces.
#:
#: An ArcFace-class candidate (InsightFace buffalo_l, w600k_r50, 512-dim) is the
#: obvious second entry. It is not listed because listing it would imply it is
#: loadable here, and it is not: it needs onnxruntime in this image, its own
#: alignment template, and a vector(512) column. That is a registry entry, a
#: migration and a re-index — in that order, and none of them guessed.
FACE_MODELS: dict[str, FaceModel] = {
    "sface-2021dec": FaceModel(
        name="sface-2021dec",
        dim=128,
        weights="/models/face_recognition_sface_2021dec.onnx",
        representation=ONNX,
        note="OpenCV Zoo SFace, 112x112 aligned BGR in, 128-d out",
    ),
}

DEFAULT_FACE_MODEL = "sface-2021dec"


class ModelMismatch(RuntimeError):
    """The configured model cannot be used against this schema. Permanent."""


@dataclass(frozen=True)
class Face:
    """One detected face: the aligned crop, its vector, and the size that
    decides whether either is worth anything."""
    embedding: np.ndarray          # L2-normalised, `dim` wide
    aligned: np.ndarray            # BGR, as the backend aligned it
    score: float                   # the detector's confidence
    width_px: int                  # face width in SOURCE pixels
    box: tuple[int, int, int, int]


class SFaceEmbedder:
    """cv2.FaceRecognizerSF. Owns its alignment, which is cv2's own warp."""

    def __init__(self, recogniser, spec: FaceModel, artifact: str) -> None:
        self._rec = recogniser
        self.name = spec.name
        self.dim = spec.dim
        # Built once at load, because that is when the facts are true — see
        # BackendSpec, which is frozen for the same reason.
        #
        # THE DEVICE IS "cpu" AS A FACT, NOT A PREFERENCE. cv2's DNN backend
        # here is the default one; there is no CUDA build of opencv in this
        # image, so claiming anything else would be the silent-downgrade
        # failure ort_spec exists to name.
        self._backend = BackendSpec(
            component="face_embedder",
            representation=spec.representation,
            runtime="opencv_dnn",
            device="cpu",
            selected_by=BY_INCUMBENT,
            artifact=artifact,
        )

    @property
    def backend(self) -> BackendSpec:
        return self._backend

    def align(self, image_bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        return self._rec.alignCrop(image_bgr, face_row)

    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray:
        vec = np.asarray(self._rec.feature(aligned_bgr), dtype="float32").ravel()
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return vec
        # STORED NORMALISED. pgvector's cosine operator does not care, but
        # anything that later compares by dot product does, and one normalised
        # end of a comparison is worse than neither.
        return vec / norm


class FaceEncoder:
    """Detector + embedder. Inactive when the models are not installed."""

    def __init__(self, detector, embedder: Optional[FaceEmbedder], *,
                 score_threshold: float, min_width_px: int,
                 error: Optional[str] = None) -> None:
        self._det = detector
        self._emb = embedder
        self.score_threshold = float(score_threshold)
        self.min_width_px = int(min_width_px)
        #: Why there is no encoder, when there is none. "Not installed" and
        #: "installed but incompatible" need different actions from an operator.
        self.error = error

    @property
    def active(self) -> bool:
        return self._det is not None and self._emb is not None

    @property
    def name(self) -> Optional[str]:
        return self._emb.name if self._emb is not None else None

    @property
    def dim(self) -> Optional[int]:
        return self._emb.dim if self._emb is not None else None

    def detect(self, image: np.ndarray) -> list[Face]:
        """Every face in the image, best first. BGR in, as OpenCV reads it."""
        if not self.active or image is None or image.size == 0:
            return []
        h, w = image.shape[:2]
        if w < 20 or h < 20:
            # Smaller than YuNet's smallest anchor; asking anyway raises on
            # some builds and can never return a usable face.
            return []
        self._det.setInputSize((w, h))
        try:
            _, raw = self._det.detect(image)
        except Exception as exc:                                  # noqa: BLE001
            log.debug("face detection failed: %s", exc)
            return []
        if raw is None:
            return []
        out: list[Face] = []
        for row in raw:
            score = float(row[-1])
            width = int(row[2])
            if score < self.score_threshold or width < self.min_width_px:
                continue
            try:
                aligned = self._emb.align(image, row)
                vec = self._emb.embed(aligned)
            except Exception as exc:                              # noqa: BLE001
                log.debug("face alignment/embedding failed: %s", exc)
                continue
            if not np.any(vec):
                continue
            out.append(Face(embedding=vec, aligned=aligned, score=score,
                            width_px=width,
                            box=(int(row[0]), int(row[1]),
                                 int(row[0]) + width, int(row[1]) + int(row[3]))))
        out.sort(key=lambda f: f.score, reverse=True)
        return out

    def best(self, image: np.ndarray) -> Optional[Face]:
        faces = self.detect(image)
        return faces[0] if faces else None

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "model": self.name,
            "dim": self.dim,
            "schema_dim": FACE_DIM,
            "score_threshold": self.score_threshold,
            "min_width_px": self.min_width_px,
            "backend": self._emb.backend.to_dict() if self._emb is not None else None,
            "error": self.error,
        }


#: How many photographs one face query may combine. Enough for a front view,
#: both three-quarter views and a change of lighting; past that the operator is
#: uploading an album rather than describing one face. camera-mgmt holds the
#: same number as a literal (separate deployable) and must not exceed this one.
MAX_QUERY_PHOTOS = 5


def fuse(vectors: list[np.ndarray]) -> tuple[np.ndarray, Optional[float]]:
    """One query vector from the faces in several photographs of ONE person.

    THE MEAN OF THE NORMALISED VECTORS, RENORMALISED. This is template pooling,
    the default way face benchmarks (IJB-B/C) turn several images of a subject
    into one query. Each photo carries identity plus its own nuisance (pose,
    light, compression). Identity is shared across the photos and the nuisance
    is not, so averaging keeps the first and cancels some of the second. Each
    input is normalised FIRST so a photo counts once whatever its raw norm.

    NOT MEASURED ON THIS SITE, and said so rather than implied. On 2026-09-15
    the live index held 223 faces and only 11 tracks with three or more — and
    those are adjacent frames of one pass, which would flatter any rule. So this
    is the published default, not a tuned choice; max-over-photos is the
    alternative to test once there is a corpus that can tell them apart.

    Also returns AGREEMENT: the lowest cosine between any two of the photos, or
    None for one photo. Averaging two DIFFERENT people yields a vector that
    resembles neither and still ranks plausibly, so the caller must be able to
    say "these may not be the same person". The number is returned, not judged
    here: the bands that interpret it live in the UI beside the bands that
    interpret match scores, and they are the same scale.
    """
    units = []
    for v in vectors:
        arr = np.asarray(v, dtype="float32").ravel()
        norm = float(np.linalg.norm(arr))
        if norm > 0.0:
            units.append(arr / norm)
    if not units:
        raise ValueError("fuse() needs at least one non-zero vector")
    stack = np.stack(units)
    mean = stack.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    # A zero mean needs exactly opposed inputs. The agreement of -1 returned
    # beside it is what says so, so falling back to the first photo hides nothing.
    fused = mean / norm if norm > 0.0 else stack[0]
    agreement: Optional[float] = None
    if len(units) > 1:
        sims = stack @ stack.T
        agreement = float(sims[np.triu_indices(len(units), k=1)].min())
    return fused.astype("float32"), agreement


def _inactive(cfg, error: str) -> FaceEncoder:
    log.warning("face search unavailable: %s", error)
    return FaceEncoder(None, None, score_threshold=cfg.score_threshold,
                       min_width_px=cfg.min_width_px, error=error)


def build(cfg, *, detector_path: str = "", weights: str = "") -> FaceEncoder:
    """Load the detector and the configured embedder, or return an inactive
    encoder carrying the reason.

    NEVER RAISES, which is plates.py's policy and for the same reason: a missing
    face model costs face search, not the service. The one thing it will not do
    is return a WORKING encoder whose width disagrees with the schema — that is
    refused permanently, because the alternative is an INSERT failure per row
    inside a write path that swallows failures by design.
    """
    spec = FACE_MODELS.get(cfg.model)
    if spec is None:
        return _inactive(cfg, f"unknown face model {cfg.model!r}; "
                              f"registered: {sorted(FACE_MODELS)}")
    if spec.dim != FACE_DIM:
        # Permanent, and it is a registry bug rather than a deployment one.
        return _inactive(
            cfg,
            f"model {spec.name} produces {spec.dim}-dim vectors but "
            f"search_faces is vector({FACE_DIM}). Changing the model is a "
            f"migration plus a re-index, not a setting — see index/faces.py.")

    det_path = detector_path or cfg.detector_weights
    rec_path = weights or cfg.recogniser_weights or spec.weights
    for path, what in ((det_path, "detector"), (rec_path, "embedder")):
        if not path or not os.path.isfile(path):
            return _inactive(cfg, f"{what} model file missing: {path or '<unset>'}")
    try:
        import cv2
        det = cv2.FaceDetectorYN.create(det_path, "", (320, 320),
                                        score_threshold=float(cfg.score_threshold),
                                        nms_threshold=0.3, top_k=20)
        rec = cv2.FaceRecognizerSF.create(rec_path, "")
    except Exception as exc:                                      # noqa: BLE001
        return _inactive(cfg, f"face models failed to load: {exc}")

    embedder = SFaceEmbedder(rec, spec, rec_path)
    log.info("face encoder ready (model=%s dim=%d score>=%.2f min_width=%dpx)",
             embedder.name, embedder.dim, cfg.score_threshold, cfg.min_width_px)
    return FaceEncoder(det, embedder, score_threshold=cfg.score_threshold,
                       min_width_px=cfg.min_width_px)
