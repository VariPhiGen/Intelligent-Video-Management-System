"""Detector sizing: the most accurate model this machine keeps up with.

What must hold, in the order an operator would notice it failing:
  * a CPU that cannot keep up gets the smaller model, not a queue that drops
    frames at the busiest moment;
  * the change happens LIVE — same pipeline, same trackers, no gap;
  * it does not flap: down at once, up only after demand stayed low;
  * a pinned model is never second-guessed, only warned about;
  * a missing candidate costs that candidate, never the detector.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import capacity                                       # noqa: E402
from analytics import detector as detector_mod                       # noqa: E402
from analytics import engine as engine_mod                           # noqa: E402
from analytics import plates as plates_mod                           # noqa: E402
from analytics import sampler as sampler_mod                         # noqa: E402
from analytics import selection as selection_mod                     # noqa: E402
from analytics.capacity import (AUTO, CapacityCache, choose,         # noqa: E402
                                demand_fps, ensure_weights)
from analytics.config import AppConfig                               # noqa: E402
from analytics.detector import SwappableDetector                     # noqa: E402
from test_lifecycle import FakePipeline, FakeSampler                 # noqa: E402

S, N = "/models/weights/yolov8s.pt", "/models/weights/yolov8n.pt"


class Model:
    """A fake detector that knows which weights it is."""

    def __init__(self, weights):
        self.weights = weights

    def detect(self, frame):
        return []


# ── demand ───────────────────────────────────────────────────────────────────
def test_demand_counts_activity_cameras_in_full_and_search_by_motion():
    # 6 search cameras at 5 fps, half moving, plus 2 activity cameras at 5 fps.
    assert demand_fps(6, 2, 5.0, 0.5) == 15.0 + 10.0
    assert demand_fps(0, 0, 5.0, 0.5) == 0.0


# ── choose ───────────────────────────────────────────────────────────────────
def _choose(fps, demand, headroom=1.25, broken=()):
    built = []

    def build(w):
        built.append(w)
        if w in broken:
            raise FileNotFoundError(f"{w} missing")
        return Model(w)
    choice, det = choose([S, N], lambda: demand, headroom, build, lambda w, d: fps[w])
    return choice, det, built


def test_the_accurate_model_when_it_keeps_up_and_nothing_else_is_loaded():
    choice, det, built = _choose({S: 10.0, N: 20.0}, demand=6.0)
    assert (choice.weights, det.weights, built) == (S, S, [S])
    assert not choice.over_capacity and "keeps up" in choice.reason


def test_the_smaller_model_when_the_accurate_one_cannot_keep_up():
    choice, _, built = _choose({S: 10.0, N: 20.0}, demand=12.0)    # needs 15
    assert choice.weights == N and built == [S, N]
    assert choice.measured == {S: 10.0, N: 20.0}
    assert "too slow" in choice.reason


def test_nothing_fits_takes_the_fastest_and_says_frames_will_drop():
    choice, _, _ = _choose({S: 10.0, N: 20.0}, demand=40.0)
    assert choice.weights == N and choice.over_capacity
    assert "dropped frames" in choice.reason


def test_a_missing_candidate_costs_only_that_candidate():
    choice, _, _ = _choose({S: 10.0, N: 20.0}, demand=1.0, broken=(S,))
    assert choice.weights == N and choice.measured[S] == "unavailable"


def test_no_candidate_at_all_is_a_failed_load():
    with pytest.raises(RuntimeError, match="no detector candidate"):
        _choose({}, demand=1.0, broken=(S, N))


def test_demand_is_read_after_the_model_loaded_not_before():
    """camera-mgmt pushes the camera set in a burst; the first camera starts
    the warm-up. A demand read up front would count one camera of six."""
    cameras = [1]

    def build(w):
        if len(cameras) == 1:
            cameras.extend([1] * 5)          # the rest of the burst lands meanwhile
        return Model(w)
    choice, _ = choose([S, N], lambda: demand_fps(len(cameras), 0, 5.0, 0.5), 1.25,
                       build, lambda w, d: {S: 10.0, N: 40.0}[w])
    assert choice.demand_fps == 15.0 and choice.weights == N


# ── cache ────────────────────────────────────────────────────────────────────
def test_cache_round_trips_and_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "artifacts" / "detector-capacity.json"
    cache = CapacityCache(str(path))
    assert cache.get("k") is None
    cache.put("k", 9.64, {"weights": "yolov8s.pt"})
    assert cache.get("k") == 9.64
    path.write_text("{not json")
    assert cache.get("k") is None
    cache.put("k2", 3.0, {})
    assert json.loads(path.read_text())["k2"]["fps"] == 3.0


def test_a_measurement_is_specific_to_what_would_change_it(tmp_path):
    w = tmp_path / "yolov8s.pt"
    w.write_bytes(b"x" * 10)
    base = capacity.machine_key(str(w), "torch", "2.14", 5)
    assert base == capacity.machine_key(str(w), "torch", "2.14", 5)
    assert base != capacity.machine_key(str(w), "openvino", "2026.4", 5)
    assert base != capacity.machine_key(str(w), "torch", "2.14", 8)
    w.write_bytes(b"x" * 11)                  # new weights under the same name
    assert base != capacity.machine_key(str(w), "torch", "2.14", 5)


# ── weights ──────────────────────────────────────────────────────────────────
def test_a_bare_catalogue_name_is_left_to_ultralytics():
    ensure_weights("yolov8n.pt")              # no exception, no download attempt


def test_a_missing_path_offline_is_a_clear_error(tmp_path, monkeypatch):
    from ultralytics.utils import downloads

    def offline(*a, **kw):
        raise ConnectionError("no route to github.com")
    monkeypatch.setattr(downloads, "attempt_download_asset", offline)
    with pytest.raises(FileNotFoundError, match="could not be downloaded"):
        ensure_weights(str(tmp_path / "yolov8s.pt"))


# ── the swap ─────────────────────────────────────────────────────────────────
def test_swap_rebinds_and_forwards():
    a, b = Model(S), Model(N)
    proxy = SwappableDetector(a)
    assert proxy.weights == S and proxy.detect(None) == []
    assert proxy.swap(b) is a
    assert proxy.inner is b and proxy.weights == N


# ── the engine, end to end ───────────────────────────────────────────────────
class CapturingPipeline(FakePipeline):
    instances: list = []

    def __init__(self, cfg, detector, *a, **kw):
        super().__init__(cfg, detector, *a, **kw)
        self.detector = detector
        CapturingPipeline.instances.append(self)


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """Factory: engine(fps={weights: fps}, pinned=None) with everything faked."""
    made = []
    CapturingPipeline.instances = []
    fps = {}
    monkeypatch.setattr(detector_mod, "build_selected", lambda sel, w, *a, **kw: Model(w))
    monkeypatch.setattr(engine_mod, "ensure_weights", lambda w: None)
    monkeypatch.setattr(engine_mod, "measure_fps", lambda det: fps[det.weights])
    monkeypatch.setattr(plates_mod, "build", lambda c, d: None)
    monkeypatch.setattr(selection_mod, "DetectorSelector", lambda c: object())
    monkeypatch.setattr(sampler_mod, "CameraSampler", FakeSampler)
    monkeypatch.setattr(engine_mod, "DetectionPipeline", CapturingPipeline)
    monkeypatch.setattr(engine_mod.AnalyticsEngine, "_reclaim", staticmethod(lambda: None))

    def make(measured, pinned=None):
        fps.update(measured)
        cfg = AppConfig()
        cfg.source.frame_source = "sampler"
        cfg.sink.url = ""
        cfg.plates.enabled = False
        cfg.lifecycle.poll_seconds = 0.05
        cfg.lifecycle.idle_timeout_seconds = 30
        cfg.models.models_dir = str(tmp_path)
        cfg.models.detector_candidates = [S, N]
        cfg.models.detector_weights = pinned or AUTO
        e = engine_mod.AnalyticsEngine(cfg)
        made.append(e)
        return e
    yield make
    for e in made:
        e.stop()


def settle(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def add(e, n, start=0):
    for i in range(start, start + n):
        e.add_camera(f"cam-{i}", f"rtsp://x/{i}", ("person", "vehicles"))


def test_warm_up_picks_the_model_the_camera_set_can_afford(engine):
    # 6 search cameras -> 15 fps x 1.25 = 18.75 needed; s does 10, n does 40.
    e = engine({S: 10.0, N: 40.0})
    add(e, 6)
    # Settles on n however the burst interleaves with the warm-up: chosen
    # outright, or chosen early on a partial set and corrected by the next
    # review. Self-correcting is the guarantee, not the timing.
    assert settle(lambda: e._choice is not None and e._choice.weights == N), \
        e._choice and e._choice.reason
    health = e.snapshot()["models"]
    assert health["detector_choice"]["model"] == "yolov8n"
    assert health["detector"] == "Model", "health must name the model, not the wrapper"


def test_outgrowing_the_model_downgrades_live_without_a_new_pipeline(engine):
    e = engine({S: 10.0, N: 40.0})
    add(e, 1)                                  # 2.5 fps x 1.25 -> s fits
    assert settle(lambda: e._pipeline is not None)
    assert e._choice.weights == S
    pipeline, proxy = CapturingPipeline.instances[0], e._detector
    add(e, 5, start=1)                         # now 15 fps -> s no longer fits
    assert settle(lambda: e._choice.weights == N), e._choice.reason
    assert e._detector is proxy and proxy.inner.weights == N
    assert len(CapturingPipeline.instances) == 1 and pipeline.detector is proxy, \
        "the swap must not rebuild the pipeline (gap, tracker reset)"
    assert "switched from yolov8s.pt to yolov8n.pt" in e._choice.reason


def test_upgrade_waits_for_demand_to_stay_low(engine, monkeypatch):
    e = engine({S: 10.0, N: 40.0})
    add(e, 6)
    assert settle(lambda: e._pipeline is not None and e._choice.weights == N)
    for i in range(1, 6):
        e.remove_camera(f"cam-{i}")            # back to 2.5 fps: s affordable again
    time.sleep(0.3)                            # several reviews later...
    assert e._choice.weights == N, "must not flap back up straight away"
    monkeypatch.setattr(engine_mod.AnalyticsEngine, "UPGRADE_AFTER_S", 0.2)
    assert settle(lambda: e._choice.weights == S), e._choice.reason
    assert e._detector.inner.weights == S


def test_a_pinned_model_is_never_switched_only_flagged(engine):
    e = engine({S: 10.0, N: 40.0}, pinned=S)
    add(e, 6)
    assert settle(lambda: e._pipeline is not None)
    # The warning follows the camera set even if the warm-up saw only part of it.
    assert settle(lambda: e.snapshot()["models"]["detector_choice"]["over_capacity"])
    time.sleep(0.3)
    choice = e.snapshot()["models"]["detector_choice"]
    assert choice["mode"] == "pinned" and choice["weights"] == S
    assert choice["over_capacity"] and "dropped frames" in choice["reason"]


def test_a_measurement_is_made_once_and_reused_by_the_next_warm_up(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_mod, "measure_fps", lambda det: calls.append(det.weights) or 10.0)
    e = engine({})
    add(e, 1)
    assert settle(lambda: e._pipeline is not None)
    e2 = engine({})
    add(e2, 1)
    assert settle(lambda: e2._pipeline is not None)
    assert calls == [S], "the second warm-up must read the cache"


# ── live evidence beats the start-up estimate ────────────────────────────────
class Slow(Model):
    def detect(self, frame):
        time.sleep(0.01)
        return []


def test_live_throughput_needs_enough_real_calls_then_tracks_them():
    proxy = SwappableDetector(Slow(S))
    for _ in range(SwappableDetector.MIN_SAMPLES - 1):
        proxy.detect(None)
    assert proxy.live_fps() is None, "too few calls to trust"
    proxy.detect(None)
    assert 20 < proxy.live_fps() < 105          # ~1 / 10 ms, with scheduling slack
    proxy.swap(Slow(N))
    assert proxy.live_fps() is None, "timings of the old model must not carry over"


def _fast_live(monkeypatch):
    monkeypatch.setattr(engine_mod.AnalyticsEngine, "LIVE_MIN_S", 0.15)
    monkeypatch.setattr(engine_mod.AnalyticsEngine, "LOAD_SAMPLE_S", 0.05)


def test_observed_load_triggers_a_downgrade_the_estimate_missed(engine, monkeypatch):
    """One camera estimates 2.5 fps, so s is chosen; then the real traffic
    (crop calls included) runs at ~20 calls/s and s cannot keep up."""
    _fast_live(monkeypatch)
    e = engine({S: 10.0, N: 40.0})
    add(e, 1)
    assert settle(lambda: e._pipeline is not None) and e._choice.weights == S
    p = CapturingPipeline.instances[0]

    def traffic():
        p.frames_queued += 1; p.frames_processed += 1; p.detector_calls += 2   # noqa: E702
        e._review_capacity()
        return e._choice.weights == N
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not traffic():
        time.sleep(0.005)
    assert e._choice.weights == N, e._choice.reason
    assert e._detector.inner.weights == N


def test_contention_seen_live_blocks_an_upgrade_the_benchmark_would_allow(engine, monkeypatch):
    """n was chosen for 6 cameras. They go; s benchmarked at 10 fps would fit
    the one left — but n runs at half its benchmark live, so s would too."""
    e = engine({S: 10.0, N: 40.0})
    add(e, 6)
    assert settle(lambda: e._pipeline is not None and e._choice.weights == N)
    for i in range(1, 6):
        e.remove_camera(f"cam-{i}")
    monkeypatch.setattr(engine_mod.AnalyticsEngine, "UPGRADE_AFTER_S", 0.1)
    # 1 camera estimates 2.5 fps -> needs 2.5 x 1.25 x 1.2 = 3.75 from s.
    e._detector.live_fps = lambda: 40.0 * 0.3          # n at 30% of its benchmark
    for _ in range(20):                                  # s would manage ~3 fps
        e._review_capacity()
        time.sleep(0.02)
    assert e._choice.weights == N, "an upgrade that cannot keep up is not an upgrade"
    e._detector.live_fps = lambda: 40.0                  # contention gone
    assert settle(lambda: (e._review_capacity(), e._choice.weights == S)[1]), e._choice.reason


def test_the_reason_is_rewritten_from_live_figures(engine, monkeypatch):
    """The sentence beside the numbers must describe the same numbers."""
    _fast_live(monkeypatch)
    e = engine({S: 10.0, N: 4.3})
    add(e, 6)
    assert settle(lambda: e._choice is not None and e._choice.weights == N)
    e._detector.live_fps = lambda: 4.2
    p = CapturingPipeline.instances[0]

    def live():
        p.frames_queued += 1; p.frames_processed += 1; p.detector_calls += 1   # noqa: E702
        e._review_capacity()
        return e._choice.basis == "live"
    assert settle(lambda: (time.sleep(0.005), live())[1])
    assert "measured live" in e._choice.reason and "12.5" not in e._choice.reason
    assert "fastest model, cannot keep up" in e._choice.reason
