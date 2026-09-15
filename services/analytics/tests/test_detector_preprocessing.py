"""Detector preprocessing, where the detector now lives.

Moved from services/smartsearch when detection split out. The measurements in
these docstrings were taken before the move and still hold: what changed is
which image runs them.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.backends import (BY_INCUMBENT, ONNX, BackendSpec,  # noqa: E402,F401
                                torch_spec)

# ── preprocessing normalisation ──────────────────────────────────────────────
class _RecordingYOLO:
    """Captures the kwargs the detector passes to ultralytics."""
    names = {0: "person", 2: "car"}
    last_kwargs: dict = {}

    def __init__(self, *a, **kw):
        pass

    def __call__(self, frame, **kw):
        type(self).last_kwargs = kw
        class _R:
            boxes = None
            names = {0: "person"}
        return [_R()]


def _detector_with_fake_yolo(monkeypatch, **kw):
    import types
    fake = types.ModuleType("ultralytics")
    fake.YOLO = _RecordingYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", fake)
    from analytics.detector import UltralyticsDetector
    return UltralyticsDetector("w.pt", 0.35, "cpu", **kw)


def test_square_letterbox_disables_rectangular_inference(monkeypatch):
    """MEASURED 2026-09-04: an exported artifact has a fixed square input, the
    torch path pads only to a stride multiple, and the two find DIFFERENT
    objects — 4 detections against 5 on bus.jpg, 2 against 3 on a wide frame.
    Disabling rect makes torch reproduce the exports exactly, so a backend swap
    stops being a behaviour change."""
    import numpy as np
    det = _detector_with_fake_yolo(monkeypatch, square_letterbox=True)
    det.detect(np.zeros((100, 200, 3), dtype="uint8"))
    assert _RecordingYOLO.last_kwargs["rect"] is False


def test_rectangular_inference_is_still_reachable(monkeypatch):
    """The historic behaviour stays available: it is ~33% fewer pixels for a
    4:3 frame and produces fewer marginal detections, which a site that has
    tuned around it may want to keep."""
    import numpy as np
    det = _detector_with_fake_yolo(monkeypatch, square_letterbox=False)
    det.detect(np.zeros((100, 200, 3), dtype="uint8"))
    assert _RecordingYOLO.last_kwargs["rect"] is True


def test_the_class_filter_survives_the_normalisation(monkeypatch):
    """rect is added alongside the existing arguments, never in place of one."""
    import numpy as np
    det = _detector_with_fake_yolo(monkeypatch, square_letterbox=True)
    det.detect(np.zeros((100, 200, 3), dtype="uint8"))
    kw = _RecordingYOLO.last_kwargs
    assert kw["classes"] == [0, 2, 3, 5, 7]
    assert kw["conf"] == 0.35
    assert kw["device"] == "cpu"
