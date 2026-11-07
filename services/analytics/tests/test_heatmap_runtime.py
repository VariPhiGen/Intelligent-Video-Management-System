"""Heatmap sessions: when a camera accumulates, when it renders, when it stops.

The lifecycle the Heatmap page depends on, driven directly on exact
timestamps — no frames queue, no detector, no tracker:

    schedule window  ×  operator ON/OFF  ×  resume | new session

and the two costs that matter: nothing at all happens for a camera that is OFF,
and a picture is made on the snapshot interval, not per frame, while the heat
underneath keeps accumulating across every snapshot.

Run: python -m pytest tests -q   (from services/analytics)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.activities import ACTIVITY_CLASSES, ActivitySpec, FrameContext, TrackedObject  # noqa: E402
from analytics.activities.Heatmap_overlay import DEFINITION, HeatmapOverlayActivity  # noqa: E402
from analytics.heatmap_runtime import (ACTIVE, CLOSED, FROZEN, HeatmapRuntime,  # noqa: E402
                                       HeatmapSettings, install)

CAM, OTHER = "cam1-9njq", "cam2-7xkp"
W, H = 1920, 1080
T0 = 1789800000.0


class Published(list):
    """Everything the runtime handed to the sink, in order."""

    def __call__(self, kind, session, snap):
        self.append((kind, session.id, session.status, snap))

    def kinds(self):
        return [k for k, _, _, _ in self]

    @property
    def images(self):
        return [s for _, _, _, s in self if s is not None]


def frame(value: int = 60) -> np.ndarray:
    return np.full((H, W, 3), value, np.uint8)


def runtime(interval: float = 60.0, publish=None) -> HeatmapRuntime:
    return HeatmapRuntime(HeatmapSettings(snapshot_interval_s=interval, output_width=640),
                          publish=publish)


def observe(rt, camera=CAM, ts=T0, points=((500.0, 700.0),), in_window=True, img=None,
            radius_px=40.0, intensity=1.0):
    rt.observe(camera, ts, points, frame() if img is None else img, width=W, height=H,
               radius_px=radius_px, intensity=intensity, in_window=in_window)


def heat_of(rt, camera=CAM):
    st = rt.status(camera)
    return st["session"] if st else None


class TestOneCamera:
    def test_the_first_frame_of_a_window_starts_a_session_and_a_picture(self):
        out = Published()
        rt = runtime(publish=out)
        observe(rt)
        s = heat_of(rt)
        assert s["status"] == ACTIVE and s["started_at"] == T0 and s["points"] == 1
        assert out.kinds() == ["live"] and out.images[0].jpeg[:2] == b"\xff\xd8"

    def test_no_detections_still_counts_the_frame_but_adds_no_heat(self):
        rt = runtime()
        observe(rt, points=())
        s = heat_of(rt)
        assert s["frames"] == 1 and s["points"] == 0 and s["peak"] == 0.0

    def test_several_people_in_one_frame_all_count(self):
        rt = runtime()
        observe(rt, points=((100.0, 200.0), (800.0, 900.0), (1500.0, 500.0)))
        assert heat_of(rt)["points"] == 3

    def test_heat_keeps_accumulating_across_snapshots(self):
        out = Published()
        rt = runtime(interval=60.0, publish=out)
        for i in range(0, 601, 5):                       # 10 minutes at one frame / 5 s
            observe(rt, ts=T0 + i)
        s = heat_of(rt)
        assert s["points"] == 121                         # every frame added heat
        assert s["peak"] == pytest.approx(121.0)          # nothing was reset
        assert out.kinds().count("live") == 11        # first frame, then every 60 s

    def test_a_picture_is_not_made_on_every_frame(self):
        out = Published()
        rt = runtime(interval=60.0, publish=out)
        for i in range(60):                               # 12 s of frames at 5 FPS
            observe(rt, ts=T0 + i * 0.2)
        assert out.kinds() == ["live"]                # one, not sixty

    def test_the_session_keeps_its_id_while_it_runs(self):
        rt = runtime()
        observe(rt, ts=T0)
        first = heat_of(rt)["session_id"]
        observe(rt, ts=T0 + 300)
        assert heat_of(rt)["session_id"] == first


class TestTheSchedule:
    def test_out_of_window_frames_do_nothing(self):
        out = Published()
        rt = runtime(publish=out)
        observe(rt, in_window=False)
        assert heat_of(rt) is None and out == []

    def test_the_window_closing_finalises_the_session(self):
        out = Published()
        rt = runtime(publish=out)
        observe(rt, ts=T0)
        observe(rt, ts=T0 + 120)
        observe(rt, ts=T0 + 180, in_window=False)         # the window closed
        assert out.kinds() == ["live", "live", "final"]
        kind, _, status, snap = out[-1]
        assert status == CLOSED and snap is not None      # the last picture was made
        assert heat_of(rt) is None                        # and the heat was let go

    def test_the_next_window_is_a_new_session(self):
        rt = runtime()
        observe(rt, ts=T0)
        first = heat_of(rt)["session_id"]
        observe(rt, ts=T0 + 100, in_window=False)
        observe(rt, ts=T0 + 86400)                        # the next day's window
        s = heat_of(rt)
        assert s["session_id"] != first and s["points"] == 1 and s["peak"] == pytest.approx(1.0)


class TestTheOperatorSwitch:
    def test_turning_off_freezes_the_session_and_makes_a_final_picture(self):
        out = Published()
        rt = runtime(publish=out)
        observe(rt, ts=T0)
        rt.set_enabled(CAM, False, reason="operator", now=T0 + 30)
        kind, _, status, snap = out[-1]
        assert (kind, status) == ("final", FROZEN) and snap is not None
        assert snap.captured_at == T0 + 30

    def test_an_off_camera_does_nothing_at_all(self):
        out = Published()
        rt = runtime(publish=out)
        rt.set_enabled(CAM, False, now=T0)
        before = len(out)
        for i in range(100):
            observe(rt, ts=T0 + i)
        assert len(out) == before                         # no render, no encode, no upload
        assert rt.snapshots_rendered == 0

    def test_resuming_continues_the_same_session_and_its_heat(self):
        rt = runtime()
        observe(rt, ts=T0)
        observe(rt, ts=T0 + 10)
        session_id, points = heat_of(rt)["session_id"], heat_of(rt)["points"]
        rt.set_enabled(CAM, False, now=T0 + 20)
        rt.set_enabled(CAM, True, mode="resume", now=T0 + 600)
        observe(rt, ts=T0 + 601)
        s = heat_of(rt)
        assert s["session_id"] == session_id and s["status"] == ACTIVE
        assert s["points"] == points + 1
        assert s["peak"] == pytest.approx(points + 1)     # the old heat is still there

    def test_starting_a_new_session_closes_the_frozen_one_and_forgets_its_heat(self):
        out = Published()
        rt = runtime(publish=out)
        observe(rt, ts=T0)
        observe(rt, ts=T0 + 10)
        old = heat_of(rt)["session_id"]
        rt.set_enabled(CAM, False, now=T0 + 20)
        rt.set_enabled(CAM, True, mode="new", now=T0 + 600)
        assert out[-1][0] == "closed" and out[-1][1] == old
        observe(rt, ts=T0 + 601)
        s = heat_of(rt)
        assert s["session_id"] != old
        assert s["points"] == 1 and s["peak"] == pytest.approx(1.0)

    def test_turning_on_a_camera_that_was_never_off_changes_nothing(self):
        out = Published()
        rt = runtime(publish=out)
        observe(rt, ts=T0)
        rt.set_enabled(CAM, True, mode="new", now=T0 + 5)
        assert heat_of(rt)["points"] == 1 and "closed" not in out.kinds()

    def test_off_during_a_window_does_not_touch_the_schedule(self):
        # The runtime never sees the schedule; it is told `in_window` per frame.
        # Turning off and on again leaves the camera accumulating as soon as the
        # window is open, which is what "the schedule is unchanged" means here.
        rt = runtime()
        observe(rt, ts=T0)
        rt.set_enabled(CAM, False, now=T0 + 1)
        rt.set_enabled(CAM, True, mode="resume", now=T0 + 2)
        observe(rt, ts=T0 + 3, in_window=True)
        assert heat_of(rt)["status"] == ACTIVE


class TestManyCameras:
    def test_each_camera_keeps_its_own_session_and_heat(self):
        rt = runtime()
        observe(rt, camera=CAM, ts=T0, points=((100.0, 100.0),))
        observe(rt, camera=OTHER, ts=T0, points=((200.0, 200.0), (300.0, 300.0)))
        a, b = heat_of(rt, CAM), heat_of(rt, OTHER)
        assert a["session_id"] != b["session_id"]
        assert (a["points"], b["points"]) == (1, 2)

    def test_switching_one_off_leaves_the_other_running(self):
        rt = runtime()
        observe(rt, camera=CAM, ts=T0)
        observe(rt, camera=OTHER, ts=T0)
        rt.set_enabled(CAM, False, now=T0 + 1)
        observe(rt, camera=CAM, ts=T0 + 2)
        observe(rt, camera=OTHER, ts=T0 + 2)
        assert heat_of(rt, CAM)["status"] == FROZEN
        assert heat_of(rt, OTHER)["status"] == ACTIVE and heat_of(rt, OTHER)["points"] == 2

    def test_forgetting_a_camera_leaves_the_others_alone(self):
        rt = runtime()
        observe(rt, camera=CAM, ts=T0)
        observe(rt, camera=OTHER, ts=T0)
        rt.forget(CAM)
        assert rt.status(CAM) is None and heat_of(rt, OTHER) is not None

    def test_only_configured_cameras_are_kept(self):
        rt = runtime()
        observe(rt, camera=CAM, ts=T0)
        observe(rt, camera=OTHER, ts=T0)
        rt.configured([OTHER])
        assert rt.status(CAM) is None and rt.status(OTHER) is not None


class TestInterruptions:
    def test_a_stream_gap_does_not_end_the_session(self):
        rt = runtime()
        observe(rt, ts=T0)
        session_id = heat_of(rt)["session_id"]
        rt.reset(CAM)                                     # the broker reconnected
        observe(rt, ts=T0 + 30)
        s = heat_of(rt)
        assert s["session_id"] == session_id and s["points"] == 2

    def test_a_frame_of_a_different_size_restarts_the_heat_not_the_session(self):
        rt = runtime()
        observe(rt, ts=T0)
        session_id = heat_of(rt)["session_id"]
        rt.observe(CAM, T0 + 10, ((100.0, 100.0),), np.full((720, 1280, 3), 60, np.uint8),
                   width=1280, height=720, radius_px=40.0, intensity=1.0, in_window=True)
        s = heat_of(rt)
        assert s["session_id"] == session_id and s["points"] == 1

    def test_a_publish_that_fails_does_not_stop_accumulation(self):
        def explode(kind, session, snap):
            raise RuntimeError("the API is down")
        rt = runtime(publish=explode)
        observe(rt, ts=T0)
        observe(rt, ts=T0 + 5)
        assert heat_of(rt)["points"] == 2 and rt.publish_failures == 1


class TestWhatTheActivityContributes:
    """The activity is the part that decides WHICH points are heat."""

    def spec(self, params=None, days=None):
        stored = {"type": "Heatmap_overlay", "regions": [], "params": dict(params or {})}
        if days is not None:
            stored["params"]["active_days"] = days
        return ActivitySpec.build(DEFINITION, stored, {})

    def activity(self, params=None, days=None):
        return HeatmapOverlayActivity(CAM, self.spec(params, days))

    def ctx(self, objects, ts=T0):
        return FrameContext(camera=CAM, ts=ts, width=W, height=H, objects=tuple(objects),
                            frame=frame())

    def obj(self, label="person", bbox=(0.4, 0.3, 0.5, 0.7)):
        return TrackedObject(track_id="t1", domain="person", label=label, confidence=0.9,
                             bbox=bbox, confirmed=True, is_new=False)

    def test_it_is_registered_under_the_deepstream_key(self):
        assert ACTIVITY_CLASSES["Heatmap_overlay"] is HeatmapOverlayActivity

    def test_it_raises_no_events(self):
        rt = install(runtime())
        try:
            assert self.activity().process(self.ctx([self.obj()])) == []
        finally:
            install(None)

    def test_heat_lands_at_the_bottom_centre_of_the_box(self):
        rt = install(runtime())
        try:
            self.activity().process(self.ctx([self.obj(bbox=(0.4, 0.3, 0.5, 0.7))]))
            matrix = rt.state(CAM)
            ys, xs = np.nonzero(matrix)
            scale = rt.settings.scale
            assert xs.mean() == pytest.approx(0.45 * W / scale, abs=2)   # centre of x
            assert ys.mean() == pytest.approx(0.7 * H / scale, abs=2)    # the bottom edge
        finally:
            install(None)

    def test_only_the_configured_classes_leave_heat(self):
        rt = install(runtime())
        try:
            act = self.activity({"classes_for_heatmap": ["person"]})
            act.process(self.ctx([self.obj(label="person"), self.obj(label="car"),
                                  self.obj(label="truck")]))
            assert rt.status(CAM)["session"]["points"] == 1
        finally:
            install(None)

    def test_vehicles_can_be_chosen_instead(self):
        rt = install(runtime())
        try:
            act = self.activity({"classes_for_heatmap": ["car", "truck"]})
            act.process(self.ctx([self.obj(label="person"), self.obj(label="car"),
                                  self.obj(label="truck")]))
            assert rt.status(CAM)["session"]["points"] == 2
        finally:
            install(None)

    def test_the_schedule_decides_whether_the_frame_counts(self):
        rt = install(runtime())
        try:
            # tm_wday of T0 is known to the Schedule through localtime; use an
            # empty day list, which means "no day" in the stored shape.
            act = self.activity(days=[])
            act.process(self.ctx([self.obj()]))
            assert rt.status(CAM)["session"] is None
        finally:
            install(None)

    def test_without_a_runtime_it_is_quiet(self):
        install(None)
        assert self.activity().process(self.ctx([self.obj()])) == []

    def test_settings_reach_the_accumulator(self):
        rt = install(runtime())
        try:
            act = self.activity({"intensity": 7.0, "radius": 10})
            act.process(self.ctx([self.obj()]))
            assert rt.status(CAM)["session"]["peak"] == pytest.approx(7.0)
        finally:
            install(None)


class Blocks(list):
    """Every 5-minute heat block handed to the chunk sink, decoded."""

    def __call__(self, meta, heat_npz, background):
        import io
        heat = np.load(io.BytesIO(heat_npz))["heat"]
        self.append((meta, heat, background))


def block_runtime(blocks):
    return HeatmapRuntime(HeatmapSettings(snapshot_interval_s=60.0, output_width=640),
                          chunks=blocks)


class TestStorageBlocks:
    """Heat is stored as 5-minute INCREMENTS on the local clock, so any range is a sum."""

    def test_blocks_end_on_the_five_minute_marks(self):
        from analytics.heatmap_runtime import next_mark
        blocks = Blocks()
        rt = block_runtime(blocks)
        t = next_mark(T0) + 60.0                 # one minute past a mark
        for i in range(0, 700, 10):              # ~11.5 minutes of frames
            observe(rt, ts=t + i)
        ends = [m["period_end"] for m, _, _ in blocks]
        assert ends == [next_mark(t), next_mark(t) + 300.0]
        assert blocks[0][0]["period_start"] == t                  # first block starts at first heat
        assert blocks[1][0]["period_start"] == next_mark(t)       # then mark to mark

    def test_the_blocks_add_up_to_the_session_heat(self):
        blocks = Blocks()
        rt = block_runtime(blocks)
        for i in range(0, 1300, 5):
            observe(rt, ts=T0 + i, points=((500.0 + i % 300, 700.0),))
        rt.set_enabled(CAM, False)               # flushes the block in progress
        total = sum(h for _, h, _ in blocks)
        acc = rt._cameras[CAM].session.accumulator
        assert np.allclose(total, acc.matrix)
        assert sum(m["points"] for m, _, _ in blocks) == acc.points

    def test_a_block_is_cut_when_heat_stops(self):
        blocks = Blocks()
        rt = block_runtime(blocks)
        observe(rt, ts=T0)
        observe(rt, ts=T0 + 20)
        observe(rt, ts=T0 + 40, in_window=False)  # schedule ended
        assert len(blocks) == 1 and blocks[0][0]["period_end"] == T0 + 40
        assert blocks[0][0]["points"] == 2

    def test_a_camera_frame_rides_with_every_block(self):
        """The VMS keeps ONE frame per camera and overwrites it, so sending one
        every block is what keeps the background at most five minutes old."""
        from analytics.heatmap_runtime import next_mark
        blocks = Blocks()
        rt = block_runtime(blocks)
        t = next_mark(T0)
        for i in range(0, 1900, 30):
            observe(rt, ts=t + i)
        assert len(blocks) == 6 and all(bg and bg[:2] == b"\xff\xd8" for _, _, bg in blocks)

    def test_the_block_in_progress_can_be_read(self):
        import io, json
        blocks = Blocks()
        rt = block_runtime(blocks)
        observe(rt, ts=T0)
        z = np.load(io.BytesIO(rt.partial(CAM)))
        meta = json.loads(str(z["meta"]))
        assert meta["period_start"] == T0 and meta["points"] == 1
        assert z["heat"].max() > 0 and bytes(z["background"][:2]) == b"\xff\xd8"
        assert rt.partial(OTHER) is None
