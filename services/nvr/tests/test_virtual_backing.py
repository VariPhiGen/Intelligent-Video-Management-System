"""Which kernels get the "this free space may not be real" caveat?

Docker Desktop backs the container filesystem with a sparse growable image
whose reported free space can exceed the host's — docker_data.vhdx on Windows,
Docker.raw on macOS. The recorder sizes its retention against that number, so an
unflagged one silently fills the host drive.

The first version matched only the WSL strings, so a Mac — the same failure,
the same sparse-image shape — got no caveat at all. These tests pin both
markers and, as much as anything, the NEGATIVE case: a native Linux appliance
(this box, and every real deployment) must not start warning about a disk that
is perfectly real.

Run (from services/nvr): python -m pytest tests -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import capacity  # noqa: E402

# Real /proc/version strings, kept verbatim — the detection is substring
# matching, so a paraphrase would not prove anything about the real thing.
NATIVE = ("Linux version 6.8.0-138-generic (buildd@lcy02-amd64-021) "
          "(x86_64-linux-gnu-gcc-12) #138-Ubuntu SMP PREEMPT_DYNAMIC")
WSL2 = ("Linux version 5.15.153.1-microsoft-standard-WSL2 "
        "(root@941d701f84f1) (gcc (GCC) 11.2.0) #1 SMP")
MACOS = ("Linux version 6.10.14-linuxkit (root@buildkitsandbox) "
         "(gcc (Alpine 13.2.1) ) #1 SMP PREEMPT")


@pytest.fixture(autouse=True)
def _uncached():
    """detect_virtual_backing is lru_cached — a kernel cannot change under a
    running process, but it does change between these tests."""
    capacity.detect_virtual_backing.cache_clear()
    yield
    capacity.detect_virtual_backing.cache_clear()


def _as(monkeypatch, version: str):
    import builtins
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path) == "/proc/version":
            from io import StringIO
            return StringIO(version)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)


def test_a_native_linux_appliance_gets_no_caveat(monkeypatch):
    _as(monkeypatch, NATIVE)
    assert capacity.detect_virtual_backing() is None


def test_docker_desktop_for_windows_is_flagged(monkeypatch):
    _as(monkeypatch, WSL2)
    assert capacity.detect_virtual_backing() == "wsl2"


def test_docker_desktop_for_mac_is_flagged(monkeypatch):
    """The regression: Docker.raw is as unbacked as docker_data.vhdx."""
    _as(monkeypatch, MACOS)
    assert capacity.detect_virtual_backing() == "linuxkit"


def test_a_missing_proc_version_is_not_an_error(monkeypatch):
    def boom(path, *a, **kw):
        raise OSError("no such file")
    monkeypatch.setattr("builtins.open", boom)
    assert capacity.detect_virtual_backing() is None
