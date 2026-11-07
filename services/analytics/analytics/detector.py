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

import collections
import logging
import multiprocessing
import os
import signal
import statistics
import time
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

#: Ceiling on one crash-isolated probe. Importing the runtime and compiling the
#: model dominate (seconds); a probe that hangs is a backend that does not work.
PROBE_TIMEOUT_S = 180.0


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


class SwappableDetector:
    """The reference both pipelines keep, so the model behind it can change live.

    WHY A SWAP AND NOT A RESTART. When the camera set outgrows the model (see
    capacity.py), the alternative is tearing the pipeline down and rebuilding
    it: a gap in detection, every tracker reset, and every object in view
    re-announced as new. Rebinding one reference costs none of that. A call
    already in flight finishes on the old model — it holds its own reference —
    and the next frame runs on the new one.
    """

    #: Real calls timed for the live throughput estimate, and how many it
    #: needs before the estimate is trusted over the start-up measurement.
    WINDOW = 64
    MIN_SAMPLES = 16

    def __init__(self, inner) -> None:
        self._inner = inner
        self._durations: collections.deque = collections.deque(maxlen=self.WINDOW)

    @property
    def inner(self):
        return self._inner

    def swap(self, new):
        old, self._inner = self._inner, new
        self._durations.clear()          # those timings were the old model's
        return old

    def detect(self, frame):
        start = time.perf_counter()
        try:
            return self._inner.detect(frame)
        finally:
            self._durations.append(time.perf_counter() - start)

    def live_fps(self) -> Optional[float]:
        """Throughput from real calls, under whatever else this machine is doing.

        BETTER EVIDENCE THAN A BENCHMARK, which runs once at warm-up — on
        2026-10-27 that read 1.8 fps for yolov8s while Smart Search was still
        loading, against ~3 under steady load. Single-stream (1 / median), so
        with a shared OpenVINO request pool it understates: the safe direction.
        """
        d = list(self._durations)
        if len(d) < self.MIN_SAMPLES:
            return None
        return 1.0 / max(statistics.median(d), 1e-6)

    def __getattr__(self, name):
        # backend, snapshot(), ... — whatever the current model exposes.
        return getattr(self._inner, name)


def _probe_child(shared: bool, artifact: str, confidence: float,
                 device: Optional[str], threads: int, requests: int) -> None:
    """Runs in a fresh interpreter: build the detector exactly as
    build_selected would, then the same smoke test. Returning is passing."""
    if shared:
        from .openvino_detector import SharedOpenVinoDetector
        det = SharedOpenVinoDetector(artifact, confidence, device,
                                     num_requests=requests, threads=threads)
    else:
        det = UltralyticsDetector(artifact, confidence, device, backend=OPENVINO)
    _smoke_test(det)


def probe_isolated(target, args: tuple, timeout: float = PROBE_TIMEOUT_S) -> Optional[str]:
    """Why `target(*args)` would take down the process running it, or None.

    Only what an except clause cannot survive is reported: death by signal,
    and a hang. A child that merely RAISES returns None — the in-process
    attempt fails the same way as an ordinary exception, keeps its retry
    budget, and reports the real error text rather than "status 1".

    THE SMOKE TEST CANNOT CATCH A NATIVE CRASH. SIGILL from an instruction the
    CPU (or the VM it runs in) does not implement, or SIGSEGV from a broken
    plugin, kills the process executing it, and no except clause runs. In
    process, that process is the service: the container restarts, warms up,
    crashes again — 30 restarts in 30 minutes on an Apple M5, zero frames
    indexed, and /health "healthy" between deaths because the crash comes on
    the first inference, after the health check has passed. In a child it is
    just a return value, and the fallback works the way it was meant to.

    SPAWN, NOT FORK: a forked child would inherit the parent's runtime threads
    and locks mid-flight, which is its own way to hang.
    """
    proc = multiprocessing.get_context("spawn").Process(target=target, args=args, daemon=True)
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.kill()
        proc.join()
        return f"probe did not finish within {timeout:.0f}s"
    code = proc.exitcode
    if code is not None and code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        hint = (" — the CPU or its VM rejects an instruction the runtime uses"
                if -code == signal.SIGILL else "")
        return f"probe crashed with {name}{hint}"
    if code:
        log.info("detector probe raised in its child (status %s); the in-process "
                 "attempt will report the error", code)
    return None


def _cap_torch_threads(threads: int) -> None:
    """Half the machine for torch inference, the rule the OpenVINO path uses.

    Torch defaults to every core, and on an appliance that also decodes every
    camera that oversubscribes: measured 2026-10-27 under live load on an
    Apple M5, yolov8n ran 3.7 fps on 10 threads and 4.6 on 5. Process-wide.
    """
    try:
        import torch
        torch.set_num_threads(threads or max(2, (os.cpu_count() or 4) // 2))
    except Exception:                                              # noqa: BLE001
        pass


def build_selected(selector, weights: str, confidence: float,
                   device: Optional[str], square_letterbox: bool = True,
                   shared_openvino: bool = False, inference_threads: int = 0,
                   inference_requests: int = 2) -> Detector:
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
        if plan.backend == OPENVINO and plan is not plans[-1]:
            # Isolated first: an in-process failure here can be a dead service
            # rather than an exception (see probe_isolated).
            why = selector.isolation_verdict(plan.backend, plan.artifact, lambda: probe_isolated(
                _probe_child, (shared_openvino, plan.artifact, confidence, device,
                               inference_threads, inference_requests)))
            if why:
                selector.record_crash(plan.backend, why)
                continue
        try:
            if shared_openvino and plan.backend == OPENVINO:
                # One compiled model, THROUGHPUT, shared by both paths. Falls
                # through to the ultralytics runtime on any failure, so an
                # optimisation can never cost this service its detector.
                from .openvino_detector import SharedOpenVinoDetector
                try:
                    shared = SharedOpenVinoDetector(
                        plan.artifact, confidence, device,
                        num_requests=inference_requests, threads=inference_threads,
                        selected_by=(BY_FALLBACK if plan.is_fallback
                                     else BY_CONFIG if plan.is_pinned
                                     else BY_PREFERENCE),
                    )
                    _smoke_test(shared)
                    selector.record_success(plan.backend)
                    return shared
                except Exception as exc:                           # noqa: BLE001
                    log.warning("shared OpenVINO detector unavailable (%s: %s); "
                                "using the ultralytics runtime for this artifact",
                                type(exc).__name__, exc)
            if plan.backend == TORCH:
                _cap_torch_threads(inference_threads)
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
