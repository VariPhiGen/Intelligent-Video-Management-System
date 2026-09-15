"""Does turning Smart Search indexing off leave a trace?

Until 2026-09-07 it did not. `update_camera` computes `search_changed` and uses
it to push the deregistration to the index, and then built the audit `changed`
list from a hand-written field list that did not mention search. With motion
already off — every camera on the appliance — the list came out empty, the
`if changed:` guard failed, and nothing was recorded. Verified on hardware: the
audit log's last row for the camera stayed at the same id across both a stop and
a re-enable.

That is the most compliance-relevant switch on the screen (it is what stops
collecting person crops and plate reads), and the audit table is hash-chained
precisely so this class of event is evidence. So the test is about the audit
CALL, not about the flag: the flag already worked.

Same shape as test_sub_track_wiring.py — call the endpoint with the database
and every collaborator stubbed, and assert only on what reached the audit
service.

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


class _Missing(SimpleNamespace):
    """Unset attributes read as None rather than raising.

    Lets a case state only the fields it depends on, and keeps a new column or
    a new CameraUpdate field from breaking tests that have nothing to do with it.
    """
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class _DB:
    def __init__(self): self.commits = 0
    async def commit(self): self.commits += 1
    async def refresh(self, obj): return None
    async def flush(self): return None
    def begin(self):
        class _Tx:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return _Tx()


def _camera(**kw):
    base = dict(id=uuid.uuid4(), slug=SLUG, rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                enabled=True, recording=True, search_indexing=True,
                search_domains=["person", "vehicles"], motion_detection=False,
                stqc_status="unknown")
    base.update(kw)
    return _Missing(**base)


@pytest.fixture
def wired(monkeypatch):
    """Everything stubbed except the audit service, which is captured."""
    audits = []
    cam = _camera()

    async def get_cam(camera_id, db): return cam
    async def principal(request): return SimpleNamespace(roles=["admin"])
    async def sync_now(*a, **kw): return None
    async def record(request, prin, action, *, target=None, detail=None):
        audits.append({"action": action, "target": target, "detail": detail})

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router, "sync_now", sync_now)
    monkeypatch.setattr(cam_router.audit_svc, "record", record)
    monkeypatch.setattr(cam_router, "_build_response", lambda c: c)
    # The pushes have their own coverage; this test is only about the trail.
    monkeypatch.setattr(settings, "nvr_sync_enabled", False)
    monkeypatch.setattr(settings, "motion_sync_enabled", False)
    monkeypatch.setattr(settings, "smartsearch_sync_enabled", False)

    return SimpleNamespace(audits=audits, cam=cam, db=_DB(),
                           req=SimpleNamespace(headers={}, query_params={}))


def _changed(audits):
    return [a["detail"]["changed"] for a in audits if a["action"] == "camera.config_changed"]


@pytest.mark.asyncio
async def test_opting_a_camera_out_of_smart_search_is_audited(wired):
    """The one-click stop on the Recording tab, and the AI Config dropdown."""
    body = _Missing(search_indexing=False, motion_detection=False)
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _changed(wired.audits) == [["search"]], (
        "stopping indexing must be recorded; motion was already off, so nothing "
        "else in the payload can carry this event into the trail"
    )


@pytest.mark.asyncio
async def test_opting_back_in_is_audited_too(wired):
    """Both directions: 'when did this camera start being indexed again' is the
    same question asked from the other side."""
    wired.cam.search_indexing = False
    body = _Missing(search_indexing=True)
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _changed(wired.audits) == [["search"]]


@pytest.mark.asyncio
async def test_narrowing_the_domains_is_audited(wired):
    """Dropping 'plate' collects strictly less; an empty set is stored as off."""
    body = _Missing(search_domains=["person"])
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _changed(wired.audits) == [["search"]]


@pytest.mark.asyncio
async def test_a_reordered_domain_list_is_not_a_change(wired):
    """Guard the other way: auditing everything is as useless as auditing
    nothing. The stored value is sorted, so a reorder must stay silent."""
    body = _Missing(search_domains=["vehicles", "person"])
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert wired.audits == []


def _search_detail(audits):
    return [a["detail"].get("search") for a in audits if a["action"] == "camera.config_changed"]


@pytest.mark.asyncio
async def test_a_domain_change_records_what_it_was_and_what_it_became(wired):
    """`{"changed": ["search"]}` alone could not say WHICH collection moved.
    On 2026-09-15 face was switched off on one camera and on for another, and
    the trail recorded the same two words for both — it took the index's own
    rows to work out which way each had gone. Turning `face` on starts
    collecting biometric data, so the entry has to name it."""
    body = _Missing(search_domains=["face", "person", "vehicles"])
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _search_detail(wired.audits) == [{
        "before": {"indexing": True, "domains": ["person", "vehicles"]},
        "after": {"indexing": True, "domains": ["face", "person", "vehicles"]},
    }]


@pytest.mark.asyncio
async def test_opting_out_records_the_domains_that_stopped(wired):
    """Indexing off keeps the stored domains, and the entry says so — "stopped
    collecting person+vehicles", not just "indexing false"."""
    body = _Missing(search_indexing=False)
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _search_detail(wired.audits) == [{
        "before": {"indexing": True, "domains": ["person", "vehicles"]},
        "after": {"indexing": False, "domains": ["person", "vehicles"]},
    }]


@pytest.mark.asyncio
async def test_a_change_that_is_not_search_carries_no_search_detail(wired):
    """A before/after on every entry would bury the ones that matter."""
    body = _Missing(recording=False)
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _changed(wired.audits) == [["recording"]]
    assert _search_detail(wired.audits) == [None]


@pytest.mark.asyncio
async def test_search_is_recorded_alongside_the_other_fields(wired):
    """Stopping recording AND indexing in one PUT must not lose either."""
    body = _Missing(recording=False, search_indexing=False)
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)
    assert _changed(wired.audits) == [["recording", "search"]]
