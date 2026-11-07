"""AI detection status for Administration -> System health.

WHY A SUMMARY AND NOT A PROXY. The analytics service's own API can add and
remove cameras; a pass-through would hand that to the browser. This reads its
/health and returns only what an administrator acts on: is detection running,
which model and runtime, why that one, and whether it can keep up.

THE FAILURE THIS EXISTS FOR (2026-10-26): the detector crash-looped 30 times in
30 minutes on an Apple M5, and every screen in the product looked normal — the
service reported healthy between deaths and simply indexed nothing. Detection
state belongs on the same page as the recorder's.

Mounted at /api/analytics/engine, admin only (main.py).
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import structlog
from fastapi import APIRouter

from ..services import analytics_client

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/analytics/engine", tags=["analytics-engine"])


def _model(weights: Optional[str]) -> Optional[str]:
    if not weights:
        return None
    name = weights.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".pt") else name


def summarise(h: dict[str, Any]) -> dict[str, Any]:
    """The analytics /health, reduced to what the System health card shows."""
    models = h.get("models") or {}
    choice = models.get("detector_choice") or {}
    backend = models.get("detector_backend") or {}
    selection = models.get("backend_selection") or {}
    life = (h.get("lifecycle") or {}).get("lifetime") or {}
    pipeline = h.get("pipeline") or {}
    warnings = []
    if choice.get("over_capacity"):
        warnings.append("The detector cannot keep up with this camera load at busy times; "
                        "some frames will not be analysed.")
    failures = selection.get("failures") or []
    if failures and backend.get("runtime") != "openvino":
        warnings.append("The faster OpenVINO runtime cannot run on this machine, so detection "
                        "uses PyTorch: " + failures[-1].split(": ", 1)[-1])
    if h.get("last_error"):
        warnings.append(f"Last error: {h['last_error']}")
    return {
        "reachable": True,
        "state": h.get("state"),
        "cameras": h.get("total_cameras"),
        "model": _model(choice.get("weights")),
        "mode": choice.get("mode"),
        "reason": choice.get("reason"),
        "runtime": backend.get("runtime"),
        "device": backend.get("device"),
        "capacity_fps": choice.get("capacity_fps"),
        "demand_fps": choice.get("demand_fps"),
        "over_capacity": bool(choice.get("over_capacity")),
        "frames_processed": life.get("frames_processed"),
        "frames_skipped_no_motion": life.get("frames_skipped_no_motion"),
        "frames_evicted": (life.get("frames_evicted") or 0) + (pipeline.get("frames_evicted") or 0),
        "detections_seen": life.get("detections_seen"),
        "warnings": warnings,
    }


@router.get("")
async def engine_status() -> dict[str, Any]:
    if not analytics_client.is_configured():
        return {"reachable": False, "configured": False,
                "warnings": ["AI detection is not deployed on this installation."]}
    try:
        resp = await analytics_client._get_client().get(f"{analytics_client._base()}/health")
        resp.raise_for_status()
        return {"configured": True, **summarise(resp.json())}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("analytics.engine_status.unreachable", error=str(exc))
        return {"reachable": False, "configured": True,
                "warnings": ["The AI detection service is not responding. If this persists, "
                             "check that the analytics container is running."]}
