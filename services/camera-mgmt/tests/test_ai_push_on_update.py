"""Does switching an AI domain on reach the services that DO the work?

THE DEFECT THIS FILE EXISTS FOR, observed on the appliance 2026-09-18. Turning
People or Vehicles on for one camera in AI Config appeared to do nothing:
`update_camera` pushed the change to the smartsearch INDEX immediately and to
nobody else. But the index only stores what it is sent — the frame broker
decodes the stream and the analytics service detects in those frames, and both
learned about the change only on their next reconcile sweep (60 s by default,
plus a model warm-up on top).

So the first camera an operator switched on produced nothing for a minute or
more. Working down a fleet, results appeared around the time the LAST camera was
switched on, which reads as "Smart Search needs every camera enabled". Faces
were the clearest case: the analytics engine loads the face reader when the
FIRST camera asks for the `face` domain (engine._note_face_demand), so the
checkbox IS the switch — it just was not reaching the engine.

The assertions are about WHICH SERVICES WERE TOLD, not about latency, because
latency is not testable here and was never the real problem: the index knowing
about a camera that nothing is decoding is not a slow success, it is a camera
that indexes nothing at all until a sweep happens to run.

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
    async def commit(self): return None
    async def refresh(self, obj): return None
    async def flush(self): return None

    def begin(self):
        class _Tx:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return _Tx()


class _Defaulting(SimpleNamespace):
    """Unset fields read as None, so a case states only what it depends on."""
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None

    def resolved_retention_minutes(self):
        """Delegates to the REAL CameraUpdate rather than re-implementing it.

        The endpoint reads retention through this method now, so a stub that
        returned a hard-coded answer would let every test here pass while the
        minutes/days conversion was broken.
        """
        from backend.models import CameraUpdate  # noqa: PLC0415 — test-local
        return CameraUpdate(
            retention_minutes=self.retention_minutes,
            retention_days=self.retention_days,
        ).resolved_retention_minutes()


@pytest.fixture
def endpoint(monkeypatch):
    """The update endpoint with every collaborator stubbed, capturing the two
    pushes this file is about."""
    # `order` records both pushes in one list: which service is told FIRST is
    # part of the contract, and two separate lists cannot express it.
    calls = SimpleNamespace(frames=[], analytics=[], order=[])
    # Starting state: a camera that records and runs NO AI, which is what a
    # newly added camera looks like since migration 038.
    cam = _Defaulting(id=uuid.uuid4(), slug=SLUG,
                      rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                      enabled=True, recording=True, privacy_masks=[],
                      motion_detection=False, search_indexing=False,
                      search_domains=["person", "vehicles"],
                      analytics_config={}, sub_track=None)

    async def get_cam(camera_id, db): return cam
    async def principal(request): return SimpleNamespace(roles=["admin"])
    async def noop(*a, **kw): return None

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router.audit_svc, "record", noop)
    monkeypatch.setattr(cam_router, "sync_camera", noop)
    monkeypatch.setattr(cam_router, "sync_removal", noop)
    monkeypatch.setattr(cam_router, "_build_response", lambda c: c)
    monkeypatch.setattr(cam_router, "_sync_recording_tracks", noop)
    monkeypatch.setattr(cam_router, "_teardown_recording_tracks", noop)
    monkeypatch.setattr(settings, "nvr_sync_enabled", False)
    monkeypatch.setattr(settings, "motion_sync_enabled", False)
    monkeypatch.setattr(settings, "smartsearch_sync_enabled", False)

    async def frames_push(slug, enabled, motion, indexing, domains, cfg):
        calls.frames.append((slug, enabled, motion, indexing,
                             tuple(sorted(domains or ()))))
        calls.order.append("frames")

    async def analytics_push(slug, enabled, indexing, domains, cfg):
        calls.analytics.append((slug, enabled, indexing,
                                tuple(sorted(domains or ()))))
        calls.order.append("analytics")

    monkeypatch.setattr(cam_router.frames_client, "push_camera", frames_push)
    monkeypatch.setattr(cam_router.analytics_client, "push_camera", analytics_push)

    return SimpleNamespace(calls=calls, cam=cam, db=_DB(),
                           req=SimpleNamespace(headers={}, query_params={}))


async def _put(endpoint, **fields):
    await cam_router.update_camera(endpoint.cam.id, _Defaulting(**fields),
                                   endpoint.req, endpoint.db)


@pytest.mark.asyncio
async def test_switching_indexing_on_reaches_the_broker_and_analytics(endpoint):
    """One camera, one switch. Both producers are told, with the state AFTER
    the change — a push carrying the old flags would be worse than none."""
    await _put(endpoint, search_indexing=True)
    assert endpoint.calls.frames == [
        (SLUG, True, False, True, ("person", "vehicles"))]
    assert endpoint.calls.analytics == [
        (SLUG, True, True, ("person", "vehicles"))]


@pytest.mark.asyncio
async def test_the_broker_is_told_before_analytics(endpoint):
    """ORDER IS LOAD-BEARING: analytics reads frames from the broker, so the
    other way round registers a detector against a camera nothing is decoding
    and leaves it waiting for notices until the next sweep anyway."""
    await _put(endpoint, search_indexing=True)
    assert endpoint.calls.order == ["frames", "analytics"]


@pytest.mark.asyncio
async def test_enabling_the_face_domain_reaches_analytics(endpoint):
    """FACES ARE THE CASE THAT MADE THIS VISIBLE. The engine loads the face
    reader on first demand and releases it when demand goes, so this push is
    the whole mechanism by which the checkbox starts face search."""
    endpoint.cam.search_indexing = True
    await _put(endpoint, search_domains=["person", "vehicles", "face"])
    assert endpoint.calls.analytics == [
        (SLUG, True, True, ("face", "person", "vehicles"))]


@pytest.mark.asyncio
async def test_narrowing_the_domains_reaches_analytics_too(endpoint):
    """Symmetry matters as much as the switch-on: analytics applies the domain
    set, so a camera narrowed to People must stop paying to crop and plate-read
    vehicles now, not a minute from now."""
    endpoint.cam.search_indexing = True
    await _put(endpoint, search_domains=["person"])
    assert endpoint.calls.analytics == [(SLUG, True, True, ("person",))]


@pytest.mark.asyncio
async def test_disabling_the_camera_reaches_both(endpoint):
    """A disabled camera must stop being decoded and detected at once. Leaving
    it to the sweep keeps a dead stream's capture thread alive for a minute."""
    endpoint.cam.search_indexing = True
    await _put(endpoint, enabled=False)
    assert endpoint.calls.frames == [
        (SLUG, False, False, True, ("person", "vehicles"))]
    assert endpoint.calls.analytics == [
        (SLUG, False, True, ("person", "vehicles"))]


@pytest.mark.asyncio
async def test_a_motion_change_reaches_the_broker(endpoint):
    """The broker's desired set is the UNION of motion's cameras and analytics'.
    `motion_client`'s own push does not reach it, so without motion_changed in
    the trigger a motion-only camera still waits out a sweep for its frames."""
    await _put(endpoint, motion_detection=True)
    assert endpoint.calls.frames == [
        (SLUG, True, True, False, ("person", "vehicles"))]


@pytest.mark.asyncio
async def test_an_unrelated_change_pushes_nothing(endpoint):
    """A rename changes nothing either service acts on. Pushing anyway would
    restart a capture thread on every edit to a camera's name."""
    await _put(endpoint, name="Gate A")
    assert endpoint.calls.frames == []
    assert endpoint.calls.analytics == []
