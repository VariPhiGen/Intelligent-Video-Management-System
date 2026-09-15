"""The candidate registry, the export cache, and the frames they are judged on.

THE PROPERTY THESE TESTS EXIST TO PROTECT is that the registry describes what
Variphi's models COULD run on, not what the machine running the tests happens
to have. A CUDA candidate must survive being evaluated on a CPU-only box, and
must come back `unverified` rather than `unavailable` — the difference between
"nothing was tested" and "it was tested and failed", which is the difference
between an honest profile and one carrying a measurement nobody took.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import artifacts, candidates, frames                   # noqa: E402
from analytics.candidates import (                                    # noqa: E402
    AVAILABLE, DETECTOR, EMBEDDER, EXCLUDED, PLATE_LOCALISER, PLATE_OCR,
    UNAVAILABLE, UNVERIFIED, Candidate,
)
from analytics.hardware import CpuInfo, GpuInfo, RuntimeInfo          # noqa: E402


def _runtimes(**overrides):
    """All runtimes present unless a test says otherwise."""
    base = {name: RuntimeInfo(name, True, "1.0")
            for name in ("torch", "ultralytics", "onnxruntime", "onnx",
                         "onnxslim", "openvino", "open_clip",
                         "fast_plate_ocr")}
    base["onnxruntime"] = RuntimeInfo("onnxruntime", True, "1.29.0",
                                      providers=["CPUExecutionProvider"])
    base.update(overrides)
    return base


def _patch_env(monkeypatch, *, gpu=False, runtimes=None):
    monkeypatch.setattr(candidates.hardware, "runtimes",
                        lambda: runtimes or _runtimes())
    monkeypatch.setattr(candidates.hardware, "gpu_info", lambda: GpuInfo(
        present=gpu, name="NVIDIA RTX 2000 Ada" if gpu else None,
        vram_mb=16380 if gpu else None))


# ── the registry is hardware-general ─────────────────────────────────────────
def test_registry_declares_all_four_components():
    got = {c.component for c in candidates.registry()}
    assert got == {DETECTOR, EMBEDDER, PLATE_LOCALISER, PLATE_OCR}


def test_cuda_candidates_are_declared_on_a_cpu_only_machine(monkeypatch):
    """The registry is a statement about the product, not about this box."""
    _patch_env(monkeypatch, gpu=False)
    cuda = [c for c in candidates.registry() if c.requires_cuda]
    assert cuda, "no CUDA candidates declared — the registry has narrowed to CPU"
    assert {c.component for c in cuda} >= {DETECTOR, EMBEDDER, PLATE_OCR}


def test_cuda_on_a_cpu_box_is_unverified_not_unavailable(monkeypatch):
    """RECORDING A NEGATIVE HERE WOULD BE INVENTING EVIDENCE. Nothing was
    tested, so a profile saying CUDA 'does not work' would read as a backend
    that had been tried and rejected."""
    _patch_env(monkeypatch, gpu=False)
    verdicts = {v.candidate.key: v for v in candidates.evaluate(
        weights={DETECTOR: "yolov8n.pt"})}
    # Scoped to components this deployment actually configured: a localiser
    # candidate reports its MISSING MODEL instead, which is the better reason.
    cuda = [v for v in verdicts.values()
            if v.candidate.requires_cuda and not v.candidate.excluded_reason
            and v.candidate.component != PLATE_LOCALISER]
    assert cuda
    for v in cuda:
        assert v.status == UNVERIFIED, f"{v.candidate.key} -> {v.status}"
        assert "cannot be judged" in v.reason


def test_cuda_becomes_available_when_a_gpu_appears(monkeypatch, tmp_path):
    """Same registry, different machine, different verdict — no code change."""
    weights = tmp_path / "yolov8n.pt"
    weights.write_bytes(b"w")
    _patch_env(monkeypatch, gpu=True,
               runtimes=_runtimes(onnxruntime=RuntimeInfo(
                   "onnxruntime", True, "1.29.0",
                   providers=["CPUExecutionProvider", "CUDAExecutionProvider"])))
    verdicts = {v.candidate.key: v for v in candidates.evaluate(
        weights={DETECTOR: str(weights)})}
    assert verdicts[f"{DETECTOR}:torch/ultralytics/cuda"].status == AVAILABLE
    assert verdicts[f"{EMBEDDER}:torch/torch/cuda"].status == AVAILABLE


# ── local verdicts ───────────────────────────────────────────────────────────
def test_a_missing_runtime_is_unavailable_with_the_reason(monkeypatch):
    _patch_env(monkeypatch, runtimes=_runtimes(
        openvino=RuntimeInfo("openvino", False, error="No module named 'openvino'")))
    verdicts = {v.candidate.key: v for v in candidates.evaluate(
        weights={DETECTOR: "yolov8n.pt"})}
    v = verdicts[f"{DETECTOR}:openvino/openvino/cpu"]
    assert v.status == UNAVAILABLE
    assert "openvino" in v.reason


def test_a_missing_execution_provider_is_unavailable(monkeypatch):
    """MEASURED: the stock onnxruntime build carries only Azure and CPU
    providers, so an OpenVINO-EP candidate is unavailable on availability
    grounds rather than because of a distribution conflict."""
    _patch_env(monkeypatch)
    verdicts = candidates.evaluate(weights={DETECTOR: "yolov8n.pt"})
    ov_ep = [v for v in verdicts
             if v.candidate.requires_provider == "OpenVINOExecutionProvider"]
    assert ov_ep
    # Declared as excluded in this version, with the reason recorded.
    assert all(v.status == EXCLUDED for v in ov_ep)
    assert all("OpenVINO execution provider" in v.reason or "displace" in v.reason
               for v in ov_ep)


def test_the_localiser_is_unverified_without_operator_weights(monkeypatch):
    """An operator with no ANPR model has not failed a test.

    And the MISSING MODEL is the reason reported, ahead of any device or
    provider reason — telling a site with no ANPR weights that their localiser
    candidate lacks a CUDA device is true and beside the point.
    """
    _patch_env(monkeypatch, gpu=False)
    verdicts = [v for v in candidates.evaluate(weights={DETECTOR: "yolov8n.pt"})
                if v.candidate.component == PLATE_LOCALISER
                and not v.candidate.excluded_reason]
    assert verdicts
    assert all(v.status == UNVERIFIED for v in verdicts)
    assert all("localiser_weights" in v.reason for v in verdicts), \
        "a device reason shadowed the missing-model reason"


def test_evaluation_never_raises_on_a_bare_machine():
    """Called during boot on machines with almost nothing installed."""
    got = candidates.evaluate()
    assert got and all(v.status in (AVAILABLE, UNAVAILABLE, UNVERIFIED, EXCLUDED)
                       for v in got)


def test_summary_is_json_shaped():
    import json
    json.dumps(candidates.summarise(candidates.evaluate()), default=str)


# ── the accuracy gate is not optional ────────────────────────────────────────
def test_every_non_incumbent_candidate_requires_an_accuracy_gate():
    """No fast path for a component whose gate is inconvenient. In particular
    the plate localiser's exported candidates must not slip through on
    performance alone — its gate is the real two-stage ANPR pipeline."""
    for c in candidates.registry():
        assert c.requires_accuracy_gate == (not c.is_incumbent)
    exported = [c for c in candidates.registry()
                if c.component == PLATE_LOCALISER and c.export_format]
    assert exported
    assert all(c.requires_accuracy_gate for c in exported)


def test_the_encoder_offers_no_alternative_representation():
    """A swap would create a permanently mixed vector corpus. The single
    representation is the decision, not an omission."""
    enc = [c for c in candidates.registry() if c.component == EMBEDDER]
    assert enc
    assert {c.representation for c in enc} == {"torch"}
    assert all(c.export_format is None for c in enc)


def test_detector_and_localiser_share_one_backend_menu():
    """Declared once and instantiated per component, so the two cannot drift
    apart in what they may consider. What differs is the GATE, not the menu."""
    def menu(component):
        return {(c.representation, c.runtime, c.device)
                for c in candidates.registry() if c.component == component}
    assert menu(DETECTOR) == menu(PLATE_LOCALISER)


# ── the export cache ─────────────────────────────────────────────────────────
def test_cache_key_follows_content_not_filename(tmp_path):
    """An operator retuning ANPR replaces the .pt IN PLACE, keeping the name.
    Keying on the filename would serve yesterday's model under today's name."""
    w = tmp_path / "variphi_anpr.pt"
    w.write_bytes(b"v1")
    first = artifacts._cache_key(str(w), "onnx", "8.4.138")
    w.write_bytes(b"v2")
    assert artifacts._cache_key(str(w), "onnx", "8.4.138") != first


def test_cache_key_separates_formats_and_ultralytics_versions(tmp_path):
    w = tmp_path / "w.pt"
    w.write_bytes(b"v1")
    onnx = artifacts._cache_key(str(w), "onnx", "8.4.138")
    assert artifacts._cache_key(str(w), "openvino", "8.4.138") != onnx
    assert artifacts._cache_key(str(w), "onnx", "8.5.0") != onnx


def test_export_version_bump_invalidates_cached_artifacts(tmp_path, monkeypatch):
    w = tmp_path / "w.pt"
    w.write_bytes(b"v1")
    before = artifacts._cache_key(str(w), "onnx", "8.4.138")
    monkeypatch.setattr(artifacts, "EXPORT_VERSION", artifacts.EXPORT_VERSION + 1)
    assert artifacts._cache_key(str(w), "onnx", "8.4.138") != before


def test_a_missing_source_reports_rather_than_raises(tmp_path):
    """Removing a candidate must never take the service down with it.

    The invariant is the shape of the answer, not the wording: an Artifact with
    ok=False and a stated reason. Which reason depends on the environment —
    without ultralytics installed the missing runtime is reported first, and
    that is equally correct.
    """
    got = artifacts.export(DETECTOR, str(tmp_path / "absent.pt"), "onnx",
                           models_dir=str(tmp_path))
    assert got.ok is False
    assert got.error and got.path is None
    pytest.importorskip("ultralytics")
    assert "not found" in got.error


def test_names_are_compared_by_value_not_by_key_type():
    """An export may key classes as strings where torch keys them as ints.
    Treating that as a loss would reject a good candidate."""
    source = {0: "license_plate", 1: "truck"}
    assert artifacts.names_preserved({"0": "license_plate", "1": "truck"}, source)
    assert not artifacts.names_preserved({"0": "license_plate"}, source)
    assert not artifacts.names_preserved(None, source)
    assert not artifacts.names_preserved({}, source)


# ── benchmark inputs ─────────────────────────────────────────────────────────
def test_production_shapes_are_not_square():
    """MEASURED: OpenVINO beat torch 1.5x on a portrait frame and 2.9x on a
    wide one. Square synthetic input would have reported the smaller number on
    exactly the shape cameras do not produce."""
    for w, h in frames.PRODUCTION_SHAPES:
        assert w != h
    assert (1920, 1080) in frames.PRODUCTION_SHAPES


def test_synthetic_frames_come_out_at_the_requested_shapes():
    fs = frames.synthetic()
    assert [(f.shape[1], f.shape[0]) for f in fs.frames] == list(frames.PRODUCTION_SHAPES)
    assert fs.is_real is False


def test_synthetic_input_is_flagged_as_not_real():
    """Two backends agreeing that there is nothing here have agreed about
    nothing, so an accuracy verdict on synthetic frames is not a pass."""
    assert frames.synthetic().is_real is False
    assert frames.synthetic().to_dict()["is_real"] is False


def test_detector_input_never_falls_back_to_crops(tmp_path, monkeypatch):
    """A crop is not a camera frame. The detector's cost is a function of
    full-frame geometry, so a real crop at the wrong shape is worse input than
    an honest synthetic frame at the right one."""
    monkeypatch.setattr(frames, "from_assets", lambda count=2: None)
    fs = frames.for_detector(camera_urls=())
    assert fs.source == frames.SYNTHETIC
    assert (1920, 1080) in fs.shapes


def test_an_unreachable_camera_does_not_abort_frame_sourcing(monkeypatch):
    def boom(*a, **kw):
        raise OSError("camera is down")
    monkeypatch.setattr(frames, "from_camera", boom)
    monkeypatch.setattr(frames, "from_assets", lambda count=2: None)
    fs = frames.for_detector(camera_urls=("rtsp://relay/gone",))
    assert fs.source == frames.SYNTHETIC


def test_crop_sourcing_returns_none_for_an_empty_store(tmp_path):
    assert frames.from_crop_dir(str(tmp_path)) is None
    assert frames.from_crop_dir(str(tmp_path / "missing")) is None


# ── weights resolution ───────────────────────────────────────────────────────
def test_a_catalogue_name_is_resolvable_before_it_is_downloaded(tmp_path):
    """`yolov8n.pt` is fetched by ultralytics on first use and is deliberately
    absent from the image, since the packaging harness refuses binaries it
    cannot account for. Judging it by existence marked every EXPORT candidate
    unavailable on a clean install while the incumbent — same weights — read
    available. That inconsistency is how this was found."""
    assert candidates.resolvable_weights("yolov8n.pt") is True


def test_an_operator_path_must_actually_exist(tmp_path):
    """Nothing fetches /models/variphi_anpr.pt. If it is not there, the
    candidate genuinely cannot run."""
    missing = tmp_path / "variphi_anpr.pt"
    assert candidates.resolvable_weights(str(missing)) is False
    missing.write_bytes(b"w")
    assert candidates.resolvable_weights(str(missing)) is True


def test_empty_weights_are_never_resolvable():
    assert candidates.resolvable_weights("") is False


def test_export_candidates_track_the_incumbent_on_weights(monkeypatch):
    """The incumbent and the export candidates read the SAME weights, so they
    must never disagree about whether those weights can be had."""
    _patch_env(monkeypatch)
    verdicts = {v.candidate.key: v for v in candidates.evaluate(
        weights={DETECTOR: "yolov8n.pt"})}
    incumbent = verdicts[f"{DETECTOR}:torch/ultralytics/cpu"]
    exported = verdicts[f"{DETECTOR}:onnx/onnxruntime/cpu"]
    assert incumbent.status == AVAILABLE
    assert exported.status == AVAILABLE, \
        "an export candidate was rejected on weights the incumbent accepted"
