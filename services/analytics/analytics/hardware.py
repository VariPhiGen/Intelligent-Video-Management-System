# ===========================================================================
# GENERATED COPY - DO NOT EDIT.
#
# Source: services/smartsearch/index/hardware.py
# Sync:   python3 scripts/sync_shared.py   (--check verifies, and CI should)
#
# Edits here are silently destroyed on the next sync. More to the point, they
# would recreate the exact duplication this replaced: two motion
# implementations that agreed until one of them was tuned. If this file needs
# to change, change the source and re-run the sync.
# ===========================================================================
"""hardware.py — what this machine can actually run, and how to name it.

TWO JOBS, AND THEY ARE THE SAME CODE. Calibration needs to know which runtimes
are installed before it can benchmark them, and a persisted profile needs a
fingerprint precise enough to tell "the same appliance" from "a different one".
Both are answered by probing rather than assuming, which is also why this module
is the thing `scripts/probe.py` runs: the verification tool and the runtime
capability detection must not be allowed to disagree.

NOTHING HERE IMPORTS A HEAVY RUNTIME AT MODULE LEVEL. This is imported during
boot, before the port opens, on machines where torch may be the only one of
these packages present. Every probe is wrapped and every failure is a recorded
absence, never an exception: "openvino is not installed" is a finding, not a
fault.

THE CPU COUNT IS THE ONE PEOPLE GET WRONG. `os.cpu_count()` reports the host's
processors, not the share this container may run on. A container limited to two
cores on a 32-core host reports 32, and a calibration profile built on that
number is calibrated for hardware the service does not have. cgroup v2's
`cpu.max`, cgroup v1's quota/period pair, and the scheduler affinity mask each
bound it, so the honest answer is the smallest of the three.

FINGERPRINT PRECISION IS DELIBERATE AND NARROW. It carries what changes an
inference decision — the CPU's instruction set, the GPU's identity, the major
versions of the runtimes actually selected — and deliberately not every
installed package. Fingerprinting the world means a routine rebuild looks like
a hardware change, and on an unattended appliance that is the difference
between a note in /health and a service that behaves differently after a
restart nobody initiated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger("smartsearch.hardware")

#: Bumped when the fingerprint's SHAPE changes — a new field, a different
#: normalisation — so old profiles are cleanly invalidated rather than compared
#: against a structure they were never built with.
FINGERPRINT_VERSION = 1

#: Instruction-set extensions that materially change CPU inference throughput.
#: Ordered widest-first: the first match is the ISA class recorded.
_ISA_ORDER = ("amx_tile", "avx512f", "avx2", "avx", "neon", "asimd")


# ── CPU ──────────────────────────────────────────────────────────────────────
def _cgroup_quota() -> Optional[float]:
    """CPU cores this cgroup may use, or None when unlimited/not applicable.

    v2 puts "<quota> <period>" (or "max <period>") in cpu.max; v1 splits it
    across cpu.cfs_quota_us and cpu.cfs_period_us with -1 meaning unlimited.
    Both paths shift around between runtimes, so every read is best-effort.
    """
    try:                                                   # cgroup v2
        with open("/sys/fs/cgroup/cpu.max", "r", encoding="utf-8") as fh:
            quota, period = fh.read().split()
        if quota != "max" and float(period) > 0:
            return float(quota) / float(period)
        return None
    except (OSError, ValueError):
        pass
    try:                                                   # cgroup v1
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r", encoding="utf-8") as fh:
            quota = float(fh.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r", encoding="utf-8") as fh:
            period = float(fh.read().strip())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def _affinity_count() -> Optional[int]:
    """Processors this process is actually scheduled on. Linux only."""
    try:
        return len(os.sched_getaffinity(0))                 # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def _proc_cpuinfo() -> dict[str, str]:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().lower()
            if k not in out:                                # first core only
                out[k] = v.strip()
    return out


@dataclass
class CpuInfo:
    model: str
    arch: str
    #: Processors the OS reports. NOT what the container may use.
    host_cpus: int
    #: The share this process can really use — min(affinity, cgroup quota).
    #: This is the number calibration budgets against.
    usable_cpus: float
    #: Widest instruction-set extension found. Drives CPU inference throughput
    #: far more than core count does, which is why it is fingerprinted.
    isa: str
    #: True when a cgroup or affinity mask bounds us below the host count.
    constrained: bool


def cpu_info() -> CpuInfo:
    info = _proc_cpuinfo()
    model = info.get("model name") or info.get("hardware") or platform.processor() \
        or platform.machine() or "unknown"
    flags = set((info.get("flags") or info.get("features") or "").split())
    isa = next((f for f in _ISA_ORDER if f in flags), "baseline")

    host = os.cpu_count() or 1
    bounds = [float(host)]
    aff = _affinity_count()
    if aff:
        bounds.append(float(aff))
    quota = _cgroup_quota()
    if quota:
        bounds.append(quota)
    usable = round(min(bounds), 2)

    return CpuInfo(
        model=re.sub(r"\s+", " ", model).strip(),
        arch=platform.machine() or "unknown",
        host_cpus=host,
        usable_cpus=usable,
        isa=isa,
        constrained=usable < host,
    )


# ── GPU ──────────────────────────────────────────────────────────────────────
@dataclass
class GpuInfo:
    present: bool
    name: Optional[str] = None
    vram_mb: Optional[int] = None
    #: Compute capability as "8.9". Part of the identity: two cards with the
    #: same VRAM and different capability are not interchangeable.
    capability: Optional[str] = None
    #: CUDA runtime torch was built against, e.g. "12.1".
    cuda_runtime: Optional[str] = None
    #: Host driver, via NVML when available. Optional: pynvml is not a
    #: dependency, and its absence must not be reported as "no GPU".
    driver: Optional[str] = None
    count: int = 0
    error: Optional[str] = None


def gpu_info() -> GpuInfo:
    try:
        import torch
    except Exception as exc:                                       # noqa: BLE001
        return GpuInfo(present=False, error=f"torch unavailable: {exc}")
    try:
        if not torch.cuda.is_available():
            return GpuInfo(present=False, cuda_runtime=getattr(torch.version, "cuda", None))
        props = torch.cuda.get_device_properties(0)
        out = GpuInfo(
            present=True,
            name=props.name,
            vram_mb=int(props.total_memory // (1024 * 1024)),
            capability=f"{props.major}.{props.minor}",
            cuda_runtime=getattr(torch.version, "cuda", None),
            count=torch.cuda.device_count(),
        )
    except Exception as exc:                                       # noqa: BLE001
        return GpuInfo(present=False, error=f"{type(exc).__name__}: {exc}")

    try:                                    # driver is a bonus, never required
        import pynvml
        pynvml.nvmlInit()
        out.driver = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(out.driver, bytes):
            out.driver = out.driver.decode()
        pynvml.nvmlShutdown()
    except Exception:                                              # noqa: BLE001
        pass
    return out


def gpu_utilisation() -> Optional[dict]:
    """Current utilisation and free VRAM, or None when NVML is unavailable.

    Used to DEFER calibration on a GPU that DeepStream is already busy with —
    smartsearch shares the card with the real-time path, and a benchmark that
    saturates it degrades live analytics. None means "cannot tell", which the
    caller must treat as "do not assume it is idle".
    """
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        rates = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        out = {
            "gpu_percent": int(rates.gpu),
            "memory_percent": int(rates.memory),
            "vram_used_mb": int(mem.used // (1024 * 1024)),
            "vram_free_mb": int(mem.free // (1024 * 1024)),
        }
        pynvml.nvmlShutdown()
        return out
    except Exception:                                              # noqa: BLE001
        return None


# ── runtimes ─────────────────────────────────────────────────────────────────
@dataclass
class RuntimeInfo:
    name: str
    available: bool
    version: Optional[str] = None
    #: Execution providers, for ONNX Runtime. Empty for everything else.
    providers: list[str] = field(default_factory=list)
    #: Devices the runtime itself reports, for OpenVINO ("CPU", "GPU", "NPU").
    devices: list[str] = field(default_factory=list)
    error: Optional[str] = None


def _probe_torch() -> RuntimeInfo:
    try:
        import torch
        return RuntimeInfo("torch", True, torch.__version__)
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("torch", False, error=str(exc))


def _probe_ultralytics() -> RuntimeInfo:
    try:
        import ultralytics
        return RuntimeInfo("ultralytics", True, ultralytics.__version__)
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("ultralytics", False, error=str(exc))


def _probe_onnxruntime() -> RuntimeInfo:
    try:
        import onnxruntime as ort
        return RuntimeInfo("onnxruntime", True, ort.__version__,
                           providers=list(ort.get_available_providers()))
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("onnxruntime", False, error=str(exc))


def _probe_onnx() -> RuntimeInfo:
    """The graph format itself, which is an EXPORT dependency and not a runtime.

    Separate from onnxruntime on purpose: `onnx` is what ultralytics needs to
    WRITE a graph, `onnxruntime` is what executes one. A machine can easily
    have the second without the first — the OCR pulls in onnxruntime alone —
    and in that state ONNX candidates cannot be built even though ONNX models
    can be run.
    """
    try:
        import onnx
        return RuntimeInfo("onnx", True, _dist_version(onnx, "onnx"))
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("onnx", False, error=str(exc))


def _probe_onnxslim() -> RuntimeInfo:
    """Optional. Ultralytics slims exported graphs with it when present and
    exports without it when not, so its absence changes artifact size rather
    than whether a candidate exists."""
    try:
        import onnxslim
        return RuntimeInfo("onnxslim", True, _dist_version(onnxslim, "onnxslim"))
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("onnxslim", False, error=str(exc))


def _probe_openvino() -> RuntimeInfo:
    try:
        import openvino
        devices: list[str] = []
        try:
            devices = list(openvino.Core().available_devices)
        except Exception:                                          # noqa: BLE001
            pass
        return RuntimeInfo("openvino", True, openvino.__version__, devices=devices)
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("openvino", False, error=str(exc))


def _probe_open_clip() -> RuntimeInfo:
    try:
        import open_clip
        return RuntimeInfo("open_clip", True,
                           _dist_version(open_clip, "open_clip_torch"))
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("open_clip", False, error=str(exc))


def _dist_version(module, dist_name: str) -> str:
    """A module's version, from the module or failing that its distribution.

    Not every package exposes __version__ — fast_plate_ocr 1.1.0 does not — and
    "unknown" in a fingerprint is a field that can never invalidate, which is
    the opposite of what a fingerprint is for.
    """
    version = getattr(module, "__version__", None)
    if version:
        return str(version)
    try:
        from importlib import metadata
        return metadata.version(dist_name)
    except Exception:                                              # noqa: BLE001
        return "unknown"


def _probe_fast_plate_ocr() -> RuntimeInfo:
    try:
        import fast_plate_ocr
        return RuntimeInfo("fast_plate_ocr", True,
                           _dist_version(fast_plate_ocr, "fast-plate-ocr"))
    except Exception as exc:                                       # noqa: BLE001
        return RuntimeInfo("fast_plate_ocr", False, error=str(exc))


def runtimes() -> dict[str, RuntimeInfo]:
    """Every runtime this service might dispatch inference through."""
    return {r.name: r for r in (
        _probe_torch(), _probe_ultralytics(), _probe_onnxruntime(),
        _probe_onnx(), _probe_onnxslim(), _probe_openvino(),
        _probe_open_clip(), _probe_fast_plate_ocr(),
    )}


# ── device resolution ────────────────────────────────────────────────────────
def resolve_device(preference: Optional[str] = None) -> str:
    """Turn a configured device preference into the device actually used.

    WHY THIS EXISTS RATHER THAN PASSING None DOWN. `UltralyticsDetector` used to
    hand `device=None` straight to ultralytics and let it choose, which gave the
    right answer and made it unreportable: /health could not say whether the
    detector was on CPU or GPU, and the only record was one boot log line. The
    resolution is the same — CUDA when torch can see it, CPU otherwise — but
    doing it here means the answer has a name that can be stored in a profile
    and shown to an operator.

    A preference is honoured verbatim, including a bad one: "cuda" on a machine
    with no GPU must fail loudly at load rather than be silently downgraded to
    CPU, because a site that asked for GPU and got CPU has a fault to fix.
    """
    if preference:
        return preference
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:                                              # noqa: BLE001
        return "cpu"


# ── fingerprint ──────────────────────────────────────────────────────────────
def file_digest(path: str) -> Optional[str]:
    """SHA-256 of a model artifact, or None when it is not there.

    Weights are downloaded at runtime and never committed, so their identity is
    the content hash. A directory — which is what an OpenVINO export is —
    hashes its files' names and contents in sorted order, so the digest is
    stable across two exports that produced the same model.
    """
    try:
        h = hashlib.sha256()
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    h.update(os.path.relpath(full, path).encode())
                    with open(full, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
        else:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _major_minor(version: Optional[str]) -> Optional[str]:
    """"2.13.0+cu121" -> "2.13". Patch releases do not change a decision, and
    fingerprinting them would recalibrate an appliance on every rebuild."""
    if not version:
        return None
    m = re.match(r"(\d+)\.(\d+)", version)
    return f"{m.group(1)}.{m.group(2)}" if m else version


@dataclass
class Fingerprint:
    """The environment identity a calibration profile is valid for.

    Split into HARD and SOFT deliberately. Hard fields change what the right
    answer is, so a mismatch means the profile cannot be trusted. Soft fields
    are recorded for diagnosis and drift reporting but never invalidate — see
    index/profile.py, which owns that comparison.
    """
    version: int
    cpu_model: str
    cpu_arch: str
    cpu_isa: str
    usable_cpus: float
    gpu_name: Optional[str]
    gpu_vram_mb: Optional[int]
    gpu_capability: Optional[str]
    cuda_runtime: Optional[str]
    #: {runtime: "major.minor"} for the runtimes that can carry inference.
    runtime_versions: dict[str, Optional[str]]
    #: {logical model name: sha256} for the artifacts on disk.
    artifacts: dict[str, Optional[str]]

    def digest(self) -> str:
        """Stable hash of the hard fields, for cheap equality and for logs."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["digest"] = self.digest()
        return d


def fingerprint(artifact_paths: Optional[dict[str, str]] = None) -> Fingerprint:
    """Identify this environment. `artifact_paths` maps a logical model name
    ("detector", "plate_localiser") to the weights file or export directory."""
    cpu = cpu_info()
    gpu = gpu_info()
    rt = runtimes()
    return Fingerprint(
        version=FINGERPRINT_VERSION,
        cpu_model=cpu.model,
        cpu_arch=cpu.arch,
        cpu_isa=cpu.isa,
        usable_cpus=cpu.usable_cpus,
        gpu_name=gpu.name,
        gpu_vram_mb=gpu.vram_mb,
        gpu_capability=gpu.capability,
        cuda_runtime=gpu.cuda_runtime,
        runtime_versions={
            name: _major_minor(info.version) if info.available else None
            for name, info in sorted(rt.items())
        },
        artifacts={
            name: file_digest(path)
            for name, path in sorted((artifact_paths or {}).items())
        },
    )


def snapshot() -> dict[str, Any]:
    """Everything the probe and /health want to show, in one call."""
    cpu = cpu_info()
    gpu = gpu_info()
    return {
        "cpu": asdict(cpu),
        "gpu": asdict(gpu),
        "runtimes": {name: asdict(info) for name, info in runtimes().items()},
        "resolved_default_device": resolve_device(None),
    }
