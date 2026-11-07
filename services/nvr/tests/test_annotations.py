"""The annotation contract: a class goes in, the sidecar carries it out.

THE SIDECAR IS A RECOVERY LOG, not a convenience. camera-mgmt writes the file
before the index row, so a row write that fails leaves a complete record on
disk — and these tests pin that every field the index needs is actually in it.
Drop a field from the sidecar and an orphan becomes unrecoverable.

Run: python -m pytest services/nvr/tests -q
"""
import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import server                                          # noqa: E402
from storage.index import Segment, SegmentIndex                 # noqa: E402

CAMERA = "gate-cam"
TS = "2026-09-25T10:00:00+00:00"


class _Upload:
    """The two attributes save_annotation actually touches on an UploadFile."""

    def __init__(self, data=b"\xff\xd8\xff\xdb" + b"0" * 64,
                 content_type="image/jpeg", filename="annotation.jpg"):
        self._data = data
        self.content_type = content_type
        self.filename = filename

    async def read(self):
        return self._data


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the module's globals at a throwaway volume with one known camera."""
    annotations = tmp_path / "annotations"
    annotations.mkdir()
    index = SegmentIndex(str(tmp_path / "segments.db"))
    index.add_segment(Segment(camera=CAMERA, start_epoch=0.0, duration=60.0,
                              filepath=str(tmp_path / f"{CAMERA}_0.ts"),
                              file_size=1000))
    monkeypatch.setattr(server, "_annotations_path", annotations)
    monkeypatch.setattr(server, "_index", index)
    monkeypatch.setattr(server, "_engine", None)
    return annotations


def _save(class_key="forklift", **kw):
    kwargs = dict(camera=CAMERA, timestamp=TS, class_key=class_key,
                  bbox_x=10.0, bbox_y=20.0, bbox_w=30.0, bbox_h=40.0,
                  frame_width=1920, frame_height=1080, image=_Upload())
    kwargs.update(kw)
    return asyncio.run(server.save_annotation(**kwargs))


def test_the_class_reaches_the_response_and_the_sidecar(wired):
    meta = _save("forklift")
    assert meta["class_key"] == "forklift"
    sidecar = Path(meta["image_path"]).with_suffix(".json")
    assert json.loads(sidecar.read_text())["class_key"] == "forklift"


def test_the_sidecar_carries_every_field_the_index_needs(wired):
    """If any of these stops being written, an orphan file stops being
    recoverable and the file-before-row ordering loses its safety net."""
    meta = _save("forklift")
    written = json.loads(Path(meta["image_path"]).with_suffix(".json").read_text())
    for field in ("id", "camera", "class_key", "timestamp_epoch",
                  "bbox", "frame", "image_path"):
        assert field in written, f"sidecar is missing {field}"
    assert set(written["bbox"]) == {"x", "y", "width", "height"}
    assert set(written["frame"]) == {"width", "height"}


def test_delete_removes_the_image_and_the_sidecar(wired):
    meta = _save("forklift")
    image = Path(meta["image_path"])
    sidecar = image.with_suffix(".json")
    assert image.exists() and sidecar.exists()

    out = asyncio.run(server.delete_annotation(meta["id"]))
    assert out == {"deleted": True}
    assert not image.exists() and not sidecar.exists()


def test_deleting_something_already_gone_is_not_an_error(wired):
    """camera-mgmt drives deletion and retries a failed pass. A retry that
    404s would strand the very row the sweep is trying to clear."""
    out = asyncio.run(server.delete_annotation(uuid.uuid4().hex))
    assert out == {"deleted": False}


def test_a_malformed_id_is_refused(wired):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.delete_annotation("../../etc/passwd"))
    assert exc.value.status_code == 422


def test_list_returns_the_sidecars(wired):
    _save("forklift")
    _save("pallet")
    out = asyncio.run(server.list_annotations(camera=CAMERA))
    assert sorted(a["class_key"] for a in out["annotations"]) == ["forklift",
                                                                 "pallet"]


def test_list_can_filter_by_capture_time(wired):
    _save("forklift")
    out = asyncio.run(server.list_annotations(camera=CAMERA, since=4102444800.0))
    assert out["annotations"] == []


def test_an_unreadable_sidecar_is_skipped_not_fatal(wired):
    """One corrupt file must not stop the adoption pass reading the rest."""
    meta = _save("forklift")
    (wired / CAMERA / "9999_deadbeef.json").write_text("{not json")
    out = asyncio.run(server.list_annotations(camera=CAMERA))
    assert [a["id"] for a in out["annotations"]] == [meta["id"]]


def test_the_image_is_served_back(wired):
    meta = _save("forklift")
    resp = asyncio.run(server.annotation_image(meta["id"]))
    assert resp.media_type == "image/jpeg"
    assert resp.body.startswith(b"\xff\xd8")
