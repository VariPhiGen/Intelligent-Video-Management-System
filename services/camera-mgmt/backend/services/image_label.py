"""image_label.py — burn an annotation's box onto a copy of its frame.

WHY A SECOND IMAGE AT ALL. The reviewing system draws boxes in the browser
from the coordinates in the payload, so the original frame is enough to review.
It is not enough for anything that leaves that browser: an exported case file,
a screenshot pasted into a report, a downstream consumer reading
`org_img_local`. Those get a flat JPEG with no coordinates attached, and a
frame with no box on it does not say what was annotated.

PURE FUNCTION, BYTES IN AND BYTES OUT. No S3, no database, no config object —
which is what makes the part that has to look right testable without a bucket.
The drawing geometry matches the reviewer's own overlay (stroke width and font
scaled off image width by the same divisors) so the exported copy and the
on-screen one agree.

COORDINATES ARE PERCENTAGES, top-left origin — the same convention the payload
carries, so this function takes exactly what was frozen into the outbox row and
never has to recompute it from pixels.
"""
from __future__ import annotations

import io
from typing import Any, Optional, Sequence

import structlog

log = structlog.get_logger(__name__)

# Matches the reviewing system's own overlay so the burned-in copy and the
# browser-drawn one look like the same thing rather than two renderings.
_STROKE_DIVISOR = 500
_FONT_DIVISOR = 60
_MIN_STROKE = 2
_MIN_FONT = 14

_DEFAULT_COLOUR = "#FF3030"


def available() -> bool:
    """Is the imaging library present?

    Pillow is pinned in requirements, but this module is imported on the relay
    path and a missing optional-looking dependency must degrade to "publish the
    original" rather than take the publish down.
    """
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def draw_boxes(blob: bytes, boxes: Sequence[dict[str, Any]]) -> bytes:
    """Return a JPEG copy of `blob` with every box drawn on it.

    Raises on a blob that is not a decodable image. Callers on the relay path
    treat that as "ship the original instead" rather than as a delivery
    failure — see qa_sink._deliver for why a corrupt frame must not park an
    annotation in the failed state.
    """
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415

    with Image.open(io.BytesIO(blob)) as src:
        # JPEG cannot hold an alpha channel and a source frame may be
        # greyscale or palettised; normalising first means the draw and the
        # save below never have to care which.
        image = src.convert("RGB")

    width, height = image.size
    stroke = max(_MIN_STROKE, round(width / _STROKE_DIVISOR))
    font_size = max(_MIN_FONT, round(width / _FONT_DIVISOR))
    draw = ImageDraw.Draw(image)
    font = _font(font_size)

    for box in boxes or []:
        geometry = _pixels(box.get("xywh"), width, height)
        if geometry is None:
            continue
        x0, y0, x1, y1 = geometry
        colour = _colour(box.get("color"))
        draw.rectangle((x0, y0, x1, y1), outline=colour, width=stroke)

        label = str(box.get("subcategory") or box.get("class_name") or "").strip()
        if label:
            _label(draw, label, x0, y0, colour, font, font_size, stroke)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=92)
    return out.getvalue()


def _pixels(xywh: Any, width: int, height: int
            ) -> Optional[tuple[float, float, float, float]]:
    """Percent [x, y, w, h] -> pixel (x0, y0, x1, y1), or None if unusable.

    Clamped to the frame. A box that runs off the edge is a real thing — an
    operator can draw right up to the border — and Pillow draws a rectangle
    with coordinates outside the canvas without complaint but without the
    edge visible either.
    """
    if not isinstance(xywh, (list, tuple)) or len(xywh) < 4:
        return None
    try:
        x, y, w, h = (float(v) for v in xywh[:4])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    x0 = max(0.0, min(x / 100.0 * width, width - 1))
    y0 = max(0.0, min(y / 100.0 * height, height - 1))
    x1 = max(x0 + 1, min((x + w) / 100.0 * width, width - 1))
    y1 = max(y0 + 1, min((y + h) / 100.0 * height, height - 1))
    return x0, y0, x1, y1


def _colour(value: Any) -> str:
    """The class colour, or the fallback. Validated rather than trusted.

    The colour reaches here from a frozen payload built out of a database row;
    an unparseable one would raise inside Pillow halfway through drawing and
    lose the boxes already drawn.
    """
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text
        except ValueError:
            pass
    return _DEFAULT_COLOUR


def _font(size: int):
    from PIL import ImageFont  # noqa: PLC0415

    # A real TrueType face if the host has one. Not assumed: a slim container
    # image carries no fonts at all, and this module must not depend on one
    # being installed.
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        # Pillow ships a scalable default (10.1+). This is what keeps the
        # label readable on a 1080p frame with no system fonts present —
        # without it the bare `load_default()` below renders a fixed ~11px
        # bitmap that is illegible once the frame is scaled to fit a screen.
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _label(draw, text: str, x0: float, y0: float, colour: str, font,
           font_size: int, stroke: int) -> None:
    """Draw the class name on a filled chip above the box, or inside it.

    Above by default, because a chip inside the box covers the thing the box
    is pointing at. Inside when the box starts too near the top edge, where
    "above" would be off-canvas.
    """
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    except Exception:                                           # noqa: BLE001
        # Older/odd font objects without textbbox. A rough estimate keeps the
        # chip roughly right rather than dropping the label entirely.
        left, top, right, bottom = 0, 0, len(text) * font_size // 2, font_size
    pad = max(2, stroke)
    chip_w = (right - left) + pad * 2
    chip_h = (bottom - top) + pad * 2

    chip_y = y0 - chip_h if y0 - chip_h >= 0 else y0
    draw.rectangle((x0, chip_y, x0 + chip_w, chip_y + chip_h), fill=colour)
    # Black on the class colour: the palette is chosen for visibility against
    # camera footage, which makes it light enough that white text disappears.
    draw.text((x0 + pad - left, chip_y + pad - top), text, fill="#000000",
              font=font)
