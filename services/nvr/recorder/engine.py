"""
Recording Engine — orchestrates StreamWorkers for all configured cameras.

This is the top-level controller that:
1. Reads camera config
2. Creates one StreamWorker per camera
3. Monitors health (restarts dead workers, alarms on indexer staleness)
4. Periodically reconciles the SQLite index against the filesystem
5. Provides a clean shutdown path

Health monitor cadence:
- Liveness check (restart dead workers): every HEALTH_CHECK_INTERVAL (30s)
- Indexer staleness alarm: every HEALTH_CHECK_INTERVAL (30s)
- FS↔DB reconciliation: every RECONCILIATION_INTERVAL (600s)

The reconciliation is the safety net that catches *any* indexing failure
mode — known or unknown — by comparing what's on disk against what's in
the index and adding any missing rows. The staleness alarm is purely
informational: it does NOT auto-restart workers, because a stalled
indexer with new files on disk is a different problem from a camera that
went offline (no new files), and an over-eager restart would thrash a
genuinely-offline camera.

The one stall that IS actionable — and that the staleness alarm alone
could not fix — is a *wedged* ffmpeg: alive and still writing bytes, but
no longer rolling segments (one ever-growing .ts, indexer frozen). The
segment-roll watchdog (step 1b) restarts exactly that case, distinguishing
it from an offline camera by requiring the open segment to still be
growing. See StreamWorker.is_segment_stalled().
"""

import logging
import shutil
import threading
import time
from pathlib import Path

from recorder.stream_worker import StreamWorker
from storage import capacity
from storage.index import SegmentIndex

logger = logging.getLogger(__name__)


class RecordingEngine:
    HEALTH_CHECK_INTERVAL = 30          # seconds between health monitor passes
    RECONCILIATION_INTERVAL = 600       # seconds between FS↔DB reconciliation
    STALENESS_FACTOR = 3                # alarm if lag > N * segment_duration
    DISK_WARN_PCT = 90                  # log WARNING above this used-% threshold
    DISK_CRIT_PCT = 95                  # log CRITICAL above this used-% threshold

    def __init__(self, config: dict, index: SegmentIndex):
        self._config = config
        self._index = index
        self._workers: dict[str, StreamWorker] = {}
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_reconcile_at: float = 0.0
        # Latest disk-usage snapshot, updated every health-monitor cycle.
        # Keys: path, total_gb, used_gb, free_gb, used_pct, state
        # state ∈ {"ok", "warning", "critical"}
        self._disk_state: dict = {"state": "unknown"}
        # Invoked (best-effort) when the disk crosses critical, so an emergency
        # evictor can free space before ffmpeg starts failing writes. Wired to
        # RetentionManager.enforce_disk_floor in main.py; None until then.
        self._disk_pressure_callback = None

        self._storage_path = Path(config["recording"]["storage_path"])
        rec_cfg = config["recording"]
        ff_cfg = config.get("ffmpeg", {})

        # Per-camera retention (days), covering runtime-added cameras too so the
        # RetentionManager can honor the value passed via POST /cameras/{name}.
        self._retention_days: dict[str, int] = {}
        # Per-camera groom-after overrides (days before footage is remuxed
        # keyframe-only). Absent = appliance default (recording.groom_after_days).
        self._groom_after_days: dict[str, int] = {}

        for cam in config["cameras"]:
            worker = StreamWorker(
                camera_name=cam["name"],
                rtsp_url=cam["rtsp_url"],
                storage_path=self._storage_path,
                index=index,
                segment_duration=rec_cfg.get("segment_duration", 60),
                rtsp_transport=ff_cfg.get("rtsp_transport", "tcp"),
                reconnect_delay=ff_cfg.get("reconnect_delay", 5),
                ffmpeg_loglevel=ff_cfg.get("loglevel", "warning"),
            )
            self._workers[cam["name"]] = worker
            if cam.get("retention_days"):
                self._retention_days[cam["name"]] = cam["retention_days"]
            if cam.get("groom_after_days") is not None:
                # `is not None`, not truthiness: 0 is a real setting ("never
                # groom this camera"), distinct from having no override at all.
                self._groom_after_days[cam["name"]] = cam["groom_after_days"]

    def get_retention_map(self) -> dict[str, int]:
        """Per-camera retention_days for cameras added at runtime or via config."""
        return dict(self._retention_days)

    def set_disk_pressure_callback(self, callback) -> None:
        """Register a zero-arg callable invoked when the disk crosses critical
        (see _check_disk_space). Used to wire RetentionManager.enforce_disk_floor
        so space is reclaimed within a health cycle, not at the next hourly pass."""
        self._disk_pressure_callback = callback

    def set_retention(self, name: str, retention_days: int) -> None:
        """Update a camera's retention without restarting its worker.

        Works whether or not the camera currently has a live worker — a
        removed camera's footage keeps aging out, so its policy must stay
        updatable too. Takes effect on the retention manager's next pass.
        """
        self._retention_days[name] = retention_days
        logger.info("Retention for '%s' set to %dd", name, retention_days)

    def get_groom_map(self) -> dict[str, int]:
        """Per-camera groom_after_days overrides (runtime or config)."""
        return dict(self._groom_after_days)

    def set_groom_after(self, name: str, groom_after_days: int | None) -> None:
        """Set a camera's groom-after override. No worker restart; the grooming
        manager picks it up on its next pass. Same live-or-not semantics as
        set_retention.

        Three states, and they are three because two could not express what a
        sub track needs:
          None -> no override; the camera follows the appliance default
          0    -> NEVER groom this camera, whatever the appliance default is
          >0   -> groom segments older than this many days

        0 used to mean "clear the override", which made "never groom" unsayable:
        a sub track asking not to be groomed was read as asking for the default
        and got rewritten keyframe-only — a slideshow, which is the one thing a
        scrub track must never become.
        """
        if groom_after_days is None:
            self._groom_after_days.pop(name, None)
            logger.info("Groom-after for '%s' cleared (appliance default)", name)
        else:
            self._groom_after_days[name] = groom_after_days
            logger.info("Groom-after for '%s' set to %s", name,
                        f"{groom_after_days}d" if groom_after_days > 0 else "never")

    def start(self) -> None:
        """Start all camera workers and the health monitor."""
        logger.info("Starting recording engine with %d cameras",
                     len(self._workers))
        for worker in self._workers.values():
            worker.start()

        self._stop_event.clear()
        self._last_reconcile_at = time.time()
        self._monitor_thread = threading.Thread(
            target=self._health_monitor, name="rec-monitor", daemon=True
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        """Stop all workers gracefully."""
        logger.info("Stopping recording engine")
        self._stop_event.set()
        for worker in self._workers.values():
            worker.stop()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)

    def add_camera(self, name: str, rtsp_url: str,
                   retention_days: int = 30,
                   masks: list | None = None,
                   groom_after_days: int | None = None) -> None:
        """Add a new camera at runtime without stopping existing recordings.

        ``masks`` (normalized privacy-mask polygons) opts the worker into the
        overlay + re-encode path; None/empty keeps zero-CPU stream copy.
        """
        if name in self._workers:
            raise ValueError(f"Camera '{name}' already exists")

        storage_path = Path(self._config["recording"]["storage_path"])
        rec_cfg = self._config["recording"]
        ff_cfg = self._config.get("ffmpeg", {})

        worker = StreamWorker(
            camera_name=name,
            rtsp_url=rtsp_url,
            storage_path=storage_path,
            index=self._index,
            segment_duration=rec_cfg.get("segment_duration", 60),
            rtsp_transport=ff_cfg.get("rtsp_transport", "tcp"),
            reconnect_delay=ff_cfg.get("reconnect_delay", 5),
            ffmpeg_loglevel=ff_cfg.get("loglevel", "warning"),
            masks=masks,
        )
        self._workers[name] = worker
        self._retention_days[name] = retention_days
        if groom_after_days is not None:
            # 0 included: see set_groom_after — it means "never groom", not
            # "no preference".
            self._groom_after_days[name] = groom_after_days
        worker.start()
        logger.info("Added and started camera '%s' at runtime (retention %dd, %d mask(s))",
                    name, retention_days, len(masks or []))

    def remove_camera(self, name: str) -> None:
        """Stop and remove a camera at runtime.

        Deliberately keeps the camera's retention entry: its already-recorded
        footage stays on disk and must continue aging out on the same policy.
        """
        if name not in self._workers:
            raise ValueError(f"Camera '{name}' not found")
        self._workers[name].stop()
        del self._workers[name]
        logger.info("Removed camera '%s'", name)

    def get_status(self) -> dict[str, dict]:
        """Return per-camera health metrics and system state for the API.

        Shape:
            {camera_name: {alive, last_indexed_at, index_lag_seconds,
                           segments_indexed, current_backoff},
             "__disk__": {path, total_gb, used_gb, free_gb, used_pct, state}}
        """
        status = {name: w.get_metrics() for name, w in self._workers.items()}
        status["__disk__"] = self._disk_state
        return status

    def _health_monitor(self) -> None:
        """Periodically check worker health, alarm on staleness, reconcile."""
        segment_duration = self._config["recording"].get("segment_duration", 60)
        staleness_threshold = self.STALENESS_FACTOR * segment_duration

        while not self._stop_event.is_set():
            now = time.time()

            # 1. Liveness: restart workers whose thread/process has died.
            for name, worker in list(self._workers.items()):
                if not worker.is_alive and not self._stop_event.is_set():
                    logger.warning("Worker for '%s' is dead, restarting", name)
                    try:
                        worker.stop()
                        worker.start()
                    except Exception:
                        logger.exception("Failed to restart worker '%s'", name)

            # 1b. Segment-roll watchdog: restart a worker whose ffmpeg is alive
            #     but wedged — pouring bytes into one ever-growing .ts without
            #     rolling a new segment (indexer frozen, playback sees nothing
            #     new). This is distinct from a dead worker (handled above) and
            #     from an offline camera (open segment old but NOT growing —
            #     left to -timeout/backoff). It's the state that previously
            #     required a manual VMS restart.
            for name, worker in list(self._workers.items()):
                if self._stop_event.is_set():
                    break
                try:
                    if worker.is_segment_stalled():
                        logger.error(
                            "Camera '%s': ffmpeg wedged — segment open >%gx "
                            "segment_duration and still growing (not rolling); "
                            "restarting worker to recover recording.",
                            name, StreamWorker.WEDGE_ROLL_FACTOR,
                        )
                        worker.stop()
                        worker.start()
                except Exception:  # never let one camera kill the monitor
                    logger.exception(
                        "Segment-roll watchdog failed for '%s'", name
                    )

            # 2. Staleness alarm (informational; no auto-restart).
            for name, worker in list(self._workers.items()):
                metrics = worker.get_metrics()
                lag = metrics["index_lag_seconds"]
                if lag is None:
                    # Worker never indexed anything yet — could be cold start
                    # or a camera that's been offline since boot. Skip until
                    # the first segment lands.
                    continue
                if lag > staleness_threshold:
                    logger.warning(
                        "Camera '%s': indexer stale — last segment indexed "
                        "%.1fs ago (threshold %ds, segments_indexed=%d)",
                        name, lag, staleness_threshold,
                        metrics["segments_indexed"],
                    )

            # 3. Disk-space check — runs every cycle (cheap shutil call).
            self._check_disk_space(self._storage_path)

            # 4. Reconciliation (every RECONCILIATION_INTERVAL).
            if now - self._last_reconcile_at >= self.RECONCILIATION_INTERVAL:
                self._reconcile_all()
                self._last_reconcile_at = now

            self._stop_event.wait(self.HEALTH_CHECK_INTERVAL)

    def _check_disk_space(self, path: Path) -> None:
        """Sample disk usage for `path` and update self._disk_state.

        Logs WARNING once usage crosses DISK_WARN_PCT and CRITICAL once it
        crosses DISK_CRIT_PCT.  Both thresholds re-fire every health-monitor
        cycle so the condition stays visible in log tails / alerting pipelines
        that watch for repeated log lines.
        """
        try:
            usage = shutil.disk_usage(path)
        except OSError as e:
            logger.error("Disk check failed for %s: %s", path, e)
            self._disk_state = {"state": "unknown", "error": str(e)}
            return

        total_gb = usage.total / 1024 ** 3
        used_gb  = usage.used  / 1024 ** 3
        free_gb  = usage.free  / 1024 ** 3
        used_pct = 100.0 * usage.used / usage.total if usage.total else 0.0

        if used_pct >= self.DISK_CRIT_PCT:
            state = "critical"
            logger.critical(
                "DISK FULL — storage path %s is %.1f%% full "
                "(%.1f GB free of %.1f GB total). "
                "ffmpeg will exit silently when the filesystem is full "
                "and recording will STOP. Free space immediately.",
                path, used_pct, free_gb, total_gb,
            )
            # Fast-path emergency eviction, so space is reclaimed within a
            # health cycle rather than at the next hourly retention pass.
            # Best-effort: a failure here must never take down the monitor.
            if self._disk_pressure_callback is not None:
                try:
                    self._disk_pressure_callback()
                except Exception:
                    logger.exception("Disk-pressure callback failed")
        elif used_pct >= self.DISK_WARN_PCT:
            state = "warning"
            logger.warning(
                "Disk space low — storage path %s is %.1f%% full "
                "(%.1f GB free of %.1f GB total).",
                path, used_pct, free_gb, total_gb,
            )
        else:
            state = "ok"

        self._disk_state = {
            "path": str(path),
            "total_gb": round(total_gb, 2),
            "used_gb":  round(used_gb,  2),
            "free_gb":  round(free_gb,  2),
            "used_pct": round(used_pct, 1),
            "state":    state,
            # Non-null when the figures above describe a dynamically expanding
            # virtual disk (Docker Desktop's WSL2 vhdx) rather than real host
            # space. Carried here as well as on /storage because this is the
            # payload the SPA's meters read: without it the header gauge, the
            # Health tab and the Storage tab all render a reassuring "2% used,
            # 935 GB free" while the host drive underneath is filling up, and
            # used_pct — which is what the thresholds above key on — cannot
            # reach DISK_WARN_PCT before the host runs out. See
            # storage/capacity.detect_virtual_backing.
            "virtual_backing": capacity.detect_virtual_backing(),
        }

    def _reconcile_all(self) -> None:
        """Run FS↔DB reconciliation for every worker. Logs per-camera result.

        Lookback window is each camera's retention period (capped at a
        sensible default if missing). This means a camera that's been
        offline for hours still has its on-disk segments rediscovered on
        the next reconciliation pass — the previous fixed 1-hour bound
        silently dropped them.
        """
        retention_by_name = {
            c["name"]: c.get("retention_days", 30)
            for c in self._config.get("cameras", [])
        }
        total_added = 0
        for name, worker in list(self._workers.items()):
            retention_days = retention_by_name.get(name, 30)
            lookback = int(retention_days) * 86400
            try:
                added = worker.reconcile(lookback_seconds=lookback)
            except Exception:
                logger.exception(
                    "Reconciliation failed for camera '%s'", name
                )
                continue
            if added:
                logger.warning(
                    "Reconciliation: camera '%s' had %d unindexed segment(s) "
                    "on disk — indexed now",
                    name, added,
                )
                total_added += added
        if total_added == 0:
            logger.debug("Reconciliation: all cameras in sync")
