"""The re-stamp hop must cover BOTH of a camera's tracks.

`d453bf1` added an FFmpeg hop for recorders whose clock wraps (the :5551 Dahua
wraps a 32-bit microsecond counter every 71 m 35 s, which MediaMTX's DTS
extractor reads as a seek backwards and answers by killing the HLS muxer).

health.py writes the flag under the CAMERA SLUG. relay._path_config reads it
under the PATH NAME it is building — and add_path/patch_path are called once
per TRACK (relay.py register_cameras_bulk, cameras.py sub patch). So for a sub
track the lookup was `stream:restamp:<slug>_sub`, a key nothing ever writes:
the main stream was healed and the sub kept being pulled from the same broken
clock, on the same cycle, with nothing to notice because the health monitor is
deliberately one-row-per-camera (services/tracks.py).

The twelfth instance of the 09-07 sub-track defect, and the same shape: a
per-camera fact consulted on a per-track surface.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import owning_slug, sub_recording_name  # noqa: E402
from backend.services import relay  # noqa: E402

SLUG = "gate-a1b2"
SUB = sub_recording_name(SLUG)


# ── the helper ───────────────────────────────────────────────────────────────

def test_a_sub_recording_name_resolves_to_its_camera():
    assert owning_slug(SUB) == SLUG


def test_a_bare_slug_is_unchanged():
    assert owning_slug(SLUG) == SLUG


def test_round_trip():
    assert owning_slug(sub_recording_name(SLUG)) == SLUG


def test_a_real_slug_never_looks_like_a_sub():
    """generate_slug ends every slug with `-<4 alnum>`, so the suffix test
    cannot misfire on a genuine camera."""
    from backend.models import generate_slug
    for _ in range(50):
        assert not generate_slug("sub").endswith("_sub")


# ── the wiring: which key does the relay actually read? ──────────────────────

@pytest.fixture
def asked(monkeypatch):
    """Capture the slug _path_config looks the flag up by."""
    seen = []

    async def get_restamp(slug):
        seen.append(slug)
        return False

    async def fingerprint(url):
        return ""

    monkeypatch.setattr(relay.redis_client, "get_restamp", get_restamp)
    monkeypatch.setattr(relay.tlsutil, "source_fingerprint", fingerprint)
    return seen


@pytest.mark.asyncio
async def test_the_main_track_is_looked_up_by_its_slug(asked):
    await relay._path_config(SLUG, "rtsp://u:pw@10.0.0.5:554/main")
    assert asked == [SLUG]


@pytest.mark.asyncio
async def test_the_sub_track_is_looked_up_by_ITS_CAMERA(asked):
    """The bug: this asked for `gate-a1b2_sub`, which nothing ever sets."""
    await relay._path_config(SUB, "rtsp://u:pw@10.0.0.5:554/sub")
    assert asked == [SLUG], (
        "the sub track must resolve to its camera; looking the flag up by the "
        "track name reads a key health.py never writes"
    )


@pytest.mark.asyncio
async def test_a_flagged_camera_puts_its_SUB_on_the_hop_too(monkeypatch):
    """Both tracks come off one recorder and therefore one clock."""
    async def flagged(slug):
        return slug == SLUG

    monkeypatch.setattr(relay.redis_client, "get_restamp", flagged)
    cfg = await relay._path_config(SUB, "rtsp://u:pw@10.0.0.5:554/sub")
    assert cfg["source"] == "publisher", "the sub of a flagged camera needs the hop"
    assert "runOnInit" in cfg
    # $MTX_PATH expands per path, so one command string is right for both tracks.
    assert "$MTX_PATH" in cfg["runOnInit"]
    assert "/sub" in cfg["runOnInit"], "the hop must pull the SUB's own URL"


@pytest.mark.asyncio
async def test_an_unflagged_camera_gets_no_hop_on_either_track(monkeypatch):
    """Guard the other way: the hop costs ~300 ms and a healthy camera must not
    pay it on either stream."""
    async def never(slug):
        return False

    monkeypatch.setattr(relay.redis_client, "get_restamp", never)
    monkeypatch.setattr(relay.tlsutil, "source_fingerprint",
                        lambda url: _noop())
    async def _noop():
        return ""
    for name in (SLUG, SUB):
        cfg = await relay._path_config(name, "rtsp://u:pw@10.0.0.5:554/x")
        assert cfg["source"] != "publisher"
        assert "runOnInit" not in cfg
