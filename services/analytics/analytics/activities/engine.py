"""engine.py — the CPU Activity Engine: configured activities, run per frame.

    broker frame (5 FPS, every frame) → YOLO → tracker → ActivityEngine → events

The engine owns no model and sees no stream. analytics/activity_pipeline.py
hands it each frame's tracked objects; the engine builds and runs the
activities each camera is configured for, and hands their events on.

THE REGISTRY IS THE SOURCE OF TRUTH FOR WHAT CAN RUN. Every activity class is
registered under its definition's key (analytics/activities/__init__.py);
`definitions()` is what GET /activities publishes and camera-mgmt copies into
its Activity Type catalog. Only definitions with status AVAILABLE ever run.

CONFIGURATION ARRIVES WITH THE CAMERA. camera-mgmt pushes each camera's stored
`analytics_config` with its registration and re-asserts it every reconcile
pass. Re-asserting an unchanged config must not reset an activity's memory, so
instances are rebuilt only when the config's fingerprint changes.

THREAD MODEL. `configure`/`forget` run on API threads; `observe` runs on the
activity worker. Configuration is copy-on-write: the worker reads a dict that
is replaced whole, never mutated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional

from .base import Activity, ActivityEvent, ActivitySpec, FrameContext

log = logging.getLogger("analytics.activities")


def config_fingerprint(analytics_config: Optional[Mapping]) -> str:
    """A stable digest of the parts of analytics_config activities run on.

    camera-mgmt computes the same function over the stored config and compares
    it with what this service reports. Both sides' tests pin one literal digest.
    """
    cfg = analytics_config if isinstance(analytics_config, Mapping) else {}
    canonical = json.dumps(
        {"regions": cfg.get("regions") or {}, "activities": cfg.get("activities") or []},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


def activity_fingerprint(entry: Mapping, regions: Mapping) -> str:
    """A digest of ONE activity: its type, its settings, and the geometry it watches.

    The camera-wide fingerprint says "something about this camera changed", which
    is the right question for "do we need to look again" and the wrong one for
    "must this activity forget everything it knows". Saving an unrelated setting
    used to rebuild every activity on the camera and discard its memory, so a
    person still standing in a restricted zone became a second violation. This
    digest is what lets an untouched activity keep running.
    """
    wanted = [r for r in (entry.get("regions") or []) if isinstance(r, str)]
    canonical = json.dumps(
        {"type": entry.get("type"), "params": entry.get("params") or {},
         "regions": [[rid, regions.get(rid)] for rid in wanted]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


@dataclass(frozen=True)
class _CameraActivities:
    fingerprint: str
    activities: tuple[Activity, ...]
    #: Keys in the config this build has no module for at all.
    unknown: tuple[str, ...]
    #: Per-activity digest, so the next pass can tell which ones really changed.
    prints: Mapping[str, str] = field(default_factory=dict)

    @property
    def runnable(self) -> tuple[Activity, ...]:
        # An implemented activity with no zone to watch (a zone-required one
        # that was given none) has nothing to evaluate.
        return tuple(a for a in self.activities if a.implemented and a.zones)

    def summary(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "configured": [a.key for a in self.activities] + list(self.unknown),
            "running": [a.key for a in self.runnable],
            "not_implemented": [a.key for a in self.activities if not a.implemented],
            "unknown": list(self.unknown),
            "zones": {a.key: ["<full frame>" if z.implicit else z.key for z in a.zones]
                      for a in self.activities},
            "not_running_no_zone": [a.key for a in self.activities
                                    if a.implemented and not a.zones],
            "settings": {a.key: {"values": dict(a.spec.params),
                                 "defaulted": list(a.spec.defaulted),
                                 "problems": dict(a.spec.problems)}
                         for a in self.activities},
        }


class ActivityEngine:
    def __init__(self, emit: Optional[Callable[[list[ActivityEvent]], None]] = None,
                 registry: Optional[Mapping[str, type[Activity]]] = None) -> None:
        if registry is None:
            from . import ACTIVITY_CLASSES
            registry = ACTIVITY_CLASSES
        self._registry = dict(registry)
        self._emit = emit
        self._lock = threading.Lock()
        self._cameras: dict[str, _CameraActivities] = {}
        #: Live zone occupancy per camera, rewritten on every observed frame:
        #: {camera: {"ts": capture time, "zones": {zone key: {...count...}}}}.
        #: A level, not an event, for callers that hold an output while a zone
        #: is occupied (the gate relay). Any person detection counts, tracked
        #: or not, so occupancy never depends on a tracker id surviving.
        self._presence: dict[str, dict] = {}

        self.frames_observed = 0
        #: Activities carried across a rebuild with their memory intact, because
        #: nothing about them changed. Each one is a duplicate event avoided.
        self.states_preserved = 0
        self.events_emitted = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    # ── the registry ────────────────────────────────────────────────────────
    def definitions(self) -> list[dict]:
        """Every registered activity's definition, as published to the VMS."""
        return [cls.definition.wire() for cls in self._registry.values()]

    # ── configuration ───────────────────────────────────────────────────────
    def configure(self, camera: str, analytics_config: Optional[Mapping]) -> dict:
        """Apply a camera's stored analytics_config. Idempotent by fingerprint."""
        cfg = analytics_config if isinstance(analytics_config, Mapping) else {}
        fingerprint = config_fingerprint(cfg)
        with self._lock:
            current = self._cameras.get(camera)
            if current is not None and current.fingerprint == fingerprint:
                return current.summary()
            regions = cfg.get("regions") if isinstance(cfg.get("regions"), Mapping) else {}
            running = {a.key: a for a in current.activities} if current is not None else {}
            was = dict(current.prints) if current is not None else {}
            built: list[Activity] = []
            unknown: list[str] = []
            prints: dict[str, str] = {}
            carried: list[str] = []
            for entry in cfg.get("activities") or []:
                if not isinstance(entry, Mapping) or not entry.get("type"):
                    continue
                cls = self._registry.get(entry["type"])
                if cls is None:
                    unknown.append(str(entry["type"]))
                    continue
                key = str(entry["type"])
                digest = activity_fingerprint(entry, regions)
                prints[key] = digest
                kept = running.get(key)
                if kept is not None and was.get(key) == digest:
                    # Nothing about THIS activity changed, so it keeps running
                    # with everything it remembers: stays, cooldowns, parking
                    # timers, half-finished crossings.
                    built.append(kept)
                    carried.append(key)
                    continue
                built.append(cls(camera, ActivitySpec.build(cls.definition, entry, regions)))
            new = _CameraActivities(fingerprint, tuple(built), tuple(unknown), prints)
            self._cameras = {**self._cameras, camera: new}
            self.states_preserved += len(carried)
        summary = new.summary()
        for act in built:
            if act.spec.problems:
                log.warning("%s on %s: settings not usable as stored, defaults used: %s",
                            act.key, camera, act.spec.problems)
        if summary["configured"]:
            log.info("activities for %s: running=%s not_implemented=%s unknown=%s no_zone=%s "
                     "state_kept=%s",
                     camera, summary["running"], summary["not_implemented"],
                     summary["unknown"], summary["not_running_no_zone"], carried)
        return summary

    def forget(self, camera: str) -> None:
        with self._lock:
            if camera in self._cameras:
                self._cameras = {k: v for k, v in self._cameras.items() if k != camera}
            self._presence.pop(camera, None)

    def reset(self, camera: str) -> None:
        entry = self._cameras.get(camera)
        for act in entry.activities if entry else ():
            act.reset()

    def forget_tracks(self, track_ids: Iterable[str]) -> None:
        """Tracker ids retired by the tracker — every camera's activities drop
        what they held for them (ids carry their camera, so none collide)."""
        ids = frozenset(track_ids)
        if not ids:
            return
        for entry in self._cameras.values():
            for act in entry.activities:
                act.forget_tracks(ids)

    # ── what the pipeline asks ──────────────────────────────────────────────
    def fingerprint(self, camera: str) -> Optional[str]:
        entry = self._cameras.get(camera)
        return entry.fingerprint if entry else None

    def has_work(self, camera: str) -> bool:
        entry = self._cameras.get(camera)
        return bool(entry and entry.runnable)

    def wanted_domains(self, camera: str) -> frozenset[str]:
        """Detector domains this camera's RUNNING activities consume."""
        entry = self._cameras.get(camera)
        if not entry:
            return frozenset()
        return frozenset().union(*(a.domains for a in entry.runnable))

    def observe(self, ctx: FrameContext) -> list[ActivityEvent]:
        """Run every runnable activity on this frame; hand events on."""
        entry = self._cameras.get(ctx.camera)
        if entry is None:
            return []
        runnable = entry.runnable
        if not runnable:
            return []
        self.frames_observed += 1
        self._note_presence(ctx, runnable)
        events: list[ActivityEvent] = []
        for act in runnable:
            try:
                events.extend(act.process(ctx))
            except Exception as exc:                               # noqa: BLE001
                self.errors += 1
                self.last_error = f"{act.key}: {type(exc).__name__}: {exc}"
                log.exception("activity %s failed on %s", act.key, ctx.camera)
        if events:
            self.events_emitted += len(events)
            if self._emit is not None:
                self._emit(events)
        return events

    def _note_presence(self, ctx: FrameContext, runnable: tuple[Activity, ...]) -> None:
        zones = {}
        for act in runnable:
            for z in act.zones:
                zones.setdefault(z.key or "__frame__", z)
        people = [o for o in ctx.objects if o.label.strip().lower() == "person"]
        out = {}
        for key, zone in zones.items():
            inside = [o for o in people if zone.contains(*o.center)]  # same point as restricted_zone_entry
            out[key] = {"name": zone.name or key, "count": len(inside),
                        "confirmed": sum(1 for o in inside if o.confirmed)}
        self._presence[ctx.camera] = {"ts": ctx.ts, "zones": out}

    def presence(self, camera: str) -> Optional[dict]:
        return self._presence.get(camera)

    # ── observability ───────────────────────────────────────────────────────
    def cameras_with_work(self) -> list[str]:
        return [slug for slug, e in self._cameras.items() if e.runnable]

    def camera_summary(self, camera: str) -> Optional[dict]:
        entry = self._cameras.get(camera)
        return entry.summary() if entry else None

    def snapshot(self) -> dict:
        return {
            "implemented": sorted(k for k, c in self._registry.items() if c.implemented),
            "on_hold": sorted(k for k, c in self._registry.items() if not c.implemented),
            "cameras": {slug: e.summary() for slug, e in self._cameras.items()},
            "frames_observed": self.frames_observed,
            "states_preserved": self.states_preserved,
            "events_emitted": self.events_emitted,
            "errors": self.errors,
            "last_error": self.last_error,
        }
