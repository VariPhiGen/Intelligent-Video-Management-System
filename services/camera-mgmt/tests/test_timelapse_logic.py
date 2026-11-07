"""Time-lapse logic without a database: settings, keys, the storage estimate, the
picture pipeline (privacy masks above all), cadence, and the render command.

The one test here that must never be weakened is the mask test: the relay is
NOT masked, so `process()` is the only thing between a camera's raw view and a
90-day archive of it. It is asserted on pixels, not on a flag.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import io
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from backend.services import timelapse as tl  # noqa: E402


def _jpeg(w=1920, h=1080, colour=(200, 200, 200), square=None) -> bytes:
    """A flat frame, optionally with a black-and-white checkerboard square (the
    'face' a mask must destroy) at `square` = (x0, y0, x1, y1) in pixels."""
    im = Image.new("RGB", (w, h), colour)
    if square:
        x0, y0, x1, y1 = square
        for y in range(y0, y1):
            for x in range(x0, x1):
                im.putpixel((x, y), (0, 0, 0) if ((x // 4) + (y // 4)) % 2 else (255, 255, 255))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


def _contrast(jpeg: bytes, box) -> float:
    with Image.open(io.BytesIO(jpeg)) as im:
        g = im.convert("L").crop(box)
        lo, hi = g.getextrema()
    return hi - lo


# ── settings ─────────────────────────────────────────────────────────────────

class TestConfig:
    def test_an_empty_config_is_complete_and_conservative(self):
        c = tl.TimelapseConfig()
        assert c.interval_s == 300 and c.resolution == "1080p" and c.retention_days == 90
        assert c.skip_dark is True and c.skip_unchanged is False
        assert c.auto_render_daily is False
        # Working hours Mon-Sat, on the recording schedule's own shape.
        assert c.schedule["mode"] == "weekly" and c.schedule["rules"][0]["days"] == [0, 1, 2, 3, 4, 5]

    @pytest.mark.parametrize("bad", [
        {"interval_s": 5}, {"interval_s": 86401}, {"resolution": "4k"}, {"jpeg_quality": 10},
        {"retention_days": 0}, {"render_fps": 60}, {"surprise": 1},
        {"schedule": {"mode": "weekly", "rules": [{"days": [9], "start": "07:00", "end": "19:00"}]}},
    ])
    def test_out_of_range_settings_are_refused(self, bad):
        with pytest.raises(Exception):
            tl.TimelapseConfig(**bad)

    def test_null_schedule_means_around_the_clock(self):
        assert tl.TimelapseConfig(schedule=None).schedule is None
        assert tl.TimelapseConfig(schedule={}).schedule is None

    def test_a_stored_config_that_no_longer_validates_falls_back_rather_than_stopping(self):
        assert tl.effective_config({"interval_s": 1, "resolution": "8k"}).interval_s == 300
        assert tl.effective_config(None).interval_s == 300


class TestKeys:
    def test_frame_keys_are_filed_by_local_day_and_second(self):
        at = datetime(2026, 10, 12, 9, 30, 5, tzinfo=timezone.utc)
        local = at.astimezone()
        key = tl.frame_key("gate-a1b2", at)
        assert key == f"timelapse/gate-a1b2/{local:%Y-%m-%d}/{local:%H%M%S}.jpg"
        assert key.startswith(tl.camera_prefix("gate-a1b2"))

    def test_renders_live_under_the_camera_prefix_so_a_camera_purge_takes_them(self):
        rid = uuid.uuid4()
        assert tl.render_key("gate-a1b2", rid).startswith(tl.camera_prefix("gate-a1b2"))


class TestEstimate:
    def test_working_hours_mon_to_sat(self):
        # 12 h × 6 days / 7 = ~617 min/day
        assert tl.active_minutes_per_day(tl.DEFAULT_SCHEDULE) == pytest.approx(12 * 60 * 6 / 7)

    def test_a_rule_that_wraps_midnight_counts_both_sides(self):
        night = {"mode": "weekly", "rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}]}
        assert tl.active_minutes_per_day(night) == pytest.approx(8 * 60)

    def test_the_estimate_the_tab_shows(self):
        e = tl.estimate(tl.TimelapseConfig())
        assert 120 <= e["frames_per_day"] <= 125                 # one every 5 min, 617 min
        assert 15 <= e["mb_per_day"] <= 40
        assert e["video_seconds_per_day"] == pytest.approx(e["frames_per_day"] / 24, abs=0.1)

    def test_the_cap_is_flagged_when_it_bites_before_retention(self):
        e = tl.estimate(tl.TimelapseConfig(interval_s=10, schedule=None, max_storage_mb=100))
        assert e["cap_reached_after_days"] is not None and e["mb_total"] == 100


# ── the picture pipeline ─────────────────────────────────────────────────────

class TestProcess:
    def test_privacy_masks_destroy_what_is_under_them_and_nothing_else(self):
        # Two identical checkerboards; only the left one is masked.
        jpeg = _jpeg(1280, 720, square=(100, 100, 300, 300))
        with Image.open(io.BytesIO(jpeg)) as im:
            im = im.copy()
        for y in range(100, 300):
            for x in range(700, 900):
                im.putpixel((x, y), (0, 0, 0) if ((x // 4) + (y // 4)) % 2 else (255, 255, 255))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=95)
        mask = [[[100 / 1280, 100 / 720], [300 / 1280, 100 / 720], [300 / 1280, 300 / 720], [100 / 1280, 300 / 720]]]
        out = tl.process(buf.getvalue(), masks=mask, resolution="original", quality=90)
        masked = _contrast(out.jpeg, (140, 140, 260, 260))
        unmasked = _contrast(out.jpeg, (740, 140, 860, 260))
        assert unmasked > 200                      # the right square is still a checkerboard
        assert masked < 40                         # the left one is mush

    def test_masks_are_applied_at_native_size_before_resizing(self):
        jpeg = _jpeg(1920, 1080, square=(0, 0, 400, 400))
        mask = [[[0, 0], [400 / 1920, 0], [400 / 1920, 400 / 1080], [0, 400 / 1080]]]
        out = tl.process(jpeg, masks=mask, resolution="720p", quality=85)
        assert (out.width, out.height) == (1280, 720)
        assert _contrast(out.jpeg, (20, 20, 240, 240)) < 40     # 400px → 266px after resize

    def test_resize_keeps_aspect_and_even_width_and_never_upscales(self):
        assert tl.process(_jpeg(2560, 1440), masks=[], resolution="1080p", quality=80).width == 1920
        small = tl.process(_jpeg(640, 360), masks=[], resolution="1080p", quality=80)
        assert (small.width, small.height) == (640, 360)

    def test_brightness_and_change_detection(self):
        dark = tl.process(_jpeg(colour=(5, 5, 5)), masks=[], resolution="720p", quality=80)
        light = tl.process(_jpeg(colour=(200, 200, 200)), masks=[], resolution="720p", quality=80)
        assert dark.brightness < 18 < light.brightness
        assert tl.thumb_diff(light.thumb, light.thumb) == 0
        assert tl.thumb_diff(dark.thumb, light.thumb) > 100
        assert tl.thumb_diff(b"", light.thumb) == 255.0


# ── cadence and capture decisions ────────────────────────────────────────────

class TestCadence:
    def test_due_after_one_interval_from_the_last_attempt(self):
        assert tl.due(1000.0, None, 300)
        assert not tl.due(1000.0, 900.0, 300)
        assert tl.due(1200.0, 900.0, 300)

    @pytest.fixture(autouse=True)
    def _clean(self):
        for d in (tl._last_attempt, tl._retry_after, tl._prev_thumb):
            d.clear()
        tl._inflight.clear()
        yield

    def _run(self, monkeypatch, cfg, *, frame=None, steering=None, busy=False, at_preset=(True, "at the preset")):
        from backend.services import snapshot, ptz_control
        stored, statuses = [], []

        async def fake_snapshot(slug, wait_s=None):
            if busy:
                raise snapshot.SnapshotBusy(slug)
            return frame

        async def fake_store(cam_id, slug, at, p, days):
            stored.append((slug, p.width, days))

        async def fake_status(slug, result, detail=""):
            statuses.append(result)

        async def fake_holder(slug):
            return steering

        monkeypatch.setattr(snapshot, "shared_snapshot", fake_snapshot)
        async def fake_ptz_check(slug, ref, tol):
            return at_preset

        monkeypatch.setattr(ptz_control, "current_holder", fake_holder)
        monkeypatch.setattr(tl, "_ptz_check", fake_ptz_check)
        monkeypatch.setattr(tl, "store_frame", fake_store)
        monkeypatch.setattr(tl, "_status", fake_status)
        cam = uuid.uuid4()
        tl._inflight.add(cam)
        out = asyncio.run(tl._capture_one(cam, "gate-a1b2", cfg, [], datetime.now(timezone.utc),
                                          asyncio.Semaphore(1)))
        return out, stored, statuses, cam

    def test_a_good_frame_is_stored_with_its_retention(self, monkeypatch):
        out, stored, statuses, cam = self._run(monkeypatch, tl.TimelapseConfig(retention_days=30), frame=_jpeg())
        assert out == "captured" and stored == [("gate-a1b2", 1920, 30)] and statuses == ["captured"]
        assert cam not in tl._inflight

    def test_a_dark_frame_is_skipped_but_still_resets_the_cadence(self, monkeypatch):
        out, stored, _, cam = self._run(monkeypatch, tl.TimelapseConfig(), frame=_jpeg(colour=(3, 3, 3)))
        assert out == "dark" and stored == [] and cam in tl._last_attempt

    def test_dark_frames_are_kept_when_asked(self, monkeypatch):
        out, stored, _, _ = self._run(monkeypatch, tl.TimelapseConfig(skip_dark=False), frame=_jpeg(colour=(3, 3, 3)))
        assert out == "captured" and stored

    def test_an_offline_stream_backs_off(self, monkeypatch):
        out, stored, statuses, cam = self._run(monkeypatch, tl.TimelapseConfig(), frame=None)
        assert out == "offline" and not stored and tl._retry_after[cam] > 0

    def test_a_busy_decode_just_waits_for_the_next_tick(self, monkeypatch):
        out, stored, _, cam = self._run(monkeypatch, tl.TimelapseConfig(), busy=True)
        assert out == "busy" and cam not in tl._last_attempt and cam not in tl._retry_after

    def test_a_ptz_framed_time_lapse_skips_while_an_operator_steers(self, monkeypatch):
        cfg = tl.TimelapseConfig(ptz_preset_only="1")
        out, stored, statuses, _ = self._run(monkeypatch, cfg, frame=_jpeg(), steering="user:alice")
        assert out == "ptz_steering" and not stored
        out, stored, _, _ = self._run(monkeypatch, cfg, frame=_jpeg(), steering=None)
        assert out == "captured"

    def test_a_patrol_parked_elsewhere_is_skipped_and_counts_as_an_attempt(self, monkeypatch):
        # A patrol moves the head WITHOUT the operator lease — only the position
        # read-back can tell. Counted as an attempt so SOAP isn't polled every tick.
        cfg = tl.TimelapseConfig(ptz_preset_only="1")
        out, stored, statuses, cam = self._run(monkeypatch, cfg, frame=_jpeg(), at_preset=(False, "pan is 0.400 off"))
        assert out == "off_preset" and not stored and statuses == ["off_preset"] and cam in tl._last_attempt

    def test_a_head_that_cannot_report_its_position_is_captured_not_silenced(self, monkeypatch):
        cfg = tl.TimelapseConfig(ptz_preset_only="1")
        out, stored, statuses, _ = self._run(monkeypatch, cfg, frame=_jpeg(), at_preset=(None, "position not reported"))
        assert out == "captured" and stored

    def test_no_preset_means_no_position_read_at_all(self, monkeypatch):
        out, stored, _, _ = self._run(monkeypatch, tl.TimelapseConfig(), frame=_jpeg(), at_preset=(False, "never asked"))
        assert out == "captured" and stored

    def test_unchanged_frames_are_skipped_only_when_asked(self, monkeypatch):
        cfg = tl.TimelapseConfig(skip_unchanged=True)
        from backend.services import snapshot, ptz_control
        stored = []

        async def snap(slug, wait_s=None):
            return _jpeg()

        async def store(cam_id, slug, at, p, days):
            stored.append(1)

        async def status(*a, **k):
            return None

        monkeypatch.setattr(snapshot, "shared_snapshot", snap)
        monkeypatch.setattr(tl, "store_frame", store)
        monkeypatch.setattr(tl, "_status", status)
        cam = uuid.uuid4()
        sem = asyncio.Semaphore(1)
        now = datetime.now(timezone.utc)
        first = asyncio.run(tl._capture_one(cam, "c", cfg, [], now, sem))
        second = asyncio.run(tl._capture_one(cam, "c", cfg, [], now + timedelta(minutes=5), sem))
        assert (first, second) == ("captured", "unchanged") and len(stored) == 1


# ── rendering ────────────────────────────────────────────────────────────────

class TestRender:
    def test_the_command_is_browser_playable_h264(self):
        cmd = tl.render_command("/tmp/x", "/tmp/x/out.mp4", fps=24, deflicker=False, nvenc=False)
        assert cmd[:2] == ["ffmpeg", "-nostdin"]
        assert cmd[cmd.index("-framerate") + 1] == "24"
        assert cmd[cmd.index("-vf") + 1] == "format=yuv420p"
        assert "libx264" in cmd and "+faststart" in cmd and cmd[-1] == "/tmp/x/out.mp4"
        assert "%06d.jpg" in cmd[cmd.index("-i") + 1]

    def test_deflicker_and_nvenc_are_opt_in(self):
        cmd = tl.render_command("/d", "/o.mp4", fps=30, deflicker=True, nvenc=True)
        assert cmd[cmd.index("-vf") + 1].startswith("deflicker=")
        assert "h264_nvenc" in cmd and "libx264" not in cmd

    def test_a_too_long_range_becomes_a_faster_video_not_a_refusal(self):
        items = list(range(1000))
        out = tl.sample_evenly(items, 100)
        assert len(out) == 100 and out[0] == 0 and out[-1] == 999 and out == sorted(out)
        assert tl.sample_evenly(items[:50], 100) == items[:50]

    def test_the_overlay_frame_is_written_at_the_render_size(self, tmp_path):
        path = tmp_path / "000000.jpg"
        tl._prepare_frame(_jpeg(1918, 1080), str(path), (1280, 720), "Gate · 2026-10-12 09:30")
        with Image.open(path) as im:
            assert im.size == (1280, 720)
            # the label box is drawn bottom-left in black
            assert im.convert("L").getpixel((8, 700)) < 40


# ── PTZ: at the preset? ──────────────────────────────────────────────────────

class TestPresetMatch:
    P = {"token": "1", "name": "Tower", "pan": 0.10, "tilt": -0.20, "zoom": 0.0}

    def _st(self, **kw):
        return {"pan": 0.10, "tilt": -0.20, "zoom": 0.0, "moving_pan_tilt": "IDLE", "moving_zoom": "IDLE", **kw}

    def test_at_the_preset_within_tolerance(self):
        assert tl.preset_match(self._st(pan=0.115), self.P, 0.02) == (True, "at the preset")

    def test_off_on_any_axis(self):
        ok, why = tl.preset_match(self._st(tilt=0.3), self.P, 0.02)
        assert ok is False and why.startswith("tilt")

    def test_moving_is_never_at_the_preset(self):
        assert tl.preset_match(self._st(moving_pan_tilt="MOVING"), self.P, 0.02)[0] is False

    def test_pan_wraps_around_the_circle(self):
        p = dict(self.P, pan=0.995)
        assert tl.preset_match(self._st(pan=-0.995), p, 0.02)[0] is True
        assert tl.preset_match(self._st(pan=-0.9), p, 0.02)[0] is False

    def test_tilt_does_not_wrap(self):
        p = dict(self.P, tilt=0.995)
        assert tl.preset_match(self._st(tilt=-0.995), p, 0.02)[0] is False

    def test_missing_zoom_compares_the_rest(self):
        assert tl.preset_match(self._st(zoom=None), self.P, 0.02)[0] is True

    def test_no_position_is_unknown_not_a_skip(self):
        assert tl.preset_match({"pan": None, "tilt": None, "zoom": None}, self.P, 0.02)[0] is None
        assert tl.preset_match(self._st(), dict(self.P, pan=None, tilt=None, zoom=None), 0.02)[0] is None
        assert tl.preset_match(None, self.P, 0.02)[0] is None

    def test_find_preset_by_token_then_by_name(self):
        ps = [{"token": "1", "name": "Tower"}, {"token": "2", "name": "Gate"}]
        assert tl.find_preset(ps, "2")["name"] == "Gate"
        assert tl.find_preset(ps, "tower")["token"] == "1"      # a typed name from before the picker
        assert tl.find_preset(ps, "9") is None

    def test_ptz_check_reads_presets_once_per_ttl_and_a_missing_preset_skips(self, monkeypatch):
        from backend.services import onvif_ptz, ptz_control
        calls = {"presets": 0}

        async def target(slug):
            return ("10.0.0.5", 80, "u", "p", "prof")

        async def presets(*a):
            calls["presets"] += 1
            return [{"token": "1", "name": "Tower", "pan": 0.1, "tilt": 0.0, "zoom": 0.0}]

        async def status(*a):
            return {"pan": 0.1, "tilt": 0.0, "zoom": 0.0, "moving_pan_tilt": "IDLE", "moving_zoom": None}

        monkeypatch.setattr(ptz_control, "_resolve_target", target)
        monkeypatch.setattr(onvif_ptz, "get_presets", presets)
        monkeypatch.setattr(onvif_ptz, "get_status", status)
        tl._preset_cache.clear()
        assert asyncio.run(tl._ptz_check("cam-x", "1", 0.02))[0] is True
        assert asyncio.run(tl._ptz_check("cam-x", "1", 0.02))[0] is True
        assert calls["presets"] == 1
        assert asyncio.run(tl._ptz_check("cam-x", "7", 0.02))[0] is False

    def test_an_onvif_failure_is_unknown(self, monkeypatch):
        from backend.services import ptz_control

        async def boom(slug):
            raise RuntimeError("soap timeout")

        monkeypatch.setattr(ptz_control, "_resolve_target", boom)
        assert asyncio.run(tl._ptz_check("cam-y", "1", 0.02))[0] is None


class TestScaleAndNoon:
    def test_scale_jpeg_shrinks_keeps_aspect_and_never_upscales(self):
        small = tl.scale_jpeg(_jpeg(1920, 1080), 640)
        with Image.open(io.BytesIO(small)) as im:
            assert im.size == (640, 360)
        src = _jpeg(320, 180)
        assert tl.scale_jpeg(src, 640) == src

    def test_nearest_noon_is_measured_on_the_local_clock(self, monkeypatch):
        monkeypatch.setattr(tl, "_local", lambda dt: dt)          # appliance clock = UTC for the test
        noon = datetime(2026, 10, 1, 12, 5, tzinfo=timezone.utc)
        morning = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        assert tl._seconds_from_noon(noon) < tl._seconds_from_noon(morning)
