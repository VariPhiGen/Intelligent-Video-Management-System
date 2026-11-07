"""Heat blocks: the parts of a range heatmap that need no database.

  * a block is read back from its .npz exactly, and anything else is refused
  * the heatmap of a range is the SUM of its blocks: two blocks of the same heat
    draw hotter than one, and a block from another frame size still counts
  * the picture is the camera frame under the colour map, at the output width
  * object keys are on the local clock, one folder per day
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import heatmap_chunks as hc  # noqa: E402


def npz(**arrays) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def spot(value: float, shape=(90, 160), at=(45, 80)) -> np.ndarray:
    m = np.zeros(shape, np.float32)
    m[at[0] - 3:at[0] + 3, at[1] - 3:at[1] + 3] = value
    return m


def frame_jpeg(w=640, h=360) -> bytes:
    return cv2.imencode(".jpg", np.full((h, w, 3), 90, np.uint8))[1].tobytes()


def decode(jpeg: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


class TestBlocks:
    def test_a_block_reads_back_exactly(self):
        m = spot(7.5)
        assert np.array_equal(hc.load_heat(npz(heat=m)), m)

    @pytest.mark.parametrize("bad", [b"not an npz", npz(other=np.zeros((2, 2))),
                                     npz(heat=np.zeros((2, 2, 2), np.float32)),
                                     npz(heat=np.zeros((2, 2), np.int32))])
    def test_anything_else_is_refused(self, bad):
        with pytest.raises(Exception):
            hc.load_heat(bad)

    def test_keys_are_local_and_per_day(self):
        at = datetime(2026, 10, 13, 4, 30, tzinfo=timezone.utc)
        local = at.astimezone()
        assert hc.chunk_key("cam-a1", at) == \
            f"heatmaps/cam-a1/{local:%Y-%m-%d}/chunks/{local:%H%M%S}.npz"


class TestDrawing:
    def test_a_range_is_the_sum_of_its_blocks(self):
        total = hc.sum_heat([spot(40), spot(40), spot(5)], (90, 160))
        assert total[45, 80] == pytest.approx(85.0)
        assert total.sum() == pytest.approx(85.0 * 36)

    def test_a_block_from_another_frame_size_still_counts(self):
        small = spot(40, shape=(45, 80), at=(22, 40))
        total = hc.sum_heat([small, spot(40)], (90, 160))
        assert total.shape == (90, 160)
        assert total[45, 80] > 40.0

    def test_heat_somewhere_else_draws_differently(self):
        # Relative colours: the SAME pattern at any amount draws the same, so
        # what must change the picture is where the heat is.
        one = decode(hc._draw([spot(40)], frame_jpeg(), 4, 640, 360))
        many = decode(hc._draw([spot(40), spot(40, at=(10, 20))], frame_jpeg(), 4, 640, 360))
        assert np.abs(one.astype(int) - many.astype(int)).sum() > 0

    def test_no_heat_is_just_the_frame_tinted_cold(self):
        img = decode(hc._draw([], frame_jpeg(), 4, 640, 360))
        assert img.shape[:2] == (360, 640)
        b, g, r = img[180, 320].astype(int)
        assert b > r                                     # JET at zero is blue

    def test_wide_frames_are_drawn_at_the_output_width(self):
        img = decode(hc._draw([spot(10)], frame_jpeg(2560, 1440), 4, 2560, 1440))
        assert img.shape[1] == hc.OUTPUT_WIDTH

    def test_no_frame_stored_yet_draws_on_grey(self):
        img = decode(hc._draw([spot(10)], None, 4, 640, 360))
        assert img.shape[:2] == (360, 640)


class TestColourScale:
    def centre(self, jpeg: bytes):
        img = decode(jpeg)
        return img[img.shape[0] // 2, img.shape[1] // 2].astype(int)

    def test_the_hottest_spot_is_red_even_when_heat_is_low(self):
        low = self.centre(hc._draw([spot(3)], frame_jpeg(), 4, 640, 360))
        high = self.centre(hc._draw([spot(400)], frame_jpeg(), 4, 640, 360))
        assert abs(low[2] - high[2]) < 10           # same colour whatever the amount

    def test_a_long_range_does_not_saturate(self):
        img = decode(hc._draw([spot(400)] * 10, frame_jpeg(), 4, 640, 360))
        # Off the spot's centre the picture is still graded, not solid red.
        assert len({tuple(p) for p in img[::40, ::40].reshape(-1, 3)}) > 1

    def test_no_heat_is_not_a_division_by_zero(self):
        img = decode(hc._draw([], frame_jpeg(), 4, 640, 360))
        assert img.shape[:2] == (360, 640)


class TestCaps:
    def cfg(self, **params):
        return {"activities": [{"type": "Heatmap_overlay", "params": params}]}

    def test_defaults_when_nothing_is_set(self):
        assert hc.caps(None) == (hc.DEFAULT_RETENTION_DAYS, hc.DEFAULT_STORAGE_MB) == (30, 500)
        assert hc.caps(self.cfg()) == (30, 500)

    def test_the_operators_values(self):
        assert hc.caps(self.cfg(retention_days=7, storage_mb=50)) == (7, 50)

    @pytest.mark.parametrize("bad", [0, -1, 2.5, "x", None, 10_000_000])
    def test_a_bad_value_falls_back_to_the_default(self, bad):
        assert hc.caps(self.cfg(retention_days=bad, storage_mb=bad)) == (30, 500)

    def test_another_activitys_settings_are_not_read(self):
        cfg = {"activities": [{"type": "stray_parking", "params": {"retention_days": 3}}]}
        assert hc.caps(cfg) == (30, 500)

