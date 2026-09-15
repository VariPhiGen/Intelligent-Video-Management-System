"""Backend descriptors, and the reporting that was missing before them.

The gap being closed: /health could say ingest was READY without ever saying
whether it was ready on a CPU or a GPU. The resolved device existed only in a
boot log line, so "is this running on the GPU?" had no answer over HTTP.

These tests pin the two properties that make the answer trustworthy — that a
spec names a resolved device rather than a preference, and that the report
SURVIVES HIBERNATION, since a question about configuration should not stop
having an answer because the weights were released.

Run: python3 -m pytest tests -q   (from services/smartsearch)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index import backends                                        # noqa: E402
from index.backends import (                                      # noqa: E402
    BY_CALIBRATION, BY_INCUMBENT, BY_SOLE, ONNX, TORCH, BackendSpec,
    onnxruntime_spec, ort_spec, torch_spec,
)


class _FakeSession:
    """An ONNX Runtime session, as far as ort_spec is concerned."""
    def __init__(self, providers):
        self._providers = providers

    def get_providers(self):
        return list(self._providers)


class _FakeDetector:
    def __init__(self, spec):
        self._spec = spec

    @property
    def backend(self):
        return self._spec


class _FakePlateReader:
    """Two stages, two independently selectable backends — as PlateReader is."""
    def __init__(self, specs):
        self._specs = specs

    @property
    def backends(self):
        return self._specs


# ── the descriptor ───────────────────────────────────────────────────────────
def test_representation_runtime_and_device_are_three_separate_facts():
    """ONNX is not a device and CUDA is not a representation. Collapsing them
    is how a system ends up with 'GPU exists, therefore everything on GPU'."""
    spec = BackendSpec(component="detector", representation=ONNX,
                       runtime=backends.RT_ONNXRUNTIME, device="cpu",
                       selected_by=BY_CALIBRATION,
                       provider="OpenVINOExecutionProvider")
    d = spec.to_dict()
    assert d["representation"] == ONNX
    assert d["runtime"] == "onnxruntime"
    assert d["device"] == "cpu"
    assert d["provider"] == "OpenVINOExecutionProvider"


def test_absent_fields_are_omitted_not_reported_as_null():
    """torch has no execution-provider concept. Emitting an empty one would
    invent a fact about a runtime that does not have the idea."""
    spec = torch_spec("embedder", "cuda")
    assert "provider" not in spec.to_dict()


def test_spec_is_frozen():
    """A spec describes a load that already happened. Changing a backend means
    loading a different model, not mutating the description of this one."""
    spec = torch_spec("detector", "cpu")
    with pytest.raises(Exception):
        spec.device = "cuda"                       # type: ignore[misc]


def test_selected_by_distinguishes_a_decision_from_a_default():
    """The distinction an operator needs when performance looks wrong: the
    backend name alone cannot tell you whether anything else was ever tried."""
    assert torch_spec("detector", "cpu").selected_by == BY_INCUMBENT
    assert torch_spec("embedder", "cpu",
                      selected_by=BY_SOLE).selected_by == BY_SOLE
    assert onnxruntime_spec("plate_ocr", "cpu",
                            selected_by=BY_CALIBRATION).selected_by == BY_CALIBRATION


def test_torch_spec_records_the_runtime_version():
    spec = torch_spec("detector", "cpu")
    assert spec.representation == TORCH
    # torch is a hard dependency of this service; if it is importable the
    # version must be captured, because the profile fingerprints it.
    pytest.importorskip("torch")
    assert spec.runtime_version


def test_specs_survive_a_runtime_being_absent(monkeypatch):
    """Building a descriptor must never be the thing that fails a load."""
    import builtins
    real_import = builtins.__import__

    def no_ort(name, *a, **kw):
        if name == "onnxruntime":
            raise ImportError("absent")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_ort)
    spec = onnxruntime_spec("plate_ocr", "cpu")
    assert spec.runtime_version is None
    assert spec.component == "plate_ocr"


# ── the silent downgrade ─────────────────────────────────────────────────────
def test_the_session_is_believed_not_the_request():
    """MEASURED AGAINST fast_plate_ocr 1.1.0 ON A CPU-ONLY BOX. Asking for
    device="cuda" did not raise: onnxruntime warned once, ran on CPU, and the
    wrapper went on reporting ['CUDAExecutionProvider'] from its own attribute
    while the session reported ['CPUExecutionProvider'].

    Reporting the request would put a confident wrong answer into /health,
    which is strictly worse than the missing answer this reporting replaces."""
    spec = ort_spec("plate_ocr", _FakeSession(["CPUExecutionProvider"]),
                    requested_device="cuda")
    assert spec.device == "cpu"
    assert spec.provider == "CPUExecutionProvider"
    assert spec.downgraded is True
    assert spec.requested_device == "cuda"


def test_an_honoured_request_is_not_flagged_as_downgraded():
    spec = ort_spec("plate_ocr", _FakeSession(["CUDAExecutionProvider"]),
                    requested_device="cuda")
    assert spec.device == "cuda"
    assert spec.downgraded is False
    assert "requested_device" not in spec.to_dict()


def test_auto_is_never_a_downgrade():
    """"auto" asked for whatever was best. Whatever it got IS what it asked
    for, so flagging it would cry wolf on every default deployment."""
    spec = ort_spec("plate_ocr", _FakeSession(["CPUExecutionProvider"]),
                    requested_device="auto")
    assert spec.device == "cpu"
    assert spec.downgraded is False


def test_a_non_compute_provider_is_skipped_when_naming_the_device():
    """MEASURED: device="auto" gave ['AzureExecutionProvider',
    'CPUExecutionProvider']. ONNX Runtime returns providers in PRIORITY order
    and falls through per-op; the Azure EP only claims remote-call custom ops,
    so a convolutional model runs on CPU. Believing the first entry reported
    the plate reader as running on a REMOTE device."""
    spec = ort_spec("plate_ocr", _FakeSession(
        ["AzureExecutionProvider", "CPUExecutionProvider"]), requested_device="auto")
    assert spec.provider == "CPUExecutionProvider"
    assert spec.device == "cpu"
    assert spec.downgraded is False


def test_cuda_still_wins_when_a_non_compute_provider_precedes_it():
    spec = ort_spec("plate_ocr", _FakeSession(
        ["AzureExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]),
        requested_device="cuda")
    assert spec.device == "cuda"
    assert spec.downgraded is False


def test_openvino_provider_does_not_claim_a_device():
    """The OpenVINO EP can target CPU, an iGPU or an NPU depending on how it
    was built. Naming one would be a guess, so the provider is reported and
    the device left unclaimed rather than invented."""
    spec = ort_spec("plate_ocr", _FakeSession(["OpenVINOExecutionProvider"]))
    assert spec.provider == "OpenVINOExecutionProvider"
    assert spec.device == "unknown"
    assert spec.downgraded is False          # unknown device cannot prove one


def test_a_session_that_cannot_be_questioned_degrades():
    """Building a descriptor is never allowed to be what fails a load."""
    class Mute:
        def get_providers(self):
            raise RuntimeError("no")
    spec = ort_spec("plate_ocr", Mute(), requested_device="cpu")
    assert spec.device == "unknown"
    assert spec.provider is None
    spec = ort_spec("plate_ocr", None, requested_device="cpu")
    assert spec.device == "unknown"


# ── what the pool records ────────────────────────────────────────────────────
def _pool():
    from index.config import AppConfig
    from index.models import ModelPool
    cfg = AppConfig()
    return ModelPool(cfg, store=None, writer=None)


def test_pool_records_a_single_backend_component():
    pool = _pool()
    pool._record_backend(_FakeDetector(torch_spec("detector", "cuda")))
    snap = pool.inference_snapshot()
    assert snap["detector"]["device"] == "cuda"
    assert snap["detector"]["runtime"] == "torch"


def test_pool_records_both_plate_stages_separately():
    """The localiser is ultralytics and the recogniser is onnxruntime. One line
    for both would hide a per-model decision that is genuinely independent."""
    pool = _pool()
    pool._record_backend(_FakePlateReader({
        "plate_localiser": torch_spec("plate_localiser", "cuda",
                                      runtime=backends.RT_ULTRALYTICS),
        "plate_ocr": onnxruntime_spec("plate_ocr", "cpu"),
    }))
    snap = pool.inference_snapshot()
    assert snap["plate_localiser"]["device"] == "cuda"
    assert snap["plate_ocr"]["device"] == "cpu"
    assert snap["plate_ocr"]["runtime"] == "onnxruntime"


def test_pool_tolerates_a_model_with_no_backend_property():
    """Test doubles and anything predating the seam must not break a warm-up.
    Reporting is worth having and never worth a failed load."""
    pool = _pool()
    pool._record_backend(object())
    assert pool.inference_snapshot() == {}


def test_never_loaded_components_are_absent_rather_than_guessed():
    """Saying nothing beats reporting a default nobody confirmed."""
    pool = _pool()
    pool._record_backend(_FakeDetector(torch_spec("detector", "cpu")))
    snap = pool.inference_snapshot()
    assert "detector" in snap
    assert "plate_ocr" not in snap
    assert "embedder" not in snap


def test_backend_report_survives_hibernation():
    """THE POINT OF RETAINING THE SPEC. An operator checking a hibernating
    appliance still needs to know whether inference runs on CPU or GPU; 'the
    models are asleep so we cannot tell you' answers a different question."""
    pool = _pool()
    pool._record_backend(_FakeDetector(torch_spec("detector", "cuda")))
    assert pool.inference_snapshot()["detector"]["loaded"] is False   # never set
    pool._detector = object()                       # simulate a live load
    assert pool.inference_snapshot()["detector"]["loaded"] is True
    pool._detector = None                           # simulate the reaper
    snap = pool.inference_snapshot()
    assert snap["detector"]["loaded"] is False
    assert snap["detector"]["device"] == "cuda"     # the answer is still there


def test_inference_snapshot_takes_no_lock():
    """/health's snapshot is deliberately lock-free so a warm-up cannot hang
    the 5 s container healthcheck. Anything it calls must obey the same rule."""
    pool = _pool()
    pool._record_backend(_FakeDetector(torch_spec("detector", "cpu")))
    acquired = pool._lock.acquire(blocking=False)
    assert acquired, "test setup: lock should be free"
    try:
        # Holding the pool lock, as a warm-up does. This must not block.
        assert pool.inference_snapshot()["detector"]["device"] == "cpu"
    finally:
        pool._lock.release()


def test_snapshot_carries_inference_into_health():
    pool = _pool()
    pool._record_backend(_FakeDetector(torch_spec("detector", "cpu")))
    snap = pool.snapshot()
    assert "inference" in snap
    assert snap["inference"]["detector"]["selected_by"] == BY_INCUMBENT
