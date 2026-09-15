"""
Smart NVR — main entry point.

Starts the recording engine, retention manager, and REST API.
"""

import argparse
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml

from api.server import create_app
from recorder.engine import RecordingEngine
from storage.grooming import GroomingManager, run_grooming_loop
from storage.index import SegmentIndex
from storage import capacity
from storage.alerts import StorageAlerter
from storage.retention import RetentionManager
from storage.settings import StorageSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("smart-nvr")


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def run_retention_loop(retention: RetentionManager, stop_event: threading.Event,
                       interval: int = 3600):
    """Run retention cleanup every `interval` seconds."""
    while not stop_event.is_set():
        try:
            retention.run_once()
        except Exception:
            logger.exception("Retention pass failed")
        stop_event.wait(interval)


def main():
    parser = argparse.ArgumentParser(description="Smart NVR System")
    parser.add_argument(
        "-c", "--config", default="config/cameras.yaml",
        help="Path to camera config YAML",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="API listen host",
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="API listen port",
    )
    parser.add_argument(
        "--no-record", action="store_true",
        help="Start API only (no recording — useful for development)",
    )
    parser.add_argument(
        "--rebuild-index", action="store_true",
        help="Rebuild segment index from filesystem before starting",
    )
    parser.add_argument(
        "--groom-now", action="store_true",
        help="Run one grooming pass immediately (ignores the nightly window)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    rec_cfg = config["recording"]

    # Ensure directories exist
    storage_path = Path(rec_cfg["storage_path"])
    storage_path.mkdir(parents=True, exist_ok=True)
    clips_path = Path(rec_cfg.get("clips_path", storage_path / "clips"))
    clips_path.mkdir(parents=True, exist_ok=True)
    annotations_path = Path(rec_cfg.get("annotations_path", storage_path / "annotations"))
    annotations_path.mkdir(parents=True, exist_ok=True)

    # Initialize segment index
    db_path = storage_path / "segments.db"
    index = SegmentIndex(db_path)
    logger.info("Segment index at %s", db_path)

    if args.rebuild_index:
        from recorder.stream_worker import StreamWorker
        logger.info("Rebuilding index from filesystem...")
        count = index.rebuild_from_filesystem(
            storage_path, StreamWorker._probe_duration
        )
        logger.info("Indexed %d segments from filesystem", count)

    # Start recording engine
    engine = None
    if not args.no_record:
        engine = RecordingEngine(config, index)
        engine.start()

    # Start retention manager
    # Storage cap: NVR_MAX_STORAGE_GB env (the product-level .env knob) takes
    # priority over the max_storage_gb key in cameras.yaml. Empty/absent = uncapped.
    max_gb = rec_cfg.get("max_storage_gb")
    env_max = os.environ.get("NVR_MAX_STORAGE_GB", "").strip()
    if env_max:
        try:
            max_gb = float(env_max)
        except ValueError:
            logger.error("Ignoring invalid NVR_MAX_STORAGE_GB=%r", env_max)
    max_storage_bytes = int(max_gb * (1024 ** 3)) if max_gb else None
    # The env/YAML value only SEEDS the cap: a value set later from the UI is
    # persisted under the data dir and wins on load, surviving restarts.
    storage_settings = StorageSettings(
        storage_path / "settings.json",
        default_max_storage_bytes=max_storage_bytes,
    )
    effective_cap = storage_settings.max_storage_bytes
    if effective_cap is not None:
        logger.info("Storage cap: %.2f GB", effective_cap / (1024 ** 3))
        # The API refuses a cap the volume cannot honour, but this path bypasses
        # it: an env/YAML seed is applied verbatim, and a cap that WAS valid goes
        # stale the day the volume shrinks or shares space with something new.
        # Warn rather than clamp — silently shrinking an operator's configured
        # cap at boot is the more surprising failure, and the disk-floor evictor
        # already protects the filesystem either way.
        head = capacity.probe(storage_path, footage_bytes=index.total_size())
        if head is not None and effective_cap > head.max_cap_bytes:
            logger.warning(
                "Storage cap of %.2f GB exceeds what %s can hold (max %d GB: "
                "%.2f GB free + %.2f GB recorded, less %g%% reserve). The cap "
                "will never be reached — the disk-floor evictor will reclaim "
                "space first, ignoring it.",
                effective_cap / (1024 ** 3), head.path, head.max_cap_gb,
                head.free_bytes / (1024 ** 3), head.footage_bytes / (1024 ** 3),
                capacity.RESERVE_FREE_PCT,
            )
    else:
        logger.warning("No storage cap set (NVR_MAX_STORAGE_GB / UI) — footage "
                       "is bounded only by per-camera retention_days")

    # Uncapped or not, say so when the volume's own numbers cannot be trusted.
    # On Docker Desktop for Windows /data/nvr is a dynamically expanding vhdx
    # with a ~1 TB virtual maximum living on a much smaller host drive, so the
    # free space reported below is not space the host can actually supply. It
    # matters because every automatic protection here is a PERCENTAGE of that
    # inflated total: the WARNING/CRITICAL meters (90%/95% used) and the
    # disk-floor evictor (fires under 5% free) all stay dormant while the host
    # drive fills to completion. Measured on a stock install: 1 camera at
    # ~10 GB/day fills a 96 GB-free C: in ~9 days, and at the moment it is full
    # the meter here reads ~11% used and the alert state still reads "ok".
    # Nothing in this process can see the host drive, so warn loudly and tell
    # the operator the one thing that does work: set an explicit cap.
    head = capacity.probe(storage_path, footage_bytes=index.total_size())
    if head is not None and head.virtual_backing:
        logger.warning(
            "%s is on a %s virtual disk: the %.0f GB free reported here is the "
            "VIRTUAL maximum and is NOT backed by the host drive, which is "
            "smaller and invisible from in here. Disk %%-thresholds and the "
            "disk-floor evictor are measured against that inflated total, so "
            "they will NOT fire before the host drive fills. Set "
            "NVR_MAX_STORAGE_GB to a size the host can genuinely honour%s.",
            head.path, head.virtual_backing, head.free_bytes / (1024 ** 3),
            "" if effective_cap is not None else " — none is set",
        )
    stop_event = threading.Event()
    retention = RetentionManager(
        index, config["cameras"], str(clips_path),
        clip_ttl_minutes=rec_cfg.get("clip_ttl_minutes", 30),
        hls_cache_ttl_minutes=rec_cfg.get("hls_cache_ttl_minutes", 360),
        storage_path=str(storage_path),
        default_retention_days=rec_cfg.get("default_retention_days", 30),
        # Runtime-added cameras (registry sync) carry their own retention_days;
        # without this hook they would never be age-cleaned.
        retention_map_provider=(engine.get_retention_map if engine else None),
        # Read the size cap live so a UI edit applies on the next pass.
        max_storage_provider=storage_settings.get_max_storage_bytes,
    )
    # Storage pressure: evaluate every minute and notify on state CHANGES.
    # Its own timer rather than a hook on the engine's 30s health pass, because
    # half of what it evaluates (is the cap evicting? how much footage is
    # actually on disk?) belongs to retention, not to the engine.
    alerter = StorageAlerter(
        state_path=storage_path / "alert-state.json",
        webhook_url=os.environ.get("NVR_ALERT_WEBHOOK_URL", ""),
        interval_seconds=float(os.environ.get("NVR_ALERT_INTERVAL_SECONDS", "60")),
        site=os.environ.get("SITE_NAME", ""),
    )

    def gather_pressure() -> dict:
        """Everything evaluate() needs, read fresh each pass."""
        normal_range, cold_range = index.range_split()
        starts = [r[0] for r in (normal_range, cold_range) if r]
        return {
            "disk_state": (engine.get_status().get("__disk__") if engine else None),
            "footage_bytes": index.total_size(),
            "limit_bytes": storage_settings.max_storage_bytes,
            "pressure": retention.pressure_snapshot(),
            "oldest_epoch": (min(starts) if starts else None),
            "configured_days": retention.configured_retention_days(),
        }

    alerter.start(gather_pressure)

    # Fast trigger for emergency disk-floor eviction: the engine's 30s disk
    # check fires this the moment the volume crosses critical, so space is
    # reclaimed before ffmpeg starts failing writes (rather than waiting for the
    # hourly retention pass, which also enforces the floor as a backstop).
    if engine is not None:
        engine.set_disk_pressure_callback(retention.enforce_disk_floor)
    retention_thread = threading.Thread(
        target=run_retention_loop,
        args=(retention, stop_event),
        name="retention",
        daemon=True,
    )
    retention_thread.start()

    # Grooming (frame-rate reduction of old footage): keyframe-only remux of
    # segments past their camera's groom-after threshold, inside a nightly
    # window. Thresholds are per camera (runtime overrides via the registry
    # sync win, then cameras.yaml, then recording.groom_after_days; 0 = off),
    # so the loop always runs — it no-ops when every camera is at 0.
    groom_days = rec_cfg.get("groom_after_days", 0) or 0
    groomer = GroomingManager(
        index, groom_after_days=groom_days,
        window=rec_cfg.get("groom_window", "02:00-06:00"),
        cameras_config=config["cameras"],
        groom_map_provider=(engine.get_groom_map if engine else None),
    )
    if groom_days > 0:
        logger.info("Grooming default: keyframe-only after %dd (window %s); "
                    "per-camera overrides apply",
                    groom_days, rec_cfg.get("groom_window", "02:00-06:00"))
    else:
        logger.info("Grooming default off — runs only for cameras with their "
                    "own groom-after value")
    if args.groom_now:
        logger.info("--groom-now: running one immediate grooming pass")
        groomer.run_once(force=True)
    grooming_thread = threading.Thread(
        target=run_grooming_loop,
        args=(groomer, stop_event),
        name="grooming",
        daemon=True,
    )
    grooming_thread.start()

    # Build lifespan: uvicorn owns SIGTERM/SIGINT and calls shutdown on its
    # own clean-exit path. Installing signal.signal() here would be overwritten
    # by uvicorn.run() — engine.stop() and stop_event.set() would never fire.
    # A lifespan context manager runs in uvicorn's shutdown sequence instead.
    @asynccontextmanager
    async def lifespan(app):
        # Startup — nothing extra needed; engine/retention started above.
        yield
        # Shutdown — called by uvicorn on SIGTERM / SIGINT / KeyboardInterrupt.
        logger.info("Shutting down — stopping engine and retention...")
        if engine:
            engine.stop()
        alerter.stop()
        stop_event.set()
        logger.info("Shutdown complete.")

    # Create and start API
    app = create_app(
        index, clips_path, engine,
        lifespan=lifespan,
        annotations_path=annotations_path,
        storage_settings=storage_settings,
        storage_path=storage_path,
        alerter=alerter,
        segment_duration=rec_cfg.get("segment_duration", 60),
        groom_default_days=groom_days,
        max_clip_seconds=rec_cfg.get("max_clip_seconds", 6 * 3600),
        # None (key absent) = size it to the encoder the startup probe finds.
        max_concurrent_extractions=rec_cfg.get("max_concurrent_extractions"),
    )

    logger.info("Starting API server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
