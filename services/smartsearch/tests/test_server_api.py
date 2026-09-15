"""server.py — the contract the VMS speaks, and the erasure it relies on.

513 lines, 17 routes, no test until now. Two things in here justify going first.

THE ERASURE. `/erase` is the search index's half of a data-subject erasure. The
VMS destroys the NVR footage for a camera and window; this destroys the crops
cut from it. If the window is misread, the caller is still told the erasure
succeeded — and a caller reading that response decides whether to tell a data
subject their footage is gone. `_parse_window` exists precisely because
`queries._parse_when` returns None on unparseable input, which is right for an
optional search filter and exactly wrong here: a dropped bound silently widens
the erasure to all of time or voids it entirely.

THE 503. When the encoder cannot be loaded the search routes must answer 503,
never an empty result set. "Nothing matched" is the one answer a search must
never give wrongly, and it is indistinguishable from "this person was never
here" to the operator reading it.

Everything is driven through `create_app`, which takes its collaborators as
arguments — so the whole API runs in-process against stubs with no database, no
model and no network.

Run (from services/smartsearch): python -m pytest tests -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.server import _parse_window, create_app  # noqa: E402
from index.queries import EncoderUnavailable  # noqa: E402

UTC = timezone.utc


# ── Stubs ──────────────────────────────────────────────────────────────────

class StubQueries:
    """A QueryService that records calls and returns whatever it is told to."""

    def __init__(self, **returns):
        self.calls: list[tuple[str, dict]] = []
        self._returns = returns
        self.raises: dict[str, Exception] = {}
        self.available = True
        self.plates_active = True

    def _answer(self, name, kwargs, default):
        self.calls.append((name, kwargs))
        if name in self.raises:
            raise self.raises[name]
        return self._returns.get(name, default)

    def search_people(self, **kw):
        return self._answer("search_people", kw, [])

    def search_people_by_image(self, **kw):
        return self._answer("search_people_by_image", kw, [])

    def search_vehicles(self, **kw):
        return self._answer("search_vehicles", kw, [])

    def search_plates(self, **kw):
        return self._answer("search_plates", kw,
                            {"plates": [], "plates_active": True, "scoped": True})

    def stats(self, domain, ids):
        return self._answer("stats", {"domain": domain, "ids": ids},
                            {"vectors_count": 0, "scoped": True, "status": "green",
                             "plates_active": None})

    def detections(self, **kw):
        return self._answer("detections", kw,
                            {"scoped": True, "summary": {}, "recent": [],
                             "recent_plates": [], "top_plates": [],
                             "plates_active": True})

    def crop_path(self, domain, hit_id):
        return self._answer("crop_path", {"domain": domain, "id": hit_id}, None)


class StubStore:
    def __init__(self, erase_result=None, erase_error=None):
        self.erase_calls: list[tuple] = []
        self._result = erase_result or {"persons": 0, "vehicles": 0,
                                        "crops_deleted": 0, "crops_failed": 0}
        self._error = erase_error

    def erase_range(self, camera, start, end):
        self.erase_calls.append((camera, start, end))
        if self._error:
            raise self._error
        return self._result

    def health(self):
        return {"ok": True}


def build(queries=None, store=None):
    """The real app, with stubbed collaborators."""
    q = queries or StubQueries()
    st = store or StubStore()
    app = create_app(
        engine=SimpleNamespace(),
        config=SimpleNamespace(),
        store=st,
        queries=q,
        pool=SimpleNamespace(),
        retention=None,
    )
    return TestClient(app, raise_server_exceptions=False), q, st


# ── 1. The erasure window — a DSR depends on this being exact ──────────────

class TestParseWindow:
    """Unit-level, because the failure is arithmetic rather than HTTP."""

    def test_a_z_suffixed_window_parses_to_utc(self):
        start, end = _parse_window("2026-09-10T00:00:00Z", "2026-09-10T12:00:00Z")
        assert start == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

    def test_a_naive_timestamp_is_read_as_utc_not_as_local(self):
        # Comparing a naive value against timestamptz would resolve against
        # whatever the database's timezone happens to be — an ambiguity an
        # erasure cannot afford.
        start, end = _parse_window("2026-09-10T00:00:00", "2026-09-10T12:00:00")
        assert start.tzinfo is not None and start.utcoffset().total_seconds() == 0
        assert end.tzinfo is not None

    def test_an_offset_window_is_preserved_as_an_instant(self):
        start, _ = _parse_window("2026-09-10T05:30:00+05:30", "2026-09-10T12:00:00Z")
        assert start == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)

    @pytest.mark.parametrize("bad", ["", "yesterday", "2026-13-45T00:00:00Z",
                                     "not-a-time", "1757462400"])
    def test_an_unparseable_bound_raises_rather_than_being_dropped(self, bad):
        # The whole reason this function exists instead of reusing
        # queries._parse_when, which returns None.
        with pytest.raises(ValueError):
            _parse_window(bad, "2026-09-10T12:00:00Z")
        with pytest.raises(ValueError):
            _parse_window("2026-09-10T00:00:00Z", bad)

    def test_the_failing_bound_is_named_in_the_message(self):
        with pytest.raises(ValueError, match="from="):
            _parse_window("rubbish", "2026-09-10T12:00:00Z")
        with pytest.raises(ValueError, match="to="):
            _parse_window("2026-09-10T00:00:00Z", "rubbish")

    def test_a_reversed_window_is_refused(self):
        # Silently empty otherwise: nothing is between a later start and an
        # earlier end, and the caller is told the erasure succeeded.
        with pytest.raises(ValueError, match="before"):
            _parse_window("2026-09-10T12:00:00Z", "2026-09-10T00:00:00Z")

    def test_a_zero_length_window_is_allowed(self):
        # An instant is a legitimate scope; only an INVERTED one is not.
        start, end = _parse_window("2026-09-10T12:00:00Z", "2026-09-10T12:00:00Z")
        assert start == end


class TestEraseEndpoint:
    def test_a_valid_window_reaches_the_store_with_parsed_instants(self):
        client, _, store = build()
        r = client.request("DELETE", "/erase", params={
            "camera": "gate-a1b2", "from": "2026-09-10T00:00:00Z",
            "to": "2026-09-10T12:00:00Z"})
        assert r.status_code == 200
        camera, start, end = store.erase_calls[0]
        assert camera == "gate-a1b2"
        assert (start, end) == (datetime(2026, 9, 10, 0, 0, tzinfo=UTC),
                                datetime(2026, 9, 10, 12, 0, tzinfo=UTC))

    @pytest.mark.parametrize("params", [
        {"camera": "c", "from": "rubbish", "to": "2026-09-10T12:00:00Z"},
        {"camera": "c", "from": "2026-09-10T00:00:00Z", "to": "rubbish"},
        {"camera": "c", "from": "2026-09-10T12:00:00Z", "to": "2026-09-10T00:00:00Z"},
    ])
    def test_a_bad_window_is_400_and_erases_nothing(self, params):
        client, _, store = build()
        r = client.request("DELETE", "/erase", params=params)
        assert r.status_code == 400
        assert store.erase_calls == [], "a rejected window still reached the store"

    def test_a_missing_bound_is_rejected_by_validation(self):
        client, _, store = build()
        r = client.request("DELETE", "/erase", params={"camera": "c"})
        assert r.status_code == 422
        assert store.erase_calls == []

    def test_a_store_failure_is_never_reported_as_success(self):
        # A caller reading this response decides whether to tell a data subject
        # their footage is destroyed.
        client, _, _ = build(store=StubStore(erase_error=RuntimeError("disk gone")))
        r = client.request("DELETE", "/erase", params={
            "camera": "c", "from": "2026-09-10T00:00:00Z",
            "to": "2026-09-10T12:00:00Z"})
        assert r.status_code == 503

    def test_orphaned_crop_files_make_the_erasure_incomplete_not_ok(self):
        # crops_failed > 0 means rows are gone but images remain on disk.
        client, _, _ = build(store=StubStore(erase_result={
            "persons": 5, "vehicles": 2, "crops_deleted": 5, "crops_failed": 2}))
        r = client.request("DELETE", "/erase", params={
            "camera": "c", "from": "2026-09-10T00:00:00Z",
            "to": "2026-09-10T12:00:00Z"})
        assert r.status_code == 200
        assert r.json()["status"] == "incomplete"

    def test_a_clean_erasure_reports_ok_with_its_counts(self):
        client, _, _ = build(store=StubStore(erase_result={
            "persons": 5, "vehicles": 2, "crops_deleted": 7, "crops_failed": 0}))
        body = client.request("DELETE", "/erase", params={
            "camera": "c", "from": "2026-09-10T00:00:00Z",
            "to": "2026-09-10T12:00:00Z"}).json()
        assert body["status"] == "ok"
        assert body["persons"] == 5 and body["crops_deleted"] == 7

    def test_the_response_echoes_the_window_it_actually_used(self):
        # So a caller can see what was destroyed rather than what it asked for.
        client, _, _ = build()
        body = client.request("DELETE", "/erase", params={
            "camera": "c", "from": "2026-09-10T00:00:00",
            "to": "2026-09-10T12:00:00Z"}).json()
        assert body["from"].startswith("2026-09-10T00:00:00")
        assert "+00:00" in body["from"], "the window was echoed without its timezone"


# ── 2. An encoder outage is a 503, never an empty result ───────────────────

class TestEncoderOutageIs503:
    @pytest.mark.parametrize("path,payload", [
        ("/person/search", {"query": "a man in red"}),
        ("/vehicles/search", {"query": "a white van"}),
    ])
    def test_a_text_search_answers_503(self, path, payload):
        q = StubQueries()
        q.raises = {"search_people": EncoderUnavailable("no encoder"),
                    "search_vehicles": EncoderUnavailable("no encoder")}
        client, _, _ = build(queries=q)
        r = client.post(path, json=payload)
        assert r.status_code == 503
        assert "results" not in r.json(), "an outage produced a result set"

    def test_the_503_says_ingest_is_unaffected(self):
        # The operator's next question is whether the appliance is still
        # recording and indexing. It is.
        q = StubQueries()
        q.raises = {"search_people": EncoderUnavailable("no encoder")}
        client, _, _ = build(queries=q)
        r = client.post("/person/search", json={"query": "x"})
        assert "unaffected" in r.json()["detail"]

    def test_plate_lookup_still_answers_when_the_encoder_is_gone(self):
        # No embedder on this path by design: the most precise identifier the
        # product has keeps working on a box where the model will not load.
        q = StubQueries()
        q.raises = {"search_people": EncoderUnavailable("no encoder")}
        client, _, _ = build(queries=q)
        assert client.get("/plates/search", params={"plate": "MH12AB1234"}).status_code == 200


# ── 3. The six routes the VMS calls must exist ─────────────────────────────

class TestTheContractSurface:
    """Two of these are reached past smartsearch_client by routers/search.py,
    which is exactly why they are easy to miss: omitting them does not fail
    loudly, it produces a results grid with no thumbnails and a dashboard that
    502s."""

    @pytest.mark.parametrize("method,path,kw", [
        ("post", "/person/search", {"json": {"query": "x"}}),
        ("post", "/vehicles/search", {"json": {"query": "x"}}),
        ("get", "/person/stats", {}),
        ("get", "/vehicles/stats", {}),
        ("get", "/detections", {}),
        ("get", "/plates/search", {"params": {"plate": "AB12"}}),
    ])
    def test_the_route_is_served(self, method, path, kw):
        client, _, _ = build()
        r = getattr(client, method)(path, **kw)
        assert r.status_code != 404, f"{method.upper()} {path} is not served"

    def test_the_crop_image_route_is_served(self):
        client, _, _ = build()
        # 404 from the handler (no such crop), not from routing.
        r = client.get("/person/image", params={"id": "1"})
        assert r.json()["detail"] == "no such crop"

    @pytest.mark.parametrize("key", ["scoped", "summary", "recent",
                                     "recent_plates", "top_plates", "plates_active"])
    def test_the_detections_response_keeps_its_keys(self, key):
        client, _, _ = build()
        assert key in client.get("/detections").json()

    def test_stats_carries_the_scoped_flag_the_vms_demands(self):
        client, _, _ = build()
        for path in ("/person/stats", "/vehicles/stats"):
            assert client.get(path).json()["scoped"] is True


# ── 4. Scope reaches the query layer intact ────────────────────────────────

class TestScopePassthrough:
    def test_an_empty_camera_list_is_passed_through_as_a_list(self):
        # Not as None. The distinction is the whole-index leak guarded in
        # test_queries.py; it has to survive the HTTP boundary to matter.
        client, q, _ = build()
        client.post("/person/search", json={"query": "x", "sensor_ids": []})
        assert q.calls[0][1]["sensor_ids"] == []

    def test_stats_uses_the_name_each_domain_puts_on_the_wire(self):
        # vehicles says camera_ids, person says sensor_ids; the client sends
        # the name its domain expects.
        client, q, _ = build()
        client.get("/person/stats", params={"sensor_ids": ["a"]})
        client.get("/vehicles/stats", params={"camera_ids": ["b"]})
        assert q.calls[0][1] == {"domain": "person", "ids": ["a"]}
        assert q.calls[1][1] == {"domain": "vehicles", "ids": ["b"]}

    def test_absent_scope_reaches_the_layer_as_none(self):
        client, q, _ = build()
        client.get("/person/stats")
        assert q.calls[0][1]["ids"] is None

    def test_an_unknown_detections_domain_is_discarded_not_passed_on(self):
        # `picked` filters to the two real domains; an unrecognised one must
        # not narrow the feed to nothing.
        client, q, _ = build()
        client.get("/detections", params={"domain": ["wombat"]})
        assert q.calls[0][1]["domains"] is None

    def test_a_recognised_detections_domain_is_passed_on(self):
        client, q, _ = build()
        client.get("/detections", params={"domain": ["vehicles"]})
        assert q.calls[0][1]["domains"] == ["vehicles"]


# ── 5. Request validation at the edge ──────────────────────────────────────

class TestValidation:
    @pytest.mark.parametrize("body", [
        {},                                   # no query at all
        {"query": ""},                        # min_length=1
        {"query": "x" * 501},                 # max_length=500
        {"query": "x", "top_k": 0},           # ge=1
        {"query": "x", "top_k": 501},         # le=500
        {"query": "x", "score_threshold": -0.1},
        {"query": "x", "score_threshold": 1.1},
    ])
    def test_a_bad_search_body_is_422_and_runs_no_query(self, body):
        client, q, _ = build()
        assert client.post("/person/search", json=body).status_code == 422
        assert q.calls == []

    @pytest.mark.parametrize("params", [
        {},                                    # plate is required
        {"plate": ""},                         # min_length=1
        {"plate": "x" * 33},                   # max_length=32
        {"plate": "AB", "limit": 0},
        {"plate": "AB", "limit": 2001},
    ])
    def test_a_bad_plate_query_is_422(self, params):
        client, q, _ = build()
        assert client.get("/plates/search", params=params).status_code == 422
        assert q.calls == []

    @pytest.mark.parametrize("params", [
        {"since_ms": -1},
        {"limit": 0},
        {"limit": 501},
        {"tz_offset_min": -901},
        {"tz_offset_min": 901},
    ])
    def test_a_bad_detections_query_is_422(self, params):
        client, q, _ = build()
        assert client.get("/detections", params=params).status_code == 422
        assert q.calls == []

    def test_an_unknown_crop_domain_is_404(self):
        client, q, _ = build()
        r = client.get("/wombat/image", params={"id": "1"})
        assert r.status_code == 404
        assert r.json()["detail"] == "unknown domain"

    def test_a_crop_whose_file_vanished_is_404_not_500(self):
        # Retention deletes rows first, so the row can outlive its file. A 500
        # would suggest the service is broken when the crop is simply gone.
        q = StubQueries(crop_path="/definitely/not/here.jpg")
        client, _, _ = build(queries=q)
        r = client.get("/person/image", params={"id": "1"})
        assert r.status_code == 404

    def test_an_empty_crop_file_is_404_not_an_empty_200(self, tmp_path):
        # The 80 zero-byte crops measured on this appliance: written before a
        # power cut, referenced by a committed row. FileResponse serves them as
        # a 200 with no body, which renders as a broken image and reads
        # downstream as "this detection just looks like nothing". The file
        # exists, so existence alone cannot be the check.
        empty = tmp_path / "torn.jpg"
        empty.touch()
        q = StubQueries(crop_path=str(empty))
        client, _, _ = build(queries=q)
        r = client.get("/person/image", params={"id": "1"})
        assert r.status_code == 404, "an empty crop was served as a 200"
        assert r.json()["detail"] == "crop file is empty"
