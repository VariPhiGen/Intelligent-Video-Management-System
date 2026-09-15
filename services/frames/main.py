"""Frame broker entry point.

Usage:
    python main.py --config config.yaml [--host 0.0.0.0] [--port 8014]

Decodes each registered camera ONCE, writes frames to shared memory, runs the
canonical motion analysis, and publishes a notice per frame. Consumers read
the frame they were told about — so the Motion service and SmartSearch see the
SAME pixels, which two independent decoders could never guarantee.

Starts with zero cameras; camera-mgmt pushes them at runtime.
"""
from __future__ import annotations

# CPU-only by contract: refuse a GPU before anything can initialise CUDA.
import os
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

import argparse
import logging
import signal
import sys

import uvicorn

from api.server import create_app
from broker.config import AppConfig
from broker.engine import FrameEngine


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Variphi frame broker")
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
    log = logging.getLogger("frames")

    engine = FrameEngine(cfg)
    app = create_app(engine)

    def _shutdown(signum, _frame):
        log.info("signal %s — stopping cameras", signum)
        engine.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    host = args.host or cfg.api.host
    port = args.port or cfg.api.port
    log.info("frame broker on %s:%d — %g fps, %d ring slots",
             host, port, cfg.capture.sample_fps, cfg.capture.ring_slots)
    uvicorn.run(app, host=host, port=port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()
