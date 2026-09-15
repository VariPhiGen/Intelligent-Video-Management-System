"""backends.py — how a loaded model says what it actually is.

A BACKEND IS THREE THINGS, NOT ONE. "GPU" is not a backend and neither is
"ONNX": the representation on disk (torch weights, an ONNX graph, an OpenVINO
IR), the runtime executing it (ultralytics/torch, onnxruntime, openvino) and the
device it runs on are independent choices. Collapsing them is how a system ends
up with a rule like "GPU present, therefore everything on GPU", which is exactly
the decision this work exists to stop making globally.

WHAT THIS FILE IS AND IS NOT, TODAY. It is the descriptor every loaded model
returns so /health can name it, and the vocabulary the calibration profile will
be written in. It is NOT yet a candidate registry: which alternatives are real
depends on three facts about the deployed image — what fast_plate_ocr's device
argument reaches, whether an ultralytics export preserves class names, and
whether two onnxruntime distributions can coexist — and none of them can be
answered from a development machine. `scripts/probe.py` answers them in the
image; the registry is written against its output rather than against
assumptions about it.

SO EVERY MODEL REPORTS `INCUMBENT` FOR NOW, and that is a true statement rather
than a placeholder: nothing else has been proven loadable yet. When candidates
arrive, `selected_by` is what distinguishes a benchmarked decision from a
fallback — the distinction an operator needs when performance looks wrong and
the backend name alone does not explain it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

#: Model representations. What is on disk.
TORCH = "torch"
ONNX = "onnx"
OPENVINO_IR = "openvino_ir"

#: Runtimes. What executes it. Kept distinct from the representation because
#: an ONNX graph can be executed by onnxruntime OR by openvino, and a decision
#: that conflates them cannot express the difference.
RT_ULTRALYTICS = "ultralytics"
RT_TORCH = "torch"
RT_ONNXRUNTIME = "onnxruntime"
RT_OPENVINO = "openvino"

#: How this backend came to be selected. Ordered from most to least evidence.
BY_CALIBRATION = "calibration"      # benchmarked ON THIS MACHINE against alternatives
#: Chosen by a rule from measurements taken during DEVELOPMENT — OpenVINO on
#: CPU, say — not by benchmarking this deployment. Distinct from
#: BY_CALIBRATION on purpose: an operator reading "calibration" would
#: reasonably believe a benchmark had run here, and none has.
BY_PREFERENCE = "preference"
BY_SOLE = "sole-candidate"          # the only implementation that exists
BY_INCUMBENT = "incumbent"          # the shipped default; nothing else proven yet
BY_FALLBACK = "fallback"            # the selected backend failed to load
BY_CONFIG = "config"                # an operator pinned it


@dataclass(frozen=True)
class BackendSpec:
    """What a loaded model is running on, in the terms /health reports.

    Frozen because it describes a load that already happened. Changing a
    backend means loading a different model, not mutating this.
    """
    #: Which of the service's four inference components this describes.
    component: str
    representation: str
    runtime: str
    #: Resolved, never a preference — "cuda", not None. index/hardware.py's
    #: resolve_device does the resolving so the answer has a name to report.
    device: str
    selected_by: str
    #: Version of the runtime that loaded it, for the profile fingerprint.
    runtime_version: Optional[str] = None
    #: Artifact on disk. A file for torch/ONNX, a directory for an OpenVINO IR.
    artifact: Optional[str] = None
    #: Execution provider, where the runtime has the concept. ONNX Runtime does;
    #: torch does not, and reporting an empty one there would invent a fact.
    provider: Optional[str] = None
    #: Set ONLY when the runtime gave us something other than what was asked
    #: for. See `ort_spec` — a silent downgrade is the failure this names.
    requested_device: Optional[str] = None

    @property
    def downgraded(self) -> bool:
        return self.requested_device is not None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


#: Which device an ONNX Runtime execution provider actually runs on. The
#: OpenVINO EP is deliberately absent: it can target CPU, iGPU or NPU depending
#: on how it was built and configured, so mapping it to a device would be a
#: guess. Its provider name is reported and its device left unclaimed.
_PROVIDER_DEVICE = {
    "CPUExecutionProvider": "cpu",
    "CUDAExecutionProvider": "cuda",
    "TensorrtExecutionProvider": "cuda",
    "ROCMExecutionProvider": "rocm",
    "MIGraphXExecutionProvider": "rocm",
    "DmlExecutionProvider": "directml",
    "CoreMLExecutionProvider": "coreml",
}

#: Providers that appear in a session's list without being where the model
#: runs. ONNX Runtime returns providers in PRIORITY order and falls through
#: per-op, so the first entry is not necessarily the one doing the work.
#: AzureExecutionProvider is registered by default in stock builds from ~1.16
#: and only claims specific remote-call custom ops — a convolutional model
#: falls straight past it to CPU.
#:
#: MEASURED: on the CPU stack, `device="auto"` produced
#: ['AzureExecutionProvider', 'CPUExecutionProvider']. Naively believing the
#: first entry reported the plate reader as running on a REMOTE device, which
#: is both wrong and alarming — exactly the class of confident-wrong answer
#: this reporting exists to prevent.
_NON_COMPUTE_PROVIDERS = frozenset({"AzureExecutionProvider"})


def ort_spec(component: str, session, requested_device: Optional[str] = None,
             artifact: Optional[str] = None,
             selected_by: str = BY_INCUMBENT) -> BackendSpec:
    """Describe an ONNX Runtime model by ASKING THE SESSION, not the caller.

    THIS FUNCTION EXISTS BECAUSE ONNX RUNTIME DOWNGRADES SILENTLY. Constructing
    a session with CUDAExecutionProvider on a machine with no CUDA build does
    not raise — it warns once and runs on CPU, and the wrapper object goes on
    reporting the provider that was requested. Measured against
    fast_plate_ocr 1.1.0: `recognizer.providers` returned
    ['CUDAExecutionProvider'] while `recognizer.model.get_providers()` returned
    ['CPUExecutionProvider'].

    So the requested device is never the reported one. The session is the only
    thing that knows, and when the two disagree `requested_device` records what
    was asked for so an operator can see a downgrade happened rather than
    wondering why "cuda" is slow.
    """
    providers = []
    try:
        providers = list(session.get_providers())
    except Exception:                                              # noqa: BLE001
        pass
    # The highest-priority provider that is actually a compute device — see
    # _NON_COMPUTE_PROVIDERS for why "first in the list" is the wrong answer.
    compute = [p for p in providers if p not in _NON_COMPUTE_PROVIDERS]
    provider = compute[0] if compute else (providers[0] if providers else None)
    device = _PROVIDER_DEVICE.get(provider or "", "unknown")
    try:
        import onnxruntime as ort
        version: Optional[str] = ort.__version__
    except Exception:                                              # noqa: BLE001
        version = None

    downgraded = (
        requested_device is not None
        and device != "unknown"
        and requested_device not in ("auto", device)
    )
    return BackendSpec(
        component=component, representation=ONNX, runtime=RT_ONNXRUNTIME,
        device=device, selected_by=selected_by, runtime_version=version,
        artifact=artifact, provider=provider,
        requested_device=requested_device if downgraded else None,
    )


def torch_spec(component: str, device: str, artifact: Optional[str] = None,
               runtime: str = RT_TORCH, selected_by: str = BY_INCUMBENT) -> BackendSpec:
    """The incumbent PyTorch backend, described. Version is read lazily and
    tolerates torch being absent — this must never be the thing that raises."""
    try:
        import torch
        version: Optional[str] = torch.__version__
    except Exception:                                              # noqa: BLE001
        version = None
    return BackendSpec(
        component=component, representation=TORCH, runtime=runtime,
        device=device, selected_by=selected_by, runtime_version=version,
        artifact=artifact,
    )


def onnxruntime_spec(component: str, device: str, artifact: Optional[str] = None,
                     provider: Optional[str] = None,
                     selected_by: str = BY_INCUMBENT) -> BackendSpec:
    """An ONNX graph executed by onnxruntime.

    The provider is the interesting half and is passed in rather than guessed:
    a session's actual provider is a property of the session, and asking the
    module which providers are AVAILABLE answers a different question from
    which one a given model ended up on.
    """
    try:
        import onnxruntime as ort
        version: Optional[str] = ort.__version__
    except Exception:                                              # noqa: BLE001
        version = None
    return BackendSpec(
        component=component, representation=ONNX, runtime=RT_ONNXRUNTIME,
        device=device, selected_by=selected_by, runtime_version=version,
        artifact=artifact, provider=provider,
    )
