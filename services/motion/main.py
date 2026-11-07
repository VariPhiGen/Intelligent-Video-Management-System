"""Motion service entry point.

Usage:
    python main.py --config config.yaml [--host 127.0.0.1] [--port 8012]

Starts with ZERO cameras: the camera-mgmt registry pushes them at runtime
(POST /cameras/{slug}?rtsp_url=...) and re-asserts the set via its reconcile
loop, so a restart here self-heals within one sync interval — identical to
the NVR's lifecycle. There is deliberately no cameras file and no stored
camera credentials: the service only ever sees relay URLs.
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
from detector.config import AppConfig
from detector.engine import MotionEngine


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="VMS motion detection service")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default=None, help="Override api.host / MOTION_BIND_HOST")
    parser.add_argument("--port", type=int, default=None, help="Override api.port / MOTION_PORT")
    args = parser.parse_args(argv)

    config = AppConfig.from_yaml(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("motion.main")

    engine = MotionEngine(config)

    # Motion ACTIVITY for the playback timeline: runs of motion, posted to the
    # VMS as presence. Separate from the incident log, which is unchanged.
    activity = sink = None
    act = config.activity
    if act.enabled and act.url and act.internal_api_key:
        from detector.activity import MotionActivityRecorder
        from detector.sink import PresenceSink
        sink = PresenceSink(act.url, act.internal_api_key)
        sink.start()
        det = config.detection
        activity = MotionActivityRecorder(
            sink, sample_interval=det.sample_interval_seconds,
            confirm_window=det.confirm_window_seconds, confirm_ratio=det.confirm_ratio,
            hold_seconds=act.hold_seconds, segment_seconds=act.segment_seconds)
        activity.start()
        engine.attach_activity(activity)
    else:
        logging.getLogger("motion").info(
            "motion activity not reported to the VMS (no MOTION_EVENTS_URL / key, or disabled)")
    subscriber = None
    if config.source.frame_source == "broker":
        # No RTSP sessions and no decoding: the frame broker already computed
        # the changed-pixel fraction on a frame it decoded once for every
        # consumer. Event semantics are untouched — both paths call the same
        # worker.apply_feature.
        from detector.notices import NoticeSubscriber
        subscriber = NoticeSubscriber(config, engine, config.source.broker_url,
                                      config.source.broker_channel_prefix)
        engine.attach_subscriber(subscriber)
        subscriber.start()
        logging.getLogger("motion").info(
            "motion signal from frame broker at %s", config.source.broker_url)
    app = create_app(engine, config)

    def _shutdown(signum, frame):  # noqa: ARG001
        logger.info("shutdown signal received; stopping capture threads")
        engine.stop_all()
        if activity is not None:
            activity.stop()
        if sink is not None:
            sink.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = args.host or config.api.host
    port = args.port or config.api.port
    logger.info("motion service listening on %s:%d (cameras arrive via registry sync)", host, port)
    uvicorn.run(app, host=host, port=port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
