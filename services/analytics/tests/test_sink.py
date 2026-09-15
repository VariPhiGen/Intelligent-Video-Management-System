"""Handing observations to Smart Search without coupling to it.

The sink is the seam made concrete, and its job is as much about what it
REFUSES to do as what it does: it must never block the detection worker, never
raise into it, and never quietly lose an observation without saying so.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.sink import ObservationSink                          # noqa: E402


def crop(h: int = 40, w: int = 20) -> np.ndarray:
    return np.full((h, w, 3), 128, dtype=np.uint8)


def obs(**kw) -> dict:
    d = {"camera": "cam-a", "ts": 1000.0, "domain": "person",
         "confidence": 0.9, "label": "person", "plate": None,
         "plate_confidence": None, "bbox": [0.1, 0.1, 0.2, 0.4],
         "tracker_id": "cam-a:1", "reason": "first_sight"}
    d.update(kw)
    return d


# ── the transport contract ───────────────────────────────────────────────────
def test_the_crop_is_encoded_losslessly():
    """PNG, NOT JPEG, and this is a correctness property rather than taste.

    The receiver embeds the crop BEFORE writing it to disk, so a lossy hop
    here would change the vector and therefore the appearance-dedup decision —
    the one thing that must stay identical while this path and the in-process
    one run side by side.
    """
    import cv2

    s = ObservationSink("http://unused")
    original = np.random.default_rng(3).integers(0, 255, (40, 20, 3),
                                                 dtype=np.uint8)
    encoded = s._encode(original)
    assert encoded[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    back = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(back, original), "the crop did not survive the wire"


def test_the_url_points_at_the_observations_endpoint():
    s = ObservationSink("http://smartsearch:8013/")
    assert s._url == "http://smartsearch:8013/observations"


# ── never block, never raise ─────────────────────────────────────────────────
def test_submitting_never_blocks_the_detection_worker():
    """The obvious implementation POSTs from the worker, which couples
    detection latency to the far service: one slow response and frames back up
    behind an HTTP call that has nothing to do with detecting."""
    s = ObservationSink("http://unused", queue_size=4)
    start = time.time()
    for _ in range(50):                       # far more than the queue holds
        s("cam-a", crop(), obs())
    assert time.time() - start < 1.0, "submitting blocked"
    # Every submission was accepted; the ones that had to make room show up as
    # evictions, which is backpressure working rather than loss.
    assert s.queued == 50
    assert s.evicted == 50 - 4
    assert s.dropped == 0, "a plain submission was refused outright"


def test_a_full_queue_drops_the_oldest():
    """Same discipline as the frame queue, for the same reason: if the
    receiver is behind, the observation that just happened is worth more than
    one from a minute ago."""
    s = ObservationSink("http://unused", queue_size=3)
    for i in range(3):
        s("cam-a", crop(), obs(ts=1000.0 + i))
    s("cam-a", crop(), obs(ts=9999.0))
    assert s.evicted == 1 and s.dropped == 0
    stamps = [s._queue.get_nowait()[1]["ts"] for _ in range(s._queue.qsize())]
    assert 9999.0 in stamps, "the newest observation was the one dropped"
    assert 1000.0 not in stamps


def test_a_dead_receiver_is_counted_not_raised():
    """A down index must cost rows, not the detection pipeline."""
    s = ObservationSink("http://127.0.0.1:1/", timeout=0.5)
    s.start()
    try:
        s("cam-a", crop(), obs())
        deadline = time.time() + 10
        while s.failed == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert s.failed == 1, "a failed delivery was not counted"
        assert s.last_error, "a failure with no explanation is not reportable"
    finally:
        s.stop()


# ── what /health has to say ──────────────────────────────────────────────────
def test_the_snapshot_distinguishes_loss_from_dedup():
    """A dropped observation and one the receiver deduplicated are opposite
    things — the first is a fault and the second is the saving working. A
    single 'not stored' number would hide the fault inside the feature."""
    s = ObservationSink("http://unused")
    snap = s.snapshot()
    for key in ("queued", "evicted", "dropped", "sent", "failed", "rows_written",
                "deduped_by_receiver", "queue_depth", "last_error"):
        assert key in snap, f"/health cannot report {key}"


def test_it_survives_a_receiver_that_answers_nonsense():
    """A 200 with a body the sink cannot parse must not kill the sender
    thread — it is the only path observations leave by."""
    import http.server
    import json as _json

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b"not json at all"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                            # noqa: A003
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    s = ObservationSink(f"http://127.0.0.1:{port}", timeout=5.0)
    s.start()
    try:
        s("cam-a", crop(), obs())
        deadline = time.time() + 10
        while s.failed == 0 and s.sent == 0 and time.time() < deadline:
            time.sleep(0.05)
        # Either outcome is acceptable; the thread staying alive is not
        # optional, so a second observation must still be handled.
        s("cam-a", crop(), obs())
        time.sleep(0.5)
        assert s._thread is not None and s._thread.is_alive(), \
            "the sender thread died on a malformed response"
    finally:
        s.stop()
        srv.shutdown()


# ── the whole frame, for the feed ────────────────────────────────────────────
def _capturing_receiver():
    import http.server

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            seen.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            out = b'{"rows_written": 1, "deduped": false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):                            # noqa: A003
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def _deliver(frame):
    srv, seen = _capturing_receiver()
    s = ObservationSink(f"http://127.0.0.1:{srv.server_address[1]}", timeout=5.0)
    s.start()
    try:
        s("cam-a", crop(), obs(), frame=frame)
        deadline = time.time() + 10
        while s.sent == 0 and s.failed == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert s.sent == 1, s.last_error
        return s, next(b for b in seen if b'name="meta"' in b)
    finally:
        s.stop()
        srv.shutdown()


def test_the_whole_frame_rides_along_as_its_own_part():
    """Beside the crop, never instead of it: the PNG crop is still what gets
    embedded, and the JPEG frame is only ever looked at."""
    jpeg = b"\xff\xd8\xff\xe0 a whole frame \xff\xd9"
    s, body = _deliver(jpeg)
    assert b'name="crop"' in body and b"image/png" in body
    assert b'name="frame"' in body and b"image/jpeg" in body
    assert jpeg in body, "the frame bytes did not survive the wire"
    assert s.frames_sent == 1


def test_no_frame_means_no_frame_part():
    s, body = _deliver(None)
    assert b'name="frame"' not in body
    assert s.frames_sent == 0


# ── the heartbeat ────────────────────────────────────────────────────────────
def test_it_announces_itself_even_with_nothing_to_send():
    """THE WHOLE POINT. A camera watching an empty yard produces no
    observations all night and is perfectly healthy; a producer that was never
    started produces none and is a total outage. From the receiving side those
    are identical unless something arrives regardless of traffic.

    This is not hypothetical: the stack was once brought up without the
    analytics profile and Smart Search reported healthy for eight hours while
    the index did not grow by a single row.
    """
    import http.server
    import json as _json

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):                                    # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            seen.append((self.path, body))
            out = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):                            # noqa: A003
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    s = ObservationSink(f"http://127.0.0.1:{port}", timeout=5.0)
    s.HEARTBEAT_SECONDS = 0.2          # so the test does not wait 30 s
    s.set_stats_source(lambda: {"cameras": 5, "observations_produced": 0})
    s.start()
    try:
        deadline = time.time() + 10
        while s.beats_sent == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert s.beats_sent >= 1, "no heartbeat with an idle queue"
        path, body = next((p, b) for p, b in seen if "heartbeat" in p)
        assert path == "/producers/analytics/heartbeat"
        # It carries something worth reading, not a bare ping.
        assert _json.loads(body)["cameras"] == 5
    finally:
        s.stop()
        srv.shutdown()


def test_a_failed_heartbeat_is_counted_separately_from_a_failed_delivery():
    """They mean different things: a failed delivery lost one observation, a
    failed heartbeat means the receiver is about to report this producer gone.
    One number for both would hide the second inside the first."""
    s = ObservationSink("http://127.0.0.1:1/", timeout=0.5)
    s.HEARTBEAT_SECONDS = 0.1
    s.start()
    try:
        deadline = time.time() + 10
        while s.beats_failed == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert s.beats_failed >= 1
        assert s.failed == 0, "a heartbeat failure was counted as a lost observation"
        assert "heartbeats_failed" in s.snapshot()
    finally:
        s.stop()
