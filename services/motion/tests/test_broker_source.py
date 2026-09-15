"""Taking the motion signal from the frame broker.

THE ONLY THING THAT MAY CHANGE IS WHERE THE NUMBER COMES FROM. Everything an
operator sees — the confirmation window, TRIGGERED/MONITORING, the cooldown,
the Events API — must be untouched. So the load-bearing test here is not that
the broker path works; it is that driving the SAME feature sequence through
both paths produces the SAME events.

That holds by construction because both call worker.apply_feature, and these
tests exist to keep it that way: if someone later reimplements the decision in
the broker path, `test_both_paths_produce_identical_events` fails.

Run: python3 -m pytest tests -q   (from services/motion)
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detector.motion_core import compute_pixel_feature, preprocess_frame  # noqa: E402
from detector.config import AppConfig                                   # noqa: E402
from detector.engine import MotionEngine                                # noqa: E402
from detector.notices import NoticeSubscriber                           # noqa: E402
from detector.worker import apply_feature                               # noqa: E402


@pytest.fixture
def cfg():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return AppConfig.from_yaml(os.path.join(here, "config.yaml"))


@pytest.fixture
def engine(cfg):
    e = MotionEngine(cfg)
    yield e
    e.stop_all()


def notice(camera: str, fraction: float, *, ts: float | None = None,
           baseline_valid: bool = True) -> dict:
    return {"v": 1, "camera": camera, "ts": ts if ts is not None else time.time(),
            "seq": 1, "slot": 0, "shm": f"/dev/shm/vms_frames.{camera}",
            "width": 1920, "height": 1080, "channels": 3, "ring_slots": 4,
            "motion": {"fraction": fraction, "regions": None, "verdict": "none",
                       "reason": "test", "baseline_valid": baseline_valid}}


# ── the property the whole phase rests on ────────────────────────────────────
def test_both_paths_produce_identical_events(cfg, engine):
    """THE EXIT CRITERION, as a test.

    The same sequence of feature values, once through the capture path's
    decision call and once through the broker's, must produce the same events —
    same count, same order, same trigger points.
    """
    strong = cfg.detection.sensitivity_presets["medium"] * 3
    quiet = 0.0
    sequence = [quiet] * 3 + [strong] * 8 + [quiet] * 6 + [strong] * 8

    def run(via_broker: bool) -> list[tuple]:
        eng = MotionEngine(cfg)
        try:
            if via_broker:
                sub = NoticeSubscriber(cfg, eng, "redis://unused/0")
                eng.attach_subscriber(sub)
            cam = eng.add_camera("cam-x", "rtsp://relay/cam-x")
            # A camera only leaves CONNECTING when frames arrive. The capture
            # loop does that on its first decode and the broker path does it on
            # its first notice; neither can here, because the URL is fake. Put
            # both runs in the same starting state so this compares the
            # DECISION and not how the two paths get connected.
            with cam.lock:
                if cam.state == "CONNECTING":
                    cam.transition_to("MONITORING")
            t = 1000.0
            for f in sequence:
                if via_broker:
                    sub._handle(notice("cam-x", f, ts=t))
                else:
                    apply_feature(cam, f, cfg, eng)
                t += cfg.detection.sample_interval_seconds
            return [(e["camera"], e["started_at"] is not None)
                    for e in eng.list_events(since_id=0, limit=1000)]
        finally:
            eng.stop_all()

    assert run(False) == run(True)


def test_the_broker_path_calls_the_shared_decision():
    """If the decision is ever reimplemented rather than called, this fails —
    which is the drift these tests exist to prevent."""
    import inspect

    from detector import notices
    src = inspect.getsource(notices)
    assert "apply_feature" in src
    # ...and does not grow its own copy of the state machine.
    for forbidden in ("transition_to(\"TRIGGERED\")", "confirm_ratio"):
        assert forbidden not in src, \
            f"the broker path has its own copy of {forbidden!r}"


# ── behaviour ────────────────────────────────────────────────────────────────
def test_a_notice_moves_a_camera_out_of_connecting(cfg, engine):
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-a", "rtsp://relay/cam-a")
    assert cam.state == "CONNECTING"
    sub._handle(notice("cam-a", 0.0))
    assert cam.state == "MONITORING"


def test_sustained_motion_triggers(cfg, engine):
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-b", "rtsp://relay/cam-b")
    strong = cfg.detection.sensitivity_presets["medium"] * 5
    t = 500.0
    for _ in range(10):
        sub._handle(notice("cam-b", strong, ts=t))
        t += cfg.detection.sample_interval_seconds
    assert cam.state == "TRIGGERED"


def test_a_quiet_scene_never_triggers(cfg, engine):
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-c", "rtsp://relay/cam-c")
    t = 500.0
    for _ in range(20):
        sub._handle(notice("cam-c", 0.0, ts=t))
        t += cfg.detection.sample_interval_seconds
    assert cam.state == "MONITORING"
    assert engine.list_events(since_id=0, limit=10) == []


def test_a_camera_we_do_not_watch_is_ignored(cfg, engine):
    """The broker serves whatever it is given; this service analyses only what
    an operator opted in. Its camera set is not ours."""
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    sub._handle(notice("never-registered", 0.9))
    assert sub.notices_ignored == 1
    assert sub.samples_applied == 0


def test_a_lost_baseline_clears_the_window_instead_of_triggering(cfg, engine):
    """A reconnect's first 'difference' spans the whole outage. Feeding it in
    would let an outage confirm itself into an alert."""
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-d", "rtsp://relay/cam-d")
    strong = cfg.detection.sensitivity_presets["medium"] * 5
    t = 500.0
    for _ in range(3):
        sub._handle(notice("cam-d", strong, ts=t)); t += 0.5
    sub._handle(notice("cam-d", 0.99, ts=t, baseline_valid=False))
    assert len(cam.motion_window) == 0, "an outage was fed in as motion"
    assert cam.state != "TRIGGERED"


def test_the_window_follows_the_brokers_actual_cadence(cfg, engine):
    """The window is a deque of samples, so its length only means three
    SECONDS while the interval matches. The broker owns the rate now, so the
    interval is measured rather than assumed."""
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-e", "rtsp://relay/cam-e")
    t = 500.0
    for _ in range(8):                       # 1 FPS, not the configured 2
        sub._handle(notice("cam-e", 0.0, ts=t))
        t += 1.0
    want = round(cfg.detection.confirm_window_seconds / 1.0)
    assert cam.motion_window.maxlen == want, \
        "the confirmation window did not follow the broker's rate"


def test_losing_the_input_closes_an_open_event(cfg, engine):
    """WHAT THE CAPTURE PATH DOES ON A LOST STREAM, so this must too.

    A disconnect ends an open incident there: the evidence stream is gone and
    re-arming on reconnect beats leaving a stale TRIGGERED. Clearing the
    window without closing the event left it open indefinitely — a divergence
    in the Events API itself, which is the one thing this phase may not
    change.
    """
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-h", "rtsp://relay/cam-h")
    strong = cfg.detection.sensitivity_presets["medium"] * 5
    t = 500.0
    for _ in range(10):
        sub._handle(notice("cam-h", strong, ts=t))
        t += cfg.detection.sample_interval_seconds
    assert cam.state == "TRIGGERED"
    open_events = [e for e in engine.list_events(since_id=0, limit=10)
                   if e["ended_at"] is None]
    assert len(open_events) == 1, "no open event to lose"

    # The notices stop.
    sub._last_ts["cam-h"] = time.time() - 3600
    sub._rearm_all()

    assert cam.state == "CONNECTING"
    assert cam.open_event is None
    assert cam.triggered_at is None
    still_open = [e for e in engine.list_events(since_id=0, limit=10)
                  if e["ended_at"] is None]
    assert still_open == [], "an incident stayed open after the input was lost"


def test_staleness_is_no_twitchier_than_the_capture_path(cfg, engine):
    """The capture loop tolerates a full read_timeout_ms before calling a
    stream lost. Deriving this from the sample cadence alone made broker mode
    flip cameras to CONNECTING after 3.2s where capture waits 5s — a visible
    difference in a phase whose whole point is that there is none."""
    from detector.notices import _stale_after
    interval = cfg.detection.sample_interval_seconds
    assert _stale_after(cfg, interval) >= cfg.capture.read_timeout_ms / 1000.0

    # A much slower broker still gets a proportionate allowance.
    assert _stale_after(cfg, 4.0) > cfg.capture.read_timeout_ms / 1000.0


def test_a_stale_camera_is_reported_once_not_once_per_pass(cfg, engine, caplog):
    """_rearm_all runs on every notice and every idle second. Logging whenever
    a camera IS stale, rather than when it BECOMES stale, wrote a line per
    pass for the whole outage — 362 of them from a single broker restart."""
    import logging
    sub = NoticeSubscriber(cfg, engine, "redis://unused/0")
    engine.attach_subscriber(sub)
    cam = engine.add_camera("cam-g", "rtsp://relay/cam-g")
    sub._handle(notice("cam-g", 0.0, ts=time.time() - 3600))
    with caplog.at_level(logging.WARNING, logger="detector.notices"):
        for _ in range(20):
            sub._rearm_all()
    lines = [r for r in caplog.records if "no notices for" in r.getMessage()]
    assert len(lines) == 1, f"logged {len(lines)} times for one outage"
    assert cam.state == "CONNECTING"


# ── configuration ────────────────────────────────────────────────────────────
def test_capture_is_still_the_default(cfg):
    assert cfg.source.frame_source == "capture"


def test_only_the_two_known_sources_are_accepted(monkeypatch):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "config.yaml")
    monkeypatch.setenv("MOTION_FRAME_SOURCE", "broker")
    assert AppConfig.from_yaml(path).source.frame_source == "broker"
    monkeypatch.setenv("MOTION_FRAME_SOURCE", "rtsp")
    with pytest.raises(ValueError):
        AppConfig.from_yaml(path)


def test_broker_mode_starts_no_capture_threads(cfg):
    """No RTSP session and no decode — that is the saving."""
    eng = MotionEngine(cfg)
    try:
        eng.attach_subscriber(NoticeSubscriber(cfg, eng, "redis://unused/0"))
        eng.add_camera("cam-f", "rtsp://relay/cam-f")
        assert eng._threads == {}, "broker mode started a capture thread"
    finally:
        eng.stop_all()
