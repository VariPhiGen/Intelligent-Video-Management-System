"""Delivering activity events to the VMS ingest.

Against a real local HTTP server, because what matters is on the wire: the
path, the service key, the body camera-mgmt's ingest parses — and what happens
when the VMS says no, or is not there.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.activities.base import FULL_FRAME, ActivityEvent       # noqa: E402
from analytics.activities.events import ActivityEventSink, _Permanent  # noqa: E402


class FakeVms:
    """A stand-in for camera-mgmt's POST /api/analytics/events."""

    def __init__(self, replies):
        self.replies = list(replies)          # (status, body) per request; last repeats
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(body)})
                status, reply = (fake.replies.pop(0) if len(fake.replies) > 1
                                 else fake.replies[0])
                raw = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def vms():
    made = []

    def make(*replies):
        f = FakeVms(replies or [(200, {"inserted": 1, "closed": 0, "duplicates": 0, "rejected": []})])
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


def event(**over):
    base = dict(camera="cam2-o8iu", activity="restricted_zone_entry", started_at=1_789_000_000.0,
                zone=FULL_FRAME, confidence=0.9, track_id="t1", object_class="person")
    base.update(over)
    return ActivityEvent(**base)


def wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_events_are_posted_to_the_vms_ingest_with_the_service_key(vms):
    fake = vms()
    sink = ActivityEventSink(fake.url, "k-123")
    sink.deliver([event().to_ingest()])
    [req] = fake.requests
    assert req["path"] == "/api/analytics/events"
    assert req["headers"]["X-Internal-Key"] == "k-123"
    [sent] = req["body"]["events"]
    assert sent["sensor_id"] == "cam2-o8iu" and sent["source"] == "cpu"
    assert sink.inserted == 1 and sink.sent == 1


def test_the_sender_thread_delivers_what_the_worker_hands_over(vms):
    fake = vms()
    sink = ActivityEventSink(fake.url, "k")
    sink.start()
    try:
        sink([event(), event(track_id="t2")])
        assert wait_for(lambda: sink.sent == 2)
        assert sum(len(r["body"]["events"]) for r in fake.requests) == 2
    finally:
        sink.stop()


def test_a_refusal_that_cannot_succeed_is_not_retried(vms):
    fake = vms((422, {"detail": "bad"}))
    sink = ActivityEventSink(fake.url, "k")
    with pytest.raises(_Permanent):
        sink.deliver([event().to_ingest()])
    sink.start()
    try:
        sink([event()])
        assert wait_for(lambda: sink.dropped_http == 1)
        time.sleep(1.2)
        assert len(fake.requests) == 2          # the direct call, and one attempt
    finally:
        sink.stop()


def test_a_transient_failure_is_retried_until_it_lands(vms):
    fake = vms((503, {"detail": "starting"}),
               (200, {"inserted": 1, "closed": 0, "duplicates": 0, "rejected": []}))
    sink = ActivityEventSink(fake.url, "k")
    sink.start()
    try:
        ev = event()
        sink([ev])
        assert wait_for(lambda: sink.inserted == 1)
        assert sink.failures == 1
        # The retry is the SAME event, so the VMS can collapse a double delivery.
        ids = [r["body"]["events"][0]["id"] for r in fake.requests]
        assert ids == [ev.id, ev.id]
    finally:
        sink.stop()


def test_refusals_by_the_vms_are_counted_by_reason(vms):
    fake = vms((200, {"inserted": 0, "closed": 0, "duplicates": 0,
                      "rejected": [{"index": 0, "reason": "activity_not_configured"}]}))
    sink = ActivityEventSink(fake.url, "k")
    sink.deliver([event().to_ingest()])
    assert sink.rejected == {"activity_not_configured": 1}


def test_the_queue_is_bounded_and_evicts_the_oldest():
    sink = ActivityEventSink("http://127.0.0.1:9", "k", queue_size=2)
    first, second, third = event(track_id="a"), event(track_id="b"), event(track_id="c")
    sink([first, second, third])
    assert sink.evicted == 1
    assert [e["track_id"] for e in sink._queue] == ["b", "c"]
