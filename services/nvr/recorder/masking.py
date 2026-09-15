"""masking.py — privacy-mask support for the recording pipeline.

Masks arrive as polygons in NORMALIZED coordinates ([[[x, y], ...], ...] with
0 <= x,y <= 1). To burn them into a recording we:

  1. probe the stream's native resolution with ffprobe (masks are rasterized
     at exactly that size so ffmpeg's filter needs no scaling), then
  2. render all polygons onto a transparent PNG (Pillow) whose ALPHA channel
     marks the masked region (the fill colour is irrelevant — StreamWorker
     uses alphaextract to pull the stencil out), and
  3. hand the PNG to StreamWorker, whose ffmpeg command Gaussian-blurs the
     region and re-encodes (libx264) instead of stream-copying.

The PNG lives next to the camera's segments and is regenerated on every
ffmpeg (re)start — a resolution change on the camera (e.g. via the stream
settings UI) is picked up on the next reconnect cycle automatically.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SEC = 15


def parse_masks(raw: str | None) -> list:
    """Parse and sanity-check a masks JSON string; invalid input → [] (loud)."""
    if not raw:
        return []
    try:
        masks = json.loads(raw)
        assert isinstance(masks, list)
        for poly in masks:
            assert isinstance(poly, list) and len(poly) >= 3
            for pt in poly:
                assert len(pt) == 2 and all(0.0 <= float(v) <= 1.0 for v in pt)
        return masks
    except (ValueError, AssertionError, TypeError) as exc:
        logger.error("Ignoring invalid privacy masks payload: %s", exc)
        return []


def probe_resolution(rtsp_url: str, transport: str = "tcp") -> tuple[int, int]:
    """Native width/height of the stream's first video track (raises on failure)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", transport,
        "-timeout", "5000000",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        rtsp_url,
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SEC
    )
    parts = (out.stdout or "").strip().split(",")
    if out.returncode != 0 or len(parts) < 2:
        raise RuntimeError(
            f"ffprobe could not read stream resolution (rc={out.returncode}): "
            f"{(out.stderr or '').strip()[:200]}"
        )
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0:
        raise RuntimeError(f"ffprobe returned nonsense resolution {width}x{height}")
    return width, height


def render_mask_png(masks: list, width: int, height: int, out_path: Path) -> Path:
    """Rasterize normalized polygons at WxH as an alpha stencil (opaque inside
    the region, transparent outside). The fill colour is unused downstream —
    StreamWorker reads only the alpha channel to blur the region."""
    from PIL import Image, ImageDraw  # lazy: only masked cameras need Pillow

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for poly in masks:
        points = [(float(x) * width, float(y) * height) for x, y in poly]
        if len(points) >= 3:
            draw.polygon(points, fill=(0, 0, 0, 255))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    logger.info("Rendered %d privacy mask(s) at %dx%d -> %s",
                len(masks), width, height, out_path)
    return out_path
