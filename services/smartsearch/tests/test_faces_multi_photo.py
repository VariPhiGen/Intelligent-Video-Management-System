"""Face search by several photographs of ONE person.

WHAT IS PINNED. `faces.fuse` pools the photos' face vectors (mean of the
normalised vectors, renormalised) and reports AGREEMENT, the least similar pair.
The endpoint accepts one photo as the raw body — the original contract, which a
VMS that predates this still speaks — or up to five as multipart `photos`.

The cases that matter most are the ones where a photo does not help: a photo
with no face is skipped and REPORTED (the rest still answer), a photo that does
not decode is a 400 naming it, and too many photos are refused before the
detector runs.

The encoder is a fake keyed on the photo's colour, so each photograph's "face"
is a known vector and the pooled query can be asserted exactly. PNG, so the
colour survives encoding.

Run (from services/smartsearch): python -m pytest tests/test_faces_multi_photo.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.server import create_app  # noqa: E402
from index.faces import MAX_QUERY_PHOTOS, Face, fuse  # noqa: E402


def unit(*xs: float) -> np.ndarray:
    v = np.zeros(128, dtype="float32")
    v[:len(xs)] = xs
    return v / np.linalg.norm(v)


# ── fuse ───────────────────────────────────────────────────────────────────

def test_one_photo_is_searched_with_its_own_vector():
    v = unit(0.6, 0.8)
    fused, agreement = fuse([v])
    assert np.allclose(fused, v, atol=1e-6)
    assert agreement is None


def test_several_photos_pool_to_a_unit_vector_between_them():
    fused, _ = fuse([unit(1, 0), unit(0, 1)])
    assert np.linalg.norm(fused) == pytest.approx(1.0, abs=1e-6)
    assert np.allclose(fused[:2], [2 ** -0.5, 2 ** -0.5], atol=1e-6)


def test_each_photo_counts_once_whatever_its_raw_length():
    # Without normalising first, a vector with a larger norm would drag the
    # query toward its photo — a weighting nobody chose.
    fused, _ = fuse([unit(1, 0) * 10.0, unit(0, 1)])
    assert fused[0] == pytest.approx(fused[1], abs=1e-6)


def test_agreement_is_the_least_similar_pair_not_the_average():
    a, b, c = unit(1, 0, 0), unit(0.8, 0.6, 0), unit(0, 0, 1)
    assert fuse([a, b])[1] == pytest.approx(0.8, abs=1e-6)
    # One stranger among close photos must show up, not be averaged away.
    assert fuse([a, b, c])[1] == pytest.approx(0.0, abs=1e-6)


def test_nothing_to_pool_is_refused():
    with pytest.raises(ValueError):
        fuse([])
    with pytest.raises(ValueError):
        fuse([np.zeros(128, dtype="float32")])


# ── the endpoint ───────────────────────────────────────────────────────────

BLUE, GREEN, GREY = (255, 0, 0), (0, 255, 0), (128, 128, 128)

#: The face the fake detector finds in a photo of each colour. GREY has none.
FACE_OF = {BLUE: unit(1, 0), GREEN: unit(0, 1)}


def photo(bgr) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((40, 40, 3), bgr, dtype=np.uint8))
    assert ok
    return buf.tobytes()


class FakeEncoder:
    active = True
    name = "sface-2021dec"
    dim = 128
    score_threshold = 0.85
    min_width_px = 40

    def __init__(self):
        self.seen: list[tuple] = []

    def detect(self, frame):
        key = tuple(int(x) for x in frame[0, 0])
        self.seen.append(key)
        vec = FACE_OF.get(key)
        if vec is None:
            return []
        return [Face(embedding=vec, aligned=frame, score=0.9, width_px=64,
                     box=(0, 0, 64, 64))]


class Queries:
    def __init__(self):
        self.calls: list[dict] = []

    def search_faces(self, **kw):
        self.calls.append(kw)
        return [{"id": "1", "score": 0.5}]


def build():
    enc, q = FakeEncoder(), Queries()
    app = create_app(engine=SimpleNamespace(), config=SimpleNamespace(),
                     store=SimpleNamespace(), queries=q,
                     pool=SimpleNamespace(face_encoder=lambda: enc), retention=None)
    return TestClient(app, raise_server_exceptions=False), enc, q


def multipart(*images: bytes):
    return [("photos", (f"p{n}.png", img, "image/png")) for n, img in enumerate(images)]


def test_one_photo_as_the_raw_body_still_answers_as_before():
    client, _, q = build()
    r = client.post("/faces/search_by_image", content=photo(BLUE),
                    headers={"Content-Type": "image/png"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert np.allclose(q.calls[0]["vector"], unit(1, 0), atol=1e-6)
    assert q.calls[0]["model"] == "sface-2021dec"
    assert body["faces_detected"] == 1
    assert body["query_face"] == {"score": 0.9, "width_px": 64}
    assert body["photos_used"] == 1 and body["agreement"] is None


def test_several_photos_are_searched_as_one_pooled_query():
    client, _, q = build()
    r = client.post("/faces/search_by_image", files=multipart(photo(BLUE), photo(GREEN)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(q.calls) == 1, "several photos must be ONE query, not one per photo"
    assert np.allclose(q.calls[0]["vector"][:2], [2 ** -0.5, 2 ** -0.5], atol=1e-6)
    assert body["photos_used"] == 2
    assert body["agreement"] == pytest.approx(0.0, abs=1e-6)
    assert [p["used"] for p in body["photos"]] == [True, True]


def test_a_photo_with_no_face_is_skipped_and_named_not_refused():
    client, _, q = build()
    r = client.post("/faces/search_by_image",
                    files=multipart(photo(GREY), photo(BLUE)))
    assert r.status_code == 200, r.text
    body = r.json()
    # The faceless photo did not pull the query anywhere.
    assert np.allclose(q.calls[0]["vector"], unit(1, 0), atol=1e-6)
    assert body["photos"][0] == {"photo": 1, "faces_detected": 0, "used": False,
                                 "score": None, "width_px": None}
    assert body["photos"][1]["used"] is True
    assert body["photos_used"] == 1 and body["agreement"] is None


def test_no_face_in_any_photo_says_so_and_runs_no_query():
    client, _, q = build()
    r = client.post("/faces/search_by_image", files=multipart(photo(GREY), photo(GREY)))
    assert r.status_code == 200
    body = r.json()
    assert q.calls == []
    assert body["faces_detected"] == 0 and body["results"] == []
    assert "any of the 2 photos" in body["detail"]


def test_too_many_photos_are_refused_before_the_detector_runs():
    client, enc, q = build()
    r = client.post("/faces/search_by_image",
                    files=multipart(*[photo(BLUE)] * (MAX_QUERY_PHOTOS + 1)))
    assert r.status_code == 400
    assert enc.seen == [] and q.calls == []


def test_the_limit_itself_is_accepted():
    client, _, q = build()
    r = client.post("/faces/search_by_image",
                    files=multipart(*[photo(BLUE)] * MAX_QUERY_PHOTOS))
    assert r.status_code == 200, r.text
    assert r.json()["photos_used"] == MAX_QUERY_PHOTOS


def test_an_undecodable_photo_is_a_400_naming_it():
    client, _, q = build()
    r = client.post("/faces/search_by_image",
                    files=multipart(photo(BLUE), b"not an image"))
    assert r.status_code == 400
    assert "photo 2" in r.json()["detail"]
    assert q.calls == []


def test_an_empty_photo_part_is_refused_naming_it():
    client, _, q = build()
    r = client.post("/faces/search_by_image", files=multipart(photo(BLUE), b""))
    assert r.status_code == 400
    assert "photo 2" in r.json()["detail"]
    assert q.calls == []


def test_multipart_without_a_photos_part_is_refused():
    client, _, q = build()
    r = client.post("/faces/search_by_image",
                    files=[("picture", ("p.png", photo(BLUE), "image/png"))])
    assert r.status_code == 400
    assert q.calls == []
