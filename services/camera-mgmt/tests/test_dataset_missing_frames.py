"""A frame the manifest lists but the disk no longer holds must be REPORTED.

THE DEFECT. Both export destinations skip a frame whose file is gone from the
dataset directory, and both used to do it quietly: Label Studio logged a
warning and returned the task count of what it did send, S3 folded it into
`skipped` beside real PUT failures. So a 1000-frame dataset whose directory
held 77 files imported as "77 tasks in project 3" — a clean success, with
nothing on screen to say 923 frames never left the appliance.

These tests pin the report, not the transport: the network half is replaced
with a fake that records what it was sent.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import labelstudio_client as ls  # noqa: E402
from backend.services import s3_upload as s3  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 50


def _dataset(tmp_path: Path, total: int, on_disk: int) -> list[dict]:
    """A manifest of `total` frames of which only the first `on_disk` exist."""
    frames = []
    for i in range(total):
        name = f"frame_{i:05d}_{1_760_000_000_000 + i}.jpg"
        if i < on_disk:
            (tmp_path / name).write_bytes(JPEG)
        frames.append({"filename": name, "timestamp": "2026-09-28T10:00:00Z",
                       "sha256": "x", "bytes": len(JPEG)})
    return frames


# ── Label Studio ──────────────────────────────────────────────────────────────

@pytest.fixture
def fake_ls(monkeypatch):
    """Label Studio that accepts every batch and says how many tasks it made."""
    sent: list[str] = []
    monkeypatch.setattr(ls.settings, "labelstudio_url", "http://ls.invalid")
    monkeypatch.setattr(ls.settings, "labelstudio_api_key", "k")

    class Resp:
        status_code = 201

        def __init__(self, n):
            self._n = n

        def json(self):
            return {"task_count": self._n}

    async def post_batch(client, project_id, files):
        sent.extend(name for name, _ in files)
        return Resp(len(files))

    monkeypatch.setattr(ls, "_post_batch", post_batch)
    return sent


def test_a_complete_dataset_reports_nothing_missing(tmp_path, fake_ls):
    frames = _dataset(tmp_path, total=60, on_disk=60)
    out = asyncio.run(ls.import_frames(3, str(tmp_path), frames))
    assert out["tasks"] == 60 and out["sent"] == 60
    assert out["missing_on_disk"] == 0 and out["missing_examples"] == []


def test_frames_missing_from_disk_are_counted_not_hidden(tmp_path, fake_ls):
    # The reported shape: 1000 listed, 77 on disk.
    frames = _dataset(tmp_path, total=1000, on_disk=77)
    out = asyncio.run(ls.import_frames(3, str(tmp_path), frames))
    assert out["sent"] == 77 and out["tasks"] == 77
    assert out["missing_on_disk"] == 923
    assert len(fake_ls) == 77


def test_it_names_some_missing_frames_so_they_can_be_looked_for(tmp_path, fake_ls):
    frames = _dataset(tmp_path, total=20, on_disk=5)
    out = asyncio.run(ls.import_frames(3, str(tmp_path), frames))
    assert out["missing_examples"] == [f["filename"] for f in frames[5:10]]


def test_nothing_on_disk_is_still_a_refusal(tmp_path, fake_ls):
    frames = _dataset(tmp_path, total=10, on_disk=0)
    with pytest.raises(ls.LabelStudioError, match="Re-collect"):
        asyncio.run(ls.import_frames(3, str(tmp_path), frames))


# ── S3 ────────────────────────────────────────────────────────────────────────

class _FakeS3:
    def __init__(self, fail: set[str] = frozenset()):
        self.keys: list[str] = []
        self._fail = fail

    def upload_file(self, path, bucket, key, ExtraArgs=None):  # noqa: N803 — boto3's name
        if key in self._fail:
            raise RuntimeError("An error occurred (SlowDown) when calling PutObject")
        self.keys.append(key)


def _run_s3(monkeypatch, tmp_path, frames, client):
    monkeypatch.setattr(s3, "_client", lambda: client)
    return asyncio.run(s3.upload_frames("my-bucket", "ds/", str(tmp_path), frames))


def test_s3_separates_missing_files_from_failed_uploads(monkeypatch, tmp_path):
    frames = _dataset(tmp_path, total=10, on_disk=6)
    fail = {f"ds/{frames[0]['filename']}"}
    out = _run_s3(monkeypatch, tmp_path, frames, _FakeS3(fail))
    assert out["uploaded"] == 5
    assert out["skipped"] == 1            # the PUT that failed
    assert out["missing_on_disk"] == 4    # never attempted: nothing to send
    assert "SlowDown" in (out["error"] or "")


def test_s3_with_nothing_on_disk_says_to_recollect(monkeypatch, tmp_path):
    frames = _dataset(tmp_path, total=5, on_disk=0)
    with pytest.raises(s3.S3UploadError, match="Re-collect"):
        _run_s3(monkeypatch, tmp_path, frames, _FakeS3())


# ── the multipart shape itself ────────────────────────────────────────────────

def test_every_file_in_a_batch_has_its_own_field_name(monkeypatch):
    """Label Studio reads the upload by FIELD NAME. Twenty-five parts all named
    "file" collapsed to the last one: a 1901-frame dataset became 77 tasks, one
    per request, each the batch's final frame. Unique names are the fix, and
    this is the only test that can see the request body."""
    monkeypatch.setattr(ls.settings, "labelstudio_url", "http://ls.invalid")
    monkeypatch.setattr(ls.settings, "labelstudio_api_key", "k")
    monkeypatch.setattr(ls, "_auth", {"scheme": ls._SCHEME_BEARER, "token": "k",
                                      "renewable": False})
    seen = {}

    class Client:
        async def post(self, url, headers=None, files=None):
            seen["fields"] = [field for field, _ in files]
            class R:
                status_code = 201
            return R()

    batch = [(f"frame_{i:05d}_1760000000000.jpg", JPEG) for i in range(25)]
    asyncio.run(ls._post_batch(Client(), 87, batch))
    assert len(seen["fields"]) == 25
    assert len(set(seen["fields"])) == 25, "field names collide — LS keeps only the last"
