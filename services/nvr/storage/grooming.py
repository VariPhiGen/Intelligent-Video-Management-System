"""
Grooming manager — reduces the frame rate of old recordings to reclaim disk
(Milestone-style "grooming"), without re-encoding.

How it works
------------
Instead of the expensive decode → drop-frames → encode pipeline, we exploit the
one frame type that has no dependencies: keyframes. ffmpeg's ``noise`` bitstream
filter can drop every non-keyframe packet at the container level::

    ffmpeg -i seg.ts -c copy -bsf:v "noise=drop=not(key)" groomed.ts

No decode, no encode — the pass is I/O-bound like recording itself. The result
plays as a keyframe-only "slideshow" (one frame per GOP, typically 0.5–1 fps),
which is exactly the evidence-retention tier commercial VMSes produce, at
~10–30% of the original size.

Operational contract
--------------------
- Only segments *fully* older than ``groom_after_days`` are touched, and each
  exactly once (``groomed`` flag in the index).
- Runs inside a configurable nightly window (default 02:00–06:00 local time);
  a pass aborts mid-batch the moment the window closes.
- Replacement is atomic (``os.replace`` within the same directory), so a clip
  extraction holding the old file keeps a valid handle, and the index filepath
  never changes. Timeline coverage is unaffected — same segments, same indexed
  durations, fewer frames inside.
- On any ffmpeg failure the original file is kept untouched and the segment is
  retried on the next pass.

Trade-off: dropping trailing non-keyframes can shorten a segment's *effective*
playable tail by up to one GOP (~1–2 s of a 60 s file). The indexed duration is
intentionally left unchanged so gap detection doesn't sprout false positives.
"""

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from storage.index import SegmentIndex

logger = logging.getLogger(__name__)

# Drop every non-keyframe video packet; pure remux (no transcode).
_GROOM_BSF = "noise=drop=not(key)"
_FFMPEG_TIMEOUT = 120  # seconds per segment — generous; remux takes < 1 s


def _parse_window(spec: str) -> tuple[int, int]:
    """'02:00-06:00' → (start_minutes, end_minutes). Overnight spans allowed."""
    start_s, end_s = spec.split("-")
    sh, sm = (int(x) for x in start_s.strip().split(":"))
    eh, em = (int(x) for x in end_s.strip().split(":"))
    return sh * 60 + sm, eh * 60 + em


class GroomingManager:
    def __init__(self, index: SegmentIndex, groom_after_days: int,
                 window: str = "02:00-06:00", batch_limit: int = 500,
                 ffmpeg_bin: str = "ffmpeg",
                 cameras_config: list[dict] | None = None,
                 groom_map_provider=None):
        self._index = index
        # Appliance default: 0 = grooming disabled unless a camera overrides.
        self._after_days = groom_after_days
        self._win_start, self._win_end = _parse_window(window)
        self._batch_limit = batch_limit
        self._ffmpeg = ffmpeg_bin
        self._cameras = {c["name"]: c for c in (cameras_config or [])}
        # Optional callable returning {camera: groom_after_days} for cameras
        # added at runtime (registry sync) — mirrors RetentionManager's
        # retention_map_provider. Provider wins, then YAML, then default.
        self._groom_provider = groom_map_provider

    def in_window(self, now: float | None = None) -> bool:
        t = datetime.fromtimestamp(now if now is not None else time.time())
        minutes = t.hour * 60 + t.minute
        if self._win_start <= self._win_end:
            return self._win_start <= minutes < self._win_end
        return minutes >= self._win_start or minutes < self._win_end  # overnight

    def _camera_groom_map(self) -> dict[str, int]:
        """Effective {camera: groom_after_days} for this pass; 0 = don't groom.

        Union of every camera in the segment index (default), the static YAML
        config, and the runtime provider. Provider wins, then YAML, then the
        appliance default.
        """
        days: dict[str, int] = {}
        try:
            for name in self._index.get_cameras():
                days[name] = self._after_days
        except Exception:
            logger.exception("Grooming: failed to enumerate index cameras")
        for name, cfg in self._cameras.items():
            days[name] = cfg.get("groom_after_days", self._after_days)
        if self._groom_provider is not None:
            try:
                for name, d in (self._groom_provider() or {}).items():
                    if d is not None:
                        # `is not None`, not truthiness: the provider sends 0 to
                        # mean "never groom this camera" (sub tracks do), and
                        # dropping it here handed them the appliance default —
                        # the exact rewrite they were opting out of.
                        days[name] = d
            except Exception:
                logger.exception("Grooming: runtime groom provider failed")
        return days

    def run_once(self, force: bool = False) -> dict[str, int]:
        """Groom one batch of eligible segments, per-camera thresholds.

        ``force=True`` ignores the time window (manual / --groom-now runs).
        Returns {'groomed': n, 'failed': n, 'bytes_saved': n}.
        """
        now = time.time()
        candidates: list[tuple[int, str, str, int]] = []
        for camera, after_days in self._camera_groom_map().items():
            if not after_days or after_days <= 0:
                continue  # grooming off for this camera
            remaining = self._batch_limit - len(candidates)
            if remaining <= 0:
                break
            candidates.extend(self._index.select_groomable(
                now - after_days * 86400, remaining, camera=camera))
        stats = {"groomed": 0, "failed": 0, "bytes_saved": 0}
        if not candidates:
            return stats

        logger.info("Grooming: %d segment(s) past their camera's threshold",
                    len(candidates))
        for seg_id, camera, filepath, old_size in candidates:
            if not force and not self.in_window():
                logger.info("Grooming: window closed mid-batch; resuming tomorrow")
                break
            new_size = self._groom_file(filepath)
            if new_size is None:
                stats["failed"] += 1
                continue
            self._index.mark_groomed(seg_id, new_size)
            stats["groomed"] += 1
            stats["bytes_saved"] += max(0, old_size - new_size)

        if stats["groomed"] or stats["failed"]:
            logger.info(
                "Grooming pass: %d groomed, %d failed, %.2f GB reclaimed",
                stats["groomed"], stats["failed"],
                stats["bytes_saved"] / (1024 ** 3),
            )
        if stats["failed"] and not stats["groomed"]:
            logger.error(
                "Grooming: every attempt failed — check that ffmpeg supports "
                "the 'noise' bitstream filter drop expression (ffmpeg >= 5.0)"
            )
        return stats

    def _groom_file(self, filepath: str) -> int | None:
        """Rewrite ``filepath`` keyframe-only. Returns new size, or None."""
        src = Path(filepath)
        if not src.is_file():
            # File vanished (e.g. retention raced us) — nothing to do; mark by
            # returning its absence as failure so the row ages out naturally.
            return None
        tmp = src.with_suffix(".groom.tmp")
        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-map", "0", "-c", "copy",
            "-bsf:v", _GROOM_BSF,
            "-f", "mpegts", str(tmp),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT,
            )
            if proc.returncode != 0:
                logger.warning("Grooming failed for %s: %s",
                               src.name, proc.stderr.strip()[:300])
                tmp.unlink(missing_ok=True)
                return None
            new_size = tmp.stat().st_size
            if new_size <= 0:
                logger.warning("Grooming produced empty output for %s", src.name)
                tmp.unlink(missing_ok=True)
                return None
            os.replace(tmp, src)   # atomic within the same directory
            return new_size
        except subprocess.TimeoutExpired:
            logger.warning("Grooming timed out for %s", src.name)
            tmp.unlink(missing_ok=True)
            return None
        except OSError as e:
            logger.warning("Grooming IO error for %s: %s", src.name, e)
            tmp.unlink(missing_ok=True)
            return None


def run_grooming_loop(groomer: GroomingManager, stop_event,
                      check_interval: int = 300) -> None:
    """Nightly scheduler: poll every ``check_interval``; groom inside the window."""
    while not stop_event.is_set():
        try:
            if groomer.in_window():
                groomer.run_once()
        except Exception:
            logger.exception("Grooming pass crashed; continuing")
        stop_event.wait(check_interval)
