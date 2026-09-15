"""server.py, the write half — registration, heartbeats and ingest.

The read half is covered in test_server_api.py. This is the other direction,
and its failure modes are the mirror image: a read defect returns a wrong
answer, a write defect quietly stops the index growing while everything still
reports healthy.

THREE THINGS CARRY THE RISK.

`POST /cameras/{name}` is an UPSERT rather than create-or-409, and deliberately
so: four uvicorn workers reconcile the registry concurrently, and create-or-409
plus a re-add once interleaved into a camera that was registered but never
sampled. The retention restamp that follows is unconditional for the same
reason — gating it on `outcome == "updated"` drops it whenever another worker
wins the race.

`POST /observations` must answer 503, never 200, when the pipeline is not ready.
A silent 200 tells the analytics service the crop was stored; it moves on, and
the observation is gone. And `rows_written == 0` is a NORMAL outcome — appearance
dedup rejecting an object already represented — so it must not read as failure.

The heartbeat exists for the quiet case. Observations stop for two very
different reasons: nothing is happening, or nothing is running. From the index's
side they are identical, and this is the only thing that tells them apart.

Hermetic: engine, store and pipeline are stubs; no database, no model.

Run (from services/smartsearch): python -m pytest tests -q
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.server import create_app  # noqa: E402


# ── Stubs ──────────────────────────────────────────────────────────────────

class StubPipeline:
    def __init__(self, written=1, raises=None):
        self.calls: list = []
        self._written = written
        self._raises = raises

    def ingest_observations(self, items):
        self.calls.append(list(items))
        if self._raises:
            raise self._raises
        return self._written


class StubEngine:
    def __init__(self, pipeline=None, outcome="created"):
        self.pipeline = pipeline
        self.upserts: list = []
        self.producers: list = []
        self.removed: list = []
        self._outcome = outcome
        self.ingest_enabled = True

    def upsert_camera(self, name, url, domains, retention_days):
        self.upserts.append((name, url, domains, retention_days))
        return SimpleNamespace(domains=domains), self._outcome

    def note_producer(self, name, stats):
        self.producers.append((name, stats))

    def snapshot(self):
        return []

    def producers_snapshot(self):
        return {"state": "RECEIVING"}

    def remove_camera(self, name):
        self.removed.append(name)
        return True


class StubStore:
    def __init__(self, restamped=0, error=None):
        self.retention_calls: list = []
        self._restamped = restamped
        self._error = error

    def set_camera_retention(self, name, days):
        self.retention_calls.append((name, days))
        return {"rows_restamped": self._restamped, "error": self._error}

    def health(self):
        return {"ok": True}


def build(engine=None, store=None, face_model=None):
    eng = engine or StubEngine()
    st = store or StubStore()
    # The pool double carries what the routes actually read off it. A bare
    # SimpleNamespace passed for as long as nothing did — and then the ingest
    # path started checking which face model this index runs, and every test
    # here turned into a 500 that said nothing about the change that caused it.
    pool = SimpleNamespace(configured_face_model=face_model,
                           faces_available=face_model is not None,
                           face_encoder=lambda: None)
    app = create_app(engine=eng, config=SimpleNamespace(), store=st,
                     queries=SimpleNamespace(available=True, plates_active=True),
                     pool=pool, retention=None)
    return TestClient(app, raise_server_exceptions=False), eng, st


def png_bytes(size=(16, 16), colour=(120, 40, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def meta(**over):
    m = {"camera": "gate-a1b2", "ts": 1_757_500_000.0, "domain": "person",
         "confidence": 0.91, "label": "person", "bbox": [0.1, 0.2, 0.3, 0.4]}
    m.update(over)
    return m


def post_observation(client, m=None, crop=None):
    return client.post("/observations", files={
        "meta": (None, json.dumps(m if m is not None else meta())),
        "crop": ("crop.png", crop if crop is not None else png_bytes(), "image/png"),
    })


# ── Camera registration ────────────────────────────────────────────────────

class TestAddCamera:
    def test_a_camera_is_registered_with_the_default_domains(self):
        client, eng, _ = build()
        r = client.post("/cameras/gate-a1b2", params={"rtsp_url": "rtsp://relay/gate"})
        assert r.status_code == 200
        assert r.json()["domains"] == ["person", "vehicles"]
        assert eng.upserts[0][2] == ("person", "vehicles")

    def test_explicit_domains_are_honoured(self):
        client, eng, _ = build()
        client.post("/cameras/gate-a1b2",
                    params={"rtsp_url": "rtsp://relay/gate", "domains": ["plate"]})
        assert eng.upserts[0][2] == ("plate",)

    def test_an_unrecognised_domain_is_refused_rather_than_silently_dropped(self):
        # Dropping it would register the camera with the DEFAULT domains, so an
        # operator who asked for one thing gets another and nothing says so.
        client, eng, _ = build()
        r = client.post("/cameras/gate-a1b2",
                        params={"rtsp_url": "rtsp://relay/gate", "domains": ["wombat"]})
        assert r.status_code == 422
        assert eng.upserts == [], "the camera was registered despite a bad domain"

    def test_a_mix_of_valid_and_invalid_domains_keeps_the_valid_ones(self):
        client, eng, _ = build()
        r = client.post("/cameras/gate-a1b2", params={
            "rtsp_url": "rtsp://relay/gate", "domains": ["person", "wombat"]})
        assert r.status_code == 200
        assert eng.upserts[0][2] == ("person",)

    def test_registration_is_an_upsert_not_a_conflict(self):
        # Four workers reconcile the registry concurrently. create-or-409 plus
        # a re-add once interleaved into a camera that was registered but never
        # sampled.
        client, _, _ = build(engine=StubEngine(outcome="updated"))
        r = client.post("/cameras/gate-a1b2", params={"rtsp_url": "rtsp://relay/gate"})
        assert r.status_code == 200
        assert r.json()["outcome"] == "updated"

    @pytest.mark.parametrize("outcome", ["created", "updated", "unchanged"])
    def test_the_retention_restamp_runs_whatever_the_outcome(self, outcome):
        # THE RACE. Only one worker sees the transition; gating the restamp on
        # `outcome == "updated"` drops it whenever another worker wins.
        client, _, store = build(engine=StubEngine(outcome=outcome))
        client.post("/cameras/gate-a1b2",
                    params={"rtsp_url": "rtsp://relay/gate", "retention_days": 7})
        assert store.retention_calls == [("gate-a1b2", 7)], (
            f"the restamp was skipped for outcome={outcome!r}"
        )

    def test_the_restamp_count_is_surfaced(self):
        # An operator who shortens retention can see the existing rows move
        # rather than having to trust that they did.
        client, _, _ = build(store=StubStore(restamped=412))
        body = client.post("/cameras/gate-a1b2",
                           params={"rtsp_url": "rtsp://relay/gate",
                                   "retention_days": 3}).json()
        assert body["rows_restamped"] == 412
        assert body["retention_error"] is None

    def test_a_failed_restamp_is_reported_not_swallowed(self):
        client, _, _ = build(store=StubStore(error="deadlock detected"))
        body = client.post("/cameras/gate-a1b2",
                           params={"rtsp_url": "rtsp://relay/gate"}).json()
        assert body["retention_error"] == "deadlock detected"

    def test_omitting_retention_follows_the_appliance_default(self):
        client, _, store = build()
        client.post("/cameras/gate-a1b2", params={"rtsp_url": "rtsp://relay/gate"})
        assert store.retention_calls == [("gate-a1b2", None)]

    @pytest.mark.parametrize("days", [0, -1, 3651])
    def test_an_out_of_range_retention_is_refused(self, days):
        client, eng, _ = build()
        r = client.post("/cameras/gate-a1b2", params={
            "rtsp_url": "rtsp://relay/gate", "retention_days": days})
        assert r.status_code == 422
        assert eng.upserts == []

    def test_the_relay_url_is_required(self):
        client, eng, _ = build()
        assert client.post("/cameras/gate-a1b2").status_code == 422
        assert eng.upserts == []


# ── The heartbeat, which exists for the quiet case ─────────────────────────

class TestProducerHeartbeat:
    def test_a_heartbeat_is_recorded(self):
        client, eng, _ = build()
        r = client.post("/producers/analytics-1/heartbeat", json={"frames": 120})
        assert r.status_code == 200
        assert eng.producers == [("analytics-1", {"frames": 120})]

    def test_a_heartbeat_with_no_body_still_counts(self):
        # THE POINT. A producer watching an empty yard sends nothing but must
        # still be able to say it is alive; refusing an empty body would make
        # a healthy quiet camera indistinguishable from a dead producer.
        client, eng, _ = build()
        r = client.post("/producers/analytics-1/heartbeat")
        assert r.status_code == 200
        assert eng.producers == [("analytics-1", {})]

    @pytest.mark.parametrize("body", [b"not json", b"[1,2,3]", b'"a string"', b"null"])
    def test_a_malformed_body_does_not_lose_the_heartbeat(self, body):
        # The liveness signal matters more than the stats it carries. Rejecting
        # the whole heartbeat over a bad stats blob would report an outage that
        # is not happening.
        client, eng, _ = build()
        r = client.post("/producers/analytics-1/heartbeat", content=body,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert eng.producers[0][1] == {}

    def test_the_producer_name_comes_back_for_correlation(self):
        client, _, _ = build()
        assert client.post("/producers/an-1/heartbeat").json()["producer"] == "an-1"


# ── Ingest ─────────────────────────────────────────────────────────────────

class TestIngestReadiness:
    def test_a_pipeline_that_is_not_ready_answers_503(self):
        # 503 rather than a silent 200: the caller must be able to retry rather
        # than believe an observation was stored.
        client, _, _ = build(engine=StubEngine(pipeline=None))
        r = post_observation(client)
        assert r.status_code == 503
        assert "not ready" in r.json()["detail"]

    def test_a_ready_pipeline_accepts_the_observation(self):
        pipe = StubPipeline(written=1)
        client, _, _ = build(engine=StubEngine(pipeline=pipe))
        r = post_observation(client)
        assert r.status_code == 200
        assert r.json() == {"accepted": True, "rows_written": 1, "deduped": False}
        assert len(pipe.calls) == 1


class TestIngestValidation:
    @pytest.fixture
    def client(self):
        c, _, _ = build(engine=StubEngine(pipeline=StubPipeline()))
        return c

    def test_a_request_with_no_parts_is_422(self, client):
        assert client.post("/observations").status_code == 422

    def test_a_missing_crop_part_is_422(self, client):
        r = client.post("/observations", files={"meta": (None, json.dumps(meta()))})
        assert r.status_code == 422

    def test_a_missing_meta_part_is_422(self, client):
        r = client.post("/observations",
                        files={"crop": ("c.png", png_bytes(), "image/png")})
        assert r.status_code == 422

    def test_unparseable_meta_is_422(self, client):
        r = client.post("/observations", files={
            "meta": (None, "{not json"),
            "crop": ("c.png", png_bytes(), "image/png")})
        assert r.status_code == 422
        assert "bad meta" in r.json()["detail"]

    @pytest.mark.parametrize("field", ["camera", "ts", "domain"])
    def test_meta_missing_a_required_field_is_422_and_names_it(self, client, field):
        m = meta()
        del m[field]
        r = post_observation(client, m=m)
        assert r.status_code == 422
        assert field in r.json()["detail"]

    def test_an_undecodable_crop_is_422_not_500(self, client):
        r = post_observation(client, crop=b"definitely not an image")
        assert r.status_code == 422
        assert "bad crop" in r.json()["detail"]

    def test_a_truncated_image_is_422(self, client):
        # `.load()` is what forces the decode; without it a truncated PNG
        # reaches the encoder and fails much later.
        r = post_observation(client, crop=png_bytes()[:40])
        assert r.status_code == 422


class TestIngestSemantics:
    def _client(self, written=1):
        pipe = StubPipeline(written=written)
        client, _, _ = build(engine=StubEngine(pipeline=pipe))
        return client, pipe

    def test_a_deduplicated_observation_is_not_a_failure(self):
        # rows_written == 0 is a NORMAL outcome: appearance dedup rejected an
        # object already represented in this window. Reporting it as an error
        # would make a working camera look broken.
        client, _ = self._client(written=0)
        r = post_observation(client)
        assert r.status_code == 200
        assert r.json() == {"accepted": True, "rows_written": 0, "deduped": True}

    def test_the_observation_reaches_the_pipeline_intact(self):
        client, pipe = self._client()
        post_observation(client, m=meta(camera="lobby-c3d4", domain="vehicles",
                                        confidence=0.77, tracker_id=42,
                                        plate="MH12AB1234", plate_confidence=0.8))
        item = pipe.calls[0][0]
        assert item.slug == "lobby-c3d4"
        assert item.domain == "vehicles"
        assert item.confidence == 0.77
        assert item.tracker_id == 42
        assert item.plate == "MH12AB1234"

    def test_the_crop_arrives_as_a_decoded_rgb_image(self):
        # It is embedded before it is saved, so it has to be a real image by
        # the time the pipeline sees it.
        client, pipe = self._client()
        post_observation(client, crop=png_bytes(size=(24, 18)))
        crop = pipe.calls[0][0].crop
        assert crop.mode == "RGB"
        assert crop.size == (24, 18)

    def test_a_greyscale_crop_is_converted_rather_than_refused(self):
        buf = io.BytesIO()
        Image.new("L", (16, 16), 128).save(buf, format="PNG")
        client, pipe = self._client()
        r = post_observation(client, crop=buf.getvalue())
        assert r.status_code == 200
        assert pipe.calls[0][0].crop.mode == "RGB"

    def test_a_missing_bbox_defaults_to_the_whole_crop(self):
        m = meta()
        del m["bbox"]
        client, pipe = self._client()
        post_observation(client, m=m)
        assert pipe.calls[0][0].bbox == (0.0, 0.0, 1.0, 1.0)

    def test_a_missing_label_falls_back_to_the_domain(self):
        m = meta()
        del m["label"]
        client, pipe = self._client()
        post_observation(client, m=m)
        assert pipe.calls[0][0].label == "person"

    def test_a_missing_confidence_is_zero_not_an_error(self):
        m = meta()
        del m["confidence"]
        client, pipe = self._client()
        assert post_observation(client, m=m).status_code == 200
        assert pipe.calls[0][0].confidence == 0.0

    def test_a_vehicle_with_no_plate_is_still_indexed(self):
        # A vehicle is indexed whether or not its plate could be read.
        client, pipe = self._client()
        r = post_observation(client, m=meta(domain="vehicles"))
        assert r.status_code == 200
        assert pipe.calls[0][0].plate is None
