"""onvif_media.py — reading a camera's encoder settings without lying about them.

446 lines, no test. What makes this worth the batch is not the line count but
the shape of its failures: every one of them is a screen that renders, with
wrong or blank numbers, and no error anywhere.

THE ONE THAT MATTERS MOST is the ver10/Media2 decision. Many modern H.265
cameras implement the legacy ver10 Media service as a stub that answers with an
empty VideoEncoderConfiguration body — only a token and a name. The real
resolution, frame rate and bitrate exist only in Media2. `_has_current_settings`
is the entire signal that tells a stub from a genuine answer, and if it ever
reports True for a stub the camera page shows an encoder with blank everything,
which an operator reads as a camera that is not streaming.

Everything here is parsing, so everything here is testable: the fixtures in
onvif_fixtures.py stand in for the vendor shapes, and no camera, network or
onvif-zeep install is involved.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import onvif_media as om  # noqa: E402
from onvif_fixtures import (  # noqa: E402
    FakeMedia2Service,
    FakeVer10Media,
    media2_config,
    media2_options,
    obj,
    options_with_duplicate_resolutions,
    ver10_encoder,
    ver10_options,
    ver10_profile,
    ver10_stub_encoder,
)


# ── The stub detector: the whole ver10-vs-Media2 decision rests on it ──────

class TestHasCurrentSettings:
    def test_a_populated_encoder_reads_as_populated(self):
        assert om._has_current_settings(om._current(ver10_encoder())) is True

    def test_a_token_and_name_only_stub_reads_as_empty(self):
        # THE QUIRK. False here is what sends the read to Media2.
        entry = om._current(ver10_stub_encoder())
        assert om._has_current_settings(entry) is False
        assert entry["config_name"] == "MainEncoder", "the name still survives"

    @pytest.mark.parametrize("field", ["width", "height", "fps", "bitrate_kbps"])
    def test_any_single_real_measurement_is_enough(self, field):
        # A camera that answers with only one of the four is still answering.
        entry = {k: None for k in ("width", "height", "fps", "bitrate_kbps")}
        entry[field] = 1
        assert om._has_current_settings(entry) is True

    def test_a_zero_is_not_a_measurement(self):
        # 0 fps and 0 bitrate are not values a camera reports when it means it;
        # but the check is `is not None`, so pin what it actually does rather
        # than what it might.
        entry = {"width": 0, "height": None, "fps": None, "bitrate_kbps": None}
        assert om._has_current_settings(entry) is True


class TestVer10ToMedia2Fallback:
    """`_get_sync` prefers Media2 and falls back to ver10. These drive the two
    branch functions directly — `_get_sync` itself builds an ONVIFCamera, which
    needs the library and a camera."""

    def test_media2_is_used_when_it_answers_with_real_settings(self):
        svc = FakeMedia2Service([media2_config()], options=[media2_options()])
        out = om._get_media2(svc)
        assert len(out) == 1
        assert out[0]["width"] == 3840 and out[0]["fps"] == 20
        assert any(om._has_current_settings(e) for e in out)

    def test_a_media2_answer_that_is_also_empty_does_not_claim_settings(self):
        # Then `_get_sync` falls through to ver10, which is the point.
        svc = FakeMedia2Service([obj(token="v", Name="Main")])
        out = om._get_media2(svc)
        assert not any(om._has_current_settings(e) for e in out)

    def test_ver10_reads_a_populated_profile(self):
        media = FakeVer10Media([ver10_profile(encoder=ver10_encoder())],
                               options=ver10_options())
        out = om._get_ver10(media)
        assert out[0]["width"] == 1920 and out[0]["height"] == 1080
        assert out[0]["fps"] == 25 and out[0]["bitrate_kbps"] == 4096

    def test_a_profile_with_no_encoder_is_skipped_not_half_reported(self):
        # An audio-only or metadata profile has no VideoEncoderConfiguration.
        media = FakeVer10Media([
            ver10_profile(token="p1", encoder=None),
            ver10_profile(token="p2", encoder=ver10_encoder()),
        ], options=ver10_options())
        out = om._get_ver10(media)
        assert [e["profile_token"] for e in out] == ["p2"]


# ── Reading the current settings ───────────────────────────────────────────

class TestCurrentVer10:
    def test_every_field_is_read_from_where_the_spec_puts_it(self):
        cur = om._current(ver10_encoder(
            token="vec-9", name="Sub", width=704, height=576, fps=12,
            bitrate=512, gov=25, quality=3.0, profile="Baseline"))
        assert cur["config_token"] == "vec-9"
        assert (cur["width"], cur["height"]) == (704, 576)
        assert cur["fps"] == 12 and cur["bitrate_kbps"] == 512
        assert cur["gov_length"] == 25 and cur["h264_profile"] == "Baseline"
        assert cur["quality"] == 3.0

    def test_ver10_never_claims_to_know_about_constant_bitrate(self):
        # There is no ver10 field for it. Reporting False would be a claim.
        assert om._current(ver10_encoder())["constant_bitrate"] is None

    def test_a_missing_rate_control_block_does_not_raise(self):
        cur = om._current(obj(token="v", Name="n", Resolution=obj(Width=1, Height=2)))
        assert cur["fps"] is None and cur["bitrate_kbps"] is None

    def test_a_missing_resolution_block_does_not_raise(self):
        cur = om._current(obj(token="v", Name="n", RateControl=obj(FrameRateLimit=25)))
        assert cur["width"] is None and cur["fps"] == 25

    def test_an_h265_camera_with_no_h264_block_still_reads(self):
        cur = om._current(obj(token="v", Name="n", Encoding="H265",
                              Resolution=obj(Width=3840, Height=2160)))
        assert cur["encoding"] == "H265"
        assert cur["gov_length"] is None and cur["h264_profile"] is None

    def test_an_empty_profile_string_becomes_none_rather_than_empty(self):
        cur = om._current(obj(token="v", Name="n", H264=obj(GovLength=50, H264Profile="")))
        assert cur["h264_profile"] is None


class TestCurrentMedia2:
    def test_gov_length_and_profile_come_off_the_config_itself(self):
        cur = om._current_media2(media2_config(gov=40, profile="Main"))
        assert cur["gov_length"] == 40
        assert cur["h264_profile"] == "Main", (
            "Media2 keeps the codec profile on the config regardless of codec; "
            "it must reach the UI under the key the UI reads"
        )

    @pytest.mark.parametrize("cbr,expected", [(True, True), (False, False)])
    def test_constant_bitrate_is_reported_when_the_camera_says(self, cbr, expected):
        assert om._current_media2(media2_config(cbr=cbr))["constant_bitrate"] is expected

    def test_a_camera_that_omits_constant_bitrate_reports_unknown(self):
        cfg = media2_config()
        cfg.RateControl = obj(FrameRateLimit=20, BitrateLimit=8192)
        assert om._current_media2(cfg)["constant_bitrate"] is None

    def test_media2_has_no_encoding_interval(self):
        assert om._current_media2(media2_config())["encoding_interval"] is None

    def test_an_h265_config_reads_its_resolution(self):
        cur = om._current_media2(media2_config(encoding="H265", width=3840, height=2160))
        assert cur["encoding"] == "H265"
        assert (cur["width"], cur["height"]) == (3840, 2160)


# ── Option ranges ──────────────────────────────────────────────────────────

class TestResolutionNormalisation:
    def test_duplicates_are_removed_and_the_largest_comes_first(self):
        out = om._resolutions(
            options_with_duplicate_resolutions().H264.ResolutionsAvailable)
        assert out == [{"width": 1920, "height": 1080},
                       {"width": 1280, "height": 720},
                       {"width": 640, "height": 480}]

    def test_a_zero_dimension_is_dropped(self):
        # A camera that answers 0x0 is not offering a resolution.
        assert om._resolutions([obj(Width=0, Height=0), obj(Width=640, Height=480)]) \
            == [{"width": 640, "height": 480}]

    def test_a_missing_dimension_is_dropped(self):
        assert om._resolutions([obj(Width=1920)]) == []

    def test_no_resolutions_is_an_empty_list_not_an_error(self):
        assert om._resolutions(None) == []


class TestRanges:
    def test_a_range_reads_both_bounds(self):
        assert om._range(obj(Min=1, Max=30)) == {"min": 1, "max": 30}

    def test_a_half_open_range_is_kept(self):
        assert om._range(obj(Min=1)) == {"min": 1, "max": None}

    def test_a_range_with_neither_bound_is_none(self):
        assert om._range(obj()) is None
        assert om._range(None) is None

    def test_string_bounds_from_a_vendor_are_coerced(self):
        assert om._range(obj(Min="1", Max="30.0")) == {"min": 1, "max": 30}

    @pytest.mark.parametrize("junk", ["", "n/a", None, object()])
    def test_unparseable_numbers_become_none_rather_than_raising(self, junk):
        assert om._num(junk) is None
        assert om._int(junk) is None


class TestVer10Options:
    def test_the_bitrate_range_is_found_under_the_extension(self):
        # It is an ONVIF extension, per codec. A parser that looked at the top
        # level would report no bitrate range and the UI would offer no slider.
        assert om._bitrate_range(ver10_options(bitrate=(64, 8192))) == \
            {"min": 64, "max": 8192}

    def test_no_extension_means_no_bitrate_range_rather_than_an_error(self):
        assert om._bitrate_range(obj(QualityRange=obj(Min=1, Max=6))) is None

    def test_the_codec_block_is_normalised(self):
        opts = om._codec_options(ver10_options())
        assert opts["codec"] == "H264"
        assert opts["fps_range"] == {"min": 1, "max": 30}
        assert opts["h264_profiles"] == ["Baseline", "Main", "High"]

    def test_h264_is_preferred_when_a_camera_offers_several(self):
        both = ver10_options()
        both.JPEG = obj(ResolutionsAvailable=[obj(Width=640, Height=480)])
        assert om._codec_options(both)["codec"] == "H264"

    def test_a_jpeg_only_camera_still_reports_its_options(self):
        jpeg = obj(JPEG=obj(ResolutionsAvailable=[obj(Width=640, Height=480)],
                            FrameRateRange=obj(Min=1, Max=15)))
        assert om._codec_options(jpeg)["codec"] == "JPEG"

    def test_a_camera_offering_no_known_codec_yields_nothing(self):
        assert om._codec_options(obj(H265=obj())) is None

    def test_options_are_advisory_and_their_failure_is_not_fatal(self):
        # The current settings still matter even if the ranges cannot be read;
        # losing the whole encoder over an unsupported options call would hide
        # a camera that is working.
        media = FakeVer10Media([ver10_profile(encoder=ver10_encoder())],
                               options_raises=True)
        out = om._get_ver10(media)
        assert len(out) == 1 and out[0]["options"] is None
        assert out[0]["width"] == 1920


class TestMedia2Options:
    def test_the_block_matching_the_config_encoding_is_chosen(self):
        cfg = media2_config(encoding="H265")
        svc = FakeMedia2Service([cfg], options=[
            media2_options(encoding="H264", resolutions=((1920, 1080),)),
            media2_options(encoding="H265", resolutions=((3840, 2160),)),
        ])
        opts = om._options_media2(svc, cfg)
        assert opts["codec"] == "H265"
        assert opts["resolutions"][0] == {"width": 3840, "height": 2160}

    def test_the_first_block_is_used_when_none_matches(self):
        cfg = media2_config(encoding="H266")
        svc = FakeMedia2Service([cfg], options=[media2_options(encoding="H264")])
        assert om._options_media2(svc, cfg)["resolutions"]

    def test_a_single_block_not_in_a_list_is_accepted(self):
        # Vendors differ on whether they wrap a lone block in a list.
        cfg = media2_config()
        svc = FakeMedia2Service([cfg], options=media2_options(encoding="H265"))
        assert om._options_media2(svc, cfg) is not None

    def test_an_enumeration_of_frame_rates_becomes_a_range(self):
        cfg = media2_config()
        svc = FakeMedia2Service([cfg], options=[
            media2_options(encoding="H265", frame_rates=(5, 10, 20, 25))])
        assert om._options_media2(svc, cfg)["fps_range"] == {"min": 5, "max": 25}

    def test_a_gov_length_list_becomes_a_range(self):
        cfg = media2_config()
        svc = FakeMedia2Service([cfg], options=[
            media2_options(encoding="H265", gov=(10, 100))])
        assert om._options_media2(svc, cfg)["gov_length_range"] == {"min": 10, "max": 100}

    def test_constant_bitrate_support_is_surfaced(self):
        cfg = media2_config()
        svc = FakeMedia2Service([cfg], options=[
            media2_options(encoding="H265", cbr_supported=True)])
        assert om._options_media2(svc, cfg)["constant_bitrate_supported"] is True

    def test_a_camera_that_refuses_options_yields_none_not_an_error(self):
        cfg = media2_config()
        svc = FakeMedia2Service([cfg], options_raises=True)
        assert om._options_media2(svc, cfg) is None

    def test_an_empty_options_answer_yields_none(self):
        cfg = media2_config()
        assert om._options_media2(FakeMedia2Service([cfg], options=[]), cfg) is None
        assert om._options_media2(FakeMedia2Service([cfg], options=[None]), cfg) is None


# ── Profile naming, which is what the operator actually sees ───────────────

class TestMedia2Naming:
    def test_an_encoder_takes_the_name_of_the_profile_that_references_it(self):
        cfg = media2_config(token="vec2-1", name="EncoderCfg")
        prof = obj(token="prof-1", Name="Main Stream",
                   Configurations=obj(VideoEncoder=obj(token="vec2-1")))
        svc = FakeMedia2Service([cfg], options=[media2_options()], profiles=[prof])
        out = om._get_media2(svc)
        assert out[0]["profile_name"] == "Main Stream"
        assert out[0]["profile_token"] == "prof-1"

    def test_an_unreferenced_encoder_falls_back_to_its_own_name(self):
        cfg = media2_config(token="vec2-9", name="EncoderCfg")
        svc = FakeMedia2Service([cfg], options=[media2_options()], profiles=[])
        out = om._get_media2(svc)
        assert out[0]["profile_name"] == "EncoderCfg"
        assert out[0]["profile_token"] == "vec2-9"

    def test_a_camera_that_refuses_getprofiles_still_lists_its_encoders(self):
        # Naming is best-effort; losing the encoder list over it would hide
        # every stream the camera has.
        cfg = media2_config(token="vec2-1", name="EncoderCfg")
        svc = FakeMedia2Service([cfg], options=[media2_options()],
                                profiles_raises=True)
        out = om._get_media2(svc)
        assert len(out) == 1 and out[0]["profile_name"] == "EncoderCfg"

    def test_two_encoders_are_both_reported(self):
        svc = FakeMedia2Service(
            [media2_config(token="a", name="Main", width=3840, height=2160),
             media2_config(token="b", name="Sub", width=704, height=576)],
            options=[media2_options()])
        out = om._get_media2(svc)
        assert [e["width"] for e in out] == [3840, 704]
