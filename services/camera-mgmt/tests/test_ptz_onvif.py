"""Unit tests for the ONVIF PTZ protocol layer (no camera required).

These pin the two things vendors punish you for getting wrong, both learned
from how the rest of this codebase describes real LAN cameras:

  • an axis that is not being driven must be ABSENT from the SOAP body, not
    present-and-zero (several firmwares fault on Zoom:{x:0} to a pan-only head);
  • a camera that reports no PTZ spaces at all must degrade to continuous-only,
    not to "supports everything" (a fault on every absolute move) and not to
    "supports nothing" (a working dome rendered dead).
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import onvif_ptz  # noqa: E402


class Node:
    """Stand-in for a zeep PTZ node — attributes only, absent means absent."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── Axis presence ────────────────────────────────────────────────────────────

def test_an_unused_axis_is_absent_not_zero():
    assert onvif_ptz._vector(0.5, None, None) == {"PanTilt": {"x": 0.5, "y": 0.0}}
    assert onvif_ptz._vector(None, None, 1.0) == {"Zoom": {"x": 1.0}}
    assert onvif_ptz._vector(0.2, -0.3, 0.4) == {
        "PanTilt": {"x": 0.2, "y": -0.3}, "Zoom": {"x": 0.4},
    }


def test_an_explicit_zero_is_still_a_move():
    """0.0 and None mean different things: 0.0 is 'drive this axis at zero
    velocity' (a valid stop-one-axis), None is 'this axis is not in the
    command'. Collapsing them would make a pan-only stop impossible."""
    assert onvif_ptz._vector(0.0, 0.0, None) == {"PanTilt": {"x": 0.0, "y": 0.0}}


def test_an_empty_vector_is_empty_so_the_caller_can_refuse():
    assert onvif_ptz._vector(None, None, None) == {}


def test_speed_follows_the_same_axis_presence_rule():
    assert onvif_ptz._speed_vector(None, 0.5, None, None) is None
    assert onvif_ptz._speed_vector(0.8, 0.5, None, None) == {"PanTilt": {"x": 0.8, "y": 0.0}}
    assert onvif_ptz._speed_vector(0.8, None, None, 1.0) == {"Zoom": {"x": 0.8}}


# ── Capability parsing ───────────────────────────────────────────────────────

def test_a_camera_reporting_no_spaces_degrades_to_continuous_only():
    """Budget firmware routinely advertises no SupportedPTZSpaces and still
    drives perfectly with ContinuousMove. Claiming absolute support for those
    cameras would offer a control that faults; claiming nothing would hide a
    working dome."""
    s = onvif_ptz._spaces(None)
    assert s["continuous"] == {"pan_tilt": True, "zoom": True}
    assert s["relative"] == {"pan_tilt": False, "zoom": False}
    assert s["absolute"] == {"pan_tilt": False, "zoom": False}
    assert s["spaces_reported"] is False


def test_a_camera_that_reports_spaces_is_taken_at_its_word():
    node = Node(SupportedPTZSpaces=Node(
        ContinuousPanTiltVelocitySpace=["x"],
        ContinuousZoomVelocitySpace=[],
        RelativePanTiltTranslationSpace=["x"],
        AbsolutePanTiltPositionSpace=["x"],
        AbsoluteZoomPositionSpace=["x"],
    ))
    s = onvif_ptz._spaces(node)
    assert s["continuous"] == {"pan_tilt": True, "zoom": False}
    assert s["relative"] == {"pan_tilt": True, "zoom": False}
    assert s["absolute"] == {"pan_tilt": True, "zoom": True}
    assert s["spaces_reported"] is True


def test_ptz_profile_skips_profiles_with_no_ptz_configuration():
    """Addressing PTZ with the sub stream's token fails with a bare NoConfig
    fault that reads like the camera has no PTZ at all."""
    media = Node(GetProfiles=lambda: [
        Node(token="sub", PTZConfiguration=None),
        Node(token="main", PTZConfiguration=Node(NodeToken="NODE_1")),
    ])
    token, conf = onvif_ptz._ptz_profile(media)
    assert token == "main"
    assert conf.NodeToken == "NODE_1"


def test_ptz_profile_returns_none_when_nothing_is_bound():
    media = Node(GetProfiles=lambda: [Node(token="only", PTZConfiguration=None)])
    assert onvif_ptz._ptz_profile(media) == (None, None)


def test_preset_entry_survives_a_nameless_preset():
    """Name is optional in the spec and blank on several vendors' factory
    presets; the row must still be usable (the UI falls back to the token)."""
    p = Node(token="1", Name=None, PTZPosition=Node(PanTilt=Node(x=0.5, y=-0.2), Zoom=Node(x=0.1)))
    assert onvif_ptz._preset_entry(p) == {
        "token": "1", "name": None, "pan": 0.5, "tilt": -0.2, "zoom": 0.1,
    }


def test_preset_entry_survives_a_preset_with_no_position_at_all():
    assert onvif_ptz._preset_entry(Node(token="7", Name="Gate")) == {
        "token": "7", "name": "Gate", "pan": None, "tilt": None, "zoom": None,
    }


# ── Session pool ─────────────────────────────────────────────────────────────

def test_a_session_is_keyed_by_credentials_not_just_address():
    """A password change must not reuse a client authenticated with the old
    one — that would keep 401-ing until the process restarted."""
    onvif_ptz._sessions.clear()
    k_old = ("1.2.3.4", 80, "u", "old")
    k_new = ("1.2.3.4", 80, "u", "new")
    onvif_ptz._sessions[k_old] = onvif_ptz._Session(object(), object(), "P1")
    assert k_new not in onvif_ptz._sessions
    onvif_ptz._drop(k_old)
    assert onvif_ptz._sessions == {}


def test_idle_sessions_are_evicted():
    """Otherwise the pool grows one live HTTP connection per camera, forever."""
    onvif_ptz._sessions.clear()
    key = ("1.2.3.4", 80, "u", "p")
    sess = onvif_ptz._Session(object(), object(), "P1")
    sess.touched = time.monotonic() - (onvif_ptz._SESSION_IDLE_SEC + 1)
    onvif_ptz._sessions[key] = sess
    onvif_ptz._evict_idle(time.monotonic())
    assert key not in onvif_ptz._sessions
