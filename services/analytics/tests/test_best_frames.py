"""Best-frame selection for ANPR, and the bounds that stop it leaking.

WHAT THIS PROTECTS. Plate reading used to run on whichever frame happened to
clear the indexing gate, which is arbitrary — a car approaching produces
steadily better looks and the gate fires on the first acceptable one, not the
best one. These tests pin that the shortlist actually prefers the better crop,
and that a busy forecourt cannot grow the process without limit.

RESOLUTION IS NEVER TRADED FOR MEMORY. A plate is small; downscaling to save
bytes would defeat the feature. The budget is met by holding FEWER crops, and
there is a test for that specifically.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.best_frames import BestFrameBuffer, score_crop, sharpness  # noqa: E402


def crop(w: int, h: int, blur: int = 0, seed: int = 0) -> np.ndarray:
    """A BGR crop with detail; `blur` makes it progressively less sharp."""
    import cv2
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    if blur:
        img = cv2.GaussianBlur(img, (blur * 2 + 1, blur * 2 + 1), 0)
    return img


# ── ranking ──────────────────────────────────────────────────────────────────
def test_a_sharper_crop_scores_higher_than_a_blurred_one_of_equal_size():
    """The case a size-only ranking gets wrong: a motion-blurred plate and a
    sharp one can share dimensions and detector confidence."""
    sharp_score, _, _ = score_crop(crop(200, 120, blur=0), 0.9)
    blur_score, _, _ = score_crop(crop(200, 120, blur=6), 0.9)
    assert sharp_score > blur_score


def test_a_bigger_crop_scores_higher_at_equal_sharpness():
    small, _, _ = score_crop(crop(100, 60, seed=1), 0.9)
    big, _, _ = score_crop(crop(400, 240, seed=1), 0.9)
    assert big > small


def test_confidence_participates_in_the_ranking():
    low, _, _ = score_crop(crop(200, 120), 0.4)
    high, _, _ = score_crop(crop(200, 120), 0.95)
    assert high > low


def test_sharpness_of_a_flat_crop_is_near_zero():
    flat = np.full((80, 120, 3), 128, dtype=np.uint8)
    assert sharpness(flat) < 1.0


# ── the shortlist ────────────────────────────────────────────────────────────
def test_the_best_crop_is_the_one_returned():
    b = BestFrameBuffer(per_track=3)
    b.offer("t1", crop(100, 60, seed=1), 0.5, 0.0)
    b.offer("t1", crop(400, 240, seed=2), 0.9, 1.0)      # clearly the best
    b.offer("t1", crop(120, 70, seed=3), 0.6, 2.0)
    best = b.best("t1")
    assert best is not None
    assert best.crop.shape[:2] == (240, 400)


def test_the_shortlist_is_bounded_per_track():
    b = BestFrameBuffer(per_track=3)
    for i in range(20):
        b.offer("t1", crop(100 + i, 60, seed=i), 0.5, float(i))
    assert len(b.candidates("t1")) == 3


def test_a_worse_crop_than_everything_held_is_not_copied():
    """`offer` copies only when it keeps — otherwise a busy scene pays a
    memcpy per detection for crops it discards immediately."""
    b = BestFrameBuffer(per_track=2)
    b.offer("t1", crop(400, 240, seed=1), 0.9, 0.0)
    b.offer("t1", crop(380, 230, seed=2), 0.9, 1.0)
    before = b.accepted
    b.offer("t1", crop(20, 12, seed=3), 0.1, 2.0)        # far worse
    assert b.accepted == before


def test_tracks_do_not_share_shortlists():
    b = BestFrameBuffer(per_track=2)
    b.offer("t1", crop(400, 240, seed=1), 0.9, 0.0)
    b.offer("t2", crop(100, 60, seed=2), 0.5, 0.0)
    assert b.best("t1").crop.shape[:2] == (240, 400)
    assert b.best("t2").crop.shape[:2] == (60, 100)


def test_an_empty_crop_is_ignored():
    b = BestFrameBuffer()
    b.offer("t1", np.zeros((0, 0, 3), dtype=np.uint8), 0.9, 0.0)
    assert b.best("t1") is None


# ── the bounds that matter ───────────────────────────────────────────────────
def test_the_byte_ceiling_is_enforced_across_all_tracks():
    """The limit that actually protects the process: per-track bounds scale
    with traffic, this one does not."""
    b = BestFrameBuffer(per_track=3, max_bytes=512 * 1024)
    for t in range(40):
        for i in range(3):
            b.offer(f"track{t}", crop(200, 200, seed=t * 10 + i), 0.9, float(i))
    snap = b.snapshot()
    assert snap["bytes_held"] <= snap["max_bytes"]
    assert snap["evicted_for_budget"] > 0


def test_crops_are_stored_at_source_resolution():
    """Never downscaled to fit a budget. A plate is small and this is exactly
    the pixels ANPR needs."""
    b = BestFrameBuffer(per_track=1)
    original = crop(517, 313, seed=5)
    b.offer("t1", original, 0.9, 0.0)
    assert b.best("t1").crop.shape == original.shape


def test_forgetting_a_track_releases_its_bytes():
    b = BestFrameBuffer(per_track=3)
    for i in range(3):
        b.offer("t1", crop(300, 200, seed=i), 0.9, float(i))
    held = b.snapshot()["bytes_held"]
    assert held > 0
    b.forget(["t1"])
    assert b.snapshot()["bytes_held"] == 0
    assert b.best("t1") is None


def test_forgetting_an_unknown_track_is_harmless():
    BestFrameBuffer().forget(["never-existed"])


# ── L. the pipeline can actually reach it ────────────────────────────────────
def test_the_pipeline_offers_vehicle_crops_and_reads_the_best_one():
    """Shortlisting and reading now sit either side of the emit boundary:
    _process collects candidates while it still has the frame, and _emit reads
    the best one for observations the policy accepted."""
    import inspect
    from analytics import pipeline as pipeline_mod

    proc = inspect.getsource(pipeline_mod.DetectionPipeline._process)
    emit = inspect.getsource(pipeline_mod.DetectionPipeline._emit)
    assert "_best_frames.offer" in proc, "vehicle crops are never shortlisted"
    assert "_best_frames.best" in emit, "the shortlist is never read"
    # Candidates are collected BEFORE the policy decides, so a good frame is
    # ready when an observation IS recorded — the best look is often not the
    # one that triggers the record.
    assert proc.index("_best_frames.offer") < proc.index("_policy.decide")


def test_anpr_still_reads_full_resolution_pixels():
    """The crop handed to the reader comes from `frame`, the decoded
    full-resolution image — never a downscaled copy and never a substream.

    This service is the last place the full frame exists: it sends a crop, not
    a frame, so if the pixels were downscaled here nothing downstream could
    recover them.
    """
    import inspect
    from analytics import pipeline as pipeline_mod

    proc = inspect.getsource(pipeline_mod.DetectionPipeline._process)
    assert "region = frame[y1:y2, x1:x2]" in proc
    emit = inspect.getsource(pipeline_mod.DetectionPipeline._emit)
    assert "crop = frame[y1:y2, x1:x2]" in emit
    for downscaler in ("resize", "INTER_AREA", "thumbnail"):
        assert downscaler not in emit,             f"{downscaler!r} in _emit: plate pixels are being degraded"
