"""base.py — what a CPU activity is, what it is given, and the rules they share.

WHERE ACTIVITIES SIT. The activity path decodes nothing: frames arrive from the
broker at its 5 FPS, EVERY frame, with no motion gate (see
analytics/activity_pipeline.py). Its own OpenVINO YOLO finds the objects and
its own tracker names them. An activity receives that result as one
`FrameContext` per frame and decides whether something happened.

THE DEFINITION IS THE DEVELOPER'S. Every activity module declares an
`ActivityDefinition`: its key, whether it can run, which zones it takes, which
detector domains it consumes, and every setting an administrator may tune —
label, kind, default, unit, bounds, allowed values. That definition is what the
analytics service publishes (GET /activities), what camera-mgmt copies into
its Activity Type catalog, and what AI Config renders. A setting that is not in
the definition does not exist; one that is, is read by the code.

THE VALUES ARE THE CAMERA'S. An activity is built from one entry of the
camera's `analytics_config`, exactly as AI Config stores it:

    {"regions":    {"region_1": {"kind": "zone", "name": "Zone 1",
                                 "points": [[x, y], ...], "direction": null}},
     "activities": [{"type": "stray_parking", "regions": ["region_1"],
                     "params": {"active_hours": null, "active_days": [...],
                                "required_duration_s": 600, ...}}]}

`ActivityDefinition.resolve_params` merges those values over the definition's
defaults: a setting the camera never stored takes its default, an unreadable one
takes its default AND is reported. Nothing here ever crashes a frame over a bad
value, and nothing silently reinterprets one.

RUNTIME STATE IS NEVER CONFIGURATION. Cooldown deadlines, per-track timers,
reported-track sets and cluster membership live on the activity instance and
nowhere else.

POINTS ARE NORMALISED 0-1, the convention zones and every bbox in this service
share. Activities that need pixel distances convert with the frame's size.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping, Optional

log = logging.getLogger("analytics.activities")

ZONE = "zone"
TRIPWIRE = "tripwire"

#: Zone rules — how an activity treats the regions a camera assigns it.
#: An area activity with NO configured zone watches the whole frame.
ZONE_OPTIONAL = "optional"
#: An area activity that needs at least one zone; with none it does not run.
ZONE_REQUIRED = "required"
#: Lines, not areas. Never widened to the frame.
ZONE_TRIPWIRE = "tripwire"

#: Can the CPU engine run it?
AVAILABLE = "available"
#: Registered so the catalog knows it exists; not implemented, never run.
HOLD = "hold"

#: Setting kinds — the VMS params_schema vocabulary.
NUMBER, TEXT, BOOL, LIST, ENUM = "number", "text", "bool", "list", "enum"

#: time.struct_time.tm_wday order (Monday = 0). AI Config stores "Mon".."Sun".
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── settings ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Param:
    """One administrator-tunable setting of an activity."""

    key: str
    label: str
    kind: str
    default: Any
    unit: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    #: A number that must be whole.
    integer: bool = False
    #: Allowed values: the choices of an ENUM, the items of a LIST.
    options: Optional[tuple] = None
    #: A LIST must keep at least this many items.
    min_items: Optional[int] = None
    help: Optional[str] = None
    #: Legacy spellings mapped to a current option, e.g. a DeepStream class
    #: name the CPU detector does not emit. Runtime compatibility only.
    aliases: Mapping[str, str] = field(default_factory=dict)

    def schema(self) -> dict:
        """The params_schema entry camera-mgmt stores and AI Config renders."""
        default = list(self.default) if isinstance(self.default, (list, tuple)) else self.default
        return {
            "key": self.key, "label": self.label, "kind": self.kind,
            "default": default, "unit": self.unit, "min": self.min, "max": self.max,
            "integer": self.integer,
            "options": list(self.options) if self.options is not None else None,
            "min_items": self.min_items, "help": self.help, "configurable": True,
        }

    def fallback(self) -> Any:
        return list(self.default) if isinstance(self.default, (list, tuple)) else self.default

    def resolve(self, raw: Any) -> tuple[Any, Optional[str]]:
        """(value to run with, problem or None) for a stored value."""
        if self.kind == NUMBER:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                return self.fallback(), f"{raw!r} is not a number"
            if self.integer and float(raw) != int(raw):
                return self.fallback(), f"{raw!r} is not a whole number"
            if self.min is not None and raw < self.min:
                return self.fallback(), f"{raw!r} is below the minimum {self.min:g}"
            if self.max is not None and raw > self.max:
                return self.fallback(), f"{raw!r} is above the maximum {self.max:g}"
            return (int(raw) if self.integer else float(raw)), None
        if self.kind == BOOL:
            if not isinstance(raw, bool):
                return self.fallback(), f"{raw!r} is not true/false"
            return raw, None
        if self.kind == TEXT:
            if not isinstance(raw, str):
                return self.fallback(), f"{raw!r} is not text"
            return raw, None
        if self.kind == ENUM:
            value = self.aliases.get(raw, raw) if isinstance(raw, str) else raw
            if self.options is not None and value not in self.options:
                return self.fallback(), f"{raw!r} is not one of {list(self.options)}"
            return value, None
        if self.kind == LIST:
            if not isinstance(raw, (list, tuple)):
                return self.fallback(), f"{raw!r} is not a list"
            kept: list = []
            dropped: list = []
            for item in raw:
                value = item.strip().lower() if isinstance(item, str) else item
                value = self.aliases.get(value, value)
                if self.options is not None and value not in self.options:
                    dropped.append(item)
                elif value not in kept:
                    kept.append(value)
            if self.min_items is not None and len(kept) < self.min_items:
                return self.fallback(), (f"needs at least {self.min_items} of {list(self.options or [])}"
                                         + (f"; unsupported: {dropped}" if dropped else ""))
            return kept, (f"unsupported values ignored: {dropped}" if dropped else None)
        return raw, None


@dataclass(frozen=True)
class ResolvedParams:
    values: dict
    #: Settings the camera never stored, running on their default.
    defaulted: tuple[str, ...] = ()
    #: Settings whose stored value could not be used as given.
    problems: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivityDefinition:
    """Everything the VMS needs to know about an activity, owned by its code."""

    key: str
    label: str
    description: str
    color: str
    status: str = AVAILABLE
    zone_rule: str = ZONE_REQUIRED
    domains: frozenset = frozenset()
    params: tuple[Param, ...] = ()
    #: Bump when the settings change shape, so the catalog shows what it holds.
    version: int = 1

    def wire(self) -> dict:
        return {
            "key": self.key, "label": self.label, "description": self.description,
            "color": self.color, "status": self.status, "zone_rule": self.zone_rule,
            "domains": sorted(self.domains), "version": self.version,
            "params_schema": [p.schema() for p in self.params],
        }

    def resolve_params(self, stored: Mapping) -> ResolvedParams:
        values: dict = {}
        defaulted: list[str] = []
        problems: dict[str, str] = {}
        for p in self.params:
            if stored.get(p.key) is None:
                values[p.key] = p.fallback()
                defaulted.append(p.key)
                continue
            value, problem = p.resolve(stored[p.key])
            values[p.key] = value
            if problem:
                problems[p.key] = problem
        return ResolvedParams(values, tuple(defaulted), problems)


# ── zones ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Zone:
    """One region an activity watches, in normalised frame coordinates."""

    #: The region id from analytics_config.regions; "" for the implicit frame.
    key: str
    name: Optional[str]
    kind: str
    points: tuple[tuple[float, float], ...]
    direction: Optional[str] = None
    #: True for the whole-frame zone an activity gets when none is configured.
    implicit: bool = False

    def contains(self, x: float, y: float) -> bool:
        """Is the normalised point (x, y) inside this area? A tripwire is a
        line, so it contains nothing."""
        if self.implicit:
            return 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        if self.kind != ZONE or len(self.points) < 3:
            return False
        inside = False
        pts = self.points
        j = len(pts) - 1
        for i, (xi, yi) in enumerate(pts):
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside


FULL_FRAME = Zone(key="", name=None, kind=ZONE,
                  points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
                  implicit=True)


def _zone_from_region(rid: str, region: Any) -> Optional[Zone]:
    if not isinstance(region, Mapping):
        return None
    kind = region.get("kind")
    try:
        pts = tuple((float(p[0]), float(p[1])) for p in region.get("points") or [])
    except (TypeError, ValueError, IndexError):
        return None
    if (kind == ZONE and len(pts) >= 3) or (kind == TRIPWIRE and len(pts) == 2):
        return Zone(key=rid, name=region.get("name") or rid, kind=kind, points=pts,
                    direction=region.get("direction"))
    return None


def resolve_zones(activity: Mapping, regions: Mapping, rule: str = ZONE_OPTIONAL) -> tuple[Zone, ...]:
    """The zones an activity watches, by its zone rule.

    Region ids that no longer resolve, and regions too malformed to use, are
    skipped. Then:

      ZONE_TRIPWIRE  the tripwires; never the frame
      ZONE_REQUIRED  the area zones; none → () and the activity does not run
      ZONE_OPTIONAL  the area zones; nothing configured → FULL_FRAME

    An optional activity whose only regions are tripwires gets () rather than
    the frame: it was pointed somewhere, just not at an area, and widening it
    to everything would be a guess.
    """
    configured = []
    for rid in activity.get("regions") or []:
        if isinstance(rid, str):
            zone = _zone_from_region(rid, regions.get(rid))
            if zone is not None:
                configured.append(zone)
    if rule == ZONE_TRIPWIRE:
        return tuple(z for z in configured if z.kind == TRIPWIRE)
    areas = tuple(z for z in configured if z.kind == ZONE)
    if areas:
        return areas
    if rule == ZONE_OPTIONAL and not configured:
        return (FULL_FRAME,)
    return ()


# ── schedule ──────────────────────────────────────────────────────────────────

def _seconds_of_day(value: Any) -> Optional[int]:
    """'HH:MM' or 'HH:MM:SS' → seconds since midnight; '24:00' → 86400."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) == 3 else 0
    if (h, m, s) == (24, 0, 0):
        return 86400
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return None
    return h * 3600 + m * 60 + s


@dataclass(frozen=True)
class Schedule:
    """AI Config's built-in schedule, with the semantics the stored shape has.

    `active_hours: null` (or absent) means all day. `active_days` ABSENT means
    every day, but PRESENT AND EMPTY means no day. A window whose end is before
    its start runs across midnight.
    """

    start: Optional[int] = None
    end: Optional[int] = None
    days: Optional[frozenset[int]] = None

    @classmethod
    def from_params(cls, params: Mapping) -> "Schedule":
        start = end = None
        hours = params.get("active_hours")
        if isinstance(hours, Mapping):
            s, e = _seconds_of_day(hours.get("start")), _seconds_of_day(hours.get("end"))
            if s is not None and e is not None:
                start, end = s, e
            else:
                log.warning("unreadable active_hours %r — treating as all day", hours)
        days = None
        if "active_days" in params:
            wanted = {str(d).strip().lower()[:3] for d in (params.get("active_days") or [])}
            days = frozenset(i for i, name in enumerate(_DAYS) if name in wanted)
        return cls(start, end, days)

    def is_active(self, ts: float) -> bool:
        t = time.localtime(ts)
        if self.days is not None and t.tm_wday not in self.days:
            return False
        if self.start is None or self.end is None:
            return True
        now = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
        if self.start <= self.end:
            return self.start <= now <= self.end
        return now >= self.start or now <= self.end


# ── what an activity is built from, and what it is given ─────────────────────

@dataclass(frozen=True)
class ActivitySpec:
    """One configured activity, resolved against its definition and its camera."""

    key: str
    #: Effective settings: the camera's values over the definition's defaults.
    params: Mapping[str, Any]
    zones: tuple[Zone, ...]
    schedule: Schedule
    defaulted: tuple[str, ...] = ()
    problems: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, definition: ActivityDefinition, activity: Mapping,
              regions: Mapping) -> "ActivitySpec":
        stored = activity.get("params")
        stored = dict(stored) if isinstance(stored, Mapping) else {}
        resolved = definition.resolve_params(stored)
        return cls(key=definition.key, params=resolved.values,
                   zones=resolve_zones(activity, regions if isinstance(regions, Mapping) else {},
                                       definition.zone_rule),
                   schedule=Schedule.from_params(stored),
                   defaulted=resolved.defaulted, problems=resolved.problems)


@dataclass(frozen=True)
class TrackedObject:
    """One detection in this frame, with the identity the tracker gave it."""

    track_id: Optional[str]
    domain: str                     # "person" | "vehicles"
    label: str                      # the detector's own class name
    confidence: float
    #: Normalised (x1, y1, x2, y2).
    bbox: tuple[float, float, float, float]
    #: The tracker considers this a real object, not a one-frame flicker.
    confirmed: bool
    #: This frame created the track.
    is_new: bool
    state: Optional[str] = None
    first_seen: Optional[float] = None

    @property
    def anchor(self) -> tuple[float, float]:
        """Bottom centre — where feet and wheels meet the ground plane."""
        x1, _, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, y2)

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def anchor_px(self, width: int, height: int) -> tuple[float, float]:
        x, y = self.anchor
        return x * width, y * height

    def width_px(self, width: int) -> float:
        return (self.bbox[2] - self.bbox[0]) * width


@dataclass(frozen=True)
class FrameContext:
    """Everything the activity pipeline knows about one frame."""

    camera: str
    ts: float                       # capture time, epoch seconds (the broker's)
    width: int
    height: int
    objects: tuple[TrackedObject, ...]
    #: The decoded frame (BGR ndarray), read-only.
    frame: Any = None


@dataclass
class ActivityEvent:
    """An activity's decision that something happened — one analytics event,
    shaped for the VMS ingest (`POST /api/analytics/events`)."""

    camera: str
    activity: str
    started_at: float
    zone: Zone
    confidence: Optional[float] = None
    track_id: Optional[str] = None
    object_class: Optional[str] = None
    attributes: dict = field(default_factory=dict)
    ended_at: Optional[float] = None
    #: Minted once, so a retried delivery lands on the same row.
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_ingest(self) -> dict:
        def iso(t: Optional[float]) -> Optional[str]:
            return None if t is None else datetime.fromtimestamp(t, tz=timezone.utc).isoformat()

        return {
            "id": self.id,
            "sensor_id": self.camera,
            "activity": self.activity,
            # The region id; the VMS resolves it to the name the operator drew.
            # None = whole frame.
            "zone": None if self.zone.implicit else self.zone.key,
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "confidence": None if self.confidence is None else round(float(self.confidence), 4),
            "track_id": self.track_id,
            "object_class": self.object_class,
            "attributes": {**self.attributes, "full_frame_zone": self.zone.implicit},
            "source": "cpu",
        }


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── the interface ─────────────────────────────────────────────────────────────

class Activity:
    """One configured activity on one camera.

    A subclass sets `definition` and, if implemented, overrides `evaluate`.
    `key`, `implemented` and `domains` are derived from the definition, so there
    is exactly one place to change them.
    """

    definition: ClassVar[ActivityDefinition]
    key: ClassVar[str] = ""
    implemented: ClassVar[bool] = False
    domains: ClassVar[frozenset] = frozenset()

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        d = cls.__dict__.get("definition")
        if d is not None:
            cls.key = d.key
            cls.implemented = d.status == AVAILABLE
            cls.domains = frozenset(d.domains)

    def __init__(self, camera: str, spec: ActivitySpec) -> None:
        self.camera = camera
        self.spec = spec

    @property
    def params(self) -> Mapping[str, Any]:
        return self.spec.params

    @property
    def zones(self) -> tuple[Zone, ...]:
        return self.spec.zones

    def process(self, ctx: FrameContext) -> list[ActivityEvent]:
        """The schedule, then the activity's own decision."""
        if not self.spec.schedule.is_active(ctx.ts):
            return []
        return self.evaluate(ctx)

    def evaluate(self, ctx: FrameContext) -> list[ActivityEvent]:
        raise NotImplementedError(f"{self.key or type(self).__name__} has no CPU implementation yet")

    def forget_tracks(self, track_ids: frozenset) -> None:
        """The tracker retired these ids; drop any state held for them."""

    def reset(self) -> None:
        """The stream was interrupted; per-track state from before is meaningless."""
