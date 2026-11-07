"""Motion activity for the playback timeline: runs of motion, not incidents.

The incident log's length is the re-arm cooldown; the timeline needs when
motion actually happened. These pin what a run is (gaps bridged by the hold),
that noise which would never confirm is never drawn, that long runs are cut
into bounded segments, and that the per-sample hook is fed by BOTH sources
through apply_feature — the same single decision point as incidents.

Run: python3 -m pytest tests -q   (from services/motion)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.activity import MotionActivityRecorder                      # noqa: E402
from detector.config import AppConfig                                    # noqa: E402
from detector.engine import MotionEngine                                 # noqa: E402
from detector.sink import PresenceSink                                   # noqa: E402
from detector.worker import apply_feature                                # noqa: E402

T0 = 1_790_000_000.0


def at(seg, key):
    return round(datetime.fromisoformat(seg[key]).timestamp() - T0, 2)


@pytest.fixture
def sent():
    return []


@pytest.fixture
def rec(sent):
    # 0.5 s samples, 3 s window at 0.6 → 4 motion samples confirm a run.
    return MotionActivityRecorder(sent.extend, sample_interval=0.5, confirm_window=3.0,
                                  confirm_ratio=0.6, hold_seconds=2.0, segment_seconds=120)


def feed(rec, cam, pattern, start=0.0, step=0.5):
    """pattern: string of 'x' (motion) and '.' (still), one per sample."""
    for i, c in enumerate(pattern):
        rec.sample(cam, c == "x", T0 + start + i * step)


def test_a_confirmed_run_is_reported_when_it_ends(rec, sent):
    feed(rec, "gate", "xxxxxx" + "." * 6)
    [seg] = sent
    assert seg["kind"] == "motion" and seg["sensor_id"] == "gate"
    assert (at(seg, "started_at"), at(seg, "ended_at")) == (0.0, 3.0)


def test_noise_that_would_never_confirm_is_never_drawn(rec, sent):
    feed(rec, "gate", "xx" + "." * 10)
    assert sent == [] and rec.runs_discarded == 1


def test_short_pauses_are_bridged_by_the_hold(rec, sent):
    feed(rec, "gate", "xxx..xxx" + "." * 8)
    [seg] = sent
    assert (at(seg, "started_at"), at(seg, "ended_at")) == (0.0, 4.0)


def test_a_long_pause_makes_two_runs(rec, sent):
    feed(rec, "gate", "xxxx" + "." * 8 + "xxxx" + "." * 8)
    assert [(at(s, "started_at"), at(s, "ended_at")) for s in sent] == [(0.0, 2.0), (6.0, 8.0)]


def test_cameras_are_independent(rec, sent):
    for i in range(6):
        rec.sample("a", True, T0 + i * 0.5)
        rec.sample("b", i < 1, T0 + i * 0.5)
    rec.flush(now=T0 + 100)
    assert [s["sensor_id"] for s in sent] == ["a"]


def test_a_long_run_is_cut_into_seamless_bounded_segments(rec, sent):
    feed(rec, "lot", "x" * 601)                      # 300 s of motion
    rec.end("lot")
    spans = [(at(s, "started_at"), at(s, "ended_at")) for s in sent]
    assert spans[0][0] == 0.0 and spans[-1][1] == 300.5
    assert all(b - a <= 120.5 for a, b in spans)
    assert all(spans[i][1] >= spans[i + 1][0] for i in range(len(spans) - 1)), "no seams"


def test_a_camera_that_stops_sending_is_closed_by_the_timer(rec, sent):
    feed(rec, "gate", "xxxxx")
    assert rec.flush(now=T0 + 3.0) == 0
    assert rec.flush(now=T0 + 8.0) == 1


def test_removing_a_camera_ends_its_run(cfg, sent):
    eng = MotionEngine(cfg)
    eng.attach_subscriber(object())                  # broker mode: no capture thread
    eng.attach_activity(MotionActivityRecorder(
        sent.extend, sample_interval=0.5, confirm_window=3.0, confirm_ratio=0.6))
    eng.add_camera("gate", "rtsp://relay/gate")
    for i in range(6):
        eng.note_motion("gate", True, T0 + i * 0.5)
    assert sent == []
    assert eng.remove_camera("gate") is True
    assert len(sent) == 1, "a removed camera's run ends where it was last seen"


# ── the hook sits in the single decision point ───────────────────────────────

@pytest.fixture
def cfg():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return AppConfig.from_yaml(os.path.join(here, "config.yaml"))


def test_apply_feature_feeds_the_recorder_with_the_same_decision(cfg):
    eng = MotionEngine(cfg)
    seen = []

    class Spy:
        def sample(self, camera, motion, ts):
            seen.append((camera, motion, ts))

        def end(self, camera):
            pass

    eng.attach_activity(Spy())
    from detector.state import CameraState
    cam = CameraState(name="gate", rtsp_url="rtsp://x", sensitivity="medium", threshold=0.02)
    cam.update_maxlen(cfg.detection.confirm_window_seconds, cfg.detection.sample_interval_seconds)
    apply_feature(cam, 0.5, cfg, eng, ts=T0)
    apply_feature(cam, 0.001, cfg, eng, ts=T0 + 0.5)
    assert seen == [("gate", True, T0), ("gate", False, T0 + 0.5)]


def test_no_recorder_is_a_no_op(cfg):
    eng = MotionEngine(cfg)
    eng.note_motion("gate", True, T0)                # must not raise
    assert eng.activity_snapshot() == {"configured": False}


def test_activity_config_reads_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTION_EVENTS_URL", "http://api:8091/")
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    y = tmp_path / "c.yaml"
    y.write_text("activity:\n  segment_seconds: 5000\n")
    act = AppConfig.from_yaml(str(y)).activity
    assert act.url == "http://api:8091" and act.internal_api_key == "k"
    assert act.segment_seconds == 540.0, "clamped under the VMS's 10-minute bound"
    monkeypatch.setenv("MOTION_ACTIVITY_ENABLED", "false")
    assert AppConfig.from_yaml(str(y)).activity.enabled is False


# ── on the wire ──────────────────────────────────────────────────────────────

class _FakeVms:
    def __init__(self, status=200):
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(body)})
                raw = json.dumps({"inserted": 1, "duplicates": 0, "rejected": []}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_segments_are_posted_to_the_timeline_ingest_with_the_service_key():
    fake = _FakeVms()
    sink = PresenceSink(fake.url, "k-9")
    try:
        sink([{"sensor_id": "gate", "kind": "motion"}])
        sink.start()
        deadline = time.monotonic() + 5
        while not fake.requests and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        sink.stop()
        fake.server.shutdown()
    [req] = fake.requests
    assert req["path"] == "/api/playback/timeline/presence"
    assert req["headers"]["X-Internal-Key"] == "k-9"
    assert req["body"] == {"segments": [{"sensor_id": "gate", "kind": "motion"}]}


def test_a_permanent_refusal_is_dropped_not_retried():
    fake = _FakeVms(status=422)
    sink = PresenceSink(fake.url, "k")
    try:
        with pytest.raises(Exception):
            sink.deliver([{"sensor_id": "gate"}])
    finally:
        fake.server.shutdown()
