"""no_person_area — ON HOLD. Registered, not implemented, never run.

No CPU logic or settings exist yet; the definition declares none, so AI Config
offers nothing the runtime would ignore.
"""
from __future__ import annotations

from .base import HOLD, ZONE_REQUIRED, Activity, ActivityDefinition, ActivityEvent, FrameContext

DEFINITION = ActivityDefinition(
    key="no_person_area",
    label="No-person area",
    description="On hold — not yet implemented on the CPU analytics engine.",
    color="#ff8a65",
    status=HOLD,
    zone_rule=ZONE_REQUIRED,
)


class NoPersonAreaActivity(Activity):
    definition = DEFINITION

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        raise NotImplementedError("no_person_area has no CPU implementation yet")
