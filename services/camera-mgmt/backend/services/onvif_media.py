"""onvif_media.py — read & change camera stream settings over ONVIF.

Exposes the camera's video encoder configurations (one per media profile —
typically Main / Sub stream) and applies changes: resolution, frame rate,
bitrate, GOP length (GovLength), quality, and codec profile.

Two ONVIF media APIs are handled, preferring the newer one when advertised:

  • Media2 (ver20) — REQUIRED for most modern H.265 cameras. Many of them
    implement the legacy ver10 Media service as a stub that returns empty
    VideoEncoderConfiguration bodies (only Name/UseCount), so the *real* current
    fps/bitrate/resolution live only here. onvif-zeep 0.2.12 ships no Media2
    binding, so we build one with the lib's ONVIFService against a bundled ver20
    WSDL (see onvif_wsdl/ + Dockerfile), reusing its WS-Security + clock-skew
    handling. The bundle's files are unmodified upstream copies — they resolve
    offline via onvif_wsdl_transport, not via rewritten schemaLocations.
  • Media *ver10* — fallback for cameras that don't advertise Media2. H.264-era
    devices populate the encoder config here.

Implementation notes, learned the hard way on real LAN cameras:
  • Every field is optional in the spec and vendors omit liberally — all reads
    are getattr-defensive and absent sections come back as None.
  • ver10 SetVideoEncoderConfiguration requires ForcePersistence; Media2's
    SetVideoEncoderConfiguration does not. Many cameras restart the RTSP stream
    on apply — callers should force a relay reconnect afterwards and let the
    health monitor settle.
  • onvif-zeep is synchronous (zeep/requests), so calls run in worker threads
    with a hard timeout, mirroring onvif_probe.py.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import structlog

from ..config import settings
from .onvif_probe import insecure_transport, is_auth_error

log = structlog.get_logger(__name__)

# ── Media2 (ver20) wiring ─────────────────────────────────────────────────────
_MEDIA2_NS = "http://www.onvif.org/ver20/media/wsdl"
_MEDIA2_BINDING = "{http://www.onvif.org/ver20/media/wsdl}Media2Binding"
# Self-contained WSDL bundle built in the image (see Dockerfile.backend). Falls
# back to the in-repo copy for non-Docker dev runs (not self-contained on its
# own — Media2 reads then simply fail and we drop to ver10).
_MEDIA2_WSDL = os.environ.get(
    "ONVIF_MEDIA2_WSDL",
    "/app/onvif_wsdl/media2.wsdl",
)


class OnvifError(Exception):
    """Camera-side ONVIF failure with a user-presentable message."""

    def __init__(self, message: str, *, auth: bool = False):
        super().__init__(message)
        self.auth = auth


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return None if n is None else int(n)


def _range(node: Any) -> Optional[dict]:
    if node is None:
        return None
    lo, hi = _int(getattr(node, "Min", None)), _int(getattr(node, "Max", None))
    if lo is None and hi is None:
        return None
    return {"min": lo, "max": hi}


def _resolutions(nodes: Any) -> list[dict]:
    out = []
    for r in nodes or []:
        w, h = _int(getattr(r, "Width", None)), _int(getattr(r, "Height", None))
        if w and h:
            out.append({"width": w, "height": h})
    # Dedupe, largest first — vendors love repeating entries.
    seen: set[tuple] = set()
    uniq = []
    for r in sorted(out, key=lambda x: -(x["width"] * x["height"])):
        key = (r["width"], r["height"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


# ── ver10 parsing ─────────────────────────────────────────────────────────────

def _codec_options(opts: Any) -> Optional[dict]:
    """Normalise per-codec option blocks (H264 / JPEG / MPEG4 / ext-H265)."""
    if opts is None:
        return None
    for codec in ("H264", "JPEG", "MPEG4"):
        node = getattr(opts, codec, None)
        if node is None:
            continue
        return {
            "codec": codec,
            "resolutions": _resolutions(getattr(node, "ResolutionsAvailable", None)),
            "fps_range": _range(getattr(node, "FrameRateRange", None)),
            "gov_length_range": _range(getattr(node, "GovLengthRange", None)),
            "encoding_interval_range": _range(getattr(node, "EncodingIntervalRange", None)),
            "h264_profiles": [str(p) for p in getattr(node, "H264ProfilesSupported", None) or []],
        }
    return None


def _bitrate_range(opts: Any) -> Optional[dict]:
    # BitrateRange is an ONVIF *extension* (per codec) — dig defensively.
    ext = getattr(opts, "Extension", None)
    for codec in ("H264", "JPEG", "MPEG4"):
        node = getattr(ext, codec, None) if ext is not None else None
        rng = _range(getattr(node, "BitrateRange", None))
        if rng:
            return rng
    return None


def _current(vec: Any) -> dict:
    rate = getattr(vec, "RateControl", None)
    h264 = getattr(vec, "H264", None)
    res = getattr(vec, "Resolution", None)
    return {
        "config_token": getattr(vec, "token", None),
        "config_name": getattr(vec, "Name", None),
        "encoding": str(getattr(vec, "Encoding", None) or ""),
        "width": _int(getattr(res, "Width", None)) if res is not None else None,
        "height": _int(getattr(res, "Height", None)) if res is not None else None,
        "quality": _num(getattr(vec, "Quality", None)),
        "fps": _int(getattr(rate, "FrameRateLimit", None)) if rate is not None else None,
        "bitrate_kbps": _int(getattr(rate, "BitrateLimit", None)) if rate is not None else None,
        "constant_bitrate": None,
        "encoding_interval": _int(getattr(rate, "EncodingInterval", None)) if rate is not None else None,
        "gov_length": _int(getattr(h264, "GovLength", None)) if h264 is not None else None,
        "h264_profile": str(getattr(h264, "H264Profile", "") or "") or None if h264 is not None else None,
    }


def _has_current_settings(entry: dict) -> bool:
    """True when the camera actually populated the encoder body (some ver10
    stubs return only token/name — that's the signal to try Media2)."""
    return any(entry.get(k) is not None for k in ("width", "height", "fps", "bitrate_kbps"))


# ── Media2 (ver20) parsing ────────────────────────────────────────────────────

def _current_media2(cfg: Any) -> dict:
    rate = getattr(cfg, "RateControl", None)
    res = getattr(cfg, "Resolution", None)
    cbr = getattr(rate, "ConstantBitRate", None) if rate is not None else None
    return {
        "config_token": getattr(cfg, "token", None),
        "config_name": getattr(cfg, "Name", None),
        "encoding": str(getattr(cfg, "Encoding", None) or ""),
        "width": _int(getattr(res, "Width", None)) if res is not None else None,
        "height": _int(getattr(res, "Height", None)) if res is not None else None,
        "quality": _num(getattr(cfg, "Quality", None)),
        "fps": _int(getattr(rate, "FrameRateLimit", None)) if rate is not None else None,
        "bitrate_kbps": _int(getattr(rate, "BitrateLimit", None)) if rate is not None else None,
        "constant_bitrate": bool(cbr) if cbr is not None else None,
        "encoding_interval": None,
        "gov_length": _int(getattr(cfg, "GovLength", None)),
        # Media2 keeps the codec profile (Main/High/…) directly on the config,
        # regardless of H264/H265 — surface it under the same key the UI reads.
        "h264_profile": str(getattr(cfg, "Profile", "") or "") or None,
    }


def _options_media2(svc: Any, cfg: Any) -> Optional[dict]:
    """Option ranges for one Media2 config. GetVideoEncoderConfigurationOptions
    returns a list of per-encoding blocks — pick the one matching this config's
    current encoding (fall back to the first)."""
    try:
        opts = svc.GetVideoEncoderConfigurationOptions(
            {"ConfigurationToken": getattr(cfg, "token", None)}
        )
    except Exception as exc:  # noqa: BLE001 — options are advisory
        log.debug("onvif_media.media2_options_failed", token=getattr(cfg, "token", None), error=str(exc))
        return None
    blocks = opts if isinstance(opts, list) else [opts]
    blocks = [b for b in blocks if b is not None]
    if not blocks:
        return None
    enc = str(getattr(cfg, "Encoding", "") or "")
    block = next((b for b in blocks if str(getattr(b, "Encoding", "")) == enc), blocks[0])

    frs = [x for x in (_num(v) for v in getattr(block, "FrameRatesSupported", None) or []) if x is not None]
    fps_range = {"min": int(min(frs)), "max": int(max(frs))} if frs else None
    gov = [x for x in (_int(v) for v in getattr(block, "GovLengthRange", None) or []) if x is not None]
    gov_range = {"min": min(gov), "max": max(gov)} if gov else None
    return {
        "codec": enc or None,
        "resolutions": _resolutions(getattr(block, "ResolutionsAvailable", None)),
        "fps_range": fps_range,
        "gov_length_range": gov_range,
        "bitrate_kbps_range": _range(getattr(block, "BitrateRange", None)),
        "quality_range": _range(getattr(block, "QualityRange", None)),
        "h264_profiles": [str(p) for p in getattr(block, "ProfilesSupported", None) or []],
        "constant_bitrate_supported": bool(getattr(block, "ConstantBitRateSupported", False)),
    }


def _get_media2(svc: Any) -> list[dict]:
    configs = svc.GetVideoEncoderConfigurations()
    # Map each encoder config to a profile that references it (for the name).
    prof_by_vec: dict[str, Any] = {}
    try:
        for pr in svc.GetProfiles({"Type": ["All"]}):
            ve = getattr(getattr(pr, "Configurations", None), "VideoEncoder", None)
            tok = getattr(ve, "token", None)
            if tok is not None:
                prof_by_vec.setdefault(tok, pr)
    except Exception as exc:  # noqa: BLE001 — naming is best-effort
        log.debug("onvif_media.media2_profiles_failed", error=str(exc))

    out: list[dict] = []
    for cfg in configs:
        tok = getattr(cfg, "token", None)
        pr = prof_by_vec.get(tok)
        entry: dict[str, Any] = {
            "profile_token": getattr(pr, "token", tok),
            "profile_name": str(getattr(pr, "Name", None) or getattr(cfg, "Name", None) or tok),
            **_current_media2(cfg),
        }
        entry["options"] = _options_media2(svc, cfg)
        out.append(entry)
    return out


def _set_media2(svc: Any, config_token: str, changes: dict) -> None:
    conf = next(
        (c for c in svc.GetVideoEncoderConfigurations() if getattr(c, "token", None) == config_token),
        None,
    )
    if conf is None:
        raise OnvifError(f"Encoder configuration '{config_token}' not found on camera")

    if changes.get("width") and changes.get("height"):
        res = getattr(conf, "Resolution", None)
        if res is None:
            raise OnvifError("Camera exposes no Resolution block on this configuration")
        res.Width = int(changes["width"])
        res.Height = int(changes["height"])

    if any(changes.get(k) is not None for k in ("fps", "bitrate_kbps", "constant_bitrate")):
        rate = getattr(conf, "RateControl", None)
        if rate is None:
            raise OnvifError("Camera exposes no RateControl block — fps/bitrate not settable via ONVIF")
        if changes.get("fps") is not None:
            rate.FrameRateLimit = float(changes["fps"])
        if changes.get("bitrate_kbps") is not None:
            rate.BitrateLimit = int(changes["bitrate_kbps"])
        if changes.get("constant_bitrate") is not None:
            rate.ConstantBitRate = bool(changes["constant_bitrate"])

    if changes.get("gov_length") is not None:
        conf.GovLength = int(changes["gov_length"])
    if changes.get("h264_profile"):
        conf.Profile = changes["h264_profile"]
    if changes.get("quality") is not None:
        conf.Quality = float(changes["quality"])

    # Media2 Set has no ForcePersistence argument (unlike ver10).
    svc.SetVideoEncoderConfiguration({"Configuration": conf})


# ── Connection helpers ────────────────────────────────────────────────────────

def _camera(ip: str, port: int, user: str, password: str):
    from onvif import ONVIFCamera  # lazy import, matches onvif_probe.py

    # adjust_time syncs the WS-Security timestamp to the device clock (computes
    # cam.dt_diff), reused below for the Media2 client.
    return ONVIFCamera(
        ip, port, user, password, adjust_time=True, transport=insecure_transport()
    )


def _media2_service(cam: Any, user: str, password: str):
    """Build a Media2 (ver20) service client if the camera advertises it and the
    bundled WSDL is present; else None (caller falls back to ver10)."""
    if not os.path.isfile(_MEDIA2_WSDL):
        return None
    xaddr = None
    try:
        for s in cam.devicemgmt.GetServices({"IncludeCapability": False}):
            if getattr(s, "Namespace", None) == _MEDIA2_NS:
                xaddr = s.XAddr
                break
    except Exception as exc:  # noqa: BLE001
        log.debug("onvif_media.getservices_failed", error=str(exc))
        return None
    if not xaddr:
        return None
    from onvif.client import ONVIFService

    return ONVIFService(
        xaddr, user, password, _MEDIA2_WSDL,
        encrypt=True, no_cache=False, dt_diff=cam.dt_diff,
        binding_name=_MEDIA2_BINDING, transport=insecure_transport(),
    )


# ── ver10 read/write ──────────────────────────────────────────────────────────

def _get_ver10(media: Any) -> list[dict]:
    out: list[dict] = []
    for profile in media.GetProfiles():
        vec = getattr(profile, "VideoEncoderConfiguration", None)
        if vec is None:
            continue
        entry: dict[str, Any] = {
            "profile_token": profile.token,
            "profile_name": str(getattr(profile, "Name", profile.token)),
            **_current(vec),
        }
        try:
            opts = media.GetVideoEncoderConfigurationOptions(
                {"ConfigurationToken": vec.token, "ProfileToken": profile.token}
            )
            entry["options"] = {
                "quality_range": _range(getattr(opts, "QualityRange", None)),
                "bitrate_kbps_range": _bitrate_range(opts),
                **(_codec_options(opts) or {}),
            }
        except Exception as exc:  # noqa: BLE001 — options are advisory
            log.debug("onvif_media.options_failed", token=vec.token, error=str(exc))
            entry["options"] = None
        out.append(entry)
    return out


def _set_ver10(media: Any, config_token: str, changes: dict) -> None:
    conf = media.GetVideoEncoderConfiguration({"ConfigurationToken": config_token})
    if conf is None:
        raise OnvifError(f"Encoder configuration '{config_token}' not found on camera")

    if changes.get("width") and changes.get("height"):
        res = getattr(conf, "Resolution", None)
        if res is None:
            raise OnvifError("Camera exposes no Resolution block on this configuration")
        res.Width = int(changes["width"])
        res.Height = int(changes["height"])

    if changes.get("fps") is not None or changes.get("bitrate_kbps") is not None:
        rate = getattr(conf, "RateControl", None)
        if rate is None:
            raise OnvifError("Camera exposes no RateControl block — fps/bitrate not settable via ONVIF")
        if changes.get("fps") is not None:
            rate.FrameRateLimit = int(changes["fps"])
        if changes.get("bitrate_kbps") is not None:
            rate.BitrateLimit = int(changes["bitrate_kbps"])

    if changes.get("gov_length") is not None or changes.get("h264_profile"):
        h264 = getattr(conf, "H264", None)
        if h264 is None:
            raise OnvifError("No H.264 section on this configuration — GOP/profile not settable")
        if changes.get("gov_length") is not None:
            h264.GovLength = int(changes["gov_length"])
        if changes.get("h264_profile"):
            h264.H264Profile = changes["h264_profile"]

    if changes.get("quality") is not None:
        conf.Quality = float(changes["quality"])

    media.SetVideoEncoderConfiguration(
        {"Configuration": conf, "ForcePersistence": True}
    )


# ── Orchestration (Media2 preferred, ver10 fallback) ──────────────────────────

def _get_sync(ip: str, port: int, user: str, password: str) -> list[dict]:
    cam = _camera(ip, port, user, password)

    svc2 = _media2_service(cam, user, password)
    if svc2 is not None:
        try:
            res = _get_media2(svc2)
            if any(_has_current_settings(e) for e in res):
                return res
            log.info("onvif_media.media2_empty_fallback_ver10", ip=ip)
        except Exception as exc:  # noqa: BLE001 — fall back to ver10
            log.info("onvif_media.media2_get_failed", ip=ip, error=str(exc))

    return _get_ver10(cam.create_media_service())


def _set_sync(
    ip: str, port: int, user: str, password: str, config_token: str, changes: dict
) -> None:
    cam = _camera(ip, port, user, password)

    svc2 = _media2_service(cam, user, password)
    if svc2 is not None:
        # Only Media2 owns this config if it lists the token — otherwise the
        # camera is really a ver10 device and we must not touch the ver10 stub.
        try:
            tokens = {getattr(c, "token", None) for c in svc2.GetVideoEncoderConfigurations()}
        except Exception as exc:  # noqa: BLE001
            tokens = set()
            log.info("onvif_media.media2_list_failed", ip=ip, error=str(exc))
        if config_token in tokens:
            _set_media2(svc2, config_token, changes)
            return

    _set_ver10(cam.create_media_service(), config_token, changes)


async def _call(fn, *args) -> Any:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args), timeout=settings.discovery_onvif_timeout
        )
    except asyncio.TimeoutError:
        raise OnvifError("Camera did not answer the ONVIF request in time")
    except OnvifError:
        raise
    except Exception as exc:  # noqa: BLE001 — zeep raises a zoo of exception types
        if is_auth_error(exc):
            raise OnvifError("Camera rejected the stored ONVIF credentials", auth=True)
        raise OnvifError(str(exc))


async def get_encoder_settings(ip: str, port: int, user: str, password: str) -> list[dict]:
    return await _call(_get_sync, ip, port, user, password)


async def set_encoder_settings(
    ip: str, port: int, user: str, password: str, config_token: str, changes: dict
) -> None:
    await _call(_set_sync, ip, port, user, password, config_token, changes)
