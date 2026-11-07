"""HTTP surface. Small on purpose: cameras in, health out.

NO QUERY API HERE. This service produces observations; searching them is Smart
Search's job and lives behind its own endpoints. Anything that reads the index
belongs there, not here.
"""
from __future__ import annotations

from typing import Any, Optional

import os
import uuid
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel


class CameraBody(BaseModel):
    """Optional JSON body of POST /cameras/{camera}.

    `analytics_config` is the camera's stored AI Config (regions + activities)
    exactly as camera-mgmt holds it; the CPU Activity Engine runs from it.
    Absent = leave this camera's activity configuration unchanged.
    """
    analytics_config: Optional[dict[str, Any]] = None


class HeatmapBody(BaseModel):
    """Body of POST /cameras/{camera}/heatmap — the operator's runtime switch.

    This is NOT the AI Config schedule. The schedule says when the heatmap is
    ALLOWED to run and lives in the camera's stored config; this says whether
    the operator wants it running now, and never rewrites the schedule.

    `mode` applies when switching ON and a frozen session is waiting:
    "resume" continues it, "new" closes it and starts a fresh accumulation.
    """
    enabled: bool
    mode: str = "resume"
    reason: Optional[str] = None


def create_app(engine) -> FastAPI:
    app = FastAPI(title="Variphi analytics", version="1")

    @app.get("/health")
    def health() -> dict:
        return engine.snapshot()

    @app.get("/activities")
    def list_activities() -> dict:
        """The activity registry: every activity this build knows, with its
        definition — status, zone rule, settings. camera-mgmt copies this into
        its Activity Type catalog; only "available" ones can be configured."""
        return {"activities": engine.activity_definitions()}

    @app.get("/cameras")
    def list_cameras() -> dict:
        cams = engine.list_cameras()
        return {"total": len(cams), "cameras": cams}

    @app.get("/cameras/{camera}")
    def get_camera(camera: str) -> dict:
        c = engine.get_camera(camera)
        if c is None:
            raise HTTPException(status_code=404, detail="unknown camera")
        return c

    @app.get("/cameras/{camera}/presence")
    def get_presence(camera: str) -> dict:
        """Who is in each activity zone on the latest analysed frame. `ts` is
        that frame's capture time, so a caller can tell a fresh 0 from a
        stalled camera."""
        p = engine.presence(camera)
        if p is None:
            raise HTTPException(status_code=404, detail="no analysed frame for this camera")
        return {"camera": camera, **p}

    @app.post("/cameras/{camera}")
    def add_camera(camera: str, rtsp_url: str = Query(...),
                   domains: str = Query("person,vehicles"),
                   body: Optional[CameraBody] = None) -> dict:
        # An explicitly EMPTY `domains` is meaningful: a camera analysed only
        # for its activities contributes nothing to Smart Search.
        wanted = tuple(d.strip() for d in domains.split(",") if d.strip())
        return engine.add_camera(camera, rtsp_url, wanted,
                                 analytics_config=body.analytics_config if body else None)

    @app.get("/cameras/{camera}/heatmap")
    def heatmap_status(camera: str) -> dict:
        """This camera's heat session, or nothing if it has never accumulated."""
        return engine.heatmap.status(camera) or {"camera": camera, "enabled": True,
                                                 "in_window": False, "session": None}

    @app.get("/cameras/{camera}/heatmap/partial")
    def heatmap_partial(camera: str) -> Response:
        """The 5-minute heat block still being filled, with the newest camera
        frame, as one .npz (heat, background, meta). The VMS adds it to the
        stored blocks so a range that ends now is up to the minute."""
        data = engine.heatmap.partial(camera)
        if data is None:
            raise HTTPException(status_code=404, detail="no heat block in progress")
        return Response(content=data, media_type="application/octet-stream")

    @app.post("/cameras/{camera}/heatmap")
    def set_heatmap(camera: str, body: HeatmapBody) -> dict:
        """Turn this camera's heatmap on or off at runtime.

        Idempotent, and safe to re-assert: camera-mgmt pushes the stored state
        here on every reconcile because this service holds it in memory only.
        """
        if body.mode not in ("resume", "new"):
            raise HTTPException(status_code=422, detail="mode must be 'resume' or 'new'")
        return engine.heatmap.set_enabled(camera, body.enabled, mode=body.mode,
                                          reason=body.reason)

    # ---- user-uploaded detection models (Data Collection) -------------------
    from analytics.user_models import ModelError, UserModelRegistry

    models_root = Path(getattr(getattr(engine, "_cfg", None), "models_dir", "/models")) / "user"
    registry = UserModelRegistry(models_root)
    max_upload = int(os.environ.get("ANALYTICS_MODEL_UPLOAD_MAX_MB", "512")) * 1024 * 1024

    @app.get("/models")
    def list_models() -> dict:
        return {"models": registry.list()}

    @app.post("/models")
    async def upload_model(file: UploadFile = File(...),
                           name: Optional[str] = Form(None)) -> dict:
        tmp = models_root / f".upload-{uuid.uuid4().hex}"
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_upload:
                        raise HTTPException(status_code=413, detail="model file too large")
                    out.write(chunk)
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(registry.add, file.filename or "", tmp, name)
        except ModelError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        finally:
            tmp.unlink(missing_ok=True)

    @app.delete("/models/{model_id}")
    def delete_model(model_id: str) -> dict:
        try:
            registry.delete(model_id)
        except ModelError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"removed": model_id}

    @app.post("/infer")
    async def infer(request: Request, model_id: str = Query(...),
                    classes: str = Query(""), conf: float = Query(0.35, ge=0.01, le=0.99)) -> dict:
        """Body = raw JPEG. Returns detections of the requested classes only."""
        data = await request.body()
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(status_code=422, detail="body is not a decodable image")
        wanted = [c.strip() for c in classes.split(",") if c.strip()]
        from starlette.concurrency import run_in_threadpool
        try:
            dets = await run_in_threadpool(registry.infer, model_id, frame, wanted, conf)
        except ModelError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"detections": dets}

    @app.delete("/cameras/{camera}")
    def remove_camera(camera: str) -> dict:
        if not engine.remove_camera(camera):
            raise HTTPException(status_code=404, detail="unknown camera")
        return {"removed": camera}

    return app
