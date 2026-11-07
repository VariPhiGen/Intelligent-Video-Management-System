"""
Retention manager — periodically deletes segments older than the configured
retention period and cleans up expired extracted clips.

Each pass runs in this order:
  1. Age-based cleanup per camera (retention_minutes).
  2. Orphan cleanup of unindexed .ts files past the same age cutoff.
  3. Global size-cap enforcement (max_storage_bytes), if configured: oldest
     segments across all cameras are deleted until total indexed size is back
     under the cap. This is a safety net for the case where age-based
     retention isn't tight enough to keep the disk under control.
  4. Expired clip cleanup (clip_ttl_minutes).
  5. HLS artifact cache eviction (hls_cache_ttl_minutes), LRU by mtime —
     the serving path touches an artifact on every hit, so a segment being
     scrubbed over repeatedly outlives one nobody has opened.

Orphan cleanup detail: .ts files that were never indexed (e.g. the last
in-progress segment when ffmpeg was killed on a container restart) are
invisible to the normal indexed-segment cleanup. We catch them in step 2: any
.ts file in a camera's segment directory that (a) is not in the DB and (b) is
older than the camera's retention cutoff is deleted alongside expired indexed
segments each pass.
"""

import logging
import shutil
import threading
import time
from pathlib import Path

from storage.index import SegmentIndex

logger = logging.getLogger(__name__)

_GB = 1024 ** 3
MINUTES_PER_DAY = 1440


class RetentionManager:
    # Emergency disk-floor eviction (see enforce_disk_floor). Measured against
    # actual filesystem free space, NOT indexed bytes — so it also accounts for
    # clips, the SQLite WAL, grooming temp files and anything else on the volume,
    # the blind spot that let a full disk silently stop all recording. The floor
    # aligns with the engine's DISK_CRIT_PCT (95% used = 5% free); recovery leaves
    # a margin above it so a single pass doesn't re-trigger every health cycle.
    DISK_FLOOR_FREE_PCT = 5.0     # trigger emergency eviction below this free-%
    DISK_TARGET_FREE_PCT = 8.0    # evict until at least this much is free again
    DISK_FLOOR_MAX_PASSES = 20    # bound the evict/recheck loop per invocation

    # ── How often the sweep runs (see sweep_interval) ────────────────────────
    # Retention used to be whole days and the pass was simply hourly: an hour
    # of slack on a thirty-day promise is nothing. Minute-granular retention
    # breaks that assumption outright — a camera set to ten minutes on an
    # hourly sweep holds footage for up to seventy, and an operator has no way
    # to tell whether the setting took effect at all.
    #
    # So the interval follows the SHORTEST retention on the appliance, divided
    # by SWEEP_DIVISOR, which bounds the overshoot at 1/4 of the period. It is
    # clamped at both ends: never more often than MIN (a sweep is real I/O and
    # nothing below a minute can be honoured anyway, since segments are 60 s),
    # never less often than MAX (the long-standing hourly floor, which is what
    # a fleet of thirty-day cameras still gets).
    SWEEP_DIVISOR = 4
    MIN_SWEEP_INTERVAL_S = 60
    MAX_SWEEP_INTERVAL_S = 3600
    # The steps that are NOT age-based deletion — orphan globbing, clip and HLS
    # eviction — stay on the old hourly cadence however fast the sweep runs.
    # They walk directories and read the whole index per camera, and none of
    # them is what a short retention is asking to happen sooner: a ten-minute
    # camera needs its SEGMENTS deleted on time, not its HLS cache swept forty
    # times an hour. Without this split, shortening one camera's retention
    # would quietly multiply the I/O cost of every camera on the box.
    HOUSEKEEPING_INTERVAL_S = 3600
    def __init__(self, index: SegmentIndex, cameras_config: list[dict],
                 clips_path: str, clip_ttl_minutes: int = 30,
                 hls_cache_ttl_minutes: int = 360,
                 storage_path: str | None = None,
                 max_storage_bytes: int | None = None,
                 default_retention_minutes: int = 30 * 1440,
                 retention_map_provider=None,
                 max_storage_provider=None,
                 clock_jump_threshold_s: float = 300.0):
        self._index = index
        # Clock-jump guard state (see run_once). We remember the wall-clock and
        # monotonic times of the previous pass; a large disagreement between the
        # two deltas means the wall clock stepped, and age-based deletion (which
        # trusts the wall clock) must pause for that pass.
        self._clock_jump_threshold = clock_jump_threshold_s
        self._prev_wall: float | None = None
        self._prev_mono: float | None = None
        self._cameras = {c["name"]: c for c in cameras_config}
        self._clips_path = Path(clips_path)
        self._clip_ttl = clip_ttl_minutes * 60  # seconds
        # HLS artifacts are worth keeping far longer than clips: they are the
        # thing that makes re-seeking free, and rebuilding one costs an ffmpeg
        # run. They are also self-invalidating (keyed on the source segment's
        # size+mtime), so a long TTL cannot serve stale footage.
        self._hls_ttl = hls_cache_ttl_minutes * 60
        self._hls_path = Path(clips_path) / "hls"
        self._storage_path = Path(storage_path) if storage_path else None
        self._max_storage_bytes = max_storage_bytes
        # Optional callable returning the current size cap in bytes (or None for
        # uncapped). Lets a UI-edited cap take effect on the next pass without a
        # restart — mirrors retention_map_provider. Falls back to the static
        # max_storage_bytes when absent.
        self._max_storage_provider = max_storage_provider
        self._default_minutes = default_retention_minutes
        # Optional callable returning {camera: retention_minutes} for cameras
        # added at runtime (the registry-sync path) — the static YAML list alone
        # would silently exempt them from age-based cleanup and fill the disk.
        self._retention_provider = retention_map_provider
        # Storage-pressure history, for the warning surfaces (storage/alerts.py).
        # Recorded as EVENTS rather than inferred later from how much footage is
        # on disk: a short on-disk span means "the cap is shortening retention"
        # only if something actually evicted, and looks identical on a camera
        # that was added yesterday. An inference would cry wolf on every new
        # install; an event cannot.
        self._pressure_lock = threading.Lock()
        self._size_cap_events = 0
        self._size_cap_last_at: float | None = None
        self._size_cap_last_freed = 0
        self._disk_floor_events = 0
        self._disk_floor_last_at: float | None = None
        # Serializes emergency disk-floor eviction: it can be invoked both by the
        # engine's 30s disk-pressure callback (health-monitor thread) and by the
        # hourly run_once pass (retention thread). The two-phase delete is
        # individually safe, but the lock stops them double-counting / racing.
        self._disk_floor_lock = threading.Lock()
        # Monotonic stamp of the last pass that did the hourly housekeeping.
        # None = never, so the first pass does it.
        self._last_housekeeping_mono: float | None = None

    def _camera_retention_map(self) -> dict[str, int]:
        """Effective {camera: retention_MINUTES} for this pass.

        Union of: every camera present in the segment index (so footage of
        deleted cameras still ages out), the static YAML config, and the
        runtime provider (engine). Provider wins, then YAML, then default.

        cameras.yaml may spell a camera's retention in either unit;
        `retention_minutes` wins and `retention_days` is converted, so the
        rest of this class only ever sees minutes.
        """
        minutes: dict[str, int] = {}
        try:
            for name in self._index.get_cameras():
                minutes[name] = self._default_minutes
        except Exception:
            logger.exception("Retention: failed to enumerate index cameras")
        for name, cfg in self._cameras.items():
            if cfg.get("retention_minutes"):
                minutes[name] = int(cfg["retention_minutes"])
            elif cfg.get("retention_days"):
                minutes[name] = int(cfg["retention_days"]) * MINUTES_PER_DAY
            else:
                minutes[name] = self._default_minutes
        if self._retention_provider is not None:
            try:
                for name, m in (self._retention_provider() or {}).items():
                    if m:
                        minutes[name] = int(m)
            except Exception:
                logger.exception("Retention: runtime retention provider failed")
        return minutes

    def sweep_interval(self) -> float:
        """Seconds to wait before the next pass, paced by the SHORTEST retention.

        Called by the loop in main.py after every pass, not once at startup:
        retention is edited from the UI at runtime, so a value computed at boot
        would leave a camera that was just set to ten minutes being swept on
        whatever cadence the fleet had an hour ago.
        """
        shortest = self.shortest_retention_minutes()
        if not shortest:
            return float(self.MAX_SWEEP_INTERVAL_S)
        want = (shortest * 60) / self.SWEEP_DIVISOR
        return float(min(self.MAX_SWEEP_INTERVAL_S,
                         max(self.MIN_SWEEP_INTERVAL_S, want)))

    def run_once(self) -> dict[str, int]:
        """Run one retention pass. Returns {camera: files_deleted}."""
        results = {}
        now = time.time()

        # Clock-jump guard. Age-based deletion trusts the wall clock (cutoff =
        # now - retention). A stepped wall clock — NTP correcting a drifted RTC,
        # or an offline appliance booting with the wrong time — would push the
        # cutoff far forward and mass-delete recent footage, or far back and
        # silently stop aging footage out. We compare the wall-clock delta since
        # the previous pass against the monotonic delta (monotonic never steps);
        # if they disagree by more than the threshold, the wall clock moved and
        # we skip time-based deletion this pass, resuming once it's stable. The
        # first pass after startup has no baseline, so it also defers — cleanup
        # is delayed one interval, never performed against an untrusted clock.
        mono = time.monotonic()
        age_cleanup_ok = True
        if self._prev_mono is None:
            age_cleanup_ok = False
            logger.info("Retention: establishing clock baseline — deferring "
                        "age-based cleanup for one pass")
        else:
            skew = abs((now - self._prev_wall) - (mono - self._prev_mono))
            if skew > self._clock_jump_threshold:
                age_cleanup_ok = False
                logger.warning(
                    "Retention: clock jump detected (wall moved %.0fs vs "
                    "monotonic %.0fs, skew %.0fs > %.0fs) — skipping age-based "
                    "deletion this pass to avoid mass-deleting or over-retaining "
                    "footage",
                    now - self._prev_wall, mono - self._prev_mono, skew,
                    self._clock_jump_threshold,
                )
        self._prev_wall, self._prev_mono = now, mono

        # Is this the pass that also does the hourly housekeeping? See
        # HOUSEKEEPING_INTERVAL_S: age-based segment deletion has to keep up
        # with the shortest retention on the box, the directory walks do not.
        housekeeping_due = (
            self._last_housekeeping_mono is None
            or (mono - self._last_housekeeping_mono) >= self.HOUSEKEEPING_INTERVAL_S
        )
        if housekeeping_due:
            self._last_housekeeping_mono = mono

        if age_cleanup_ok:
            for name, retention_minutes in self._camera_retention_map().items():
                cutoff = now - (retention_minutes * 60)

                # Delete indexed segments older than cutoff
                deleted_paths = self._index.delete_before(name, cutoff)
                for p in deleted_paths:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning("Failed to delete %s: %s", p, e)

                # Delete orphan .ts files (not in DB) older than cutoff.
                # Hourly only: it globs the camera directory and reads every
                # indexed path for the camera, which is the expensive half of
                # this pass, and an unindexed stray costs disk rather than a
                # broken retention promise.
                orphans = self._clean_orphans(name, cutoff) if housekeeping_due else 0

                total = len(deleted_paths) + orphans
                results[name] = total
                if total:
                    logger.info(
                        "Retention: camera %s — deleted %d indexed segment(s), "
                        "%d orphan(s)",
                        name, len(deleted_paths), orphans,
                    )

        # Global size-cap enforcement (safety net beyond per-camera age).
        # Ordering-based (oldest segments first), NOT wall-clock based, so it
        # stays safe and active even when a clock jump has paused age cleanup —
        # it remains the disk safety net. Read live so a UI change to the cap
        # applies on the next pass.
        limit = self._size_limit()
        if limit is not None:
            total_before = self._index.total_size()
            if total_before > limit:
                self._enforce_size_cap(total_before, limit)

        # Emergency disk-floor safety net (real free space, always on). The
        # engine's 30s disk callback is the fast trigger; this makes every
        # retention pass enforce it too, so the floor holds even if the
        # callback is unwired. Not throttled with the housekeeping: it is one
        # statvfs when there is no pressure, and it is the last thing standing
        # between a full disk and recording stopping altogether.
        self.enforce_disk_floor()

        # Clean expired clips (mtime-based, so also skipped on an untrusted
        # clock). Hourly, for the same reason as the orphan sweep: these have
        # their own TTLs and are not what a short retention is asking for.
        if age_cleanup_ok and housekeeping_due:
            clip_count = self._clean_clips(now)
            if clip_count:
                logger.info("Retention: deleted %d expired clips", clip_count)
            hls_count = self._clean_hls_cache(now)
            if hls_count:
                logger.info("Retention: evicted %d stale HLS artifacts", hls_count)

        return results

    def pressure_snapshot(self) -> dict:
        """What has actually evicted, for the warning surfaces. Never blocks
        long: the lock only ever guards six integer assignments."""
        with self._pressure_lock:
            return {
                "size_cap_evictions": self._size_cap_events,
                "size_cap_last_at": self._size_cap_last_at,
                "size_cap_last_freed_bytes": self._size_cap_last_freed,
                "disk_floor_evictions": self._disk_floor_events,
                "disk_floor_last_at": self._disk_floor_last_at,
            }

    def configured_retention_minutes(self) -> int | None:
        """The longest retention any camera is configured for — the promise most
        at risk when the cap starts evicting. None when no camera is known."""
        try:
            mins = [int(m) for m in self._camera_retention_map().values() if m]
        except Exception:
            logger.exception("Retention: camera map unavailable")
            return None
        return max(mins) if mins else None

    def shortest_retention_minutes(self) -> int | None:
        """The SHORTEST retention any camera is configured for.

        This is what paces the sweep (main.run_retention_loop). An hourly pass
        was fine while a day was the finest period expressible — an hour of
        slack on thirty days is nothing. It is not fine for a camera kept for
        ten minutes, which would hold footage for up to seventy: the policy
        would be off by a factor of seven, and the operator would have no way
        to tell whether the setting had taken effect at all.
        """
        try:
            mins = [int(m) for m in self._camera_retention_map().values() if m]
        except Exception:
            logger.exception("Retention: camera map unavailable")
            return None
        return min(mins) if mins else None

    def _size_limit(self) -> int | None:
        """Current size cap in bytes (None = uncapped). Provider wins, so a
        UI-edited value is picked up live; else the static constructor value."""
        if self._max_storage_provider is not None:
            try:
                return self._max_storage_provider()
            except Exception:
                logger.exception("Retention: size-cap provider failed")
        return self._max_storage_bytes

    def _delete_candidates(self, candidates: list[tuple[int, str, int]],
                           *, reason: str) -> int:
        """Two-phase delete a batch of (id, path, size): unlink files first,
        then remove index rows only for the ones whose unlink succeeded. If disk
        IO fails (EROFS, NFS, permissions) the row stays in the index and the
        next pass retries it — keeps DB↔FS in sync without leaking disk. Returns
        bytes actually freed. Shared by the size cap and the disk-floor evictor.
        """
        deleted_ids: list[int] = []
        bytes_freed = 0
        for seg_id, path, size in candidates:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as e:
                logger.warning("%s: failed to delete %s: %s", reason, path, e)
                continue
            deleted_ids.append(seg_id)
            bytes_freed += size
        self._index.delete_by_ids(deleted_ids)
        return bytes_freed

    def _enforce_size_cap(self, total_before: int, limit: int) -> None:
        """Delete oldest segments globally until total INDEXED size <= the cap."""
        candidates = self._index.select_oldest_until_under(limit)
        bytes_freed = self._delete_candidates(candidates, reason="Size-cap")
        total_after = self._index.total_size()
        with self._pressure_lock:
            self._size_cap_events += 1
            self._size_cap_last_at = time.time()
            self._size_cap_last_freed = bytes_freed
        logger.info(
            "Retention size-cap: total %.2f GB > limit %.2f GB; "
            "deleted %d segment(s), freed %.2f GB → now %.2f GB",
            total_before / _GB, limit / _GB, len(candidates),
            bytes_freed / _GB, total_after / _GB,
        )

    def enforce_disk_floor(self) -> int:
        """Emergency eviction: if the storage volume's *filesystem* free space
        has fallen below DISK_FLOOR_FREE_PCT, delete globally-oldest segments
        until free space recovers to DISK_TARGET_FREE_PCT. Returns bytes freed.

        This is the missing safety net that keeps a full disk from silently
        stopping ALL recording (ffmpeg exits on write failure). It differs from
        _enforce_size_cap in two load-bearing ways:
          • it measures real disk-free (shutil.disk_usage), so it accounts for
            clips, the SQLite WAL, grooming temp files and any other volume
            tenant — the indexed-bytes cap is blind to all of those;
          • it is unconditional — it runs even with no size cap configured.
        Ordering-based (oldest first), like the size cap, so it is safe during a
        clock jump. The open, mid-write segment is not indexed yet, so it can
        never be selected. Loud by design: this deletes footage regardless of
        retention_minutes to keep recording alive (availability over retention).
        When per-camera retention floors land (gap 7) this becomes the explicit
        last-resort path that may cross a floor, and must alert when it does.
        """
        if self._storage_path is None:
            return 0
        # Non-blocking: if an eviction is already running (engine callback vs the
        # hourly pass), let it finish rather than queue a second pass.
        if not self._disk_floor_lock.acquire(blocking=False):
            return 0
        try:
            try:
                usage = shutil.disk_usage(self._storage_path)
            except OSError as e:
                logger.error("Disk-floor: cannot stat %s: %s", self._storage_path, e)
                return 0
            floor = usage.total * self.DISK_FLOOR_FREE_PCT / 100.0
            target = usage.total * self.DISK_TARGET_FREE_PCT / 100.0
            if usage.free >= floor:
                return 0  # not critical

            # Regenerable bytes go first. The HLS cache is derived entirely
            # from segments still on disk, so dropping it costs a re-remux on
            # the next seek, whereas everything below this line costs footage.
            purged = self._purge_hls_cache()
            if purged:
                logger.warning("Disk-floor: dropped %.2f GB of HLS cache "
                               "before evicting footage", purged / (1024 ** 3))
                try:
                    usage = shutil.disk_usage(self._storage_path)
                except OSError:
                    pass
                if usage.free >= target:
                    return 0

            with self._pressure_lock:
                self._disk_floor_events += 1
                self._disk_floor_last_at = time.time()

            logger.critical(
                "Disk-floor EMERGENCY on %s: %.2f GB free < %.2f GB floor "
                "(%.1f%%) — age-based retention was not enough; evicting oldest "
                "footage to recover to %.2f GB free, IGNORING retention_minutes.",
                self._storage_path, usage.free / _GB, floor / _GB,
                self.DISK_FLOOR_FREE_PCT, target / _GB,
            )

            total_freed = 0
            for _ in range(self.DISK_FLOOR_MAX_PASSES):
                usage = shutil.disk_usage(self._storage_path)
                if usage.free >= target:
                    break
                deficit = target - usage.free
                indexed = self._index.total_size()
                if indexed <= 0:
                    logger.critical(
                        "Disk-floor: still %.2f GB short but no indexed segments "
                        "remain — the volume is full of non-segment data; cannot "
                        "recover by evicting footage.", deficit / _GB,
                    )
                    break
                # Ask for the oldest segments summing to >= the deficit, with a
                # 10% margin so filesystem block-rounding / metadata overhead
                # doesn't leave us just short and spinning.
                want = min(indexed, int(deficit * 1.1))
                candidates = self._index.select_oldest_until_under(indexed - want)
                if not candidates:
                    break
                freed = self._delete_candidates(candidates, reason="Disk-floor")
                total_freed += freed
                if freed == 0:
                    logger.error(
                        "Disk-floor: selected %d segment(s) but freed 0 bytes "
                        "(unlink failures?) — aborting to avoid a hot loop.",
                        len(candidates),
                    )
                    break

            final = shutil.disk_usage(self._storage_path)
            logger.critical(
                "Disk-floor: freed %.2f GB → %.2f GB free (target %.2f GB).",
                total_freed / _GB, final.free / _GB, target / _GB,
            )
            return total_freed
        finally:
            self._disk_floor_lock.release()

    def _clean_orphans(self, camera: str, cutoff: float) -> int:
        """Delete unindexed .ts files in the camera dir that are past the cutoff.

        Uses mtime as the age proxy — reliable enough because ffmpeg sets mtime
        to the wall-clock time it finishes writing the segment.
        """
        if self._storage_path is None:
            return 0
        cam_dir = self._storage_path / camera
        if not cam_dir.is_dir():
            return 0

        try:
            indexed = self._index.get_indexed_filepaths_since(camera, 0)
        except Exception:
            logger.exception("Retention: failed to query index for orphan check (%s)", camera)
            return 0

        count = 0
        for ts_file in cam_dir.glob("*.ts"):
            try:
                if str(ts_file) in indexed:
                    continue
                if ts_file.stat().st_mtime < cutoff:
                    ts_file.unlink()
                    count += 1
                    logger.debug("Retention: deleted orphan %s", ts_file.name)
            except OSError as e:
                logger.warning("Retention: could not delete orphan %s: %s", ts_file, e)
        return count

    def _purge_hls_cache(self) -> int:
        """Delete every cached HLS artifact. Returns bytes reclaimed.

        Used only by the disk-floor emergency: these files are derived, so
        they are the cheapest thing on the volume to give back.
        """
        if not self._hls_path.exists():
            return 0
        freed = 0
        for f in self._hls_path.rglob("*"):
            try:
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink()
            except OSError:
                pass
        return freed

    def _clean_hls_cache(self, now: float) -> int:
        """Evict HLS artifacts untouched for longer than the cache TTL.

        Also removes a camera's directory once it empties, so purging a camera
        that is never coming back doesn't leave a stub per camera forever.
        """
        if not self._hls_path.exists():
            return 0
        count = 0
        for cam_dir in self._hls_path.iterdir():
            if not cam_dir.is_dir():
                continue
            for f in cam_dir.iterdir():
                try:
                    if not f.is_file():
                        continue
                    if (now - f.stat().st_mtime) > self._hls_ttl:
                        f.unlink()
                        count += 1
                except OSError:
                    pass
            try:
                if not any(cam_dir.iterdir()):
                    cam_dir.rmdir()
            except OSError:
                pass
        return count

    def _clean_clips(self, now: float) -> int:
        """Delete extracted clips older than clip_ttl."""
        if not self._clips_path.exists():
            return 0
        count = 0
        for f in self._clips_path.glob("*.mp4"):
            try:
                if (now - f.stat().st_mtime) > self._clip_ttl:
                    f.unlink()
                    count += 1
            except OSError:
                pass
        return count
