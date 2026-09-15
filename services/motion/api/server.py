"""server.py — headless REST API for the motion service.

No auth of its own: binds 127.0.0.1 (or the internal bridge network) and is
reached exclusively through camera-mgmt's authenticated /api/motion proxy —
the same trust posture as the NVR.

Cameras are pushed here by the registry sync (add/remove are idempotent from
the caller's perspective: 409/404 mean "already in the desired state").
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from detector.config import AppConfig
from detector.engine import MotionEngine


def create_app(engine: MotionEngine, config: AppConfig) -> FastAPI:
    app = FastAPI(
        title="VMS Motion Service",
        description="CPU-only motion detection over the MediaMTX relay.",
        version="1.0.0",
    )

    @app.get("/health")
    def health() -> dict:
        by_state: dict[str, int] = {"MONITORING": 0, "TRIGGERED": 0, "CONNECTING": 0}
        for cam in engine.snapshot():
            by_state[cam["state"]] = by_state.get(cam["state"], 0) + 1
        total = engine.camera_count
        source = engine.source_snapshot()
        return {
            "status": "ok",
            "total_cameras": total,
            "by_state": by_state,
            # One capture thread per camera in capture mode; in broker mode
            # there are none — a single subscription feeds every camera.
            "threads": total if source.get("source") == "capture" else 0,
            "available_cpu_cores": os.cpu_count() or 4,
            # WHERE THE MOTION SIGNAL COMES FROM. In broker mode this service
            # opens no RTSP sessions and decodes nothing: it consumes the
            # changed-pixel fraction the frame broker already computed. Event
            # semantics are identical either way — both paths run the same
            # confirmation window and state machine.
            "frame_source": source,
        }

    @app.get("/cameras")
    def list_cameras() -> dict:
        cameras = engine.snapshot()
        return {"total": len(cameras), "cameras": cameras}

    @app.get("/cameras/{name}")
    def get_camera(name: str) -> dict:
        cam = engine.get_camera(name)
        if cam is None:
            raise HTTPException(404, f"Camera '{name}' not found")
        with cam.lock:
            return cam.to_dict()

    @app.post("/cameras/{name}")
    def add_camera(
        name: str,
        rtsp_url: str = Query(..., description="Relay URL to analyse"),
        sensitivity: Optional[str] = Query(
            default=None, description="low / medium / high (default from config)"
        ),
    ) -> dict:
        try:
            cam = engine.add_camera(name, rtsp_url, sensitivity)
        except KeyError:
            raise HTTPException(409, f"Camera '{name}' already exists")
        return {"status": "ok", "camera": name, "sensitivity": cam.sensitivity}

    @app.delete("/cameras/{name}")
    def remove_camera(name: str) -> dict:
        if not engine.remove_camera(name):
            raise HTTPException(404, f"Camera '{name}' not found")
        return {"status": "ok", "camera": name}

    @app.post("/cameras/{name}/reset")
    def reset_camera(name: str) -> dict:
        state = engine.reset_camera(name)
        if state is None:
            raise HTTPException(404, f"Camera '{name}' not found")
        return {"status": "ok", "camera": name, "state": state}

    @app.get("/events")
    def list_events(
        since_id: int = Query(default=0, ge=0, description="Only events with id > this"),
        camera: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        events = engine.list_events(since_id=since_id, camera=camera, limit=limit)
        return {"total": len(events), "events": events}

    return app
