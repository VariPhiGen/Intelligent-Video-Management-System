"""plates.py — read a number plate off a vehicle crop.

TWO STAGES, AND BOTH ARE REQUIRED. Recognition models are trained on tightly
cropped plates; handed a whole car they return an empty string. Measured
2026-08-31 against `cct-xs-v2-global-model`:

    synthetic plate crop  -> "H12DE143"  mean char prob 0.929
    random noise          -> ""          mean char prob 1.000
    flat grey             -> ""          mean char prob 0.993
    a person crop         -> ""          mean char prob 1.000

**Confidence does not separate plates from non-plates.** A non-plate scores
1.000, because the model is confidently predicting "no characters at all". The
usable signal is the EMPTINESS of the decoded string; confidence only grades how
well an actual plate was read. Anything that thresholds on confidence alone will
accept every piece of noise it is given.

So a localiser is not an optimisation here — without one this returns nothing,
and the service says so rather than pretending plate search works.

TWO LOCALISERS, AND THE SPLIT IS THE PRODUCT SHAPE (the Frigate model —
open baseline, paid accuracy):

  * The OPEN BASELINE: `open-image-models` (**MIT**, ONNX, same author as the
    recogniser), auto-downloaded on first use exactly like the recogniser — so
    plate reading WORKS on a first start, with both stages MIT and no
    operator-supplied file. Generic global training: it finds plates, but a
    region's formats (stacked Indian truck plates, say) deserve better.
  * The SITE-TUNED override: operator-supplied weights via
    `ANALYTICS_PLATE_WEIGHTS`, loaded through ultralytics (**AGPL-3.0** — fine
    while this ships in tier 1; see index/detector.py). This is where a
    region-trained model — including a commercial one — plugs in, and it wins
    over the baseline whenever set.

Keeping the reader MIT means the half that would be hardest to replace is also
the half with no licence entanglement.

Weights are never committed: both localisers and the recognition model download
to (or are mounted into) the models volume; git never carries a binary.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

import numpy as np
from PIL import Image

from .backends import (BY_INCUMBENT, RT_ULTRALYTICS, BackendSpec, ort_spec,
                       torch_spec)
from .hardware import resolve_device

log = logging.getLogger("analytics.plates")

#: A plate is letters and digits. Everything else — spaces, dashes, the state
#: badge some models emit — is noise for a trigram match, and normalising at
#: WRITE time means the query side never has to guess which variant was stored.
_KEEP = re.compile(r"[^A-Z0-9]")


@dataclass(frozen=True)
class PlateRead:
    text: str
    confidence: float


def normalise(raw: str) -> str:
    return _KEEP.sub("", (raw or "").upper())


class PlateLocaliser(Protocol):
    def locate(self, crop: np.ndarray) -> Sequence[tuple[int, int, int, int]]: ...

    @property
    def backend(self) -> BackendSpec: ...


class PlateRecogniser(Protocol):
    """Characters from a tightly-cropped plate region.

    THE CANDIDATE AXIS HERE IS WIDER THAN IT LOOKS. Probed against
    fast_plate_ocr 1.1.0, the constructor takes not only
    `device: Literal['cuda','cpu','auto']` but also `providers` (a full ONNX
    Runtime provider sequence, tuples with options included) and `sess_options`.
    So execution-provider selection is genuinely available — the limit is which
    providers the installed onnxruntime BUILD carries, not what this wrapper
    will accept. On the CPU image that was CPUExecutionProvider and
    AzureExecutionProvider only.

    WHICH MAKES THE SESSION THE ONLY SOURCE OF TRUTH. Asking for a provider the
    build does not have is not an error: it warns and falls back, and the
    wrapper keeps reporting the request. See `ort_spec` in index/backends.py.

    `char_probs` is part of the contract, not an implementation detail: both
    `min_confidence` and the 0.80 two-line row gate read it, so a backend that
    changes its meaning changes plate acceptance without changing any threshold.
    """
    def run(self, image: np.ndarray, return_confidence: bool = True): ...

    @property
    def backend(self) -> BackendSpec: ...


class UltralyticsPlateLocaliser:
    """Finds plate regions inside a vehicle crop, using operator-supplied weights.

    CLASS-AWARE WHEN IT CAN BE. If the model names a class that looks like a
    plate, only that class is boxed; otherwise every class is treated as a plate,
    which is what a purpose-built single-class model wants.

    That distinction is not theoretical. The two ANPR models on the GPU box are
    `variphi_anpr.pt` (one class, `license_plate`) and `variphi_anpr_v2.pt`
    (`license_plate` AND `truck`). Class-agnostic handling of v2 would hand whole
    trucks to the recogniser, which decodes them as empty and confidently — see
    the measurements at the top of this file — so the cost would be silent and
    the symptom would be "ANPR does nothing".
    """

    def __init__(self, weights: str, confidence: float = 0.3,
                 device: str | None = None,
                 square_letterbox: bool = False) -> None:
        from ultralytics import YOLO

        self._model = YOLO(weights)
        self._conf = confidence
        # DEFAULTS TO RECTANGULAR, UNLIKE THE DETECTOR, AND NOT BY OVERSIGHT.
        # The detector's square normalisation was validated on real detections
        # before being switched on. Nothing equivalent exists for ANPR: the
        # two-line thresholds in this file were fitted to 109 real reads under
        # rectangular preprocessing, and square padding changes which plate
        # regions are boxed and how much margin they carry — margin being
        # exactly what the recogniser needs.
        #
        # An EXPORTED localiser is fixed-input and therefore square regardless.
        # That difference belongs in the candidate's accuracy profile, for the
        # ANPR parity gate to accept or reject, rather than being imposed on
        # production first so a benchmark compares more neatly.
        self._rect = not square_letterbox
        # Resolved for the same reason as the detector's: a device nobody can
        # name is a device nobody can check. See index/detector.py.
        self._device = resolve_device(device)
        self._backend = torch_spec(
            "plate_localiser", self._device, artifact=weights,
            runtime=RT_ULTRALYTICS, selected_by=BY_INCUMBENT,
        )
        names = getattr(self._model, "names", {}) or {}
        plate_ids = [i for i, n in names.items() if "plate" in str(n).lower()]
        # None means "no filter" — every class is a plate. A model with plate
        # classes AND others gets filtered to just the plates.
        self._classes = plate_ids if plate_ids and len(plate_ids) < len(names) else None
        log.info("plate localiser loaded weights=%s conf=%.2f classes=%s (of %s)",
                 weights, confidence,
                 [names[i] for i in self._classes] if self._classes else "all",
                 list(names.values()))

    @property
    def backend(self) -> BackendSpec:
        return self._backend

    def locate(self, crop: np.ndarray) -> list[tuple[int, int, int, int]]:
        r = self._model(crop, conf=self._conf, verbose=False,
                        device=self._device, classes=self._classes,
                        rect=self._rect)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return []
        out = []
        for b in r.boxes.xyxy.cpu().numpy():
            x1, y1, x2, y2 = (int(v) for v in b)
            if x2 - x1 >= 8 and y2 - y1 >= 4:
                out.append((x1, y1, x2, y2))
        return out


class OnnxPlateLocaliser:
    """The open-baseline localiser: an `open-image-models` plate detector.

    MIT + ONNX, auto-downloaded on first use like the recogniser — this is what
    makes plate reading work on a clean install with nothing supplied. The
    models are single-purpose plate detectors, so no class filtering is needed
    (contrast UltralyticsPlateLocaliser, which must guard against multi-class
    ANPR weights handing whole trucks to the recogniser).

    Confidence is filtered HERE rather than passed to the constructor, so this
    stays correct across open-image-models versions whatever kwargs its
    constructor grows or renames.
    """

    def __init__(self, model_name: str, confidence: float = 0.3) -> None:
        from open_image_models import LicensePlateDetector

        self._model = LicensePlateDetector(detection_model=model_name)
        self._conf = confidence
        # Named loudly because this is a DEFAULT: since the open baseline
        # shipped, an install that never configured plate weights reads and
        # stores plate strings — personal data — the first time this loads.
        # The opt-out is ANALYTICS_PLATE_MODEL= (explicitly empty), which the
        # log line below repeats at every startup.
        log.info("plate reading ACTIVE via the open-baseline detector "
                 "(model=%s conf=%.2f, default since the localiser shipped; "
                 "set ANALYTICS_PLATE_MODEL= empty to disable)",
                 model_name, confidence)

    def locate(self, crop: np.ndarray) -> list[tuple[int, int, int, int]]:
        out = []
        for det in self._model.predict(crop) or []:
            if float(getattr(det, "confidence", 0.0)) < self._conf:
                continue
            bb = det.bounding_box
            x1, y1, x2, y2 = (int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2))
            if x2 - x1 >= 8 and y2 - y1 >= 4:
                out.append((x1, y1, x2, y2))
        return out


class PlateReader:
    def __init__(self, model_name: str, device: str | None,
                 localiser: Optional[PlateLocaliser],
                 min_confidence: float = 0.6, min_length: int = 4,
                 region_padding: float = 0.12,
                 two_line_max_aspect: float = 2.2,
                 two_line_min_row_confidence: float = 0.80) -> None:
        from fast_plate_ocr import LicensePlateRecognizer

        # "auto" IS PASSED THROUGH RATHER THAN RESOLVED. The detector and the
        # encoder are torch, where our resolution and theirs agree exactly.
        # This is a third-party wrapper whose accepted device values are its
        # own — `Literal['cuda','cpu','auto']` in 1.1.0 — so handing it a
        # torch-style "cuda:0" would be inventing an API.
        requested = device or "auto"
        self._ocr = LicensePlateRecognizer(model_name, device=requested)
        # AND THEN WE ASK THE SESSION WHAT IT ACTUALLY GOT. This wrapper accepts
        # device="cuda" on a machine with no CUDA build, warns once, and runs on
        # CPU while still reporting CUDA from its own `.providers` attribute.
        # Measured on 1.1.0: `.providers` said ['CUDAExecutionProvider'] and
        # `.model.get_providers()` said ['CPUExecutionProvider']. Reporting the
        # request would put a confident wrong answer in /health, which is worse
        # than the missing answer this reporting exists to replace.
        self._ocr_backend = ort_spec(
            "plate_ocr", getattr(self._ocr, "model", None),
            requested_device=requested, artifact=model_name,
            selected_by=BY_INCUMBENT,
        )
        if self._ocr_backend.downgraded:
            log.warning(
                "plate recogniser asked for device=%s and got %s (%s) — the "
                "runtime downgraded silently; check the onnxruntime build",
                requested, self._ocr_backend.device, self._ocr_backend.provider,
            )
        self._localiser = localiser
        self._min_conf = min_confidence
        self._min_len = min_length
        self._padding = region_padding
        self._two_line_aspect = two_line_max_aspect
        self._two_line_row_conf = two_line_min_row_confidence
        self.two_line_reads = 0
        self.reads_attempted = 0
        self.reads_accepted = 0
        self.reads_rejected_empty = 0
        self.reads_rejected_low = 0
        log.info("plate reader loaded model=%s localiser=%s min_conf=%.2f",
                 model_name, "yes" if localiser else "NONE", min_confidence)

    @property
    def active(self) -> bool:
        """False without a localiser. Recognition alone returns empty strings on
        whole-vehicle crops, so claiming plate search works would be a lie."""
        return self._localiser is not None

    @property
    def backends(self) -> dict[str, BackendSpec]:
        """Both stages, separately. They are independently selectable — the
        localiser is ultralytics and the recogniser is onnxruntime — so
        collapsing them into one line would hide a real per-model decision."""
        out = {"plate_ocr": self._ocr_backend}
        if self._localiser is not None:
            out["plate_localiser"] = self._localiser.backend
        return out

    def _recognise(self, region: np.ndarray) -> Optional[PlateRead]:
        self.reads_attempted += 1
        # The model's input is a fixed 128x64 RGB; give it exactly that rather
        # than relying on whatever resize path happens to be inside.
        img = np.array(Image.fromarray(region).convert("RGB").resize((128, 64)))
        pred = self._ocr.run(img, return_confidence=True)[0]
        text = normalise(pred.plate)
        if len(text) < self._min_len:
            # The real rejection. An empty or near-empty decode is what a
            # non-plate produces, and it produces it CONFIDENTLY.
            self.reads_rejected_empty += 1
            return None
        conf = float(np.mean(pred.char_probs)) if pred.char_probs is not None else 0.0
        if conf < self._min_conf:
            self.reads_rejected_low += 1
            return None
        self.reads_accepted += 1
        return PlateRead(text=text, confidence=conf)

    def _read_two_line(self, region: np.ndarray,
                       whole: Optional[PlateRead]) -> Optional[PlateRead]:
        """Read a STACKED plate by its rows, or return `whole` unchanged.

        `_recognise` resizes every region to a fixed 128x64 — a 2:1 single-line
        shape — and the model behind it reads one line. Hand it a stacked plate
        (~1.4:1, two rows) and it reads straight across both and drops a
        character, CONFIDENTLY: measured on `JH10AF7931`, the stacked plate read
        `JH10AF793` at 0.979-0.994 while the single-line plate on the same
        bumper read correctly at 0.924 and lost. Confidence is not comparable
        across region shapes, so the wrong answer wins on merit.

        Rows overlap slightly (0.55 / 0.45) because a clean split through the
        gap is not guaranteed — better to include a sliver of the other row than
        to shave the tops off characters, which is the failure being fixed.

        Three guards, because this OVERRIDES an answer the model already
        accepted and a bad override is worse than the truncation:
          * both rows must decode at all,
          * both must clear `two_line_min_row_confidence` — the one row that
            invented a character scored 0.679 against 0.96+ for good rows,
          * the join must be LONGER than the whole-region read. The defect is
            lost characters; a split that does not recover any has not
            demonstrated it read something the other pass missed.
        """
        h = region.shape[0]
        if h < 8:
            return whole
        top = self._recognise(region[: int(h * 0.55)])
        bottom = self._recognise(region[int(h * 0.45) :])
        if not top or not bottom:
            return whole
        if (top.confidence < self._two_line_row_conf
                or bottom.confidence < self._two_line_row_conf):
            return whole
        text = normalise(top.text + bottom.text)
        if whole is not None and len(text) <= len(whole.text):
            return whole
        if len(text) < self._min_len:
            return whole
        # Length-weighted mean, so plate_confidence keeps meaning "mean
        # character probability" rather than becoming a mean of two means.
        n_t, n_b = len(top.text), len(bottom.text)
        conf = (top.confidence * n_t + bottom.confidence * n_b) / max(1, n_t + n_b)
        self.two_line_reads += 1
        return PlateRead(text=text, confidence=conf)

    def read(self, vehicle_crop: np.ndarray) -> Optional[PlateRead]:
        """Best plate found in this vehicle crop, or None."""
        if self._localiser is None:
            return None
        best: Optional[PlateRead] = None
        h, w = vehicle_crop.shape[:2]
        for (x1, y1, x2, y2) in self._localiser.locate(vehicle_crop):
            # PAD THE BOX — the recogniser needs whitespace around the
            # characters. Measured 2026-08-31: the same three plates read as
            # "L8CAF503" / "AO5MJ456" / "H12DE143" from crops that hugged the
            # characters, and EXACTLY — DL8CAF5030, KA05MJ4567, MH12DE1433 —
            # from renders with real margin. Losing the first and last character
            # is the difference between a plate that matches and one that never
            # will.
            #
            # PADDING IS NOT A CURE. It can only include pixels that exist: a
            # crop already flush to the plate edge stayed truncated even at 12%,
            # because there was nothing outside it to add. The real requirement
            # is that the localiser boxes generously and the plate occupies
            # enough of the frame to have a border at all.
            px = int((x2 - x1) * self._padding)
            py = int((y2 - y1) * self._padding)
            region = vehicle_crop[
                max(0, y1 - py):min(h, y2 + py),
                max(0, x1 - px):min(w, x2 + px),
            ]
            if region.size == 0:
                continue
            got = self._recognise(region)
            # A near-square region is a stacked plate, not a wide one. On this
            # site that is the COMMON case: 93 of 109 measured regions fell
            # under the threshold and 69 clustered at ~1.5.
            bw, bh = (x2 - x1), (y2 - y1)
            if self._two_line_aspect > 0 and bh > 0 and bw / bh < self._two_line_aspect:
                got = self._read_two_line(region, got)
            if got and (best is None or got.confidence > best.confidence):
                best = got
        return best

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "reads_attempted": self.reads_attempted,
            "reads_accepted": self.reads_accepted,
            "rejected_no_characters": self.reads_rejected_empty,
            "rejected_low_confidence": self.reads_rejected_low,
            # How often a stacked plate was recovered by reading its rows. Worth
            # surfacing: if this is zero on a site whose plates are stacked, the
            # aspect threshold is wrong for that site's camera angles.
            "two_line_reads": self.two_line_reads,
        }


def build(cfg, device: str | None) -> Optional[PlateReader]:
    """None when plate reading is off or cannot be built. Never raises: a
    missing plate reader must cost plate search, not the whole ingest."""
    if not cfg.enabled:
        return None
    # Site-tuned weights beat the open baseline; the baseline beats nothing.
    # A weights load that FAILS does not fall back: the operator pointed at a
    # specific model, and silently substituting a generic one would make "my
    # custom model is misconfigured" look like "my custom model is mediocre".
    localiser: Optional[PlateLocaliser] = None
    if cfg.localiser_weights:
        try:
            localiser = UltralyticsPlateLocaliser(
                cfg.localiser_weights, cfg.localiser_confidence, device,
                cfg.localiser_square_letterbox,
            )
        except Exception as exc:                                   # noqa: BLE001
            log.error("plate localiser failed to load (%s) — plate reading "
                      "INACTIVE: %s", cfg.localiser_weights, exc)
            return None
    elif getattr(cfg, "localiser_model", ""):
        try:
            localiser = OnnxPlateLocaliser(cfg.localiser_model,
                                           cfg.localiser_confidence)
        except Exception as exc:                                   # noqa: BLE001
            log.error("open-baseline plate localiser failed to load (%s) — "
                      "plate reading INACTIVE: %s", cfg.localiser_model, exc)
            return None
    else:
        log.warning(
            "plate localisation disabled (localiser_weights and localiser_model "
            "both empty) — plate reading INACTIVE. Recognition alone returns "
            "empty strings on whole-vehicle crops, so vehicles will be indexed "
            "without plates."
        )
        return None
    try:
        return PlateReader(cfg.model, device, localiser,
                           cfg.min_confidence, cfg.min_length, cfg.region_padding,
                           cfg.two_line_max_aspect, cfg.two_line_min_row_confidence)
    except Exception as exc:                                       # noqa: BLE001
        log.error("plate recogniser failed to load — plate reading INACTIVE: %s", exc)
        return None
