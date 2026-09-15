"""Model lifecycle: load on the first camera, release when the last one goes.

WHY THIS FILE EXISTS. `9f5a5ec` moved the detector and the plate reader out of
Smart Search and into this service, but left behind the ModelPool that gave
them a lifetime. The result was observed on the appliance on 2026-09-09:
switching Person, Vehicles and ANPR off on all seven cameras stopped every
decode, every detector call and every write to the index — and left this
service holding 164 MiB of GPU and 1.36 GiB of RSS for models with nothing to
detect, for ever. Smart Search, on the same box at the same moment, released
its encoder 300 s after its last camera. These tests are that lifecycle put
back where the models now live.

They drive analytics/engine.AnalyticsEngine with FAKE weights, because what is
being tested is the STATE MACHINE, the LOCKING and the DEMAND SIGNAL, not
whether YOLO loads. Three things here cannot be found by running the service:

  * `test_a_domain_change_does_not_unload` — camera-mgmt reconciles from every
    uvicorn worker, so the registered count touches zero on ordinary traffic.
    Without the debounce that unloads and reloads the weights on an edit.
  * `test_a_failed_plate_load_is_not_retried_on_every_reassert` — every camera
    is re-asserted once a minute, so clearing the failure flag on demand rather
    than on the transition into demand retries an egress-blocked download for
    as long as the appliance runs.
  * `test_the_warm_up_does_not_block_the_caller` — camera-mgmt's POST /cameras
    has a 10 s timeout and the real load took 41 s on this appliance's first
    camera. Blocking it there made the sync time out, retry, and warm again.

Run: python3 -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import detector as detector_mod                    # noqa: E402
from analytics import engine as engine_mod                        # noqa: E402
from analytics import plates as plates_mod                        # noqa: E402
from analytics import sampler as sampler_mod                      # noqa: E402
from analytics import selection as selection_mod                  # noqa: E402
from analytics.config import AppConfig                            # noqa: E402
from analytics.engine import (                                    # noqa: E402
    DISABLED, FAILED, IDLE, RUNNING, WARMING_UP, AnalyticsEngine,
)

IDLE_TIMEOUT = 1.0
POLL = 0.05
#: The reaper floors its own poll at 1 s, so a test that only wants to prove
#: "nothing was released" has to outlast a tick or it proves nothing at all.
TICKS = 2.5
#: Long enough for a warm-up thread plus a couple of reaper ticks, short enough
#: that a hang fails the test rather than the suite.
SETTLE = 5.0


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeDetector:
    def detect(self, frame):
        return []


class FakePlateReader:
    active = True

    def read(self, crop):
        return None


class FakePipeline:
    """Only what the engine actually touches, plus the counters it folds."""

    def __init__(self, *a, **kw):
        self.domains = {}
        self.stopped = 0
        self.plate_reader = kw.get("plate_reader")
        self.started = False
        # Non-zero so lifetime folding is visible rather than vacuously equal.
        self.frames_processed = 7
        self.detector_calls = 11
        self.detections_seen = 3
        for k in engine_mod._LIFETIME_KEYS:
            if not hasattr(self, k):
                setattr(self, k, 0)

    def start(self):
        self.started = True

    def stop(self, join_timeout: float = 10.0):
        self.stopped += 1

    def set_domains(self, slug, domains):
        self.domains[slug] = tuple(domains)

    def forget_domains(self, slug):
        self.domains.pop(slug, None)

    def domains_for(self, slug):
        return self.domains.get(slug, ("person", "vehicles"))

    def set_plate_reader(self, reader):
        self.plate_reader = reader

    def reset_gate(self, slug):
        pass

    def note_gated_before_read(self, slug):
        pass

    def submit(self, slug, frame, ts, motion=None):
        self.frames_processed += 1

    def snapshot(self):
        return {"frames_processed": self.frames_processed}


class FakeSampler:
    def __init__(self, slug, url, fps, on_frame=None, on_reconnect=None):
        self.slug = slug
        self.stopped = 0

    def start(self):
        pass

    def stop(self):
        self.stopped += 1

    def snapshot(self):
        return {"state": "SAMPLING", "frames_sampled": 0}


class Rig:
    """An engine with fake weights, and a count of what was built."""

    def __init__(self, engine, calls):
        self.engine = engine
        self.calls = calls

    def settle(self, predicate, timeout: float = SETTLE):
        """Wait for the reaper or a warm-up thread, or fail with what it is."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(POLL / 2)
        return False


@pytest.fixture
def rig(monkeypatch):
    """Factory: rig(**overrides) builds an engine with everything faked."""
    engines: list[AnalyticsEngine] = []
    calls = {"detector": 0, "plates": 0, "reclaim": 0}

    def fake_build_selected(*a, **kw):
        calls["detector"] += 1
        return FakeDetector()

    def fake_build_plates(cfg, device):
        calls["plates"] += 1
        return FakePlateReader() if cfg.enabled else None

    monkeypatch.setattr(detector_mod, "build_selected", fake_build_selected)
    monkeypatch.setattr(plates_mod, "build", fake_build_plates)
    monkeypatch.setattr(selection_mod, "DetectorSelector", lambda cfg: object())
    monkeypatch.setattr(sampler_mod, "CameraSampler", FakeSampler)
    monkeypatch.setattr(engine_mod, "DetectionPipeline", FakePipeline)
    monkeypatch.setattr(engine_mod.AnalyticsEngine, "_reclaim",
                        staticmethod(lambda: calls.__setitem__(
                            "reclaim", calls["reclaim"] + 1)))

    def make(idle_timeout: float = IDLE_TIMEOUT, detect_enabled: bool = True,
             plates_enabled: bool = True) -> Rig:
        cfg = AppConfig()
        # Sampler mode: no redis, no broker thread. The lifecycle is identical
        # either way — the frame source is not what the models depend on.
        cfg.source.frame_source = "sampler"
        cfg.sink.url = ""
        cfg.detect.enabled = detect_enabled
        cfg.plates.enabled = plates_enabled
        cfg.lifecycle.idle_timeout_seconds = idle_timeout
        cfg.lifecycle.poll_seconds = POLL
        cfg.lifecycle.retry_seconds = POLL
        eng = AnalyticsEngine(cfg)
        engines.append(eng)
        return Rig(eng, calls)

    yield make
    for e in engines:
        e.stop()


def _warm(rig_: Rig, slug: str = "cam-1", domains=("person", "vehicles")):
    """Add a camera and wait for the pipeline to actually land."""
    rig_.engine.add_camera(slug, f"rtsp://x/{slug}", tuple(domains))
    assert rig_.settle(lambda: rig_.engine._pipeline is not None), \
        f"pipeline never came up (state={rig_.engine._state})"


# ── loading ──────────────────────────────────────────────────────────────────

def test_no_camera_loads_no_weights(rig):
    """A deployment that registers nothing must not pay for weights."""
    r = rig()
    time.sleep(POLL * 4)
    assert r.calls["detector"] == 0
    assert r.engine._pipeline is None
    assert r.engine.snapshot()["state"] == IDLE


def test_detection_switched_off_never_loads(rig):
    r = rig(detect_enabled=False)
    r.engine.add_camera("cam-1", "rtsp://x/1", ("person",))
    time.sleep(POLL * 6)
    assert r.calls["detector"] == 0
    assert r.engine.snapshot()["state"] == DISABLED


def test_the_first_camera_warms_up(rig):
    r = rig()
    _warm(r)
    assert r.calls["detector"] == 1
    assert r.engine.snapshot()["state"] == RUNNING


def test_the_warm_up_does_not_block_the_caller(rig, monkeypatch):
    """THE 10-SECOND TIMEOUT. camera-mgmt's POST /cameras gives up after 10 s;
    the real detector took 41 s on this appliance's first camera. add_camera
    must return while that is still happening, not after it."""
    gate = threading.Event()

    def slow_build(*a, **kw):
        gate.wait(SETTLE)
        return FakeDetector()

    monkeypatch.setattr(detector_mod, "build_selected", slow_build)
    r = rig()
    started = time.monotonic()
    snap = r.engine.add_camera("cam-1", "rtsp://x/1", ("person",))
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"add_camera blocked for {elapsed:.1f}s on a model load"
    # And it says so rather than claiming the camera is being analysed.
    assert snap["state"] == WARMING_UP
    gate.set()


def test_a_camera_registered_during_the_warm_up_gets_its_domains(rig, monkeypatch):
    """Cameras that arrive mid-load were never handed to set_domains, so
    without the catch-up loop they silently fall back to the person+vehicles
    default in domains_for — a person-only camera would start paying to crop
    and track vehicles."""
    gate = threading.Event()

    def slow_build(*a, **kw):
        gate.wait(SETTLE)
        return FakeDetector()

    monkeypatch.setattr(detector_mod, "build_selected", slow_build)
    r = rig()
    r.engine.add_camera("cam-1", "rtsp://x/1", ("person",))
    r.engine.add_camera("cam-2", "rtsp://x/2", ("vehicles",))
    gate.set()
    assert r.settle(lambda: r.engine._pipeline is not None)
    assert r.engine._pipeline.domains == {
        "cam-1": ("person",), "cam-2": ("vehicles",)}


def test_frames_before_the_models_are_counted_not_crashed(rig, monkeypatch):
    """A frame arriving while nothing is loaded is what a warm-up and a
    hibernated service both look like. It must be visible on /health, not an
    exception in the broker's callback."""
    gate = threading.Event()
    monkeypatch.setattr(detector_mod, "build_selected",
                        lambda *a, **kw: (gate.wait(SETTLE), FakeDetector())[1])
    r = rig()
    r.engine.add_camera("cam-1", "rtsp://x/1", ("person",))
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    r.engine._deliver("cam-1", frame, time.time(), None)
    r.engine._deliver("cam-1", frame, time.time(), None)
    gate.set()
    life = r.engine.snapshot()["lifecycle"]
    assert life["frames_dropped_no_pipeline"] == 2


# ── release ──────────────────────────────────────────────────────────────────

def test_the_last_camera_going_releases_the_models(rig):
    """THE BUG THIS FILE EXISTS FOR. Zero cameras must not cost weights."""
    r = rig()
    _warm(r)
    pipeline = r.engine._pipeline

    r.engine.remove_camera("cam-1")
    assert r.settle(lambda: r.engine._pipeline is None), \
        "models were never released after the last camera went"

    assert pipeline.stopped == 1, "the worker thread was left running"
    assert r.engine._detector is None
    assert r.engine._plates is None
    assert r.engine._state == IDLE
    assert r.calls["reclaim"] >= 1, "memory was dropped but never reclaimed"


def test_release_waits_for_the_timer(rig):
    """Not on the transition: the count touches zero on ordinary traffic."""
    r = rig(idle_timeout=30.0)
    _warm(r)
    r.engine.remove_camera("cam-1")
    # Outlast several reaper ticks: this must fail if the release ignores the
    # timer, not merely if it happens inside remove_camera.
    time.sleep(TICKS)
    assert r.engine._pipeline is not None, "released before the timer expired"


def test_a_domain_change_does_not_unload(rig):
    """A remove followed by an add inside the timer is one edit, not an idle
    period. camera-mgmt reconciles from four uvicorn workers, so this is the
    normal case rather than a corner."""
    r = rig()
    _warm(r)
    pipeline = r.engine._pipeline

    r.engine.remove_camera("cam-1")
    r.engine.add_camera("cam-1", "rtsp://x/cam-1", ("person",))
    time.sleep(IDLE_TIMEOUT + POLL * 6)

    assert r.engine._pipeline is pipeline, "an ordinary edit unloaded the models"
    assert r.calls["detector"] == 1, "an ordinary edit reloaded the weights"


def test_hibernation_off_holds_the_models(rig):
    """0 keeps the pre-hibernation behaviour, which is what a permanently busy
    site wants. It must be a real off switch, not a very long timer."""
    r = rig(idle_timeout=0.0)
    _warm(r)
    r.engine.remove_camera("cam-1")
    time.sleep(TICKS)
    assert r.engine._pipeline is not None
    assert r.engine.snapshot()["lifecycle"]["hibernation"]["enabled"] is False


def test_a_camera_coming_back_warms_up_again(rig):
    r = rig()
    _warm(r)
    r.engine.remove_camera("cam-1")
    assert r.settle(lambda: r.engine._pipeline is None)

    _warm(r, "cam-2")
    assert r.calls["detector"] == 2
    snap = r.engine.snapshot()
    assert snap["lifecycle"]["hibernation"]["warmups"] == 2
    assert snap["lifecycle"]["hibernation"]["hibernations"] == 1


def test_counters_survive_the_hibernation(rig):
    """An operator watching /health must not see throughput walk backwards
    every time a site goes quiet overnight — that is indistinguishable from a
    crash loop."""
    r = rig()
    _warm(r)
    before = r.engine.snapshot()["lifecycle"]["lifetime"]["frames_processed"]
    assert before == 7

    r.engine.remove_camera("cam-1")
    assert r.settle(lambda: r.engine._pipeline is None)
    assert r.engine.snapshot()["lifecycle"]["lifetime"]["frames_processed"] == 7

    _warm(r, "cam-2")
    assert r.engine.snapshot()["lifecycle"]["lifetime"]["frames_processed"] == 14


def test_a_failed_warm_up_is_retried(rig, monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no CUDA device")
        return FakeDetector()

    monkeypatch.setattr(detector_mod, "build_selected", flaky)
    r = rig()
    r.engine.add_camera("cam-1", "rtsp://x/1", ("person",))
    assert r.settle(lambda: r.engine._state == FAILED)
    assert r.engine.snapshot()["last_error"].startswith("RuntimeError")
    assert r.settle(lambda: r.engine._pipeline is not None), "never retried"
    assert r.engine.snapshot()["last_error"] is None


# ── ANPR demand ──────────────────────────────────────────────────────────────

def test_no_camera_wants_plates_so_no_reader_is_loaded(rig):
    """THE SECOND HALF OF THE BUG. plates.build answers to configuration
    alone, so an appliance with ANPR switched off on every camera still loaded
    the localiser and the OCR model — a second model over every vehicle,
    resident for a domain nobody asked for."""
    r = rig()
    _warm(r, "cam-1", ("person", "vehicles"))
    assert r.calls["plates"] == 0, "loaded the plate reader nobody asked for"
    assert r.engine._plates is None
    models = r.engine.snapshot()["models"]
    assert models["plates_wanted"] is False
    assert models["plates_loaded"] is False
    assert models["plates_load_failed"] is False


def test_a_plate_camera_loads_the_reader(rig):
    r = rig()
    _warm(r, "gate", ("person", "vehicles", "plate"))
    assert r.settle(lambda: r.engine._plates is not None)
    models = r.engine.snapshot()["models"]
    assert models["plates_wanted"] is True
    assert models["plates_active"] is True
    assert r.engine._pipeline.plate_reader is not None


def test_switching_anpr_on_later_loads_the_reader(rig):
    """The reader must follow demand, not only the first camera's domains."""
    r = rig()
    _warm(r, "gate", ("person", "vehicles"))
    assert r.calls["plates"] == 0

    r.engine.add_camera("gate", "rtsp://x/gate", ("person", "vehicles", "plate"))
    assert r.settle(lambda: r.engine._plates is not None), \
        "turning ANPR on did not load the reader"
    assert r.engine._pipeline.plate_reader is not None


def test_dropping_the_last_plate_camera_releases_only_the_reader(rig):
    """A site running ANPR on one gate and plain detection everywhere else
    must get the OCR model back when that gate's ANPR goes off — without
    disturbing the detection the other cameras are still paying for."""
    r = rig()
    _warm(r, "gate", ("person", "vehicles", "plate"))
    _warm(r, "yard", ("person",))
    assert r.settle(lambda: r.engine._plates is not None)
    pipeline = r.engine._pipeline

    r.engine.add_camera("gate", "rtsp://x/gate", ("person", "vehicles"))
    assert r.settle(lambda: r.engine._plates is None), \
        "the plate reader outlived the last camera that wanted it"

    assert r.engine._pipeline is pipeline, "the detector was torn down too"
    assert r.engine._detector is not None
    assert pipeline.plate_reader is None
    assert pipeline.stopped == 0, "the worker was restarted for a plates change"


def test_the_plate_readers_clock_is_visible_while_it_runs(rig):
    """With cameras registered the DETECTOR's clock is null, so if the plate
    reader's own countdown were not reported an operator who has just switched
    ANPR off everywhere would see a loaded reader and nothing explaining why."""
    r = rig(idle_timeout=30.0)
    _warm(r, "gate", ("person", "plate"))
    assert r.settle(lambda: r.engine._plates is not None)

    r.engine.add_camera("gate", "rtsp://x/gate", ("person",))
    hib = r.engine.snapshot()["lifecycle"]["hibernation"]
    assert hib["idle_for_seconds"] is None, "a camera is registered"
    assert hib["plates_idle_for_seconds"] is not None, \
        "the plate reader is counting down with nothing to show for it"


def test_demand_lost_during_the_load_still_releases_the_reader(rig, monkeypatch):
    """THE CLOCK MUST BE ARMED FROM DEMAND, NOT FROM THE INSTALL.

    The plate build takes seconds, and ANPR can be switched off during them. The
    install then lands a reader nobody wants — and if it stamps "not idle" on
    the way in, the only things that re-arm the countdown are add_camera and
    remove_camera. On a fleet that is otherwise stable those never come, so the
    reader is held for ever: exactly the leak this whole change exists to fix,
    reintroduced through the back door.
    """
    gate = threading.Event()
    entered = threading.Event()
    built = {"n": 0}

    def slow_plates(cfg, device):
        entered.set()
        gate.wait(SETTLE)
        built["n"] += 1
        return FakePlateReader()

    monkeypatch.setattr(plates_mod, "build", slow_plates)
    r = rig()
    r.engine.add_camera("gate", "rtsp://x/gate", ("person", "plate"))
    # WAIT FOR THE BUILD TO ACTUALLY START before withdrawing demand. The
    # warm-up decides what to build under the lock, so without this the main
    # thread can win that race, `want_plates` is False before it is ever read,
    # and the test silently exercises nothing — which is what it did: it passed
    # alone and failed in a loaded suite, four runs in five.
    assert entered.wait(SETTLE), "the plate build never started"

    # ANPR goes off while the reader is still loading, and nothing touches the
    # camera set afterwards.
    r.engine.add_camera("gate", "rtsp://x/gate", ("person",))
    gate.set()

    # ASSERT THE INSTALL FIRST, or this test is vacuous: `_plates is None` is
    # trivially true before the loader has landed, so checking only the end
    # state passes just as happily when the reader was never built at all.
    assert r.settle(lambda: r.engine._plates is not None), \
        "the reader never landed — this test proves nothing without it"
    assert built["n"] == 1

    assert r.settle(lambda: r.engine._plates is None), \
        "a reader installed after its demand vanished was never released"


def test_a_failed_plate_load_is_not_retried_on_every_reassert(rig, monkeypatch):
    """THE SPIN THIS GUARD EXISTS FOR. build() returns None rather than raising
    when the weights cannot be downloaded, and camera-mgmt re-asserts every
    camera once a minute. Clearing the failure flag on demand rather than on
    the TRANSITION into demand retries an egress-blocked download for as long
    as the appliance runs."""
    attempts = {"n": 0}

    def cannot_download(cfg, device):
        attempts["n"] += 1
        return None

    monkeypatch.setattr(plates_mod, "build", cannot_download)
    r = rig()
    _warm(r, "gate", ("person", "vehicles", "plate"))
    assert r.settle(lambda: r.engine._plates_load_failed)

    # Five reconcile passes' worth of re-assertion.
    for _ in range(5):
        r.engine.add_camera("gate", "rtsp://x/gate",
                            ("person", "vehicles", "plate"))
        time.sleep(POLL * 2)

    assert attempts["n"] == 1, (
        f"retried a failing plate download {attempts['n']} times")
    models = r.engine.snapshot()["models"]
    # AND THE HEALTH ANSWER STAYS HONEST: "nobody asked" and "it could not be
    # had" are completely different things to tell someone whose plate search
    # came back empty.
    assert models["plates_wanted"] is True
    assert models["plates_loaded"] is False
    assert models["plates_load_failed"] is True


def test_turning_anpr_off_and_on_again_retries_the_download(rig, monkeypatch):
    """The transition IS a good reason to try again — an operator toggling
    ANPR is asking for exactly that."""
    attempts = {"n": 0}
    monkeypatch.setattr(plates_mod, "build",
                        lambda cfg, device: (attempts.__setitem__(
                            "n", attempts["n"] + 1), None)[1])
    r = rig()
    _warm(r, "gate", ("person", "plate"))
    assert r.settle(lambda: r.engine._plates_load_failed)

    r.engine.add_camera("gate", "rtsp://x/gate", ("person",))
    time.sleep(POLL * 2)
    r.engine.add_camera("gate", "rtsp://x/gate", ("person", "plate"))
    assert r.settle(lambda: attempts["n"] == 2), \
        "switching ANPR off and back on did not retry the load"


# ── locking ──────────────────────────────────────────────────────────────────

def test_concurrent_churn_does_not_deadlock(rig):
    """The reaper takes the engine lock and so does every registry call, and
    the warm-up installs models under it. Adds and removes racing the reaper
    is exactly the traffic a reconcile loop in four workers produces."""
    r = rig()
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn(n: int):
        try:
            while not stop.is_set():
                slug = f"cam-{n}"
                r.engine.add_camera(slug, f"rtsp://x/{slug}",
                                    ("person", "plate") if n % 2 else ("person",))
                r.engine.list_cameras()
                r.engine.snapshot()
                r.engine.remove_camera(slug)
        except BaseException as exc:                               # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=SETTLE)

    assert not any(t.is_alive() for t in threads), "deadlocked under churn"
    assert not errors, f"churn raised: {errors[0]!r}"
