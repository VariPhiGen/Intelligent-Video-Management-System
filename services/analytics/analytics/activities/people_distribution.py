"""People Distribution — how many people are in each zone right now, and who.

A CPU port of the DeepStream activity (deepstream-yolo-e2e
python_module/component/activities/people_distribution.py + vms_distribution.py),
so a box with no GPU reports the same thing to the same VMS page.

A LEVEL, NOT AN OCCURRENCE. `process` returns no events: occupancy is posted as
periodic samples to the VMS People Distribution ingest
(`POST /api/analytics/people-distribution`), never filed as analytics events.

Rules carried over from DeepStream (each fixed a bug on a real camera):
  1. The zone anchor is the FEET, ((x1+x2)/2, y2) -- not the box centre, which
     sits in the next zone for anyone standing at an edge.
  2. A tracker is in exactly ONE zone. On a change it is removed from every
     zone before being placed, so a duplicate can never survive.
  3. No zones configured -> "frame"; outside every zone -> "outside". Both are
     reported to the VMS as ``__unzoned__``.
  4. A person is dropped `exit_grace_seconds` after they were last seen (or at
     once when the tracker retires the id), so the live count falls promptly.
     DeepStream counts 30 FRAMES at ~30 fps; the CPU path samples ~5 fps, so the
     grace is in seconds here to mean the same thing.

CPU differences: only CONFIRMED tracks count (the CPU tracker's own "not a
one-frame flicker", the same rule restricted_zone_entry uses), and the classes
are what the CPU detector can label.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .base import (AVAILABLE, LIST, NUMBER, ZONE_OPTIONAL, Activity,
                   ActivityDefinition, FrameContext, Param)

PERSON_DOMAIN = "person"

log = logging.getLogger(__name__)

CPU_CLASSES = ("person",)
DEFAULT_CLASSES = ("person",)

#: How often a sample is posted (DeepStream: DEEPSTREAM_VMS_DISTRIBUTION_EVERY=10).
SAMPLE_SECONDS = 10.0
#: How long an unseen person still counts as in their zone.
EXIT_GRACE_SECONDS = 1.0

OUTSIDE = "outside"
FRAME = "frame"
#: The label the VMS UI knows for "people not in any drawn zone".
UNZONED = "__unzoned__"
_UNZONED_KEYS = (OUTSIDE, FRAME, "", None)

DEFINITION = ActivityDefinition(
    key="people_distribution",
    label="People distribution",
    description="Counts how many people are in each zone right now, sampled on a "
                "timer, so an operator can see where people are rather than only "
                "where they have been.",
    color="#42a5f5",
    status=AVAILABLE,
    zone_rule=ZONE_OPTIONAL,
    domains=frozenset({PERSON_DOMAIN}),
    params=(
        Param("tracked_classes", "Classes to count", LIST, DEFAULT_CLASSES,
              options=CPU_CLASSES, min_items=1,
              help="Which detected objects count as people."),
        Param("sample_seconds", "Sample every", NUMBER, SAMPLE_SECONDS,
              unit="s", integer=True, min=5, max=300,
              help="How often the current count is recorded. This is the reading "
                   "interval, not a delay before counting starts."),
        Param("exit_grace_seconds", "Exit grace", NUMBER, EXIT_GRACE_SECONDS,
              unit="s", min=0, max=30,
              help="How long someone the detector briefly lost still counts as "
                   "in their zone."),
    ),
)


def build_payload(camera: str, zone_trackers: dict, now: float,
                  params: Optional[dict] = None, id_prefix: str = "") -> dict:
    """``zone_trackers`` is ``{zone: {tracker_id: {"first_seen_time": epoch}}}``.

    Same wire shape as DeepStream's, with source "cpu". Configured zones are
    always listed (count 0 when empty) so the VMS shows them; ``__unzoned__``
    only when someone is in it.
    """
    merged: dict[str, dict] = {}
    for zone, trackers in (zone_trackers or {}).items():
        name = UNZONED if zone in _UNZONED_KEYS else str(zone)
        merged.setdefault(name, {}).update(trackers or {})
    zones = []
    for name in sorted(merged):
        trackers = merged[name]
        if name == UNZONED and not trackers:
            continue
        dwell = [max(0.0, now - float(t.get("first_seen_time", now)))
                 for t in trackers.values()]
        zones.append({
            "zone_id": name,
            "zone_name": name,
            "current_count": len(trackers),
            "tracker_ids": [f"{id_prefix}{tid}" for tid in trackers],
            "dwell_seconds_max": round(max(dwell), 1) if dwell else 0.0,
            "dwell_seconds_avg": round(sum(dwell) / len(dwell), 1) if dwell else 0.0,
        })
    return {
        "camera": camera,
        "source": "cpu",
        "captured_at": round(now, 3),
        "total_count": sum(z["current_count"] for z in zones),
        "zones": zones,
        "params": dict(params or {}),
    }


class PeopleDistributionActivity(Activity):
    definition = DEFINITION

    def __init__(self, camera, spec) -> None:
        super().__init__(camera, spec)
        self.classes = frozenset(
            str(c).strip().lower() for c in spec.params["tracked_classes"])
        self.sample_seconds = float(spec.params["sample_seconds"])
        self.grace = float(spec.params.get("exit_grace_seconds", EXIT_GRACE_SECONDS))
        # Zone names as the VMS shows them; an implicit full-frame zone is FRAME.
        self._named = [(z, FRAME if z.implicit else (z.name or z.key)) for z in self.zones]
        self._last_sample = 0.0
        #: {zone name: {tracker_id: {"first_seen_time", "last_seen", "last_box", "class_name"}}}
        self.data: dict[str, dict] = {}
        self._where: dict[str, str] = {}           # tracker_id -> zone it is in now
        #: The most recent sample, for whatever reads it (also posted to the VMS).
        self.latest: Optional[dict] = None

    def _zone_for(self, foot) -> str:
        if not self._named:
            return FRAME
        for zone, name in self._named:
            if zone.contains(*foot):
                return name
        return OUTSIDE

    def _drop(self, tracker_id) -> None:
        for trackers in self.data.values():
            trackers.pop(tracker_id, None)
        self._where.pop(tracker_id, None)

    def process(self, ctx: FrameContext) -> list:
        """Track who is where on every frame; sample on the timer. No events."""
        if not self.spec.schedule.is_active(ctx.ts):
            return []
        now = ctx.ts or time.time()
        for _, name in self._named:
            self.data.setdefault(name, {})
        for obj in ctx.objects:
            cls = obj.label.strip().lower()
            if cls not in self.classes or not obj.confirmed or obj.track_id is None:
                continue
            tid = obj.track_id
            zone = self._zone_for(obj.anchor)            # feet, normalised
            if self._where.get(tid) != zone:
                # moved (or new): out of EVERY zone first, then into the new one
                first_seen = None
                for trackers in self.data.values():
                    info = trackers.pop(tid, None)
                    if info and first_seen is None:
                        first_seen = info.get("first_seen_time")
                self.data.setdefault(zone, {})[tid] = {
                    "first_seen_time": first_seen or obj.first_seen or now}
                self._where[tid] = zone
            info = self.data[zone][tid]
            info.update(last_seen=now, last_box=list(obj.bbox), class_name=cls)

        # exit: unseen for longer than the grace
        for tid, zone in list(self._where.items()):
            if now - self.data.get(zone, {}).get(tid, {}).get("last_seen", 0.0) > self.grace:
                self._drop(tid)

        if now - self._last_sample >= self.sample_seconds:
            self._last_sample = now
            self.latest = build_payload(self.camera, self.data, now,
                                        dict(self.spec.params), _ID_PREFIX)
            sink = _SINK
            if sink is not None:
                sink.observe(self.latest)
        return []

    def count(self, ctx: FrameContext) -> dict:
        """The occupancy as of this frame, without waiting for the sample timer."""
        self.process(ctx)
        return build_payload(self.camera, self.data, ctx.ts or time.time(),
                             dict(self.spec.params), _ID_PREFIX)

    def forget_tracks(self, track_ids: frozenset) -> None:
        for tid in track_ids:
            self._drop(tid)

    def reset(self) -> None:
        self._last_sample = 0.0
        self.data = {}
        self._where = {}
        self.latest = None


# ── delivery to the VMS ─────────────────────────────────────────────────────
#: Tracker ids restart whenever this process does; the VMS unions ids within a
#: clock hour, so tell this run's ids apart from the last run's.
_ID_PREFIX = f"{int(time.time()) % 100000:x}-"


class _Permanent(Exception):
    """A rejection retrying cannot fix."""


class DistributionSink:
    """Posts samples to the VMS on its own thread. Newest sample per camera wins
    (a stale count is worthless), `observe` never blocks the frame path, a 4xx
    other than 408/429 is dropped, anything else is replaced by the next sample."""

    def __init__(self, url: str, key: str, timeout: float = 3.0) -> None:
        self.url = url.rstrip("/") + "/api/analytics/people-distribution"
        self._key = key
        self._timeout = timeout
        self._cv = threading.Condition()
        self._pending: dict[str, dict] = {}
        self.counters = {"sampled": 0, "sent": 0, "refused": 0, "failed": 0,
                         "last_error": None}
        self._last_fail_log = 0.0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="people-distribution",
                                        daemon=True)
        self._thread.start()
        log.info("people distribution sink -> %s", self.url)

    def observe(self, payload: dict) -> None:
        with self._cv:
            self._pending[payload["camera"]] = payload
            self.counters["sampled"] += 1
            self._cv.notify()

    def drain_once(self) -> int:
        with self._cv:
            batch, self._pending = list(self._pending.values()), {}
        for payload in batch:
            try:
                self._post(payload)
                self.counters["sent"] += 1
            except _Permanent as e:
                self.counters["refused"] += 1
                self._fail(f"refused: {e}")
            except Exception as e:                                  # noqa: BLE001
                self.counters["failed"] += 1
                self._fail(f"failed (will send the next sample): {e}")
        return len(batch)

    def _run(self) -> None:
        while True:
            with self._cv:
                if not self._pending:
                    self._cv.wait(timeout=5.0)
            try:
                self.drain_once()
            except Exception as e:                                  # noqa: BLE001
                self.counters["last_error"] = f"drain: {e}"

    def _fail(self, msg: str) -> None:
        self.counters["last_error"] = msg
        now = time.monotonic()
        if now - self._last_fail_log >= 60.0:
            self._last_fail_log = now
            log.warning("people distribution %s", msg)

    def _post(self, payload: dict) -> None:
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-Internal-Key": self._key})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                r.read()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (408, 429):
                raise _Permanent(f"HTTP {e.code}") from e
            raise

    def snapshot(self) -> dict:
        with self._cv:
            return {**self.counters, "pending": len(self._pending), "url": self.url}


_SINK: Optional[DistributionSink] = None


def install(sink: Optional[DistributionSink]) -> Optional[DistributionSink]:
    """Set the process-wide sink (the engine does this at start). None = samples
    are kept in `latest` and not posted."""
    global _SINK
    _SINK = sink
    return sink
