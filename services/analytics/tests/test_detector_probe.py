"""Crash-isolated detector probe: a backend that kills its process must cost a
fallback, never the service.

Found 2026-10-26 on an Apple M5 under Docker: OpenVINO's ARM CPU plugin runs an
SVE instruction (`uqdecd`) the chip does not implement, SIGILL on the first
inference, 30 container restarts in 30 minutes, zero frames indexed. The
in-process smoke test could not see it — a signal is not an exception.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import signal
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import detector, selection                             # noqa: E402
from analytics.artifacts import Artifact                              # noqa: E402
from analytics.config import ModelConfig                              # noqa: E402
from analytics.hardware import RuntimeInfo                            # noqa: E402
from analytics.selection import OPENVINO, TORCH, DetectorSelector     # noqa: E402


# ── targets for real child processes (module level: spawn imports them) ─────
def _dies_with_sigill():
    os.kill(os.getpid(), signal.SIGILL)


def _raises():
    raise RuntimeError("plugin refused the model")


def _hangs():
    time.sleep(60)


def _passes():
    return None


# ── probe_isolated, with real processes ──────────────────────────────────────
def test_a_native_crash_is_a_reason_not_a_dead_parent():
    why = detector.probe_isolated(_dies_with_sigill, ())
    assert why is not None and "SIGILL" in why and "rejects an instruction" in why


def test_a_python_failure_is_left_to_the_in_process_handler():
    """Not a crash: the ordinary path catches it, retries it within budget and
    reports the actual exception rather than an exit status."""
    assert detector.probe_isolated(_raises, ()) is None


def test_a_hung_probe_is_killed_and_reported():
    started = time.monotonic()
    why = detector.probe_isolated(_hangs, (), timeout=1)
    assert why == "probe did not finish within 1s"
    assert time.monotonic() - started < 15


def test_a_clean_run_passes():
    assert detector.probe_isolated(_passes, ()) is None


# ── build_selected: the probe gates OpenVINO, torch still always loads ───────
class _Fake:
    built: list = []

    def __init__(self, artifact, confidence, device, square_letterbox=True,
                 backend=TORCH, selected_by="preference"):
        self.backend_name = backend
        _Fake.built.append(backend)

    def detect(self, frame):
        return []


@pytest.fixture
def env(monkeypatch):
    _Fake.built = []
    monkeypatch.setattr(selection, "resolve_device", lambda _p: "cpu")
    monkeypatch.setattr(selection, "runtimes", lambda: {
        OPENVINO: RuntimeInfo(OPENVINO, True, "2026.4.1"),
        TORCH: RuntimeInfo(TORCH, True, "2.14.1+cpu"),
    })
    monkeypatch.setattr(selection.artifacts, "export", lambda *a, **kw: Artifact(
        ok=True, component="detector", export_format="openvino", source="yolov8n.pt",
        path="/models/artifacts/detector/k/yolov8n_openvino_model", export_seconds=0.1,
        size_bytes=1, names={"0": "person"}, error=None))
    monkeypatch.setattr(detector, "UltralyticsDetector", _Fake)
    calls = []

    def probe(verdict):
        def run(target, args, timeout=detector.PROBE_TIMEOUT_S):
            calls.append(args)
            return verdict
        monkeypatch.setattr(detector, "probe_isolated", run)
    return probe, calls


def _cfg(**kw):
    c = ModelConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_a_crashing_openvino_is_never_run_in_process(env):
    probe, calls = env
    probe("probe crashed with SIGILL")
    sel = DetectorSelector(_cfg())
    det = detector.build_selected(sel, "yolov8n.pt", 0.35, "cpu")
    assert det.backend_name == TORCH
    assert _Fake.built == [TORCH], "OpenVINO must not be constructed in process"
    snap = sel.snapshot()
    assert snap["exhausted"] and "SIGILL" in snap["failures"][0]


def test_a_crash_is_learned_once_per_process(env):
    probe, calls = env
    probe("probe crashed with SIGILL")
    sel = DetectorSelector(_cfg())
    for _ in range(3):                      # three warm-ups (cameras come and go)
        detector.build_selected(sel, "yolov8n.pt", 0.35, "cpu")
    assert len(calls) == 1
    assert [p.backend for p in sel.plans("yolov8n.pt")] == [TORCH]


def test_a_passing_probe_keeps_openvino_and_runs_once(env):
    probe, calls = env
    probe(None)
    sel = DetectorSelector(_cfg())
    for _ in range(2):
        det = detector.build_selected(sel, "yolov8n.pt", 0.35, "cpu")
        assert det.backend_name == OPENVINO
    assert len(calls) == 1


def test_pinning_openvino_cannot_pin_a_crash_loop(env):
    probe, _ = env
    probe("probe crashed with SIGILL")
    sel = DetectorSelector(_cfg(detector_backend=OPENVINO))
    assert detector.build_selected(sel, "yolov8n.pt", 0.35, "cpu").backend_name == TORCH


def test_torch_alone_is_not_probed(env):
    probe, calls = env
    probe("would be wrong to ask")
    sel = DetectorSelector(_cfg(detector_backend=TORCH))
    assert detector.build_selected(sel, "yolov8n.pt", 0.35, "cpu").backend_name == TORCH
    assert calls == []


def test_the_probe_mirrors_the_shared_detector_configuration(env):
    probe, calls = env
    probe(None)
    detector.build_selected(DetectorSelector(_cfg()), "yolov8n.pt", 0.35, "cpu",
                            shared_openvino=True, inference_threads=4, inference_requests=2)
    shared, artifact, conf, device, threads, requests = calls[0]
    assert (shared, conf, device, threads, requests) == (True, 0.35, "cpu", 4, 2)
    assert artifact.endswith("_openvino_model")
