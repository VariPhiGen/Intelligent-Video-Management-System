"""Unit tests for stacked (two-line) plate reading — index/plates.py.

`_recognise` resizes every region to a fixed 128x64 — a 2:1 single-line shape —
and the model behind it reads one line. Handed a stacked plate (~1.4:1) it reads
straight across both rows and drops a character, and it does so CONFIDENTLY:
measured on a real truck, the stacked plate read `JH10AF793` at 0.979-0.994
while the correctly-shaped single-line plate on the same bumper read at 0.924
and LOST, because `read()` ranks candidates by confidence and confidence is not
comparable across region geometries.

That is not an edge case on this site: 93 of 109 measured plate regions were
near-square and 69 clustered at ~1.5.

What is pinned here is the guard rather than the happy path. This override
replaces an answer the model already accepted, so a bad override is worse than
the truncation it fixes — a wrong plate that looks well-formed is harder to
doubt than an obviously short one.

No model is loaded: `_read_two_line` is pure given `_recognise`, which is
stubbed. Runs anywhere.

Run: python3 -m pytest tests -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.plates import PlateRead, PlateReader  # noqa: E402


def reader(rows, *, row_conf=0.80, min_len=4):
    """A PlateReader whose `_recognise` returns canned reads, top row first.

    Built without __init__ on purpose: the real one loads an ONNX model, and
    the logic under test never touches it.
    """
    r = object.__new__(PlateReader)
    r._two_line_aspect = 2.2
    r._two_line_row_conf = row_conf
    r._min_len = min_len
    r.two_line_reads = 0
    seq = list(rows)
    r._recognise = lambda region: seq.pop(0) if seq else None
    return r


REGION = np.zeros((60, 90, 3), dtype=np.uint8)   # near-square: a stacked plate
WHOLE = PlateRead(text="JH10AF793", confidence=0.98)   # the truncated read


def test_rows_are_joined_when_both_are_confident():
    """The case this exists for: 9 characters become the real 10."""
    r = reader([PlateRead("JH10", 1.0), PlateRead("AF7931", 0.96)])
    got = r._read_two_line(REGION, WHOLE)
    assert got.text == "JH10AF7931"
    assert r.two_line_reads == 1


def test_confidence_is_a_length_weighted_mean_of_the_rows():
    """`plate_confidence` means 'mean character probability' everywhere else;
    averaging two means would quietly change what the column reports."""
    r = reader([PlateRead("JH10", 1.0), PlateRead("AF7931", 0.90)])
    got = r._read_two_line(REGION, WHOLE)
    assert got.confidence == pytest.approx((1.0 * 4 + 0.90 * 6) / 10)


def test_a_weak_row_is_rejected_rather_than_believed():
    """The measured failure: one crop's bottom row scored 0.679 and produced
    `JH10LAF7931` — an invented character. Trading a truncation for a wrong
    plate is not an improvement, so the whole-region read stands."""
    r = reader([PlateRead("JH10", 0.999), PlateRead("LAF7931", 0.679)])
    assert r._read_two_line(REGION, WHOLE) is WHOLE
    assert r.two_line_reads == 0


def test_a_split_that_recovers_nothing_is_rejected():
    """The defect is LOST characters. A join no longer than what the single
    pass already read has not demonstrated it saw anything the other missed."""
    r = reader([PlateRead("JH10", 1.0), PlateRead("AF79", 1.0)])   # 8 <= 9
    assert r._read_two_line(REGION, WHOLE) is WHOLE


def test_equal_length_is_also_rejected_not_just_shorter():
    r = reader([PlateRead("JH10A", 1.0), PlateRead("F7931", 1.0)])  # 10 vs 10
    assert r._read_two_line(REGION, PlateRead("JH10AF7931", 0.9)) is not None
    r2 = reader([PlateRead("JH10", 1.0), PlateRead("AF793", 1.0)])  # 9 == 9
    assert r2._read_two_line(REGION, WHOLE) is WHOLE


def test_an_undecodable_row_falls_back():
    assert reader([None, PlateRead("AF7931", 1.0)])._read_two_line(REGION, WHOLE) is WHOLE
    assert reader([PlateRead("JH10", 1.0), None])._read_two_line(REGION, WHOLE) is WHOLE


def test_the_join_still_has_to_clear_min_length():
    """With no whole-region read to fall back to, the join is the only
    candidate — and a two-character join is a non-plate like any other."""
    r = reader([PlateRead("JH", 1.0), PlateRead("10", 1.0)], min_len=6)
    assert r._read_two_line(REGION, None) is None


def test_it_works_when_the_single_pass_read_nothing_at_all():
    r = reader([PlateRead("JH10", 1.0), PlateRead("AF7931", 1.0)])
    got = r._read_two_line(REGION, None)
    assert got.text == "JH10AF7931"


def test_the_join_is_normalised():
    """Row reads carry the model's punctuation and spacing; the stored plate
    must not, or an exact-match lookup silently fails."""
    r = reader([PlateRead("JH-10", 1.0), PlateRead("AF 7931", 1.0)])
    assert r._read_two_line(REGION, WHOLE).text == "JH10AF7931"


def test_a_region_too_short_to_split_is_left_alone():
    tiny = np.zeros((4, 40, 3), dtype=np.uint8)
    assert reader([])._read_two_line(tiny, WHOLE) is WHOLE


@pytest.mark.parametrize("conf", [0.0, 0.5, 0.799])
def test_the_row_floor_is_enforced_on_both_rows(conf):
    assert reader([PlateRead("JH10", conf), PlateRead("AF7931", 1.0)]) \
        ._read_two_line(REGION, WHOLE) is WHOLE
    assert reader([PlateRead("JH10", 1.0), PlateRead("AF7931", conf)]) \
        ._read_two_line(REGION, WHOLE) is WHOLE


def test_row_floor_sits_well_above_min_confidence():
    """Overriding an accepted answer needs more evidence than accepting one.
    If these ever converge, a 0.6 row could overturn a 0.99 whole-plate read."""
    from analytics.config import PlateConfig
    c = PlateConfig()
    assert c.two_line_min_row_confidence > c.min_confidence
