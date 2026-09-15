"""engine.py — the set of cameras this service indexes.

State is in-memory, exactly like the motion service: a restart loses every
camera and the registry's reconcile loop re-asserts them within one interval.
The service never sees camera credentials, only relay URLs.

NO CAPTURE THREADS. Detection moved to services/analytics, which decodes
nothing itself either — it reads the frame broker — and posts finished
observations to POST /observations. What is registered here is which cameras
are SEARCHABLE and which domains each contributes, not anything that pulls
pixels.

THE PIPELINE IS NOT A CONSTANT. It arrives when the first camera is registered
and goes away once the last one has been gone a while — see models.ModelPool.
So a camera can be registered before the encoder is resident, and every path
here has to treat a missing pipeline as "not yet" rather than as "broken". Two
rules come out of that and are easy to violate later:

  * The engine never calls the pool while holding the engine lock, and the pool
    never calls the engine while holding the pool lock. Both call each other, so
    without a fixed order the first camera removed during a warm-up deadlocks.
  * One camera's registry operations are serialised by a per-slug lock, because
    `upsert_camera` is a remove followed by an add and the registry issues the
    same instruction from four workers at once. See its docstring.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import AppConfig
from .models import DISABLED, ModelPool, UNAVAILABLE
from .pipeline import IngestPipeline

log = logging.getLogger("smartsearch.engine")


DEFAULT_DOMAINS = ("person", "vehicles")

#: Registered, and its frames will be sampled as soon as the weights are in.
#: Distinct from REGISTERED_NO_INGEST, which means they never will be.
WARMING_UP = "WARMING_UP"

#: A producer unheard from for longer than this is reported stale. Three times
#: the analytics heartbeat interval, so one missed beat is not an alarm.
PRODUCER_STALE_SECONDS = 90.0


@dataclass
class CameraEntry:
    slug: str
    rtsp_url: str
    #: Which domains this camera contributes. Empty would mean it contributes
    #: nothing, which the registry treats as "not indexed" and never sends.
    domains: tuple[str, ...] = DEFAULT_DOMAINS
    #: How long crops from this camera stay searchable, or None to follow the
    #: appliance default. Pushed by the registry from the camera's recording
    #: retention so the index cannot outlive the footage it describes — see
    #: Store.set_camera_retention for why this has to restamp existing rows.
    retention_days: int | None = None
    added_at: float = field(default_factory=time.time)

    def to_dict(self, ingest_state: str = "", ingest_error: Optional[str] = None,
                broker: Optional[dict] = None, receive_only: bool = True) -> dict:
        """One state, because there is one way frames reach this service now:
        they do not. Analytics detects and posts observations, so reporting
        WARMING_UP or CONNECTING here would describe a decoder that does not
        exist and send an operator hunting a fault that is not there.

        `broker` and `receive_only` are kept in the signature so existing
        callers and tests do not have to change in the same commit; both are
        ignored.
        """
        return {
            "name": self.slug,
            "rtsp_url": self.rtsp_url,
            "added_at": self.added_at,
            "domains": list(self.domains),
            # Reported so the registry's reconcile pass can see drift. Without
            # it a retention change whose push failed would heal only on the
            # next restart, which is the failure mode reconcile exists for.
            "retention_days": self.retention_days,
            "state": ("REGISTERED_NO_INGEST"
                      if (ingest_state in (DISABLED, UNAVAILABLE) or ingest_error)
                      else "RECEIVING"),
            "frames_sampled": 0,
            "frame_source": "none",
            "last_error": ingest_error or None,
        }


class IndexEngine:
    def __init__(self, config: AppConfig, pool: ModelPool) -> None:
        self._config = config
        self._pool = pool
        self._pipeline: Optional[IngestPipeline] = None
        # NO FRAME SOURCE AT ALL. Detection moved to the analytics service,
        # which posts finished observations to POST /observations. This
        # service never looks at a frame, so there is no sampler, no
        # subscription and no decoder here.
        self._cameras: dict[str, CameraEntry] = {}
        self._lock = threading.Lock()
        #: Per-camera, and ALWAYS taken before self._lock, never after — the
        #: full order is slug lock -> engine lock -> pool lock. Re-entrant
        #: because upsert_camera holds one across the remove/add pair that
        #: takes it again. Never taken by the pool, so it adds no cycle.
        self._slug_locks: dict[str, threading.RLock] = {}
        self._slug_guard = threading.Lock()
        #: Producers that have announced themselves, by name. See
        #: note_producer: this is what turns "the index stopped growing" from
        #: a silent condition into a reported one.
        self._producers: dict[str, dict] = {}
        pool.set_listener(self._on_pipeline)

    def _slug_lock(self, slug: str) -> threading.RLock:
        """One lock per camera, created on demand and kept.

        Not reaped on removal: a lock a thread is currently inside must not be
        replaced under it, and a few hundred bytes per camera the deployment has
        ever registered is not a leak worth that risk.
        """
        with self._slug_guard:
            lock = self._slug_locks.get(slug)
            if lock is None:
                lock = threading.RLock()
                self._slug_locks[slug] = lock
            return lock

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def pipeline(self) -> Optional[IngestPipeline]:
        return self._pipeline

    @property
    def ingest_enabled(self) -> bool:
        """Whether registered cameras WILL be indexed — now or once warm.

        Deliberately not "is a pipeline loaded right now": a hibernating service
        with no cameras still has ingest enabled, and an operator told otherwise
        would go looking for a fault that is not there.
        """
        return self._pool.ingest_possible

    @property
    def camera_count(self) -> int:
        with self._lock:
            return len(self._cameras)

    def snapshot(self) -> list[dict]:
        state, error = self._pool.ingest_state, self._pool.ingest_error
        with self._lock:
            return [c.to_dict(state, error)
                    for c in self._cameras.values()]

    def get_camera(self, slug: str) -> Optional[CameraEntry]:
        with self._lock:
            return self._cameras.get(slug)

    def describe(self, entry: CameraEntry) -> dict:
        return entry.to_dict(self._pool.ingest_state, self._pool.ingest_error)

    # ── who is feeding this index ───────────────────────────────────────────
    def note_producer(self, name: str, stats: Optional[dict] = None) -> None:
        """A producer saying it is alive.

        WHY A HEARTBEAT AND NOT JUST 'HAVE WE HAD OBSERVATIONS LATELY'. A quiet
        camera legitimately produces nothing for hours, so silence cannot be
        read as a fault — a staleness check on observations alone would cry
        wolf every night and be ignored by morning. A heartbeat arrives whether
        or not anything was detected, which is what separates "the producer is
        alive and the scene is quiet" from "the producer is gone".

        THIS EXISTS BECAUSE THE SECOND ONE HAPPENED. The stack was brought up
        without the analytics profile and this service reported healthy for
        eight hours while the index did not grow by a single row.
        """
        self._producers[name] = {
            "last_seen": time.time(),
            "stats": stats or {},
        }

    def producers_snapshot(self) -> dict:
        """Who is feeding this index, and whether they still are."""
        now = time.time()
        out: dict[str, dict] = {}
        for name, rec in self._producers.items():
            ago = now - rec["last_seen"]
            out[name] = {
                "seconds_ago": round(ago, 1),
                "stale": ago > PRODUCER_STALE_SECONDS,
                **rec["stats"],
            }
        healthy = [n for n, p in out.items() if not p["stale"]]
        return {
            "expected": True,
            "producers": out,
            # THE LINE AN OPERATOR ACTUALLY READS. This service detects
            # nothing itself, so with no live producer the index cannot grow
            # no matter how healthy everything looks.
            "state": "RECEIVING" if healthy else (
                "NO_PRODUCER" if not out else "PRODUCER_STALE"),
            "detail": None if healthy else (
                "nothing is feeding this index. The analytics service does the "
                "detecting now: check it is running (COMPOSE_PROFILES must "
                "include 'analytics') and that ANALYTICS_SINK_URL points here."),
        }

    def frame_source_snapshot(self) -> dict:
        """What is feeding this pipeline, for /health.

        Reported even though there is only one answer, because "where do the
        rows come from" is the first question anyone asks of a service that no
        longer decodes anything.
        """
        return {"source": "none",
                "state": "RECEIVING",
                "detail": "detection runs in the analytics service; "
                          "observations arrive on POST /observations"}

    def _attach_locked(self, entry: CameraEntry,
                       pipeline: Optional[IngestPipeline]) -> None:
        """Caller holds the lock. Idempotent.

        There is no capture thread to start any more — analytics detects and
        posts observations — so all this does is tell the pipeline which
        domains the camera contributes. That still matters: the pipeline
        refuses an observation for a domain this camera does not contribute,
        and it is the last check before the store an operator later trusts.
        """
        if pipeline is None:
            return
        pipeline.set_domains(entry.slug, entry.domains)

    def _on_pipeline(self, pipeline: Optional[IngestPipeline]) -> None:
        """The pool handing over a pipeline, or taking it back.

        Called with the pool lock NOT held, so it is free to join threads.
        """
        if pipeline is not None:
            with self._lock:
                self._pipeline = pipeline
                for entry in self._cameras.values():
                    self._attach_locked(entry, pipeline)
            log.info("ingest attached to %d camera(s)", self.camera_count)
            return

        # No capture threads to join, because there are none to start. The
        # lock-then-join dance this used to do existed only for them.
        with self._lock:
            self._pipeline = None
        log.info("ingest detached")

    # ── registry ─────────────────────────────────────────────────────────────
    def add_camera(self, slug: str, rtsp_url: str,
                   domains: tuple[str, ...] = DEFAULT_DOMAINS,
                   retention_days: int | None = None) -> CameraEntry:
        """Idempotent from the caller's side: an existing camera raises KeyError
        so the API can answer 409, which the registry sync treats as success."""
        with self._slug_lock(slug), self._lock:
            if slug in self._cameras:
                raise KeyError(slug)
            entry = CameraEntry(slug=slug, rtsp_url=rtsp_url,
                                domains=tuple(domains) or DEFAULT_DOMAINS,
                                retention_days=retention_days)
            self._cameras[slug] = entry
            first = len(self._cameras) == 1
            pipeline = self._pipeline
            self._attach_locked(entry, pipeline)
        if first:
            # OUTSIDE the lock — see the lock-order rule at the top. Returns
            # immediately; the warm-up runs on its own thread and comes back
            # through _on_pipeline.
            self._pool.want_ingest(True)
        log.info("camera added slug=%s", slug)
        return entry

    def upsert_camera(self, slug: str, rtsp_url: str,
                      domains: tuple[str, ...] = DEFAULT_DOMAINS,
                      retention_days: int | None = None
                      ) -> tuple[CameraEntry, str]:
        """Create the camera, or bring an existing one to this state. Returns
        (entry, "created" | "updated" | "unchanged").

        IDEMPOTENT ON PURPOSE. The registry runs its reconcile loop in every
        uvicorn worker — four of them — so several loops issue the same
        instruction concurrently. With create-or-409 plus remove-and-re-add, two
        loops interleave into add / remove / add and a camera can end up present
        in the registry but missing from the pipeline. An upsert makes a race
        harmless: every loop is asking for the same end state.

        A domain change does NOT restart capture. Only a URL change does, because
        only that invalidates the connection — and note that path drops the
        camera count to zero on a single-camera deployment, which is exactly why
        the model release is on a timer rather than on the transition.

        THE REMOVE AND THE ADD ARE ONE ATOMIC STEP, under the per-slug lock.
        They were not, and being idempotent was not enough to make that safe: on
        a URL change all four reconcile loops see a stale URL, all four remove,
        one add wins and the other three hit `add_camera`'s "already exists"
        KeyError — which is not a 409 here, it escapes the route as a 500. The
        registry logs it as a failed sync and retries into the same race.
        """
        domains = tuple(domains) or DEFAULT_DOMAINS
        with self._slug_lock(slug):
            with self._lock:
                entry = self._cameras.get(slug)
                if entry is not None and entry.rtsp_url == rtsp_url:
                    # Retention is metadata about how long rows live, not about
                    # where frames come from, so a change to it never restarts
                    # capture — it is applied here and again in the route, which
                    # owns the store and does the restamp.
                    retention_changed = entry.retention_days != retention_days
                    if entry.domains == domains and not retention_changed:
                        return entry, "unchanged"
                    entry.domains = domains
                    entry.retention_days = retention_days
                    if self._pipeline is not None:
                        self._pipeline.set_domains(slug, domains)
                    log.info("camera updated slug=%s domains=%s retention_days=%s",
                             slug, list(domains), retention_days)
                    return entry, "updated"
            # URL changed, or brand new. Outside the REGISTRY lock, because
            # stopping a capture thread joins; still inside the slug lock, so no
            # other worker can interleave its own remove/add with this one.
            if entry is not None:
                self.remove_camera(slug)
            return self.add_camera(slug, rtsp_url, domains, retention_days), "created"

    def remove_camera(self, slug: str) -> bool:
        with self._slug_lock(slug):
            with self._lock:
                entry = self._cameras.pop(slug, None)
                last = entry is not None and not self._cameras
                pipeline = self._pipeline
            if entry is None:
                return False
            if pipeline is not None:
                pipeline.forget_domains(slug)
        if last:
            # Outside the slug lock too — see the lock-order rule above.
            self._pool.want_ingest(False)
        log.info("camera removed slug=%s", slug)
        return True

    def stop_all(self) -> None:
        with self._lock:
            entries = list(self._cameras.values())
            self._cameras.clear()
        if entries:
            self._pool.want_ingest(False)
        self._pool.stop()
