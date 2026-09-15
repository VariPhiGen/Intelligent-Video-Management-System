"""candidates.py — every backend a model COULD run on, and whether it can here.

THE REGISTRY IS HARDWARE-GENERAL; THE VERDICT IS LOCAL. A candidate is declared
once, for all deployments, and then asked whether it is available on THIS
machine. That split is what stops the registry drifting into a description of
whichever box it was last run on: a CUDA candidate is declared on a CPU-only
appliance too, and simply reports `unverified` there rather than vanishing.

FOUR VERDICTS, AND THEY ARE NOT DEGREES OF THE SAME THING:

    available    every prerequisite is here; benchmark it
    unavailable  a prerequisite is missing on this machine, and that is a fact
                 about the machine — the candidate stays declared
    unverified   cannot be judged from here at all. A CUDA row on a box with no
                 GPU is not "unavailable": nothing was tested, and reporting a
                 negative would be inventing evidence
    excluded     deliberately not offered in this version, with the reason

The difference between `unavailable` and `unverified` is the one that matters.
Recording "CUDA does not work" from a machine with no CUDA device would be a
measurement nobody took, and a profile carrying it would look like evidence.

DECLARING A CANDIDATE IS NOT PROPOSING IT. Availability gets a candidate as far
as the benchmark. Nothing reaches production without also passing the accuracy
gate for its component, and for the plate localiser that gate is the real ANPR
pipeline — see `requires_accuracy_gate`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from . import hardware
from .backends import (ONNX, OPENVINO_IR, RT_ONNXRUNTIME, RT_OPENVINO,
                       RT_TORCH, RT_ULTRALYTICS, TORCH)

log = logging.getLogger("analytics.candidates")

#: Components. Same names the /health `inference` block reports.
DETECTOR = "detector"
EMBEDDER = "embedder"
PLATE_LOCALISER = "plate_localiser"
PLATE_OCR = "plate_ocr"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNVERIFIED = "unverified"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class Candidate:
    """One (component, representation, runtime, device) a model might run on."""
    component: str
    representation: str
    runtime: str
    device: str
    #: Ultralytics export format, where this candidate needs an artifact built.
    #: None means it runs from the weights the deployment already has.
    export_format: Optional[str] = None
    #: Python modules that must import for this candidate to work at all.
    requires: tuple[str, ...] = ()
    #: ONNX Runtime execution provider this candidate needs in the build.
    requires_provider: Optional[str] = None
    #: True when a CUDA device must be present. Absent GPU makes the verdict
    #: `unverified`, never `unavailable` — see the module docstring.
    requires_cuda: bool = False
    #: Set for a candidate deliberately withheld in this version.
    excluded_reason: Optional[str] = None
    #: Why this candidate is worth trying at all, carried into the profile so a
    #: decision can be read without the code.
    rationale: str = ""

    @property
    def key(self) -> str:
        art = self.export_format or self.representation
        return f"{self.component}:{art}/{self.runtime}/{self.device}"

    @property
    def is_incumbent(self) -> bool:
        """The shipped implementation, which needs no artifact and is the
        reference every other candidate is measured against."""
        return self.export_format is None and self.representation == TORCH

    @property
    def requires_accuracy_gate(self) -> bool:
        """Every non-incumbent candidate must prove it before it can be picked.

        There is no fast path for a component whose gate is inconvenient. The
        plate localiser's gate runs the real two-stage ANPR pipeline, because
        the thing that can break is the interaction — a localiser box that
        shifts by a few pixels changes the margin the recogniser gets, and
        margin is what decides whether a plate reads at all.
        """
        return not self.is_incumbent


def resolvable_weights(reference: str) -> bool:
    """Can ultralytics get these weights, whether or not they exist right now?

    TWO KINDS OF REFERENCE, AND CONFLATING THEM MISJUDGES BOTH. A bare model
    name like `yolov8n.pt` is a catalogue entry ultralytics DOWNLOADS on first
    use — the shipped default, and deliberately absent from the image because
    the packaging harness refuses binaries it cannot account for. A reference
    with a directory component, like `/models/variphi_anpr.pt`, is an
    operator-supplied file: nothing will fetch it, so if it is not there the
    candidate genuinely cannot run.

    Judging the first kind by existence marks every export candidate
    unavailable on a clean install while the incumbent reports available, from
    the same weights — which is how this was found.
    """
    if not reference:
        return False
    head, tail = os.path.split(reference)
    if head:                          # a path: it must actually be there
        return os.path.exists(reference)
    return bool(tail)                 # a catalogue name: ultralytics resolves it


@dataclass
class Verdict:
    candidate: Candidate
    status: str
    reason: str
    #: Populated for `available` candidates once an artifact has been built.
    artifact: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "key": self.candidate.key,
            "component": self.candidate.component,
            "representation": self.candidate.representation,
            "runtime": self.candidate.runtime,
            "device": self.candidate.device,
            "status": self.status,
            "reason": self.reason,
        }
        if self.candidate.rationale:
            out["rationale"] = self.candidate.rationale
        if self.artifact:
            out["artifact"] = self.artifact
        if self.detail:
            out["detail"] = self.detail
        return out


# ── the registry ─────────────────────────────────────────────────────────────
def _yolo_candidates(component: str) -> list[Candidate]:
    """Both YOLO-backed components have identical backend options.

    Declared once and instantiated per component rather than copied, so the
    detector and the localiser cannot drift apart in what they are allowed to
    consider. What differs between them is the ACCURACY GATE, not the menu.
    """
    return [
        Candidate(component, TORCH, RT_ULTRALYTICS, "cpu",
                  requires=("torch", "ultralytics"),
                  rationale="the shipped implementation; the reference"),
        Candidate(component, TORCH, RT_ULTRALYTICS, "cuda",
                  requires=("torch", "ultralytics"), requires_cuda=True,
                  rationale="incumbent weights on the GPU"),
        Candidate(component, OPENVINO_IR, RT_OPENVINO, "cpu",
                  export_format="openvino",
                  requires=("openvino", "ultralytics"),
                  rationale="measured 1.5-2.9x over torch on CPU, widening on "
                            "non-square frames"),
        Candidate(component, ONNX, RT_ONNXRUNTIME, "cpu",
                  export_format="onnx",
                  requires=("onnxruntime", "onnx", "ultralytics"),
                  requires_provider="CPUExecutionProvider",
                  rationale="measured output identical to torch under matched "
                            "preprocessing"),
        Candidate(component, ONNX, RT_ONNXRUNTIME, "cuda",
                  export_format="onnx",
                  requires=("onnxruntime", "onnx", "ultralytics"),
                  requires_provider="CUDAExecutionProvider", requires_cuda=True,
                  excluded_reason=(
                      "needs onnxruntime-gpu, which replaces the onnxruntime "
                      "distribution fast-plate-ocr installs. Unverified on GPU "
                      "hardware; the torch/cuda incumbent already clears the "
                      "workload by a wide margin"),
                  rationale="GPU ONNX execution"),
    ]


def registry() -> list[Candidate]:
    """Every candidate this version knows about, for any hardware."""
    out: list[Candidate] = []
    out += _yolo_candidates(DETECTOR)
    out += _yolo_candidates(PLATE_LOCALISER)

    # OCR. Already ONNX, so the axis is the execution provider — and probing
    # fast_plate_ocr 1.1.0 showed it takes a full `providers` sequence with
    # per-provider options, not just a coarse device switch.
    out += [
        Candidate(PLATE_OCR, ONNX, RT_ONNXRUNTIME, "cpu",
                  requires=("onnxruntime", "fast_plate_ocr"),
                  requires_provider="CPUExecutionProvider",
                  rationale="the shipped implementation; the reference"),
        Candidate(PLATE_OCR, ONNX, RT_ONNXRUNTIME, "cuda",
                  requires=("onnxruntime", "fast_plate_ocr"),
                  requires_provider="CUDAExecutionProvider", requires_cuda=True,
                  rationale="3 MB model; the provider is selectable, the "
                            "benefit is not yet measured"),
        Candidate(PLATE_OCR, ONNX, RT_ONNXRUNTIME, "cpu",
                  requires=("onnxruntime", "fast_plate_ocr"),
                  requires_provider="OpenVINOExecutionProvider",
                  excluded_reason=(
                      "the OpenVINO execution provider is absent from the "
                      "stock onnxruntime build (measured: only Azure and CPU "
                      "providers), and onnxruntime-openvino would displace the "
                      "distribution fast-plate-ocr needs"),
                  rationale="OpenVINO kernels for the recogniser"),
    ]

    # The encoder. ONE ENTRY, and that is the decision rather than an omission:
    # an alternative would have to prove RANK agreement against the vectors
    # already stored, which needs a mature index and real queries — neither of
    # which exists at the moment a first calibration runs. See index/embedder.py.
    out.append(
        Candidate(EMBEDDER, TORCH, RT_TORCH, "cpu",
                  requires=("torch", "open_clip"),
                  rationale="sole candidate: a swap would create a permanently "
                            "mixed vector corpus")
    )
    out.append(
        Candidate(EMBEDDER, TORCH, RT_TORCH, "cuda",
                  requires=("torch", "open_clip"), requires_cuda=True,
                  rationale="same model, GPU device: the vectors are unchanged")
    )
    return out


# ── judging them here ────────────────────────────────────────────────────────
def evaluate(candidates: Optional[list[Candidate]] = None,
             *, weights: Optional[dict[str, str]] = None) -> list[Verdict]:
    """Ask each declared candidate whether it can run on THIS machine.

    Cheap and side-effect free: imports and attribute reads only. Nothing is
    exported, loaded or benchmarked here — this decides what is worth the
    expense, and index/artifacts.py does the expensive part.
    """
    cands = registry() if candidates is None else candidates
    rt = hardware.runtimes()
    gpu = hardware.gpu_info()
    providers = set(rt["onnxruntime"].providers) if rt["onnxruntime"].available else set()
    weights = weights or {}

    out: list[Verdict] = []
    for c in cands:
        if c.excluded_reason:
            out.append(Verdict(c, EXCLUDED, c.excluded_reason))
            continue

        missing = [m for m in c.requires if not rt.get(m, None) or not rt[m].available]
        if missing:
            out.append(Verdict(c, UNAVAILABLE,
                               f"missing runtime: {', '.join(sorted(missing))}"))
            continue

        # THE MODEL BEFORE THE MACHINE. Checked ahead of the device and the
        # provider because it is the more fundamental reason and the more
        # useful one to report: telling an operator with no ANPR model that
        # their localiser candidate lacks a CUDA device is true and beside the
        # point. A component the deployment never configured has not failed a
        # test — it was never in one.
        needed = weights.get(c.component)
        if c.component == PLATE_LOCALISER and not needed:
            out.append(Verdict(c, UNVERIFIED,
                               "no plate localiser weights configured "
                               "(plates.localiser_weights is empty)"))
            continue
        if needed and not resolvable_weights(needed):
            out.append(Verdict(c, UNAVAILABLE, f"weights not found: {needed}"))
            continue

        if c.requires_cuda and not gpu.present:
            # NOT `unavailable`. Nothing was tested, so a negative would be a
            # measurement nobody took — and a profile carrying it would read
            # like evidence that this backend had been tried and rejected.
            out.append(Verdict(c, UNVERIFIED,
                               "no CUDA device on this machine; the candidate "
                               "is declared but cannot be judged from here"))
            continue

        if c.requires_provider and c.requires_provider not in providers:
            out.append(Verdict(
                c, UNAVAILABLE,
                f"onnxruntime build has no {c.requires_provider} "
                f"(present: {', '.join(sorted(providers)) or 'none'})"))
            continue

        out.append(Verdict(c, AVAILABLE, "all prerequisites present"))
    return out


def summarise(verdicts: list[Verdict]) -> dict[str, Any]:
    """Counts plus the per-component breakdown, for /health and the profile."""
    by_status: dict[str, int] = {}
    by_component: dict[str, list[dict]] = {}
    for v in verdicts:
        by_status[v.status] = by_status.get(v.status, 0) + 1
        by_component.setdefault(v.candidate.component, []).append(v.to_dict())
    return {
        "totals": by_status,
        "benchmarkable": [v.candidate.key for v in verdicts if v.status == AVAILABLE],
        "by_component": by_component,
    }
