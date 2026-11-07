"""Where ONVIF dials: `cameras.ip` as a DEVICE address, not a stream host.

Every ONVIF surface — maintenance, imaging, OSD, encoder, PTZ — resolves its
target from `cameras.ip`, and that column was filled from the RTSP URL's host on
create, on bulk import, and again on every URL edit. The host is the camera only
when nothing re-publishes the stream. Register 121 cameras by a relay's
re-published URLs (`rtsp://<relay>:8554/<slug>`) and all 121 carry the relay's
address, so every device setting fails with a connection refused against a host
the operator never typed — the relay speaks RTSP on 8554 and no ONVIF at all.

So the rules asserted here:

  • a device address may be given explicitly, at create and in bulk, and is
    never a URL (a scheme, port or path in it is a silently dead target);
  • an address the DEVICE has answered on is never overwritten by a stream host;
  • an unverified one still follows the RTSP URL, which is what keeps the
    scanner's dedupe key current for the cameras discovery finds.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.models import CameraCreate, OnvifCredentialsBody, validate_device_address
from backend.routers.cameras import (
    _device_address_after_url_change,
    _onvif_verified,
)


# ── The address itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize("given,stored", [
    ("192.168.2.155", "192.168.2.155"),
    ("  192.168.2.155  ", "192.168.2.155"),     # pasted out of a spreadsheet
    ("cam-lobby.vms.lan", "cam-lobby.vms.lan"),  # DHCP reservation + internal DNS
    ("fd00::1", "fd00::1"),
    ("", None),                                  # an empty CSV column is "unset"
    ("   ", None),
    (None, None),
])
def test_an_address_is_taken_as_given(given, stored):
    assert validate_device_address(given) == stored


@pytest.mark.parametrize("bad", [
    "rtsp://192.168.2.155:554/cam/realmonitor",  # the whole URL
    "http://192.168.2.155",
    "192.168.2.155:80",                          # port belongs in onvif_port
    "192.168.2.155/onvif/device_service",
    "admin@192.168.2.155",
    "192.168.2.155 192.168.2.156",
    "-cam.vms.lan",
])
def test_an_address_that_is_not_an_address_is_refused(bad):
    """Each of these parses into an unreachable ONVIF target and shows up much
    later as a connection error, so it is refused at the edge."""
    with pytest.raises(ValueError):
        validate_device_address(bad)


def test_a_create_body_carries_an_explicit_device_address():
    body = CameraCreate(
        name="Lobby", rtsp_url="rtsp://192.168.2.240:8554/lobby-a1b2",
        ip="192.168.2.155",
    )
    assert body.ip == "192.168.2.155"


def test_a_create_body_without_one_leaves_it_unset():
    """Absent means "fall back to the RTSP host" — the handler decides, so the
    model must not invent an address of its own."""
    body = CameraCreate(name="Lobby", rtsp_url="rtsp://192.168.2.155:554/cam/realmonitor")
    assert body.ip is None


def test_a_credentials_body_may_carry_a_corrected_address():
    body = OnvifCredentialsBody(username="admin", password="x", ip=" 192.168.2.155 ")
    assert body.ip == "192.168.2.155"


def test_a_credentials_body_rejects_a_url_as_an_address():
    with pytest.raises(ValueError):
        OnvifCredentialsBody(username="admin", password="x", ip="rtsp://192.168.2.155:554/s")


# ── The address under an RTSP URL edit ─────────────────────────────────────

def _camera(ip, *, verified):
    return SimpleNamespace(
        ip=ip, slug="lobby",
        onvif_port=80 if verified else None,
        enc_password=b"fernet" if verified else None,
    )


def test_a_verified_address_survives_a_relay_url():
    """The regression: pointing a working camera at a relay's re-published URL
    must not move its ONVIF target onto the relay."""
    cam = _camera("192.168.2.155", verified=True)
    assert _device_address_after_url_change(
        cam, "rtsp://192.168.2.240:8554/lobby-a1b2") == "192.168.2.155"


def test_an_unverified_address_follows_the_url():
    """Nothing has proven the stored address, and the scanner dedupes on it, so
    the newest guess is the better guess."""
    cam = _camera("192.168.2.9", verified=False)
    assert _device_address_after_url_change(
        cam, "rtsp://192.168.2.155:554/cam/realmonitor") == "192.168.2.155"


def test_a_camera_with_no_address_gets_one_from_the_url():
    cam = _camera(None, verified=False)
    assert _device_address_after_url_change(
        cam, "rtsp://192.168.2.155:554/cam/realmonitor") == "192.168.2.155"


def test_verified_means_all_three_of_address_port_and_credentials():
    assert _onvif_verified(_camera("192.168.2.155", verified=True))
    assert not _onvif_verified(_camera("192.168.2.155", verified=False))
    assert not _onvif_verified(_camera(None, verified=True))


# ── Over HTTP ──────────────────────────────────────────────────────────────
#
# The stub session answers reads empty, so these assert the CREATE path — the
# one that decides what address a bulk/manual registration lands with.

VALID_CAMERA = {
    "name": "Lobby",
    "rtsp_url": "rtsp://192.168.2.240:8554/lobby-a1b2",
    "lawful_basis": "Public safety / State function",
    "purpose": "Lobby monitoring",
}


@pytest.fixture
def admin(mint):
    return mint(["admin"])


@pytest.mark.asyncio
async def test_registering_by_a_relay_url_can_still_name_the_camera(client, admin):
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "ip": "192.168.2.155"})
    assert r.status_code == 201, r.text
    assert r.json()["ip"] == "192.168.2.155"


@pytest.mark.asyncio
async def test_without_an_address_the_rtsp_host_is_still_the_fallback(client, admin):
    r = await client.post("/api/cameras", headers=admin, json=VALID_CAMERA)
    assert r.status_code == 201, r.text
    assert r.json()["ip"] == "192.168.2.240"


@pytest.mark.asyncio
async def test_an_address_that_is_a_url_is_422(client, admin):
    r = await client.post("/api/cameras", headers=admin,
                          json={**VALID_CAMERA, "ip": "rtsp://192.168.2.155:554/s"})
    assert r.status_code == 422
    assert any("ip" in str(e.get("loc", "")) for e in r.json()["detail"]), r.text
