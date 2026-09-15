"""Unit tests for sitemap upload validation (backend/models.py).

Magic-byte sniffing is the security boundary for uploads — the filename and
client Content-Type are attacker-controlled. Run via the containerized pytest
command in the plan's Global Constraints.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.models import (  # noqa: E402
    CalibrationBody,
    HaPlacementsBody,
    SITEMAP_MAX_BYTES,
    image_dimensions,
    sniff_image_content_type,
    validate_sitemap_dimensions,
)
from pydantic import ValidationError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
SVG_NO_DECL = b"  <svg viewBox='0 0 10 10'></svg>"
SVG_DOCTYPE = (
    b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"'
    b' "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
    b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
)
SVG_COMMENT = b"<!-- floor plan -->\n<svg xmlns='http://www.w3.org/2000/svg'></svg>"


@pytest.mark.parametrize("data,expected", [
    (PNG, "image/png"),
    (JPEG, "image/jpeg"),
    (WEBP, "image/webp"),
    (SVG, "image/svg+xml"),
    (SVG_NO_DECL, "image/svg+xml"),
    (SVG_DOCTYPE, "image/svg+xml"),
    (SVG_COMMENT, "image/svg+xml"),
])
def test_sniffs_allowlisted_types(data, expected):
    assert sniff_image_content_type(data) == expected


@pytest.mark.parametrize("data", [
    b"GIF89a" + b"\x00" * 32,                       # GIF: not allowlisted
    b"%PDF-1.7" + b"\x00" * 32,                     # PDF
    b"<html><script>alert(1)</script></html>",      # HTML masquerading
    b"<!doctype html><html><svg>x</svg></html>",    # HTML doctype smuggling an <svg>
    b"<!-- hi --><html><svg>x</svg></html>",        # comment hiding <html>
    b"",                                            # empty
    b"\x00" * 64,                                   # junk
])
def test_rejects_everything_else(data):
    assert sniff_image_content_type(data) is None


def test_size_cap_is_ten_mb():
    assert SITEMAP_MAX_BYTES == 10 * 1024 * 1024


# ── Georeferencing calibration (migration 026) ────────────────────────────────

def _pt(x=0.5, y=0.5, lat=28.6, lng=77.2):
    return {"x": x, "y": y, "lat": lat, "lng": lng}


def test_calibration_accepts_two_points():
    b = CalibrationBody(points=[_pt(0, 0, 28.61, 77.20), _pt(1, 1, 28.60, 77.21)])
    assert len(b.points) == 2


def test_calibration_accepts_three_points():
    b = CalibrationBody(points=[_pt(), _pt(0, 0), _pt(1, 0)])
    assert len(b.points) == 3


def test_calibration_empty_clears():
    assert CalibrationBody(points=[]).points == []
    assert CalibrationBody().points == []


@pytest.mark.parametrize("n", [1, 4, 5])
def test_calibration_rejects_bad_count(n):
    with pytest.raises(ValidationError):
        CalibrationBody(points=[_pt() for _ in range(n)])


@pytest.mark.parametrize("bad", [
    {"x": -0.1, "y": 0.5, "lat": 0, "lng": 0},    # x below 0
    {"x": 1.5, "y": 0.5, "lat": 0, "lng": 0},     # x above 1
    {"x": 0.5, "y": 0.5, "lat": 200, "lng": 0},   # lat out of range
    {"x": 0.5, "y": 0.5, "lat": 0, "lng": 999},   # lng out of range
    {"x": 0.5, "y": 0.5, "lat": 0},               # missing lng
    {"x": 0.5, "y": 0.5, "lat": 0, "lng": 0, "z": 1},  # extra key forbidden
])
def test_calibration_rejects_bad_point(bad):
    with pytest.raises(ValidationError):
        CalibrationBody(points=[bad, _pt()])


# ── HA device placement (migration 028) ──────────────────────────────────────
# The Map tab replaces the whole list on every edit, so the body is the only
# guard against a placement that would render off the plan or twice over.

def test_ha_placements_accepts_empty_and_normalized():
    assert HaPlacementsBody(devices=[]).devices == []
    body = HaPlacementsBody(devices=[
        {"id": "siren-zb", "x": 0.0, "y": 1.0},
        {"id": "floodlight-pn", "x": 0.5, "y": 0.5},
    ])
    assert [d.id for d in body.devices] == ["siren-zb", "floodlight-pn"]


@pytest.mark.parametrize("x,y", [(-0.01, 0.5), (1.01, 0.5), (0.5, -0.01), (0.5, 1.01)])
def test_ha_placements_rejects_off_plan(x, y):
    with pytest.raises(ValidationError):
        HaPlacementsBody(devices=[{"id": "siren-zb", "x": x, "y": y}])


def test_ha_placements_rejects_duplicate_device():
    with pytest.raises(ValidationError):
        HaPlacementsBody(devices=[
            {"id": "siren-zb", "x": 0.1, "y": 0.1},
            {"id": "siren-zb", "x": 0.9, "y": 0.9},
        ])


def test_ha_placements_rejects_blank_id_and_extra_keys():
    with pytest.raises(ValidationError):
        HaPlacementsBody(devices=[{"id": "", "x": 0.1, "y": 0.1}])
    with pytest.raises(ValidationError):
        HaPlacementsBody(devices=[{"id": "siren-zb", "x": 0.1, "y": 0.1, "name": "x"}])


# ── Sitemap dimensions ───────────────────────────────────────────────────────
# The byte cap does not bound decode cost — a flat-colour 12000x12000 PNG is
# under half a megabyte and expands to ~550 MB of RGBA in the browser — so the
# upload reads dimensions from the header and bounds them separately.

def _png(width: int, height: int) -> bytes:
    """Just enough PNG for the header parser: signature + IHDR."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big") + b"IHDR"
        + width.to_bytes(4, "big") + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def test_png_dimensions_from_header():
    assert image_dimensions(_png(1024, 768), "image/png") == (1024, 768)


def test_dimensions_none_for_svg_and_truncated_input():
    # SVG has no pixel size to bound — exempt by design, not by accident.
    assert image_dimensions(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml") is None
    assert image_dimensions(b"\x89PNG\r\n\x1a\n", "image/png") is None
    assert image_dimensions(b"\xff\xd8\xff", "image/jpeg") is None


@pytest.mark.parametrize("w,h", [(1024, 768), (600, 600), (12000, 3000), (900, 7000)])
def test_usable_plans_accepted(w, h):
    assert validate_sitemap_dimensions(w, h) is None


@pytest.mark.parametrize("w,h,expect", [
    (599, 400, "too small"),          # unreadable at 6x zoom, coarse placement
    (13000, 900, "too large"),        # per-side cap
    (9000, 9000, "too many pixels"),  # 81 MP — the decode bomb
    (9000, 1000, "too elongated"),    # 9:1 renders as a strip
    (0, 100, "Could not read"),
])
def test_unusable_plans_rejected(w, h, expect):
    msg = validate_sitemap_dimensions(w, h)
    assert msg is not None and expect in msg
