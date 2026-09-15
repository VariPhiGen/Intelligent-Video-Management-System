"""entry_exit_WLE_logs — people crossing a gateway line, logged as Entry or Exit. IMPLEMENTED on CPU.

THE TRIPWIRE IS THE GATEWAY. Its two stored ends, A and B, are only the line's
geometry; nobody walks from A to B. People cross the line, roughly
perpendicular to it, and the tripwire's `direction` says which way ACROSS it
is the way people walk when they ENTER:

    a2b    entry heads to the left of A→B as drawn on the frame
    b2a    entry heads to the right of A→B
    both   (or unset) no entry direction — this tripwire is NOT counted

The opposite of the entry direction is always EXIT. These are the same two
sides the DeepStream integration used for its Entry counter, so a tripwire that
was oriented for it keeps its meaning. A `both` tripwire says nothing about
which side is which, so no convention is invented for it: it is listed as
having no entry direction (and logged once) until an operator sets one.

THE RULE, per tripwire, per tracked person, frame by frame (whole bounding box,
never a foot point):

    box fully on one side of the line          → remember that side
    box across the line, touching the tripwire → the person is in the gateway
    box fully on the OTHER side, having touched the tripwire on the way
                                               → ONE crossing: toward the entry
                                                 side = ENTRY, else EXIT
    touches the tripwire, returns to the same side → nothing

Every completed crossing is recorded, so a person who walks in and later walks
out is one Entry and one Exit. A person seen on one side and then on the other
with no frame in between (frames dropped or skipped) crossed if the path between
the two boxes meets the tripwire; one who went across the line's extension
beside the tripwire did not cross it.

IDENTITY IS THE TRACKER'S. Sides are remembered per tracker id. When the tracker
retires an id (unseen for its max age, 3 s by default) that memory goes, so a
half-finished crossing is never counted and never counted twice. Only confirmed
tracks raise: a crossing completed while the track is still tentative is held
and raised — stamped with the moment it completed — once the tracker confirms
it, so a one-frame flicker never counts. A detection with no tracker id cannot
have sides and raises nothing.

THE EVENT carries `attributes.direction` ("entry" | "exit"), the tripwire's
region id and name, and the box at the moment of crossing. The VMS counts these
per time bucket for the Events page's Entry / Exit graph.

ADMINISTRATOR SETTINGS: none; the tripwire and its entry direction are drawn in
Zones & Analytics.
DEVELOPER CONSTANTS: the person class ("person").
RUNTIME STATE: per (tripwire, track) the last side and whether the box has been
across the line and touched the tripwire since; crossings awaiting confirmation.
ZONES: tripwires with an entry direction only (zone rule TRIPWIRE). With none the
activity does not run; it is never widened to the whole frame.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import cv2

from .base import (AVAILABLE, ZONE_TRIPWIRE, Activity, ActivityDefinition, ActivityEvent,
                   FrameContext, TrackedObject, Zone)

log = logging.getLogger("analytics.activities")

#: The CPU detector's class for a person.
PERSON_CLASSES = frozenset({"person"})

ENTRY = "entry"
EXIT = "exit"

#: The side (as `_side` numbers it) that entry movement heads to, per stored
#: tripwire direction. A direction not listed has no entry side.
ENTRY_SIDE = {"a2b": -1, "b2a": 1}

DEFINITION = ActivityDefinition(
    key="entry_exit_WLE_logs",
    label="Entry / exit",
    description="Counts people crossing a tripwire as Entry or Exit. The tripwire's arrow points "
                "the way people walk when they enter; crossing the other way is an exit.",
    color="#4fd1c5",
    status=AVAILABLE,
    zone_rule=ZONE_TRIPWIRE,
    domains=frozenset({"person"}),
    #: 2: one Entry or Exit per completed crossing, instead of one touch per track.
    version=2,
)


def box_touches_tripwire(bbox: tuple[float, float, float, float],
                         line: tuple[tuple[float, float], ...],
                         width: int, height: int) -> bool:
    """Does the tripwire segment intersect or touch the box?

    Both are normalised; the test runs in pixels with cv2.clipLine, whose
    rectangle (x, y, w, h) covers columns x..x+w-1 and rows y..y+h-1.
    """
    x1, y1, x2, y2 = bbox
    left, top = math.floor(x1 * width), math.floor(y1 * height)
    right, bottom = math.ceil(x2 * width), math.ceil(y2 * height)
    rect = (left, top, right - left + 1, bottom - top + 1)
    (ax, ay), (bx, by) = line
    inside, _, _ = cv2.clipLine(rect, (round(ax * width), round(ay * height)),
                                (round(bx * width), round(by * height)))
    return bool(inside)


def _side(bbox: tuple[float, float, float, float], line: tuple[tuple[float, float], ...],
          width: int, height: int) -> int:
    """Which side of the line through A→B the WHOLE box is on.

    -1 or +1 when all four corners are strictly on one side; 0 when the box is
    across the line (or a corner lies exactly on it). On the frame (y down), -1
    is the side to the left of A→B — where an `a2b` entry arrow points — and +1
    the side to its right.
    """
    (ax, ay), (bx, by) = line
    ax, ay, bx, by = ax * width, ay * height, bx * width, by * height
    dx, dy = bx - ax, by - ay
    x1, y1, x2, y2 = bbox
    signs = set()
    for px, py in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
        cross = dx * (py * height - ay) - dy * (px * width - ax)
        signs.add((cross > 0) - (cross < 0))
    return signs.pop() if len(signs) == 1 and 0 not in signs else 0


def _union(a: tuple[float, float, float, float], b: tuple[float, float, float, float]):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


@dataclass
class _Passage:
    """Where one tracked person is relative to one tripwire."""

    #: The last side the whole box was on; 0 until it has been fully on one.
    side: int = 0
    #: The box on that frame.
    side_box: Optional[tuple[float, float, float, float]] = None
    #: Seen across the line since leaving `side`...
    across: bool = False
    #: ...and touching the tripwire segment itself, not its extension.
    touched: bool = False


class EntryExitWLELogsActivity(Activity):
    definition = DEFINITION

    def __init__(self, camera, spec) -> None:
        super().__init__(camera, spec)
        #: Tripwires with an entry direction. One without cannot tell Entry from Exit.
        self._tripwires = tuple(z for z in spec.zones if z.direction in ENTRY_SIDE)
        #: Names of the assigned tripwires that have no entry direction.
        self.unoriented = tuple(z.name for z in spec.zones if z.direction not in ENTRY_SIDE)
        if self.unoriented:
            log.warning("entry_exit_WLE_logs on %s: tripwire(s) %s have no entry direction — "
                        "their crossings are not counted until one is set",
                        camera, list(self.unoriented))
        self._passages: dict[tuple[str, str], _Passage] = {}
        #: Crossings completed by a track the tracker has not confirmed yet.
        self._pending: dict[tuple[str, str], list[tuple[float, str, TrackedObject]]] = {}

    @property
    def zones(self) -> tuple[Zone, ...]:
        return self._tripwires

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        events: list[ActivityEvent] = []
        for obj in ctx.objects:
            if obj.track_id is None or obj.label.strip().lower() not in PERSON_CLASSES:
                continue
            for wire in self._tripwires:
                key = (wire.key, obj.track_id)
                passage = self._passages.setdefault(key, _Passage())
                crossing = self._step(passage, obj, wire, ctx)
                if crossing is not None:
                    self._pending.setdefault(key, []).append((ctx.ts, crossing, obj))
                if obj.confirmed and key in self._pending:
                    for ts, direction, seen in self._pending.pop(key):
                        events.append(self._event(ctx, wire, ts, direction, seen))
        return events

    def _step(self, p: _Passage, obj: TrackedObject, wire: Zone, ctx: FrameContext) -> Optional[str]:
        """Advance one person's passage past one tripwire; the crossing it completes, if any."""
        side = _side(obj.bbox, wire.points, ctx.width, ctx.height)
        if side == 0:
            p.across = True
            if box_touches_tripwire(obj.bbox, wire.points, ctx.width, ctx.height):
                p.touched = True
            return None
        crossing = None
        if p.side and side != p.side:
            if p.across:
                through = p.touched
            else:
                # No frame in between: did the path from one box to the other meet it?
                through = box_touches_tripwire(_union(p.side_box, obj.bbox), wire.points,
                                               ctx.width, ctx.height)
            if through:
                crossing = ENTRY if side == ENTRY_SIDE[wire.direction] else EXIT
        p.side, p.side_box, p.across, p.touched = side, obj.bbox, False, False
        return crossing

    def _event(self, ctx: FrameContext, wire: Zone, ts: float, direction: str,
               obj: TrackedObject) -> ActivityEvent:
        return ActivityEvent(
            camera=ctx.camera, activity=self.key, started_at=ts, zone=wire,
            confidence=obj.confidence, track_id=obj.track_id, object_class=obj.label,
            attributes={
                "direction": direction,
                "tripwire_id": wire.key,
                "tripwire_name": wire.name,
                "bbox": [round(v, 4) for v in obj.bbox],
            },
        )

    def forget_tracks(self, track_ids: frozenset) -> None:
        for store in (self._passages, self._pending):
            for key in [k for k in store if k[1] in track_ids]:
                del store[key]

    def reset(self) -> None:
        self._passages.clear()
        self._pending.clear()
