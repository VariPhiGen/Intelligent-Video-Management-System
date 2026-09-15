#!/usr/bin/env python3
"""timecode.py — read the burned-in EPOCH back out of recorded video.

WHY NOT OCR. Tesseract was tried first and is not good enough: on a clean,
high-contrast, monospace render of `EPOCH 1789055315` it returned
`01789055312` — a hallucinated leading digit and a wrong final one. A playback
test whose central assertion is "the video shows the second we asked for"
cannot be built on a reader that is right most of the time, and a tolerance
wide enough to absorb OCR error would also absorb the bug being hunted.

WHAT THIS DOES INSTEAD. The font, size and colour are fixed by publish.sh, so
the digits are the same bitmaps every time. The decoder therefore:

  1. renders its own reference strip — `EPOCH 0123456789` — with the SAME
     ffmpeg drawtext invocation, so the glyphs cannot drift from the ones under
     test even if the fixture's font changes;
  2. thresholds the green channel (the EPOCH line is 0x00FF88 on black);
  3. segments into glyphs by columns that contain any lit pixel, splitting
     letters from digits at the widest gap — the space;
  4. matches each glyph to the nearest reference by Hamming distance over a
     normalised bitmap.

Nearest-neighbour rather than exact equality because H.264 is lossy: a glyph
that survives a re-encode is the same shape but not the same pixels. Measured
distances on real recorded frames are 0-14 against the correct digit, with the
runner-up far behind, so the classification is unambiguous even though the
bitmaps are not identical.

Output is JSON on stdout: one decoded epoch per extracted frame, in order.

    docker run --rm -v /tmp/clips:/w vms-e2e-rtsp-cam \\
        python3 /timecode.py /w/clip.mp4
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image

FONT = os.environ.get("TIMECODE_FONT", "/usr/share/fonts/dejavu/DejaVuSansMono.ttf")

#: Where publish.sh draws the EPOCH line, plus slack for the descender.
BAND_TOP, BAND_BOTTOM, BAND_RIGHT = 55, 125, 1000
#: Green above this is text; the background is pure black.
GREEN_THRESHOLD = 130
#: Normalised glyph size for comparison. Large enough that 3 and 8 do not
#: collide, small enough that compression noise averages out.
CELL = (16, 22)


def _binarize(img: Image.Image) -> Image.Image:
    return img.convert("RGB").split()[1].point(
        lambda v: 255 if v > GREEN_THRESHOLD else 0)


def _glyph_columns(bw: Image.Image, min_gap: int = 2) -> list[tuple[int, int]]:
    """Runs of columns containing lit pixels — one per glyph."""
    w, h = bw.size
    lit = [any(bw.getpixel((x, y)) for y in range(h)) for x in range(w)]
    runs: list[tuple[int, int]] = []
    start, gap = None, 0
    for x, on in enumerate(lit):
        if on:
            if start is None:
                start = x
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                runs.append((start, x - gap + 1))
                start, gap = None, 0
    if start is not None:
        runs.append((start, w))
    return runs


def _tighten(bw: Image.Image, x0: int, x1: int) -> Image.Image | None:
    w, h = bw.size
    rows = [y for y in range(h) if any(bw.getpixel((x, y)) for x in range(x0, x1))]
    if not rows:
        return None
    return bw.crop((x0, rows[0], x1, rows[-1] + 1))


def _signature(cell: Image.Image) -> tuple[int, ...]:
    small = cell.resize(CELL, Image.LANCZOS).point(lambda v: 1 if v > 110 else 0)
    return tuple(small.getdata())


def _digit_cells(path: str) -> list[Image.Image]:
    """The glyphs AFTER the space — i.e. the number, not the word EPOCH."""
    band = Image.open(path).crop((0, BAND_TOP, BAND_RIGHT, BAND_BOTTOM))
    bw = _binarize(band)
    runs = _glyph_columns(bw)
    if len(runs) < 2:
        return []
    gaps = [(i, runs[i][0] - runs[i - 1][1]) for i in range(1, len(runs))]
    split = max(gaps, key=lambda t: t[1])[0]
    cells = [_tighten(bw, a, b) for a, b in runs[split:]]
    return [c for c in cells if c is not None]


def _reference(workdir: str) -> list[tuple[int, ...]]:
    """Render `EPOCH 0123456789` with the fixture's own filter and learn it."""
    ref_png = os.path.join(workdir, "_tc_reference.png")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=1280x200:r=1",
         "-vf", f"drawtext=fontfile={FONT}:text='EPOCH 0123456789'"
                f":x=24:y=64:fontsize=54:fontcolor=0x00FF88",
         "-frames:v", "1", "-y", ref_png],
        check=True,
    )
    cells = _digit_cells(ref_png)
    if len(cells) != 10:
        raise SystemExit(f"reference produced {len(cells)} digits, expected 10")
    return [_signature(c) for c in cells]


def _decode(path: str, refs: list[tuple[int, ...]]) -> int | None:
    out = ""
    for cell in _digit_cells(path):
        sig = _signature(cell)
        best, best_d = None, None
        for value, ref in enumerate(refs):
            d = sum(1 for a, b in zip(sig, ref) if a != b)
            if best_d is None or d < best_d:
                best, best_d = value, d
        out += str(best)
    return int(out) if out.isdigit() and len(out) >= 9 else None


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: timecode.py <video> [fps]")
    video, fps = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "1")

    with tempfile.TemporaryDirectory() as work:
        refs = _reference(work)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video,
             "-vf", f"fps={fps}", "-y", os.path.join(work, "f_%03d.png")],
            check=True,
        )
        frames = sorted(glob.glob(os.path.join(work, "f_*.png")))
        epochs = [_decode(f, refs) for f in frames]

    print(json.dumps({"frames": len(frames), "epochs": epochs}))


if __name__ == "__main__":
    main()
