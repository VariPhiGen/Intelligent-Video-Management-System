"""engine.py + worker.py — the registry and the publish that feeds both consumers.

The frames broker exists because this loop used to run TWICE: the motion service
decoded a relay to get a scalar, SmartSearch's sampler decoded the same relay to
get frames, and their frames never lined up. Now one thread per camera decodes
once and publishes once, and both consumers read the same pixels.

That makes two things load-bearing, and neither had a test.

THE PUBLISH ORDER. The frame goes into shared memory FIRST, is analysed second,
and the notice goes out THIRD. A consumer that hears about a frame before it is
written reads the previous frame — or a torn one — for every sample, and nothing
about that is visible from either side. `_publish` says the order matters; these
tests are what stops it being reordered by someone tidying it.

THE RING IDENTITY. A camera that changes resolution mid-session (a profile
switch, a re-probe) must get a new ring. Publishing a 1080p frame into a 720p
ring is the corruption that produces "half the image is last week".

Hermetic: the ring, motion analyser and notice publisher are stubs; no OpenCV
capture, no /dev/shm, no camera.

Run (from services/frames): python -m pytest tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from broker.config import AppConfig, CaptureConfig, NoticeConfig  # noqa: E402
from broker.engine import FrameEngine  # noqa: E402
from broker.worker import CameraWorker  # noqa: E402


def frame(w=640, h=480):
    return np.zeros((h, w, 3), dtype=np.uint8)


class StubRing:
    def __init__(self, width=640, height=480):
        self.width, self.height = width, height
        self.channels, self.slots, self.epoch = 3, 8, 1
        self.path = "/dev/shm/stub"
        self.published: list = []

    def publish(self, frame, ts):
        self.published.append((frame.shape, ts))
        return len(self.published), len(self.published) % self.slots

    def snapshot(self):
        return {"ring_bytes": self.width * self.height * 3 * self.slots}

    def close(self):
        pass


class Recorder:
    """One list, so ordering across collaborators is directly assertable."""

    def __init__(self):
        self.events: list[str] = []


class StubMotion:
    def __init__(self, rec):
        self._rec = rec
        self.resets = 0

    def analyse(self, frame):
        self._rec.events.append("analyse")
        return SimpleNamespace(to_dict=lambda: {"score": 0.0})

    def reset(self):
        self.resets += 1

    def snapshot(self):
        return {"score": 0.0}


class StubPublisher:
    def __init__(self, rec=None, raises=None):
        self._rec = rec
        self.notices: list = []
        self._raises = raises

    def publish(self, camera, notice):
        if self._rec:
            self._rec.events.append("notice")
        if self._raises:
            raise self._raises
        self.notices.append((camera, notice))

    def snapshot(self):
        return {"published": len(self.notices)}


def config(sample_fps=2.0, ring_slots=8):
    """The REAL AppConfig, with the notice publisher switched off.

    Using the real dataclass rather than a stand-in means these tests also
    fail if a field the worker reads is renamed — which a SimpleNamespace
    would happily absorb. `notice.enabled=False` is what keeps it off Valkey.
    """
    return AppConfig(
        capture=CaptureConfig(sample_fps=sample_fps, ring_slots=ring_slots,
                              reconnect_backoff_initial_seconds=1.0,
                              reconnect_backoff_max_seconds=30.0),
        notice=NoticeConfig(enabled=False),
    )


def worker(rec=None, pub=None, ring=None):
    rec = rec or Recorder()
    w = CameraWorker("gate-a1b2", "rtsp://relay/gate", config(),
                     pub or StubPublisher(rec))
    w.motion = StubMotion(rec)
    if ring is not None:
        w._ring = ring
    return w, rec


# ── The publish order, which both consumers depend on ──────────────────────

class TestPublishOrder:
    def test_the_frame_is_in_shared_memory_before_anyone_is_told(self):
        # A consumer that hears about a frame before it is written reads the
        # previous one, every sample, invisibly from both sides.
        rec = Recorder()
        ring = StubRing()

        class OrderedRing(StubRing):
            def publish(self, frame, ts):
                rec.events.append("shm")
                return 1, 0

        w, _ = worker(rec=rec, ring=OrderedRing())
        w._publish(frame(), 1000.0)
        assert rec.events == ["shm", "analyse", "notice"], (
            f"the publish order changed: {rec.events}"
        )

    def test_motion_is_analysed_on_the_frame_that_was_just_written(self):
        ring = StubRing()
        w, _ = worker(ring=ring)
        f = frame(320, 240)
        w._ring = StubRing(320, 240)
        w._publish(f, 1000.0)
        assert w._ring.published[0][0] == f.shape

    def test_the_notice_carries_the_sequence_and_slot_the_ring_returned(self):
        pub = StubPublisher()
        w, _ = worker(pub=pub, ring=StubRing())
        w._publish(frame(), 1000.0)
        _, notice = pub.notices[0]
        assert notice["seq"] == 1
        assert notice["slot"] == 1 % 8

    def test_the_notice_describes_the_ring_geometry(self):
        # A consumer maps shared memory from these numbers; a mismatch is a
        # misread buffer rather than an error.
        pub = StubPublisher()
        w, _ = worker(pub=pub, ring=StubRing(1920, 1080))
        w._publish(frame(1920, 1080), 1000.0)
        _, notice = pub.notices[0]
        assert (notice["width"], notice["height"]) == (1920, 1080)
        assert notice["channels"] == 3 and notice["ring_slots"] == 8

    def test_the_notice_carries_the_ring_epoch(self):
        # WHICH RING THIS SLOT IS IN. After a broker restart the path is
        # reused, so a consumer holding the previous mmap reads a frozen image
        # whose sequence never advances — blind, while still reporting its ring
        # mapped. The epoch is what makes it remap.
        pub = StubPublisher()
        w, _ = worker(pub=pub, ring=StubRing())
        w._publish(frame(), 1000.0)
        assert pub.notices[0][1]["epoch"] == 1

    def test_the_notice_carries_one_timebase_for_every_consumer(self):
        # `ts` is wall-clock seconds and is the only timebase: every consumer,
        # log line and stored row uses it, so a frame can be traced end to end.
        pub = StubPublisher()
        w, _ = worker(pub=pub, ring=StubRing())
        w._publish(frame(), 1757500000.5)
        assert pub.notices[0][1]["ts"] == 1757500000.5

    def test_one_notice_serves_both_consumers(self):
        # Motion reads motion.fraction and ignores the geometry; SmartSearch
        # reads the shm coordinates and ignores the scalar. Keeping it ONE
        # message is what guarantees they describe the same frame.
        pub = StubPublisher()
        w, _ = worker(pub=pub, ring=StubRing())
        w._publish(frame(), 1000.0)
        n = pub.notices[0][1]
        assert "motion" in n and "shm" in n and "seq" in n

    def test_a_successful_publish_is_counted(self):
        w, _ = worker(ring=StubRing())
        w._publish(frame(), 1000.0)
        assert w.frames_published == 1


class TestRingIdentity:
    def test_a_frame_matching_the_ring_reuses_it(self):
        ring = StubRing(640, 480)
        w, _ = worker(ring=ring)
        w._publish(frame(640, 480), 1000.0)
        assert w._ring is ring

    def test_a_resolution_change_gets_a_new_ring(self):
        # A profile switch or a re-probe changes the frame size mid-session.
        # Publishing 1080p into a 720p ring is the corruption that shows up as
        # half an image from a previous frame.
        ring = StubRing(640, 480)
        w, _ = worker(ring=ring)
        made: list = []

        def _ensure(f):
            new = StubRing(f.shape[1], f.shape[0])
            made.append(new)
            w._ring = new
            return new

        w._ensure_ring = _ensure
        w._publish(frame(1920, 1080), 1000.0)
        assert made, "the ring was reused for a different resolution"
        assert (w._ring.width, w._ring.height) == (1920, 1080)

    def test_the_first_frame_creates_a_ring(self):
        w, _ = worker()
        assert w._ring is None
        made: list = []
        w._ensure_ring = lambda f: (made.append(f.shape) or StubRing())
        w._publish(frame(), 1000.0)
        assert made


class TestSnapshot:
    def test_a_worker_reports_its_counters(self):
        w, _ = worker(ring=StubRing())
        w._publish(frame(), 1000.0)
        w.frames_decoded = 5
        snap = w.snapshot()
        assert snap["camera"] == "gate-a1b2"
        assert snap["frames_published"] == 1
        assert snap["frames_decoded"] == 5

    def test_a_worker_with_no_ring_yet_still_snapshots(self):
        # Called by /health during the connecting window; raising there would
        # take the health endpoint down for one camera being slow to open.
        w, _ = worker()
        snap = w.snapshot()
        assert "ring" not in snap
        assert snap["state"] in ("CONNECTING", "STARTING", "STOPPED", "STREAMING")


# ── The registry ───────────────────────────────────────────────────────────

class StubWorker:
    def __init__(self, camera, rtsp_url, cfg, pub):
        self.camera, self.rtsp_url = camera, rtsp_url
        self.started = self.stopped = 0
        self.state = "STREAMING"

    def start(self):
        self.started += 1

    def stop(self, timeout=5.0):
        self.stopped += 1

    def snapshot(self):
        return {"camera": self.camera, "state": self.state,
                "ring": {"ring_bytes": 10_000_000}}


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr("broker.engine.CameraWorker", StubWorker)
    monkeypatch.setattr("broker.engine.shm_capacity",
                        lambda: {"total_bytes": 64_000_000, "total_mb": 64})
    return FrameEngine(config())


class TestRegistry:
    def test_it_starts_with_no_cameras(self, engine):
        # camera-mgmt pushes them at runtime; a restart self-heals within one
        # sync interval.
        assert engine.snapshot() == []

    def test_adding_a_camera_starts_its_worker(self, engine):
        w = engine.add_camera("gate-a1b2", "rtsp://relay/gate")
        assert w.started == 1
        assert engine.get("gate-a1b2") is w

    def test_adding_the_same_camera_twice_is_refused(self, engine):
        engine.add_camera("gate-a1b2", "rtsp://relay/gate")
        with pytest.raises(KeyError):
            engine.add_camera("gate-a1b2", "rtsp://relay/gate")

    def test_an_upsert_with_an_unchanged_url_does_not_restart_the_worker(self, engine):
        # The reconcile loop calls this unconditionally, every interval. A
        # restart per interval would mean the camera never streams for long.
        first = engine.upsert_camera("gate-a1b2", "rtsp://relay/gate")
        second = engine.upsert_camera("gate-a1b2", "rtsp://relay/gate")
        assert second is first
        assert first.started == 1 and first.stopped == 0

    def test_an_upsert_with_a_new_url_replaces_the_worker(self, engine):
        # A URL change is a remove-then-add: the old session is pointed at a
        # stream that may no longer exist.
        first = engine.upsert_camera("gate-a1b2", "rtsp://relay/gate")
        second = engine.upsert_camera("gate-a1b2", "rtsp://relay/gate2")
        assert second is not first
        assert first.stopped == 1
        assert second.rtsp_url == "rtsp://relay/gate2"

    def test_an_upsert_for_an_unknown_camera_adds_it(self, engine):
        w = engine.upsert_camera("new-cam", "rtsp://relay/new")
        assert w.started == 1

    def test_removing_a_camera_stops_its_worker(self, engine):
        w = engine.add_camera("gate-a1b2", "rtsp://relay/gate")
        assert engine.remove_camera("gate-a1b2") is True
        assert w.stopped == 1
        assert engine.get("gate-a1b2") is None

    def test_removing_an_unknown_camera_is_false_not_an_error(self, engine):
        # The reconcile loop removes cameras it believes are gone; raising
        # would break a sync over a camera that was already removed.
        assert engine.remove_camera("never-existed") is False

    def test_shutdown_stops_every_worker(self, engine):
        a = engine.add_camera("a", "rtsp://relay/a")
        b = engine.add_camera("b", "rtsp://relay/b")
        engine.shutdown()
        assert a.stopped == 1 and b.stopped == 1
        assert engine.snapshot() == []


class TestHealth:
    def test_health_counts_cameras_by_state(self, engine):
        engine.add_camera("a", "rtsp://relay/a")
        engine.add_camera("b", "rtsp://relay/b")
        engine.get("b").state = "CONNECTING"
        h = engine.health()
        assert h["total_cameras"] == 2
        assert h["by_state"] == {"STREAMING": 1, "CONNECTING": 1}

    def test_an_empty_broker_is_still_healthy(self, engine):
        # No cameras is the normal state at boot, not a fault.
        h = engine.health()
        assert h["status"] == "ok" and h["total_cameras"] == 0

    def test_rings_that_will_not_fit_in_shm_are_warned_about_before_they_fail(self, engine):
        # THE FAILURE THIS SERVICE IS MOST LIKELY TO HIT: a default Docker
        # install gives /dev/shm 64 MB, under half of one five-camera set.
        for i in range(6):
            engine.add_camera(f"c{i}", f"rtsp://relay/{i}")
        h = engine.health()
        assert h["shm_warning"] is not None
        assert "shm_size" in h["shm_warning"]

    def test_a_ring_set_that_fits_raises_no_warning(self, engine):
        engine.add_camera("a", "rtsp://relay/a")
        assert engine.health()["shm_warning"] is None

    def test_health_reports_the_sampling_configuration(self, engine):
        h = engine.health()
        assert h["sample_fps"] == 2.0 and h["ring_slots"] == 8
