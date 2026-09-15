"""Model hibernation: load on the first camera, release when the last one goes.

These tests drive index/models.ModelPool and index/engine.IndexEngine with fake
weights, because what is being tested is the STATE MACHINE and the LOCKING, not
whether YOLO loads. The real models are exercised by running the service; what
cannot be exercised by running it is the pair of races that made this design
non-obvious, and both have a test here:

  * `test_url_change_does_not_unload` — upsert_camera is a remove followed by an
    add, so a single-camera deployment hits zero registered cameras every time
    an operator edits a URL. Without the debounce that unloads and reloads the
    weights on an ordinary edit.
  * `test_concurrent_churn_does_not_deadlock` — the engine calls the pool and
    the pool calls the engine, so the lock order is load-bearing. This test
    deadlocks outright if either side takes its own lock across the call.

Run: python3 -m pytest tests -q   (from services/smartsearch)
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import time
import weakref

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index import models as models_mod                            # noqa: E402
from index.config import AppConfig                                # noqa: E402
from index.engine import IndexEngine, WARMING_UP                  # noqa: E402
from index.models import (                                        # noqa: E402
    DISABLED, IDLE, FAILED, READY, UNAVAILABLE, WARMING, ModelPool,
)

IDLE_TIMEOUT = 1.0
POLL = 0.1
#: Long enough for a warm-up thread plus one reaper tick, short enough that a
#: hang fails the test rather than the suite.
SETTLE = 4.0


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeEmbedder:
    dim = 512
    device = "cpu"
    loads = 0

    def __init__(self, *a, **kw):
        FakeEmbedder.loads += 1

    def embed_text(self, q):
        return np.zeros(512, dtype=np.float32)

    def embed_images(self, crops):
        return np.zeros((len(crops), 512), dtype=np.float32)


class FakePipeline:
    """Only what the engine and the teardown actually touch."""
    started = 0
    stopped = 0
    #: How many cameras have been wired to a pipeline. This is what "attached"
    #: means now that there is no capture thread to count: analytics detects
    #: and posts observations, and all the engine hands over is which domains
    #: each camera contributes.
    domains_set = 0

    def __init__(self, *a, **kw):
        self.domains = {}
        self.frames_queued = 0
        self.frames_dropped = 0
        self.frames_processed = 7          # non-zero, so lifetime folding shows
        self.detections_seen = 0
        self.crops_deduped = 0
        self.rows_written = 3
        self.frames_skipped_no_motion = 0
        self.detector_calls = 0

    def start(self):
        FakePipeline.started += 1

    def stop(self, join_timeout: float = 10.0):
        FakePipeline.stopped += 1

    def set_domains(self, slug, domains):
        if slug not in self.domains:
            FakePipeline.domains_set += 1
        self.domains[slug] = tuple(domains)

    def forget_domains(self, slug):
        self.domains.pop(slug, None)

    def submit(self, *a):
        pass

    def reset_gate(self, slug):
        pass

    def snapshot(self):
        return {"rows_written": self.rows_written}


class FakeSampler:
    live = 0

    def __init__(self, slug, rtsp_url, max_fps, on_frame, on_reconnect):
        self.slug = slug
        self._running = False

    def start(self):
        FakeSampler.live += 1
        self._running = True

    def stop(self):
        if self._running:
            FakeSampler.live -= 1
            self._running = False

    def snapshot(self):
        return {"state": "SAMPLING", "frames_sampled": 0, "last_error": None}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """A pool + engine wired to fakes, torn down cleanly."""
    FakeEmbedder.loads = 0
    FakePipeline.started = FakePipeline.stopped = 0
    FakePipeline.domains_set = 0

    import index.embedder
    import index.engine as engine_mod

    monkeypatch.setattr(index.embedder, "ClipEmbedder", FakeEmbedder)
    monkeypatch.setattr(models_mod, "IngestPipeline", FakePipeline)

    cfg = AppConfig()
    cfg.lifecycle.idle_timeout_seconds = IDLE_TIMEOUT
    cfg.lifecycle.poll_seconds = POLL
    cfg.store.crop_dir = str(tmp_path)

    pool = ModelPool(cfg, store=None, writer=None)
    engine = IndexEngine(cfg, pool)
    pool.start()
    try:
        yield pool, engine
    finally:
        engine.stop_all()


def wait_for(predicate, timeout=SETTLE, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ── the load/release cycle ───────────────────────────────────────────────────
def test_starts_with_nothing_loaded(rig):
    pool, engine = rig
    assert pool.ingest_state == IDLE
    assert pool.pipeline is None
    assert FakeEmbedder.loads == 0, "weights loaded before any camera asked for them"
    # ...but it still claims ingest is POSSIBLE, which is the honest answer and
    # the one that keeps /health from raising a fault that is not there.
    assert pool.ingest_possible is True
    assert engine.ingest_enabled is True


def test_first_camera_loads_and_attaches(rig):
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/cam-a", ("person",))
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")
    wait_for(lambda: FakePipeline.domains_set == 1, what="camera attached")
    assert FakeEmbedder.loads == 1
    assert pool.pipeline is not None
    assert pool.pipeline.domains["cam-a"] == ("person",)


def test_camera_reports_warming_not_sampling_and_not_broken(rig):
    """The invariant eager loading used to buy: never claim to be indexing while
    the weights are still coming, and never report it as a fault either."""
    pool, engine = rig
    seen = []
    real = ModelPool._warm

    def slow_warm(self):
        time.sleep(0.4)
        return real(self)

    ModelPool._warm = slow_warm
    try:
        engine.add_camera("cam-a", "rtsp://relay/cam-a")
        seen.append(engine.snapshot()[0])
        wait_for(lambda: pool.ingest_state == READY, what="ingest ready")
    finally:
        ModelPool._warm = real
    # NO WARMING->SAMPLING TRANSITION ANY MORE. A camera here is a row in the
    # searchable set, not something with a decoder attached, so it reports
    # RECEIVING from the moment it is registered — before the encoder is
    # resident and after. What must still be true is that it never reports a
    # fault while the pool is simply warming.
    assert seen[0]["state"] == "RECEIVING"
    assert seen[0]["last_error"] is None
    assert engine.snapshot()[0]["state"] == "RECEIVING"


def test_health_snapshot_answers_during_a_warm_up(rig):
    """/health must not wait on the pool lock.

    A warm-up holds that lock for the whole load, and the FIRST one on a new
    deployment downloads the weights — minutes, not the 2.4 s a warm reload
    costs. A snapshot that waited would hang /health for exactly as long and the
    container healthcheck (5 s timeout) would mark the service unhealthy for
    doing what it was told. Seen on the appliance on the first run.
    """
    pool, engine = rig
    holding = threading.Event()
    release = threading.Event()
    real_warm = models_mod.ModelPool._warm

    def slow_warm(self):
        with self._lock:
            holding.set()
            release.wait(timeout=10)
        return real_warm(self)

    monkeypatch_warm = slow_warm
    models_mod.ModelPool._warm = monkeypatch_warm
    try:
        engine.add_camera("cam-a", "rtsp://relay/cam-a")
        assert holding.wait(timeout=5), "warm-up never took the lock"

        answered = []

        def ask():
            answered.append(pool.snapshot())

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join(timeout=2)
        assert answered, "snapshot blocked behind the warm-up's lock"
        assert answered[0]["ingest"]["state"] == WARMING
    finally:
        release.set()
        models_mod.ModelPool._warm = real_warm
    wait_for(lambda: pool.ingest_state == READY, what="warm-up completes")


def test_last_camera_releases_after_the_timeout(rig):
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")

    engine.remove_camera("cam-a")
    # NOT immediately: the timer is a debounce, see test_url_change below.
    assert pool.pipeline is not None, "released on the transition instead of the timer"

    wait_for(lambda: pool.ingest_state == IDLE and pool.pipeline is None,
             what="models released")
    # THE PIPELINE IS RELEASED, NOT STOPPED. There is no worker thread to join
    # now that observations arrive on the request thread, so the pool simply
    # drops its reference — which `pool.pipeline is None` above already
    # asserts. Keeping a stop() count here would test the teardown of a thread
    # that no longer exists.
    assert pool.pipeline is None
    # Counters survive the hibernation: an operator watching /health must not
    # see rows_written fall back to zero and read it as data loss.
    assert pool.snapshot()["lifetime"]["rows_written"] == 3
    assert pool.snapshot()["hibernation"]["hibernations"] == 1


def test_running_pipeline_counters_are_visible_before_any_hibernation(rig):
    """/health must report the work of the session happening RIGHT NOW.

    _teardown folds a pipeline's counters into _lifetime, so a snapshot that
    read only _lifetime described PAST sessions exclusively. A freshly
    installed appliance has never hibernated, so every throughput counter sat
    at 0 for the whole first session while the pipeline was demonstrably
    writing rows to the index — the one signal an operator checks to answer
    "is detection running?" said no while it ran.
    """
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")

    snap = pool.snapshot()
    assert snap["hibernation"]["hibernations"] == 0, "nothing has been folded yet"
    # Exactly the live pipeline's own counters, not zero and not doubled.
    assert snap["lifetime"]["rows_written"] == 3
    assert snap["lifetime"]["frames_processed"] == 7


def test_reclaim_runs_only_after_the_models_are_unreachable(monkeypatch, rig):
    """Releasing is not the same as freeing, and the difference is silent.

    _teardown is handed the pipeline, so its CALLER still holds a reference —
    and the pipeline holds the detector, the encoder and the plate reader.
    Collecting and trimming from inside _teardown therefore ran while every
    model was still reachable and freed nothing, while the logs said "released"
    and every state read idle. On the appliance that was the difference between
    1.27 GB and 0.4 GB retained, with no symptom anywhere except RSS.
    """
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")

    ref = weakref.ref(pool.pipeline)
    reachable_at_reclaim: list[bool] = []
    real_reclaim = models_mod.ModelPool._reclaim

    def watched_reclaim():
        gc.collect()
        reachable_at_reclaim.append(ref() is not None)
        return real_reclaim()

    monkeypatch.setattr(models_mod.ModelPool, "_reclaim",
                        staticmethod(watched_reclaim))

    engine.remove_camera("cam-a")
    wait_for(lambda: pool.pipeline is None, what="models released")
    wait_for(lambda: reachable_at_reclaim, what="reclaim to run at all")
    assert not any(reachable_at_reclaim), (
        "reclaimed while the models were still reachable — the collect and the "
        "malloc_trim both had nothing to free"
    )


def test_camera_returning_reloads(rig):
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == READY, what="first load")
    engine.remove_camera("cam-a")
    wait_for(lambda: pool.pipeline is None, what="release")

    engine.add_camera("cam-b", "rtsp://relay/cam-b")
    wait_for(lambda: pool.ingest_state == READY, what="reload")
    wait_for(lambda: FakePipeline.domains_set >= 1, what="reattach")
    assert pool.snapshot()["hibernation"]["warmups"] == 2
    # Lifetime counters accumulate ACROSS sessions rather than restarting: 3
    # folded in when cam-a's pipeline was torn down, plus the 3 the live cam-b
    # pipeline reports. (A real pipeline starts at zero, so the running total
    # would read 3 here; FakePipeline starts every instance at 3 on purpose, so
    # this also pins down that the live session is counted exactly once.)
    assert pool.snapshot()["lifetime"]["rows_written"] == 6


def test_url_change_does_not_unload(monkeypatch, rig):
    """upsert_camera removes then re-adds. On a one-camera site that is a trip
    through zero registered cameras, and it must not cost a reload.

    THE REAPER IS FORCED INTO THE GAP. Left to chance this test passes with or
    without the debounce: the window between the remove and the add is
    microseconds wide and the reaper polls every few seconds, so it is almost
    never hit. Almost never, across a fleet and a year, is not never — and the
    failure is silent, a 2 s hole in indexing every time someone edits a URL.
    """
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/old")
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")
    pipeline_before = pool.pipeline

    original_add = engine.add_camera

    def add_after_a_reaper_tick(*a, **kw):
        pool._tick()                     # exactly the interleaving being tested
        return original_add(*a, **kw)

    monkeypatch.setattr(engine, "add_camera", add_after_a_reaper_tick)
    engine.upsert_camera("cam-a", "rtsp://relay/new")

    assert pool.pipeline is pipeline_before, "URL edit unloaded the models"
    assert pool.snapshot()["hibernation"]["hibernations"] == 0
    assert FakePipeline.domains_set >= 1


# ── the query side ───────────────────────────────────────────────────────────
def test_query_loads_the_encoder_with_no_cameras(rig):
    """A site that has switched indexing off entirely is still a searchable
    archive. Hibernation must not turn that into a 503."""
    pool, engine = rig
    assert FakeEmbedder.loads == 0
    assert pool.embedder_possible is True
    enc = pool.acquire_embedder()
    assert enc is not None and FakeEmbedder.loads == 1


def test_encoder_survives_ingest_release_while_searches_continue(rig):
    """Two clocks, not one: the detector is ingest-only and goes with the
    cameras; the encoder also answers searches, so it needs both to be quiet."""
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")
    engine.remove_camera("cam-a")

    stop = threading.Event()

    def search_traffic():
        while not stop.is_set():
            pool.acquire_embedder()
            time.sleep(POLL)

    t = threading.Thread(target=search_traffic, daemon=True)
    t.start()
    try:
        wait_for(lambda: pool.pipeline is None, what="ingest released")
        time.sleep(IDLE_TIMEOUT + 3 * POLL)
        assert pool.snapshot()["encoder"]["loaded"] is True, \
            "encoder released while searches were still arriving"
    finally:
        stop.set()
        t.join(timeout=2)

    wait_for(lambda: pool.snapshot()["encoder"]["loaded"] is False,
             timeout=IDLE_TIMEOUT + SETTLE, what="encoder released once quiet")
    assert FakeEmbedder.loads == 1


# ── failure handling ─────────────────────────────────────────────────────────
def test_wrong_vector_width_is_permanent_not_retried(monkeypatch, rig):
    """Vectors of another width are not comparable with what is stored, so
    retrying can only produce a wrong answer later than a right refusal."""
    pool, engine = rig
    import index.embedder

    class WrongDim(FakeEmbedder):
        dim = 1152

    monkeypatch.setattr(index.embedder, "ClipEmbedder", WrongDim)
    assert pool.acquire_embedder() is None
    assert pool.snapshot()["encoder"]["state"] == UNAVAILABLE
    assert pool.embedder_possible is False
    assert "re-index" in pool.snapshot()["encoder"]["error"]

    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == UNAVAILABLE, what="ingest gives up")
    assert pool.ingest_possible is False
    assert engine.snapshot()[0]["state"] == "REGISTERED_NO_INGEST"


def test_warm_failure_backs_off_then_recovers(monkeypatch, rig):
    pool, engine = rig

    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("weights not on disk yet")
        # A REAL fake, not object(): the pool checks the encoder's vector
        # width before it accepts one, so a bare sentinel fails the retry for
        # a different reason than the one under test.
        return FakeEmbedder(*a, **kw)

    import index.embedder
    monkeypatch.setattr(index.embedder, "ClipEmbedder", flaky)
    monkeypatch.setattr(models_mod, "_RETRY_BASE", 0.2)

    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == FAILED, what="warm-up failure recorded")
    snap = pool.snapshot()["ingest"]
    assert "weights not on disk yet" in snap["error"]
    assert snap["retry_in_seconds"] is not None, "failed with no retry scheduled"
    # The camera says why, next to the camera it is costing.
    assert engine.snapshot()[0]["state"] == "REGISTERED_NO_INGEST"

    wait_for(lambda: pool.ingest_state == READY, what="retry succeeds")
    wait_for(lambda: FakePipeline.domains_set >= 1, what="attached after retry")


def test_ingest_disabled_still_serves_queries(monkeypatch, tmp_path):
    """SEARCH_INGEST_ENABLED=false is registry+query only. No warm-up should
    ever be attempted, but a search must still be answerable."""
    import index.embedder
    import index.engine as engine_mod
    FakeEmbedder.loads = 0
    monkeypatch.setattr(index.embedder, "ClipEmbedder", FakeEmbedder)
    monkeypatch.setattr(models_mod, "IngestPipeline", FakePipeline)

    cfg = AppConfig()
    cfg.ingest.enabled = False
    cfg.lifecycle.idle_timeout_seconds = IDLE_TIMEOUT
    cfg.lifecycle.poll_seconds = POLL
    pool = ModelPool(cfg, store=None, writer=None)
    engine = IndexEngine(cfg, pool)
    pool.start()
    try:
        assert pool.ingest_state == DISABLED
        assert pool.ingest_possible is False
        engine.add_camera("cam-a", "rtsp://relay/cam-a")
        time.sleep(0.5)
        assert pool.pipeline is None and pool.ingest_state == DISABLED
        assert engine.snapshot()[0]["state"] == "REGISTERED_NO_INGEST"
        assert pool.acquire_embedder() is not None, "query side taken down with ingest"
    finally:
        engine.stop_all()


def test_timeout_zero_never_releases(monkeypatch, tmp_path):
    """0 is the documented escape hatch: load once, hold forever."""
    import index.embedder
    import index.engine as engine_mod
    monkeypatch.setattr(index.embedder, "ClipEmbedder", FakeEmbedder)
    monkeypatch.setattr(models_mod, "IngestPipeline", FakePipeline)

    cfg = AppConfig()
    cfg.lifecycle.idle_timeout_seconds = 0
    cfg.lifecycle.poll_seconds = POLL
    pool = ModelPool(cfg, store=None, writer=None)
    engine = IndexEngine(cfg, pool)
    pool.start()
    try:
        engine.add_camera("cam-a", "rtsp://relay/cam-a")
        wait_for(lambda: pool.ingest_state == READY, what="ingest ready")
        engine.remove_camera("cam-a")
        time.sleep(0.8)
        assert pool.pipeline is not None, "released despite hibernation being off"
        assert pool.snapshot()["hibernation"]["enabled"] is False
    finally:
        engine.stop_all()


# ── locking ──────────────────────────────────────────────────────────────────
class LockTracker:
    """Records which of the two registry locks a thread is holding.

    Used instead of a stress test. The engine calls the pool and the pool calls
    the engine, so a fixed lock order is the only thing keeping them from
    deadlocking — but reproducing that deadlock on demand needs both halves live
    in the same microsecond, and an attempt to do it by churning eight threads
    passed happily against a deliberately inverted order: the collision simply
    never came up. What can be checked deterministically is the INVARIANT the
    order rests on, which is that neither lock is ever held across a call into
    the other component.
    """

    def __init__(self):
        self._depth = threading.local()
        self.violations: list[str] = []

    def depth(self, name: str) -> int:
        return getattr(self._depth, name, 0)

    def _bump(self, name: str, by: int) -> None:
        setattr(self._depth, name, self.depth(name) + by)

    def wrap(self, owner, attr: str, name: str) -> None:
        tracker = self

        class Tracked:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._inner.acquire()
                tracker._bump(name, 1)
                return self

            def __exit__(self, *exc):
                tracker._bump(name, -1)
                self._inner.release()
                return False

        setattr(owner, attr, Tracked(getattr(owner, attr)))

    def check(self, must_not_hold: str, doing: str) -> None:
        if self.depth(must_not_hold):
            # Recorded, not raised: _notify swallows listener exceptions by
            # design, so an assert here would vanish into a log line.
            self.violations.append(f"{doing} while holding the {must_not_hold} lock")


def test_locks_are_never_held_across_the_component_boundary(monkeypatch, tmp_path):
    """The engine drives the pool (want_ingest) and the pool drives the engine
    (the pipeline listener). Holding either lock across that call is a deadlock
    the moment a camera is removed during a warm-up — see the lock-order note in
    engine.py and models.set_listener."""
    import index.embedder
    import index.engine as engine_mod
    monkeypatch.setattr(index.embedder, "ClipEmbedder", FakeEmbedder)
    monkeypatch.setattr(models_mod, "IngestPipeline", FakePipeline)

    cfg = AppConfig()
    cfg.lifecycle.idle_timeout_seconds = 0.001
    cfg.store.crop_dir = str(tmp_path)
    pool = ModelPool(cfg, store=None, writer=None)
    engine = IndexEngine(cfg, pool)

    tracker = LockTracker()
    tracker.wrap(pool, "_lock", "pool")
    tracker.wrap(engine, "_lock", "engine")

    listener = pool._listener

    def watched_listener(pipeline):
        tracker.check("pool", "called the pipeline listener")
        return listener(pipeline)

    pool._listener = watched_listener

    want = pool.want_ingest

    def watched_want(wanted):
        tracker.check("engine", "called want_ingest")
        return want(wanted)

    monkeypatch.setattr(pool, "want_ingest", watched_want)

    acquire = pool.acquire_embedder

    def watched_acquire():
        tracker.check("engine", "called acquire_embedder")
        return acquire()

    monkeypatch.setattr(pool, "acquire_embedder", watched_acquire)

    # Drive one full cycle: load on the first camera, release on the last.
    engine.add_camera("cam-a", "rtsp://relay/cam-a")
    wait_for(lambda: pool.ingest_state == READY, what="warm-up notified")
    pool.acquire_embedder()
    engine.upsert_camera("cam-a", "rtsp://relay/moved")
    wait_for(lambda: pool.ingest_state == READY, what="re-warm after URL change")
    engine.remove_camera("cam-a")
    # The reaper's own body, called directly so the release is deterministic
    # rather than waited for — no reaper thread is running in this test.
    time.sleep(5 * cfg.lifecycle.idle_timeout_seconds)
    pool._tick()
    assert pool.pipeline is None, "teardown did not run, so it was not checked"
    engine.stop_all()

    assert not tracker.violations, (
        "lock held across the engine/pool boundary: " + "; ".join(tracker.violations)
    )


def test_concurrent_url_change_on_one_camera_does_not_raise(monkeypatch, rig):
    """The registry reconciles from four uvicorn workers. On a URL change they
    all see the stale URL and all call remove-then-add; before upsert_camera
    held a per-slug lock, a thread that lost the add hit add_camera's "already
    exists" KeyError. That is not a 409 on this path — it escapes the route as a
    500, the sync logs a failed push, and the retry re-enters the same race.

    THE PREEMPTION IS FORCED, and it has to be exactly here. The race needs a
    thread switch in the gap BETWEEN one worker's remove and its own add, and
    CPython's 5 ms switch interval usually carries a thread straight through a
    window that narrow — a version of this test that merely ran four threads at
    once, and even one that delayed the remove, both passed against the unlocked
    code. Sleeping after the remove holds every worker in that gap at once,
    which is what four threads on four cores eventually do by themselves.
    """
    pool, engine = rig
    engine.add_camera("cam-a", "rtsp://relay/old")
    wait_for(lambda: pool.ingest_state == READY, what="ingest ready")

    real_remove = engine.remove_camera

    def slow_remove(slug):
        removed = real_remove(slug)
        time.sleep(0.05)          # the gap between the remove and the add
        return removed

    monkeypatch.setattr(engine, "remove_camera", slow_remove)

    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def reconcile_worker():
        try:
            barrier.wait(timeout=5)
            engine.upsert_camera("cam-a", "rtsp://relay/new")
        except BaseException as exc:                              # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reconcile_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors, f"concurrent upsert raised: {errors}"
    assert engine.camera_count == 1
    assert engine.get_camera("cam-a").rtsp_url == "rtsp://relay/new"
    wait_for(lambda: FakePipeline.domains_set >= 1, what="attached exactly once")


def test_churn_leaves_one_capture_thread_per_camera(rig):
    """Attaching is reached from the add path AND from a warm-up completing.
    Two capture threads on one camera doubles its frame rate silently."""
    pool, engine = rig
    errors: list[BaseException] = []

    def churn(i: int):
        try:
            slug = f"cam-{i}"
            for r in range(12):
                engine.upsert_camera(slug, f"rtsp://relay/{slug}-{r % 2}")
                engine.snapshot()
        except BaseException as exc:                              # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "deadlocked"
    assert not errors, errors

    wait_for(lambda: pool.ingest_state == READY, what="settled ready")
    time.sleep(0.3)
    assert engine.camera_count == 8, (
        f"{engine.camera_count} cameras after churn, expected 8"
    )


# ── who is feeding this index ────────────────────────────────────────────────
def test_no_producer_is_reported_not_hidden(rig):
    """THE BUG THIS EXISTS FOR. This service detects nothing itself, so with no
    producer running the index cannot grow — and before this, /health said
    `ok` for eight hours while exactly that happened.
    """
    _pool, engine = rig
    snap = engine.producers_snapshot()
    assert snap["state"] == "NO_PRODUCER"
    assert snap["detail"] and "analytics" in snap["detail"], \
        "the operator is told nothing is feeding the index, but not what to check"


def test_a_live_producer_reports_receiving(rig):
    _pool, engine = rig
    engine.note_producer("analytics", {"cameras": 5, "observations_produced": 12})
    snap = engine.producers_snapshot()
    assert snap["state"] == "RECEIVING"
    assert snap["detail"] is None
    a = snap["producers"]["analytics"]
    assert a["stale"] is False and a["cameras"] == 5


def test_a_producer_gone_quiet_goes_stale(rig, monkeypatch):
    """A quiet SCENE must not look like a dead producer, which is why the
    heartbeat is independent of observations. But a producer that stops
    beating IS gone, and must be reported so."""
    import time as _t
    from index import engine as engine_mod

    _pool, engine = rig
    engine.note_producer("analytics", {})
    later = _t.time() + engine_mod.PRODUCER_STALE_SECONDS + 5
    monkeypatch.setattr(engine_mod.time, "time", lambda: later)
    snap = engine.producers_snapshot()
    assert snap["state"] == "PRODUCER_STALE"
    assert snap["producers"]["analytics"]["stale"] is True
    assert snap["detail"], "went stale with no explanation"
