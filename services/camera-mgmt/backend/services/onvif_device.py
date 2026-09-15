"""onvif_device.py — camera imaging, OSD, and maintenance controls over ONVIF.

Sibling of onvif_media.py (encoder settings), reusing its connection helper,
error type, and thread/timeout wrapper. Four capability groups:

  • Imaging (ver20/imaging, as universal as Media): brightness, contrast,
    saturation, sharpness, IR cut filter (day/night), WDR, backlight
    compensation, exposure, white balance, focus mode — with camera-reported
    ranges via GetOptions, one block per video source.
  • OSD (Media ver10 OSD ops): read/update burned-in text overlays and
    date/time stamps (position, plain text, formats). Vendor support is the
    quirkiest of the four — reads degrade to a clear OnvifError.
  • SystemReboot — remote power-cycle for a wedged camera.
  • Time sync — SetSystemDateAndTime(Manual, UTC=now). Wrong camera clocks
    skew OSD timestamps and are the top cause of ONVIF auth failures; syncing
    kills that failure class. The camera's TimeZone/DST are preserved.

Same defensive rules as onvif_media.py: every ONVIF field is optional in the
spec, so all reads are getattr-guarded; sync zeep calls run in worker threads
with a hard timeout via `_call`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from .onvif_media import OnvifError, _call, _camera, _int, _num, _range

log = structlog.get_logger(__name__)


# ─── Imaging: parse ───────────────────────────────────────────────────────────

def _mode_level(node: Any) -> Optional[dict]:
    if node is None:
        return None
    return {
        "mode": str(getattr(node, "Mode", "") or "") or None,
        "level": _num(getattr(node, "Level", None)),
    }


def _imaging_current(cur: Any) -> dict:
    exp = getattr(cur, "Exposure", None)
    wb = getattr(cur, "WhiteBalance", None)
    focus = getattr(cur, "Focus", None)
    return {
        "brightness": _num(getattr(cur, "Brightness", None)),
        "contrast": _num(getattr(cur, "Contrast", None)),
        "color_saturation": _num(getattr(cur, "ColorSaturation", None)),
        "sharpness": _num(getattr(cur, "Sharpness", None)),
        "ir_cut_filter": str(getattr(cur, "IrCutFilter", "") or "") or None,
        "wdr": _mode_level(getattr(cur, "WideDynamicRange", None)),
        "blc": _mode_level(getattr(cur, "BacklightCompensation", None)),
        "exposure": None if exp is None else {
            "mode": str(getattr(exp, "Mode", "") or "") or None,
            "exposure_time": _num(getattr(exp, "ExposureTime", None)),
            "gain": _num(getattr(exp, "Gain", None)),
            "iris": _num(getattr(exp, "Iris", None)),
        },
        "white_balance": None if wb is None else {
            "mode": str(getattr(wb, "Mode", "") or "") or None,
            "cr_gain": _num(getattr(wb, "CrGain", None)),
            "cb_gain": _num(getattr(wb, "CbGain", None)),
        },
        "focus_mode": (
            str(getattr(focus, "AutoFocusMode", "") or "") or None
        ) if focus is not None else None,
    }


def _modes(node: Any, attr: str) -> list[str]:
    return [str(m) for m in getattr(node, attr, None) or []]


def _imaging_options(opts: Any) -> Optional[dict]:
    if opts is None:
        return None
    wdr = getattr(opts, "WideDynamicRange", None)
    blc = getattr(opts, "BacklightCompensation", None)
    exp = getattr(opts, "Exposure", None)
    wb = getattr(opts, "WhiteBalance", None)
    focus = getattr(opts, "Focus", None)
    return {
        "brightness_range": _range(getattr(opts, "Brightness", None)),
        "contrast_range": _range(getattr(opts, "Contrast", None)),
        "color_saturation_range": _range(getattr(opts, "ColorSaturation", None)),
        "sharpness_range": _range(getattr(opts, "Sharpness", None)),
        "ir_cut_filter_modes": _modes(opts, "IrCutFilterModes"),
        "wdr": None if wdr is None else {
            "modes": _modes(wdr, "Mode"),
            "level_range": _range(getattr(wdr, "Level", None)),
        },
        "blc": None if blc is None else {
            "modes": _modes(blc, "Mode"),
            "level_range": _range(getattr(blc, "Level", None)),
        },
        "exposure": None if exp is None else {
            "modes": _modes(exp, "Mode"),
            "exposure_time_range": _range(getattr(exp, "ExposureTime", None)),
            "gain_range": _range(getattr(exp, "Gain", None)),
            "iris_range": _range(getattr(exp, "Iris", None)),
        },
        "white_balance": None if wb is None else {
            "modes": _modes(wb, "Mode"),
            "cr_gain_range": _range(getattr(wb, "YrGain", None) or getattr(wb, "CrGain", None)),
            "cb_gain_range": _range(getattr(wb, "YbGain", None) or getattr(wb, "CbGain", None)),
        },
        "focus_modes": _modes(focus, "AutoFocusModes") if focus is not None else [],
    }


# ─── Imaging: read / write ────────────────────────────────────────────────────

def _video_sources(cam: Any) -> list[Any]:
    media = cam.create_media_service()
    sources = media.GetVideoSources() or []
    if not sources:
        raise OnvifError("Camera reports no video sources")
    return sources


def _imaging_get_sync(ip: str, port: int, user: str, password: str) -> list[dict]:
    cam = _camera(ip, port, user, password)
    imaging = cam.create_imaging_service()
    out: list[dict] = []
    for src in _video_sources(cam):
        token = getattr(src, "token", None)
        try:
            cur = imaging.GetImagingSettings({"VideoSourceToken": token})
        except Exception as exc:  # noqa: BLE001
            raise OnvifError(f"Camera does not expose ONVIF imaging settings: {exc}")
        entry: dict[str, Any] = {"source_token": token, **_imaging_current(cur)}
        try:
            entry["options"] = _imaging_options(
                imaging.GetOptions({"VideoSourceToken": token})
            )
        except Exception as exc:  # noqa: BLE001 — options are advisory
            log.debug("onvif_device.imaging_options_failed", ip=ip, error=str(exc))
            entry["options"] = None
        out.append(entry)
    return out


def _require(node: Any, block: str) -> Any:
    if node is None:
        raise OnvifError(f"Camera exposes no {block} block — not settable via ONVIF")
    return node


def _imaging_set_sync(
    ip: str, port: int, user: str, password: str, source_token: str, changes: dict
) -> None:
    cam = _camera(ip, port, user, password)
    imaging = cam.create_imaging_service()
    cur = imaging.GetImagingSettings({"VideoSourceToken": source_token})

    for key, attr in (
        ("brightness", "Brightness"),
        ("contrast", "Contrast"),
        ("color_saturation", "ColorSaturation"),
        ("sharpness", "Sharpness"),
    ):
        if changes.get(key) is not None:
            setattr(cur, attr, float(changes[key]))

    if changes.get("ir_cut_filter"):
        cur.IrCutFilter = changes["ir_cut_filter"]

    if changes.get("wdr_mode") or changes.get("wdr_level") is not None:
        node = _require(getattr(cur, "WideDynamicRange", None), "WideDynamicRange")
        if changes.get("wdr_mode"):
            node.Mode = changes["wdr_mode"]
        if changes.get("wdr_level") is not None:
            node.Level = float(changes["wdr_level"])

    if changes.get("blc_mode") or changes.get("blc_level") is not None:
        node = _require(getattr(cur, "BacklightCompensation", None), "BacklightCompensation")
        if changes.get("blc_mode"):
            node.Mode = changes["blc_mode"]
        if changes.get("blc_level") is not None:
            node.Level = float(changes["blc_level"])

    exp_keys = ("exposure_mode", "exposure_time", "gain", "iris")
    if any(changes.get(k) is not None for k in exp_keys):
        node = _require(getattr(cur, "Exposure", None), "Exposure")
        if changes.get("exposure_mode"):
            node.Mode = changes["exposure_mode"]
        if changes.get("exposure_time") is not None:
            node.ExposureTime = float(changes["exposure_time"])
        if changes.get("gain") is not None:
            node.Gain = float(changes["gain"])
        if changes.get("iris") is not None:
            node.Iris = float(changes["iris"])

    wb_keys = ("wb_mode", "wb_cr_gain", "wb_cb_gain")
    if any(changes.get(k) is not None for k in wb_keys):
        node = _require(getattr(cur, "WhiteBalance", None), "WhiteBalance")
        if changes.get("wb_mode"):
            node.Mode = changes["wb_mode"]
        if changes.get("wb_cr_gain") is not None:
            node.CrGain = float(changes["wb_cr_gain"])
        if changes.get("wb_cb_gain") is not None:
            node.CbGain = float(changes["wb_cb_gain"])

    if changes.get("focus_mode"):
        node = _require(getattr(cur, "Focus", None), "Focus")
        node.AutoFocusMode = changes["focus_mode"]

    imaging.SetImagingSettings(
        {"VideoSourceToken": source_token, "ImagingSettings": cur, "ForcePersistence": True}
    )


# ─── OSD ──────────────────────────────────────────────────────────────────────

def _ref_token(ref: Any) -> Optional[str]:
    """ONVIF reference tokens (e.g. VideoSourceConfigurationToken) come back from
    zeep as OSDReference objects with the token in ``_value_1``, not as a plain
    string — pull the string out so the response stays JSON-serializable."""
    if ref is None:
        return None
    return getattr(ref, "_value_1", None) or str(ref)


def _osd_entry(o: Any) -> dict:
    ts = getattr(o, "TextString", None)
    pos = getattr(o, "Position", None)
    return {
        "token": getattr(o, "token", None),
        "video_source_configuration": _ref_token(getattr(o, "VideoSourceConfigurationToken", None)),
        "type": str(getattr(o, "Type", "") or "") or None,
        "position": _position_label(pos),
        "text_type": (str(getattr(ts, "Type", "") or "") or None) if ts is not None else None,
        "text": getattr(ts, "PlainText", None) if ts is not None else None,
        "date_format": getattr(ts, "DateFormat", None) if ts is not None else None,
        "time_format": getattr(ts, "TimeFormat", None) if ts is not None else None,
    }


def _get_osds(media: Any) -> list[Any]:
    try:
        return media.GetOSDs() or []
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(f"Camera does not support ONVIF OSD configuration: {exc}")


# Many firmwares (CP-Plus/Dahua and others seen in the field) advertise no
# PositionOption keywords and ignore Position.Type="UpperRight"/etc. entirely —
# they only honor Type="Custom" with a normalized Pos vector. ONVIF's space is
# y-up: x -1 left .. +1 right, y -1 bottom .. +1 top. For those cameras we
# translate the corner keyword to coordinates so the overlay actually moves.
# Right-corner X is inset from +1 because Pos anchors the text's left edge and
# would otherwise clip off-screen.
_CORNER_POS = {
    "UpperLeft":  (-0.95,  0.95),
    "UpperRight": ( 0.60,  0.95),
    "LowerLeft":  (-0.95, -0.90),
    "LowerRight": ( 0.60, -0.90),
    "Center":     ( 0.0,   0.0),
}


def _position_label(pos: Any) -> Optional[str]:
    """Human-facing position for the UI. Custom-only cameras report Type="Custom"
    even for overlays we placed at a preset, so map coordinates that match a
    _CORNER_POS entry back to its label; genuinely custom spots stay "Custom"."""
    if pos is None:
        return None
    typ = str(getattr(pos, "Type", "") or "") or None
    if typ != "Custom":
        return typ
    p = getattr(pos, "Pos", None)
    try:
        x, y = float(p.x), float(p.y)
    except (TypeError, AttributeError):
        return typ
    for label, (cx, cy) in _CORNER_POS.items():
        if abs(x - cx) < 0.06 and abs(y - cy) < 0.06:
            return label
    return typ


def _osd_position_options(media: Any, osd: Any) -> list[str]:
    """Position.Type keywords the camera advertises for this OSD's video source.
    Empty when the firmware advertises none (→ treat as Custom-only)."""
    vsc = getattr(osd, "VideoSourceConfigurationToken", None)
    tok = getattr(vsc, "_value_1", None) or vsc
    if tok is None:
        return []
    try:
        opts = media.GetOSDOptions({"ConfigurationToken": tok})
    except Exception:  # noqa: BLE001 — options are advisory; fall back to Custom
        return []
    return [str(p) for p in (getattr(opts, "PositionOption", None) or [])]


def _osd_get_sync(ip: str, port: int, user: str, password: str) -> list[dict]:
    cam = _camera(ip, port, user, password)
    return [_osd_entry(o) for o in _get_osds(cam.create_media_service())]


def _osd_set_sync(
    ip: str, port: int, user: str, password: str, osd_token: str, changes: dict
) -> None:
    cam = _camera(ip, port, user, password)
    media = cam.create_media_service()
    osd = next(
        (o for o in _get_osds(media) if getattr(o, "token", None) == osd_token), None
    )
    if osd is None:
        raise OnvifError(f"OSD '{osd_token}' not found on camera")

    if changes.get("position"):
        want = changes["position"]
        pos = _require(getattr(osd, "Position", None), "OSD Position")
        if want != "Custom" and want in _osd_position_options(media, osd):
            # Camera honors the ONVIF corner keyword directly.
            pos.Type = want
            # Some firmwares reject a stale Custom Pos alongside a keyword.
            if getattr(pos, "Pos", None) is not None:
                pos.Pos = None
        elif want in _CORNER_POS:
            # Firmware ignores corner keywords — place via Custom coordinates.
            x, y = _CORNER_POS[want]
            pos.Type = "Custom"
            cur = getattr(pos, "Pos", None)
            if cur is not None:
                cur.x, cur.y = x, y
            else:
                pos.Pos = {"x": x, "y": y}
        else:
            # "Custom" (or an unknown value) — pass through unchanged coords.
            pos.Type = want

    ts_keys = ("text", "date_format", "time_format")
    if any(changes.get(k) is not None for k in ts_keys):
        ts = _require(getattr(osd, "TextString", None), "OSD TextString")
        if changes.get("text") is not None:
            ts.PlainText = changes["text"]
        if changes.get("date_format") is not None:
            ts.DateFormat = changes["date_format"]
        if changes.get("time_format") is not None:
            ts.TimeFormat = changes["time_format"]

    media.SetOSD({"OSD": osd})


# ─── Credential verification (GetDeviceInformation as the auth probe) ────────

def _verify_device_sync(ip: str, port: int, user: str, password: str) -> dict:
    cam = _camera(ip, port, user, password)
    info = cam.create_devicemgmt_service().GetDeviceInformation()
    return {
        "vendor": getattr(info, "Manufacturer", None),
        "model": getattr(info, "Model", None),
        "firmware": getattr(info, "FirmwareVersion", None),
    }


# ─── Maintenance: reboot + time sync ──────────────────────────────────────────

def _reboot_sync(ip: str, port: int, user: str, password: str) -> dict:
    cam = _camera(ip, port, user, password)
    msg = cam.create_devicemgmt_service().SystemReboot()
    return {"status": "rebooting", "message": str(msg) if msg else None}


def _camera_utc(dev: Any) -> Optional[datetime]:
    try:
        cur = dev.GetSystemDateAndTime()
        utc = getattr(cur, "UTCDateTime", None)
        d, t = getattr(utc, "Date", None), getattr(utc, "Time", None)
        if d is None or t is None:
            return None
        return datetime(
            _int(d.Year), _int(d.Month), _int(d.Day),
            _int(t.Hour), _int(t.Minute), _int(t.Second),
            tzinfo=timezone.utc,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("onvif_device.get_time_failed", error=str(exc))
        return None


def _sync_time_sync(ip: str, port: int, user: str, password: str) -> dict:
    cam = _camera(ip, port, user, password)
    dev = cam.create_devicemgmt_service()

    before = _camera_utc(dev)
    skew = round((before - datetime.now(timezone.utc)).total_seconds(), 1) if before else None

    # Preserve the camera's timezone/DST — we only correct the UTC clock.
    try:
        cur = dev.GetSystemDateAndTime()
        tz = getattr(cur, "TimeZone", None)
        dst = bool(getattr(cur, "DaylightSavings", False))
    except Exception:  # noqa: BLE001
        tz, dst = None, False

    req = dev.create_type("SetSystemDateAndTime")
    req.DateTimeType = "Manual"
    req.DaylightSavings = dst
    if tz is not None:
        req.TimeZone = tz
    now = datetime.now(timezone.utc)
    req.UTCDateTime = {
        "Date": {"Year": now.year, "Month": now.month, "Day": now.day},
        "Time": {"Hour": now.hour, "Minute": now.minute, "Second": now.second},
    }
    dev.SetSystemDateAndTime(req)

    return {
        "status": "synced",
        "skew_before_seconds": skew,
        "camera_time_was": before.isoformat() if before else None,
        "synced_to": now.isoformat(),
    }


# ─── Public async API ─────────────────────────────────────────────────────────

async def get_imaging(ip: str, port: int, user: str, password: str) -> list[dict]:
    return await _call(_imaging_get_sync, ip, port, user, password)


async def set_imaging(
    ip: str, port: int, user: str, password: str, source_token: str, changes: dict
) -> None:
    await _call(_imaging_set_sync, ip, port, user, password, source_token, changes)


async def get_osds(ip: str, port: int, user: str, password: str) -> list[dict]:
    return await _call(_osd_get_sync, ip, port, user, password)


async def set_osd(
    ip: str, port: int, user: str, password: str, osd_token: str, changes: dict
) -> None:
    await _call(_osd_set_sync, ip, port, user, password, osd_token, changes)


async def verify_device(ip: str, port: int, user: str, password: str) -> dict:
    return await _call(_verify_device_sync, ip, port, user, password)


async def reboot(ip: str, port: int, user: str, password: str) -> dict:
    return await _call(_reboot_sync, ip, port, user, password)


async def sync_time(ip: str, port: int, user: str, password: str) -> dict:
    return await _call(_sync_time_sync, ip, port, user, password)
