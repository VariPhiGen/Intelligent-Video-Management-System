"""Sub-track integrity: the ways a second recording track goes silently wrong.

Every case here was reachable in the shipped substream feature, and what unites
them is that none of them produce an error. A sub records unmasked, or gets
groomed into a slideshow, or has its configuration erased, or outlives the
camera it belonged to — and every screen in the product goes on saying the
right thing. So these tests pin the calls that go OUT to the NVR rather than
any status the UI could show, because the outgoing call is the only place the
difference is observable.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings  # noqa: E402
from backend.models import sub_recording_name  # noqa: E402
from backend.routers import cameras as cam_router  # noqa: E402
from backend.services import nvr_client, substream, tracks  # noqa: E402


SUB = {"url_raw": "rtsp://10.0.0.5:554/sub", "codec": "h264",
       "width": 704, "height": 576, "recording_enabled": True}


def camera(**kw):
    base = dict(slug="gate-a1b2", rtsp_url="rtsp://user:pw@10.0.0.5:554/main",
                enabled=True, recording=True, recording_schedule=None,
                privacy_masks=[], retention_days=30, groom_after_days=None,
                sub_track=None)
    base.update(kw)
    return SimpleNamespace(**base)


SUB_NAME = sub_recording_name("gate-a1b2")


# ── The NVR client's groom encoding ──────────────────────────────────────────
#
# Three states have to survive the wire: no override, never groom, N days. Two
# of them used to collapse onto each other, which is what made "never" unsayable.

class _Resp:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = ""


class _FakeHTTP:
    def __init__(self):
        self.calls = []

    async def post(self, url, params=None):
        self.calls.append(("POST", url, dict(params or {})))
        return _Resp()

    async def put(self, url, params=None):
        self.calls.append(("PUT", url, dict(params or {})))
        return _Resp()

    async def delete(self, url, params=None):
        self.calls.append(("DELETE", url, dict(params or {})))
        return _Resp()


@pytest.fixture
def http(monkeypatch):
    fake = _FakeHTTP()
    monkeypatch.setattr(nvr_client, "_get_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_never_groom_survives_add_camera(http):
    """0 is a setting, not an absence. A truthy test dropped it and the sub
    inherited the appliance groom default — the rewrite it was opting out of."""
    await nvr_client.add_camera("gate-a1b2_sub", retention_days=3, groom_after_days=0)
    _m, _u, params = http.calls[0]
    assert params["groom_after_days"] == 0


@pytest.mark.asyncio
async def test_no_override_is_omitted_from_add_camera(http):
    await nvr_client.add_camera("gate-a1b2", retention_days=30, groom_after_days=None)
    _m, _u, params = http.calls[0]
    assert "groom_after_days" not in params


@pytest.mark.asyncio
async def test_set_groom_distinguishes_never_from_default(http):
    await nvr_client.set_groom("a_sub", 0)      # never
    await nvr_client.set_groom("a", None)       # clear back to the default
    await nvr_client.set_groom("b", 7)          # threshold
    sent = [params for _m, _u, params in http.calls]
    assert sent[0] == {"groom_after_days": 0}
    assert sent[1] == {}, "clearing must OMIT the parameter, not send 0"
    assert sent[2] == {"groom_after_days": 7}


# ── Track fan-out: masks, retention and grooming reach the sub ───────────────

class _FakeNVR:
    """Records the NVR calls a router action makes, in order."""

    def __init__(self):
        self.added, self.removed, self.purged = [], [], []
        self.purge_result = True

    async def add_camera(self, name, retention_days=None, masks=None,
                         groom_after_days=None):
        self.added.append({"name": name, "retention_days": retention_days,
                           "masks": masks, "groom_after_days": groom_after_days})
        return True

    async def remove_camera(self, name):
        self.removed.append(name)
        return True

    async def purge_recordings(self, name):
        self.purged.append(name)
        return self.purge_result


@pytest.fixture
def nvr(monkeypatch):
    fake = _FakeNVR()
    monkeypatch.setattr(cam_router, "nvr_client", fake)
    monkeypatch.setattr(settings, "nvr_sync_enabled", True)
    return fake


@pytest.mark.asyncio
async def test_mask_change_restarts_the_sub_too(nvr):
    """The bug: masks are burned in at record time and reconcile only re-adds
    tracks it finds MISSING, so an already-recording sub was never revisited and
    kept recording unmasked forever."""
    masks = [[[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]]]
    await cam_router._sync_recording_tracks(
        camera(sub_track=SUB, privacy_masks=masks), restart=True)
    by_name = {a["name"]: a for a in nvr.added}
    assert set(by_name) == {"gate-a1b2", SUB_NAME}
    assert by_name[SUB_NAME]["masks"] == masks
    assert nvr.removed.count(SUB_NAME) == 1, "restart must stop the old sub worker"


@pytest.mark.asyncio
async def test_the_sub_is_added_with_never_groom(nvr):
    await cam_router._sync_recording_tracks(camera(sub_track=SUB))
    sub = next(a for a in nvr.added if a["name"] == SUB_NAME)
    assert sub["groom_after_days"] == 0


@pytest.mark.asyncio
async def test_the_sub_keeps_its_own_shorter_retention(nvr):
    await cam_router._sync_recording_tracks(camera(sub_track=SUB, retention_days=30))
    sub = next(a for a in nvr.added if a["name"] == SUB_NAME)
    main = next(a for a in nvr.added if a["name"] == "gate-a1b2")
    assert sub["retention_days"] < main["retention_days"]


@pytest.mark.asyncio
async def test_a_disabled_camera_stops_both_tracks(nvr):
    await cam_router._sync_recording_tracks(camera(sub_track=SUB, enabled=False))
    assert nvr.added == []
    assert set(nvr.removed) == {"gate-a1b2", SUB_NAME}


@pytest.mark.asyncio
async def test_switching_the_sub_off_stops_only_the_sub(nvr):
    off = {**SUB, "recording_enabled": False}
    await cam_router._sync_recording_tracks(camera(sub_track=off))
    assert [a["name"] for a in nvr.added] == ["gate-a1b2"]
    assert nvr.removed == [SUB_NAME]


@pytest.mark.asyncio
async def test_a_camera_with_no_sub_still_records_its_main(nvr):
    await cam_router._sync_recording_tracks(camera())
    assert [a["name"] for a in nvr.added] == ["gate-a1b2"]


# ── Deletion: erasure has to cover footage the flag no longer describes ──────

@pytest.mark.asyncio
async def test_purge_covers_the_sub_even_when_it_is_switched_off(nvr):
    """The DPDP case. Gating the sub purge on "is the sub enabled right now"
    left a low-res copy of the purged footage on disk, with no camera row left
    to reach it through, while the purge reported success."""
    ok = await cam_router._teardown_recording_tracks("gate-a1b2", purge=True)
    assert nvr.purged == ["gate-a1b2", SUB_NAME]
    assert ok is True


@pytest.mark.asyncio
async def test_teardown_stops_both_workers(nvr):
    await cam_router._teardown_recording_tracks("gate-a1b2", purge=False)
    assert nvr.removed == ["gate-a1b2", SUB_NAME]
    assert nvr.purged == []


@pytest.mark.asyncio
async def test_a_failed_purge_is_not_reported_as_a_purge(nvr):
    nvr.purge_result = False
    ok = await cam_router._teardown_recording_tracks("gate-a1b2", purge=True)
    assert ok is False
    assert nvr.purged == ["gate-a1b2", SUB_NAME], "both are attempted regardless"


# ── Re-probing must not destroy what it failed to measure ───────────────────

@pytest.mark.asyncio
async def test_an_unreachable_camera_reports_unreachable_not_no_sub(monkeypatch):
    """The distinction the resolve endpoint refuses to overwrite a stored
    sub_track on. A camera mid-reboot probes exactly like a camera with no
    substream, and storing that erased the sub's URL and recording flag."""
    async def dead(url):
        return None
    monkeypatch.setattr(substream, "probe_stream", dead)
    monkeypatch.setattr(substream, "measure_bitrate", dead)
    resolved, reason = await substream.resolve_detailed(
        SimpleNamespace(slug="gate-a1b2", rtsp_url="rtsp://u:p@10.0.0.5:554/main",
                        rtsp_candidates=[], sub_track=SUB))
    assert resolved is None
    assert reason == substream.UNREACHABLE


@pytest.mark.asyncio
async def test_a_reachable_camera_with_nothing_useful_says_so(monkeypatch):
    """The other side of it: this one IS a fact about the camera, and may be
    stored."""
    async def only_the_main(url):
        return {"codec": "h264", "width": 1920, "height": 1080, "fps": 25}
    async def no_measurement(url):
        return None
    monkeypatch.setattr(substream, "probe_stream", only_the_main)
    monkeypatch.setattr(substream, "measure_bitrate", no_measurement)
    monkeypatch.setattr(substream, "candidate_urls", lambda cam: [])
    resolved, reason = await substream.resolve_detailed(
        SimpleNamespace(slug="gate-a1b2", rtsp_url="rtsp://u:p@10.0.0.5:554/main",
                        rtsp_candidates=[], sub_track=None))
    assert resolved is None
    assert reason == substream.NO_USABLE_SUB


@pytest.mark.asyncio
async def test_a_stray_zero_on_a_main_still_means_inherit(nvr):
    """0 means "never groom" on the wire now, but on a MAIN camera it has always
    meant "no override". A row holding 0 (a hand-written UPDATE, a future create
    path) must not silently exempt the camera from grooming."""
    await cam_router._sync_recording_tracks(camera(groom_after_days=0))
    main = next(a for a in nvr.added if a["name"] == "gate-a1b2")
    assert main["groom_after_days"] is None


@pytest.mark.asyncio
async def test_a_real_main_override_is_passed_through(nvr):
    await cam_router._sync_recording_tracks(camera(groom_after_days=7))
    main = next(a for a in nvr.added if a["name"] == "gate-a1b2")
    assert main["groom_after_days"] == 7


# ── DSR scoped erasure has to reach the sub too ─────────────────────────────
#
# The statutory path, and the same defect as the delete purge: a camera with a
# sub holds two copies of the erased window, and only one was being deleted.

class _EraseHTTP:
    """Fake NVR for erase_range: per-recording-name canned responses."""

    def __init__(self, table):
        self.table = table          # name -> status code
        self.asked = []

    async def delete(self, url, params=None):
        name = url.rstrip("/").split("/cameras/")[1].split("/")[0]
        self.asked.append(name)
        status = self.table.get(name, 404)
        resp = _Resp(status)
        resp.json = lambda: ({"status": "ok", "camera": name,
                              "segments_deleted": 2, "bytes_freed": 1024,
                              "deleted": [{"filepath": f"/d/{name}.ts"}],
                              "skipped_partial_overlap": []}
                             if status == 200 else {})
        return resp


@pytest.mark.asyncio
async def test_scoped_erasure_deletes_the_sub_copy_too(monkeypatch):
    fake = _EraseHTTP({"gate-a1b2": 200, SUB_NAME: 200})
    monkeypatch.setattr(nvr_client, "_get_client", lambda: fake)
    out = await nvr_client.erase_range_all_tracks("gate-a1b2", "T0", "T1")
    assert fake.asked == ["gate-a1b2", SUB_NAME]
    assert out["segments_deleted"] == 4, "both tracks' deletions are reported"
    assert out["tracks_erased"] == {"gate-a1b2": 2, SUB_NAME: 2}


@pytest.mark.asyncio
async def test_a_camera_with_no_sub_erases_cleanly(monkeypatch):
    """Most cameras never recorded a sub. "Nothing here to erase" is a complete
    answer, not a failed one — otherwise every erasure would report failure."""
    fake = _EraseHTTP({"gate-a1b2": 200})          # sub 404s
    monkeypatch.setattr(nvr_client, "_get_client", lambda: fake)
    out = await nvr_client.erase_range_all_tracks("gate-a1b2", "T0", "T1")
    assert out is not None
    assert out["segments_deleted"] == 2
    assert out["tracks_erased"][SUB_NAME] == 0


@pytest.mark.asyncio
async def test_a_failed_sub_erase_fails_the_whole_erasure(monkeypatch):
    """The main is gone and the low-res copy may not be. That must not read as
    fulfilled — a half-done erasure reported as done is the false claim."""
    fake = _EraseHTTP({"gate-a1b2": 200, SUB_NAME: 502})
    monkeypatch.setattr(nvr_client, "_get_client", lambda: fake)
    assert await nvr_client.erase_range_all_tracks("gate-a1b2", "T0", "T1") is None


@pytest.mark.asyncio
async def test_a_404_is_only_softened_when_asked_for(monkeypatch):
    """missing_ok is opt-in: the MAIN track 404ing is still a hard failure."""
    fake = _EraseHTTP({})
    monkeypatch.setattr(nvr_client, "_get_client", lambda: fake)
    assert await nvr_client.erase_range("gate-a1b2", "T0", "T1") is None
    assert await nvr_client.erase_range("gate-a1b2", "T0", "T1", missing_ok=True) is not None


# ── The sub's relay path has to follow its URL ──────────────────────────────

class _FakeRelayHTTP:
    def __init__(self, exists=False):
        self.exists = exists
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append(("POST", url, (json or {}).get("source")))
        r = _Resp(400 if self.exists else 200)
        r.text = '{"error":"path already exists"}' if self.exists else "{}"
        r.raise_for_status = lambda: None
        return r

    async def patch(self, url, json=None):
        self.calls.append(("PATCH", url, (json or {}).get("source")))
        r = _Resp(200)
        r.raise_for_status = lambda: None
        return r


@pytest.mark.asyncio
async def test_ensure_path_repoints_an_existing_path(monkeypatch):
    """add_path collapses "created" and "already there" onto True without
    touching the config, so a sub whose URL changed kept pulling the old
    profile — or nothing, after a password rotation."""
    from backend.services import relay
    fake = _FakeRelayHTTP(exists=True)
    monkeypatch.setattr(relay, "_get_client", lambda: fake)
    async def no_fp(url):
        return ""
    monkeypatch.setattr(relay.tlsutil, "source_fingerprint", no_fp)
    # _path_config reads the restamp flag from Valkey, so stubbing the HTTP
    # client is not enough to make this test hermetic — without this it opens a
    # socket to 127.0.0.1:6380 and fails wherever the broker is not up, which
    # is every CI runner. Same stub as test_restamp_tracks.py.
    async def not_flagged(slug):
        return False
    monkeypatch.setattr(relay.redis_client, "get_restamp", not_flagged)
    await relay.ensure_path("gate-a1b2_sub", "rtsp://u:new@10.0.0.5:554/sub")
    methods = [c[0] for c in fake.calls]
    assert "PATCH" in methods, "an existing path must be repointed, not skipped"
    assert fake.calls[-1][2] == "rtsp://u:new@10.0.0.5:554/sub"


# ── A re-probe must not discard what an operator chose ──────────────────────

PRIOR = {"url_raw": "rtsp://10.0.0.5:554/sub", "recording_enabled": True,
         "retention_days": 14, "codec": "h264"}


def _fresh():
    """What resolve() hands back: the camera's answer, no operator opinion."""
    return {"url_raw": "rtsp://10.0.0.5:554/sub2", "recording_enabled": False,
            "codec": "hevc"}


def test_a_reprobe_keeps_the_retention_an_operator_chose():
    """recording_enabled was carried across a re-probe and retention_days was
    not, so a deliberate 14-day sub retention silently reverted to the 3-day
    default and footage someone chose to keep started ageing out."""
    out = tracks.carry_operator_settings(PRIOR, _fresh())
    assert out["retention_days"] == 14


def test_a_reprobe_keeps_the_recording_switched_on():
    out = tracks.carry_operator_settings(PRIOR, _fresh())
    assert out["recording_enabled"] is True


def test_a_reprobe_still_adopts_what_the_camera_actually_offers():
    """The carry-over must not become a freeze: the probe's own findings — the
    URL and codec it just measured — are the point of re-checking."""
    out = tracks.carry_operator_settings(PRIOR, _fresh())
    assert out["url_raw"] == "rtsp://10.0.0.5:554/sub2"
    assert out["codec"] == "hevc"


def test_a_first_resolve_has_nothing_to_carry():
    out = tracks.carry_operator_settings(None, _fresh())
    assert out["recording_enabled"] is False
    assert "retention_days" not in out


def test_carrying_onto_nothing_stays_nothing():
    """A camera that no longer offers a usable sub is cleared, not resurrected
    with the old settings attached."""
    assert tracks.carry_operator_settings(PRIOR, None) is None


def test_every_operator_field_is_covered_by_this_file():
    """Guards the list itself. A field added to OPERATOR_SET_FIELDS without a
    test above is a field that can silently start being discarded again."""
    assert set(tracks.OPERATOR_SET_FIELDS) == {"recording_enabled", "retention_days"}


# ── Whole-camera purge covers every track ──────────────────────────────────

def test_purge_fan_out_matches_only_a_whole_camera_purge():
    """Scoped erasure carries a time window and is audited on its own terms —
    sweeping it into the all-tracks fan-out would erase a camera's whole
    history for a request scoped to ten minutes."""
    from backend.routers.nvr import _purge_camera
    assert _purge_camera("cameras/gate-a1b2/recordings") == "gate-a1b2"
    assert _purge_camera("cameras/gate-a1b2/recordings?x=1") == "gate-a1b2"
    assert _purge_camera("cameras/gate-a1b2/recordings/range?from=a&to=b") is None
    assert _purge_camera("cameras/gate-a1b2/retention") is None
    assert _purge_camera("clip?camera=gate-a1b2") is None


# ── Probing a TLS camera ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_skips_certificate_verification(monkeypatch):
    """Camera certificates are self-signed. Without -tls_verify 0 every probe of
    an rtsps:// camera returned None, which reads as "no substream" — the
    feature was absent on those cameras, not degraded, and silently."""
    seen = {}

    async def fake_exec(*args, **kw):
        seen["argv"] = args
        raise OSError("stop here")   # we only care about the argv

    monkeypatch.setattr(substream.asyncio, "create_subprocess_exec", fake_exec)
    await substream.probe_stream("rtsps://u:p@10.0.0.5:322/sub")
    argv = seen["argv"]
    assert "-tls_verify" in argv
    assert argv[argv.index("-tls_verify") + 1] == "0"


@pytest.mark.asyncio
async def test_a_missing_ffprobe_is_not_a_500(monkeypatch):
    """FileNotFoundError used to escape the endpoint. A probe that cannot run
    has learned nothing, which is what None means everywhere else here."""
    async def no_binary(*args, **kw):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(substream.asyncio, "create_subprocess_exec", no_binary)
    assert await substream.probe_stream("rtsp://10.0.0.5:554/sub") is None


class _PurgeHTTP:
    """Fake NVR for the proxy's purge fan-out; per-name canned responses."""

    def __init__(self, table):
        self.table = table
        self.asked = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def delete(self, url):
        import httpx
        name = url.rstrip("/").split("/cameras/")[1].split("/")[0]
        self.asked.append(name)
        status = self.table.get(name, 404)
        if status == "unreachable":
            raise httpx.ConnectError("connection refused")
        r = _Resp(status)
        r.content = b'{"detail":"No recordings for camera"}'
        r.json = lambda: {"status": "ok", "camera": name,
                          "segments_deleted": 5, "bytes_freed": 1000}
        return r


@pytest.fixture
def purge_nvr(monkeypatch):
    from backend.routers import nvr as nvr_router

    async def no_audit(*a, **kw):
        return None
    monkeypatch.setattr(nvr_router.audit_svc, "record", no_audit)

    def install(table):
        fake = _PurgeHTTP(table)
        monkeypatch.setattr(nvr_router.httpx, "AsyncClient", lambda **kw: fake)
        return nvr_router, fake
    return install


async def _purge(nvr_router, camera="gate-a1b2"):
    resp = await nvr_router._purge_all_tracks(
        camera, SimpleNamespace(headers={}, query_params={}), object())
    import json as _json
    return resp.status_code, _json.loads(resp.body)


@pytest.mark.asyncio
async def test_purging_a_camera_purges_its_sub_track(purge_nvr):
    """The Storage tab folds the sub's bytes into the camera's row and claims
    them as freed. Before the fan-out, only the main was actually deleted."""
    router, fake = purge_nvr({"gate-a1b2": 200, SUB_NAME: 200})
    status, body = await _purge(router)
    assert status == 200
    assert fake.asked == ["gate-a1b2", SUB_NAME]
    assert body["bytes_freed"] == 2000, "the sub's bytes belong in the freed total"
    assert body["segments_deleted"] == 10
    assert body["tracks_purged"] == {"gate-a1b2": 5, SUB_NAME: 5}


@pytest.mark.asyncio
async def test_a_camera_with_no_sub_purges_normally(purge_nvr):
    router, _fake = purge_nvr({"gate-a1b2": 200})       # sub 404s
    status, body = await _purge(router)
    assert status == 200
    assert body["bytes_freed"] == 1000
    assert body["tracks_purged"][SUB_NAME] == 0


@pytest.mark.asyncio
async def test_the_mains_refusal_is_passed_through_verbatim(purge_nvr):
    """"No recordings for this camera" is an answer the SPA already reads; the
    fan-out must not turn it into a fabricated success."""
    router, _fake = purge_nvr({})                       # main 404s too
    resp = await router._purge_all_tracks(
        "gate-a1b2", SimpleNamespace(headers={}, query_params={}), object())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_failed_sub_purge_is_an_error_not_a_footnote(purge_nvr):
    """The purge response is the basis of a deletion claim. A 200 with the
    sub's failure buried in a field reads to the SPA as fully freed — while a
    low-res copy of exactly that footage remains on disk."""
    router, fake = purge_nvr({"gate-a1b2": 200, SUB_NAME: 500})
    status, body = await _purge(router)
    assert status == 502
    assert body["status"] == "partial"
    assert "sub track failed" in body["detail"].lower() or "sub" in body["detail"]
    # What DID happen is still reported truthfully.
    assert body["bytes_freed"] == 1000
    assert body["tracks_purged"] == {"gate-a1b2": 5, SUB_NAME: 0}
    assert fake.asked == ["gate-a1b2", SUB_NAME]


@pytest.mark.asyncio
async def test_an_unreachable_sub_purge_is_an_error_too(purge_nvr):
    """Mid-fan-out unreachability: the main was purged, the sub was never
    answered. Same rule — the operator retries rather than trusts."""
    router, _fake = purge_nvr({"gate-a1b2": 200, SUB_NAME: "unreachable"})
    status, body = await _purge(router)
    assert status == 502
    assert body["status"] == "partial"
    assert body["bytes_freed"] == 1000


@pytest.mark.asyncio
async def test_a_failed_sub_after_an_empty_main_does_not_claim_a_purge(purge_nvr):
    """{main: 404, sub: 500} — the detail must not assert 'Purged the main
    track' when zero segments were deleted; a deletion claim has to be true."""
    router, _fake = purge_nvr({SUB_NAME: 500})          # main 404s, sub errors
    status, body = await _purge(router)
    assert status == 502
    assert body["status"] == "partial"
    assert "no recordings" in body["detail"].lower()
    assert "purged the main" not in body["detail"].lower()
    assert body["segments_deleted"] == 0


@pytest.mark.asyncio
async def test_a_purge_of_an_empty_camera_is_not_audited(purge_nvr, monkeypatch):
    """Both tracks 404 → the NVR's own 404 comes back BEFORE any audit write.
    An 'evidence.purged' row implies a purge actually executed; a double-click
    on an empty camera must not mint records an auditor reads as 'evidence
    existed and was destroyed'."""
    from backend.routers import nvr as nvr_router
    audited = []

    async def record(*a, **kw):
        audited.append(kw.get("detail"))
    monkeypatch.setattr(nvr_router.audit_svc, "record", record)

    router, _fake = purge_nvr({})                       # both tracks 404
    resp = await router._purge_all_tracks(
        "gate-a1b2", SimpleNamespace(headers={}, query_params={}), object())
    assert resp.status_code == 404
    assert audited == [], "nothing was deleted, nothing failed — no audit row"


@pytest.mark.asyncio
async def test_erase_all_tracks_survives_an_empty_main(monkeypatch):
    """Scoped erasure's mirror of the purge fan-out bug: a main whose segments
    all aged out (404) must not abort before the sub is erased — the low-res
    copy is exactly what the data principal asked to have deleted."""
    calls = []

    async def fake_erase(slug, frm, to, missing_ok=False):
        calls.append((slug, missing_ok))
        if slug == "gate-a1b2":
            assert missing_ok, "the main erase must tolerate 'no footage there'"
            return {"status": "ok", "camera": slug, "segments_deleted": 0,
                    "bytes_freed": 0, "deleted": [], "skipped_partial_overlap": []}
        return {"status": "ok", "camera": slug, "segments_deleted": 3,
                "bytes_freed": 300, "deleted": ["s1", "s2", "s3"],
                "skipped_partial_overlap": []}

    monkeypatch.setattr(nvr_client, "erase_range", fake_erase)
    out = await nvr_client.erase_range_all_tracks("gate-a1b2", "a", "b")
    assert out is not None
    assert [c[0] for c in calls] == ["gate-a1b2", SUB_NAME]
    assert out["segments_deleted"] == 3
    assert out["tracks_erased"] == {"gate-a1b2": 0, SUB_NAME: 3}


@pytest.mark.asyncio
async def test_a_main_with_no_footage_still_purges_the_sub(purge_nvr):
    """A camera whose main already aged out (or was range-erased) can still
    hold sub segments — the row counted them, so the purge must reach them.
    Returning the main's 404 early left those bytes undeletable forever."""
    router, fake = purge_nvr({SUB_NAME: 200})           # main 404s, sub has footage
    status, body = await _purge(router)
    assert status == 200
    assert fake.asked == ["gate-a1b2", SUB_NAME]
    assert body["bytes_freed"] == 1000
    assert body["tracks_purged"] == {"gate-a1b2": 0, SUB_NAME: 5}
