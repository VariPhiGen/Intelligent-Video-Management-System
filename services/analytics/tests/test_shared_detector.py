"""The shared OpenVINO detector must see the same image and find the same boxes.

Replacing the runtime is only safe if nothing downstream can tell. These run
against the REAL exported artifact when one is present, comparing the shared
path with the ultralytics path frame by frame; without an artifact they skip
rather than pretend.

Run (from services/analytics): python -m pytest tests/test_shared_detector.py -q
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARTIFACTS = sorted(glob.glob("/models/artifacts/detector/*/yolov8n_openvino_model"))
try:
    import openvino  # noqa: F401
    HAVE_OPENVINO = True
except Exception:                                                  # noqa: BLE001
    HAVE_OPENVINO = False
# Both are needed, and a developer machine usually has neither: the runtime
# lives in the service image, and the artifact is exported into the models
# volume on first load.
ARTIFACT = ARTIFACTS[0] if ARTIFACTS else None
# And a CPU that can run it. Apple M4/M5 under Docker cannot (SIGILL in the
# ARM plugin — see test_detector_probe.py); running these in process there
# kills the whole pytest run instead of failing one test.
CRASH = None
if ARTIFACT and HAVE_OPENVINO:
    from analytics.detector import _probe_child, probe_isolated
    CRASH = probe_isolated(_probe_child, (True, ARTIFACT, 0.35, "cpu", 0, 2))
pytestmark = [
    pytest.mark.skipif(not (ARTIFACTS and HAVE_OPENVINO),
                       reason="needs the OpenVINO runtime and an exported artifact (run in the image)"),
    pytest.mark.skipif(CRASH is not None, reason=f"OpenVINO cannot run on this CPU: {CRASH}"),
]
CONF = 0.35


def frames():
    """Recorded frames when this machine has some, and synthetic ones always.

    The synthetic frames carry the awkward shapes - not square, tiny, odd
    aspect - because that is where letterboxing and box rescaling disagree if
    they are going to disagree at all.
    """
    out = []
    for path in sorted(glob.glob("/w/*.ts"))[:1]:
        import cv2
        cap = cv2.VideoCapture(path)
        for i in range(400):
            if not cap.grab():
                break
            if i % 80 == 0:
                ok, f = cap.retrieve()
                if ok and f is not None:
                    out.append(f)
        cap.release()
    rng = np.random.default_rng(11)
    for shape in ((1080, 1920, 3), (480, 640, 3), (576, 704, 3), (64, 64, 3), (120, 800, 3)):
        out.append(rng.integers(0, 255, shape, dtype=np.uint8))
    out.append(np.zeros((64, 64, 3), dtype=np.uint8))
    return out


@pytest.fixture(scope="module")
def pair():
    from analytics.detector import UltralyticsDetector
    from analytics.openvino_detector import SharedOpenVinoDetector
    shared = SharedOpenVinoDetector(ARTIFACT, CONF, device="cpu")
    incumbent = UltralyticsDetector(ARTIFACT, CONF, device="cpu", backend="openvino")
    return shared, incumbent


class TestEquivalence:
    def test_the_same_frames_give_the_same_detections(self, pair):
        shared, incumbent = pair
        checked = 0
        for frame in frames():
            a = sorted((d.domain, d.label, d.xyxy) for d in shared.detect(frame))
            b = sorted((d.domain, d.label, d.xyxy) for d in incumbent.detect(frame))
            assert a == b, f"shared path differs on a {frame.shape} frame"
            checked += 1
        assert checked >= 6

    def test_confidences_agree(self, pair):
        shared, incumbent = pair
        for frame in frames():
            a = sorted(round(d.confidence, 4) for d in shared.detect(frame))
            b = sorted(round(d.confidence, 4) for d in incumbent.detect(frame))
            assert a == pytest.approx(b, abs=1e-3)


class TestTheRuntime:
    def test_it_compiles_one_model_for_throughput_with_bounded_threads(self, pair):
        shared, _ = pair
        snap = shared.snapshot()
        assert snap["shared"] is True
        assert snap["threads"] == pytest.approx(max(2, (os.cpu_count() or 4) // 2))
        assert snap["streams"] >= 1 and snap["requests"] >= 1

    def test_it_reports_what_inference_costs(self, pair):
        shared, _ = pair
        shared.detect(np.zeros((480, 640, 3), dtype=np.uint8))
        snap = shared.snapshot()
        assert snap["calls"] >= 1
        assert snap["inference_p50_ms"] is not None and snap["inference_p50_ms"] > 0

    def test_it_survives_concurrent_callers(self, pair):
        # Both pipelines call this object from their own worker threads.
        import threading
        shared, _ = pair
        frame = frames()[0]
        errors, counts = [], []

        def work():
            try:
                for _ in range(4):
                    counts.append(len(shared.detect(frame)))
            except Exception as exc:                               # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors and len(counts) == 16
        assert len(set(counts)) == 1, "the same frame must give the same answer"

    def test_the_backend_still_reports_openvino(self, pair):
        shared, _ = pair
        assert shared.backend.runtime == "openvino"
        assert shared.backend.component == "detector"
