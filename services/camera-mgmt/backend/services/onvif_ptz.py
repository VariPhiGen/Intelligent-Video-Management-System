"""onvif_ptz.py — pan / tilt / zoom over the ONVIF PTZ service (ver20).

Third sibling of onvif_media.py (encoder) and onvif_device.py (imaging/OSD/
maintenance), reusing their connection helper, error type and thread/timeout
wrapper. This module is PROTOCOL ONLY — it speaks ONVIF and normalises what
comes back. Who is allowed to move a camera, who currently holds control, and
what stops a move that nobody ended lives one layer up in ptz_control.py.

Why the split matters: a continuous move is the only camera command in this
product that keeps running after the request returns. Leaving that policy
inside the protocol module is how you ship a camera that spins forever when a
browser tab dies.

Capability detection, in the order a camera actually answers it:

  1. ``create_ptz_service()`` — onvif-zeep builds the client from the XAddr the
     device published in GetCapabilities. A fixed camera has no PTZ XAddr and
     this raises, which is the cheapest and most reliable "no PTZ" signal there
     is. No guessing from vendor/model strings.
  2. A media profile carrying a ``PTZConfiguration``. Every PTZ operation is
     addressed by ProfileToken, so a device that advertises the service but
     binds it to no profile (multi-channel encoders with one PTZ head do this)
     is correctly reported as not controllable *on this camera*.
  3. ``GetNodes`` — which move kinds, how many presets, home support, auxiliary
     commands. Vendors under-report here constantly, so an empty/absent spaces
     block degrades to "continuous only" rather than to "nothing" (continuous
     is the one operation every PTZ camera in the field implements) — see
     _spaces().

ONVIF axis convention, which the UI depends on: x = pan, +1 right; y = tilt,
+1 UP; zoom x, +1 tele. Velocities and relative translations are in the generic
normalised space (-1..1), and we deliberately omit the `space` attribute so the
camera applies its own default — naming a space is the single most common way
to get a Sender/InvalidArgs fault out of a cheap firmware.
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any, Callable, Optional

import structlog

from .onvif_media import OnvifError, _call, _camera, _num

log = structlog.get_logger(__name__)


# ─── Session pool ─────────────────────────────────────────────────────────────
#
# PTZ is the only ONVIF surface in this product that is *interactive*. The
# encoder and imaging forms talk to a camera once when a modal opens, so
# onvif_media/_device build a fresh ONVIFCamera per call and nobody notices the
# cost. That cost is real though: constructing one runs GetSystemDateAndTime
# (adjust_time) plus GetCapabilities before the operation you actually wanted.
# Three round trips to nudge a camera 2° left, re-paid every time a held arrow
# re-issues, is the difference between a pad that tracks your finger and a pad
# that feels broken.
#
# So PTZ keeps the built client and its HTTP keep-alive around, keyed by
# credentials as well as address so a password change cannot reuse a session
# authenticated with the old one. Entries idle out, and ANY failure drops the
# session and retries once on a fresh one — which is what makes a camera reboot,
# a DHCP move or a firmware upgrade self-heal instead of wedging the pad until
# the API restarts.
#
# onvif-zeep is built on requests/zeep and is NOT thread-safe, and _call hands
# these functions to a worker-thread pool. Each session therefore carries its
# own lock: concurrent commands to the SAME camera serialise (they are a single
# physical head — they were always going to), while different cameras run in
# parallel.

_SESSION_IDLE_SEC = 120.0


class _Session:
    __slots__ = ("ptz", "media", "profile_token", "touched", "lock")

    def __init__(self, ptz: Any, media: Any, profile_token: Optional[str]):
        self.ptz = ptz
        self.media = media
        self.profile_token = profile_token
        self.touched = time.monotonic()
        self.lock = threading.Lock()


_sessions: dict[tuple, _Session] = {}
_sessions_guard = threading.Lock()

# Per-camera build lock — the single-flight that stops a cold camera being
# built twice at once. The build is 1–3 s (clock sync + GetCapabilities + WSDL
# parse), and it is done OUTSIDE _sessions_guard so it does not block other
# cameras. That opens a stampede window: press-then-release fires move and stop
# within ~100 ms, both miss the empty pool, and both build their own session —
# exactly the `cold: true, build_ms: ~1100` seen on BOTH lines of one drag in
# the field. With this lock the first caller builds while the rest wait on the
# SAME build and then find it warm, so one cold camera costs one build, not one
# per in-flight command. The lock map is keyed like the pool and never grows
# past the camera count.
_build_locks: dict[tuple, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _evict_idle(now: float) -> None:
    for key in [k for k, s in _sessions.items() if now - s.touched > _SESSION_IDLE_SEC]:
        _sessions.pop(key, None)


def _build_session(ip: str, port: int, user: str, password: str) -> _Session:
    cam = _camera(ip, port, user, password)
    try:
        ptz = cam.create_ptz_service()
    except Exception:  # noqa: BLE001 — no PTZ XAddr in GetCapabilities
        raise OnvifError("Camera advertises no ONVIF PTZ service")
    media = cam.create_media_service()
    token, _conf = _ptz_profile(media)
    if token is None:
        raise OnvifError(
            "No media profile on this camera is bound to a PTZ configuration"
        )
    return _Session(ptz, media, token)


def _session(ip: str, port: int, user: str, password: str) -> tuple[tuple, _Session, bool]:
    """Returns (key, session, built) — `built` True when this call had to
    construct the client (a "cold" command: clock sync + GetCapabilities +
    WSDL parse, the 1–3 s a warm command never pays). Surfaced so the timing
    log can say WHY a command was slow, not just that it was."""
    key = (ip, int(port), user, password)
    now = time.monotonic()
    with _sessions_guard:
        _evict_idle(now)
        sess = _sessions.get(key)
        if sess is not None:
            sess.touched = now
            return key, sess, False

    # Cold. Serialise the build per camera so concurrent commands share it.
    with _build_locks_guard:
        blk = _build_locks.setdefault(key, threading.Lock())

    with blk:
        # Re-check under the build lock: a racing caller may have finished the
        # build while we waited on it. If so we are warm, not cold — the wait
        # was the winner's build time, not a second one.
        with _sessions_guard:
            sess = _sessions.get(key)
            if sess is not None:
                sess.touched = time.monotonic()
                return key, sess, False
        sess = _build_session(ip, port, user, password)
        with _sessions_guard:
            _sessions[key] = sess
    return key, sess, True


def _drop(key: tuple) -> None:
    with _sessions_guard:
        _sessions.pop(key, None)


def _with_ptz(ip: str, port: int, user: str, password: str,
              profile_token: Optional[str], fn: Callable[[Any, str], Any],
              label: str = "call") -> Any:
    """Run `fn(ptz_service, profile_token)` on a pooled session, retrying once on
    a fresh one.

    The retry is not defensive noise: a pooled zeep client outlives camera
    reboots and address changes, and without it the first command after any of
    those fails and the operator is left pressing a dead arrow. One retry, then
    the error is the camera's to report.

    Emits ONE timing line per command — ``onvif_ptz.timing`` — carrying the op,
    the camera ip, whether the session was cold (built here) or warm (pooled),
    the time spent building a cold session, and the SOAP round trip itself.
    This is the line that answers "ONVIF delay or video delay?": if
    ``soap_ms`` is small (tens of ms on a LAN) while the operator sees lag, the
    delay is in the video path, not here. See docs note in the router.
    """
    t0 = time.monotonic()
    key, sess, built = _session(ip, port, user, password)
    sess_ms = round((time.monotonic() - t0) * 1000, 1)
    # build_ms: time WE spent building. wait_ms: time we spent blocked on a
    # concurrent build for the same camera (single-flight — this command shared
    # a build another in-flight command was already running). Both explain a
    # high total_ms without a slow SOAP; only one is ever non-zero.
    build_ms = sess_ms if built else 0.0
    wait_ms = 0.0 if built else sess_ms
    token = profile_token or sess.profile_token
    t_soap = time.monotonic()
    try:
        with sess.lock:
            result = fn(sess.ptz, token)
        log.info("onvif_ptz.timing", op=label, ip=ip, cold=built,
                 build_ms=build_ms, wait_ms=wait_ms,
                 soap_ms=round((time.monotonic() - t_soap) * 1000, 1))
        return result
    except OnvifError:
        raise
    except Exception as first:  # noqa: BLE001
        log.info("onvif_ptz.session_retry", ip=ip, error=str(first))
        _drop(key)
        t_retry = time.monotonic()
        key, sess, built = _session(ip, port, user, password)
        build_ms = round((time.monotonic() - t_retry) * 1000, 1) if built else 0.0
        # The cached profile token may be exactly what went stale (firmware
        # upgrades renumber them), so prefer the freshly resolved one.
        t_soap = time.monotonic()
        with sess.lock:
            result = fn(sess.ptz, sess.profile_token or token)
        log.info("onvif_ptz.timing", op=label, ip=ip, cold=True, retried=True,
                 build_ms=build_ms, soap_ms=round((time.monotonic() - t_soap) * 1000, 1))
        return result


# ─── Profile / node resolution ────────────────────────────────────────────────

def _ptz_profile(media: Any) -> tuple[Optional[str], Any]:
    """First media profile that carries a PTZConfiguration, as (token, config).

    Not simply "the first profile": on a camera whose sub stream has no PTZ
    configuration attached, addressing PTZ with that profile's token fails with
    a bare NoConfig fault that reads like the camera has no PTZ at all.
    """
    for p in media.GetProfiles() or []:
        conf = getattr(p, "PTZConfiguration", None)
        if conf is not None:
            return getattr(p, "token", None), conf
    return None, None


def _node_for(ptz: Any, conf: Any) -> Any:
    """The PTZ node backing this configuration (GetNode by token, else the
    first node). Advisory only — a camera that cannot answer still moves."""
    node_token = getattr(conf, "NodeToken", None)
    if node_token:
        try:
            return ptz.GetNode({"NodeToken": node_token})
        except Exception as exc:  # noqa: BLE001 — fall through to GetNodes
            log.debug("onvif_ptz.get_node_failed", token=node_token, error=str(exc))
    try:
        nodes = ptz.GetNodes() or []
        return nodes[0] if nodes else None
    except Exception as exc:  # noqa: BLE001
        log.debug("onvif_ptz.get_nodes_failed", error=str(exc))
        return None


def _has(spaces: Any, attr: str) -> bool:
    return bool(getattr(spaces, attr, None))


def _spaces(node: Any) -> dict:
    """Which move kinds this node advertises.

    The fallback is the load-bearing part. When a node reports no
    SupportedPTZSpaces at all — common on budget firmware — we claim continuous
    and nothing else. Continuous move is universal among PTZ cameras; absolute
    and relative are not, and offering an absolute control that the camera
    answers with a fault is worse than not offering it.
    """
    spaces = getattr(node, "SupportedPTZSpaces", None) if node is not None else None
    if spaces is None:
        return {
            "continuous": {"pan_tilt": True, "zoom": True},
            "relative": {"pan_tilt": False, "zoom": False},
            "absolute": {"pan_tilt": False, "zoom": False},
            "spaces_reported": False,
        }
    return {
        "continuous": {
            "pan_tilt": _has(spaces, "ContinuousPanTiltVelocitySpace"),
            "zoom": _has(spaces, "ContinuousZoomVelocitySpace"),
        },
        "relative": {
            "pan_tilt": _has(spaces, "RelativePanTiltTranslationSpace"),
            "zoom": _has(spaces, "RelativeZoomTranslationSpace"),
        },
        "absolute": {
            "pan_tilt": _has(spaces, "AbsolutePanTiltPositionSpace"),
            "zoom": _has(spaces, "AbsoluteZoomPositionSpace"),
        },
        "spaces_reported": True,
    }


def _range(r: Any) -> Optional[list]:
    lo, hi = _num(getattr(r, "Min", None)), _num(getattr(r, "Max", None))
    return [lo, hi] if lo is not None and hi is not None and hi > lo else None


def _ranges(node: Any, conf: Any) -> dict:
    """The absolute position limits: the configuration's PanTiltLimits / ZoomLimits when the installer narrowed
    them (a dome told not to look above the horizon), else the node's AbsolutePanTiltPositionSpace range.
    {"pan": [min, max], "tilt": [...], "zoom": [...]} with only what the camera reports. Click-to-centre
    calibration keeps its test moves inside these — i-PRO answers anything outside with "Destination out of
    bounds" (alpha, 2026-10-26)."""
    out: dict = {}
    pt_lim = getattr(getattr(conf, "PanTiltLimits", None), "Range", None) if conf is not None else None
    z_lim = getattr(getattr(conf, "ZoomLimits", None), "Range", None) if conf is not None else None
    spaces = getattr(node, "SupportedPTZSpaces", None) if node is not None else None

    def first(seq: Any) -> Any:
        try:
            return (list(seq) or [None])[0]
        except TypeError:
            return seq

    pt_space = first(getattr(spaces, "AbsolutePanTiltPositionSpace", None)) if spaces is not None else None
    z_space = first(getattr(spaces, "AbsoluteZoomPositionSpace", None)) if spaces is not None else None
    for axis, attr, lim, space in (("pan", "XRange", pt_lim, pt_space), ("tilt", "YRange", pt_lim, pt_space),
                                   ("zoom", "XRange", z_lim, z_space)):
        r = _range(getattr(lim, attr, None)) if lim is not None else None
        r = r or (_range(getattr(space, attr, None)) if space is not None else None)
        if r:
            out[axis] = r
    return out


# ─── Capability probe ─────────────────────────────────────────────────────────

def _probe_sync(ip: str, port: int, user: str, password: str) -> dict:
    # A probe is the authoritative answer, so it never reads through the pool —
    # and it drops any cached session first, because the reason someone forced a
    # re-probe is usually that the cached view of this camera has gone wrong.
    _drop((ip, int(port), user, password))
    cam = _camera(ip, port, user, password)

    try:
        ptz = cam.create_ptz_service()
    except Exception:  # noqa: BLE001
        # Device published no PTZ XAddr in GetCapabilities — a fixed camera.
        # Not an error to the caller: "this camera cannot pan" is an answer.
        return {"supported": False, "reason": "Camera advertises no ONVIF PTZ service"}

    profile_token, conf = _ptz_profile(cam.create_media_service())
    if profile_token is None:
        return {
            "supported": False,
            "reason": "Camera has an ONVIF PTZ service but no media profile bound to a "
                      "PTZ configuration — nothing on this channel is movable",
        }

    node = _node_for(ptz, conf)
    max_presets = None
    for attr in ("MaximumNumberOfPresets", "MaximumNumberOfPreset"):
        val = getattr(node, attr, None) if node is not None else None
        if val is not None:
            max_presets = int(val)
            break

    aux = [str(a) for a in (getattr(node, "AuxiliaryCommands", None) or [])]

    return {
        "supported": True,
        "profile_token": profile_token,
        "node_token": getattr(node, "token", None) if node is not None else
                      (getattr(conf, "NodeToken", None) or None),
        **_spaces(node),
        "presets": {
            "supported": True,      # every PTZ profile supports GetPresets
            "max": max_presets,
        },
        "home": {"supported": bool(getattr(node, "HomeSupported", False))},
        "aux_commands": aux,
        "ranges": _ranges(node, conf),
    }


# ─── Movement ─────────────────────────────────────────────────────────────────

def _vector(pan: Optional[float], tilt: Optional[float], zoom: Optional[float]) -> dict:
    """PanTilt / Zoom sub-vectors, each omitted when that axis is not moving.

    Sending Zoom:{x:0} alongside a pan is a fault on several firmwares ("this
    node does not support zoom"), so an axis that is not being driven must be
    absent from the message, not present-and-zero.
    """
    out: dict[str, Any] = {}
    if pan is not None or tilt is not None:
        out["PanTilt"] = {"x": float(pan or 0.0), "y": float(tilt or 0.0)}
    if zoom is not None:
        out["Zoom"] = {"x": float(zoom)}
    return out


def _speed_vector(speed: Optional[float], pan: Optional[float],
                  tilt: Optional[float], zoom: Optional[float]) -> Optional[dict]:
    """Speed carries the same axis-presence rule as the movement vector: an axis
    absent from the move must be absent from its speed, or cameras that only pan
    reject the whole message."""
    if speed is None:
        return None
    return _vector(
        speed if pan is not None else None,
        speed if tilt is not None else None,
        speed if zoom is not None else None,
    ) or None


def _continuous_sync(ip: str, port: int, user: str, password: str, profile_token: Optional[str],
                     pan: Optional[float], tilt: Optional[float], zoom: Optional[float],
                     timeout_s: float) -> dict:
    velocity = _vector(pan, tilt, zoom)
    if not velocity:
        raise OnvifError("No axis to move")

    def op(ptz: Any, token: str) -> dict:
        req = {"ProfileToken": token, "Velocity": velocity,
               "Timeout": timedelta(seconds=timeout_s)}
        try:
            ptz.ContinuousMove(req)
        except Exception as exc:  # noqa: BLE001
            # A large minority of firmwares reject or choke on the optional
            # Timeout. Retry without it — ptz_control's watchdog is the real
            # stop guarantee either way; the field is just the cheaper one.
            log.info("onvif_ptz.continuous_timeout_rejected", error=str(exc))
            req.pop("Timeout")
            ptz.ContinuousMove(req)
        return {"profile_token": token}

    return _with_ptz(ip, port, user, password, profile_token, op, label="continuous")


def _stop_sync(ip: str, port: int, user: str, password: str,
               profile_token: Optional[str]) -> dict:
    def op(ptz: Any, token: str) -> dict:
        ptz.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True})
        return {"profile_token": token}

    return _with_ptz(ip, port, user, password, profile_token, op, label="stop")


def _relative_sync(ip: str, port: int, user: str, password: str, profile_token: Optional[str],
                   pan: Optional[float], tilt: Optional[float], zoom: Optional[float],
                   speed: Optional[float]) -> dict:
    translation = _vector(pan, tilt, zoom)
    if not translation:
        raise OnvifError("No axis to move")
    sp = _speed_vector(speed, pan, tilt, zoom)

    def op(ptz: Any, token: str) -> dict:
        req: dict[str, Any] = {"ProfileToken": token, "Translation": translation}
        if sp:
            req["Speed"] = sp
        try:
            ptz.RelativeMove(req)
        except Exception as exc:  # noqa: BLE001 — Speed is optional and often refused
            if "Speed" not in req:
                raise
            log.info("onvif_ptz.relative_speed_rejected", error=str(exc))
            req.pop("Speed")
            ptz.RelativeMove(req)
        return {"profile_token": token}

    return _with_ptz(ip, port, user, password, profile_token, op, label="relative")


def _absolute_sync(ip: str, port: int, user: str, password: str, profile_token: Optional[str],
                   pan: Optional[float], tilt: Optional[float], zoom: Optional[float],
                   speed: Optional[float]) -> dict:
    position = _vector(pan, tilt, zoom)
    if not position:
        raise OnvifError("No axis to move")
    sp = _speed_vector(speed, pan, tilt, zoom)

    def op(ptz: Any, token: str) -> dict:
        req: dict[str, Any] = {"ProfileToken": token, "Position": position}
        if sp:
            req["Speed"] = sp
        try:
            ptz.AbsoluteMove(req)
        except Exception as exc:  # noqa: BLE001
            if "Speed" not in req:
                raise
            log.info("onvif_ptz.absolute_speed_rejected", error=str(exc))
            req.pop("Speed")
            ptz.AbsoluteMove(req)
        return {"profile_token": token}

    return _with_ptz(ip, port, user, password, profile_token, op, label="absolute")


def _status_sync(ip: str, port: int, user: str, password: str,
                 profile_token: Optional[str]) -> dict:
    def op(ptz: Any, token: str) -> Any:
        return ptz.GetStatus({"ProfileToken": token})

    st = _with_ptz(ip, port, user, password, profile_token, op, label="status")
    pos = getattr(st, "Position", None)
    pt = getattr(pos, "PanTilt", None) if pos is not None else None
    zm = getattr(pos, "Zoom", None) if pos is not None else None
    move = getattr(st, "MoveStatus", None)
    return {
        "pan": _num(getattr(pt, "x", None)),
        "tilt": _num(getattr(pt, "y", None)),
        "zoom": _num(getattr(zm, "x", None)),
        "moving_pan_tilt": str(getattr(move, "PanTilt", "") or "") or None,
        "moving_zoom": str(getattr(move, "Zoom", "") or "") or None,
    }


# ─── Presets ──────────────────────────────────────────────────────────────────

def _preset_entry(p: Any) -> dict:
    pos = getattr(p, "PTZPosition", None)
    pt = getattr(pos, "PanTilt", None) if pos is not None else None
    zm = getattr(pos, "Zoom", None) if pos is not None else None
    return {
        "token": getattr(p, "token", None),
        # Name is optional in the spec and blank on several vendors' factory
        # presets — the UI shows the token when it is missing.
        "name": getattr(p, "Name", None),
        "pan": _num(getattr(pt, "x", None)),
        "tilt": _num(getattr(pt, "y", None)),
        "zoom": _num(getattr(zm, "x", None)),
    }


def _presets_sync(ip: str, port: int, user: str, password: str,
                  profile_token: Optional[str]) -> list[dict]:
    def op(ptz: Any, token: str) -> Any:
        return ptz.GetPresets({"ProfileToken": token}) or []

    try:
        presets = _with_ptz(ip, port, user, password, profile_token, op, label="presets")
    except OnvifError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(f"Camera does not expose ONVIF PTZ presets: {exc}")
    return [_preset_entry(p) for p in presets]


def _goto_preset_sync(ip: str, port: int, user: str, password: str, profile_token: Optional[str],
                      preset_token: str, speed: Optional[float]) -> dict:
    def op(ptz: Any, token: str) -> dict:
        req: dict[str, Any] = {"ProfileToken": token, "PresetToken": preset_token}
        if speed is not None:
            req["Speed"] = _vector(speed, speed, speed)
        try:
            ptz.GotoPreset(req)
        except Exception as exc:  # noqa: BLE001 — Speed is optional and often refused
            if "Speed" not in req:
                raise
            log.info("onvif_ptz.goto_preset_speed_rejected", error=str(exc))
            req.pop("Speed")
            ptz.GotoPreset(req)
        return {"preset_token": preset_token}

    return _with_ptz(ip, port, user, password, profile_token, op, label="goto_preset")


def _set_preset_sync(ip: str, port: int, user: str, password: str, profile_token: Optional[str],
                     name: str, preset_token: Optional[str]) -> dict:
    """Store the camera's CURRENT position as a preset.

    The camera holds the position (that is what GotoPreset recalls); VMS keeps
    a record of it (services/ptz_presets) that is re-synced from the camera on
    every list, so a change made in the camera's own web UI still shows.
    """
    def op(ptz: Any, token: str) -> dict:
        req: dict[str, Any] = {"ProfileToken": token, "PresetName": name}
        if preset_token:
            req["PresetToken"] = preset_token
        resp = ptz.SetPreset(req)
        # Per spec SetPreset returns the new token; several firmwares return
        # nothing when OVERWRITING an existing preset, in which case the token
        # the caller sent is still the right answer.
        new_token = (
            getattr(resp, "PresetToken", None)
            or (resp if isinstance(resp, str) else None)
            or preset_token
        )
        return {"preset_token": new_token, "name": name}

    return _with_ptz(ip, port, user, password, profile_token, op, label="set_preset")


def _remove_preset_sync(ip: str, port: int, user: str, password: str,
                        profile_token: Optional[str], preset_token: str) -> dict:
    def op(ptz: Any, token: str) -> dict:
        ptz.RemovePreset({"ProfileToken": token, "PresetToken": preset_token})
        return {"preset_token": preset_token}

    return _with_ptz(ip, port, user, password, profile_token, op, label="remove_preset")


# ─── Home ─────────────────────────────────────────────────────────────────────

def _goto_home_sync(ip: str, port: int, user: str, password: str,
                    profile_token: Optional[str], speed: Optional[float]) -> dict:
    def op(ptz: Any, token: str) -> dict:
        req: dict[str, Any] = {"ProfileToken": token}
        if speed is not None:
            req["Speed"] = _vector(speed, speed, speed)
        try:
            ptz.GotoHomePosition(req)
        except Exception as exc:  # noqa: BLE001
            if "Speed" not in req:
                raise OnvifError(f"Camera refused the home position command: {exc}")
            req.pop("Speed")
            ptz.GotoHomePosition(req)
        return {"status": "moving_home"}

    return _with_ptz(ip, port, user, password, profile_token, op, label="goto_home")


def _set_home_sync(ip: str, port: int, user: str, password: str,
                   profile_token: Optional[str]) -> dict:
    def op(ptz: Any, token: str) -> dict:
        ptz.SetHomePosition({"ProfileToken": token})
        return {"status": "home_set"}

    try:
        return _with_ptz(ip, port, user, password, profile_token, op, label="set_home")
    except OnvifError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OnvifError(
            f"Camera does not allow the home position to be set over ONVIF: {exc}"
        )


# ─── Public async API ─────────────────────────────────────────────────────────

def _warm_sync(ip: str, port: int, user: str, password: str) -> bool:
    """Ensure THIS worker has a live session for the camera — build it if cold,
    touch it if warm — without a SOAP command. The keep-warm loop calls this in
    every worker so a camera under active control is warm on whichever worker
    the next command happens to land on (uvicorn runs --workers 4 and the pool
    is process-local, so a command scatters across four independent pools)."""
    _session(ip, port, user, password)
    return True


async def warm(ip: str, port: int, user: str, password: str) -> bool:
    return await _call(_warm_sync, ip, port, user, password)


async def probe(ip: str, port: int, user: str, password: str) -> dict:
    return await _call(_probe_sync, ip, port, user, password)


async def continuous_move(ip: str, port: int, user: str, password: str,
                          profile_token: Optional[str], *, pan: Optional[float],
                          tilt: Optional[float], zoom: Optional[float],
                          timeout_s: float) -> dict:
    return await _call(_continuous_sync, ip, port, user, password, profile_token,
                       pan, tilt, zoom, timeout_s)


async def stop(ip: str, port: int, user: str, password: str,
               profile_token: Optional[str]) -> dict:
    return await _call(_stop_sync, ip, port, user, password, profile_token)


async def relative_move(ip: str, port: int, user: str, password: str,
                        profile_token: Optional[str], *, pan: Optional[float],
                        tilt: Optional[float], zoom: Optional[float],
                        speed: Optional[float]) -> dict:
    return await _call(_relative_sync, ip, port, user, password, profile_token,
                       pan, tilt, zoom, speed)


async def absolute_move(ip: str, port: int, user: str, password: str,
                        profile_token: Optional[str], *, pan: Optional[float],
                        tilt: Optional[float], zoom: Optional[float],
                        speed: Optional[float]) -> dict:
    return await _call(_absolute_sync, ip, port, user, password, profile_token,
                       pan, tilt, zoom, speed)


async def get_status(ip: str, port: int, user: str, password: str,
                     profile_token: Optional[str]) -> dict:
    return await _call(_status_sync, ip, port, user, password, profile_token)


async def get_presets(ip: str, port: int, user: str, password: str,
                      profile_token: Optional[str]) -> list[dict]:
    return await _call(_presets_sync, ip, port, user, password, profile_token)


async def goto_preset(ip: str, port: int, user: str, password: str,
                      profile_token: Optional[str], preset_token: str,
                      speed: Optional[float]) -> dict:
    return await _call(_goto_preset_sync, ip, port, user, password, profile_token,
                       preset_token, speed)


async def set_preset(ip: str, port: int, user: str, password: str,
                     profile_token: Optional[str], name: str,
                     preset_token: Optional[str]) -> dict:
    return await _call(_set_preset_sync, ip, port, user, password, profile_token,
                       name, preset_token)


async def remove_preset(ip: str, port: int, user: str, password: str,
                        profile_token: Optional[str], preset_token: str) -> dict:
    return await _call(_remove_preset_sync, ip, port, user, password, profile_token,
                       preset_token)


async def goto_home(ip: str, port: int, user: str, password: str,
                    profile_token: Optional[str], speed: Optional[float]) -> dict:
    return await _call(_goto_home_sync, ip, port, user, password, profile_token, speed)


async def set_home(ip: str, port: int, user: str, password: str,
                   profile_token: Optional[str]) -> dict:
    return await _call(_set_home_sync, ip, port, user, password, profile_token)
