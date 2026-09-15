"""people_gathering — people clustered together for a sustained time. IMPLEMENTED on CPU.

Ported from the DeepStream-era `people_gathering.py` onto the CPU engine: YOLO
person detections, the CPU tracker's ids, normalised boxes converted to pixels
for distances, the VMS zone format and frame timestamps. The clustering is the
supplied algorithm, unchanged; the DeepStream dependencies (`user_data`,
Shapely, scipy, `video_clip_recorder`, Kafka) are not carried over.

RULE, per zone, per frame (as supplied):

  1. every pair of person-like tracked detections whose bottom centres are
     closer than `proximity_factor` (1.35) × the wider box's width, BOTH inside
     the same zone, is linked (a pair belongs to the first zone containing both);
  2. linked pairs form clusters by union-find (`find_clusters_by_zone`);
  3. a cluster of at least `min_group_size` (2) people is ACTIVE unless 70% or
     more of its members were already reported;
  4. while any cluster is active the gathering is sustained; once it has been
     sustained for `required_duration_s` each active cluster is reported —
     unless the zone is cooling down (`cooldown_s`, 100 s, after an event), in
     which case the timer restarts — and the timer restarts after an event;
  5. with no active cluster, a sustained timer survives gaps of up to
     `last_time` seconds, then resets.

TIME, NOT FRAMES. The supplied code counted frames against `frame_accuracy`
(1800). That counter is replaced by elapsed timestamps against
`required_duration_s`; `frame_accuracy` is never read and never reinterpreted.
`last_time` was already seconds in the supplied code and keeps its key.

ADMINISTRATOR SETTINGS: `required_duration_s`, `last_time`, `min_group_size`,
`proximity_factor`, `cooldown_s`. The last three were the supplied code's fixed
2, 1.35 and 100 s; those remain their defaults.
DEVELOPER CONSTANTS: person-like classes, the 70% repeat rule — as supplied.
NOT EXPOSED: the catalog's `person_limit`; the supplied code never reads it and
its meaning is not established (see the report).
RUNTIME STATE: per zone the sustained-since time, last active time and
cooldown deadline; the set of reported tracker ids.
ZONES: area zones; no zone configured → the whole frame.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Optional

from .base import (AVAILABLE, NUMBER, ZONE_OPTIONAL, Activity, ActivityDefinition,
                   ActivityEvent, FrameContext, Param, iso_utc)

#: As supplied. The CPU detector emits only "person"; the others never occur.
ALLOWED_CLASSES = frozenset({"person", "student", "staff", "security guard"})
#: Default of `proximity_factor`: two people are "together" when their bottom
#: centres are closer than this many widths of the wider box (supplied: 1.35).
PROXIMITY_FACTOR = 1.35
#: Default of `min_group_size` (supplied: a cluster needs at least 2 people).
MIN_GROUP_SIZE = 2
#: Supplied: a cluster whose members were already reported at this share or
#: more is not a new gathering.
REPEAT_SHARE_PERCENT = 70
#: Default of `cooldown_s` (supplied: 100 s after an event, per zone).
COOLDOWN_S = 100.0

DEFINITION = ActivityDefinition(
    key="people_gathering",
    label="People gathering",
    description="Raises an event when people stay clustered together inside a zone "
                "for the required duration.",
    color="#ff8a65",
    status=AVAILABLE,
    zone_rule=ZONE_OPTIONAL,
    domains=frozenset({"person"}),
    params=(
        Param("required_duration_s", "Gathering duration", NUMBER, 60, unit="s",
              min=1, max=86400,
              help="How long a group must stay together before an event is raised."),
        Param("last_time", "Reset after no gathering", NUMBER, 30, unit="s",
              min=1, max=3600,
              help="If no group is seen for this long, the gathering timer starts over."),
        Param("min_group_size", "Minimum people", NUMBER, MIN_GROUP_SIZE, integer=True,
              min=2, max=50,
              help="The fewest people standing close together that count as a gathering."),
        Param("proximity_factor", "Proximity factor", NUMBER, PROXIMITY_FACTOR,
              unit="× box width", min=0.5, max=10.0,
              help="Two people are close when their feet are nearer than this many widths "
                   "of the wider person's box. Higher values count people further apart."),
        Param("cooldown_s", "Cooldown", NUMBER, COOLDOWN_S, unit="s", min=1, max=86400,
              help="After a gathering event, how long this zone waits before it can raise "
                   "another."),
    ),
    #: 2: minimum people, proximity factor and cooldown became settings.
    version=2,
)


@dataclass
class _ZoneState:
    since: Optional[float] = None
    last_seen: float = 0.0
    cooldown_until: float = 0.0


class PeopleGatheringActivity(Activity):
    definition = DEFINITION

    def __init__(self, camera, spec) -> None:
        super().__init__(camera, spec)
        self.required_duration_s = float(spec.params["required_duration_s"])
        self.reset_after_s = float(spec.params["last_time"])
        self.min_group_size = int(spec.params["min_group_size"])
        self.proximity_factor = float(spec.params["proximity_factor"])
        self.cooldown_s = float(spec.params["cooldown_s"])
        self._zones: dict[str, _ZoneState] = {}
        #: Supplied `violation_id_data`: tracker ids already reported.
        self._reported: set[str] = set()

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        people = [o for o in ctx.objects
                  if o.label in ALLOWED_CLASSES and o.track_id is not None]
        by_id = {o.track_id: o for o in people}

        gathered: dict[str, list[list[str]]] = {z.key: [] for z in self.zones}
        for a, b in combinations(people, 2):
            ax, ay = a.anchor_px(ctx.width, ctx.height)
            bx, by = b.anchor_px(ctx.width, ctx.height)
            threshold = max(a.width_px(ctx.width), b.width_px(ctx.width)) * self.proximity_factor
            if math.hypot(ax - bx, ay - by) < threshold:
                for zone in self.zones:
                    if zone.contains(*a.anchor) and zone.contains(*b.anchor):
                        gathered[zone.key].append([a.track_id, b.track_id])
                        break

        clusters = find_clusters_by_zone(gathered)
        events: list[ActivityEvent] = []
        for zone in self.zones:
            state = self._zones.setdefault(zone.key, _ZoneState())
            active = []
            for cluster in clusters.get(zone.key, []):
                if len(cluster) >= self.min_group_size:
                    repeat = sum(1 for t in cluster if t in self._reported)
                    if int(repeat * 100 / len(cluster)) < REPEAT_SHARE_PERCENT:
                        active.append(cluster)

            if active:
                if state.since is None:
                    state.since = ctx.ts
                state.last_seen = ctx.ts
                if ctx.ts - state.since < self.required_duration_s:
                    continue
                for cluster in active:
                    sustained_since = state.since
                    self._reported.update(cluster)
                    if ctx.ts < state.cooldown_until:
                        state.since = ctx.ts
                        state.last_seen = ctx.ts
                        continue
                    events.append(self._event(ctx, zone, cluster, by_id, sustained_since))
                    state.since = ctx.ts
                    state.last_seen = ctx.ts
                    state.cooldown_until = ctx.ts + self.cooldown_s
            elif state.since is not None and ctx.ts - state.last_seen > self.reset_after_s:
                state.since = None
                state.last_seen = ctx.ts
        return events

    def _event(self, ctx, zone, cluster, by_id, sustained_since) -> ActivityEvent:
        members = [by_id[t] for t in cluster if t in by_id]
        x1 = min(o.bbox[0] for o in members)
        y1 = min(o.bbox[1] for o in members)
        x2 = max(o.bbox[2] for o in members)
        y2 = max(o.bbox[3] for o in members)
        return ActivityEvent(
            camera=ctx.camera, activity=self.key, started_at=ctx.ts, zone=zone,
            confidence=sum(o.confidence for o in members) / len(members),
            object_class="person",
            attributes={
                "zone_name": zone.name,
                "person_count": len(cluster),
                "track_ids": sorted(cluster),
                "bbox": [round(v, 4) for v in (x1, y1, x2, y2)],
                "gathering_since": iso_utc(sustained_since),
                "sustained_s": round(ctx.ts - sustained_since, 1),
            },
        )

    def forget_tracks(self, track_ids: frozenset) -> None:
        self._reported.difference_update(track_ids)

    def reset(self) -> None:
        self._zones.clear()
        self._reported.clear()


def find_clusters_by_zone(gathered_tracker_ids: dict) -> dict:
    """Group co-located tracker-id pairs into connected clusters per zone using
    a union-find structure. The supplied implementation, unchanged."""
    parent: dict = {}

    def find(x):
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x, y):
        root_x = find(x)
        root_y = find(y)
        if root_x != root_y:
            parent[root_y] = root_x

    zone_clusters: dict = {}
    for _zone_name, pairs in gathered_tracker_ids.items():
        for tracker1, tracker2 in pairs:
            if tracker1 not in parent:
                parent[tracker1] = tracker1
            if tracker2 not in parent:
                parent[tracker2] = tracker2
            union(tracker1, tracker2)

    for tracker in parent:
        root = find(tracker)
        zone_clusters.setdefault(root, []).append(tracker)

    result: dict = {}
    for zone_name in gathered_tracker_ids:
        zone_result = []
        seen = set()
        for tracker1, tracker2 in gathered_tracker_ids[zone_name]:
            root1, root2 = find(tracker1), find(tracker2)
            if root1 not in seen:
                zone_result.append(zone_clusters[root1])
                seen.add(root1)
            if root2 not in seen:
                zone_result.append(zone_clusters[root2])
                seen.add(root2)
        result[zone_name] = zone_result
    return result
