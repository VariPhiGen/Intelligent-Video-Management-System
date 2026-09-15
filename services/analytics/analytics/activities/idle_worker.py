"""idle_worker — ON HOLD. Registered, not implemented, never run.

No CPU logic or settings exist yet; the definition declares none, so AI Config
offers nothing the runtime would ignore.
"""
from __future__ import annotations

from .base import HOLD, ZONE_REQUIRED, Activity, ActivityDefinition, ActivityEvent, FrameContext

DEFINITION = ActivityDefinition(
    key="idle_worker",
    label="Idle worker",
    description="On hold — not yet implemented on the CPU analytics engine.",
    color="#4fd1c5",
    status=HOLD,
    zone_rule=ZONE_REQUIRED,
)


class IdleWorkerActivity(Activity):
    definition = DEFINITION

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        raise NotImplementedError("idle_worker has no CPU implementation yet")
