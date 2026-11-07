"""Regression tests for the ONVIF-over-HTTPS fix.

A camera whose firmware answers at least one ONVIF operation (confirmed:
GetSystemDateAndTime, fired by every client build via adjust_time) only over
HTTPS made every live ONVIF call 502 "did not answer in time" — PTZ, encoder,
imaging, OSD — while the RTSP stream (a separate, scheme-less path) kept
working. onvif-zeep decides the scheme purely from whether the host string
it's handed already starts with "https://"; the port number plays no part.

These tests pin the fix at three levels: the pure host-building function,
the router helper that every ONVIF route shares, and the credentials
endpoint that is the only place an operator can turn the flag on.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.crypto import encrypt  # noqa: E402
from backend.models import onvif_host  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402
from backend.services.onvif_media import OnvifError  # noqa: E402


# ── onvif_host(): the pure scheme-building function ────────────────────────

def test_plain_http_by_default():
    assert onvif_host("192.168.0.127", None) == "192.168.0.127"
    assert onvif_host("192.168.0.127", False) == "192.168.0.127"


def test_https_when_flagged():
    assert onvif_host("192.168.0.127", True) == "https://192.168.0.127"


def test_no_ip_stays_falsy_even_when_flagged():
    # Nothing to prefix — callers gate on `ip` truthiness separately
    # (_onvif_target_or_409 raises 409 before onvif_host ever sees a None ip).
    assert onvif_host(None, True) is None
    assert onvif_host("", True) == ""


# ── _onvif_target_or_409(): the router helper every ONVIF route shares ────

class _Cam(SimpleNamespace):
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


def _camera(**kw):
    base = dict(
        id=uuid.uuid4(), slug="gate-a1b2", ip="192.168.0.127", onvif_port=80,
        enc_username=encrypt("admin"), enc_password=encrypt("secret"),
    )
    base.update(kw)
    return _Cam(**base)


def test_target_stays_plain_http_when_unflagged():
    ip, port, user, password = cam_router._onvif_target_or_409(_camera())
    assert ip == "192.168.0.127"
    assert (port, user, password) == (80, "admin", "secret")


def test_target_is_https_prefixed_when_flagged():
    ip, _port, _user, _password = cam_router._onvif_target_or_409(
        _camera(onvif_use_https=True)
    )
    assert ip == "https://192.168.0.127"


def test_target_still_refuses_without_credentials():
    with pytest.raises(cam_router.HTTPException) as exc:
        cam_router._onvif_target_or_409(_camera(enc_password=None))
    assert exc.value.status_code == 409


# ── set_onvif_credentials: the only place the flag can be turned on ───────

class _DB:
    def __init__(self): self.commits = 0
    async def commit(self): self.commits += 1


@pytest.mark.asyncio
async def test_verify_and_save_probes_https_when_the_operator_checks_the_box(monkeypatch):
    """The bug this fixes: a camera whose ONVIF needs HTTPS 502'd forever
    because nothing in the app ever built an https:// target for it. Saving
    with onvif_use_https=True must probe (and therefore verify) over HTTPS,
    not silently keep using plain HTTP."""
    cam = _camera(onvif_port=None, onvif_use_https=None)
    seen = SimpleNamespace(verify_host=None, probe_host=None)

    async def get_cam(camera_id, db): return cam

    async def verify_device(host, port, user, password):
        seen.verify_host = host
        return {"vendor": "Acme", "model": "X1", "firmware": "1.0"}

    async def probe(host, port, user, password):
        seen.probe_host = host
        return {"supported": False, "reason": "Camera advertises no ONVIF PTZ service"}

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router.onvif_device, "verify_device", verify_device)
    monkeypatch.setattr(cam_router.onvif_ptz, "probe", probe)

    body = cam_router.OnvifCredentialsBody(
        username="admin", password="secret", onvif_port=443, onvif_use_https=True,
    )
    result = await cam_router.set_onvif_credentials(cam.id, body, _DB())

    assert seen.verify_host == "https://192.168.0.127"
    assert seen.probe_host == "https://192.168.0.127"
    assert cam.onvif_use_https is True
    assert result["onvif_use_https"] is True


@pytest.mark.asyncio
async def test_verify_and_save_keeps_plain_http_by_default(monkeypatch):
    """Every camera that works today must keep working exactly as before —
    omitting onvif_use_https from the request must not flip a previously
    unset camera to HTTPS."""
    cam = _camera()
    seen = SimpleNamespace(verify_host=None)

    async def get_cam(camera_id, db): return cam

    async def verify_device(host, port, user, password):
        seen.verify_host = host
        return {"vendor": None, "model": None, "firmware": None}

    async def probe(host, port, user, password):
        raise OnvifError("Camera advertises no ONVIF PTZ service")

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router.onvif_device, "verify_device", verify_device)
    monkeypatch.setattr(cam_router.onvif_ptz, "probe", probe)

    body = cam_router.OnvifCredentialsBody(username="admin", password="secret")
    result = await cam_router.set_onvif_credentials(cam.id, body, _DB())

    assert seen.verify_host == "192.168.0.127"
    assert cam.onvif_use_https is False
    assert result["onvif_use_https"] is False


def test_onvif_error_is_never_blank():
    """A zeep / requests / ssl exception that stringifies to "" used to reach the UI as a bare "Request failed (HTTP
    502)". The class name at least says whether it was TLS, a refused connection or a timeout."""
    import asyncio

    from backend.services import onvif_media

    class SSLZeroReturnError(Exception):
        pass

    def boom():
        raise SSLZeroReturnError()

    try:
        asyncio.run(onvif_media._call(boom))
    except onvif_media.OnvifError as exc:
        assert "SSLZeroReturnError" in str(exc) and exc.auth is False
    else:
        raise AssertionError("expected OnvifError")
