"""restricted_zone_entry — a person entering a restricted area. IMPLEMENTED on CPU.

Ported from the DeepStream-era `restricted_zone_entry.py` onto the CPU engine's
interfaces (YOLO person detections in normalised boxes, the VMS zone format,
frame timestamps, the CPU tracker's ids). The DeepStream dependencies — `user_data`,
Shapely, `video_clip_recorder`, `create_result_events`, Kafka — are not carried
over; an event goes to the VMS through ActivityEvent.

RULE, per zone, per tracked person:

    outside → box CENTRE enters the zone     → ONE event
    stays inside, however long               → nothing more
    seen outside the zone                    → armed again
    enters again                             → a new event, unless it is within
                                               `cooldown_s` of that person's last
                                               event in this zone

WHAT A STAY IS. A stay belongs to one tracker id in one zone. It ends when the
person is SEEN outside the zone, or when the tracker retires the id (not seen
for its max age, 3 s by default — e.g. they walked out of view). A frame in
which the detector merely misses them does not end it. Every person has their
own stay: someone walking in while another person is already inside raises
their own event.

WHY `cooldown_s` STAYS. Stays replace the earlier zone-wide "one event every
cooldown while anyone is inside", so the cooldown no longer produces events.
It now guards re-entry: a person standing on the zone's edge, whose box centre
wavers across it, would otherwise be outside and inside again every few frames
and raise an event each time. Re-entering within `cooldown_s` of their last
event in this zone starts a new stay WITHOUT an event; after it, re-entering
raises one.

LIMITS. The rule is only as good as the tracker's identity: a person the
tracker loses and recreates (occluded longer than its max age, or an id switch)
is a new person and raises a new event. A detection without a tracker id cannot
have a stay and raises nothing.

HISTORY. The supplied code alerted once per zone and never again (`alert_sent`
was never cleared); the first CPU port raised one event per zone every cooldown.
Both are replaced by the stay rule above.

ADMINISTRATOR SETTINGS: `cooldown_s` (default 60 s, as supplied).
DEVELOPER CONSTANTS: the class checked ("person"); the box centre as the point
(the supplied code's choice). The supplied fixed event confidence (0.9) is
replaced by the real detection confidence.
RUNTIME STATE: per (zone, track) whether the person is inside, and the time of
their last event there.
ZONES: area zones; no zone configured → the whole frame.
"""
from __future__ import annotations

from .base import (AVAILABLE, NUMBER, ZONE_OPTIONAL, Activity, ActivityDefinition,
                   ActivityEvent, FrameContext, Param)

#: The class the supplied implementation checks for.
PERSON_LABEL = "person"

DEFINITION = ActivityDefinition(
    key="restricted_zone_entry",
    label="Restricted zone entry",
    description="Raises one event when a person enters a restricted zone. The person must "
                "leave the zone before they can raise another.",
    color="#4fd1c5",
    status=AVAILABLE,
    zone_rule=ZONE_OPTIONAL,
    domains=frozenset({"person"}),
    params=(
        Param("cooldown_s", "Cooldown", NUMBER, 60, unit="s", min=1, max=86400,
              help="A person who stays inside raises one event. After a person's event, "
                   "leaving and re-entering the same zone within this time raises nothing."),
    ),
    #: 2: one event per stay, instead of one per cooldown while inside.
    version=2,
)


class RestrictedZoneEntryActivity(Activity):
    definition = DEFINITION

    def __init__(self, camera, spec) -> None:
        super().__init__(camera, spec)
        self.cooldown_s = float(spec.params["cooldown_s"])
        #: (zone key, track id) pairs with a stay in progress.
        self._inside: set[tuple[str, str]] = set()
        #: (zone key, track id) → epoch seconds of that person's last event there.
        self._last_event: dict[tuple[str, str], float] = {}

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        events: list[ActivityEvent] = []
        people = [o for o in ctx.objects if o.label == PERSON_LABEL]
        if not people:
            return events
        for zone in self.zones:
            inside = [o for o in people if zone.contains(*o.center)]
            inside_ids = {o.track_id for o in inside}
            # Seen outside this zone: the stay is over, the person is armed again.
            for o in people:
                if o.track_id is not None and o.track_id not in inside_ids:
                    self._inside.discard((zone.key, o.track_id))
            for who in inside:
                if who.track_id is None:
                    continue
                stay = (zone.key, who.track_id)
                if stay in self._inside:
                    continue
                self._inside.add(stay)
                last = self._last_event.get(stay)
                if last is not None and ctx.ts - last < self.cooldown_s:
                    continue
                self._last_event[stay] = ctx.ts
                events.append(ActivityEvent(
                    camera=ctx.camera, activity=self.key, started_at=ctx.ts, zone=zone,
                    confidence=who.confidence, track_id=who.track_id, object_class=who.label,
                    attributes={
                        "zone_name": zone.name,
                        "bbox": [round(v, 4) for v in who.bbox],
                        "people_in_zone": len(inside),
                        "cooldown_s": self.cooldown_s,
                    },
                ))
        return events

    def forget_tracks(self, track_ids: frozenset) -> None:
        self._inside = {k for k in self._inside if k[1] not in track_ids}
        for k in [k for k in self._last_event if k[1] in track_ids]:
            del self._last_event[k]

    def reset(self) -> None:
        self._inside.clear()
        self._last_event.clear()
