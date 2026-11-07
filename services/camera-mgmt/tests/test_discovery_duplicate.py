"""Adding a stream that is already a camera must say so, not report success.

Found 2026-10-26: an operator added channel=21 of an NVR that was already
registered as "top". `_register` hit the unique rtsp_url index and returned
False, but both add endpoints still answered 200 — the wizard showed a green
"added to relay — Already registered in the relay" toast, moved on, and the
camera never appeared. They retried; same silent result.

The contract pinned here:
  1. the refusal names the camera that already owns the stream
  2. both endpoints turn it into a 409, so every wizard caller shows it as an
     error and stays put

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings  # noqa: E402
from backend.routers import discovery as disc  # noqa: E402

pytestmark = pytest.mark.asyncio


class _DupDB:
    """The `fail_on`-th commit trips the unique rtsp_url index; lookups find
    `owner`. add_manual commits its staging row first, so it fails on 2."""
    def __init__(self, owner, fail_on=1):
        self.owner = owner
        self.fail_on = fail_on
        self.commits = 0
    async def commit(self):
        self.commits += 1
        if self.commits == self.fail_on:
            raise IntegrityError("stmt", {}, Exception("ix_cameras_rtsp_url"))
    async def rollback(self): return None
    async def refresh(self, obj): return None
    async def flush(self): return None
    def add(self, obj): return None
    async def execute(self, *a, **kw):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []),
                               scalar_one_or_none=lambda: self.owner)


def _camera():
    return SimpleNamespace(
        id=uuid.uuid4(), ip="10.0.0.5", slug=None, name=None, rtsp_url=None,
        enabled=False, recording=True, stage="discovered", vendor=None,
        model=None, rtsp_port=554, enc_username=None, enc_password=None,
        rtsp_candidates=[{"profile": "p", "token": "t",
                          "url_raw": "rtsp://10.0.0.5:554/main", "verified": True}],
        privacy_masks=[], camera_metadata={}, discovery_error=None,
        last_scanned_at=None,
    )


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    async def record(*a, **kw): return None
    async def ensure_unique(slug, db): return None
    monkeypatch.setattr(disc.audit_svc, "record", record)
    monkeypatch.setattr(disc, "_ensure_unique_slug", ensure_unique)
    monkeypatch.setattr(disc, "validate_lawful_basis",
                        lambda b, p: (b or "public_safety", p or "test"))
    monkeypatch.setattr(disc, "generate_slug", lambda n: "gate-a1b2")
    monkeypatch.setattr(disc, "encrypt", lambda v: b"enc")
    monkeypatch.setattr(disc, "decrypt", lambda v: "u")
    monkeypatch.setattr(settings, "nvr_sync_enabled", False)


async def test_the_refusal_names_the_camera_that_owns_the_stream():
    cam = _camera()
    ok = await disc._register(
        cam, _DupDB(owner="top"), name="Gate",
        rtsp_url="rtsp://u:pw@10.0.0.5:554/main", enabled=False,
        lawful_basis="public_safety", purpose="perimeter",
    )
    assert ok is False
    assert '"top"' in cam.discovery_error


async def test_the_refusal_still_reads_when_the_owner_cannot_be_found():
    cam = _camera()
    await disc._register(
        cam, _DupDB(owner=None), name="Gate",
        rtsp_url="rtsp://u:pw@10.0.0.5:554/main", enabled=False,
        lawful_basis="public_safety", purpose="perimeter",
    )
    assert "already added" in cam.discovery_error


async def test_a_manual_add_of_an_existing_stream_is_409(monkeypatch):
    monkeypatch.setattr(disc, "_out", lambda c: c)
    body = disc.ManualAddBody(rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                              lawful_basis="public_safety", purpose="perimeter",
                              verify=False)
    with pytest.raises(HTTPException) as exc:
        await disc.add_manual(body, None, _DupDB(owner="top", fail_on=2))
    assert exc.value.status_code == 409
    assert '"top"' in exc.value.detail


async def test_promoting_a_device_onto_an_existing_stream_is_409(monkeypatch):
    cam = _camera()
    async def get(device_id, db): return cam
    monkeypatch.setattr(disc, "_get_or_404", get)
    monkeypatch.setattr(disc, "_staging_or_409", lambda c, action: None)
    monkeypatch.setattr(disc, "_out", lambda c: c)
    body = disc.AddBody(name="Gate", profile_index=0, verify=False,
                        lawful_basis="public_safety", purpose="perimeter")
    with pytest.raises(HTTPException) as exc:
        await disc.add_to_relay(cam.id, body, None, _DupDB(owner="top"))
    assert exc.value.status_code == 409
    assert '"top"' in exc.value.detail
