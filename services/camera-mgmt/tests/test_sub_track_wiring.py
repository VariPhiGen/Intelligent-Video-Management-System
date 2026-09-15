"""Do the ENDPOINTS actually call the fixes?

test_sub_track_integrity.py pins the behaviour of the functions. It cannot tell
you whether anything calls them — and that is not a theoretical worry, it is the
shape of every bug this feature has had. `tracks.py` has been the single
definition of a camera's tracks since the sub shipped, and it warns in its own
docstring about surfaces drifting away from it; the masks endpoint, the delete
path, the purge proxy and the DSR erasure all drifted anyway, each by simply not
calling it.

So these tests are deliberately shallow and deliberately about wiring: they call
the endpoint function directly with the database and serialisation stubbed out,
and assert only on WHICH collaborator it reached and with what. A mutation that
deletes a call from an endpoint body has to fail something here.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1])) 

from backend.config import settings  # noqa: E402
from backend.models import sub_recording_name  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402
from backend.services import substream  # noqa: E402

SUB = {"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264", "width": 704,
       "height": 576, "recording_enabled": True, "retention_days": 14}
SLUG = "gate-a1b2"
SUB_NAME = sub_recording_name(SLUG)


class _DB:
    """Enough AsyncSession for an endpoint body; asserts nothing itself."""
    def __init__(self): self.commits = 0
    async def commit(self): self.commits += 1
    async def refresh(self, obj): return None
    async def delete(self, obj): return None
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


class _Cam(SimpleNamespace):
    """A Camera row for wiring purposes.

    Unset columns read as None instead of raising, so a test states only the
    fields its assertion depends on. Adding a column to the model must not make
    an unrelated wiring test start failing for a reason that has nothing to do
    with what it checks.
    """
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return None


def _camera(**kw):
    base = dict(id=uuid.uuid4(), slug=SLUG, rtsp_url="rtsp://u:pw@10.0.0.5:554/main",
                enabled=True, recording=True, recording_schedule=None,
                privacy_masks=[], retention_days=30, groom_after_days=None,
                sub_track=None)
    base.update(kw)
    return _Cam(**base)


@pytest.fixture
def wired(monkeypatch):
    """Stub everything an endpoint touches EXCEPT the collaborators under test."""
    calls = SimpleNamespace(sync=[], teardown=[], relay=[], codec=[], purge_proxy=[])
    cam = _camera()

    async def get_cam(camera_id, db): return cam
    async def principal(request): return SimpleNamespace(roles=["admin"])
    async def audit(*a, **kw): return None
    async def sync_now(*a, **kw): return None

    monkeypatch.setattr(cam_router, "_get_camera_or_404", get_cam)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    monkeypatch.setattr(cam_router.audit_svc, "record", audit)
    monkeypatch.setattr(cam_router, "sync_now", sync_now)
    monkeypatch.setattr(cam_router, "_build_response", lambda c: c)
    monkeypatch.setattr(cam_router.CameraResponse, "from_orm",
                        staticmethod(lambda c, url: c))
    monkeypatch.setattr(settings, "nvr_sync_enabled", True)
    monkeypatch.setattr(settings, "motion_sync_enabled", False)
    monkeypatch.setattr(settings, "smartsearch_sync_enabled", False)

    async def sync(camera, *, restart=False):
        calls.sync.append({"slug": camera.slug, "restart": restart})
    async def teardown(slug, *, purge): 
        calls.teardown.append({"slug": slug, "purge": purge}); return True
    monkeypatch.setattr(cam_router, "_sync_recording_tracks", sync)
    monkeypatch.setattr(cam_router, "_teardown_recording_tracks", teardown)

    class _Relay:
        async def add_path(self, n, u, on_demand=False): calls.relay.append(("add", n, u))
        async def ensure_path(self, n, u, on_demand=False): calls.relay.append(("ensure", n, u))
        async def patch_path(self, n, u, on_demand=False): calls.relay.append(("patch", n, u))
        async def remove_path(self, n): calls.relay.append(("remove", n, None))
    monkeypatch.setattr(cam_router, "relay", _Relay())

    class _Redis:
        async def delete_stream_keys(self, slug): return None
    monkeypatch.setattr(cam_router, "redis_client", _Redis())

    class _NVR:
        async def invalidate_codec_cache(self, n): calls.codec.append(n); return True
        async def add_camera(self, *a, **kw): return True
        async def remove_camera(self, *a, **kw): return True
        async def set_retention(self, *a, **kw): return True
        async def set_groom(self, *a, **kw): return True
        async def purge_recordings(self, *a, **kw): return True
    monkeypatch.setattr(cam_router, "nvr_client", _NVR())

    return SimpleNamespace(calls=calls, cam=cam, db=_DB(),
                           req=SimpleNamespace(headers={}, query_params={}))


# ── The masks endpoint must restart the tracks ─────────────────────────────

@pytest.mark.asyncio
async def test_saving_masks_restarts_the_recording_tracks(wired):
    """Masks are burned in at record time and reconcile cannot heal them, so if
    this endpoint stops restarting workers the change never reaches disk."""
    body = SimpleNamespace(masks=[[[0.1, 0.1], [0.4, 0.4]]])
    await cam_router.update_privacy_masks(wired.cam.id, body, wired.req, wired.db)
    assert wired.calls.sync == [{"slug": SLUG, "restart": True}]


# ── Delete must tear down and purge every track ────────────────────────────

@pytest.mark.asyncio
async def test_deleting_a_camera_tears_down_every_track(wired):
    await cam_router.delete_camera(wired.cam.id, wired.req, True, wired.db)
    assert wired.calls.teardown == [{"slug": SLUG, "purge": True}]


@pytest.mark.asyncio
async def test_deleting_a_camera_removes_both_relay_paths(wired):
    await cam_router.delete_camera(wired.cam.id, wired.req, False, wired.db)
    removed = [n for verb, n, _u in wired.calls.relay if verb == "remove"]
    assert removed == [SLUG, SUB_NAME]


# ── Re-resolve: the three things it must and must not do ───────────────────

@pytest.mark.asyncio
async def test_an_unreachable_camera_does_not_overwrite_a_stored_sub(wired, monkeypatch):
    from fastapi import HTTPException
    wired.cam.sub_track = dict(SUB)
    async def unreachable(camera): return None, substream.UNREACHABLE
    monkeypatch.setattr(substream, "resolve_detailed", unreachable)
    with pytest.raises(HTTPException) as exc:
        await cam_router.resolve_sub_track(wired.cam.id, wired.req, wired.db)
    assert exc.value.status_code == 503
    assert wired.cam.sub_track == SUB, "the stored configuration must be untouched"
    assert wired.db.commits == 0


@pytest.mark.asyncio
async def test_a_reprobe_keeps_the_operators_retention(wired, monkeypatch):
    wired.cam.sub_track = dict(SUB)
    fresh = {"url_raw": SUB["url_raw"], "codec": "h264", "width": 704,
             "height": 576, "recording_enabled": False, "source": "onvif"}
    async def ok(camera): return dict(fresh), substream.RESOLVED
    monkeypatch.setattr(substream, "resolve_detailed", ok)
    await cam_router.resolve_sub_track(wired.cam.id, wired.req, wired.db)
    assert wired.cam.sub_track["retention_days"] == 14
    assert wired.cam.sub_track["recording_enabled"] is True


@pytest.mark.asyncio
async def test_a_swapped_profile_repoints_the_relay_and_drops_the_codec(wired, monkeypatch):
    """Three things point at the old stream after a swap and none notice alone."""
    wired.cam.sub_track = dict(SUB)
    swapped = {"url_raw": "rtsp://10.0.0.5:554/sub-hevc", "codec": "hevc",
               "width": 704, "height": 576, "recording_enabled": False,
               "source": "onvif"}
    async def ok(camera): return dict(swapped), substream.RESOLVED
    monkeypatch.setattr(substream, "resolve_detailed", ok)
    await cam_router.resolve_sub_track(wired.cam.id, wired.req, wired.db)
    assert ("ensure", SUB_NAME, "rtsp://u:pw@10.0.0.5:554/sub-hevc") in wired.calls.relay
    assert wired.calls.codec == [SUB_NAME]
    assert wired.calls.sync == [{"slug": SLUG, "restart": True}]


@pytest.mark.asyncio
async def test_an_unchanged_profile_leaves_the_recording_alone(wired, monkeypatch):
    """The swap handling must not fire on every re-check — that would restart a
    healthy recording for nothing."""
    wired.cam.sub_track = dict(SUB)
    async def ok(camera):
        return {"url_raw": SUB["url_raw"], "codec": "h264", "width": 704,
                "height": 576, "recording_enabled": False, "source": "onvif"}, substream.RESOLVED
    monkeypatch.setattr(substream, "resolve_detailed", ok)
    await cam_router.resolve_sub_track(wired.cam.id, wired.req, wired.db)
    assert wired.calls.sync == []
    assert wired.calls.codec == []


# ── Enabling a sub, and a credential change ────────────────────────────────

@pytest.mark.asyncio
async def test_enabling_a_sub_repoints_rather_than_adds(wired):
    """add_path reports a stale existing path as success and keeps pulling the
    old profile."""
    wired.cam.sub_track = {**SUB, "recording_enabled": False}
    body = SimpleNamespace(recording_enabled=True, retention_days=None)
    await cam_router.set_sub_track_recording(wired.cam.id, body, wired.req, wired.db)
    verbs = [(v, n) for v, n, _u in wired.calls.relay]
    assert ("ensure", SUB_NAME) in verbs
    assert ("add", SUB_NAME) not in verbs


# ── The NVR proxy must route a whole-camera purge through the fan-out ──────

@pytest.mark.asyncio
async def test_the_proxy_sends_a_camera_purge_through_the_fan_out(monkeypatch):
    """If the proxy stops dispatching, the fan-out is dead code and the sub's
    footage survives a purge that reports it as freed."""
    from backend.routers import nvr as nvr_router
    seen = {}

    async def principal(request): return SimpleNamespace(
        roles=["admin"], has_any=lambda r: True)
    async def fan_out(camera, request, p):
        seen["camera"] = camera
        return "FANNED"
    monkeypatch.setattr(nvr_router, "get_principal", principal)
    monkeypatch.setattr(nvr_router, "_purge_all_tracks", fan_out)

    req = SimpleNamespace(method="DELETE", headers={}, query_params={},
                          body=lambda: None)
    out = await nvr_router.proxy(f"cameras/{SLUG}/recordings", req)
    assert out == "FANNED" and seen["camera"] == SLUG


@pytest.mark.asyncio
async def test_the_proxy_leaves_scoped_erasure_alone(monkeypatch):
    """`.../recordings/range` carries a time window. Routing it through an
    all-tracks purge would erase a camera's whole history for a request scoped
    to ten minutes."""
    from backend.routers import nvr as nvr_router
    called = []

    async def principal(request): return SimpleNamespace(
        roles=["admin"], has_any=lambda r: True)
    async def fan_out(camera, request, p):
        called.append(camera)
        return "FANNED"
    async def audit(*a, **kw): return None
    monkeypatch.setattr(nvr_router, "get_principal", principal)
    monkeypatch.setattr(nvr_router, "_purge_all_tracks", fan_out)
    monkeypatch.setattr(nvr_router.audit_svc, "record", audit)

    class _C:
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False
        async def request(s, *a, **kw):
            return SimpleNamespace(status_code=200, content=b"{}", headers={})
    monkeypatch.setattr(nvr_router.httpx, "AsyncClient", lambda **kw: _C())

    req = SimpleNamespace(method="DELETE", headers={}, query_params={},
                          body=_noop_body)
    await nvr_router.proxy(f"cameras/{SLUG}/recordings/range?from=a&to=b", req)
    assert called == [], "scoped erasure must not be swept into the fan-out"


async def _noop_body():
    return b""


# ── A credential change has to reach the sub's relay path ─────────────────

@pytest.mark.asyncio
async def test_an_rtsp_url_change_repoints_both_relay_paths(wired, monkeypatch):
    """The sub's URL is composed from the main's credentials, so a password
    rotation invalidates it too. Repointing only the main leaves the sub
    authenticating with the old password — it stops pulling, silently, and the
    relay path still exists so nothing reports a problem.

    Drives the real endpoint: replaying its branch here would pass with the
    endpoint gutted, which is the failure mode these tests exist to rule out."""
    from backend.models import CameraUpdate
    wired.cam.sub_track = dict(SUB)
    async def principal(request): return SimpleNamespace(
        roles=["admin"], sub="t", username="t", has_any=lambda r: True)
    monkeypatch.setattr(cam_router, "get_principal", principal)
    for name in ("motion_client", "smartsearch_sync", "deepstream_config"):
        if hasattr(cam_router, name):
            monkeypatch.setattr(cam_router, name, _Swallow())
    monkeypatch.setattr(cam_router, "health_svc", _Swallow())

    body = CameraUpdate(rtsp_url="rtsp://u:NEWPASS@10.0.0.5:554/main")
    await cam_router.update_camera(wired.cam.id, body, wired.req, wired.db)

    patched = [n for verb, n, _u in wired.calls.relay if verb == "patch"]
    assert SUB_NAME in patched, "the sub's path must follow the new credentials"


class _Swallow:
    """Accepts any attribute and any await — for collaborators that are not
    what a given test is about."""
    def __getattr__(self, _name):
        async def _any(*a, **kw): return None
        return _any


# ── The DSR fulfilment must use the all-tracks erasure ────────────────────

@pytest.mark.asyncio
async def test_fulfilling_an_erasure_erases_every_track(monkeypatch):
    """The statutory path. `erase_range` deletes one recording name; a camera
    with a sub holds two copies of the erased window. Calling the single-track
    version here closed the request as FULFILLED with the low-res copy still on
    disk — not a partial erasure, a false claim of one."""
    # importorskip, not a plain import: `dsr` lives in the tier-3 compliance
    # extension, which .publicignore strips from the open-core tree. A hard
    # import here made the PUBLIC suite fail on a module that is absent by
    # design — red on a contributor's first `pytest` run, for no defect.
    dsr_mod = pytest.importorskip(
        "backend.extensions.compliance.dsr",
        reason="tier-3 compliance extension is not present in the open-core tree",
    )
    used = []

    async def all_tracks(slug, frm, to):
        used.append(("all_tracks", slug))
        return {"segments_deleted": 2, "bytes_freed": 10, "deleted": [],
                "skipped_partial_overlap": [], "tracks_erased": {slug: 2}}

    async def single(slug, frm, to, missing_ok=False):
        used.append(("single", slug))
        return {"segments_deleted": 1, "bytes_freed": 5}

    monkeypatch.setattr(dsr_mod.nvr_client, "erase_range_all_tracks", all_tracks)
    monkeypatch.setattr(dsr_mod.nvr_client, "erase_range", single)

    async def no_search(slug, frm, to): return []
    monkeypatch.setattr(dsr_mod.smartsearch_client, "erase_range", no_search)

    ref = SimpleNamespace(camera_id=SLUG,
                          from_=_dt(2026, 9, 7, 10), to=_dt(2026, 9, 7, 11))
    results, all_ok = await _run_erasure(dsr_mod, [ref])

    assert used == [("all_tracks", SLUG)], \
        "erasure must go through the all-tracks path, not the single-track one"
    assert all_ok is True
    assert results[0]["tracks_erased"] == {SLUG: 2}


def _dt(y, m, d, h):
    from datetime import datetime, timezone
    return datetime(y, m, d, h, tzinfo=timezone.utc)


async def _run_erasure(dsr_mod, refs):
    """Drive fulfil_dsr's erasure loop against stubbed collaborators.

    Extracted so the assertion above is about which client call the module
    makes, not about DSR row bookkeeping — but it calls the module's own
    function rather than restating the loop, which is the whole point.
    """
    return await dsr_mod.erase_refs(refs)


# ── The health monitor's orphan sweep must not eat a relay-owned track ─────
#
# THE THIRTEENTH INSTANCE OF THIS BUG CLASS, and the one that shows why the
# question has to be asked of the RELAY rather than the recorder.
#
# The sweep deletes every MediaMTX path it does not recognise, which is right:
# a deleted camera's path can survive a failed removal and keep an RTSP session
# open against hardware that only allows one or two. But "recognise" was being
# answered with the recorder's track list, and the relay legitimately holds one
# more — a sub that is RESOLVED but not recorded, carried on demand so the live
# view can fall back to it on a camera the browser cannot decode.
#
# `substream.resolve()` always stores `recording_enabled: False`, so that is the
# DEFAULT state of every camera with a discovered sub. Reconcile added the path;
# the sweep deleted it ~17 ms later; repeat every poll, forever. Nothing raised,
# nothing logged as an error, and the only symptom was a live view that fell
# back to nothing on exactly the cameras the fallback was built for.
#
# These tests drive the real `_poll_all()` with the database and MediaMTX
# stubbed, and assert only on WHICH paths it decided to delete — so they state
# the invariant (a relay-owned name is never an orphan) rather than the
# implementation (that health.py calls some particular helper).

from backend.services import health as health_svc  # noqa: E402
from backend.services import tracks  # noqa: E402

RESOLVED_SUB = {"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
                "width": 704, "height": 576, "verified": True,
                # The whole point: resolution records what the camera offers and
                # never switches recording on. A test using True here would pass
                # against the broken sweep.
                "recording_enabled": False}


class _SweepDB:
    """The two queries `_poll_all` makes, answered in order.

    First `select(Camera)` for the cameras to health-check, then
    `select(Camera.slug, Camera.rtsp_url, Camera.sub_track)` for the ownership
    set. Returning the same rows to both would hide a sweep that consulted the
    wrong one.
    """
    def __init__(self, cameras, rows):
        self._cameras, self._rows, self.n = cameras, rows, 0

    async def execute(self, *a, **kw):
        self.n += 1
        if self.n == 1:
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: self._cameras))
        return SimpleNamespace(all=lambda: self._rows)


def _sweep(monkeypatch, *, camera, paths):
    """Run one orphan sweep over `paths` with `camera` the only registry row.

    Returns the path names the sweep removed.
    """
    removed: list[str] = []
    row = SimpleNamespace(slug=camera.slug, rtsp_url=camera.rtsp_url,
                          sub_track=camera.sub_track)
    db = _SweepDB([camera], [row])

    class _Session:
        async def __aenter__(self): return db
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(health_svc, "AsyncSessionLocal", lambda: _Session())

    class _Relay:
        async def list_active_paths(self):
            return [{"name": n} for n in paths]
        async def remove_path(self, name):
            removed.append(name); return True
    monkeypatch.setattr(health_svc, "relay", _Relay())

    # The per-camera health check is a different subject; neutralise it so a
    # failure here can only be about ownership.
    async def _no_check(cam): return None
    monkeypatch.setattr(health_svc, "check_camera", _no_check)

    return removed


@pytest.mark.asyncio
async def test_a_resolved_sub_is_not_an_orphan(monkeypatch):
    """THE REGRESSION. A sub the relay carries on demand is owned, not stray.

    Fails against the pre-fix sweep, which asked the recorder whether
    `<slug>_sub` was known and got "no" for every unrecorded sub.
    """
    cam = _camera(sub_track=RESOLVED_SUB)
    removed = _sweep(monkeypatch, camera=cam, paths=[SLUG, SUB_NAME])
    await health_svc._poll_all()

    assert SUB_NAME not in removed, (
        f"the orphan sweep deleted {SUB_NAME}, a relay path this camera owns — "
        "the live view's H.265 fallback has nothing left to attach to"
    )
    assert removed == [], f"nothing here is an orphan, but {removed} was removed"


@pytest.mark.asyncio
async def test_every_relay_owned_track_survives_the_sweep(monkeypatch):
    """The invariant itself: relay_tracks_for(camera) ⊆ known_names.

    Stated over the enumeration rather than over one hard-coded name, so a
    future third track is covered the day it is added instead of the day
    somebody remembers to extend this file.
    """
    cam = _camera(sub_track=RESOLVED_SUB)
    owned = [t.recording_name for t in tracks.relay_tracks_for(cam)]
    assert SUB_NAME in owned, "fixture no longer produces a relay-owned sub"

    removed = _sweep(monkeypatch, camera=cam, paths=owned)
    await health_svc._poll_all()

    assert removed == [], (
        f"the sweep removed {removed}, which the relay is told to carry"
    )


@pytest.mark.asyncio
async def test_a_recording_sub_is_still_not_an_orphan(monkeypatch):
    """The case the PREVIOUS fix bought. Keeping it so the next change to this
    set cannot trade one half of the problem for the other."""
    cam = _camera(sub_track={**RESOLVED_SUB, "recording_enabled": True})
    removed = _sweep(monkeypatch, camera=cam, paths=[SLUG, SUB_NAME])
    await health_svc._poll_all()
    assert removed == []


@pytest.mark.asyncio
async def test_a_genuinely_unknown_path_is_still_swept(monkeypatch):
    """The sweep must keep doing its job. Widening ownership to fix the above
    is only correct if a stray path is still collected — a deleted camera's
    path left pulling can lock out its re-added successor on hardware that
    allows one or two sessions."""
    cam = _camera(sub_track=RESOLVED_SUB)
    removed = _sweep(monkeypatch, camera=cam,
                     paths=[SLUG, SUB_NAME, "ghost-z9y8", "ghost-z9y8_sub"])
    await health_svc._poll_all()

    assert removed == ["ghost-z9y8", "ghost-z9y8_sub"], (
        "a path belonging to no registry camera must still be removed"
    )


@pytest.mark.asyncio
async def test_a_camera_with_no_sub_owns_only_its_main(monkeypatch):
    """Ownership widened to the relay's view must not become 'anything ending
    in _sub is fine' — a camera without a sub track owns no `_sub` path, and a
    stray one is still an orphan."""
    cam = _camera(sub_track=None)
    removed = _sweep(monkeypatch, camera=cam, paths=[SLUG, SUB_NAME])
    await health_svc._poll_all()

    assert removed == [SUB_NAME], (
        "this camera has no sub track, so its `_sub` path belongs to nobody"
    )
