"""engine.py — cameras in, models loaded on demand, observations out.

WHAT THIS OWNS. The camera set (pushed at runtime by camera-mgmt, in memory
only and re-asserted by its reconcile loop), the frame source, and the lifetime
of the detector and plate reader. One pipeline, one worker; the models cost is
paid once for every camera.

WHAT IT DELIBERATELY DOES NOT OWN. Anything downstream of the crop. There is no
encoder here, no database, no retention and no query API — those belong to
Smart Search, and keeping them out is the whole reason this service exists.

THE MODELS ARE INGEST-ONLY, SO THEY COME AND GO WITH THE CAMERAS. Three rules,
each of them a bug that was observed before it was written down:

1. LOAD ON THE FIRST CAMERA, NOT AT IMPORT. A deployment that registers no
   analytics cameras should not pay for weights it will never run, and the
   health endpoint must answer before the first load finishes or the container
   is marked unhealthy while it is doing exactly what it was asked to do.

2. RELEASE WHEN THE LAST CAMERA GOES — on a timer, never on the transition.
   Switching every camera's Person/Vehicles/ANPR off left this service holding
   a YOLO pass's worth of weights for ever: measured on the appliance, 164 MiB
   of GPU and 1.36 GiB of RSS with zero cameras and nothing to detect. The
   invariant is older than this service — Smart Search's index/models.py said
   "the detector and plate reader are ingest-only and go when the cameras go"
   and enforced it, until `9f5a5ec` moved the models here and left the
   lifecycle behind. The timer is what makes it safe: camera-mgmt reconciles
   from every uvicorn worker and a domain change is a remove followed by an
   add, so the count touches zero without anybody switching anything off.

3. WARM-UP NEVER BLOCKS THE CALLER. camera-mgmt's POST /cameras has a 10 s
   timeout; loading the detector inline took 41 s on this appliance's first
   camera (15:19:46 → 15:20:27 in the container log), so the sync timed out,
   retried, and was healed only by a later reconcile pass. With hibernation
   that would recur on every wake instead of once per boot. The load runs on
   its own thread and cameras registered meanwhile are attached when it lands.

THE PLATE READER HAS ITS OWN DEMAND SIGNAL. It is a second model over every
vehicle and only the `plate` domain asks for it, so it is loaded when some
camera wants plates and released when none do — independently of the detector,
which every domain needs. `plates.build` answers to configuration alone, so
without this an appliance with ANPR switched off on every camera still loaded
the localiser and the OCR model and still reported them active.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import threading
import time
from typing import Optional

import numpy as np

from .config import AppConfig
from .pipeline import DetectionPipeline

log = logging.getLogger(__name__)

WARMING_UP = "WARMING_UP"
RUNNING = "RUNNING"
IDLE = "IDLE"
DISABLED = "DISABLED"
FAILED = "FAILED"

PLATE_DOMAIN = "plate"
#: The face domain. A second model over every PERSON, on exactly the plate
#: reader's terms — see the demand block below.
FACE_DOMAIN = "face"

#: Pipeline counters folded forward when a pipeline is released, so /health on
#: an appliance that hibernates nightly does not appear to re-derive its whole
#: throughput history every morning.
_LIFETIME_KEYS = (
    "frames_queued", "frames_dropped", "frames_evicted", "frames_processed",
    "frames_skipped_no_motion", "frames_gated_by_broker", "detector_calls",
    "detections_seen", "detections_nested", "detections_tentative",
    "crops_too_small",
    "crops_suppressed_by_policy", "observations_produced", "localiser_calls",
)


def _load_malloc_trim():
    """glibc's malloc_trim, or a no-op where there isn't one.

    Resolved once: this is called on every release, and musl (Alpine) has no
    such symbol at all — a missing trim costs memory, never correctness.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                           use_errno=True)
        fn = libc.malloc_trim
        fn.argtypes = [ctypes.c_size_t]
        fn.restype = ctypes.c_int
        return lambda: fn(0)
    except (OSError, AttributeError) as exc:                       # noqa: BLE001
        log.info("malloc_trim unavailable (%s); released memory will be reused "
                 "by this process rather than returned to the OS", exc)
        return lambda: None


_malloc_trim = _load_malloc_trim()


class CameraEntry:
    __slots__ = ("slug", "url", "domains", "sampler")

    def __init__(self, slug: str, url: str, domains: tuple[str, ...]) -> None:
        self.slug = slug
        self.url = url
        self.domains = domains
        self.sampler = None

    def to_dict(self, broker: Optional[dict], models_ready: bool = True,
                activities: Optional[dict] = None) -> dict:
        d = {"name": self.slug, "domains": list(self.domains),
             # What the CPU Activity Engine runs for this camera, and the
             # fingerprint camera-mgmt compares with the config it stored.
             "activities": activities or {"fingerprint": None, "configured": [],
                                          "running": []}}
        if self.sampler is not None:
            d.update(self.sampler.snapshot())
            d["frame_source"] = "sampler"
        elif broker is not None:
            # BROKER MODE HAS NO SAMPLER. Without this branch every camera
            # reports WARMING_UP forever while frames are flowing normally.
            # The frame source is process-wide, so per-camera state is the
            # subscription's state.
            d.update({
                "state": "SAMPLING" if broker.get("state") == "SUBSCRIBED"
                         else "CONNECTING",
                "frames_sampled": broker.get("frames_sampled", 0),
                "last_frame_at": broker.get("last_frame_at"),
                "last_error": broker.get("last_error"),
                "frame_source": "broker",
            })
        else:
            d.update({"state": WARMING_UP, "frames_sampled": 0})
        # THE FRAMES ARE FLOWING BUT NOTHING IS LOOKING AT THEM. Reporting
        # SAMPLING through a warm-up would tell an operator this camera is
        # being analysed while its frames are being dropped on the floor.
        if not models_ready:
            d["state"] = WARMING_UP
        return d


class AnalyticsEngine:
    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        # Re-entrant: the loader installs models under the lock and then asks
        # whether another pass is due, and both sides already hold it.
        self._lock = threading.RLock()
        self._cameras: dict[str, CameraEntry] = {}
        self._pipeline: Optional[DetectionPipeline] = None
        self._detector = None
        self._plates = None
        #: True when a plate-reader load ATTEMPT concluded with no reader while
        #: configuration said one should exist. Stops the loader respawning
        #: against an egress-blocked download for ever.
        self._plates_load_failed = False
        #: The face reader, and whether a load ATTEMPT concluded without one.
        #: Same three-state shape as plates: absent because nobody asked,
        #: absent because it could not be had, or present.
        self._faces = None
        self._faces_load_failed = False
        self._faces_demand = False
        #: Last observed plate demand. Kept so the failure flag above is
        #: cleared on the TRANSITION into demand and not on every re-assert:
        #: camera-mgmt re-asserts each camera every reconcile pass, so clearing
        #: it on demand alone would retry a failing download once a minute for
        #: as long as the appliance ran.
        self._plates_demand = False
        #: Owns the detector-backend decision and its failure budget. Built
        #: ONCE, on the engine rather than inside a warm-up, so the attempt
        #: count survives a hibernation: a backend that could not be built an
        #: hour ago is not more likely to build now, and retrying it on every
        #: wake would spend an export attempt each time a quiet site comes back.
        self._selector = None
        self._state = DISABLED if not config.detect.enabled else IDLE
        self._error: Optional[str] = None
        self._sink = None

        self._idle_since: Optional[float] = time.monotonic()
        self._plates_idle_since: Optional[float] = None
        self._retry_at = 0.0
        self._loader: Optional[threading.Thread] = None
        self._lifetime = {k: 0 for k in _LIFETIME_KEYS}
        self._hibernations = 0
        self._warmups = 0
        #: Frames that arrived while no pipeline was loaded. Not a fault — it
        #: is what a warm-up and a hibernated service both look like — but an
        #: operator asking "why is nothing being detected" needs to see it.
        self._frames_dropped_no_pipeline = 0

        # ── the CPU activity path ───────────────────────────────────────────
        # OPTION 1: its own detector and its own pipeline, fed every frame of
        # an activity camera with no motion gate (analytics/activity_pipeline.py).
        # Loaded when some camera has a running activity, released on the same
        # debounce as the other models when none has.
        self._activity_detector = None
        self._activity_pipeline = None
        self._activity_idle_since: Optional[float] = None
        self._activity_frames_dropped = 0

        # The sink is built here, not injected, so its lifetime matches the
        # engine's. An empty URL leaves it None, which is phase A: detect,
        # decide, count, send nothing.
        if config.sink.url:
            from .sink import ObservationSink
            self._sink = ObservationSink(config.sink.url,
                                         timeout=config.sink.timeout_seconds)
            # What the heartbeat carries. Set before start() so the first beat
            # is already informative rather than an empty ping.
            self._sink.set_stats_source(self._producer_stats)
            self._sink.start()
        else:
            log.info("no sink configured - observations are counted and dropped")

        # THE CPU ACTIVITY ENGINE OUTLIVES EVERY PIPELINE. Its configuration is
        # the cameras' — pushed with them by camera-mgmt — so it lives here,
        # beside the camera set, and survives a hibernation that releases the
        # pipeline and its models.
        from .activities.engine import ActivityEngine
        self._event_sink = None
        if config.events.url:
            from .activities.events import ActivityEventSink
            ev = config.events
            self._event_sink = ActivityEventSink(
                ev.url, ev.internal_api_key, timeout=ev.timeout_seconds,
                queue_size=ev.queue_size, batch_size=ev.batch_size)
            self._event_sink.start()
        else:
            log.info("no events URL configured - activity events are counted "
                     "and dropped")
        self._activities = ActivityEngine(emit=self._event_sink)

        self._broker = None
        if config.source.frame_source == "broker":
            from .frame_source import BrokerSubscriber
            self._broker = BrokerSubscriber(
                config.source.broker_url,
                on_frame=self._deliver,
                channel_prefix=config.source.broker_channel_prefix,
                on_discontinuity=self._on_discontinuity,
                on_gated=self._on_gated,
                on_activity_frame=self._deliver_activity,
            )
            self._broker.start()
            log.info("frame source: broker at %s", config.source.broker_url)
        else:
            log.info("frame source: own sampler at %g fps",
                     config.source.max_sample_fps)

        # ── the clock ───────────────────────────────────────────────────────
        self._stop_event = threading.Event()
        self._reaper: Optional[threading.Thread] = None
        if config.detect.enabled and self._idle_timeout > 0:
            self._reaper = threading.Thread(target=self._reap,
                                            name="model-reaper", daemon=True)
            self._reaper.start()
            log.info("model hibernation on: weights released after %.0fs with "
                     "no camera", self._idle_timeout)
        elif config.detect.enabled:
            log.info("model hibernation OFF (lifecycle.idle_timeout_seconds "
                     "<= 0) - weights load on the first camera and are held")

    @property
    def _idle_timeout(self) -> float:
        return self._cfg.lifecycle.idle_timeout_seconds

    # ── the sink ────────────────────────────────────────────────────────────
    def attach_sink(self, sink) -> None:
        """Where accepted observations go. None (phase A) means they are
        counted and dropped, which is what makes this service safe to run
        beside the pipeline it will eventually replace."""
        self._sink = sink

    def _producer_stats(self) -> dict:
        """A snapshot small enough to send every 30 s and useful enough to be
        worth reading on the receiver's /health: is it watching anything, is
        it finding anything, is it losing anything."""
        p = self._pipeline
        return {
            "state": self._state,
            "cameras": len(self._cameras),
            "frames_processed": self._lifetime_value("frames_processed", p),
            "detections_seen": self._lifetime_value("detections_seen", p),
            "observations_produced": self._lifetime_value(
                "observations_produced", p),
            "last_error": self._error,
        }

    def _lifetime_value(self, key: str, pipeline) -> int:
        """A counter's value across every pipeline this process has had.

        WITHOUT THIS A HIBERNATION LOOKS LIKE A RESTART. The receiver watches
        these to decide whether the producer is alive and finding things; a
        counter that walks backwards each time a site goes quiet overnight is
        indistinguishable from a crash loop.
        """
        base = self._lifetime.get(key, 0)
        return base + int(getattr(pipeline, key, 0) or 0)

    # ── cameras ─────────────────────────────────────────────────────────────
    def add_camera(self, slug: str, url: str,
                   domains: tuple[str, ...] = ("person", "vehicles"),
                   analytics_config: Optional[dict] = None) -> dict:
        """Register (or re-assert) a camera. NEVER BLOCKS ON A MODEL LOAD.

        Re-assertion is the normal case, not the exception: camera-mgmt's
        reconcile heals a domain drift by calling this again rather than
        removing and re-adding, precisely so the count does not touch zero.
        """
        with self._lock:
            entry = self._cameras.get(slug)
            if entry is None:
                entry = CameraEntry(slug, url, domains)
                self._cameras[slug] = entry
            else:
                entry.url, entry.domains = url, tuple(domains)
            # A camera is here, so the idle clock is not running — whatever it
            # said a moment ago.
            self._idle_since = None
            # None = the registry sent no activity configuration (an older
            # camera-mgmt): keep what is there. {} = the camera has none.
            if analytics_config is not None:
                self._activities.configure(slug, analytics_config)
            pipeline = self._pipeline
            if pipeline is not None:
                pipeline.set_domains(slug, tuple(domains))
            self._note_plate_demand()
            self._note_activity_demand()
            self._note_face_demand()
            self._ensure_models()
            every_frame = self._activity_camera(slug)
            snap = entry.to_dict(self._broker.snapshot() if self._broker else None,
                                 models_ready=pipeline is not None,
                                 activities=self._activities.camera_summary(slug))

        if self._broker is not None:
            # Smart Search's motion-gated frames only for a camera that
            # contributes to it; EVERY frame for one with a running activity.
            if domains:
                self._broker.want(slug)
            else:
                self._broker.unwant(slug)
            if every_frame:
                self._broker.want_every_frame(slug)
            else:
                self._broker.unwant_every_frame(slug)
        else:
            self._attach_sampler(slug)
        return snap

    def remove_camera(self, slug: str) -> bool:
        with self._lock:
            entry = self._cameras.pop(slug, None)
            if entry is None:
                return False
            self._activities.forget(slug)
            if self._activity_pipeline is not None:
                self._activity_pipeline.forget(slug)
            if self._pipeline is not None:
                self._pipeline.forget_domains(slug)
                self._pipeline.reset_gate(slug)
            self._note_plate_demand()
            self._note_activity_demand()
            self._note_face_demand()
            if not self._cameras and self._idle_since is None:
                # THE CLOCK STARTS HERE, THE RELEASE HAPPENS IN THE REAPER.
                # Unloading on the transition itself would thrash on ordinary
                # registry traffic — see LifecycleConfig.
                self._idle_since = time.monotonic()
                if self._pipeline is not None and self._idle_timeout > 0:
                    log.info("last camera unregistered — releasing detection "
                             "models in %.0fs unless one comes back",
                             self._idle_timeout)
            sampler = entry.sampler

        # Outside the lock: stopping a sampler joins a capture thread, which
        # can take seconds and must not be done holding a lock the reaper wants.
        if self._broker is not None:
            self._broker.unwant(slug)
            self._broker.unwant_every_frame(slug)
        if sampler is not None:
            sampler.stop()
        return True

    def list_cameras(self) -> list[dict]:
        snap = self._broker.snapshot() if self._broker else None
        with self._lock:
            ready = self._pipeline is not None
            return [e.to_dict(snap, ready, self._activities.camera_summary(e.slug))
                    for e in self._cameras.values()]

    def get_camera(self, slug: str) -> Optional[dict]:
        snap = self._broker.snapshot() if self._broker else None
        with self._lock:
            e = self._cameras.get(slug)
            ready = self._pipeline is not None
        return (e.to_dict(snap, ready, self._activities.camera_summary(slug))
                if e else None)

    # ── frame delivery ──────────────────────────────────────────────────────
    def _deliver(self, slug: str, frame: np.ndarray, ts: float,
                 motion: Optional[dict]) -> None:
        p = self._pipeline
        if p is None:
            # Warming up, or hibernated with a camera that has just come back.
            # Counted rather than silently discarded.
            self._frames_dropped_no_pipeline += 1
            return
        p.submit(slug, frame, ts, motion)

    def _activity_camera(self, slug: str) -> bool:
        """Does this camera have an activity the CPU engine will run?"""
        return bool(self._cfg.activities.enabled and self._activities.has_work(slug))

    def _deliver_activity(self, slug: str, frame: np.ndarray, ts: float) -> None:
        """Every frame of an activity camera — the broker does not gate these."""
        p = self._activity_pipeline
        if p is None:
            self._activity_frames_dropped += 1
            return
        p.submit(slug, frame, ts)

    def _deliver_sampled(self, slug: str, frame: np.ndarray, ts: float) -> None:
        """Sampler mode (no broker): Smart Search's pipeline gates the frame
        itself; the activity path takes every frame."""
        entry = self._cameras.get(slug)
        if entry is not None and entry.domains:
            self._deliver(slug, frame, ts, None)
        if self._activity_camera(slug):
            self._deliver_activity(slug, frame, ts)

    def activity_definitions(self) -> list[dict]:
        return self._activities.definitions()

    def _on_discontinuity(self, slug: str) -> None:
        """The broker reconnected, so its motion baseline is a new scene.
        Reset per-camera state: frames either side of an outage are separated
        by however long it lasted, and associating across that gap is
        guesswork."""
        p = self._pipeline
        if p is not None:
            p.reset_gate(slug)
        ap = self._activity_pipeline
        if ap is not None:
            ap.reset(slug)

    def _on_gated(self, slug: str) -> None:
        p = self._pipeline
        if p is not None:
            p.note_gated_before_read(slug)

    # ── plate demand ────────────────────────────────────────────────────────
    def _plates_wanted(self) -> bool:
        """Caller holds the lock. Does ANY registered camera ask for ANPR?"""
        return any(PLATE_DOMAIN in e.domains for e in self._cameras.values())

    def _note_plate_demand(self) -> None:
        """Caller holds the lock. Start or stop the plate reader's own clock.

        A SEPARATE CLOCK FROM THE DETECTOR'S, because the two answer different
        questions. Every camera needs the detector, so its clock is "are there
        any cameras". Only `plate` cameras need the reader, so a site that runs
        ANPR on one gate and plain detection everywhere else releases the OCR
        model when that one gate's ANPR is switched off, without disturbing the
        detection the other cameras are still paying for.
        """
        wanted = self._plates_wanted()
        if wanted and not self._plates_demand:
            # Demand has just APPEARED — an operator switching ANPR back on is
            # a good reason to try a download that failed before. Demand that
            # was already there is not, or every reconcile pass would retry.
            self._plates_load_failed = False
        if wanted:
            self._plates_idle_since = None
        elif self._plates is not None and self._plates_idle_since is None:
            self._plates_idle_since = time.monotonic()
            if self._idle_timeout > 0:
                log.info("no camera asks for plates — releasing the plate "
                         "reader in %.0fs unless one does", self._idle_timeout)
        self._plates_demand = wanted

    def _release_plates(self, why: str) -> None:
        """Caller holds the lock. Reclaims in the CALLER — see _reclaim."""
        if self._plates is None:
            return
        self._plates = None
        self._plates_idle_since = None
        if self._pipeline is not None:
            self._pipeline.set_plate_reader(None)
        log.info("plate reader released (%s)", why)

    # ── activity demand ─────────────────────────────────────────────────────
    def _activity_wanted(self) -> bool:
        """Caller holds the lock. Does any camera have a running activity?"""
        return bool(self._cfg.activities.enabled) and any(
            self._activities.has_work(slug) for slug in self._cameras)

    def _note_activity_demand(self) -> None:
        """Caller holds the lock. Start or stop the activity detector's clock."""
        if self._activity_wanted():
            self._activity_idle_since = None
        elif self._activity_pipeline is not None and self._activity_idle_since is None:
            self._activity_idle_since = time.monotonic()

    def _detach_activity(self):
        """Caller holds the lock. The CALLER stops the returned pipeline."""
        pipeline = self._activity_pipeline
        self._activity_pipeline = None
        self._activity_detector = None
        self._activity_idle_since = None
        return pipeline

    # ── face demand ─────────────────────────────────────────────────────────
    def _faces_wanted(self) -> bool:
        """Caller holds the lock. Does ANY registered camera ask for faces?"""
        return any(FACE_DOMAIN in e.domains for e in self._cameras.values())

    def _note_face_demand(self) -> None:
        """Caller holds the lock. Mirrors _note_plate_demand.

        Its own flag, not the plate reader's: a site can run ANPR at a gate and
        faces at a door, and switching one off must not release the other's
        model. Sharing one demand flag is how the cheap thing ends up paying
        the expensive thing's price — the same trap the relay-vs-record split
        in the NVR exists to avoid.
        """
        wanted = self._faces_wanted()
        if wanted and not self._faces_demand:
            # Demand has just appeared: an operator switching the domain on is
            # a real reason to retry a load that failed before.
            self._faces_load_failed = False
        elif not wanted and self._faces is not None:
            # RELEASED AT ONCE, where plates get an idle countdown. The reason
            # is the cost of being wrong: reloading these two files takes a
            # moment and holding them keeps ~40 MB plus a live biometric model
            # on a service no camera has asked to do face work. For a plate
            # reader the same call is a download, which is why that one waits.
            self._release_faces("no camera asks for faces")
        self._faces_demand = wanted

    def _release_faces(self, why: str) -> None:
        """Caller holds the lock. Reclaims in the CALLER — see _reclaim."""
        if self._faces is None:
            return
        self._faces = None
        if self._pipeline is not None:
            self._pipeline.set_face_reader(None)
        log.info("face reader released (%s)", why)

    # ── models ──────────────────────────────────────────────────────────────
    def _needs_warmup(self) -> bool:
        """Caller holds the lock. Is there a model we should have and don't?"""
        if not self._cfg.detect.enabled or not self._cameras:
            return False
        if self._pipeline is None or self._detector is None:
            return True
        if self._activity_wanted() and self._activity_pipeline is None:
            return True
        if (self._cfg.plates.enabled and self._plates_wanted()
                and self._plates is None and not self._plates_load_failed):
            return True
        return bool(self._cfg.faces.enabled and self._faces_wanted()
                    and self._faces is None and not self._faces_load_failed)

    def _ensure_models(self) -> None:
        """Caller holds the lock. Start a warm-up if one is due and none runs.

        RETURNS NOTHING AND WAITS FOR NOTHING. Everything that used to depend
        on a pipeline being back from this call now tolerates None — see
        _deliver and to_dict — because the alternative is a 41-second POST.
        """
        if self._stop_event.is_set() or not self._needs_warmup():
            return
        if self._loader is not None and self._loader.is_alive():
            return
        if self._state == FAILED and time.monotonic() < self._retry_at:
            return
        self._state = WARMING_UP
        self._loader = threading.Thread(target=self._warm,
                                        name="analytics-warmup", daemon=True)
        self._loader.start()

    def _warm(self) -> None:
        """Build whatever is missing, OFF THE LOCK, then install it under it."""
        cfg = self._cfg
        with self._lock:
            need_detector = self._detector is None or self._pipeline is None
            want_plates = (cfg.plates.enabled and self._plates_wanted()
                           and self._plates is None)
            need_activity = self._activity_wanted() and self._activity_pipeline is None
            want_faces = (cfg.faces.enabled and self._faces_wanted()
                          and self._faces is None)
            if self._selector is None:
                from .selection import DetectorSelector
                self._selector = DetectorSelector(cfg.models)
            selector = self._selector
            detector = self._detector

        plates = None
        activity_detector = None
        faces = None
        try:
            if need_detector:
                from .detector import build_selected
                m = cfg.models
                # Prefers OpenVINO on CPU and falls back to torch WITHIN this
                # call, so a failed optimisation never leaves this service
                # without a detector.
                detector = build_selected(
                    selector, m.detector_weights, cfg.detect.confidence,
                    m.device, m.square_letterbox,
                )
            if want_plates:
                from .plates import build as build_plate_reader
                # Never fatal: no plate reader costs plate reading, not
                # detection.
                plates = build_plate_reader(cfg.plates, cfg.models.device)
            if need_activity:
                from .detector import build_selected
                m = cfg.models
                # OPTION 1: the activity path's OWN detector instance, from the
                # same selector — OpenVINO on CPU, same weights and confidence.
                activity_detector = build_selected(
                    selector, m.detector_weights, cfg.detect.confidence,
                    m.device, m.square_letterbox,
                )
            if want_faces:
                from .faces import build as build_face_reader
                # Same policy: a missing face model costs the face domain, and
                # every other domain on that camera keeps working.
                faces = build_face_reader(cfg.faces, cfg.models.device)
        except Exception as exc:                                   # noqa: BLE001
            with self._lock:
                self._state = FAILED
                self._error = f"{type(exc).__name__}: {exc}"
                self._retry_at = (time.monotonic()
                                  + max(1.0, cfg.lifecycle.retry_seconds))
                self._loader = None
            log.exception("could not build the detection models")
            return

        with self._lock:
            if self._stop_event.is_set():
                # The service is shutting down and stop() has already run its
                # teardown. Installing here would start a worker thread nothing
                # will ever stop and hand it models nothing will ever release.
                self._loader = None
                return
            if want_plates and plates is None:
                # Configuration says there should be one and there is not.
                # Recorded so the loader does not respawn against an
                # egress-blocked download on every tick — and so /health can
                # tell "nobody asked" from "it could not be had".
                self._plates_load_failed = True
            if want_faces and faces is None:
                # Configuration asked and the models are not there. Recorded so
                # the loader does not respawn on every tick, and so /health can
                # tell "nobody asked" from "it could not be had".
                self._faces_load_failed = True
            if faces is not None:
                self._faces = faces
                self._note_face_demand()
            if plates is not None:
                self._plates = plates
                # ARM THE CLOCK FROM CURRENT DEMAND, not from the fact that a
                # load just finished. The build takes seconds and ANPR can be
                # switched off during them; stamping "not idle" here installs a
                # reader nobody wants with nothing left to start its countdown,
                # because only add_camera and remove_camera re-arm it and a
                # stable fleet sends neither.
                self._note_plate_demand()
            if detector is not None:
                self._detector = detector
            if self._pipeline is None and self._detector is not None:
                self._pipeline = DetectionPipeline(
                    cfg, self._detector, plate_reader=self._plates,
                    face_reader=self._faces, sink=self._sink,
                )
                # Cameras that arrived during the warm-up have been waiting for
                # exactly this. Their domains were never applied, so without
                # this loop every one of them would fall back to the
                # ("person", "vehicles") default in domains_for.
                for slug, entry in self._cameras.items():
                    self._pipeline.set_domains(slug, entry.domains)
                self._pipeline.start()
                self._warmups += 1
            elif self._pipeline is not None and plates is not None:
                self._pipeline.set_plate_reader(plates)
            if self._pipeline is not None and faces is not None:
                # NOT an elif. A pass that built only the face reader must still
                # install it, and a pass that built both must install both —
                # chaining this onto the plates branch would silently drop one.
                self._pipeline.set_face_reader(faces)
            if activity_detector is not None and self._activity_pipeline is None:
                from .activity_pipeline import ActivityPipeline
                self._activity_detector = activity_detector
                self._activity_pipeline = ActivityPipeline(cfg, activity_detector,
                                                           self._activities)
                self._activity_pipeline.start()
                self._note_activity_demand()
                log.info("activity pipeline up (detector=%s) — every frame, "
                         "no motion gate", type(activity_detector).__name__)
            if self._pipeline is not None:
                self._state = RUNNING
                self._error = None
            self._loader = None
            pending = list(self._cameras) if self._broker is None else []
            if need_detector and self._pipeline is not None:
                log.info("detection pipeline up (detector=%s plates=%s)",
                         type(self._detector).__name__,
                         "yes" if self._plates else "no")
            elif plates is not None:
                # A plates-only pass: the detector was already running, so
                # saying "pipeline up" here would misreport a warm start.
                log.info("plate reader attached to the running pipeline")

        # Sampler mode only, and outside the lock: starting a sampler opens an
        # RTSP session.
        for slug in pending:
            self._attach_sampler(slug)

    def _attach_sampler(self, slug: str) -> None:
        """Sampler mode: give this camera a capture thread, once."""
        with self._lock:
            entry = self._cameras.get(slug)
            pipeline = self._pipeline
            if entry is None or pipeline is None or entry.sampler is not None:
                return
            from .sampler import CameraSampler
            # BOTH CALLBACKS GO THROUGH THE ENGINE, NOT THE PIPELINE. Binding
            # `pipeline.submit` and `pipeline.reset_gate` directly would put a
            # strong reference to this pipeline inside a capture thread that
            # outlives it, so a hibernation would free nothing and every frame
            # after the next warm-up would be fed to the stopped worker.
            entry.sampler = CameraSampler(
                slug, entry.url, self._cfg.source.max_sample_fps,
                on_frame=lambda s, f, t: self._deliver_sampled(s, f, t),
                on_reconnect=self._on_discontinuity,
            )
            sampler = entry.sampler
        sampler.start()

    def _detach(self) -> Optional[DetectionPipeline]:
        """Caller holds the lock. Drop every engine reference to the models and
        hand the pipeline back so the CALLER can stop it and release the last
        reference outside the lock.

        DELIBERATELY DOES NOT RECLAIM — see _reclaim for why that would be an
        expensive no-op here.
        """
        pipeline = self._pipeline
        self._pipeline = None
        self._detector = None
        self._plates = None
        self._plates_idle_since = None
        self._plates_load_failed = False
        if self._state in (RUNNING, WARMING_UP, FAILED):
            self._state = IDLE
        self._error = None
        return pipeline

    @staticmethod
    def _reclaim() -> None:
        """Give the memory back rather than merely dropping the reference.

        THREE STEPS, AND DROPPING THE REFERENCE IS ONLY THE FIRST. glibc keeps
        freed arenas for reuse rather than returning them, so without the trim
        much of the released memory stays charged to the process and an
        operator reasonably concludes nothing was freed. What cannot be given
        back is the import floor: torch and ultralytics map shared libraries
        that live as long as the process, which is the honest ceiling on
        hibernation and the reason "stop the container" is the only thing that
        beats it.

        On a GPU box the third step is the one that matters: torch's caching
        allocator holds device memory until it is told not to, so `del model`
        alone frees nothing visible in nvidia-smi.
        """
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:                                          # noqa: BLE001
            pass
        _malloc_trim()

    # ── the clock ───────────────────────────────────────────────────────────
    def _reap(self) -> None:
        poll = max(1.0, self._cfg.lifecycle.poll_seconds)
        while not self._stop_event.wait(poll):
            try:
                self._tick()
            except Exception:                                      # noqa: BLE001
                log.exception("model reaper tick failed")

    def _tick(self) -> None:
        now = time.monotonic()
        timeout = self._idle_timeout
        pipeline: Optional[DetectionPipeline] = None
        activity_pipeline = None
        dropped_plates = False

        with self._lock:
            if self._cameras:
                self._idle_since = None
                # A warm-up that failed, or a plate reader that became wanted
                # while one was already running, is picked up here.
                self._ensure_models()
                # RE-DERIVED EVERY PASS, not just on a camera event. This is the
                # backstop that makes the plate clock self-correcting: whatever
                # left it out of step with demand — a load that landed after its
                # demand vanished, a future caller that forgets — is repaired on
                # the next tick rather than leaking a model until a camera
                # happens to change.
                self._note_plate_demand()
                # Same backstop for the face reader. It has no idle clock — it
                # is released the moment demand goes — so this pass is what
                # catches a release that should have happened and did not.
                self._note_face_demand()
                if (self._plates is not None and not self._plates_wanted()
                        and self._plates_idle_since is not None
                        and now - self._plates_idle_since >= timeout):
                    self._release_plates(
                        f"no camera has asked for plates for {timeout:.0f}s")
                    dropped_plates = True
                self._note_activity_demand()
                if (self._activity_pipeline is not None and not self._activity_wanted()
                        and self._activity_idle_since is not None
                        and now - self._activity_idle_since >= timeout):
                    activity_pipeline = self._detach_activity()
            else:
                if self._idle_since is None:
                    self._idle_since = now
                elif now - self._idle_since >= timeout:
                    if self._pipeline is not None:
                        pipeline = self._detach()
                    if self._activity_pipeline is not None:
                        activity_pipeline = self._detach_activity()

        if activity_pipeline is not None:
            activity_pipeline.stop()
            activity_pipeline = None
            log.info("activity detector released (no running activity for %.0fs)", timeout)
            dropped_plates = True        # i.e. something was released: reclaim below

        if pipeline is not None:
            # Stop the worker BEFORE folding its counters, so nothing is still
            # incrementing them, and before the reference goes.
            pipeline.stop()
            with self._lock:
                for k in _LIFETIME_KEYS:
                    self._lifetime[k] += int(getattr(pipeline, k, 0) or 0)
                self._hibernations += 1
            pipeline = None          # the last reference — before _reclaim
            self._reclaim()
            log.info("detection models released (no cameras for %.0fs)", timeout)
        elif dropped_plates:
            self._reclaim()

    def stop(self) -> None:
        self._stop_event.set()
        if self._broker is not None:
            self._broker.stop()
        if self._sink is not None:
            self._sink.stop()
        if self._event_sink is not None:
            self._event_sink.stop()
        with self._lock:
            entries = list(self._cameras.values())
            pipeline = self._detach()
            activity_pipeline = self._detach_activity()
        for e in entries:
            if e.sampler is not None:
                e.sampler.stop()
        if pipeline is not None:
            pipeline.stop()
        if activity_pipeline is not None:
            activity_pipeline.stop()
        activity_pipeline = None
        pipeline = None              # the last reference — before _reclaim
        self._reclaim()

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        p = self._pipeline
        with self._lock:
            plates_wanted = self._plates_wanted()
            idle_since = self._idle_since
            plates_idle_since = self._plates_idle_since
            lifetime = {k: self._lifetime_value(k, p) for k in _LIFETIME_KEYS}
        d = {
            "status": "ok",
            "state": self._state,
            "last_error": self._error,
            "total_cameras": len(self._cameras),
            "frame_source": (self._broker.snapshot() if self._broker
                             else {"source": "sampler"}),
            # PHASE A SAYS SO OUT LOUD. With no sink, everything this service
            # decides is counted and then dropped; reporting it as if it were
            # feeding an index would be the misleading half of a safe rollout.
            "sink": (self._sink.snapshot() if self._sink is not None
                     else {"configured": False, "url": None}),
            "models": {
                "detector": type(self._detector).__name__ if self._detector else None,
                "detector_loaded": self._detector is not None,
                # THREE DIFFERENT ANSWERS, AND CONFLATING THEM IS THE BUG THIS
                # SPLIT EXISTS TO AVOID. `plates_wanted` is whether any camera
                # asked; `plates_loaded` is whether a reader is resident right
                # now; `plates_active` is whether that reader can actually
                # read. "Nobody asked" and "the download failed" are completely
                # different things to tell someone whose plate search came back
                # empty — see tests/test_plate_health.py.
                "plates_wanted": plates_wanted,
                "plates_loaded": self._plates is not None,
                "plates_active": bool(getattr(self._plates, "active", False)),
                "plates_load_failed": self._plates_load_failed,
                # The activity path's own instance (Option 1).
                "activity_detector": (type(self._activity_detector).__name__
                                      if self._activity_detector else None),
                "activity_detector_loaded": self._activity_detector is not None,
            },
            "lifecycle": {
                "hibernation": {
                    "enabled": self._idle_timeout > 0,
                    "idle_timeout_seconds": self._idle_timeout,
                    "idle_for_seconds": (
                        None if idle_since is None
                        else round(time.monotonic() - idle_since, 1)),
                    # THE PLATE READER'S OWN CLOCK, reported separately because
                    # it answers a different question. With cameras registered
                    # the detector's clock is null and only this one is
                    # running, so without it "ANPR is off everywhere but the
                    # reader is still loaded" has no visible explanation.
                    "plates_idle_for_seconds": (
                        None if plates_idle_since is None
                        else round(time.monotonic() - plates_idle_since, 1)),
                    "hibernations": self._hibernations,
                    "warmups": self._warmups,
                },
                # Counters folded across every pipeline this process has had,
                # so a night of hibernation does not read as a restart.
                "lifetime": lifetime,
                "frames_dropped_no_pipeline": self._frames_dropped_no_pipeline,
                "activity_frames_dropped_no_pipeline": self._activity_frames_dropped,
            },
        }
        # The CPU Activity Engine and where its events go. Reported even with
        # no pipeline loaded: configuration is held across hibernation.
        d["activities"] = self._activities.snapshot()
        d["activity_events"] = (self._event_sink.snapshot()
                                if self._event_sink is not None
                                else {"configured": False, "url": None})
        ap = self._activity_pipeline
        d["activity_pipeline"] = (ap.snapshot() if ap is not None
                                  else {"state": "IDLE", "motion_gated": False})
        d["pipeline"] = p.snapshot() if p is not None else {"state": self._state}
        return d
