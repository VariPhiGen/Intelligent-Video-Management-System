"""Vendor-shaped ONVIF responses, for the parsers that have to survive them.

WHAT THESE ARE, HONESTLY. Not packet captures. onvif-zeep hands the service
layer deserialised Python objects, not XML, so a captured SOAP envelope would
have to be replayed through zeep's parser to be useful — which would make these
tests a test of zeep. What the product's code actually has to cope with is the
SHAPE of those objects, and every shape below is reconstructed from a quirk the
source documents in its own comments:

  * "Many of them implement the legacy ver10 Media service as a stub that
    returns empty VideoEncoderConfiguration bodies (only Name/UseCount)"
    -> ver10_stub_profile()
  * "Dedupe, largest first — vendors love repeating entries"
    -> options_with_duplicate_resolutions()
  * "BitrateRange is an ONVIF *extension* (per codec) — dig defensively"
    -> ver10_options() puts it under Extension, and nowhere else.
  * "Media2 keeps the codec profile (Main/High/…) directly on the config"
    -> media2_config()

A zeep object answers `getattr(x, "Missing", default)` with the default, which
is exactly what SimpleNamespace does, so the stand-in is faithful for the one
thing these parsers do: defensive attribute reads.

If a real camera is ever found that breaks a parser, the fix is to add its shape
here with the vendor named in a comment, not to loosen the assertion.
"""
from __future__ import annotations

from types import SimpleNamespace as N


def obj(**kw):
    """A zeep-ish response node."""
    return N(**kw)


# ── ver10 ──────────────────────────────────────────────────────────────────

def ver10_encoder(token="vec-1", name="MainEncoder", width=1920, height=1080,
                  fps=25, bitrate=4096, gov=50, quality=4.0,
                  encoding="H264", profile="Main"):
    """A ver10 VideoEncoderConfiguration that is actually populated."""
    return obj(
        token=token, Name=name, Encoding=encoding, Quality=quality,
        Resolution=obj(Width=width, Height=height),
        RateControl=obj(FrameRateLimit=fps, BitrateLimit=bitrate,
                        EncodingInterval=1),
        H264=obj(GovLength=gov, H264Profile=profile),
    )


def ver10_stub_encoder(token="vec-1", name="MainEncoder"):
    """THE QUIRK. A ver10 body carrying only Name and token.

    Cameras that implement ver10 purely for compatibility answer like this, and
    the real settings live in Media2. `_has_current_settings` is the signal that
    tells the two apart; without it the UI shows an encoder with blank
    resolution, blank fps and blank bitrate, and an operator reads that as a
    camera that is not streaming.
    """
    return obj(token=token, Name=name, UseCount=1)


def ver10_profile(token="prof-1", name="MainStream", encoder=None):
    return obj(token=token, Name=name, VideoEncoderConfiguration=encoder)


def ver10_options(resolutions=((1920, 1080), (1280, 720)), fps=(1, 30),
                  gov=(1, 100), quality=(1, 6), bitrate=(64, 8192),
                  codec="H264", profiles=("Baseline", "Main", "High")):
    """GetVideoEncoderConfigurationOptions, ver10 shape.

    BitrateRange sits under Extension.<codec>, which is where the spec puts it
    and where a naive parser will not look.
    """
    node = obj(
        ResolutionsAvailable=[obj(Width=w, Height=h) for w, h in resolutions],
        FrameRateRange=obj(Min=fps[0], Max=fps[1]),
        GovLengthRange=obj(Min=gov[0], Max=gov[1]),
        EncodingIntervalRange=obj(Min=1, Max=10),
        H264ProfilesSupported=list(profiles),
    )
    return obj(
        QualityRange=obj(Min=quality[0], Max=quality[1]),
        Extension=obj(**{codec: obj(BitrateRange=obj(Min=bitrate[0], Max=bitrate[1]))}),
        **{codec: node},
    )


def options_with_duplicate_resolutions():
    """Vendors repeat entries and return them in no useful order."""
    return obj(
        H264=obj(
            ResolutionsAvailable=[
                obj(Width=1280, Height=720),
                obj(Width=1920, Height=1080),
                obj(Width=1280, Height=720),   # repeat
                obj(Width=640, Height=480),
                obj(Width=1920, Height=1080),  # repeat
            ],
            FrameRateRange=obj(Min=1, Max=30),
        ),
    )


# ── Media2 (ver20) ─────────────────────────────────────────────────────────

def media2_config(token="vec2-1", name="Main", width=3840, height=2160,
                  fps=20, bitrate=8192, gov=40, encoding="H265",
                  profile="Main", cbr=False, quality=5.0):
    """Media2 keeps GovLength and the codec Profile directly on the config,
    and carries ConstantBitRate, which ver10 has no field for."""
    return obj(
        token=token, Name=name, Encoding=encoding, Quality=quality,
        Resolution=obj(Width=width, Height=height),
        RateControl=obj(FrameRateLimit=fps, BitrateLimit=bitrate,
                        ConstantBitRate=cbr),
        GovLength=gov, Profile=profile,
    )


def media2_options(encoding="H265", resolutions=((3840, 2160), (1920, 1080)),
                   frame_rates=(5, 10, 20, 25), gov=(10, 100),
                   bitrate=(256, 16384), quality=(1, 6),
                   profiles=("Main",), cbr_supported=True):
    """Media2 answers with a LIST of per-encoding blocks, and reports frame
    rates as an enumeration rather than a range."""
    return obj(
        Encoding=encoding,
        ResolutionsAvailable=[obj(Width=w, Height=h) for w, h in resolutions],
        FrameRatesSupported=list(frame_rates),
        GovLengthRange=list(gov),
        BitrateRange=obj(Min=bitrate[0], Max=bitrate[1]),
        QualityRange=obj(Min=quality[0], Max=quality[1]),
        ProfilesSupported=list(profiles),
        ConstantBitRateSupported=cbr_supported,
    )


class FakeMedia2Service:
    """Enough Media2 service for `_get_media2`."""

    def __init__(self, configs, options=None, profiles=None,
                 options_raises=False, profiles_raises=False):
        self._configs = configs
        self._options = options
        self._profiles = profiles or []
        self._options_raises = options_raises
        self._profiles_raises = profiles_raises

    def GetVideoEncoderConfigurations(self):
        return self._configs

    def GetVideoEncoderConfigurationOptions(self, _req):
        if self._options_raises:
            raise RuntimeError("camera does not implement options")
        return self._options

    def GetProfiles(self, _req):
        if self._profiles_raises:
            raise RuntimeError("GetProfiles not supported")
        return self._profiles


class FakeVer10Media:
    """Enough ver10 media service for `_get_ver10`."""

    def __init__(self, profiles, options=None, options_raises=False):
        self._profiles = profiles
        self._options = options
        self._options_raises = options_raises
        self.option_calls: list[dict] = []

    def GetProfiles(self):
        return self._profiles

    def GetVideoEncoderConfigurationOptions(self, req):
        self.option_calls.append(req)
        if self._options_raises:
            raise RuntimeError("options unsupported")
        return self._options
