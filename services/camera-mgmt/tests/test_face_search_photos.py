"""Face search with several photos, through the VMS proxy and its index client.

WHAT IS PINNED.

* The proxy accepts one photo as the raw body (the original contract, which a
  cached older bundle still sends) or up to MAX_FACE_PHOTOS as multipart
  `photos`, and refuses too many, an empty part or an oversized photo BEFORE
  the index is called.
* The index client keeps the single-photo request byte-for-byte what it was, so
  an index that predates multi-photo search still answers it, and uses
  multipart only for several.
* The per-photo report and `agreement` reach the browser, and the audit row
  records how many photos — never the photos, which are biometric data.

Driven over HTTP for the proxy (validation runs inside the routed handler,
after auth) with the index client and the scope rules stubbed, and against an
httpx MockTransport for the client. No network either way.

Run from anywhere: python -m pytest services/camera-mgmt/tests/test_face_search_photos.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from backend.routers import search as search_router
from backend.services import search_scope
from backend.services import smartsearch_client as index_client

pytestmark = pytest.mark.asyncio

JPEG = b"\xff\xd8\xff\xe0 a photo"


@pytest.fixture
def index(monkeypatch):
    """The index client, the camera registry and the audit trail, stubbed."""
    calls: list[dict] = []
    audits: list[dict] = []

    async def fake_search(**kw):
        calls.append(kw)
        n = len(kw["images"])
        return {
            "results": [], "faces_detected": n,
            "photos": [{"photo": i + 1, "faces_detected": 1, "used": True}
                       for i in range(n)],
            "photos_used": n,
            "agreement": 0.41 if n > 1 else None,
        }

    async def searchable(db):
        return [SimpleNamespace(slug="cabin")], None

    async def scope(hits, **kw):
        return search_scope.ScopeResult()

    async def record(*a, **kw):
        audits.append(kw)

    monkeypatch.setattr(search_router.index_client, "search_faces_by_image", fake_search)
    monkeypatch.setattr(search_router, "_searchable", searchable)
    monkeypatch.setattr(search_router.search_scope, "scope", scope)
    monkeypatch.setattr(search_router.audit_svc, "record", record)
    return SimpleNamespace(calls=calls, audits=audits)


def _photos(*images: bytes):
    return [("photos", (f"p{n}.jpg", img, "image/jpeg")) for n, img in enumerate(images)]


# ── the proxy ──────────────────────────────────────────────────────────────

async def test_one_photo_as_the_raw_body_is_still_accepted(client, mint, index):
    r = await client.post("/api/search/faces/by-image", content=JPEG,
                          headers={**mint(["admin"]), "Content-Type": "image/jpeg"})
    assert r.status_code == 200, r.text
    assert index.calls[0]["images"] == [JPEG]
    assert index.audits[0]["detail"]["photos"] == 1
    assert index.audits[0]["detail"]["image_bytes"] == len(JPEG)


async def test_several_photos_reach_the_index_in_order_as_one_search(client, mint, index):
    a, b, c = JPEG + b"a", JPEG + b"bb", JPEG + b"ccc"
    r = await client.post("/api/search/faces/by-image", files=_photos(a, b, c),
                          headers=mint(["admin"]))
    assert r.status_code == 200, r.text
    assert len(index.calls) == 1
    assert index.calls[0]["images"] == [a, b, c]
    body = r.json()
    # The facts about the query that only the index knows reach the browser.
    assert body["photos_used"] == 3
    assert body["agreement"] == 0.41
    assert [p["photo"] for p in body["photos"]] == [1, 2, 3]
    detail = index.audits[0]["detail"]
    assert detail["photos"] == 3
    assert detail["image_bytes"] == len(a) + len(b) + len(c)
    assert JPEG not in repr(detail).encode(), "photo bytes leaked into the audit row"


async def test_more_than_the_limit_is_refused_before_the_index(client, mint, index):
    r = await client.post(
        "/api/search/faces/by-image",
        files=_photos(*[JPEG] * (search_router.MAX_FACE_PHOTOS + 1)),
        headers=mint(["admin"]))
    assert r.status_code == 400
    assert index.calls == []


async def test_an_empty_photo_is_refused_naming_it(client, mint, index):
    r = await client.post("/api/search/faces/by-image", files=_photos(JPEG, b""),
                          headers=mint(["admin"]))
    assert r.status_code == 400
    assert "Photo 2" in r.text
    assert index.calls == []


async def test_an_oversized_photo_is_413(client, mint, index):
    big = b"\xff" * (8 * 1024 * 1024 + 1)
    r = await client.post("/api/search/faces/by-image", files=_photos(JPEG, big),
                          headers=mint(["admin"]))
    assert r.status_code == 413
    assert index.calls == []


async def test_no_photo_at_all_is_400(client, mint, index):
    r = await client.post("/api/search/faces/by-image", headers=mint(["admin"]))
    assert r.status_code == 400
    assert index.calls == []


async def test_the_limit_does_not_exceed_the_index_limit():
    # Separate deployables, so no shared import — but a proxy that accepted
    # more than the index would hand operators a 400 from behind it.
    assert search_router.MAX_FACE_PHOTOS <= 5


# ── the index client ───────────────────────────────────────────────────────

@pytest.fixture
def wire(monkeypatch):
    """Every request the client makes, answered in-process."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(index_client.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(index_client, "is_configured", lambda: True)
    monkeypatch.setattr(index_client, "_base", lambda: "http://index.test")
    return seen


async def test_one_photo_is_sent_exactly_as_before(wire):
    await index_client.search_faces_by_image(images=[JPEG], top_k=5, score_threshold=0.0)
    (req,) = wire
    assert req.headers["content-type"] == "image/jpeg"
    assert req.content == JPEG


async def test_several_photos_are_sent_as_multipart_parts(wire):
    await index_client.search_faces_by_image(
        images=[JPEG + b"1", JPEG + b"2"], top_k=5, score_threshold=0.0)
    (req,) = wire
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert req.content.count(b'name="photos"') == 2
    assert JPEG + b"1" in req.content and JPEG + b"2" in req.content
