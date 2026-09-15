"""The CPU activity path, end to end inside this service.

    broker frame (5 FPS, EVERY frame) → activity YOLO → tracker → activities

These drive the REAL ActivityPipeline and the REAL tracker with a detector that
returns what it is told, and pin the claims the path rests on:

  * no motion gate — an identical, motionless frame reaches the detector every
    time, so a parked car and a group standing still keep their tracks and
    their timers;
  * the broker hands the activity path every frame of an activity camera while
    Smart Search's delivery for the same notice stays gated exactly as before;
  * the activity path has its own detector instance (Option 1), loaded only
    when a camera has a running activity;
  * Smart Search's DetectionPipeline knows nothing about activities;
  * nothing in the path needs DeepStream.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import ast
import inspect
import json
import mmap
import os
import pathlib
import struct
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import engine as engine_mod                            # noqa: E402
from analytics import frame_source                                    # noqa: E402
from analytics.activities.engine import ActivityEngine                # noqa: E402
from analytics.activity_pipeline import ActivityPipeline              # noqa: E402
from analytics.config import AppConfig                                # noqa: E402
from analytics.detector import Detection                              # noqa: E402
from analytics.pipeline import DetectionPipeline                      # noqa: E402

CAM = "cam3-2qyj"
W, H = 640, 480
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LEFT_HALF = {"kind": "zone", "name": "Left half", "color": "#4fd1c5",
             "points": [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]], "direction": None}


class FakeDetector:
    def __init__(self, detections=None) -> None:
        self.detections = list(detections or [])
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return list(self.detections)


@pytest.fixture
def cfg() -> AppConfig:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return AppConfig.from_yaml(os.path.join(here, "config.yaml"))


def person(x1=20, y1=150, x2=60, y2=400, conf=0.9) -> Detection:
    return Detection(domain="person", xyxy=(x1, y1, x2, y2), confidence=conf, label="person")


def car(x1=60, y1=300, x2=200, y2=420, conf=0.8, label="car") -> Detection:
    return Detection(domain="vehicles", xyxy=(x1, y1, x2, y2), confidence=conf, label=label)


def ai_config(activity, regions=("left",), **params) -> dict:
    p = {"active_hours": None, "active_days": list(ALL_DAYS)}
    p.update(params)
    return {"regions": {"left": LEFT_HALF},
            "activities": [{"type": activity, "regions": list(regions), "params": p}]}


class Rig:
    def __init__(self, cfg, detections, config=None):
        self.events = []
        self.detector = FakeDetector(detections)
        self.engine = ActivityEngine(emit=self.events.extend)
        if config is not None:
            self.engine.configure(CAM, config)
        self.pipeline = ActivityPipeline(cfg, self.detector, self.engine)
        self.still = np.zeros((H, W, 3), dtype=np.uint8)     # never changes: no motion

    def run(self, start, seconds, fps=5.0):
        n = int(seconds * fps)
        for i in range(n):
            self.pipeline.process(CAM, self.still, start + i / fps)
        return self


# ── no motion gate ───────────────────────────────────────────────────────────

def test_every_frame_of_a_still_scene_reaches_the_activity_detector(cfg):
    rig = Rig(cfg, [car()], ai_config("stray_parking", required_duration_s=600)).run(0.0, 10)
    assert rig.detector.calls == 50
    assert rig.pipeline.snapshot()["motion_gated"] is False


def test_a_parked_vehicle_keeps_one_track_and_raises_stray_parking(cfg):
    seen = []
    rig = Rig(cfg, [car()], ai_config("stray_parking", required_duration_s=30))
    original = rig.engine.observe
    rig.engine.observe = lambda ctx: (seen.extend(o.track_id for o in ctx.objects), original(ctx))[1]
    rig.run(0.0, 40)
    assert len(set(seen)) == 1, "the stationary car kept one identity"
    [ev] = rig.events
    assert ev.activity == "stray_parking" and ev.track_id == seen[0]
    assert 30.0 <= ev.attributes["parked_s"] < 30.5


def test_people_standing_together_raise_a_gathering(cfg):
    rig = Rig(cfg, [person(20, 150, 60, 400), person(70, 150, 110, 400)],
              ai_config("people_gathering", required_duration_s=20, last_time=30)).run(0.0, 30)
    [ev] = rig.events
    assert ev.activity == "people_gathering" and ev.attributes["person_count"] == 2


def test_people_gathering_reads_its_behaviour_settings_on_the_live_path(cfg):
    two = [person(20, 150, 60, 400), person(70, 150, 110, 400)]    # feet 50 px apart, boxes 40 px wide
    base = {"required_duration_s": 20, "last_time": 30}
    assert len(Rig(cfg, two, ai_config("people_gathering", **base)).run(0.0, 30).events) == 1
    assert Rig(cfg, two, ai_config("people_gathering", **base, min_group_size=3)).run(0.0, 30).events == []
    assert Rig(cfg, two, ai_config("people_gathering", **base, proximity_factor=1.0)).run(0.0, 30).events == []


def test_a_person_in_a_restricted_zone_raises_on_the_first_frame(cfg):
    rig = Rig(cfg, [person()], ai_config("restricted_zone_entry", cooldown_s=60)).run(100.0, 1)
    [ev] = rig.events
    assert ev.started_at == 100.0 and ev.zone.key == "left"


def test_a_person_standing_in_a_restricted_zone_raises_once_not_every_cooldown(cfg):
    rig = Rig(cfg, [person()], ai_config("restricted_zone_entry", cooldown_s=5)).run(100.0, 60)
    assert len(rig.events) == 1


def test_a_person_walking_up_across_a_tripwire_is_one_entry_on_the_live_path(cfg):
    door = {"kind": "tripwire", "name": "Door", "color": "#ffb020",
            "points": [[0.1, 0.5], [0.9, 0.5]], "direction": "a2b"}   # entry = up the frame
    config = {"regions": {"door": door},
              "activities": [{"type": "entry_exit_WLE_logs", "regions": ["door"],
                              "params": {"active_hours": None, "active_days": list(ALL_DAYS)}}]}
    rig = Rig(cfg, [], config)
    t = 0.0
    for top in range(300, 20, -20):              # a 160 px tall person walking up past y = 240
        rig.detector.detections = [person(300, top, 340, top + 160)]
        rig.run(t, 0.4)
        t += 0.4
    [ev] = rig.events
    assert ev.attributes["direction"] == "entry" and ev.zone.key == "door"


def test_a_vehicle_that_disappears_past_the_tracker_age_starts_again(cfg):
    rig = Rig(cfg, [car()], ai_config("stray_parking", required_duration_s=20))
    rig.run(0.0, 15)
    rig.detector.detections = []
    rig.run(15.0, cfg.tracking.max_age_seconds + 2)        # gone long enough to retire
    rig.detector.detections = [car()]
    rig.run(15.0 + cfg.tracking.max_age_seconds + 2, 15)
    assert rig.events == [] and rig.pipeline.tracks_retired >= 1


def test_a_camera_without_a_running_activity_costs_no_detector_call(cfg):
    rig = Rig(cfg, [car()], ai_config("entry_exit_WLE_logs")).run(0.0, 2)
    assert rig.detector.calls == 0 and rig.pipeline.frames_skipped_no_work == 10


def test_only_domains_a_running_activity_consumes_are_tracked(cfg):
    seen = []
    rig = Rig(cfg, [car(), person(400, 100, 440, 400)], ai_config("stray_parking"))
    original = rig.engine.observe
    rig.engine.observe = lambda ctx: (seen.extend(o.domain for o in ctx.objects), original(ctx))[1]
    rig.run(0.0, 1)
    assert set(seen) == {"vehicles"}


# ── the broker: every frame for activities, Smart Search gated as before ────

def _ring(tmp_path, monkeypatch, camera, ts=1000.0, seq=1, slot=0):
    monkeypatch.setattr(frame_source, "_SHM_DIR", str(tmp_path))
    w, h, c, slots = 8, 6, 3, 2
    size = frame_source.CTRL_BYTES + slots * w * h * c
    path = frame_source.shm_path(camera)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    with open(path, "r+b") as fh:
        mm = mmap.mmap(fh.fileno(), size)
        struct.pack_into("<Q", mm, slot * 16, seq * 2)
        mm.flush()
        mm.close()
    return {"camera": camera, "ts": ts, "seq": seq, "slot": slot, "width": w, "height": h,
            "channels": c, "ring_slots": slots, "epoch": 1}


def _subscriber(activity_calls, search_calls, gated_calls):
    return frame_source.BrokerSubscriber(
        "redis://unused", on_frame=lambda s, f, t, m: search_calls.append(s),
        on_gated=gated_calls.append,
        on_activity_frame=lambda s, f, t: activity_calls.append((s, f.shape, t)))


@pytest.mark.parametrize("motion, search_expected", [
    ({"regions": None, "baseline_valid": True}, False),          # no motion
    ({"regions": [], "baseline_valid": True}, True),             # motion
])
def test_an_activity_camera_gets_every_frame_and_search_stays_gated(
        tmp_path, monkeypatch, motion, search_expected):
    notice = _ring(tmp_path, monkeypatch, CAM)
    act, search, gated = [], [], []
    sub = _subscriber(act, search, gated)
    sub.want(CAM)
    sub.want_every_frame(CAM)
    sub._handle({**notice, "motion": motion})
    assert act == [(CAM, (6, 8, 3), 1000.0)]
    assert (search == [CAM]) is search_expected
    assert (gated == [CAM]) is (not search_expected)


def test_a_search_only_camera_is_gated_exactly_as_before(tmp_path, monkeypatch):
    notice = _ring(tmp_path, monkeypatch, CAM)
    act, search, gated = [], [], []
    sub = _subscriber(act, search, gated)
    sub.want(CAM)
    sub._handle({**notice, "motion": {"regions": None, "baseline_valid": True}})
    assert act == [] and search == [] and gated == [CAM]
    assert sub.frames_gated_before_read == 1 and sub.activity_frames_delivered == 0


def test_an_activity_only_camera_never_reaches_search(tmp_path, monkeypatch):
    notice = _ring(tmp_path, monkeypatch, CAM)
    act, search, gated = [], [], []
    sub = _subscriber(act, search, gated)
    sub.want_every_frame(CAM)
    sub._handle({**notice, "motion": {"regions": [], "baseline_valid": True}})
    assert len(act) == 1 and search == [] and gated == []


def test_a_camera_nobody_wants_is_ignored(tmp_path, monkeypatch):
    notice = _ring(tmp_path, monkeypatch, CAM)
    act, search, gated = [], [], []
    sub = _subscriber(act, search, gated)
    sub._handle({**notice, "motion": {"regions": [], "baseline_valid": True}})
    assert act == search == gated == [] and sub.notices_ignored == 1


# ── Smart Search is untouched ────────────────────────────────────────────────

def test_the_smart_search_pipeline_knows_nothing_about_activities():
    assert "activities" not in inspect.signature(DetectionPipeline.__init__).parameters
    src = pathlib.Path(inspect.getfile(DetectionPipeline)).read_text(encoding="utf-8")
    assert "activit" not in src.lower()


def test_the_smart_search_pipeline_still_skips_a_no_motion_frame(cfg):
    detector = FakeDetector([person()])
    p = DetectionPipeline(cfg, detector)
    p._process(CAM, np.zeros((H, W, 3), dtype=np.uint8), 1.0,
               {"fraction": 0.0, "regions": None, "verdict": "none", "reason": "no motion",
                "baseline_valid": True})
    assert detector.calls == 0


# ── the service: Option 1, loaded on demand ─────────────────────────────────

POLL = 0.05


def _settle(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL)
    return False


def test_the_activity_path_gets_its_own_detector_only_when_an_activity_runs(cfg, monkeypatch):
    from analytics import detector as detector_mod, plates as plates_mod, selection as selection_mod

    built = []

    def fake_build_selected(*a, **kw):
        d = FakeDetector([car()])
        built.append(d)
        return d

    monkeypatch.setattr(detector_mod, "build_selected", fake_build_selected)
    monkeypatch.setattr(selection_mod, "DetectorSelector", lambda c: object())
    monkeypatch.setattr(plates_mod, "build", lambda c, d: None)
    cfg.source.frame_source = "sampler"
    cfg.sink.url = ""
    cfg.events.url = ""
    cfg.plates.enabled = False
    cfg.lifecycle.idle_timeout_seconds = 0.2
    cfg.lifecycle.poll_seconds = POLL
    engine = engine_mod.AnalyticsEngine(cfg)
    try:
        engine.add_camera("search-only", "rtsp://relay/a", ("person",))
        assert _settle(lambda: engine._pipeline is not None)
        assert engine._activity_pipeline is None and len(built) == 1

        engine.add_camera(CAM, "rtsp://relay/b", (), analytics_config=ai_config(
            "stray_parking", required_duration_s=600))
        assert _settle(lambda: engine._activity_pipeline is not None)
        assert len(built) == 2 and engine._activity_detector is not engine._detector
        assert engine.snapshot()["activity_pipeline"]["motion_gated"] is False

        submitted = []
        monkeypatch.setattr(engine._activity_pipeline, "submit",
                            lambda s, f, t: submitted.append(s))
        smart = []
        monkeypatch.setattr(engine._pipeline, "submit", lambda *a, **k: smart.append(a[0]))
        still = np.zeros((H, W, 3), dtype=np.uint8)
        engine._deliver_sampled(CAM, still, 1.0)
        engine._deliver_sampled("search-only", still, 1.0)
        assert submitted == [CAM] and smart == ["search-only"]

        engine.add_camera(CAM, "rtsp://relay/b", (), analytics_config={})
        assert _settle(lambda: engine._activity_pipeline is None, timeout=5.0)
        assert engine._pipeline is not None                     # Smart Search keeps its own
    finally:
        engine.stop()


def test_the_registry_is_published(cfg):
    pytest.importorskip("fastapi", reason="fastapi lives in the analytics image")
    pytest.importorskip("httpx", reason="TestClient needs httpx")
    from fastapi.testclient import TestClient

    from api.server import create_app

    cfg.detect.enabled = False
    cfg.source.frame_source = "sampler"
    cfg.sink.url = cfg.events.url = ""
    engine = engine_mod.AnalyticsEngine(cfg)
    try:
        body = TestClient(create_app(engine)).get("/activities").json()
        status = {d["key"]: d["status"] for d in body["activities"]}
        assert status["stray_parking"] == "available"
        assert status["entry_exit_WLE_logs"] == "available"
        assert status["idle_worker"] == "hold" and "person_detection" not in status
    finally:
        engine.stop()


def test_the_activity_path_does_not_import_deepstream():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in [*(root / "analytics").rglob("*.py"), *(root / "api").rglob("*.py"), root / "main.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if any("deepstream" in n.lower() for n in names):
                    offenders.append(str(path.relative_to(root)))
    assert not offenders, offenders
