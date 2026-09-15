"""Analytics service entry point.

    python main.py --config config.yaml

Detects objects and reads plates on frames it takes from the frame broker,
tracks them, and decides which observations deserve a record. Produces crops
and metadata; embedding, deduplication, storage and search are Smart Search's.

Starts with zero cameras; camera-mgmt pushes them at runtime.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys

import uvicorn

from analytics.config import AppConfig
from analytics.engine import AnalyticsEngine
from api.server import create_app


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Variphi analytics")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    cfg = AppConfig.from_yaml(args.config)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("analytics")

    engine = AnalyticsEngine(cfg)
    app = create_app(engine)

    def _shutdown(signum, _frame):
        log.info("signal %s - stopping", signum)
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    host = args.host or cfg.api.host
    port = args.port or cfg.api.port
    log.info("analytics on %s:%d - frames from %s, sink %s",
             host, port, cfg.source.frame_source, cfg.sink.url or "(none)")
    uvicorn.run(app, host=host, port=port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
