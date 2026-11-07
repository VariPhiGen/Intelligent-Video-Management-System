"""Who may read the pile, and who may only see their own frame back.

The split matters: an operator who drew a box must be able to look at what they
saved (`playback_search`), but browsing and exporting the training pile is Data
Collection's job (`data_collection`), which operators do not hold by default.
"""
from __future__ import annotations

import uuid

import pytest

URL = "/api/annotations"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "/stats"])
async def test_browsing_the_pile_needs_data_collection(client, mint, path):
    assert (await client.get(URL + path,
                             headers=mint(["operator"]))).status_code == 403
    assert (await client.get(URL + path,
                             headers=mint(["viewer"]))).status_code == 403
    assert (await client.get(URL + path,
                             headers=mint(["supervisor"]))).status_code == 200


@pytest.mark.asyncio
async def test_an_operator_may_fetch_a_frame_back(client, mint, monkeypatch, db):
    """Gated on playback_search, not data_collection: it is the frame they
    just drew on, and not being able to see it would make the save feel lost."""
    from backend.models import Annotation
    from backend.services import nvr_client
    from datetime import datetime, timezone

    aid = uuid.uuid4()
    db.add(Annotation(
        id=aid, camera_slug="gate-cam", class_key="forklift",
        captured_at=datetime.now(timezone.utc),
        bbox_x=1, bbox_y=1, bbox_w=2, bbox_h=2,
        frame_width=1920, frame_height=1080, image_path="/a.jpg"))

    async def _image(annotation_id):
        return b"\xff\xd8jpegbytes"
    monkeypatch.setattr(nvr_client, "fetch_annotation_image", _image)

    r = await client.get(f"{URL}/{aid}/image", headers=mint(["operator"]))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")


@pytest.mark.asyncio
async def test_a_frame_the_nvr_has_lost_is_a_502_not_a_broken_image(
        client, mint, monkeypatch, db):
    from backend.models import Annotation
    from backend.services import nvr_client
    from datetime import datetime, timezone

    aid = uuid.uuid4()
    db.add(Annotation(
        id=aid, camera_slug="gate-cam", class_key="forklift",
        captured_at=datetime.now(timezone.utc),
        bbox_x=1, bbox_y=1, bbox_w=2, bbox_h=2,
        frame_width=1920, frame_height=1080, image_path="/a.jpg"))

    async def _gone(annotation_id):
        return None
    monkeypatch.setattr(nvr_client, "fetch_annotation_image", _gone)

    r = await client.get(f"{URL}/{aid}/image", headers=mint(["supervisor"]))
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_an_unknown_annotation_is_a_404(client, mint):
    r = await client.get(f"{URL}/{uuid.uuid4()}/image",
                         headers=mint(["supervisor"]))
    assert r.status_code == 404
