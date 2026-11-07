"""openvino_detector.py — one compiled model, several inference requests, shared.

WHY THIS EXISTS. Both detection paths used to build their own
`UltralyticsDetector`, and ultralytics hardcodes `PERFORMANCE_HINT: LATENCY`
with no way to change it. That left two compiled models on one box, each taking
a full-width thread pool, each executing synchronously behind a lock inside the
compiled model. Intel's guidance for this shape is the opposite, and measuring
it here agreed:

    two models, LATENCY, auto threads   0.090 CPU-s/frame   p50 94 ms   2.24 cores
    ONE model, THROUGHPUT, 4 threads    0.059 CPU-s/frame   p50 54 ms   1.39 cores

(5 cameras x 5 FPS offered on an 8-core budget; both sustained the load.) So the
model is compiled once, with THROUGHPUT and a bounded thread count, and shared
by both paths.

CONCURRENCY IS A POOL OF REQUESTS, NOT ASYNC CALLBACKS. Each pipeline has one
worker thread, so at most two calls are ever in flight; a pool of inference
requests lets those run on separate streams while `detect()` stays an ordinary
blocking call, which is what every caller already expects. Ultralytics pins
LATENCY precisely because `AsyncInferQueue` can hang on some CPUs, and callbacks
would buy nothing over a pool this small.

THE ARITHMETIC IS ULTRALYTICS' OWN. Letterboxing, NMS and box rescaling are
imported from ultralytics rather than rewritten, so this path sees the same
image and decodes the same boxes as the path it replaces. Only the runtime
changed, and a test asserts the two agree box for box on real frames.
"""
from __future__ import annotations

import collections
import glob
import logging
import os
import queue
import threading
import time
from typing import Optional, Sequence

import numpy as np

from .backends import OPENVINO_IR, RT_OPENVINO, BackendSpec
from .detector import _PERSON, _VEHICLE, PERSON_DOMAIN, VEHICLE_DOMAIN, Detection
from .hardware import resolve_device

log = logging.getLogger("analytics.detector")

#: Ultralytics' predict defaults, restated so this path cannot drift from them.
IOU_THRESHOLD = 0.7
MAX_DETECTIONS = 300

#: How many recent calls the latency percentiles are taken over.
LATENCY_WINDOW = 200


def _nms():
    """Ultralytics' NMS, wherever this version keeps it.

    It moved from `utils.ops` to `utils.nms` inside the 8.x line and this repo
    pins `ultralytics>=8.3,<9`, so both layouts have to be acceptable — the
    alternative is a detector that stops decoding boxes on a patch upgrade.
    """
    try:
        from ultralytics.utils import nms
        return nms.non_max_suppression
    except (ImportError, AttributeError):                          # pragma: no cover
        from ultralytics.utils.ops import non_max_suppression
        return non_max_suppression


def auto_threads(cores: Optional[int] = None) -> int:
    """Half the machine, floor two.

    Decoding five 1080p streams is the other half of this box's work, and the
    measured cost per frame stops improving once one inference has more than a
    few threads: 8 threads cost 0.090 CPU-s per frame against 0.059 for four.
    Leaving headroom is both cheaper and faster.
    """
    n = cores or os.cpu_count() or 4
    return max(2, n // 2)


class SharedOpenVinoDetector:
    """The detector both pipelines use. Thread-safe by request pool."""

    def __init__(self, artifact: str, confidence: float = 0.35,
                 device: Optional[str] = None, num_requests: int = 2,
                 threads: int = 0, streams: int = 0,
                 selected_by: str = "preference") -> None:
        import openvino as ov
        from ultralytics.data.augment import LetterBox

        self._conf = float(confidence)
        self._classes = [_PERSON, *_VEHICLE]
        self._device = resolve_device(device)
        xml = self._find_xml(artifact)
        self._names, meta_imgsz = self._read_metadata(xml)

        core = ov.Core()
        model = core.read_model(xml)
        threads = threads or auto_threads()
        config = {
            "INFERENCE_NUM_THREADS": int(threads),
            # Several workloads share this box (two pipelines, five decoders),
            # which is exactly the case Intel recommends pinning off for.
            "ENABLE_CPU_PINNING": False,
        }
        if streams > 0:
            # EXPLICIT STREAMS, NO HINT. With two callers, the THROUGHPUT hint
            # split the thread budget into four streams and nothing used the
            # spare two: measured 25.8 ms p50 against 17.1 ms for the path this
            # replaces, at the same throughput. Two wider streams keep the CPU
            # saving AND the latency. Intel's own guidance is to set the hint to
            # none when choosing streams by hand.
            config["NUM_STREAMS"] = int(streams)
        else:
            config["PERFORMANCE_HINT"] = "THROUGHPUT"
            # How many calls can be in flight: one worker thread per pipeline.
            config["PERFORMANCE_HINT_NUM_REQUESTS"] = max(1, int(num_requests))
        self._compiled = core.compile_model(model, "CPU", config)
        self._shape = tuple(self._compiled.inputs[0].shape)          # 1,3,H,W
        self._letterbox = LetterBox((self._shape[2], self._shape[3]), auto=False)

        pool_size = max(1, int(self._compiled.get_property(
            "OPTIMAL_NUMBER_OF_INFER_REQUESTS")))
        self._pool: queue.Queue = queue.Queue()
        for _ in range(pool_size):
            self._pool.put(self._compiled.create_infer_request())
        self._pool_size = pool_size

        self._streams = self._compiled.get_property("NUM_STREAMS")
        self._threads = self._compiled.get_property("INFERENCE_NUM_THREADS")
        self._stats_lock = threading.Lock()
        self._durations: collections.deque = collections.deque(maxlen=LATENCY_WINDOW)
        self.calls = 0
        self.waits = 0
        self._backend = BackendSpec(
            component="detector", representation=OPENVINO_IR, runtime=RT_OPENVINO,
            device=self._device, selected_by=selected_by, artifact=artifact,
            runtime_version=ov.__version__,
        )
        log.info("detector loaded backend=openvino(shared) artifact=%s input=%s "
                 "threads=%s streams=%s requests=%d conf=%.2f",
                 artifact, self._shape, self._threads, self._streams, pool_size,
                 self._conf)
        if meta_imgsz and int(meta_imgsz[-1]) != int(self._shape[3]):
            log.warning("artifact metadata says imgsz=%s but the model input is %s",
                        meta_imgsz, self._shape)

    # ── loading helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _find_xml(artifact: str) -> str:
        if artifact.endswith(".xml"):
            return artifact
        found = sorted(glob.glob(os.path.join(artifact, "*.xml")))
        if not found:
            raise FileNotFoundError(f"no OpenVINO IR (.xml) under {artifact}")
        return found[0]

    @staticmethod
    def _read_metadata(xml: str) -> tuple[dict, Optional[Sequence[int]]]:
        """Class names and the exported input size, or empty when unreadable.

        The names are cosmetic (the `type` field on a stored detection); the
        class IDS decide the domain and those are a property of the model
        family, so a missing metadata file is not fatal.
        """
        try:
            import yaml
            meta = yaml.safe_load(
                open(os.path.join(os.path.dirname(xml), "metadata.yaml"),
                     encoding="utf-8")) or {}
            names = {int(k): str(v) for k, v in (meta.get("names") or {}).items()}
            imgsz = meta.get("imgsz")
            return names, (imgsz if isinstance(imgsz, (list, tuple)) else None)
        except Exception:                                          # noqa: BLE001
            return {}, None

    # ── the interface ───────────────────────────────────────────────────────
    @property
    def backend(self) -> BackendSpec:
        return self._backend

    def detect(self, frame: np.ndarray) -> list[Detection]:
        import torch
        from ultralytics.utils import ops
        non_max_suppression = _nms()

        padded = self._letterbox(image=frame)
        x = padded[..., ::-1].transpose(2, 0, 1)[None]            # BGR->RGB, NCHW
        x = np.ascontiguousarray(x, dtype=np.float32) / 255.0

        waited = False
        try:
            request = self._pool.get_nowait()
        except queue.Empty:
            waited = True                    # every stream busy: two callers, one free
            request = self._pool.get()
        started = time.perf_counter()
        try:
            request.infer({0: x})
            # Copied before the request goes back: the next caller will reuse
            # its output buffer.
            raw = np.array(request.get_output_tensor(0).data, copy=True)
        finally:
            self._pool.put(request)
        elapsed = time.perf_counter() - started
        with self._stats_lock:
            self._durations.append(elapsed)
            self.calls += 1
            if waited:
                self.waits += 1

        # Positional, and nc=0, exactly as the detect predictor calls it.
        kept = non_max_suppression(
            torch.from_numpy(raw), self._conf, IOU_THRESHOLD, self._classes, False,
            max_det=MAX_DETECTIONS, nc=0,
        )[0]
        if kept is None or not len(kept):
            return []
        # No rounding: the path this replaces truncates the float box, and a
        # box that moves by a pixel moves objects across zone edges.
        boxes = ops.scale_boxes(x.shape[2:], kept[:, :4].clone(), frame.shape)
        out: list[Detection] = []
        for (x1, y1, x2, y2), conf, cls in zip(boxes.tolist(), kept[:, 4].tolist(),
                                               kept[:, 5].tolist()):
            cid = int(cls)
            out.append(Detection(
                domain=PERSON_DOMAIN if cid == _PERSON else VEHICLE_DOMAIN,
                xyxy=(int(x1), int(y1), int(x2), int(y2)),
                confidence=float(conf),
                label=self._names.get(cid, str(cid)),
            ))
        return out

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._stats_lock:
            ordered = sorted(self._durations)
            calls, waits = self.calls, self.waits

        def pct(q: float):
            if not ordered:
                return None
            return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000, 1)

        return {"shared": True, "streams": self._streams, "threads": self._threads,
                "requests": self._pool_size, "calls": calls,
                "waited_for_a_request": waits,
                "inference_p50_ms": pct(0.5), "inference_p95_ms": pct(0.95)}
