"""The heatmap sink: what it queues, what it posts, what it gives up on.

Artifacts are a few hundred KB each, so the queue is short and the OLDEST is
the one dropped — a stale overlay is worthless beside the one just made. The
HTTP call is stubbed here; nothing in these tests opens a socket.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.heatmap import Snapshot  # noqa: E402
from analytics.heatmap_runtime import ACTIVE, FROZEN, Session  # noqa: E402
from analytics.heatmap_sink import INTERNAL_HEADER, HeatmapSink, _Permanent  # noqa: E402

CAM = "cam1-9njq"


class Posted:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error is not None:
            raise self.error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"stored": true}'


def session(status=ACTIVE, **kw):
    s = Session(id="abc123", camera=CAM, started_at=1789800000.0, status=status, **kw)
    return s


def snap(jpeg=b"\xff\xd8jpegbytes"):
    return Snapshot(jpeg=jpeg, width=1280, height=720, captured_at=1789800060.0,
                    points=42, peak=17.5)


def sink(**kw) -> HeatmapSink:
    return HeatmapSink("http://api:8000", "secret-key", **kw)


class TestQueueing:
    def test_it_never_blocks_or_raises_on_the_worker(self):
        s = sink(queue_size=2)
        for _ in range(10):
            s("snapshot", session(), snap())
        assert s.queued == 10 and s.evicted == 8

    def test_the_oldest_artifact_is_the_one_dropped(self):
        s = sink(queue_size=1)
        s("snapshot", session(), snap(b"\xff\xd8old"))
        s("snapshot", session(), snap(b"\xff\xd8new"))
        _, parts = s._queue[0]
        assert parts[0][3] == b"\xff\xd8new"

    def test_a_lifecycle_change_carries_no_image(self):
        s = sink()
        s("closed", session(status="closed", close_reason="new session"), None)
        meta, parts = s._queue[0]
        assert parts == [] and meta["kind"] == "closed"
        assert meta["close_reason"] == "new session"

    def test_the_metadata_says_which_session_and_where_it_stands(self):
        s = sink()
        s("final", session(status=FROZEN, ended_at=1789800100.0, close_reason="operator"),
          snap())
        meta, _ = s._queue[0]
        assert meta["camera"] == CAM and meta["session_id"] == "abc123"
        assert meta["status"] == FROZEN and meta["kind"] == "final"
        assert meta["session_started_at"] == 1789800000.0
        assert meta["session_ended_at"] == 1789800100.0
        assert meta["captured_at"] == 1789800060.0
        assert (meta["width"], meta["height"]) == (1280, 720)


class TestDelivery:
    def test_it_posts_multipart_with_the_internal_key(self, monkeypatch):
        posted = Posted()
        monkeypatch.setattr("urllib.request.urlopen", posted)
        s = sink()
        s.deliver({"kind": "snapshot", "session_id": "abc123"}, b"\xff\xd8jpeg")
        [req] = posted.requests
        assert req.full_url == "http://api:8000/api/analytics/heatmaps"
        headers = {k.lower(): v for k, v in req.headers.items()}   # urllib capitalises
        assert headers[INTERNAL_HEADER.lower()] == "secret-key"
        assert headers["content-type"].startswith("multipart/form-data; boundary=")
        body = req.data
        assert b'name="meta"' in body and b'name="image"' in body
        assert b"\xff\xd8jpeg" in body
        assert json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])["session_id"] == "abc123"
        assert s.sent == 1 and s.bytes_sent == len(body)

    def test_metadata_only_messages_have_no_image_part(self, monkeypatch):
        posted = Posted()
        monkeypatch.setattr("urllib.request.urlopen", posted)
        sink().deliver({"kind": "closed"}, None)
        assert b'name="image"' not in posted.requests[0].data

    def test_a_refusal_that_repeating_cannot_fix_is_permanent(self, monkeypatch):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
        monkeypatch.setattr("urllib.request.urlopen", Posted(error=err))
        with pytest.raises(_Permanent):
            sink().deliver({"kind": "snapshot"}, b"x")

    @pytest.mark.parametrize("code", [408, 429, 500, 503])
    def test_a_refusal_worth_retrying_is_raised_as_is(self, monkeypatch, code):
        err = urllib.error.HTTPError("u", code, "nope", {}, None)
        monkeypatch.setattr("urllib.request.urlopen", Posted(error=err))
        with pytest.raises(urllib.error.HTTPError):
            sink().deliver({"kind": "snapshot"}, b"x")

    def test_the_snapshot_reports_what_it_has_delivered(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", Posted())
        s = sink()
        s.deliver({"kind": "snapshot"}, b"\xff\xd8jpeg")
        snapshot = s.snapshot()
        assert snapshot["sent"] == 1 and snapshot["queue_depth"] == 0
        assert snapshot["url"].endswith("/api/analytics/heatmaps")


class TestHeatBlocks:
    def test_a_block_carries_its_array_and_maybe_a_frame(self):
        s = sink()
        s.chunk({"camera": CAM, "period_start": 1.0, "period_end": 2.0}, b"npz", b"\xff\xd8bg")
        meta, parts = s._queue[0]
        assert meta["camera"] == CAM
        assert [p[0] for p in parts] == ["heat", "background"]
        s.chunk({"camera": CAM}, b"npz", None)
        assert [p[0] for p in s._queue[1][1]] == ["heat"]
