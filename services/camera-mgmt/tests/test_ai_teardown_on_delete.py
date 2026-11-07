"""Does deleting a camera stop the AI work it was costing?

THE DEFECT THIS FILE EXISTS FOR, found 2026-09-18 while fixing the update path.
`delete_camera` tore down the relay, both recording tracks and the motion
service, and told the frame broker, the analytics service and the search index
nothing at all. They kept decoding, detecting and ingesting the camera for ever.

AND NO RECONCILE LOOP COULD HEAL IT. Each loop builds BOTH its desired set and
its `absent` set from registry rows, then deliberately leaves alone any name it
does not recognise — another VMS may be feeding the same index, and removing a
stranger's camera would be worse than leaking one. A deleted camera has no row,
so it appears in neither set. It is not drift the next pass corrects; it is
invisible. With no camera left in the UI there was also nothing to switch it off
through, so the only cure was restarting the services.

Deletion is therefore one of the few operations that MUST be pushed, and this is
the only place that can push it — which is exactly the argument the comment
above `_teardown_recording_tracks` already makes for the sub track.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402

SLUG = "gate-a1b2"


class _DB:
    def __init__(self): self.deleted = []
    async def commit(self): return None
    async def refresh(self, obj): return None
    async def flush(self): return None
    async def delete(self, obj): self.deleted.append(obj)

    def begin(self):
        class _Tx:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return _Tx()


@pytest.fixture
def endpoint(monkeypatch):
    """DELETE /cameras/{id} with every collaborator stubbed, recording which
    services were told and in what order."""
    calls = SimpleNamespace(removed=[])
    cam = SimpleNamespace(id=uuid.uuid4(), slug=SLUG)

    async def get_cam(camera_id, db): return cam
    async def principal(request): return SimpleNamespace(roles=["admin"])
    async def noop(*a, **kw): return None
    async def teardown(slug, purge=False): return True

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router.audit_svc, "record", noop)
    monkeypatch.setattr(cam_router, "sync_camera", noop)
    monkeypatch.setattr(cam_router, "sync_removal", noop)
    monkeypatch.setattr(cam_router, "_teardown_recording_tracks", teardown)
    monkeypatch.setattr(cam_router.relay, "remove_path", noop)
    monkeypatch.setattr(cam_router.redis_client, "delete_stream_keys", noop)
    monkeypatch.setattr(settings, "motion_sync_enabled", False)

    def _record(name):
        async def remove(slug):
            calls.removed.append((name, slug))
            return True
        return remove

    for mod, name in ((cam_router.analytics_client, "analytics"),
                      (cam_router.smartsearch_sync, "smartsearch"),
                      (cam_router.frames_client, "frames")):
        monkeypatch.setattr(mod, "is_configured", lambda: True)
        monkeypatch.setattr(mod, "remove_camera", _record(name))

    monkeypatch.setattr(settings, "analytics_sync_enabled", True)
    monkeypatch.setattr(settings, "smartsearch_sync_enabled", True)
    monkeypatch.setattr(settings, "frames_sync_enabled", True)

    return SimpleNamespace(calls=calls, cam=cam, db=_DB(),
                           req=SimpleNamespace(headers={}, query_params={}))


async def _delete(endpoint, purge=False):
    return await cam_router.delete_camera(endpoint.cam.id, endpoint.req,
                                          purge_recordings=purge, db=endpoint.db)


@pytest.mark.asyncio
async def test_every_ai_service_is_told(endpoint):
    """All three, not just the index. Missing any one of them leaves the camera
    costing something nothing can switch off."""
    await _delete(endpoint)
    assert {name for name, _ in endpoint.calls.removed} == {
        "analytics", "smartsearch", "frames"}
    assert {slug for _, slug in endpoint.calls.removed} == {SLUG}


@pytest.mark.asyncio
async def test_the_broker_is_released_last(endpoint):
    """ORDER IS THE MIRROR OF THE ADD. The broker owns the decode thread and the
    shared-memory ring its consumers read; freeing it first would leave analytics
    and the index reading a camera that vanished underneath them."""
    await _delete(endpoint)
    assert [name for name, _ in endpoint.calls.removed][-1] == "frames"


@pytest.mark.asyncio
async def test_a_purge_does_not_change_who_is_told(endpoint):
    """`purge_recordings` is about FOOTAGE. Unregistering from ingest happens
    either way — a camera left out of the index because the operator did not
    tick purge would be a camera still being indexed after it was deleted."""
    await _delete(endpoint, purge=True)
    assert len(endpoint.calls.removed) == 3


@pytest.mark.asyncio
async def test_nothing_is_called_when_a_service_is_not_deployed(endpoint,
                                                                monkeypatch):
    """Each of the three is off by default (empty URL). A VMS running none of
    them must not spend three requests on every delete."""
    for mod in (cam_router.analytics_client, cam_router.smartsearch_sync,
                cam_router.frames_client):
        monkeypatch.setattr(mod, "is_configured", lambda: False)
    await _delete(endpoint)
    assert endpoint.calls.removed == []


@pytest.mark.asyncio
async def test_the_camera_row_is_still_deleted(endpoint):
    """The pushes are best-effort decoration on an operation that has already
    committed. Whatever they do, the camera is gone and the call returns 204."""
    resp = await _delete(endpoint)
    assert resp.status_code == 204
    assert endpoint.db.deleted == [endpoint.cam]
