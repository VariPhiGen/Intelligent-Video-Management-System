"""models.py — the heavy artefacts, loaded on demand and released when idle.

THE PROCESS IS NOT THE THING WITH A LIFECYCLE; THE WEIGHTS ARE. This service
used to load the detector, the encoder and the plate reader before the port
opened and hold them for as long as it ran, whether or not a single camera was
being indexed. Measured end-to-end on the reference appliance, 7 cameras, CPU
build:

    booted, no cameras yet          ~0%     52 MB
    indexing 7 cameras              ~60%  2.07 GB
    Smart Search off on every camera ~0.1% 1.10 GB

The CPU saving is total. The memory saving is partial and the floor is one-way:
importing torch, ultralytics, onnxruntime and OpenCV maps libraries and arenas
that outlive any model, so a process that has loaded once never returns to its
52 MB boot figure. Only restarting the container gets that back.

An operator who turns Smart Search off on every camera has said, unambiguously,
that they do not want this work done. Honouring that by stopping the container
would be the obvious reading and the wrong one: ingest and query live in the
same process, so stopping it also makes ALREADY-INDEXED footage unsearchable,
and something privileged then has to watch the database to notice a camera
being re-enabled. Releasing the weights instead costs the same memory, keeps
history searchable, and needs no privilege at all. It is the same shape as
Ollama's keep-alive and Triton's explicit model control.

FOUR RULES, each of which cost something to learn:

1. **Release is on a timer, never on the transition itself.** `upsert_camera`
   removes and re-adds when a URL changes, so the camera count legitimately
   touches zero mid-update, and camera-mgmt reconciles from four uvicorn
   workers at once. Unloading the instant the last camera goes would thrash on
   both. The timer (default 300 s) makes a transient zero a non-event.

2. **Warm-up never blocks the caller.** The registry's POST /cameras has a 10 s
   timeout and the FIRST warm-up downloads ~600 MB of weights. Loading inline
   would time out the sync, which would retry, which would warm again. The load
   runs on its own thread; cameras registered meanwhile sit in WARMING and
   have their samplers attached when it finishes.

3. **/health must never claim ingest is running while it is warming.** That was
   the original reason models loaded before the port opened, and it survives
   here as a state machine rather than as ordering: `lifecycle.ingest.state` is
   one of disabled/idle/warming/ready/failed/unavailable, and `capabilities`
   answers the different question of whether ingest will happen at all.

4. **The encoder is shared with the query side, so it has its own clock.** The
   detector and plate reader are ingest-only and go when the cameras go. The
   encoder is what turns search text into a vector, so it is released only when
   nothing has been INDEXED and nothing has been SEARCHED for the timeout.
   Warm reload is ~1.7 s (measured), well inside the VMS's 30 s client timeout.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import threading
import time
from typing import Callable, Optional

from .config import AppConfig
from .pipeline import IngestPipeline
from .store import Store
from .writer import CropWriter

log = logging.getLogger("smartsearch.models")

#: Vector width the schema is built for. A model of any other width does not
#: produce comparable vectors, so this is a permanent failure and not a retry:
#: see migrations/001_init.sql, changing it is a re-index, not a migration.
EMBED_DIM = 512

# Load states. Strings on purpose — they go straight into /health.
DISABLED = "disabled"          # ingest.enabled = false; registry only
IDLE = "idle"                  # nothing loaded, nothing wanted
WARMING = "warming"            # load in progress
READY = "ready"
FAILED = "failed"              # transient; retried with backoff
UNAVAILABLE = "unavailable"    # permanent; retrying cannot help

_RETRY_BASE = 30.0
_RETRY_MAX = 600.0

#: Counters folded forward across hibernations, so /health does not appear to
#: lose rows every time the service goes quiet.
_LIFETIME_KEYS = (
    "frames_queued", "frames_dropped", "frames_processed", "detections_seen",
    "crops_deduped", "rows_written", "frames_skipped_no_motion", "detector_calls",
    # Per-stage call counts. Folded forward like the rest so a workload
    # coefficient is measured over the deployment's life rather than since the
    # last wake — a service that hibernates nightly would otherwise re-derive
    # its own workload from a few minutes of evidence every morning.
    "crops_embedded", "localiser_calls",
    "crops_suppressed_by_policy",
    # Tracking and indexing counters, folded forward for the same reason as the
    # rest: a suppression ratio measured since the last wake is measured over a
    # few minutes on an appliance that hibernates nightly.
    "detections_tentative", "observations_indexed", "observations_suppressed",
    "tracks_created", "tracks_confirmed", "tracks_expired",
)


def _load_malloc_trim():
    """glibc's malloc_trim, or a no-op where there isn't one.

    Resolved once: this is called on every release, and musl (Alpine) has no
    such symbol at all — a missing trim costs memory, never correctness.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fn = libc.malloc_trim
        fn.argtypes = [ctypes.c_size_t]
        fn.restype = ctypes.c_int
        return lambda: fn(0)
    except (OSError, AttributeError) as exc:                       # noqa: BLE001
        log.info("malloc_trim unavailable (%s); released memory will be reused "
                 "by this process rather than returned to the OS", exc)
        return lambda: None


_malloc_trim = _load_malloc_trim()


class ModelPool:
    """Owns the detector, the encoder, the plate reader and the ingest pipeline.

    Nothing else constructs them. The engine asks for a pipeline and is handed
    one — or None, which it must treat as "not yet", never as "broken".
    """

    def __init__(self, config: AppConfig, store: Store, writer: CropWriter) -> None:
        self._cfg = config
        self._store = store
        self._writer = writer

        # Re-entrant: _spawn_loader is reached both from want_ingest and from
        # the reaper's retry path, and both already hold it.
        self._lock = threading.RLock()
        self._detector = None
        self._embedder = None
        self._plates = None
        #: The QUERY-side face encoder (YuNet + SFace). Built on first use, not
        #: at boot: a deployment with no face cameras must not pay 37 MB of
        #: SFace to answer person searches, and a deployment with them pays it
        #: once. Ingest never touches this — analytics embeds faces off the raw
        #: frame, see index/faces.py.
        self._faces = None
        self._faces_tried = False
        #: ITS OWN CLOCK, not the ingest one. The CLIP encoder stays resident
        #: while cameras are registered because ingest embeds every crop with
        #: it. NOTHING at ingest touches the face encoder — analytics embeds
        #: faces off the raw frame — so the only thing that can want this is a
        #: search, and on a live appliance `_idle_since` never starts at all.
        #: Sharing that clock pinned 37 MB for the life of the process on every
        #: box that has cameras, which is every box.
        self._last_face_query: float = 0.0
        #: True when a plate-reader load ATTEMPT concluded with no reader while
        #: configuration said one should exist — see plates_available.
        self._plates_load_failed = False
        self._pipeline: Optional[IngestPipeline] = None

        self._ingest_state = DISABLED if not config.ingest.enabled else IDLE
        self._embed_state = IDLE
        self._ingest_error: Optional[str] = None
        self._embed_error: Optional[str] = None

        #: True while at least one camera is registered. The engine owns the
        #: count; the pool only needs the boolean.
        self._wanted = False
        self._idle_since: Optional[float] = time.monotonic()
        self._last_query: float = 0.0
        self._fail_count = 0
        self._retry_at = 0.0
        self._loader: Optional[threading.Thread] = None
        self._listener: Optional[Callable[[Optional[IngestPipeline]], None]] = None
        #: Serialises listener calls, and is NOT the pool lock — see _notify.
        self._notify_lock = threading.Lock()
        self._lifetime = {k: 0 for k in _LIFETIME_KEYS}
        self._hibernations = 0
        self._warmups = 0

        #: What each component last loaded on, as plain dicts keyed by
        #: component name. RETAINED ACROSS A RELEASE on purpose: an operator
        #: checking /health on a hibernating appliance still needs to know
        #: whether inference runs on CPU or GPU, and "the models are asleep so
        #: we cannot tell you" is the wrong answer to a question about
        #: configuration. The dict is a few frozen scalars; keeping it costs
        #: nothing against the memory hibernation exists to reclaim, and the
        #: `loaded` flag beside it says whether the weights are resident now.
        self._backends: dict[str, dict] = {}

        #: Owns the detector-backend decision and its failure budget. Lives on
        #: the pool rather than inside a warm-up so the attempt count SURVIVES
        #: hibernation: a backend that could not be built an hour ago is not
        #: more likely to build now, and retrying it on every wake would spend
        #: an export attempt each time a quiet site comes back.

        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, name="model-reaper",
                                        daemon=True)

    # ── wiring ───────────────────────────────────────────────────────────────
    def set_listener(self, fn: Callable[[Optional[IngestPipeline]], None]) -> None:
        """Called with the pipeline when it appears, and with None when it goes.

        LOCK ORDER, AND IT IS LOAD-BEARING: the pool NEVER calls the engine
        while holding the pool lock, and the engine never calls the pool while
        holding the engine lock. Both directions exist — the engine drives
        want_ingest, the pool drives this listener — so without a fixed order
        the two deadlock the first time a camera is removed during a warm-up.
        The listener also joins capture threads, which can take seconds; that
        must not be done holding a lock the reaper wants.
        """
        self._listener = fn

    def start(self) -> None:
        if self._idle_timeout > 0:
            self._reaper.start()
            log.info("model hibernation on: weights released after %.0fs idle",
                     self._idle_timeout)
        else:
            log.info("model hibernation OFF (lifecycle.idle_timeout_seconds <= 0)")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            pipeline = self._detach_ingest()
        self._teardown(pipeline, "shutdown")
        pipeline = None                  # the last reference — before _reclaim
        with self._lock:
            self._release_embedder("shutdown")
        self._reclaim()

    @property
    def _idle_timeout(self) -> float:
        return self._cfg.lifecycle.idle_timeout_seconds

    # ── what the engine drives ───────────────────────────────────────────────
    def want_ingest(self, wanted: bool) -> None:
        """Told by the engine whenever the registered-camera count crosses zero."""
        with self._lock:
            if wanted == self._wanted:
                return
            self._wanted = wanted
            if wanted:
                self._idle_since = None
                log.info("first camera registered — warming ingest models")
                self._spawn_loader()
            else:
                self._idle_since = time.monotonic()
                if self._idle_timeout > 0:
                    log.info("last camera unregistered — releasing ingest models "
                             "in %.0fs unless one comes back", self._idle_timeout)

    @property
    def pipeline(self) -> Optional[IngestPipeline]:
        return self._pipeline

    @property
    def ingest_state(self) -> str:
        return self._ingest_state

    @property
    def ingest_error(self) -> Optional[str]:
        """Why ingest is not running, or None. Reported per-camera so an
        operator sees the cause next to the camera it is costing them."""
        return self._ingest_error

    @property
    def ingest_possible(self) -> bool:
        """Will a registered camera be indexed — now, or once warm? False only
        when ingest is switched off or can never work on this deployment."""
        return self._ingest_state not in (DISABLED, UNAVAILABLE)

    # ── what the query side drives ───────────────────────────────────────────
    @property
    def embedder_possible(self) -> bool:
        """Whether text search can be answered — loading first if it must."""
        return self._embed_state != UNAVAILABLE

    # ── faces (query side only) ─────────────────────────────────────────────
    def face_encoder(self):
        """The face encoder, loaded on first use. Never raises; an inactive
        encoder is the answer when the models are not installed.

        TOUCHES THE QUERY CLOCK, like any other search does. Without that the
        reaper would release the model between two face searches by an operator
        who has not stopped working — and reloading 37 MB per query is the
        trade hibernation exists to avoid.
        """
        self._last_face_query = time.monotonic()
        with self._lock:
            if self._faces is not None or self._faces_tried:
                return self._faces
            self._faces_tried = True
        # OFF THE LOCK. Loading SFace reads 37 MB from the models volume, and
        # holding the pool lock across that would stall /health for the whole
        # read — the exact defect snapshot() was made lock-free to avoid.
        from .faces import build as build_faces
        encoder = build_faces(self._cfg.faces)
        with self._lock:
            self._faces = encoder
        return encoder

    @property
    def configured_face_model(self) -> Optional[str]:
        """Which face embedder this index is running, from CONFIGURATION.

        Read without loading anything, because two callers need it on paths
        that must stay cheap: every observation is validated against it (37 MB
        of SFace must not be pulled off disk by the first POST), and every face
        query is scoped to it. Configuration is also the honest source — a row
        is scoped to the model this deployment is set up to run, whether or not
        the file happens to be loadable this minute.
        """
        if not self._cfg.faces.enabled:
            return None
        return self._cfg.faces.model or None

    @property
    def faces_available(self) -> bool:
        """Whether this deployment can answer a face query at all.

        Configuration, not a probe, for plates_available's reason: the UI has to
        distinguish "face search is not set up here" from "no matches", and an
        empty result cannot carry that difference.
        """
        if not self._cfg.faces.enabled:
            return False
        enc = self._faces
        if enc is not None:
            return bool(enc.active)
        import os
        return (os.path.isfile(self._cfg.faces.detector_weights)
                and os.path.isfile(self._cfg.faces.recogniser_weights))

    def face_state(self) -> dict:
        """What /health says about face search.

        Reports the ERROR when there is one. "No model installed" and "a model
        installed whose width the schema cannot hold" need different actions,
        and an `available: false` that cannot tell them apart sends an operator
        looking in the wrong place — the distinction device.py draws for the
        GPU and plates.py draws for the reader.
        """
        enc = self._faces
        state = {
            "enabled": bool(self._cfg.faces.enabled),
            "model": self.configured_face_model,
            "available": self.faces_available,
            "loaded": bool(enc is not None and enc.active),
        }
        if enc is not None:
            state.update(enc.snapshot())
        return state

    @property
    def plates_available(self) -> bool:
        """Whether this deployment reads plates.

        Answered from CONFIGURATION when the reader is not loaded. The UI uses
        this to say "not enabled here" instead of showing an empty result, and
        that distinction must survive hibernation: a hibernating service still
        reads plates, it is simply not reading one right now.

        Unlocked on purpose — see snapshot().
        """
        # NO LIVE READER TO ASK. Plate reading moved to the analytics
        # service, so this is a statement about how the deployment is
        # CONFIGURED, not a probe of a loaded model. Analytics reports whether
        # its reader actually loaded; what this answers is the UI's question,
        # "does this deployment do ANPR at all".
        return bool(self._cfg.plates.enabled
                    and (self._cfg.plates.localiser_weights
                         or self._cfg.plates.localiser_model))

    def acquire_embedder(self):
        """The encoder, loading it if necessary. None means it cannot be had.

        Returns a strong reference: the caller keeps the model alive for the
        duration of its query even if the reaper releases the pool's own.
        """
        self._last_query = time.monotonic()
        with self._lock:
            if self._embedder is not None:
                return self._embedder
            if self._embed_state == UNAVAILABLE:
                return None
            return self._load_embedder()

    # ── loading ──────────────────────────────────────────────────────────────
    def _load_embedder(self):
        """Caller holds the lock. Held deliberately across the ~1.7 s load: two
        threads waiting is correct, two copies of the model is not."""
        self._embed_state = WARMING
        try:
            from .embedder import ClipEmbedder
            m = self._cfg.models
            emb = ClipEmbedder(m.clip_model, m.clip_pretrained, m.device,
                               m.embed_batch_size)
        except Exception as exc:                                   # noqa: BLE001
            self._embed_state = FAILED
            self._embed_error = f"{type(exc).__name__}: {exc}"
            log.error("encoder failed to load: %s", self._embed_error)
            return None
        if emb.dim != EMBED_DIM:
            # Permanent. Vectors of another width are not comparable with what
            # is already stored, so retrying only produces a wrong answer later.
            self._embed_state = UNAVAILABLE
            self._embed_error = (
                f"model produces {emb.dim}-dim vectors but the schema is "
                f"vector({EMBED_DIM}). Changing the model is a re-index, not a "
                f"migration; see migrations/001_init.sql."
            )
            log.error("encoder UNAVAILABLE — %s", self._embed_error)
            return None
        self._embedder = emb
        self._embed_state = READY
        self._embed_error = None
        self._record_backend(emb)
        return emb

    def _record_backend(self, model) -> None:
        """Note what a freshly loaded model is running on.

        Tolerant by design: a model that predates the backend seam, or a test
        double that does not implement it, must not break a warm-up. Reporting
        is worth having and is never worth a failed load.
        """
        try:
            spec = getattr(model, "backend", None)
            if spec is not None:
                self._backends[spec.component] = spec.to_dict()
                return
            for name, spec in (getattr(model, "backends", None) or {}).items():
                self._backends[name] = spec.to_dict()
        except Exception:                                          # noqa: BLE001
            pass

    def _spawn_loader(self) -> None:
        """Caller holds the lock. At most one warm-up runs at a time."""
        if self._ingest_state in (DISABLED, UNAVAILABLE, READY, WARMING):
            return
        if self._loader is not None and self._loader.is_alive():
            return
        self._ingest_state = WARMING
        self._loader = threading.Thread(target=self._warm, name="model-warm",
                                        daemon=True)
        self._loader.start()

    def _warm(self) -> None:
        started = time.monotonic()
        pipeline: Optional[IngestPipeline] = None
        with self._lock:
            if not self._wanted:
                # Every camera went away while this thread was starting. Fall
                # back to idle rather than loading weights nobody asked for.
                self._ingest_state = IDLE
                return
            try:
                embedder = self._embedder or self._load_embedder()
                if embedder is None:
                    raise RuntimeError(self._embed_error or "encoder unavailable")

                # NO DETECTOR AND NO PLATE READER. Both moved to the
                # analytics service, which posts finished observations here.
                # Loading them would export an OpenVINO artifact, hold a plate
                # localiser and an OCR model resident, and never run any of
                # them once.
                #
                # plates_available is answered from that service now, not from
                # a reader in this process — see the property below.

                # No detector, no plate reader, no queue and no worker: this
                # pipeline is reached from POST /observations on the request
                # thread, so there is nothing to schedule.
                pipeline = IngestPipeline(self._cfg, embedder, self._store,
                                          self._writer)
                self._pipeline = pipeline
                self._ingest_state = READY
                self._ingest_error = None
                self._fail_count = 0
                self._warmups += 1
            except Exception as exc:                               # noqa: BLE001
                self._detector = None
                self._plates = None
                self._ingest_error = f"{type(exc).__name__}: {exc}"
                if self._embed_state == UNAVAILABLE:
                    self._ingest_state = UNAVAILABLE
                    log.error("ingest UNAVAILABLE — %s", self._ingest_error)
                else:
                    self._fail_count += 1
                    delay = min(_RETRY_BASE * (2 ** (self._fail_count - 1)), _RETRY_MAX)
                    self._retry_at = time.monotonic() + delay
                    self._ingest_state = FAILED
                    log.error("ingest warm-up failed (attempt %d, retry in %.0fs): %s",
                              self._fail_count, delay, self._ingest_error)
                return
        # Outside the lock: the engine starts capture threads from here.
        log.info("ingest models ready in %.1fs", time.monotonic() - started)
        self._notify()

    # ── releasing ────────────────────────────────────────────────────────────
    def _detach_ingest(self) -> Optional[IngestPipeline]:
        """Caller holds the lock. Marks ingest unloaded and hands the pipeline
        back for the caller to tear down OUTSIDE the lock — see set_listener."""
        pipeline, self._pipeline = self._pipeline, None
        # Dropping these here is safe while the worker drains: the pipeline
        # holds its own references and keeps them alive until it is collected.
        self._detector = None
        self._plates = None
        if self._ingest_state in (READY, WARMING, FAILED):
            self._ingest_state = IDLE
        return pipeline

    def _teardown(self, pipeline: Optional[IngestPipeline], why: str) -> None:
        """NO LOCK HELD. Stops the capture threads, then the worker, then folds
        the counters forward — in that order, so nothing is still feeding a dead
        queue.

        DELIBERATELY DOES NOT RECLAIM. The caller still holds a reference to
        `pipeline` — it passed it in — and the pipeline holds the detector, the
        encoder and the plate reader. Reclaiming here collects and trims while
        every model is still reachable, which is exactly as useless as it
        sounds and completely invisible: the logs say "released", the states all
        read idle, and RSS does not move. Measured on the appliance, that alone
        was the difference between 1.27 GB and 0.4 GB retained. The caller drops
        its reference and calls _reclaim() once, at the end.
        """
        if pipeline is None:
            return
        self._notify()
        # Nothing to stop: no worker thread, no queue.
        with self._lock:
            for k in _LIFETIME_KEYS:
                self._lifetime[k] += int(getattr(pipeline, k, 0) or 0)
            self._hibernations += 1
        log.info("ingest models released (%s)", why)

    def _release_faces(self, why: str) -> None:
        """Caller holds the lock. Reclaims in the caller — see _teardown.

        THE FACE ENCODER IS QUERY-SIDE ONLY, so it rides the encoder's clock:
        ingest never touches it (analytics embeds off the raw frame), which
        means the only thing that can want it is a search. Left out of
        hibernation it pinned ~37 MB of SFace plus cv2's DNN arenas for the
        life of the process after ONE face query — on an appliance that
        deliberately returns its models to the OS when nothing is asking.

        `_faces_tried` is cleared with it. That flag exists to stop a missing
        model file being re-probed on every query; keeping it set through a
        release would make the encoder unloadable until a restart.
        """
        if self._faces is None:
            return
        self._faces = None
        self._faces_tried = False
        log.info("face encoder released (%s)", why)

    def _release_embedder(self, why: str) -> None:
        """Caller holds the lock. Reclaims in the caller — see _teardown."""
        if self._embedder is None:
            return
        self._embedder = None
        if self._embed_state in (READY, FAILED):
            self._embed_state = IDLE
        log.info("encoder released (%s)", why)

    @staticmethod
    def _reclaim() -> None:
        """Give the memory back rather than merely dropping the reference.

        THREE STEPS, AND DROPPING THE REFERENCE IS ONLY THE FIRST. Measured in
        the service image, loading CLIP and YOLO on top of a 391 MB
        imports-only floor takes RSS to 1007 MB:

            del + gc.collect()   1007 MB -> 595 MB
            malloc_trim(0)        595 MB -> 401 MB

        glibc keeps freed arenas for reuse rather than returning them, so
        without the trim two thirds of the released memory stays charged to the
        process and an operator reasonably concludes nothing was freed. What
        cannot be given back is the ~390 MB floor: importing torch and
        ultralytics maps shared libraries that live as long as the process. That
        is the honest ceiling on hibernation, and the reason "stop the
        container" is the only thing that beats it.

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

    def _notify(self) -> None:
        """Hand the engine the CURRENT pipeline, whatever it is.

        Deliberately re-read under the lock rather than passed in by the caller.
        A warm-up notifies after releasing the lock, and so does a teardown, so
        the two can interleave and deliver "here is a pipeline" AFTER "it is
        gone" — leaving the engine feeding frames to a stopped worker for as
        long as the cameras stay up. Reading the truth at delivery time makes
        the order irrelevant; _notify_lock stops two deliveries overlapping.

        _notify_lock is taken OUTSIDE the pool lock and the pool lock is only
        taken inside it, never the reverse.
        """
        if self._listener is None:
            return
        with self._notify_lock:
            with self._lock:
                current = self._pipeline
            try:
                self._listener(current)
            except Exception:                                      # noqa: BLE001
                log.exception("pipeline listener failed")

    # ── the clock ────────────────────────────────────────────────────────────
    def _reap(self) -> None:
        poll = max(1.0, self._cfg.lifecycle.poll_seconds)
        while not self._stop.wait(poll):
            try:
                self._tick()
            except Exception:                                      # noqa: BLE001
                log.exception("model reaper tick failed")

    def _tick(self) -> None:
        now = time.monotonic()
        timeout = self._idle_timeout
        pipeline: Optional[IngestPipeline] = None
        drop_encoder = False
        # BEFORE the camera check, deliberately. Everything below this line is
        # about ingest and stands down while cameras are registered; the face
        # encoder is not about ingest, so an appliance that always has cameras
        # would otherwise never release it.
        self._reap_faces(now, timeout)
        with self._lock:
            if self._wanted:
                # Cameras are registered: the only thing due here is a retry of
                # a warm-up that failed.
                if self._ingest_state == FAILED and now >= self._retry_at:
                    log.info("retrying ingest warm-up")
                    self._spawn_loader()
                return
            if self._idle_since is None:
                self._idle_since = now
                return
            if now - self._idle_since < timeout:
                return
            if self._pipeline is not None:
                pipeline = self._detach_ingest()
            # The encoder answers searches too, so BOTH clocks must be quiet
            # before it goes: a service with no cameras is still a searchable
            # archive, and reloading it on every query would be the wrong trade.
            quiet_for = now - max(self._idle_since, self._last_query)
            drop_encoder = self._embedder is not None and quiet_for >= timeout
            # Same clock, same reason: a face query is a search, and an
            # appliance with nobody searching should not be holding a face
            # model. Tracked separately because the two load independently —
            # a deployment can have face search and no text search, or either
            # one loaded while the other never has been.

        released = pipeline is not None
        self._teardown(pipeline, f"no cameras for {timeout:.0f}s")
        pipeline = None                  # the last reference — before _reclaim
        if drop_encoder:
            with self._lock:
                self._release_embedder(f"no cameras or searches for {timeout:.0f}s")
        if released or drop_encoder:
            self._reclaim()

    def _reap_faces(self, now: float, timeout: float) -> None:
        """Release the query-side face encoder once nobody has searched.

        Runs whether or not cameras are registered — see _tick. Reclaims here
        rather than leaving it to the caller, because on a live appliance this
        is usually the only thing released on a given tick and `_reclaim` is
        what actually returns the arenas to the OS.
        """
        if timeout <= 0:
            return
        with self._lock:
            if self._faces is None or now - self._last_face_query < timeout:
                return
            self._release_faces(f"no face searches for {timeout:.0f}s")
        self._reclaim()

    # ── observability ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        """Status for /health. DELIBERATELY TAKES NO LOCK.

        A warm-up holds the pool lock for as long as the load takes, and the
        first one on a new deployment DOWNLOADS the weights — minutes, not the
        2.4 s a warm reload costs. A snapshot that waited for the lock would
        hang /health for exactly as long, and the container healthcheck (5 s
        timeout) would mark a service unhealthy for doing precisely what it was
        asked to do. Observed on the appliance the first time this ran.

        Everything read here is a scalar or a fixed-key dict, so an unlocked
        read can only ever be slightly out of date — which is what a status
        endpoint is anyway. Being able to ANSWER during a load matters more than
        the answer being a single consistent instant.
        """
        idle_since = self._idle_since
        idle_for = (round(time.monotonic() - idle_since, 1)
                    if idle_since is not None else None)
        ingest_state = self._ingest_state

        # _lifetime only gains a pipeline's counters when that pipeline is torn
        # down (_teardown folds them in), so on its own it reports the work of
        # PAST sessions only. A freshly installed appliance never hibernates
        # while it is busy, so /health showed frames_processed/detections_seen/
        # rows_written flat at 0 for the entire first session — the pipeline
        # looked dead while it was actively writing rows to the index. Add the
        # RUNNING pipeline's counters on top so the numbers track live work.
        #
        # Read the reference once: _detach_ingest clears self._pipeline BEFORE
        # _teardown folds the counters, so a snapshot landing between the two
        # briefly misses that session — an undercount that the fold corrects a
        # moment later. Re-reading self._pipeline per key could instead count
        # one session TWICE, which is the error an operator cannot recover from.
        live = self._pipeline
        lifetime = dict(self._lifetime)
        if live is not None:
            for k in _LIFETIME_KEYS:
                lifetime[k] += int(getattr(live, k, 0) or 0)

        return {
            "hibernation": {
                "enabled": self._idle_timeout > 0,
                "idle_timeout_seconds": self._idle_timeout,
                "idle_for_seconds": idle_for,
                "hibernations": self._hibernations,
                "warmups": self._warmups,
            },
            "ingest": {
                "state": ingest_state,
                "error": self._ingest_error,
                "retry_in_seconds": (
                    round(max(0.0, self._retry_at - time.monotonic()), 1)
                    if ingest_state == FAILED else None
                ),
            },
            "encoder": {
                "state": self._embed_state,
                "error": self._embed_error,
                "loaded": self._embedder is not None,
            },
            "plates": {"available": self.plates_available,
                       # Read by the analytics service, not here. Said out
                       # loud so nobody reads `loaded: false` as a fault.
                       "read_by": "analytics"},
            # Faces are EMBEDDED by analytics and only queried here, so this
            # says which model both sides must agree on — the one thing that
            # cannot be diagnosed from either service alone.
            "faces": {**self.face_state(), "embedded_by": "analytics"},
            "inference": self.inference_snapshot(),
            # NO detector_selection here any more: this service loads no
            # detector. The analytics service reports which backend it chose.
            # NOT dict(self._lifetime): the local folds the RUNNING pipeline's
            # counters on top, so a first session no longer reports zeros
            # while it is actively writing rows. See where it is built above.
            "lifetime": lifetime,
        }

    def inference_snapshot(self) -> dict:
        """What each model is running on. NO LOCK, for the reason above.

        This is the answer to a question that previously had none: /health
        reported that ingest was ready without ever saying whether it was ready
        on a CPU or a GPU, and the only record of the resolved device was one
        line in the boot log. An operator diagnosing "why is this slow" should
        not have to read logs to learn which device is doing the work.

        `selected_by` matters as much as the backend name. It distinguishes a
        decision that was benchmarked from one that is simply what shipped —
        the difference between "this is the fastest option here" and "nothing
        else has been tried", which look identical if only the name is shown.
        """
        loaded = {
            "detector": self._detector is not None,
            "embedder": self._embedder is not None,
            "plate_localiser": self._plates is not None,
            "plate_ocr": self._plates is not None,
        }
        out: dict[str, dict] = {}
        for component, spec in self._backends.items():
            entry = dict(spec)
            entry["loaded"] = loaded.get(component, False)
            out[component] = entry
        # A component that has never been loaded has no spec to report, and
        # saying nothing is more honest than reporting a default we have not
        # confirmed. The key's absence is the signal.
        return out
