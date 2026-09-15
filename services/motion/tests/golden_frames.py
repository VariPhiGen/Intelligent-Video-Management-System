"""The frames the golden fixture was captured from.

DETERMINISTIC AND SHARED. Each service replays these to check its own copy of
the motion algorithm still produces what the original implementations did, so
all three must generate byte-identical frames. They do not import each other —
separate Docker build contexts — so this file is synced by
scripts/sync_motion.py alongside the algorithm and the fixture.

The fixture records a sha256 over these frames. If numpy ever changes what its
seeded generator emits, the golden tests fail saying the INPUTS changed rather
than leaving someone to conclude the algorithm broke.
"""
from __future__ import annotations

import cv2
import numpy as np


def synthetic_frames(n: int, w: int = 1920, h: int = 1080):
    """Deterministic frames with a moving body, a size change, a scene change
    and a quiet stretch — one of each branch the gate can take."""
    rng = np.random.default_rng(7)
    base = rng.integers(0, 45, (h, w, 3), dtype=np.uint8)
    for i in range(n):
        f = base.copy()
        if i % 37 == 36:                      # scene change: everything moves
            f = rng.integers(90, 255, (h, w, 3), dtype=np.uint8)
        elif i % 11 < 7:                      # a walker, growing as it nears
            x = 150 + (i * 23) % (w - 400)
            hh = 180 + (i % 11) * 40
            cv2.rectangle(f, (x, h - hh - 60), (x + hh // 3, h - 60),
                          (215, 215, 215), -1)
            if i % 5 == 0:                    # a second, smaller object
                cv2.rectangle(f, (w - 500, 300), (w - 440, 430), (200, 190, 180), -1)
        # else: quiet frame, nothing drawn
        yield f
