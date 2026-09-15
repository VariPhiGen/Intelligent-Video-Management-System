"""Hardware detection and the calibration fingerprint.

WHAT IS ACTUALLY BEING TESTED. Not "does this machine have a GPU" — that is a
property of the runner, not of the code, and a test asserting it would pass or
fail for the wrong reason. What matters is that every probe DEGRADES rather than
raises when a runtime is missing, and that the fingerprint changes for exactly
the environment differences that change an inference decision and for no others.

The CPU-count case is the one with a real bug behind it: os.cpu_count() reports
the host's processors, so a container limited to two cores on a 32-core host
reports 32 and any profile built on that number is calibrated for hardware the
service does not have.

Run: python3 -m pytest tests -q   (from services/smartsearch)
"""
from __future__ import annotations

import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index import hardware                                        # noqa: E402


# ── CPU ──────────────────────────────────────────────────────────────────────
def test_cpu_info_is_populated_and_self_consistent():
    cpu = hardware.cpu_info()
    assert cpu.model and cpu.arch
    assert cpu.host_cpus >= 1
    assert cpu.usable_cpus > 0
    # Whatever the platform, we can never be allowed MORE than the host has.
    assert cpu.usable_cpus <= cpu.host_cpus
    assert cpu.constrained == (cpu.usable_cpus < cpu.host_cpus)
    assert cpu.isa in ("baseline",) + hardware._ISA_ORDER


def test_cgroup_v2_quota_bounds_the_usable_cpu_count(tmp_path, monkeypatch):
    """A container limited below the host count must report the limit.

    This is the whole reason usable_cpus exists as a separate field: budgeting
    a calibration against host cores on a limited container plans for hardware
    that is not there.
    """
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("150000 100000")            # 1.5 cores
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path) == "/sys/fs/cgroup/cpu.max":
            return real_open(cpu_max, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    monkeypatch.setattr(hardware, "_affinity_count", lambda: 32)

    assert hardware._cgroup_quota() == pytest.approx(1.5)
    cpu = hardware.cpu_info()
    assert cpu.usable_cpus == pytest.approx(1.5)
    assert cpu.host_cpus == 32
    assert cpu.constrained is True


def test_unlimited_cgroup_quota_reads_as_none(tmp_path, monkeypatch):
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("max 100000")
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **kw: (
        real_open(cpu_max, *a, **kw) if str(p) == "/sys/fs/cgroup/cpu.max"
        else real_open(p, *a, **kw)))
    assert hardware._cgroup_quota() is None


def test_affinity_bounds_the_usable_cpu_count(monkeypatch):
    """Affinity alone constrains us even where no cgroup quota is set."""
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    monkeypatch.setattr(hardware, "_affinity_count", lambda: 4)
    monkeypatch.setattr(hardware, "_cgroup_quota", lambda: None)
    assert hardware.cpu_info().usable_cpus == 4


def test_unreadable_cgroup_files_do_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no cgroup here")
    monkeypatch.setattr(builtins, "open", boom)
    assert hardware._cgroup_quota() is None


# ── runtimes ─────────────────────────────────────────────────────────────────
def test_every_runtime_probe_reports_rather_than_raises():
    """A missing runtime is a finding. The probe must never be what fails."""
    found = hardware.runtimes()
    for name in ("torch", "ultralytics", "onnxruntime", "openvino",
                 "open_clip", "fast_plate_ocr"):
        assert name in found
        info = found[name]
        assert isinstance(info.available, bool)
        if info.available:
            assert info.version
        else:
            assert info.error, f"{name} unavailable but gave no reason"


def test_absent_runtime_is_recorded_not_raised(monkeypatch):
    real_import = builtins.__import__

    def no_openvino(name, *a, **kw):
        if name == "openvino":
            raise ImportError("No module named 'openvino'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_openvino)
    info = hardware._probe_openvino()
    assert info.available is False
    assert "openvino" in info.error


def test_gpu_probe_survives_torch_being_absent(monkeypatch):
    real_import = builtins.__import__

    def no_torch(name, *a, **kw):
        if name == "torch":
            raise ImportError("No module named 'torch'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_torch)
    gpu = hardware.gpu_info()
    assert gpu.present is False
    assert "torch" in gpu.error


# ── device resolution ────────────────────────────────────────────────────────
def test_explicit_device_preference_is_honoured_verbatim():
    """A site that asked for CUDA and silently got CPU has a fault to find.
    Resolution must not 'helpfully' downgrade a stated preference."""
    assert hardware.resolve_device("cpu") == "cpu"
    assert hardware.resolve_device("cuda") == "cuda"
    assert hardware.resolve_device("cuda:1") == "cuda:1"


def test_absent_preference_resolves_to_a_concrete_device():
    resolved = hardware.resolve_device(None)
    assert resolved in ("cpu", "cuda")
    # The point of resolving at all: never None, always nameable in /health.
    assert resolved is not None


# ── fingerprint ──────────────────────────────────────────────────────────────
def test_fingerprint_is_stable_across_calls():
    a = hardware.fingerprint()
    b = hardware.fingerprint()
    assert a.digest() == b.digest()


def test_fingerprint_changes_when_the_gpu_changes(monkeypatch):
    base = hardware.fingerprint()
    monkeypatch.setattr(hardware, "gpu_info", lambda: hardware.GpuInfo(
        present=True, name="NVIDIA RTX 2000 Ada", vram_mb=16380,
        capability="8.9", cuda_runtime="12.1"))
    changed = hardware.fingerprint()
    assert changed.digest() != base.digest()
    assert changed.gpu_name == "NVIDIA RTX 2000 Ada"


def test_fingerprint_changes_when_the_isa_changes(monkeypatch):
    """AVX-512 versus AVX2 changes CPU inference throughput enough that a
    profile fitted on one is not evidence about the other."""
    monkeypatch.setattr(hardware, "cpu_info", lambda: hardware.CpuInfo(
        model="Xeon", arch="x86_64", host_cpus=8, usable_cpus=8.0,
        isa="avx2", constrained=False))
    avx2 = hardware.fingerprint().digest()
    monkeypatch.setattr(hardware, "cpu_info", lambda: hardware.CpuInfo(
        model="Xeon", arch="x86_64", host_cpus=8, usable_cpus=8.0,
        isa="avx512f", constrained=False))
    assert hardware.fingerprint().digest() != avx2


def test_patch_versions_do_not_change_the_fingerprint():
    """The narrowness is the point: fingerprinting every patch release turns a
    routine rebuild into a hardware change, and an unattended appliance then
    behaves differently after a restart nobody asked for."""
    assert hardware._major_minor("2.13.0+cu121") == "2.13"
    assert hardware._major_minor("2.13.4") == "2.13"
    assert hardware._major_minor("2.13.0") == hardware._major_minor("2.13.9")
    assert hardware._major_minor("2.14.0") != hardware._major_minor("2.13.0")
    assert hardware._major_minor(None) is None


def test_artifact_digest_tracks_content_not_name(tmp_path):
    a, b = tmp_path / "w.pt", tmp_path / "other.pt"
    a.write_bytes(b"weights-v1")
    b.write_bytes(b"weights-v1")
    assert hardware.file_digest(str(a)) == hardware.file_digest(str(b))
    a.write_bytes(b"weights-v2")
    assert hardware.file_digest(str(a)) != hardware.file_digest(str(b))


def test_missing_artifact_digests_as_none_rather_than_raising(tmp_path):
    assert hardware.file_digest(str(tmp_path / "never-downloaded.pt")) is None


def test_directory_artifacts_hash_reproducibly(tmp_path):
    """An OpenVINO export is a directory, not a file. Two exports producing the
    same model must fingerprint the same."""
    for d in ("ir_a", "ir_b"):
        p = tmp_path / d
        p.mkdir()
        (p / "model.xml").write_text("<net/>")
        (p / "model.bin").write_bytes(b"\x00\x01")
    assert (hardware.file_digest(str(tmp_path / "ir_a"))
            == hardware.file_digest(str(tmp_path / "ir_b")))
    (tmp_path / "ir_b" / "model.bin").write_bytes(b"\x00\x02")
    assert (hardware.file_digest(str(tmp_path / "ir_a"))
            != hardware.file_digest(str(tmp_path / "ir_b")))


def test_fingerprint_tracks_artifact_changes(tmp_path):
    w = tmp_path / "yolov8n.pt"
    w.write_bytes(b"v1")
    before = hardware.fingerprint({"detector": str(w)}).digest()
    w.write_bytes(b"v2")
    assert hardware.fingerprint({"detector": str(w)}).digest() != before


def test_fingerprint_version_bump_invalidates_everything(monkeypatch):
    before = hardware.fingerprint().digest()
    monkeypatch.setattr(hardware, "FINGERPRINT_VERSION",
                        hardware.FINGERPRINT_VERSION + 1)
    assert hardware.fingerprint().digest() != before


def test_snapshot_is_json_serialisable():
    """It goes into /diagnostics/hardware and into a persisted profile."""
    import json
    json.dumps(hardware.snapshot(), default=str)
    json.dumps(hardware.fingerprint().to_dict(), default=str)
