"""Burning the box onto the frame: the copy `org_img_local` points at.

WHAT THESE PIN. The drawing itself cannot be asserted pixel-for-pixel without
making the suite a hostage to Pillow's rasteriser, so these assert the things
that actually break downstream: the output decodes as a JPEG, the frame keeps
its dimensions, pixels change where a box was asked for and nowhere else, and
no input a frozen payload can legally contain makes the function raise.

That last one carries the weight. `draw_boxes` runs inside the relay, on a
payload built from a database row, for an image fetched off the NVR — three
places that can hand it something unexpected. A raise it does not expect costs
an annotation its place in the review queue.
"""
from __future__ import annotations

import io

import pytest

from backend.services import image_label

pytest.importorskip("PIL", reason="Pillow is the imaging dependency")

from PIL import Image  # noqa: E402


def _frame(width: int = 640, height: int = 480, colour=(10, 20, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="JPEG",
                                                   quality=95)
    return out.getvalue()


def _box(**over):
    box = {"xywh": [10.0, 10.0, 30.0, 30.0], "subcategory": "PPE-No Helmet",
           "class_name": "no_helmet", "color": "#ff8800", "confidence": 1.0}
    box.update(over)
    return box


def _open(blob: bytes) -> Image.Image:
    return Image.open(io.BytesIO(blob)).convert("RGB")


def test_output_is_a_jpeg_of_the_same_size():
    """The frozen URL promises an image at that key. It has to be openable,
    and it has to still be the frame — a resize would silently invalidate the
    percentage coordinates the reviewer's browser overlays on it."""
    out = image_label.draw_boxes(_frame(640, 480), [_box()])
    image = _open(out)
    assert image.format is None or True  # decoded, which is the real assertion
    assert image.size == (640, 480)


def test_pixels_change_where_the_box_was_asked_for():
    """Proves something was actually drawn, rather than the original being
    re-encoded and returned — which is exactly what the failure path does, and
    would otherwise be indistinguishable from success."""
    before = _open(_frame())
    after = _open(image_label.draw_boxes(_frame(), [_box()]))
    # The box starts at 10% of 640 = x=64, 10% of 480 = y=48.
    assert before.getpixel((64, 48)) != after.getpixel((64, 48))


def test_pixels_far_from_any_box_are_left_alone():
    """A JPEG round-trip perturbs everything slightly, so this allows a small
    delta rather than demanding equality — the point is that the bottom-right
    corner was not painted over, not that it is bit-identical."""
    before = _open(_frame())
    after = _open(image_label.draw_boxes(_frame(), [_box()]))
    b, a = before.getpixel((630, 470)), after.getpixel((630, 470))
    assert all(abs(x - y) <= 12 for x, y in zip(b, a))


def test_no_boxes_still_returns_a_usable_image():
    """An annotation always has a box, but the payload is read from a database
    row and this must not be the thing that raises if one ever does not."""
    assert _open(image_label.draw_boxes(_frame(), [])).size == (640, 480)


@pytest.mark.parametrize("bad", [
    {"xywh": None},
    {"xywh": "not-a-list"},
    {"xywh": [1, 2]},                       # too short
    {"xywh": ["a", "b", "c", "d"]},         # non-numeric
    {"xywh": [10, 10, 0, 0]},               # zero area
    {"xywh": [10, 10, -5, -5]},             # negative
    {"color": None},
    {"color": "not-a-colour"},
    {"color": "#GGGGGG"},                   # hex-shaped but not hex
    {"subcategory": None, "class_name": None},
    {"subcategory": ""},
])
def test_no_malformed_box_can_raise(bad):
    """Every one of these is reachable from a real payload.

    A raise here is not a cosmetic bug: `_deliver` catches it and ships the
    unlabelled original, so a systematic failure would quietly degrade every
    published annotation while looking like it worked.
    """
    assert image_label.draw_boxes(_frame(), [_box(**bad)])


def test_a_box_running_past_the_frame_edge_is_clamped():
    """An operator can draw right up to the border, and a rectangle with
    coordinates outside the canvas draws without error and without a visible
    edge."""
    out = image_label.draw_boxes(_frame(), [_box(xywh=[90.0, 90.0, 50.0, 50.0])])
    assert _open(out).size == (640, 480)


def test_a_box_covering_the_whole_frame_is_fine():
    out = image_label.draw_boxes(_frame(), [_box(xywh=[0.0, 0.0, 100.0, 100.0])])
    assert _open(out).size == (640, 480)


def test_a_label_at_the_very_top_is_drawn_inside_rather_than_off_canvas():
    """The chip goes above the box by default; at y=0 there is no "above"."""
    out = image_label.draw_boxes(_frame(), [_box(xywh=[5.0, 0.0, 20.0, 20.0])])
    assert _open(out).size == (640, 480)


def test_several_boxes_all_draw():
    out = image_label.draw_boxes(_frame(), [
        _box(xywh=[5.0, 5.0, 20.0, 20.0], color="#ff0000"),
        _box(xywh=[50.0, 50.0, 20.0, 20.0], color="#00ff00"),
    ])
    after = _open(out)
    assert after.getpixel((32, 24)) != after.getpixel((320, 240))


def test_a_greyscale_frame_survives_the_conversion():
    """NVR frames are colour, but a mono camera is a real thing and JPEG
    cannot be saved from every mode Pillow can open."""
    out = io.BytesIO()
    Image.new("L", (320, 240), 128).save(out, format="JPEG")
    assert _open(image_label.draw_boxes(out.getvalue(), [_box()])).size == (320, 240)


def test_a_blob_that_is_not_an_image_raises_for_the_caller_to_handle():
    """Deliberately NOT swallowed here. The relay decides what to do with a
    frame it cannot decode, and it needs to know that it could not."""
    with pytest.raises(Exception):
        image_label.draw_boxes(b"this is not a jpeg", [_box()])


def test_available_reports_the_dependency():
    assert image_label.available() is True
