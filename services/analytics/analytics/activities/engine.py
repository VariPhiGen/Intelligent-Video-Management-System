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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _CameraActivities:
    fingerprint: str
    activities: tuple[Activity, ...]
    #: Keys in the config this build has no module for at all.
    unknown: tuple[str, ...]

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

        self.frames_observed = 0
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
            built: list[Activity] = []
            unknown: list[str] = []
            for entry in cfg.get("activities") or []:
                if not isinstance(entry, Mapping) or not entry.get("type"):
                    continue
                cls = self._registry.get(entry["type"])
                if cls is None:
                    unknown.append(str(entry["type"]))
                    continue
                built.append(cls(camera, ActivitySpec.build(cls.definition, entry, regions)))
            new = _CameraActivities(fingerprint, tuple(built), tuple(unknown))
            self._cameras = {**self._cameras, camera: new}
        summary = new.summary()
        for act in built:
            if act.spec.problems:
                log.warning("%s on %s: settings not usable as stored, defaults used: %s",
                            act.key, camera, act.spec.problems)
        if summary["configured"]:
            log.info("activities for %s: running=%s not_implemented=%s unknown=%s no_zone=%s",
                     camera, summary["running"], summary["not_implemented"],
                     summary["unknown"], summary["not_running_no_zone"])
        return summary

    def forget(self, camera: str) -> None:
        with self._lock:
            if camera in self._cameras:
                self._cameras = {k: v for k, v in self._cameras.items() if k != camera}

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
            "events_emitted": self.events_emitted,
            "errors": self.errors,
            "last_error": self.last_error,
        }
