"""detector.py — find people and vehicles in a frame.

THE INTERFACE IS THE POINT. Everything downstream depends on `Detection` and
`Detector.detect`, and on nothing else.

Ultralytics YOLO is **AGPL-3.0**. That is compatible with this repository today
because Smart Search ships in tier 1, which is AGPL-3.0 itself. It would NOT be
compatible if this feature ever moved behind a paid tier: AGPL bars that code
from a commercial tier permanently. AGPL is a one-way door, so the door is kept
narrow: swapping to an Apache-2.0 detector (RT-DETR, YOLOX, NanoDet) means
writing one more class in this file and changing one line of configuration.
Nothing else imports ultralytics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

import numpy as np

from .backends import (BY_CONFIG, BY_FALLBACK, BY_INCUMBENT, BY_PREFERENCE,
                       OPENVINO_IR,
                       RT_OPENVINO, RT_ULTRALYTICS, BackendSpec, torch_spec)
from .hardware import resolve_device

log = logging.getLogger("analytics.detector")

TORCH = "torch"
OPENVINO = "openvino"


def _openvino_version() -> str | None:
    try:
        import openvino
        return openvino.__version__
    except Exception:                                              # noqa: BLE001
        return None

# COCO ids. Kept here rather than in config: they are a property of the model
# family, not a deployment choice.
_PERSON = 0
_VEHICLE = (2, 3, 5, 7)          # car, motorcycle, bus, truck

PERSON_DOMAIN = "person"
VEHICLE_DOMAIN = "vehicles"      # plural, to match the wire contract


@dataclass(frozen=True)
class Detection:
    domain: str                  # PERSON_DOMAIN | VEHICLE_DOMAIN
    xyxy: tuple[int, int, int, int]
    confidence: float
    #: Model's own class label, kept for the /detections `type` field.
    label: str


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> Sequence[Detection]: ...

    @property
    def backend(self) -> BackendSpec:
        """What this detector is actually running on, for /health.

        Part of the interface rather than an attribute of one implementation:
        the whole point of naming a backend is that an operator can read it
        without knowing which class was constructed.
        """
        ...


class UltralyticsDetector:
    """YOLO via ultralytics. The only module in this service that imports it."""

    def __init__(self, weights: str = "yolov8n.pt", confidence: float = 0.35,
                 device: str | None = None, square_letterbox: bool = True,
                 backend: str = TORCH, selected_by: str = BY_INCUMBENT) -> None:
        from ultralytics import YOLO      # imported late: keeps the seam honest

        self._model = YOLO(weights)
        self._conf = confidence
        # SQUARE LETTERBOX SO A BACKEND SWAP IS NOT A BEHAVIOUR CHANGE. An
        # exported artifact has a fixed square input and pads to it; the torch
        # path pads only to a stride multiple. The two see different images and
        # find different objects — measured, see models.square_letterbox in
        # index/config.py. Exported models ignore this flag, because their input
        # shape already forces the answer.
        self._rect = not square_letterbox
        # RESOLVED HERE RATHER THAN DEFERRED TO ULTRALYTICS. Passing None let
        # ultralytics pick, which gave the right answer and made it
        # unreportable: /health could not say whether inference was on CPU or
        # GPU and the only record was one line in the boot log. The choice is
        # identical — CUDA when torch can see it, CPU otherwise — but now it has
        # a name that can be stored in a profile and shown to an operator.
        self._device = resolve_device(device)
        self._classes = [_PERSON, *_VEHICLE]
        # THE REPRESENTATION IS NOT THE RUNTIME. Both paths go through
        # ultralytics — that is what keeps preprocessing, the class filter and
        # box decoding identical between them — but the weights being executed
        # are a torch checkpoint in one case and an OpenVINO IR in the other,
        # and /health has to be able to say which.
        if backend == OPENVINO:
            self._backend = BackendSpec(
                component="detector", representation=OPENVINO_IR,
                runtime=RT_OPENVINO, device=self._device,
                selected_by=selected_by, artifact=weights,
                runtime_version=_openvino_version(),
            )
        else:
            self._backend = torch_spec(
                "detector", self._device, artifact=weights,
                runtime=RT_ULTRALYTICS, selected_by=selected_by,
            )
        log.info("detector loaded backend=%s weights=%s device=%s conf=%.2f",
                 backend, weights, self._device, confidence)

    @property
    def backend(self) -> BackendSpec:
        return self._backend

    def detect(self, frame: np.ndarray) -> list[Detection]:
        r = self._model(
            frame, classes=self._classes, conf=self._conf,
            verbose=False, device=self._device, rect=self._rect,
        )[0]
        if r.boxes is None or len(r.boxes) == 0:
            return []
        names = r.names
        out: list[Detection] = []
        for box, cls, conf in zip(
            r.boxes.xyxy.cpu().numpy(),
            r.boxes.cls.cpu().numpy().astype(int),
            r.boxes.conf.cpu().numpy(),
        ):
            x1, y1, x2, y2 = (int(v) for v in box)
            out.append(Detection(
                domain=PERSON_DOMAIN if cls == _PERSON else VEHICLE_DOMAIN,
                xyxy=(x1, y1, x2, y2),
                confidence=float(conf),
                label=str(names.get(int(cls), int(cls))),
            ))
        return out


def _smoke_test(detector: "UltralyticsDetector") -> None:
    """Run one inference, because CONSTRUCTING IS NOT EVIDENCE OF WORKING.

    Ultralytics defers backend construction to the first predict() call:
    `YOLO(path)` succeeds on a path it cannot actually execute, and the
    TypeError arrives later, per frame, inside the pipeline's own exception
    handler — where a fallback cannot see it.

    That is not hypothetical. A malformed OpenVINO artifact path was accepted
    at load on 2026-09-04, reported by /health as `openvino ready`, and then
    failed on every frame: 18,508 frames processed, 8,603 detector calls, ZERO
    detections and zero rows written, for hours, with no failure recorded
    because nothing had failed at load time.

    One inference on a tiny black frame costs a few milliseconds once per
    warm-up and converts that silent outage into an ordinary fallback.
    """
    probe = np.zeros((64, 64, 3), dtype=np.uint8)
    detector.detect(probe)


def build_selected(selector, weights: str, confidence: float,
                   device: Optional[str], square_letterbox: bool = True) -> Detector:
    """Load the best detector this machine will actually give us.

    Walks the selector's plans in order — preferred first, torch last — and
    returns the first that loads. THE LAST PLAN IS ALWAYS TORCH, so the only
    way this raises is if the incumbent itself is broken, which is a real fault
    rather than a failed optimisation.

    Falling through is deliberately silent to the caller: ingest does not care
    which implementation it got, and a site must not stop indexing because an
    optimisation it never asked for could not be built.
    """
    plans = selector.plans(weights)
    last: Optional[Exception] = None
    for plan in plans:
        try:
            det = UltralyticsDetector(
                plan.artifact, confidence, device, square_letterbox,
                backend=plan.backend,
                # Never BY_CALIBRATION: no benchmark ran on this
                # machine, and saying so would invent evidence.
                selected_by=(BY_FALLBACK if plan.is_fallback
                             else BY_CONFIG if plan.is_pinned
                             else BY_PREFERENCE),
            )
            _smoke_test(det)
            selector.record_success(plan.backend)
            return det
        except Exception as exc:                                   # noqa: BLE001
            last = exc
            # Only a non-final plan counts as a failed OPTIMISATION. The final
            # plan failing is the incumbent failing, which is not something to
            # retry-and-give-up on — it is reported to the pool as a fault.
            if plan is not plans[-1]:
                selector.record_failure(plan.backend, f"{type(exc).__name__}: {exc}")
    raise RuntimeError(
        f"no detector implementation could be loaded from {weights}: {last}"
    )


def build(name: str, weights: str, confidence: float, device: Optional[str],
          square_letterbox: bool = True) -> Detector:
    if name == "ultralytics":
        return UltralyticsDetector(weights, confidence, device, square_letterbox)
    # An unknown name is a configuration error, and failing loudly at boot beats
    # a service that runs with no detector and indexes nothing.
    raise ValueError(
        f"unknown detector '{name}'. Implement it in index/detector.py — the "
        "interface is Detector.detect(frame) -> Sequence[Detection]."
    )
