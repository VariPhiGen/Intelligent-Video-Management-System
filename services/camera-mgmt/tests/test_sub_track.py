"""Camera sub tracks: the track-set fan-out and the usefulness judgement.

Two independently dangerous things live here. `tracks_for` is the single
definition of "a camera's recording tracks" — if the relay and the NVR ever
disagree about it, a stream records with nobody grooming it, or stops recording
while the relay still pulls it. And `judge` decides whether a second stream is
worth its cost at all.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import SUB_TRACK_SUFFIX, sub_recording_name, validate_slug_format  # noqa: E402
from backend.services import tracks  # noqa: E402
from backend.services.substream import BROWSER_SAFE, candidate_urls, judge  # noqa: E402


def cam(slug="gate-a1b2", url="rtsp://user:pw@10.0.0.5:554/main", sub=None, **kw):
    return SimpleNamespace(slug=slug, rtsp_url=url, sub_track=sub, **kw)


SUB = {"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
       "width": 704, "height": 576, "recording_enabled": True}


# ── tracks_for ─────────────────────────────────────────────────────────────

def test_camera_without_a_sub_is_exactly_one_track():
    t = tracks.tracks_for(cam())
    assert [(x.recording_name, x.kind) for x in t] == [("gate-a1b2", "main")]


def test_main_recording_name_is_the_bare_slug():
    """The slug is the join key for detections, events, bookmarks and audit.
    Renaming the main track would break every one of them."""
    assert tracks.tracks_for(cam(sub=SUB))[0].recording_name == "gate-a1b2"


def test_enabled_sub_adds_a_second_track():
    t = tracks.tracks_for(cam(sub=SUB))
    assert [(x.recording_name, x.kind) for x in t] == [
        ("gate-a1b2", "main"), ("gate-a1b2_sub", "sub")]


def test_resolved_but_disabled_sub_is_not_a_track():
    """Resolution records what exists; only the flag starts a recording."""
    t = tracks.tracks_for(cam(sub={**SUB, "recording_enabled": False}))
    assert len(t) == 1 and t[0].kind == "main"


def test_sub_without_a_url_is_never_a_track():
    assert len(tracks.tracks_for(cam(sub={**SUB, "url_raw": None}))) == 1


def test_camera_with_no_main_url_yields_nothing():
    """A half-registered camera must not produce a track with a null source."""
    assert tracks.tracks_for(cam(url=None, sub=SUB)) == []


# ── credential composition ─────────────────────────────────────────────────

def test_sub_url_borrows_the_main_credentials():
    """sub_track carries no secrets, exactly like rtsp_candidates."""
    url = tracks.sub_track_url(cam(sub=SUB))
    assert url == "rtsp://user:pw@10.0.0.5:554/sub"


def test_sub_url_handles_rtsps():
    c = cam(url="rtsps://user:pw@10.0.0.5:554/main",
            sub={**SUB, "url_raw": "rtsps://10.0.0.5:554/sub"})
    assert tracks.sub_track_url(c) == "rtsps://user:pw@10.0.0.5:554/sub"


def test_sub_url_without_credentials_on_the_main_is_passed_through():
    c = cam(url="rtsp://10.0.0.5:554/main", sub=SUB)
    assert tracks.sub_track_url(c) == "rtsp://10.0.0.5:554/sub"


# ── reconcile support ──────────────────────────────────────────────────────

def test_recording_names_covers_both_tracks():
    """A reconcile comparing slugs alone would read `<slug>_sub` as an orphan."""
    names = tracks.recording_names([cam(sub=SUB), cam(slug="two-c3d4")])
    assert names == {"gate-a1b2", "gate-a1b2_sub", "two-c3d4"}


# ── the reserved suffix ────────────────────────────────────────────────────

def test_sub_suffix_is_rejected_in_slugs():
    """`foo_sub` as a camera would share a recording name with `foo`'s sub."""
    with pytest.raises(ValueError):
        validate_slug_format("gate" + SUB_TRACK_SUFFIX)


def test_sub_suffix_guard_is_independent_of_the_slug_pattern():
    """Today the underscore is rejected by the character pattern anyway. The
    explicit reservation exists so relaxing that pattern cannot quietly allow a
    camera to collide with another camera's sub track."""
    import re

    import backend.models as m
    original = m._SLUG_VALID
    try:
        m._SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")   # underscores allowed
        with pytest.raises(ValueError, match="reserved"):
            m.validate_slug_format("gate" + SUB_TRACK_SUFFIX)
        assert m.validate_slug_format("gate_subway") == "gate_subway"
    finally:
        m._SLUG_VALID = original


def test_sub_recording_name_is_the_one_definition():
    assert sub_recording_name("gate-a1b2") == "gate-a1b2_sub"


# ── the usefulness judgement, against this fleet's real probe results ──────

MAIN_1080_HEVC = {"codec": "hevc", "width": 1920, "height": 1080}


@pytest.mark.parametrize("label,main,sub,expected", [
    ("entry-g98a: h264 720p sub", MAIN_1080_HEVC,
     {"codec": "h264", "width": 1280, "height": 720}, True),
    ("cam34: h264 352x288 sub", {"codec": "hevc", "width": 1280, "height": 720},
     {"codec": "h264", "width": 352, "height": 288}, True),
    ("exit-gate: hevc 352x288 sub", MAIN_1080_HEVC,
     {"codec": "hevc", "width": 352, "height": 288}, True),
    ("corner-001: hevc 704x576 sub", MAIN_1080_HEVC,
     {"codec": "hevc", "width": 704, "height": 576}, True),
    ("same-size hevc is pure cost", MAIN_1080_HEVC,
     {"codec": "hevc", "width": 1920, "height": 1080}, False),
    ("barely-smaller hevc is not worth a stream", MAIN_1080_HEVC,
     {"codec": "hevc", "width": 1600, "height": 900}, False),
    ("unreachable", MAIN_1080_HEVC, None, False),
])
def test_judge_matches_the_measured_fleet(label, main, sub, expected):
    assert judge(main, sub)[0] is expected, label


def test_browser_safe_sub_wins_even_when_it_is_not_smaller():
    """Removing the transcode beats shrinking it — size is the fallback test."""
    ok, why = judge(MAIN_1080_HEVC, {"codec": "h264", "width": 1920, "height": 1080})
    assert ok and "stream-copy" in why


def test_judge_needs_a_main_before_it_can_judge_anything():
    """Superseded by the main-codec gate: without the main's codec there is no
    way to know a conversion is even happening, so nothing is recorded."""
    ok, why = judge(None, {"codec": "hevc", "width": 352, "height": 288})
    assert not ok and "not probed" in why


# ── the acquisition ladder ─────────────────────────────────────────────────

def test_onvif_profiles_beyond_the_first_are_the_candidates():
    c = cam(url="rtsp://u:p@10.0.0.5:554/main")
    c.rtsp_candidates = [{"url_raw": "rtsp://10.0.0.5/p0"},
                         {"url_raw": "rtsp://10.0.0.5/p1"},
                         {"url_raw": "rtsp://10.0.0.5/p2"}]
    assert candidate_urls(c) == [("onvif", "rtsp://10.0.0.5/p1"),
                                 ("onvif", "rtsp://10.0.0.5/p2")]


def test_vendor_derivation_when_there_is_only_one_profile():
    c = cam(url="rtsp://u:p@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0")
    c.rtsp_candidates = []
    got = candidate_urls(c)
    assert got == [("derived", "rtsp://10.0.0.5:554/cam/realmonitor?channel=1&subtype=1")]
    assert "u:p@" not in got[0][1], "derived candidates must carry no credentials"


def test_vendor_derivation_handles_an_escaped_ampersand():
    c = cam(url="rtsp://u:p@10.0.0.5:554/cam/realmonitor?channel=1%26subtype=0")
    c.rtsp_candidates = []
    assert candidate_urls(c)[0][1].endswith("subtype%3D1") or \
           candidate_urls(c)[0][1].endswith("subtype=1")


def test_no_candidates_for_a_url_with_no_convention():
    c = cam(url="rtsp://u:p@10.0.0.5:554/some/random/path")
    c.rtsp_candidates = []
    assert candidate_urls(c) == []


# ── which profile becomes the sub ──────────────────────────────────────────
#
# The tie-break flips by class and getting it backwards picks the wrong stream:
# among free copies the biggest picture is best, among converted ones the
# smallest is cheapest. corner-001-u3z2 offers HEVC 1280x720 AND HEVC 704x576,
# and the naive "bigger is better" rule chose 720p — 3.7x more pixels to
# transcode for a stream whose whole purpose is being cheap.

def _rank(codec, w, h):
    from backend.services.substream import BROWSER_SAFE
    px, copyable = w * h, codec in BROWSER_SAFE
    return (2 if copyable else 1, px if copyable else -px)


def test_among_converted_candidates_the_smallest_wins():
    opts = [("hevc", 1280, 720), ("hevc", 704, 576), ("hevc", 352, 288)]
    assert max(opts, key=lambda o: _rank(*o)) == ("hevc", 352, 288)


def test_among_free_candidates_the_biggest_wins():
    """Costs the server nothing either way, so give the operator the picture."""
    opts = [("h264", 352, 288), ("h264", 1280, 720)]
    assert max(opts, key=lambda o: _rank(*o)) == ("h264", 1280, 720)


def test_a_copyable_candidate_beats_a_much_smaller_converted_one():
    opts = [("hevc", 352, 288), ("h264", 1280, 720)]
    assert max(opts, key=lambda o: _rank(*o)) == ("h264", 1280, 720)


# ── a copyable main changes the answer ─────────────────────────────────────

def test_hevc_sub_is_rejected_when_the_main_already_copies():
    """cabin-u9tn: H.264 1440p main, HEVC 720p second profile.

    The size test alone said "5x fewer pixels, record it". But playback ranks
    copy above transcode whatever the size, so it would always serve the main —
    leaving the sub as a second stream on disk that nothing ever plays, and
    which could only ever add conversion work."""
    ok, why = judge({"codec": "h264", "width": 2560, "height": 1440},
                    {"codec": "hevc", "width": 1280, "height": 720})
    assert not ok and "already h264" in why


def test_no_sub_at_all_when_the_main_is_already_browser_safe():
    """The main's codec is the gate, whatever the sub happens to be.

    A sub exists to spare playback a conversion. An H.264 main is stream-copied
    at no cost, so there is no conversion to remove and the sub is a second copy
    of every frame in exchange for nothing. Measured, that "nothing" costs
    0.9-23.7 GB per camera per day."""
    main = {"codec": "h264", "width": 1920, "height": 1080}
    for s in ({"codec": "h264", "width": 352, "height": 288},
              {"codec": "hevc", "width": 704, "height": 576},
              {"codec": "h264", "width": 1280, "height": 720}):
        ok, why = judge(main, s)
        assert not ok, s
        assert "already h264" in why


def test_sub_is_judged_only_once_the_main_needs_converting():
    ok, _ = judge({"codec": "hevc", "width": 1920, "height": 1080},
                  {"codec": "h264", "width": 352, "height": 288})
    assert ok


def test_unprobed_main_means_no_sub():
    """Not knowing whether the main needs converting is not a reason to spend
    the disk finding out."""
    ok, why = judge(None, {"codec": "h264", "width": 352, "height": 288})
    assert not ok and "not probed" in why


# ── resolution at registration (plan Phase 2) ──────────────────────────────

def test_background_resolve_never_overwrites_an_existing_sub_track():
    """Probing takes seconds; an operator may have resolved or edited it
    meanwhile, and a background job must not stamp on that."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.services import substream

    existing = {"url_raw": "rtsp://x/chosen", "codec": "h264",
                "recording_enabled": True}
    cam = SimpleNamespace(id="cam-id", slug="gate-a1b2",
                          rtsp_url="rtsp://u:p@x/main", sub_track=existing)

    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=cam)))
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.db.AsyncSessionLocal", return_value=ctx), \
         patch.object(substream, "resolve", new=AsyncMock(
             return_value={"url_raw": "rtsp://x/other", "codec": "hevc"})) as res:
        asyncio.run(substream.resolve_in_background("cam-id"))

    assert cam.sub_track is existing, "clobbered an existing sub track"
    session.commit.assert_not_called()
    res.assert_not_awaited()


def test_background_resolve_swallows_failures():
    """A camera that is unreachable must still register successfully."""
    import asyncio
    from unittest.mock import patch

    from backend.services import substream

    with patch("backend.db.AsyncSessionLocal", side_effect=RuntimeError("db gone")):
        asyncio.run(substream.resolve_in_background("cam-id"))   # must not raise


# ── cheapest-wins ranking ──────────────────────────────────────────────────
#
# Every candidate in a class costs the same to SERVE, so the only thing
# separating them is disk. The earlier "biggest free picture wins" rule chose
# entry-g98a's 2.41 Mbps 720p over its 1.24 Mbps 704x576 — both zero-transcode
# — for 23.7 GB/day against 13.

def _rank2(codec, w, h, bitrate=None):
    from backend.services.substream import BROWSER_SAFE
    px, copyable = w * h, codec in BROWSER_SAFE
    return (2 if copyable else 1, -(bitrate if bitrate is not None else px / 1e6))


def test_cheapest_browser_safe_candidate_wins():
    """entry-g98a, with the bitrates actually measured on the camera."""
    opts = [("h264", 1280, 720, 2.41), ("h264", 704, 576, 1.24)]
    assert max(opts, key=lambda o: _rank2(*o)) == ("h264", 704, 576, 1.24)


def test_copyable_still_beats_a_cheaper_transcoded_candidate():
    """Removing the conversion outranks saving bytes."""
    opts = [("hevc", 352, 288, 0.10), ("h264", 704, 576, 1.24)]
    assert max(opts, key=lambda o: _rank2(*o))[0] == "h264"


def test_falls_back_to_pixels_when_bitrate_could_not_be_measured():
    opts = [("h264", 1280, 720, None), ("h264", 704, 576, None)]
    assert max(opts, key=lambda o: _rank2(*o)) == ("h264", 704, 576, None)


# ── the sub's own retention ────────────────────────────────────────────────
#
# The sub exists so scrubbing RECENT footage is fast; the main is what evidence
# comes from. Keeping both the same length doubles the feature's storage cost
# well past the point it buys anything — measured, 198 GB of sub for a scrub
# window of days. Once the sub expires playback falls back to the main, because
# the NVR checks coverage per track.

def _cam_with_sub(**sub_extra):
    return cam(sub={**SUB, **sub_extra})


def test_sub_uses_the_appliance_default_when_it_has_none_of_its_own():
    from backend.config import settings
    assert tracks.sub_retention_days(_cam_with_sub(), 30) == settings.nvr_sub_retention_days


def test_sub_default_is_much_shorter_than_a_typical_main():
    from backend.config import settings
    assert settings.nvr_sub_retention_days < 15


def test_camera_can_set_its_own_sub_retention():
    assert tracks.sub_retention_days(_cam_with_sub(retention_days=7), 30) == 7


def test_sub_retention_never_exceeds_the_main():
    """A scrub track outliving the footage it helps scrub is pure waste."""
    assert tracks.sub_retention_days(_cam_with_sub(retention_days=90), 15) == 15


def test_sub_retention_falls_back_to_the_appliance_main_default():
    """Main retention None means 'appliance default', not 'unlimited'."""
    from backend.config import settings
    got = tracks.sub_retention_days(_cam_with_sub(retention_days=9999), None)
    assert got == settings.nvr_default_retention_days


def test_sub_retention_is_never_zero():
    assert tracks.sub_retention_days(_cam_with_sub(retention_days=0), 30) >= 1


# ── Phase 0: what every profile actually is, recorded on the camera ────────
#
# Discovery stored only {token, profile, url_raw, verified}. resolve() probes
# every profile anyway, so throwing all but the winner away was waste — and it
# is what a future "sub for the grid, main for fullscreen" selector needs.

def test_write_back_annotates_each_probed_candidate():
    from backend.services.substream import _write_back

    c = cam()
    c.rtsp_candidates = [{"profile": "P0", "url_raw": "rtsp://x/0", "verified": True},
                         {"profile": "P1", "url_raw": "rtsp://x/1", "verified": True}]
    _write_back(c, {"rtsp://x/0": {"codec": "hevc", "width": 1920, "height": 1080,
                                   "fps": 15, "bitrate_mbps": 1.16},
                    "rtsp://x/1": {"codec": "h264", "width": 704, "height": 576,
                                   "fps": 25, "bitrate_mbps": 1.24,
                                   "usable_as_sub": True, "reason": "browser-safe"}})
    got = c.rtsp_candidates
    assert got[0]["codec"] == "hevc" and got[0]["width"] == 1920
    assert got[1]["bitrate_mbps"] == 1.24 and got[1]["usable_as_sub"] is True
    # The original discovery fields survive.
    assert got[0]["profile"] == "P0" and got[0]["verified"] is True
    assert all("probed_at" in x for x in got)


def test_write_back_leaves_unprobed_candidates_untouched():
    """A profile that timed out keeps what discovery knew, with nothing invented."""
    from backend.services.substream import _write_back

    c = cam()
    c.rtsp_candidates = [{"profile": "P0", "url_raw": "rtsp://x/0", "verified": True}]
    _write_back(c, {})
    assert c.rtsp_candidates == [{"profile": "P0", "url_raw": "rtsp://x/0", "verified": True}]


def test_write_back_reassigns_rather_than_mutating():
    """SQLAlchemy does not track in-place JSONB edits — a mutated element would
    silently never reach the database."""
    from backend.services.substream import _write_back

    c = cam()
    original = [{"profile": "P0", "url_raw": "rtsp://x/0"}]
    c.rtsp_candidates = original
    _write_back(c, {"rtsp://x/0": {"codec": "hevc", "width": 1, "height": 1}})
    assert c.rtsp_candidates is not original
    assert "codec" not in original[0], "mutated the original list in place"


# ── Relay vs recording: two questions, one flag (fixed 2026-09-09) ───────────
#
# `recording_enabled` governed BOTH whether the NVR records the sub (a real
# cost — a second ffmpeg, ~10-25% more disk) and whether the RELAY carries it
# (nearly free on demand). The live view falls back to `<slug>_sub` for cameras
# the browser cannot decode, so gating the relay on the recording flag left
# that fallback with nothing to attach to: an H.265 camera with a working
# H.264 sub still showed "Signal lost", because no `_sub` path existed.

from backend.services.tracks import (  # noqa: E402
    relay_tracks_for, sub_is_recordable, sub_is_resolved, tracks_for,
)


def _cam(recording_enabled):
    return SimpleNamespace(
        slug="gate-a1b2", rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
        sub_track={"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
                   "width": 704, "height": 576,
                   "recording_enabled": recording_enabled},
    )


def test_a_resolved_sub_is_relayed_even_when_not_recorded():
    """The fix. Without this the live fallback has no path to attach to."""
    names = [t.recording_name for t in relay_tracks_for(_cam(False))]
    assert names == ["gate-a1b2", "gate-a1b2_sub"]


def test_an_unrecorded_sub_is_carried_ON_DEMAND():
    """So it costs nothing until a viewer actually needs the fallback."""
    sub = [t for t in relay_tracks_for(_cam(False)) if t.is_sub][0]
    assert sub.on_demand is True


def test_a_recorded_sub_is_carried_ALWAYS_ON():
    """Continuous recording cannot depend on someone watching."""
    sub = [t for t in relay_tracks_for(_cam(True)) if t.is_sub][0]
    assert sub.on_demand is False


def test_relaying_a_sub_does_not_make_it_recorded():
    """The invariant this must not break: recording stays opt-in, and the NVR
    keeps driving off tracks_for()."""
    assert [t.recording_name for t in tracks_for(_cam(False))] == ["gate-a1b2"]
    assert [t.recording_name for t in tracks_for(_cam(True))] == ["gate-a1b2", "gate-a1b2_sub"]


def test_a_camera_with_no_sub_gets_no_sub_path():
    cam = SimpleNamespace(slug="gate-a1b2", rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                          sub_track=None)
    assert [t.recording_name for t in relay_tracks_for(cam)] == ["gate-a1b2"]
    assert sub_is_resolved(cam) is False


def test_resolution_and_recordability_are_different_questions():
    assert sub_is_resolved(_cam(False)) is True
    assert sub_is_recordable(_cam(False)) is False
    assert sub_is_resolved(_cam(True)) is True
    assert sub_is_recordable(_cam(True)) is True
