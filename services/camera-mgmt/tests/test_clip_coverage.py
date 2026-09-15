"""fetch_clip must carry the recorder's coverage report back with the bytes.

The NVR measures whether it actually held footage across the whole requested
window and says so in `X-NVR-Coverage` (server.py `_compute_coverage`). That
header is the only evidence the chain-of-custody certificate's continuity
statement rests on, so the failure modes here are asymmetric: reading a missing
header as full coverage is CORRECT (the NVR omits it when coverage is full),
while reading an unparseable one as full coverage would manufacture a clean
attestation out of a value we did not understand.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import nvr_client  # noqa: E402


class _Resp:
    def __init__(self, headers, status_code=200, content=b"MP4DATA"):
        self.headers = headers
        self.status_code = status_code
        self.content = content


class _Client:
    """Stands in for httpx.AsyncClient as an async context manager."""
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, params=None): return self._resp


@pytest.fixture
def fetch(monkeypatch):
    async def _fetch(headers):
        monkeypatch.setattr(nvr_client.httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(headers)))
        return await nvr_client.fetch_clip(
            "gate-a1b2", "2026-08-01T10:00:00Z", "2026-08-01T10:02:00Z")
    return _fetch


@pytest.mark.asyncio
async def test_no_header_means_full_coverage(fetch):
    """The NVR only sets the header when coverage is PARTIAL."""
    clip = await fetch({})
    assert clip.coverage == 1.0
    assert clip.data == b"MP4DATA"


@pytest.mark.asyncio
async def test_a_partial_coverage_header_is_carried_through(fetch):
    clip = await fetch({"X-NVR-Coverage": "0.72",
                        "X-NVR-Warning": "Partial coverage: recording gap in requested window"})
    assert clip.coverage == 0.72
    assert "gap" in clip.warning


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abc", "1.5", "-0.2", "NaN%"])
async def test_a_header_we_cannot_trust_is_unknown_not_full(fetch, bad):
    """`None` propagates to an "unverified" certificate; 1.0 would propagate to
    a false clean bill of health."""
    clip = await fetch({"X-NVR-Coverage": bad})
    assert clip.coverage is None, f"{bad!r} must not read as measured coverage"


def test_the_parser_accepts_the_boundaries():
    assert nvr_client._parse_coverage("0") == 0.0
    assert nvr_client._parse_coverage("1") == 1.0
    assert nvr_client._parse_coverage("0.999") == pytest.approx(0.999)
