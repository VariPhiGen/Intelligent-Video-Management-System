"""Detector backend selection: prefer OpenVINO on CPU, fall back without cost.

THE PROPERTY WORTH PROTECTING is not "OpenVINO gets chosen". It is that
choosing it can never leave the service without a detector. A site indexes
nothing if the detector fails to load, so an optimisation that can fail must
fail into the incumbent, in the same call, every time.

The measurements behind the preference, live 1920x1080 camera frames, 15 timed
rounds after a discarded warm-up:

    torch      p50 40.8ms   p95 62.0ms
    openvino   p50 17.6ms   p95 21.8ms
    onnx       p50 89.4ms   p95 215.9ms   <- not offered; slower than incumbent

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import selection                                       # noqa: E402
from analytics.artifacts import Artifact                              # noqa: E402
from analytics.config import ModelConfig                              # noqa: E402
from analytics.hardware import RuntimeInfo                            # noqa: E402
from analytics.selection import OPENVINO, TORCH, DetectorSelector     # noqa: E402


def _env(monkeypatch, *, device="cpu", openvino_available=True, export_ok=True,
         export_error="export blew up"):
    monkeypatch.setattr(selection, "resolve_device", lambda _p: device)
    monkeypatch.setattr(selection, "runtimes", lambda: {
        OPENVINO: RuntimeInfo(OPENVINO, openvino_available, "2026.3.1"),
        TORCH: RuntimeInfo(TORCH, True, "2.14.0+cpu"),
    })
    monkeypatch.setattr(selection.artifacts, "export", lambda *a, **kw: Artifact(
        ok=export_ok, component="detector", export_format="openvino",
        source="yolov8n.pt",
        path="/models/artifacts/detector/k/yolov8n_openvino_model" if export_ok else None,
        export_seconds=2.1, size_bytes=12_900_000, names={"0": "person"},
        error=None if export_ok else export_error,
    ))


def _cfg(**kw):
    c = ModelConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── the preference ───────────────────────────────────────────────────────────
def test_cpu_prefers_openvino(monkeypatch):
    _env(monkeypatch, device="cpu")
    plans = DetectorSelector(_cfg()).plans("yolov8n.pt")
    assert plans[0].backend == OPENVINO
    assert plans[0].artifact.endswith("_openvino_model")
    assert "2.3x" in plans[0].reason


def test_cuda_prefers_torch(monkeypatch):
    """The OpenVINO build in this image reports a CPU device only, and the
    torch/cuda path is already far past what the workload needs."""
    _env(monkeypatch, device="cuda")
    plans = DetectorSelector(_cfg()).plans("yolov8n.pt")
    assert [p.backend for p in plans] == [TORCH]
    assert "CPU-only" in plans[0].reason


def test_openvino_absent_falls_to_torch(monkeypatch):
    _env(monkeypatch, openvino_available=False)
    plans = DetectorSelector(_cfg()).plans("yolov8n.pt")
    assert [p.backend for p in plans] == [TORCH]
    assert "not installed" in plans[0].reason


def test_configuration_can_pin_either_backend(monkeypatch):
    _env(monkeypatch, device="cuda")          # would otherwise choose torch
    plans = DetectorSelector(_cfg(detector_backend="openvino")).plans("w.pt")
    assert plans[0].backend == OPENVINO
    _env(monkeypatch, device="cpu")           # would otherwise choose openvino
    plans = DetectorSelector(_cfg(detector_backend="torch")).plans("w.pt")
    assert [p.backend for p in plans] == [TORCH]


def test_an_unknown_backend_name_does_not_silently_downgrade(monkeypatch):
    """A typo in configuration must not quietly put a site on the slow path,
    and must not take the service down either."""
    _env(monkeypatch, device="cpu")
    plans = DetectorSelector(_cfg(detector_backend="openvimo")).plans("w.pt")
    assert plans[0].backend == OPENVINO          # fell back to auto, not torch


# ── falling back must be free ────────────────────────────────────────────────
def test_torch_is_always_the_last_plan(monkeypatch):
    """The only implementation that needs no export, no extra runtime and no
    artifact cache. A detector that fails to load is a service that indexes
    nothing, so it has to be the floor."""
    _env(monkeypatch, device="cpu")
    plans = DetectorSelector(_cfg()).plans("yolov8n.pt")
    assert plans[-1].backend == TORCH
    assert plans[-1].is_fallback is True


def test_an_export_failure_is_an_attempt_and_yields_torch(monkeypatch):
    """A build that cannot be produced is one of the ways the preferred backend
    does not work here, and repeating it every warm-up wastes minutes."""
    _env(monkeypatch, export_ok=False, export_error="no space left on device")
    sel = DetectorSelector(_cfg())
    plans = sel.plans("yolov8n.pt")
    assert [p.backend for p in plans] == [TORCH]
    snap = sel.snapshot()
    assert snap["attempts_used"] == 1
    assert "no space left on device" in snap["failures"][0]


def test_preferred_backend_is_retried_until_the_budget_runs_out(monkeypatch):
    _env(monkeypatch, device="cpu")
    sel = DetectorSelector(_cfg(backend_attempts=3))
    for i in range(3):
        assert sel.plans("yolov8n.pt")[0].backend == OPENVINO, f"gave up at {i}"
        sel.record_failure(OPENVINO, "libopenvino.so: cannot open shared object")
    # Budget spent: stop paying an export and a failed load on every warm-up.
    plans = sel.plans("yolov8n.pt")
    assert [p.backend for p in plans] == [TORCH]
    assert "not retried" in plans[0].reason
    assert sel.snapshot()["exhausted"] is True


def test_zero_attempts_means_never_try_the_optimisation(monkeypatch):
    _env(monkeypatch, device="cpu")
    sel = DetectorSelector(_cfg(backend_attempts=0))
    assert [p.backend for p in sel.plans("w.pt")] == [TORCH]


def test_success_does_not_refund_the_budget(monkeypatch):
    """A backend that loads intermittently is not a backend that works.
    Zeroing the counter on success would let one that fails every other
    warm-up retry for ever."""
    _env(monkeypatch, device="cpu")
    sel = DetectorSelector(_cfg(backend_attempts=2))
    sel.record_failure(OPENVINO, "transient")
    sel.record_success(OPENVINO)
    assert sel.snapshot()["attempts_used"] == 1
    sel.record_failure(OPENVINO, "again")
    assert sel.snapshot()["exhausted"] is True


# ── what /health is told ─────────────────────────────────────────────────────
def test_snapshot_explains_why_not_just_what(monkeypatch):
    """A site on torch because OpenVINO would not build looks identical to one
    that never tried, unless the reasons are reported."""
    _env(monkeypatch, device="cpu")
    sel = DetectorSelector(_cfg(backend_attempts=1))
    sel.record_failure(OPENVINO, "Illegal instruction (no AVX2)")
    snap = sel.snapshot()
    assert snap["configured"] == "auto"
    assert snap["preferred"] == OPENVINO
    assert snap["exhausted"] is True
    assert "Illegal instruction" in snap["failures"][0]


def test_snapshot_is_json_shaped(monkeypatch):
    import json
    _env(monkeypatch, device="cpu")
    sel = DetectorSelector(_cfg())
    sel.plans("yolov8n.pt")
    json.dumps(sel.snapshot())


# ── the fallback path end to end ─────────────────────────────────────────────
def test_build_selected_falls_through_to_torch_without_raising(monkeypatch):
    """The whole point: a failed optimisation costs nothing but a log line."""
    from analytics import detector as det_mod

    _env(monkeypatch, device="cpu")
    built: list[str] = []

    class _Fake:
        def __init__(self, artifact, conf, device, square, backend=TORCH,
                     selected_by="incumbent"):
            built.append(backend)
            if backend == OPENVINO:
                raise RuntimeError("libopenvino.so: cannot open shared object file")

        def detect(self, frame):     # reached by the load-time smoke test
            return []

    monkeypatch.setattr(det_mod, "UltralyticsDetector", _Fake)
    sel = DetectorSelector(_cfg())
    got = det_mod.build_selected(sel, "yolov8n.pt", 0.35, "cpu")
    assert isinstance(got, _Fake)
    assert built == [OPENVINO, TORCH], "did not try the preferred backend first"
    assert sel.snapshot()["attempts_used"] == 1


def test_build_selected_raises_only_when_the_incumbent_itself_fails(monkeypatch):
    """Torch failing is a real fault, not a failed optimisation — the pool must
    see it, record it and retry with backoff rather than swallow it."""
    from analytics import detector as det_mod

    _env(monkeypatch, device="cpu")

    class _AllBroken:
        def __init__(self, *a, **kw):
            raise RuntimeError("weights are corrupt")

        def detect(self, frame):
            return []

    monkeypatch.setattr(det_mod, "UltralyticsDetector", _AllBroken)
    sel = DetectorSelector(_cfg())
    with pytest.raises(RuntimeError, match="no detector implementation"):
        det_mod.build_selected(sel, "yolov8n.pt", 0.35, "cpu")
    # The final plan failing is NOT counted as a spent optimisation attempt.
    assert sel.snapshot()["attempts_used"] == 1


# ── constructing is not evidence of working ──────────────────────────────────
def test_a_backend_that_loads_but_cannot_infer_falls_back(monkeypatch):
    """THE OUTAGE THIS PREVENTS. Ultralytics defers backend construction to the
    first predict(), so YOLO(bad_path) succeeds and the TypeError arrives per
    frame inside the pipeline's own handler, where the fallback cannot see it.

    Measured 2026-09-04 on the live service: 18,508 frames processed, 8,603
    detector calls, ZERO detections and zero rows written for hours, while
    /health reported `openvino ready` and attempts_used stayed at 0."""
    from analytics import detector as det_mod

    _env(monkeypatch, device="cpu")
    built: list[str] = []

    class _LazyFail:
        def __init__(self, artifact, conf, device, square, backend=TORCH,
                     selected_by="incumbent"):
            built.append(backend)
            self._backend_name = backend

        def detect(self, frame):
            if self._backend_name == OPENVINO:
                raise TypeError("not a supported model format")
            return []

    monkeypatch.setattr(det_mod, "UltralyticsDetector", _LazyFail)
    sel = DetectorSelector(_cfg())
    got = det_mod.build_selected(sel, "yolov8n.pt", 0.35, "cpu")

    assert built == [OPENVINO, TORCH], "did not fall back after a working load"
    assert got._backend_name == TORCH
    assert sel.snapshot()["attempts_used"] == 1, "silent failure went uncounted"


def test_the_smoke_test_uses_the_real_detect_path(monkeypatch):
    """It has to go through detect(), not a private hook: the whole point is to
    exercise the lazy backend construction the constructor skips."""
    from analytics import detector as det_mod

    seen = []

    class _Recorder:
        def detect(self, frame):
            seen.append(frame.shape)
            return []

    det_mod._smoke_test(_Recorder())
    assert seen and len(seen[0]) == 3 and seen[0][2] == 3, "not an image"
