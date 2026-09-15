"""HTTP surface for the frame broker.

Deliberately small: register a camera, unregister it, report health. There is
no frame endpoint and never should be — frames leave through shared memory,
and an HTTP path for them would be the 62 MB/s mistake this service exists to
avoid.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query

from broker.engine import FrameEngine


def create_app(engine: FrameEngine) -> FastAPI:
    app = FastAPI(title="Variphi Frame Broker", version="1.0")

    @app.get("/health")
    def health() -> dict:
        return engine.health()

    @app.get("/cameras")
    def list_cameras() -> dict:
        cams = engine.snapshot()
        return {"total": len(cams), "cameras": cams}

    @app.get("/cameras/{camera}")
    def get_camera(camera: str) -> dict:
        w = engine.get(camera)
        if w is None:
            raise HTTPException(404, f"Camera '{camera}' not found")
        return w.snapshot()

    @app.post("/cameras/{camera}")
    def add_camera(camera: str,
                   rtsp_url: str = Query(..., description="Relay URL to decode")) -> dict:
        # upsert, not add: the registry's reconcile loop re-asserts the whole
        # set every interval and must not have to know what already exists.
        w = engine.upsert_camera(camera, rtsp_url)
        return {"status": "ok", "camera": camera, "state": w.state}

    @app.delete("/cameras/{camera}")
    def remove_camera(camera: str) -> dict:
        if not engine.remove_camera(camera):
            raise HTTPException(404, f"Camera '{camera}' not found")
        return {"status": "ok", "camera": camera}

    return app
