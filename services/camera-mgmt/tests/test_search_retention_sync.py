"""The index must not outlive the footage it describes.

Smart Search stores a crop of every person and vehicle it detects. A crop is
personal data cut FROM a recording, so the retention an operator sets on a
camera has to bind it: a camera dropped to two days of footage that leaves
thirty days of crops searchable is the appliance keeping — and, through the AI
Analytics dashboard, still SHOWING — personal data it has told the operator, and
through the compliance posture the data subject, that it deleted.

Two halves are pinned here. That the registry resolves retention to a CONCRETE
number before pushing it (the two services' notions of "default" are different
numbers and nothing keeps them in step), and that reconcile treats retention
drift the way it treats domain drift — because a push that failed is otherwise
invisible until a restart.

Run: python3 -m pytest tests/test_search_retention_sync.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services import smartsearch_sync  # noqa: E402
from backend.config import settings  # noqa: E402


# ── resolving "default" ──────────────────────────────────────────────────────

def test_an_explicit_camera_retention_is_used_as_is():
    assert smartsearch_sync.effective_retention_days(2) == 2


def test_no_camera_retention_resolves_to_the_appliance_recording_default():
    """NOT to the index's own SEARCH_RETENTION_DAYS. Those are two different
    numbers, and sending None would let the index keep crops for thirty days on
    an appliance recording for two."""
    assert (smartsearch_sync.effective_retention_days(None)
            == settings.nvr_default_retention_days)


def test_zero_means_reset_to_default_not_expire_immediately():
    """0 is the registry's sentinel for "clear the override" (routers/cameras.py
    stores it as NULL). Forwarding a literal 0 would expire every crop from that
    camera on the next sweep."""
    assert (smartsearch_sync.effective_retention_days(0)
            == settings.nvr_default_retention_days)


# ── drift ────────────────────────────────────────────────────────────────────

class Recorder:
    """Stands in for the index service."""

    def __init__(self, current):
        self.current = current
        self.added: list[tuple] = []
        self.removed: list[str] = []

    async def list_cameras(self):
        return self.current

    async def add_camera(self, slug, domains=None, retention_days=None):
        self.added.append((slug, tuple(domains or ()), retention_days))
        return True

    async def remove_camera(self, slug):
        self.removed.append(slug)
        return True


@pytest.fixture
def index(monkeypatch):
    def _make(current):
        rec = Recorder(current)
        monkeypatch.setattr(smartsearch_sync, "list_cameras", rec.list_cameras)
        monkeypatch.setattr(smartsearch_sync, "add_camera", rec.add_camera)
        monkeypatch.setattr(smartsearch_sync, "remove_camera", rec.remove_camera)
        return rec
    return _make


@pytest.mark.asyncio
async def test_a_new_camera_is_added_with_its_retention(index):
    rec = index({})
    await smartsearch_sync.reconcile({"a": (["person"], 2)}, set())
    assert rec.added == [("a", ("person",), 2)]


@pytest.mark.asyncio
async def test_retention_drift_is_corrected(index):
    """The failure this exists for: the immediate push from the update handler
    failed, so the index is still holding this camera's crops on the old clock
    and nothing else in the system would notice."""
    rec = index({"a": (["person"], 30)})
    await smartsearch_sync.reconcile({"a": (["person"], 2)}, set())
    assert rec.added == [("a", ("person",), 2)]


@pytest.mark.asyncio
async def test_an_index_running_on_its_own_default_counts_as_drift(index):
    """`None` on the index side means it never received this camera's policy and
    is falling back to SEARCH_RETENTION_DAYS. That is drift however the two
    numbers happen to line up today — the next change to either one separates
    them silently."""
    rec = index({"a": (["person"], None)})
    await smartsearch_sync.reconcile({"a": (["person"], None)}, set())
    assert rec.added == [("a", ("person",),
                          settings.nvr_default_retention_days)]


@pytest.mark.asyncio
async def test_a_converged_camera_is_left_alone(index):
    """Reconcile runs in four workers on every sync interval, and each add
    restamps rows. Re-asserting a camera that already agrees would be an UPDATE
    over the whole index, four times a minute."""
    rec = index({"a": (["person"], settings.nvr_default_retention_days)})
    await smartsearch_sync.reconcile({"a": (["person"], None)}, set())
    assert rec.added == []


@pytest.mark.asyncio
async def test_domain_drift_still_heals_and_carries_retention(index):
    rec = index({"a": (["person"], 2)})
    await smartsearch_sync.reconcile({"a": (["person", "vehicles"], 2)}, set())
    assert rec.added == [("a", ("person", "vehicles"), 2)]


@pytest.mark.asyncio
async def test_an_unreachable_index_changes_nothing(index):
    """Not "the index has no cameras". Acting on an unknown would remove every
    camera the registry knows about."""
    rec = index(None)
    await smartsearch_sync.reconcile({"a": (["person"], 2)}, {"b"})
    assert rec.added == [] and rec.removed == []


# ── wiring: does PUT /cameras/{id} actually push the change? ─────────────────
#
# The tests above pin the reconcile loop, which is the BACKSTOP — it converges
# within nvr_sync_interval. That is not the same as the endpoint doing its job,
# and the difference is a minute of an appliance advertising a retention it is
# not applying to the crops. Same rationale as test_sub_track_wiring.py: shallow,
# about which collaborator was reached and with what, so deleting the call from
# the endpoint body fails something here.

import uuid  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from backend.routers import cameras as cam_router  # noqa: E402


class _DB:
    async def commit(self): return None
    async def refresh(self, obj): return None
    async def flush(self): return None
    async def execute(self, *a, **kw):
        return SimpleNamespace(scalar_one_or_none=lambda: None,
                               scalars=lambda: SimpleNamespace(all=lambda: []),
                               all=lambda: [], scalar=lambda: 0)

    def begin(self):
        class _Tx:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False
        return _Tx()


class _Defaulting(SimpleNamespace):
    """Unset fields read as None rather than raising, so a test states only what
    its assertion depends on."""
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


@pytest.fixture
def endpoint(monkeypatch):
    calls = SimpleNamespace(search=[], removed=[], nvr_retention=[])
    cam = _Defaulting(id=uuid.uuid4(), slug="gate-a1b2",
                      rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                      enabled=True, recording=False, privacy_masks=[],
                      retention_days=30, search_indexing=True,
                      search_domains=["person", "vehicles"], sub_track=None)

    async def get_cam(camera_id, db): return cam
    async def principal(request): return SimpleNamespace(roles=["admin"])
    async def noop(*a, **kw): return None

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router.audit_svc, "record", noop)
    monkeypatch.setattr(cam_router, "sync_now", noop)
    monkeypatch.setattr(cam_router, "_build_response", lambda c: c)
    monkeypatch.setattr(cam_router, "_sync_recording_tracks", noop)
    monkeypatch.setattr(cam_router, "_teardown_recording_tracks", noop)
    monkeypatch.setattr(settings, "motion_sync_enabled", False)
    monkeypatch.setattr(settings, "nvr_sync_enabled", True)
    monkeypatch.setattr(settings, "smartsearch_sync_enabled", True)

    class _NVR:
        async def set_retention(self, slug, days):
            calls.nvr_retention.append((slug, days)); return True
        async def set_groom(self, *a, **kw): return True
        async def invalidate_codec_cache(self, *a, **kw): return True
    monkeypatch.setattr(cam_router, "nvr_client", _NVR())

    async def restart(slug, domains=None, retention_days=None):
        calls.search.append((slug, tuple(domains or ()), retention_days))
        return True

    async def remove(slug):
        calls.removed.append(slug); return True

    monkeypatch.setattr(cam_router.smartsearch_sync, "is_configured", lambda: True)
    monkeypatch.setattr(cam_router.smartsearch_sync, "restart_camera", restart)
    monkeypatch.setattr(cam_router.smartsearch_sync, "remove_camera", remove)

    return SimpleNamespace(calls=calls, cam=cam, db=_DB(),
                           req=SimpleNamespace(headers={}, query_params={}))


@pytest.mark.asyncio
async def test_a_retention_change_alone_reaches_the_index(endpoint):
    """THE REGRESSION THIS FILE EXISTS FOR. Before this, the Smart Search push
    fired only on a domain or enabled change, so shortening retention updated
    the NVR and left the crops cut from that footage on the old clock."""
    body = _Defaulting(retention_days=2)
    await cam_router.update_camera(endpoint.cam.id, body, endpoint.req, endpoint.db)
    assert endpoint.calls.nvr_retention == [("gate-a1b2", 2)]
    assert endpoint.calls.search == [("gate-a1b2", ("person", "vehicles"), 2)]


@pytest.mark.asyncio
async def test_an_unrelated_change_does_not_push_retention(endpoint):
    """The push is not free — it restamps every row for the camera. A rename
    must not trigger it."""
    body = _Defaulting(name="Gate A")
    await cam_router.update_camera(endpoint.cam.id, body, endpoint.req, endpoint.db)
    assert endpoint.calls.search == []


@pytest.mark.asyncio
async def test_turning_indexing_off_removes_rather_than_restamps(endpoint):
    """Opting a camera out of Smart Search is not a retention change, and must
    not be turned into one by the shared trigger."""
    endpoint.cam.search_indexing = False
    body = _Defaulting(search_indexing=False, retention_days=2)
    await cam_router.update_camera(endpoint.cam.id, body, endpoint.req, endpoint.db)
    assert endpoint.calls.search == []
    assert endpoint.calls.removed == ["gate-a1b2"]
