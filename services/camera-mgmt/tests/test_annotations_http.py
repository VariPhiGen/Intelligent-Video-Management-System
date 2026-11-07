"""Creating an annotation: validate first, write the file, then the row.

THE ORDERING IS THE DESIGN. A row with no file is a ghost the UI lists and
cannot display; a file with no row is recoverable from its sidecar. So the file
lands first, and a failed row write must NOT delete it.

Class validation runs before the NVR is touched at all — the NVR does not own
the taxonomy and would faithfully record a class that does not exist.

WHAT IS NOT HERE: what actually reached the `annotations` table. The conftest
`db` is a StubSession; see test_annotations_db.py.
"""
from __future__ import annotations

import pytest

URL = "/api/annotations"


def _form(**over):
    body = {"camera": "gate-cam", "timestamp": "2026-09-25T10:00:00+00:00",
            "class_key": "forklift",
            "bbox_x": "10", "bbox_y": "20", "bbox_w": "30", "bbox_h": "40",
            "frame_width": "1920", "frame_height": "1080"}
    body.update(over)
    return body


def _files():
    return {"image": ("f.jpg", b"\xff\xd8\xff\xdb" + b"0" * 64, "image/jpeg")}


@pytest.fixture
def seeded_class(db):
    """A class the router's `db.get` will actually find."""
    from backend.models import LabelClass

    def _seed(key="forklift", active=True):
        db.add(LabelClass(key=key, label=key.title(), color="#ff8800",
                          sort_order=0, active=active))
    return _seed


@pytest.fixture
def spy_nvr(monkeypatch):
    """Records whether the NVR was reached, and with what."""
    from backend.services import nvr_client

    calls: list[dict] = []

    async def _save(form, files):
        calls.append(form)
        return {
            "id": "0" * 32, "camera": form["camera"],
            "class_key": form["class_key"], "timestamp": form["timestamp"],
            "timestamp_epoch": 1790000000.0,
            "bbox": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0},
            "frame": {"width": 1920, "height": 1080},
            "image_path": "/data/nvr/annotations/gate-cam/1790000000_" + "0" * 32 + ".jpg",
        }

    monkeypatch.setattr(nvr_client, "save_annotation", _save)
    return calls


@pytest.mark.asyncio
async def test_a_viewer_cannot_annotate(client, mint, seeded_class, spy_nvr):
    seeded_class()
    r = await client.post(URL, data=_form(), files=_files(),
                          headers=mint(["viewer"]))
    assert r.status_code == 403
    assert spy_nvr == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "supervisor", "admin"])
async def test_anyone_who_may_use_playback_may_annotate(
        client, mint, seeded_class, spy_nvr, role):
    """Drawing a box is an operator act, gated exactly as the Playback page it
    is drawn from — not as fleet administration."""
    seeded_class()
    r = await client.post(URL, data=_form(), files=_files(),
                          headers=mint([role]))
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_an_unknown_class_is_refused_before_the_nvr_is_called(
        client, mint, spy_nvr):
    r = await client.post(URL, data=_form(class_key="nosuchclass"),
                          files=_files(), headers=mint(["operator"]))
    assert r.status_code == 422
    assert spy_nvr == [], "the NVR must not be asked to store an invalid class"


@pytest.mark.asyncio
async def test_a_retired_class_is_refused_before_the_nvr_is_called(
        client, mint, seeded_class, spy_nvr):
    """Retirement takes a class out of circulation. If it still accepted new
    boxes, retiring would mean nothing."""
    seeded_class("old", active=False)
    r = await client.post(URL, data=_form(class_key="old"), files=_files(),
                          headers=mint(["operator"]))
    assert r.status_code == 422
    assert spy_nvr == []


@pytest.mark.asyncio
async def test_the_response_echoes_the_class_and_an_expiry(
        client, mint, seeded_class, spy_nvr):
    seeded_class()
    r = await client.post(URL, data=_form(), files=_files(),
                          headers=mint(["operator"]))
    body = r.json()
    assert body["class_key"] == "forklift"
    assert body["camera_slug"] == "gate-cam"
    # Not dataset-attached, so it is on a clock.
    assert body["expires_at"] is not None


@pytest.mark.asyncio
async def test_a_dataset_attached_annotation_has_no_expiry(
        client, mint, seeded_class, spy_nvr):
    """It has a named owner and a deletable parent, so its removal is the
    dataset's business rather than a sweep's."""
    seeded_class()
    r = await client.post(URL, data=_form(dataset_id="7"), files=_files(),
                          headers=mint(["operator"]))
    assert r.status_code == 201
    assert r.json()["expires_at"] is None


@pytest.mark.asyncio
async def test_an_unreachable_nvr_is_a_502_and_writes_nothing(
        client, mint, seeded_class, monkeypatch, db):
    from backend.services import nvr_client

    async def _down(form, files):
        return None
    monkeypatch.setattr(nvr_client, "save_annotation", _down)

    seeded_class()
    before = len(db.added)
    r = await client.post(URL, data=_form(), files=_files(),
                          headers=mint(["operator"]))
    assert r.status_code == 502
    assert len(db.added) == before, "no row for a frame that was never stored"


@pytest.mark.asyncio
async def test_the_class_is_required(client, mint, seeded_class, spy_nvr):
    seeded_class()
    body = _form()
    del body["class_key"]
    r = await client.post(URL, data=body, files=_files(),
                          headers=mint(["operator"]))
    assert r.status_code == 422
    assert spy_nvr == []


@pytest.mark.asyncio
async def test_managing_the_pile_is_not_an_operator_act(client, mint):
    """Deleting from the training pile is Data Collection's job, which
    operators do not hold by default."""
    import uuid
    r = await client.delete(f"{URL}/{uuid.uuid4()}", headers=mint(["operator"]))
    assert r.status_code == 403


# ── Relabel and keep ────────────────────────────────────────────────────────
# Two authorities in one endpoint: fixing your own misclick rides on the
# capability that let you draw the box; deciding what the appliance keeps
# does not.


@pytest.fixture
def an_annotation(db):
    """An annotation already in this session's identity map."""
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone
    from backend.models import Annotation

    def _seed(**kw):
        aid = kw.pop("id", _uuid.uuid4())
        now = datetime.now(timezone.utc)
        db.add(Annotation(
            id=aid, camera_slug="gate-cam", class_key="forklift",
            captured_at=now,
            bbox_x=10, bbox_y=20, bbox_w=30, bbox_h=40,
            frame_width=1920, frame_height=1080, image_path="/a.jpg",
            expires_at=kw.pop("expires_at", now + timedelta(days=2)),
            created_at=now, **kw))
        return aid
    return _seed


@pytest.mark.asyncio
async def test_an_operator_may_fix_their_own_misclick(
        client, mint, seeded_class, an_annotation):
    """The frame was captured at pointer-down and the footage may have aged
    out, so if relabel were harder than drawing, the only remedy for a wrong
    class would be destroying the sample."""
    seeded_class("pallet")
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}", json={"class_key": "pallet"},
                           headers=mint(["operator"]))
    assert r.status_code == 200, r.text
    assert r.json()["class_key"] == "pallet"


@pytest.mark.asyncio
async def test_relabelling_to_an_unknown_class_is_refused(
        client, mint, an_annotation):
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}", json={"class_key": "nosuchclass"},
                           headers=mint(["operator"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_relabelling_to_a_retired_class_is_refused(
        client, mint, seeded_class, an_annotation):
    seeded_class("old", active=False)
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}", json={"class_key": "old"},
                           headers=mint(["operator"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_viewer_may_not_relabel(client, mint, seeded_class,
                                        an_annotation):
    seeded_class("pallet")
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}", json={"class_key": "pallet"},
                           headers=mint(["viewer"]))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_keeping_needs_more_than_drawing_does(client, mint,
                                                    an_annotation):
    """An operator can fix a label. Deciding what the appliance retains for
    ever is what the posture report holds someone accountable for."""
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}",
                           json={"keep": True, "kept_reason": "training set"},
                           headers=mint(["operator"]))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_supervisor_can_keep_one_with_a_reason(client, mint,
                                                       an_annotation):
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}",
                           json={"keep": True, "kept_reason": "forklift set"},
                           headers=mint(["supervisor"]))
    assert r.status_code == 200, r.text
    assert r.json()["expires_at"] is None
    assert r.json()["kept_reason"] == "forklift set"


@pytest.mark.asyncio
async def test_releasing_puts_it_back_on_the_clock(client, mint,
                                                   an_annotation):
    """Measured from CAPTURE, not from now — the deadline belongs to when the
    frame was taken, so release must not silently extend it."""
    from datetime import datetime, timezone
    aid = an_annotation(expires_at=None)
    r = await client.patch(f"{URL}/{aid}", json={"keep": False},
                           headers=mint(["supervisor"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_at"] is not None
    assert body["kept_reason"] is None
    from backend.config import settings
    captured = datetime.fromisoformat(body["captured_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert (expires - captured).total_seconds() == \
        settings.annotation_ttl_minutes * 60


@pytest.mark.asyncio
async def test_an_empty_patch_is_refused(client, mint, an_annotation):
    aid = an_annotation()
    r = await client.patch(f"{URL}/{aid}", json={}, headers=mint(["supervisor"]))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_the_key_of_an_unknown_annotation_is_a_404(client, mint):
    import uuid as _uuid
    r = await client.patch(f"{URL}/{_uuid.uuid4()}",
                           json={"class_key": "forklift"},
                           headers=mint(["supervisor"]))
    assert r.status_code == 404
