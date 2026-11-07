"""Playback presence: when each confirmed track was in view, as bounded segments.

What the VMS draws under the playback timeline as person / vehicle bars
(camera-mgmt services/timeline.py). The rules that matter are all about TIME:
where a presence starts, when it is cut, and that it always ends — including
on a camera that has gone quiet and sends no more frames to age its tracks out.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.config import AppConfig                              # noqa: E402
from analytics.detector import Detection                            # noqa: E402
from analytics.pipeline import DetectionPipeline                    # noqa: E402
from analytics.presence import PresenceRecorder                     # noqa: E402

T0 = 1_790_000_000.0


def at(seg, key):
    return datetime.fromisoformat(seg[key]).timestamp() - T0


@pytest.fixture
def sent():
    return []


@pytest.fixture
def rec(sent):
    return PresenceRecorder(sent.extend, segment_seconds=120, idle_close_seconds=10)


def test_a_presence_runs_from_first_sighting_to_last(rec, sent):
    rec.observe("gate", "person", "t1", T0 + 2, first_seen=T0, confidence=0.6, label="person")
    rec.observe("gate", "person", "t1", T0 + 30, first_seen=T0, confidence=0.9)
    assert sent == [], "nothing is sent while the object is still in view"
    rec.close(["t1"])
    [seg] = sent
    assert seg["sensor_id"] == "gate" and seg["kind"] == "person"
    assert (at(seg, "started_at"), at(seg, "ended_at")) == (0, 30)
    assert seg["peak_confidence"] == 0.9 and seg["track_id"] == "t1"


def test_domains_map_to_kinds_and_others_are_ignored(rec, sent):
    rec.observe("gate", "vehicles", "v1", T0, first_seen=T0, confidence=0.8, label="truck")
    rec.observe("gate", "plate", "p1", T0, first_seen=T0, confidence=0.8)
    rec.close(["v1", "p1"])
    [seg] = sent
    assert seg["kind"] == "vehicle" and seg["label"] == "truck"


def test_a_single_sighting_is_never_zero_length(rec, sent):
    rec.observe("gate", "person", "t1", T0, first_seen=T0, confidence=0.7)
    rec.close(["t1"])
    assert at(sent[0], "ended_at") - at(sent[0], "started_at") >= 1.0


def test_a_long_stay_is_cut_into_seamless_segments(rec, sent):
    for s in range(0, 301, 10):
        rec.observe("lot", "vehicles", "v1", T0 + s, first_seen=T0, confidence=0.8)
    rec.close(["v1"])
    spans = [(at(s, "started_at"), at(s, "ended_at")) for s in sent]
    assert spans == [(0, 120), (120, 240), (240, 300)]
    assert all(e - s <= 120 for s, e in spans), "every segment within the VMS's bound"


def test_a_quiet_camera_still_closes_its_presences(rec, sent):
    """The tracker only ages tracks out when frames with detections arrive; a
    motion-gated camera may send none for hours after the last person left."""
    rec.observe("gate", "person", "t1", T0 + 5, first_seen=T0, confidence=0.9)
    assert rec.flush(now=T0 + 12) == 0
    assert rec.flush(now=T0 + 16) == 1
    assert (at(sent[0], "started_at"), at(sent[0], "ended_at")) == (0, 5)


def test_a_revived_identity_resumes_after_what_was_sent(rec, sent):
    """Re-association can bring a closed identity back. Restarting from its
    first sighting would re-send the stretch already sent under the same id,
    and the VMS would drop the new one as a retry."""
    rec.observe("gate", "person", "t1", T0 + 5, first_seen=T0, confidence=0.9)
    rec.flush(now=T0 + 20)
    rec.observe("gate", "person", "t1", T0 + 25, first_seen=T0, confidence=0.9)
    rec.observe("gate", "person", "t1", T0 + 40, first_seen=T0, confidence=0.9)
    rec.close(["t1"])
    spans = [(at(s, "started_at"), at(s, "ended_at")) for s in sent]
    assert spans == [(0, 5), (5, 40)]
    assert len({s["started_at"] for s in sent}) == 2, "distinct starts → distinct ids"


def test_shutdown_closes_what_is_open(rec, sent):
    rec.observe("gate", "person", "t1", T0 + 3, first_seen=T0, confidence=0.9)
    rec.stop()
    assert len(sent) == 1


def test_an_emit_that_raises_never_reaches_the_detection_worker():
    def boom(_):
        raise RuntimeError("sink gone")

    rec = PresenceRecorder(boom)
    rec.observe("gate", "person", "t1", T0, first_seen=T0, confidence=0.9)
    rec.close(["t1"])                                   # must not raise


# ── wired into the pipeline ──────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.seen, self.closed = [], []

    def observe(self, slug, domain, track_id, ts, first_seen, confidence, label=None):
        self.seen.append((slug, domain, ts))

    def close(self, ids):
        self.closed.extend(ids)


class FakeDetector:
    def __init__(self, dets):
        self.dets = dets

    def detect(self, frame):
        return list(self.dets)


@pytest.fixture
def cfg() -> AppConfig:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    c = AppConfig.from_yaml(os.path.join(here, "config.yaml"))
    return c


def _motion():
    return {"fraction": 0.01, "regions": [], "verdict": "regions", "reason": "test",
            "baseline_valid": True}


def test_the_pipeline_reports_confirmed_tracks_only(cfg):
    person = Detection(domain="person", xyxy=(10, 10, 90, 210), confidence=0.9, label="person")
    pres = _Recorder()
    p = DetectionPipeline(cfg, FakeDetector([person]), presence=pres)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    p._process("cam-a", frame, 1000.0, _motion())
    assert pres.seen == [], "a track seen once is tentative — not a presence yet"
    for t in (1000.5, 1001.0, 1001.5):
        p._process("cam-a", frame, t, _motion())
    assert pres.seen and all(s[:2] == ("cam-a", "person") for s in pres.seen)


def test_the_pipeline_closes_what_the_tracker_retires(cfg):
    person = Detection(domain="person", xyxy=(10, 10, 90, 210), confidence=0.9, label="person")
    far = Detection(domain="person", xyxy=(500, 200, 580, 400), confidence=0.9, label="person")
    pres = _Recorder()
    det = FakeDetector([person])
    p = DetectionPipeline(cfg, det, presence=pres)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for t in (1000.0, 1000.5, 1001.0, 1001.5):
        p._process("cam-a", frame, t, _motion())
    det.dets = [far]                       # the first person is gone
    for t in range(1002, 1030, 2):
        p._process("cam-a", frame, float(t), _motion())
    assert pres.closed, "a retired identity must end its presence"


def test_presence_config_is_clamped_to_the_vms_bound(monkeypatch, tmp_path):
    y = tmp_path / "c.yaml"
    y.write_text("presence:\n  segment_seconds: 3600\n")
    assert AppConfig.from_yaml(str(y)).presence.segment_seconds == 540.0
    monkeypatch.setenv("ANALYTICS_PRESENCE_ENABLED", "false")
    assert AppConfig.from_yaml(str(y)).presence.enabled is False
