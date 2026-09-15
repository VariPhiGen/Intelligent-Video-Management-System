"""HTTP surface. Small on purpose: cameras in, health out.

NO QUERY API HERE. This service produces observations; searching them is Smart
Search's job and lives behind its own endpoints. Anything that reads the index
belongs there, not here.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel


class CameraBody(BaseModel):
    """Optional JSON body of POST /cameras/{camera}.

    `analytics_config` is the camera's stored AI Config (regions + activities)
    exactly as camera-mgmt holds it; the CPU Activity Engine runs from it.
    Absent = leave this camera's activity configuration unchanged.
    """
    analytics_config: Optional[dict[str, Any]] = None


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

    @app.post("/cameras/{camera}")
    def add_camera(camera: str, rtsp_url: str = Query(...),
                   domains: str = Query("person,vehicles"),
                   body: Optional[CameraBody] = None) -> dict:
        # An explicitly EMPTY `domains` is meaningful: a camera analysed only
        # for its activities contributes nothing to Smart Search.
        wanted = tuple(d.strip() for d in domains.split(",") if d.strip())
        return engine.add_camera(camera, rtsp_url, wanted,
                                 analytics_config=body.analytics_config if body else None)

    @app.delete("/cameras/{camera}")
    def remove_camera(camera: str) -> dict:
        if not engine.remove_camera(camera):
            raise HTTPException(status_code=404, detail="unknown camera")
        return {"removed": camera}

    return app
