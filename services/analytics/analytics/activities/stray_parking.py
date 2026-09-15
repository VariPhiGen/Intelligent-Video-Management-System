"""stray_parking — a vehicle staying inside a no-parking zone. IMPLEMENTED on CPU.

Ported from the DeepStream-era `stray_parking.py` onto the CPU engine: YOLO
vehicle detections, the CPU tracker's ids, the VMS zone format and frame
timestamps. The DeepStream dependencies (`user_data`, Shapely,
`video_clip_recorder`, Kafka) are not carried over.

RULE, per tracked vehicle, per zone:

    a tracked vehicle of a configured class, inside the zone
        → timing starts at the first frame it is seen inside
        → once it has stayed inside for `required_duration_s`: one event,
          and that track is never reported again (supplied: violation list)

TIME, NOT FRAMES. The supplied code counted frames (`required_frames`, 2400).
That counter is replaced by elapsed timestamps against `required_duration_s`.
`required_frames` is never read and never reinterpreted as seconds.

WHAT "STAYED INSIDE" MEANS HERE. The timer resets when the vehicle is seen
OUTSIDE the zone, and when its track is retired by the tracker. A frame in
which YOLO simply misses the vehicle does not reset it — the tracker keeps the
id through short gaps. (The supplied frame counter never reset on leaving the
zone; the time-based "condition remains satisfied" is the agreed meaning.) The
activity path is not motion gated, so a parked vehicle keeps being detected at
5 FPS and keeps its track.

ADMINISTRATOR SETTINGS: `vehicle_classes`, `required_duration_s`.
CLASS VOCABULARY: the CPU detector's — car, motorcycle, bus, truck. The
DeepStream name `bike` is accepted and mapped to `motorcycle`;
`other_moving_machinary` has no CPU equivalent and is ignored (reported).
DEVELOPER CONSTANTS: the vehicle's bottom centre as its position in the zone
(the supplied helper `is_object_in_zone` is not available to read). The
supplied fixed event confidence (0.6) is replaced by the detection confidence.
NOT EXPOSED: `min_overlap` — not read by the supplied code; its meaning lives in
a helper that is not available (see the report).
RUNTIME STATE: per (zone, track) the time the vehicle entered; reported tracks.
ZONES: a zone is REQUIRED. "Parking anywhere in view" is not assumed.
"""
from __future__ import annotations

from .base import (AVAILABLE, LIST, NUMBER, ZONE_REQUIRED, Activity, ActivityDefinition,
                   ActivityEvent, FrameContext, Param, iso_utc)

VEHICLE_DOMAIN = "vehicles"
#: What the CPU detector emits for vehicles (COCO).
CPU_VEHICLE_CLASSES = ("car", "motorcycle", "bus", "truck")

DEFINITION = ActivityDefinition(
    key="stray_parking",
    label="Stray parking",
    description="Raises an event when a vehicle stays inside a no-parking zone for the "
                "required parking duration.",
    color="#ffb020",
    status=AVAILABLE,
    zone_rule=ZONE_REQUIRED,
    domains=frozenset({VEHICLE_DOMAIN}),
    params=(
        Param("vehicle_classes", "Vehicle types", LIST, ("car", "truck", "bus", "motorcycle"),
              options=CPU_VEHICLE_CLASSES, min_items=1,
              aliases={"bike": "motorcycle"},
              help="Which kinds of vehicle count."),
        Param("required_duration_s", "Required parking duration", NUMBER, 600, unit="s",
              min=1, max=86400,
              help="How long a vehicle must stay inside the zone before an event is raised."),
    ),
)


class StrayParkingActivity(Activity):
    definition = DEFINITION

    def __init__(self, camera, spec) -> None:
        super().__init__(camera, spec)
        self.vehicle_classes = frozenset(spec.params["vehicle_classes"])
        self.required_duration_s = float(spec.params["required_duration_s"])
        #: (zone key, track id) → epoch seconds it was first seen inside, this stay.
        self._entered: dict[tuple[str, str], float] = {}
        #: Supplied `violation_id_data`: tracks already reported.
        self._reported: set[str] = set()

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        events: list[ActivityEvent] = []
        for obj in ctx.objects:
            if (obj.domain != VEHICLE_DOMAIN or obj.label not in self.vehicle_classes
                    or obj.track_id is None or obj.track_id in self._reported):
                continue
            x, y = obj.anchor
            for zone in self.zones:
                key = (zone.key, obj.track_id)
                if not zone.contains(x, y):
                    self._entered.pop(key, None)
                    continue
                entered = self._entered.setdefault(key, ctx.ts)
                if ctx.ts - entered < self.required_duration_s:
                    continue
                self._reported.add(obj.track_id)
                for stale in [k for k in self._entered if k[1] == obj.track_id]:
                    del self._entered[stale]
                events.append(ActivityEvent(
                    camera=ctx.camera, activity=self.key, started_at=ctx.ts, zone=zone,
                    confidence=obj.confidence, track_id=obj.track_id, object_class=obj.label,
                    attributes={
                        "zone_name": zone.name,
                        "bbox": [round(v, 4) for v in obj.bbox],
                        "parked_since": iso_utc(entered),
                        "parked_s": round(ctx.ts - entered, 1),
                    },
                ))
                break
        return events

    def forget_tracks(self, track_ids: frozenset) -> None:
        for key in [k for k in self._entered if k[1] in track_ids]:
            del self._entered[key]
        self._reported.difference_update(track_ids)

    def reset(self) -> None:
        self._entered.clear()
        self._reported.clear()
