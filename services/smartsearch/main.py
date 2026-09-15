"""Smart Search index service entry point.

Usage:
    python main.py --config config.yaml [--host 127.0.0.1] [--port 8013]

Starts with ZERO cameras: the camera-mgmt registry pushes them at runtime
(POST /cameras/{slug}?rtsp_url=...) and re-asserts the set via its reconcile
loop, so a restart here self-heals within one sync interval — identical to the
NVR and motion services. There is deliberately no cameras file and no stored
camera credentials: this service only ever sees relay URLs.

MODELS ARE NO LONGER LOADED HERE. They used to be loaded before the port opened
and held for the life of the process, which meant a deployment with Smart Search
switched off on every camera still paid 2.9 GB resident and a busy core for work
nobody had asked for. They are now owned by index/models.ModelPool: loaded when
the first camera is registered, released once the last one has been gone for the
idle timeout, and reloaded on demand by a search. Read that module before
changing anything here.

The invariant that made eager loading right in the first place is kept, just
enforced differently. The original reason was that lazy loading would let
/health go green while the service quietly indexed nothing for ~20 s — a health
check that lies is worse than one that is slow. So the state is now explicit:
`capabilities.ingest` answers "will registered cameras be indexed", while
`lifecycle.ingest.state` answers "is that happening right now", and no reading
of either can be mistaken for the other.

One thing genuinely moved later: the encoder's output width is checked when it
first loads rather than at boot, because there is no encoder at boot. A model of
the wrong width is a permanent failure — vectors from another model are not
comparable with what is stored — so it latches UNAVAILABLE and says so in
/health, rather than being retried into a wrong answer.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import uvicorn

from api.server import create_app
from index.config import AppConfig
from index.engine import IndexEngine
from index.models import ModelPool
from index.queries import QueryService
from index.retention import RetentionThread
from index.store import Store
from index.writer import CropWriter

log = logging.getLogger("smartsearch.main")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="VMS Smart Search index service")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default=None, help="Override api.host / SMARTSEARCH_BIND_HOST")
    parser.add_argument("--port", type=int, default=None, help="Override api.port / SMARTSEARCH_PORT")
    args = parser.parse_args(argv)

    config = AppConfig.from_yaml(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Bring the index schema to head BEFORE opening the store — migrate && serve,
    # the same posture as camera-mgmt. This ran by hand while the service was
    # opt-in; now that the stack starts it by default nobody is there to run it,
    # and serving against a missing table means every ingest and query fails
    # while /health keeps reporting ok. A failed migration must stop the
    # container instead: a schema we cannot reason about is worse than a restart
    # loop that names the problem.
    rc = subprocess.call(
        [sys.executable, str(Path(__file__).resolve().parent / "migrations" / "migrate.py")],
        env={**os.environ, "SEARCHDB_URL": config.store.dsn},
    )
    if rc != 0:
        log.error("index database migration failed (exit %d) — refusing to serve", rc)
        raise SystemExit(rc)

    store = Store(config.store.dsn, config.store.retention_days)
    db = store.health()
    if db.get("reachable"):
        log.info("index database ok (schema %s, %s persons, %s vehicles)",
                 db.get("schema_version"), db["rows"]["persons"], db["rows"]["vehicles"])
        if not db.get("filtered_search_correct"):
            log.warning("hnsw.iterative_scan is off — scoped searches can return "
                        "zero rows and report success. Apply migration 002.")
    else:
        log.warning("index database unreachable: %s", db.get("error"))

    writer = CropWriter(config.store.crop_dir, config.store.crop_fsync)
    pool = ModelPool(config, store, writer)
    engine = IndexEngine(config, pool)
    # Started unconditionally, and NOT tied to model lifecycle: expiry has to
    # keep running while the models are hibernating, or switching Smart Search
    # off on every camera would freeze the index at its current size forever.
    retention = RetentionThread(store, config.store.retention_sweep_seconds,
                                crop_root=config.store.crop_dir,
                                orphan_every_n=config.store.retention_orphan_every_n,
                                nvr_url=config.store.nvr_url,
                                coverage_every_n=config.store.retention_coverage_every_n,
                                frames_root=str(writer.frames_root),
                                frame_retention_days=config.store.frame_retention_days)
    app = create_app(engine, config, store, QueryService(store, pool), pool,
                     retention=retention)
    pool.start()
    retention.start()
    if not config.ingest.enabled:
        log.warning("ingest disabled by configuration — registry and query only")

    def _shutdown(signum, frame):  # noqa: ARG001
        log.info("shutdown signal received; stopping capture threads")
        engine.stop_all()          # also stops the pool and releases the models
        retention.stop()
        store.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = args.host or config.api.host
    port = args.port or config.api.port
    log.info("smartsearch service listening on %s:%d "
             "(cameras arrive via registry sync; models load with the first one)",
             host, port)
    uvicorn.run(app, host=host, port=port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
