"""GET /cameras must include cameras that are RECORDING but not yet indexed.

Built from the segment index alone, a camera added a moment ago did not appear
until its first 60 s segment closed and was indexed — while ffmpeg was
demonstrably writing it to disk. Observed on a clean install: the recorder
logged "Started recording worker", the .ts file grew, and this endpoint
returned {"cameras": []} for the next ~90 s.

The cost is not cosmetic. camera-mgmt's reconcile reads "absent from this list"
as "needs adding", so it re-POSTed every cycle and took a 409 each time:

    08:31:59 POST /cameras/<slug> 200      (added, worker started)
    08:32:03 POST /cameras/<slug> 409
    08:33:03 POST /cameras/<slug> 409

Idempotent, so nothing broke — but the loop could never converge, and a REAL
add failure looks identical from camera-mgmt's side: both end at
`nvr.sync.add.ok`.

Run (from services/nvr): python -m pytest tests -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import server  # noqa: E402


class _Index:
    """Only what list_cameras touches."""
    def __init__(self, cameras): self._c = list(cameras)
    def get_cameras(self): return list(self._c)
    def get_recording_range(self, cam): return None
    def total_size(self, cam): return 0


class _Engine:
    def __init__(self, workers):
        self._workers = {w: SimpleNamespace(rtsp_url=f"rtsp://127.0.0.1:8654/{w}")
                         for w in workers}
    def get_status(self):
        return {w: {"alive": True, "last_indexed_at": None,
                    "index_lag_seconds": None, "segments_indexed": 0,
                    "current_backoff": None} for w in self._workers}


@pytest.fixture
def wired(monkeypatch):
    def setup(indexed, recording):
        monkeypatch.setattr(server, "_index", _Index(indexed))
        monkeypatch.setattr(server, "_engine", _Engine(recording))
    return setup


async def _names(**kw):
    return [c["name"] for c in (await server.list_cameras())["cameras"]]


@pytest.mark.asyncio
async def test_a_camera_recording_but_not_yet_indexed_is_listed(wired):
    """The whole finding: the first 60-90 s of every camera's life."""
    wired(indexed=[], recording=["gate-a1b2"])
    assert await _names() == ["gate-a1b2"]


@pytest.mark.asyncio
async def test_it_is_reported_as_recording_not_as_a_husk(wired):
    wired(indexed=[], recording=["gate-a1b2"])
    cam = (await server.list_cameras())["cameras"][0]
    assert cam["recording"] is True
    assert cam["rtsp_url"].endswith("gate-a1b2")


@pytest.mark.asyncio
async def test_indexed_footage_from_a_stopped_camera_still_shows(wired):
    """The other half of the union: a camera whose recorder is gone but whose
    footage is on disk must not vanish from the listing — playback and the
    retention UI both read this."""
    wired(indexed=["old-cam-x1y2"], recording=[])
    assert await _names() == ["old-cam-x1y2"]


@pytest.mark.asyncio
async def test_the_two_sets_are_merged_without_duplicates(wired):
    """The steady state: indexed AND recording is one camera, not two rows."""
    wired(indexed=["gate-a1b2"], recording=["gate-a1b2"])
    assert await _names() == ["gate-a1b2"]


@pytest.mark.asyncio
async def test_both_sets_appear_together(wired):
    wired(indexed=["old-cam-x1y2"], recording=["gate-a1b2"])
    assert await _names() == ["gate-a1b2", "old-cam-x1y2"]


@pytest.mark.asyncio
async def test_no_engine_at_all_still_lists_indexed_footage(wired, monkeypatch):
    """The API can serve before the engine is constructed; it must not raise."""
    monkeypatch.setattr(server, "_index", _Index(["old-cam-x1y2"]))
    monkeypatch.setattr(server, "_engine", None)
    assert await _names() == ["old-cam-x1y2"]
