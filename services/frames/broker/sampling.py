"""sampling.py — which grabbed frame to publish.

WHAT TIME A FRAME HAPPENED is decided in clock.py, by one contract for every
camera: never after it arrived, never more than 5 s before, strictly increasing,
spaced like the source when the source can be trusted and at the stream's own
frame rate when it cannot. This module only chooses WHICH frames to publish,
and it chooses on that normalized time.

THE BUG THIS REPLACED (2026-09-17). The loop used to publish when 200 ms of
WALL CLOCK had passed since the last publish. Frames do not arrive evenly: on
this appliance the camera relay is reached over a WAN hop and arrival gaps
measured p50 5.7 ms, p90 85 ms, p99 434 ms, max 3.3 s - frames come in bursts.
A wall-clock gate publishes roughly ONE FRAME PER BURST, so a 5 FPS target
delivered 2.5-3.3 FPS. Measured on cam3: 2.82 FPS with the wall-clock rule,
5.00 FPS with a content-time rule.

SO "EVERY 200 ms" MEANS 200 ms OF VIDEO - of normalized time, which follows the
video's own spacing - whatever the network did on the way here.

AFTER A GAP, NO CATCH-UP BURST. If the timeline moved on by more than a whole
interval past the deadline, the deadline is resynchronised to the frame, so a
stream that was away for three seconds resumes at the sample rate instead of
dumping three seconds of video downstream at once.
"""
from __future__ import annotations

from typing import Optional

from .clock import AUTO, StreamClock

DEADLINE_SLACK_S = 1e-6


class FrameSampler:
    """Decides, per grabbed frame, whether to publish it and with what timestamp.

    One per capture session: construct it when the stream opens and throw it
    away when the stream ends, so nothing is carried across a reconnect.
    Everything it needs to know about the source is what the source declares
    (`source_fps`) and how long it took to open - no camera is special.
    """

    def __init__(self, sample_fps: float, source_fps: float = 0.0,
                 policy: str = AUTO, open_seconds: float = 0.0) -> None:
        self.interval_s = 1.0 / sample_fps if sample_fps > 0 else 0.5
        self.clock = StreamClock(declared_fps=source_fps, policy=policy,
                                 open_seconds=open_seconds)
        self._next_due: Optional[float] = None
        self._last_ts: Optional[float] = None
        self.published = 0
        self.skipped = 0

    @property
    def mode(self) -> str:
        """The clock discipline in use: "source" or "paced" (see clock.py)."""
        return self.clock.mode

    def consider(self, pts_ms, now: float) -> Optional[float]:
        """The timestamp to publish this frame with, or None to skip it.

        `pts_ms` is whatever the source stamped (anything, including nothing);
        `now` is when the frame arrived. Every grabbed frame goes through the
        clock, published or not - the clock's statistics need all of them.
        """
        t = self.clock.normalize(pts_ms, now)
        if self._next_due is None:
            self._next_due = t                  # the first frame is always due
        # A microsecond of slack: in seconds, a frame exactly on its deadline
        # can compute a hair below it and be skipped for nothing.
        if t < self._next_due - DEADLINE_SLACK_S or (
                self._last_ts is not None and t <= self._last_ts):
            self.skipped += 1
            return None
        # Advance the DEADLINE, not the frame: a frame usually lands a little
        # past its deadline (frames ~52 ms apart on one of these sources), and
        # advancing from the frame would add that overshoot to every cycle -
        # measured as 4.4 FPS against a 5 FPS target. If the timeline moved on
        # by more than a whole interval, it had a real gap: resync to the frame
        # instead, so the recovery is one frame rather than a catch-up burst.
        self._next_due += self.interval_s
        if t - self._next_due > self.interval_s:
            self._next_due = t + self.interval_s
        self._last_ts = t
        self.published += 1
        return t

    def snapshot(self) -> dict:
        return {"sample_fps": round(1.0 / self.interval_s, 3),
                "published": self.published, "skipped": self.skipped,
                **self.clock.snapshot()}
