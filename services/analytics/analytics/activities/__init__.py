"""CPU activities — the registry of what the CPU engine can run.

Every activity module declares an `ActivityDefinition` and one class,
registered here under the definition's key. That registry is the source of
truth for the VMS: GET /activities publishes its definitions, camera-mgmt
copies them into the Activity Type catalog, and AI Config only offers the ones
whose status is "available". A definition "on hold" is known but never run.

To add an activity: write its module (definition + `evaluate`), register the
class below, add its tests. Nothing else in the pipeline changes.
"""
from __future__ import annotations

from .base import (AVAILABLE, FULL_FRAME, HOLD, ZONE_OPTIONAL, ZONE_REQUIRED, ZONE_TRIPWIRE,
                   Activity, ActivityDefinition, ActivityEvent, ActivitySpec, FrameContext,
                   Param, Schedule, TrackedObject, Zone, resolve_zones)
from .car_detection import CarDetectionActivity
from .entry_exit_WLE_logs import EntryExitWLELogsActivity
from .idle_worker import IdleWorkerActivity
from .no_person_area import NoPersonAreaActivity
from .people_gathering import PeopleGatheringActivity
from .restricted_zone_entry import RestrictedZoneEntryActivity
from .stray_parking import StrayParkingActivity

ACTIVITY_CLASSES: dict[str, type[Activity]] = {
    cls.definition.key: cls for cls in (
        RestrictedZoneEntryActivity,
        PeopleGatheringActivity,
        StrayParkingActivity,
        EntryExitWLELogsActivity,
        NoPersonAreaActivity,
        CarDetectionActivity,
        IdleWorkerActivity,
    )
}

__all__ = [
    "ACTIVITY_CLASSES", "AVAILABLE", "FULL_FRAME", "HOLD", "ZONE_OPTIONAL", "ZONE_REQUIRED",
    "ZONE_TRIPWIRE", "Activity", "ActivityDefinition", "ActivityEvent", "ActivitySpec",
    "FrameContext", "Param", "Schedule", "TrackedObject", "Zone", "resolve_zones",
]
