"""car_detection — ON HOLD. Registered, not implemented, never run.

No CPU logic or settings exist yet; the definition declares none, so AI Config
offers nothing the runtime would ignore.
"""
from __future__ import annotations

from .base import HOLD, ZONE_REQUIRED, Activity, ActivityDefinition, ActivityEvent, FrameContext

DEFINITION = ActivityDefinition(
    key="car_detection",
    label="Car detection",
    description="On hold — not yet implemented on the CPU analytics engine.",
    color="#ff8a65",
    status=HOLD,
    zone_rule=ZONE_REQUIRED,
)


class CarDetectionActivity(Activity):
    definition = DEFINITION

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        raise NotImplementedError("car_detection has no CPU implementation yet")
