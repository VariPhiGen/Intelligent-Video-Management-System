"""Heatmap_overlay — where people spent their time, accumulated over a session. IMPLEMENTED on CPU.

Ported from the DeepStream-era `Heatmap_overlay` probe onto the CPU engine. The
rule is the supplied one, unchanged:

  * every DETECTION of a class in `classes_for_heatmap`, every frame, drops
    `intensity` of heat in a disc of `radius` pixels at its BOTTOM CENTRE;
  * nothing decays and nothing de-duplicates, so a person who stands still
    keeps adding heat — dwell is the signal;
  * the picture is the accumulated heat, blurred, clipped at 255 (NOT
    normalised) and colour-mapped under the camera frame.

DETECTIONS, NOT TRACKS. The supplied code accumulates from the frame's
detection boxes and never consults its tracker, so two boxes on one person add
twice and a long dwell reads as a hot spot. That is kept deliberately for v1:
the CPU engine's tracker ids are right here in `ctx.objects` and using them
would be a different, quieter heatmap. If that is wanted later it is a filter
on the loop below, not a redesign.

NO ZONES IN V1. The supplied code loads this activity's zones and never applies
them; heat covers the whole frame. `zone_rule` is OPTIONAL so a camera needs no
region drawn, and any region assigned is accepted but not used — matching the
reference rather than quietly changing what the picture means.

NO EVENTS. This activity produces a picture, not a row in the Events list, so
`evaluate` always returns none. The picture leaves through the heatmap runtime
(analytics/heatmap_runtime.py) and is stored by camera-mgmt.

THE SCHEDULE IS READ HERE, NOT ABOVE. Every other activity lets
`Activity.process` drop frames outside its window. This one overrides `process`
because it must SEE the window close: that edge is what finalises the session
and makes the last picture, exactly as the supplied code's
`heatmap_window_active_previously` did.

ADMINISTRATOR SETTINGS: `classes_for_heatmap`, `intensity`, `radius` — the
three the supplied implementation reads — plus the two STORAGE CAPS,
`retention_days` and `storage_mb`. The caps are not read here: analytics only
produces heat. They are stored with the camera's config and enforced by the
VMS retention sweep (camera-mgmt services/heatmap_chunks.py), which deletes the
oldest 5-minute heat blocks from MinIO past either cap. Decay, blur kernel and opacity are NOT
settings because the supplied implementation has no decay and hard-codes the
other two; rendering constants live in analytics/heatmap.py.
RUNTIME STATE: none here. The session and its matrix belong to the runtime, so
an operator's ON/OFF and a reconfigure cannot lose them.
ZONES: none required; the whole frame.
"""
from __future__ import annotations

import logging

from ..heatmap_runtime import runtime
from .base import (AVAILABLE, LIST, NUMBER, ZONE_OPTIONAL, Activity, ActivityDefinition,
                   FrameContext, Param)

log = logging.getLogger("analytics.activities")

#: The detector's domains, spelled here so an activity never imports it.
PERSON_DOMAIN, VEHICLE_DOMAIN = "person", "vehicles"
#: What the CPU detector can label, and therefore what may contribute heat.
CPU_CLASSES = ("person", "car", "motorcycle", "bus", "truck")
#: The supplied default.
DEFAULT_CLASSES = ("person",)
INTENSITY = 1.0
RADIUS_PX = 40
#: Storage caps, enforced by the VMS. A block is ~1.5 KB compressed, so one
#: camera uses well under 1 MB a day: the defaults keep a month inside 500 MB.
RETENTION_DAYS = 30
STORAGE_MB = 500

DEFINITION = ActivityDefinition(
    key="Heatmap_overlay",
    label="Heatmap overlay",
    description="Builds a heat picture of where the chosen objects spent their time, "
                "accumulated for as long as the heatmap runs on this camera.",
    color="#ef5350",
    status=AVAILABLE,
    zone_rule=ZONE_OPTIONAL,
    domains=frozenset({PERSON_DOMAIN, VEHICLE_DOMAIN}),
    params=(
        Param("classes_for_heatmap", "Classes for heatmap", LIST, DEFAULT_CLASSES,
              options=CPU_CLASSES, min_items=1,
              aliases={"bike": "motorcycle"},
              help="Which detected objects leave heat behind."),
        Param("intensity", "Intensity", NUMBER, INTENSITY, min=0.01, max=255.0,
              help="Heat added per object per frame. Higher values reach full colour sooner."),
        Param("radius", "Radius", NUMBER, RADIUS_PX, unit="px", integer=True, min=1, max=500,
              help="How wide the heat spreads around each object's feet, in pixels of the "
                   "camera's own frame."),
        Param("retention_days", "Keep heatmap for", NUMBER, RETENTION_DAYS, unit="days",
              integer=True, min=1, max=3650,
              help="Heat older than this is deleted from storage, oldest first."),
        Param("storage_mb", "Storage cap", NUMBER, STORAGE_MB, unit="MB", integer=True,
              min=10, max=1_000_000,
              help="When this camera's stored heat goes over this size, the oldest is "
                   "deleted until it fits, even if it is newer than the days above."),
    ),
)


class HeatmapOverlayActivity(Activity):
    definition = DEFINITION

    def __init__(self, camera, spec) -> None:
        super().__init__(camera, spec)
        self.classes = frozenset(str(c).strip().lower() for c in spec.params["classes_for_heatmap"])
        self.intensity = float(spec.params["intensity"])
        self.radius_px = float(spec.params["radius"])
        self._warned = False

    def process(self, ctx: FrameContext) -> list:
        """Every frame, in or out of the window — the close is what finalises."""
        heat = runtime()
        if heat is None:
            if not self._warned:
                log.warning("heatmap configured on %s but the runtime is not available", self.camera)
                self._warned = True
            return []
        in_window = self.spec.schedule.is_active(ctx.ts)
        heat.observe(
            self.camera, ctx.ts,
            self._points(ctx) if in_window else (),
            ctx.frame,
            width=ctx.width, height=ctx.height,
            radius_px=self.radius_px, intensity=self.intensity,
            in_window=in_window,
            params={"classes_for_heatmap": sorted(self.classes),
                    "intensity": self.intensity, "radius": self.radius_px},
        )
        return []

    def _points(self, ctx: FrameContext) -> list[tuple[float, float]]:
        """Bottom centre, in frame pixels, of every detection that qualifies."""
        return [obj.anchor_px(ctx.width, ctx.height) for obj in ctx.objects
                if obj.label.strip().lower() in self.classes]

    def evaluate(self, ctx: FrameContext) -> list:
        """Unused: `process` is overridden, and a heatmap raises no events."""
        return []
