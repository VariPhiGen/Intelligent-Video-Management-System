"""Capability gating on the NVR reverse-proxy.

The proxy maps a request path to the capability it requires. HLS playback added
a new path head (`hls/...`) that serves recorded footage byte-for-byte, so it
has to demand the same capability as `clip` — a head that falls through this
map is served to any authenticated principal, which for footage would be a
silent authorisation hole rather than a visible error.
"""
import pytest

from backend.routers.nvr import _read_capability


@pytest.mark.parametrize("path", [
    "hls/cam-1/index.m3u8?from=x&to=y",
    "hls/cam-1/ts/cam-1_20260904_173700.f30cf3b7.ts",
    "hls/cam-1/fmp4/cam-1_20260904_173700.f30cf3b7.m4s",
    "clip?camera=cam-1",
    "snapshot?camera=cam-1",
    "coverage?camera=cam-1",
    "cameras",
])
def test_footage_paths_require_playback_search(path):
    assert _read_capability(path) == "playback_search"


@pytest.mark.parametrize("path,expected", [
    ("report/uptime", "export_reports"),
    ("health", None),          # monitoring, not footage
    ("storage", None),
])
def test_other_heads_keep_their_gating(path, expected):
    assert _read_capability(path) == expected
