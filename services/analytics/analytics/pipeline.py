"""pipeline.py — the single detection worker every camera feeds.

WHERE THIS SERVICE STOPS. It detects objects, reads plates, tracks them across
frames and decides which observations deserve a record. It produces CROPS and
METADATA. It does not embed, deduplicate, store or search: those are Smart
Search's job and they need the CLIP encoder and the database, neither of which
exists here.

The seam is the crop. Everything above it is this file; everything below it is
unchanged in Smart Search:

    detect -> domain filter -> size filter -> tracker -> policy -> CROP
                                                                    |
                                                    CLIP -> dedup -> store

ONE WORKER, NOT ONE PER CAMERA. The models are loaded once and shared; frames
arrive from N sources through one bounded queue. That is what makes cost scale
with total frame rate rather than with camera count.

BACKPRESSURE IS A DROP, NOT A WAIT, and what is discarded is the OLDEST. For a
forensic index a frame that has sat in a queue for a minute is worth less than
the one that just arrived: both describe the same scene and only one of them is
current. Blocking the producer instead would stall the stream, build latency
and eventually drop frames anyway — but silently, where nothing can report it.

Order of operations matters for cost: detect first (cheap, and on a quiet
camera the motion gate has already rejected most frames), crop, then track,
then apply the policy. Identity and policy both run BEFORE anything leaves this
service, because everything downstream of the crop is expensive — an encoder
pass, a JPEG, a row — and this is the last place it can be avoided.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

import numpy as np

from .best_frames import BestFrameBuffer
from .config import AppConfig
from .detector import Detection, Detector, PERSON_DOMAIN

#: The face domain's name on the wire, in `search_domains`, and in the index.
FACE_DOMAIN = "face"
from .indexing_policy import IndexingPolicy
from .motion import MotionGate, MotionResult
from .nested_boxes import suppress_nested
from .tracking import ObjectTracker

log = logging.getLogger(__name__)

#: (slug, crop, metadata) — what this service produces. The sink decides what
#: to do with it; in phase A there is no sink and these are only counted.
Observation = dict
#: Called as sink(slug, crop, obs, frame=jpeg_bytes_or_None). `frame` is the
#: whole frame the crop was cut from, already downscaled and JPEG-encoded —
#: for the Recent-detections feed only; the crop is still what gets embedded.
ObservationSink = Callable[..., None]

#: A frame holding more objects than this is past the point where labelled
#: boxes tell an operator anything, and the list rides along on every row the
#: frame writes. Drawing only: nothing about the crops or the index changes.
MAX_FRAME_BOXES = 40


class DetectionPipeline:
    def __init__(self, config: AppConfig, detector: Detector,
                 plate_reader=None, face_reader=None,
                 sink: Optional[ObservationSink] = None) -> None:
        self._cfg = config
        self._detector = detector
        self._plates = plate_reader
        self._faces = face_reader
        self._sink = sink

        self._queue: queue.Queue = queue.Queue(maxsize=config.detect.queue_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # One gate per camera, and only in sampler mode: in broker mode the
        # canonical analysis arrives with the frame and these are never built.
        self._gates: dict[str, MotionGate] = {}
        self._domains: dict[str, tuple[str, ...]] = {}

        t = config.tracking
        self._tracker = ObjectTracker(
            max_association_cost=t.max_association_cost,
            velocity_alpha=t.velocity_alpha,
            confirm_after_seconds=t.confirm_after_seconds,
            max_age_seconds=t.max_age_seconds,
            stationary_after_seconds=t.stationary_after_seconds,
            stationary_distance=t.stationary_distance,
            max_tracks_per_key=t.max_tracks_per_key,
        ) if t.enabled else None
        self._index_tentative = t.index_tentative

        p = config.index_policy
        self._policy = IndexingPolicy(
            suppression_enabled=p.suppression_enabled,
            displacement_heights=p.displacement_heights,
            scale_change_ratio=p.scale_change_ratio,
            scale_change_persist=p.scale_change_persist,
            heartbeat_seconds=p.heartbeat_seconds,
            quality_confidence_gain=p.quality_confidence_gain,
            min_interval_seconds=p.min_interval_seconds,
        )
        self._best_frames = BestFrameBuffer()

        # ── counters, all of them lifetime and all on /health ──────────────
        self.frames_queued = 0
        self.frames_dropped = 0
        self.frames_evicted = 0
        #: Seconds the last evicted frame had been waiting. How far behind real
        #: time the worker is, in units an operator can act on.
        self.last_evicted_age = 0.0
        #: Age of the frame the worker most recently picked up. A rising value
        #: is the pipeline falling behind BEFORE anything is lost, which is the
        #: early warning a drop count only gives once it is too late.
        self.last_backlog_age = 0.0
        self.frames_processed = 0
        self.frames_skipped_no_motion = 0
        self.frames_gated_by_broker = 0
        self.detector_calls = 0
        self.detections_seen = 0
        #: Boxes dropped for lying inside another box of the same domain. A
        #: rate that climbs is the DETECTOR degrading, not this working
        #: harder — it is the only place a split box is visible as a number.
        self.detections_nested = 0
        self.detections_tentative = 0
        self.crops_too_small = 0
        self.crops_suppressed_by_policy = 0
        self.observations_produced = 0
        #: Whole-frame JPEGs made for the feed. At most one per frame however
        #: many of its objects are recorded, so this runs below
        #: observations_produced whenever frames carry several objects.
        self.snapshots_encoded = 0
        self.localiser_calls = 0
        #: Face observations emitted, and person crops that were searched and
        #: yielded nothing. Kept apart because they answer different questions:
        #: the first is what the domain produced, the second is whether the
        #: camera can see faces at all. On this site ~90% of person crops carry
        #: no usable face, so a single counter would read as a broken model.
        self.faces_emitted = 0
        self.face_crops_searched = 0
        self.last_error: Optional[str] = None

    # ── the plate reader, which comes and goes ──────────────────────────────
    def set_plate_reader(self, reader) -> None:
        """Attach or detach ANPR without rebuilding the pipeline.

        THE READER IS A SECOND MODEL OVER EVERY VEHICLE and only the `plate`
        domain asks for it, so it is loaded when some camera wants plates and
        released when none do — while the detector, which every domain needs,
        stays where it is. Rebuilding the pipeline to change one of its two
        models would throw away the tracker, the policy anchors and the best-
        frame buffers, which is a recall loss for a memory saving.

        A plain assignment: the worker reads this attribute on each emit, and
        Python's attribute writes are atomic, so a swap mid-frame yields the
        old reader or the new one and never a half-built state. The counterpart
        gate is `"plate" in wanted` in _emit — this decides whether ANPR is
        POSSIBLE, that decides whether this camera asked for it.
        """
        self._plates = reader

    @property
    def plate_reader(self):
        return self._plates

    def set_face_reader(self, reader) -> None:
        """Install or drop the face reader.

        THE SAME SHAPE AS set_plate_reader AND FOR THE SAME REASON: it is a
        second model over every PERSON, wanted only by cameras that asked for
        the `face` domain, so it is loaded on demand and released when demand
        goes. The gate at the call site is `"face" in wanted`, which decides
        per camera; this decides whether the model exists at all.
        """
        self._faces = reader

    @property
    def face_reader(self):
        return self._faces

    # ── camera registration ─────────────────────────────────────────────────
    def set_domains(self, slug: str, domains: tuple[str, ...]) -> None:
        self._domains[slug] = tuple(domains)

    def forget_domains(self, slug: str) -> None:
        self._domains.pop(slug, None)

    def domains_for(self, slug: str) -> tuple[str, ...]:
        return self._domains.get(slug, ("person", "vehicles"))

    def reset_gate(self, slug: str) -> None:
        """Drop per-camera state after a reconnect. Frames either side of an
        outage are separated by however long it lasted, and associating across
        that gap is guesswork wearing the costume of continuity."""
        g = self._gates.get(slug)
        if g is not None:
            g.reset()
        if self._tracker is not None:
            self._tracker.forget(slug)
        gone = self._tracker.drain_expired() if self._tracker else []
        if gone:
            self._policy.forget(gone)
            self._best_frames.forget(gone)

    def note_gated_before_read(self, slug: str) -> None:
        """A frame the broker found no motion in, dropped before it was copied.

        COUNTED ANYWAY. The gate stats are how an operator sees that gating is
        working at all, and moving the decision upstream of the copy must not
        make it look like the gate stopped running."""
        self.frames_gated_by_broker += 1
        self.frames_skipped_no_motion += 1

    # ── intake ──────────────────────────────────────────────────────────────
    def submit(self, slug: str, frame: np.ndarray, ts: float,
               motion: Optional[dict] = None) -> None:
        """Queue a frame, preferring the NEWEST when there is no room."""
        try:
            self._queue.put_nowait((slug, frame, ts, motion))
            self.frames_queued += 1
            return
        except queue.Full:
            pass
        # Make room. A racing consumer may have drained it already, which is
        # fine: the put below is what has to succeed.
        try:
            stale = self._queue.get_nowait()
            self.frames_evicted += 1
            self.last_evicted_age = max(0.0, ts - stale[2])
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait((slug, frame, ts, motion))
            self.frames_queued += 1
        except queue.Full:
            # Another producer refilled the slot between the two calls. The
            # newest frame loses this time; counted, and rare by construction.
            self.frames_dropped += 1

    # ── worker ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="analytics",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        log.info("detection worker started (queue=%d)", self._queue.maxsize)
        while not self._stop.is_set():
            try:
                slug, frame, ts, motion = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self.last_backlog_age = max(0.0, time.time() - ts)
            try:
                self._process(slug, frame, ts, motion)
                self.frames_processed += 1
            except Exception as exc:                     # noqa: BLE001
                # The worker is the whole pipeline. It survives anything, and
                # says what it survived.
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("detection failed for %s", slug)

    # ── the gate ────────────────────────────────────────────────────────────
    def _gate(self, slug: str) -> MotionGate:
        g = self._gates.get(slug)
        if g is None:
            m = self._cfg.motion
            g = MotionGate(
                threshold=m.threshold, min_area_fraction=m.min_area_fraction,
                dilate_iterations=m.dilate_iterations, max_regions=m.max_regions,
                region_padding=m.region_padding,
                full_frame_fraction=m.full_frame_fraction,
                scene_change_fraction=m.scene_change_fraction,
                despeckle_iterations=m.despeckle_iterations,
            )
            self._gates[slug] = g
        return g

    @staticmethod
    def _result_from_notice(motion: dict) -> MotionResult:
        """The broker's analysis, rebuilt as the value the local gate returns.

        THE SAME TYPE ON BOTH PATHS, which is what keeps exactly one code path
        below this point. The notice is that type serialised, so this is a
        reconstruction rather than a conversion.
        """
        regions = motion.get("regions")
        return MotionResult(
            fraction=float(motion.get("fraction", 0.0)),
            regions=None if regions is None else [tuple(r) for r in regions],
            verdict=motion.get("verdict", "regions"),
            reason=motion.get("reason", "broker"),
            baseline_valid=bool(motion.get("baseline_valid", True)),
        )

    def _detect(self, slug: str, frame: np.ndarray,
                motion: Optional[dict] = None) -> list[Detection]:
        """What the detector found, with split boxes collapsed to one.

        ONE SEAM, AND IT IS HERE. Suppression has to happen before ANYTHING is
        told there were two objects: the tracker treats two boxes in a frame as
        two objects by invariant, and CLIP dedup compares appearance, which a
        torso and a whole body do not share. Neither can undo this later, so
        neither is asked to. See analytics/nested_boxes.py.

        It also covers a second source of duplicates that has nothing to do
        with the detector: motion regions are merged BEFORE their 15% padding
        is applied, so two padded regions can overlap, and an object in the
        overlap is detected once per region by separate detector calls that no
        NMS spans.
        """
        found = self._detect_raw(slug, frame, motion)
        if len(found) < 2:
            return found
        kept = suppress_nested(found, self._cfg.detect.nested_containment)
        self.detections_nested += len(found) - len(kept)
        return kept

    def _detect_raw(self, slug: str, frame: np.ndarray,
                    motion: Optional[dict] = None) -> list[Detection]:
        """Detector calls for one frame, gated and optionally region-cropped.

        TWO SOURCES OF THE GATE DECISION, and the difference is only WHERE the
        differencing happened, never what it decided:

          motion=None  we decoded this frame ourselves, so the local gate runs
                       on it. The sampler fallback.
          motion={...} vms_frames decoded it and already ran the canonical
                       analysis on THESE pixels. Re-running our gate would
                       difference the same frame against the same predecessor
                       and reach the same answer for the same cost.
        """
        if not self._cfg.motion.enabled:
            self.detector_calls += 1
            return list(self._detector.detect(frame))

        if motion is not None:
            result = self._result_from_notice(motion)
            self.frames_gated_by_broker += 1
        else:
            result = self._gate(slug).evaluate(frame)
        if result.regions is None:
            self.frames_skipped_no_motion += 1
            return []
        if not result.regions:
            self.detector_calls += 1
            return list(self._detector.detect(frame))

        out: list[Detection] = []
        for (rx1, ry1, rx2, ry2) in result.regions:
            crop = frame[ry1:ry2, rx1:rx2]
            if crop.size == 0:
                continue
            self.detector_calls += 1
            for d in self._detector.detect(crop):
                x1, y1, x2, y2 = d.xyxy
                # Back to FULL-frame coordinates. Getting this wrong does not
                # crash — it stores a plausible bbox in the wrong place.
                out.append(Detection(
                    domain=d.domain,
                    xyxy=(x1 + rx1, y1 + ry1, x2 + rx1, y2 + ry1),
                    confidence=d.confidence, label=d.label,
                ))
        return out

    # ── the frame ───────────────────────────────────────────────────────────
    def _process(self, slug: str, frame: np.ndarray, ts: float,
                 motion: Optional[dict] = None) -> None:
        detections = self._detect(slug, frame, motion)
        if not detections:
            return

        # Drop domains this camera does not contribute. This does NOT save
        # detection time — the detector's class filter is applied after the
        # forward pass — but it saves everything downstream of the crop.
        wanted = self.domains_for(slug)
        detections = [d for d in detections if d.domain in wanted]
        if not detections:
            return
        self.detections_seen += len(detections)

        min_w = self._cfg.detect.min_crop_width
        min_h = self._cfg.detect.min_crop_height
        h, w = frame.shape[:2]

        # Size filter first: it is free, and a crop too small to embed must not
        # occupy a slot in the tracker's state either.
        big_enough = [d for d in detections
                      if (d.xyxy[2] - d.xyxy[0]) >= min_w
                      and (d.xyxy[3] - d.xyxy[1]) >= min_h]
        self.crops_too_small += len(detections) - len(big_enough)
        if not big_enough:
            return

        # ── IDENTITY, THEN POLICY, AND BOTH BEFORE ANYTHING LEAVES ─────────
        # The tracker names each detection; the policy decides which names
        # deserve a record this time. Everything past this point costs an
        # encoder pass, a JPEG and a row in another service.
        if self._tracker is None:
            tracked = [_Untracked(d) for d in big_enough]
            gone: list = []
        else:
            tracked = self._tracker.update(slug, big_enough, ts)
            # Drained once and shared: draining twice would hand the second
            # caller an empty list and leak the state it was meant to free.
            gone = self._tracker.drain_expired()
        if gone:
            self._policy.forget(gone)
            self._best_frames.forget(gone)

        # One whole-frame JPEG per frame, shared by every object in it that is
        # recorded — see _emit. Filled lazily, so a frame whose detections are
        # all suppressed by the policy costs no encode at all.
        snap: dict = {}
        # EVERY object in this frame, for the picture the feed draws over.
        # Collected across the whole frame before anything is sent, because an
        # observation can only describe its own object: a card built from one
        # of them could box one person and leave the other three in the shot
        # unmarked, which is what this list exists to fix.
        boxes: list[dict] = []
        # Emission waits until the frame is fully decided, so the box list is
        # complete before the first observation carries it.
        pending: list[tuple] = []
        for td in tracked:
            det, track = td.detection, td.track
            if track is not None and not track.confirmed and not self._index_tentative:
                # Too young to trust. A RECALL decision, not a saving.
                self.detections_tentative += 1
                continue

            # A vehicle's best looks are collected whether or not this
            # observation is recorded: the point is to have a good frame ready
            # when one IS, and the best frame is often not the one that
            # triggers the record.
            if (self._plates is not None and getattr(self._plates, "active", False)
                    and det.domain != PERSON_DOMAIN and "plate" in wanted
                    and track is not None):
                x1, y1, x2, y2 = det.xyxy
                region = frame[y1:y2, x1:x2]
                if region.size:
                    self._best_frames.offer(track.track_id, region,
                                            det.confidence, ts)

            reason = (self._policy.decide(track, det, ts)
                      if track is not None else "untracked")
            # A box for every object, whether or not this observation of it is
            # recorded. The picture is the frame's detection result; the policy
            # decides which objects the frame is SENT for, not which of them
            # were in it.
            x1, y1, x2, y2 = det.xyxy
            boxes.append({
                "bbox": [x1 / w, y1 / h, x2 / w, y2 / h],
                "label": det.label,
                "domain": det.domain,
                "tracker_id": track.track_id if track else None,
                "confidence": round(float(det.confidence), 3),
                # The object this frame was sent for, as against one that is
                # merely in shot and was already recorded inside the interval.
                "recorded": reason is not None,
            })
            if reason is None:
                self._policy.suppress()
                self.crops_suppressed_by_policy += 1
                continue
            if track is not None:
                self._policy.record(track, det, ts, reason)
            pending.append((det, track, reason))

        # Capped once and then SHARED by every observation the frame produces:
        # the list is the same for all of them, and copying it per object buys
        # nothing on a frame that already holds a crowd.
        drawn = boxes[:MAX_FRAME_BOXES]
        for det, track, reason in pending:
            self._emit(slug, frame, det, track, ts, reason, wanted, w, h,
                       snap, drawn)

    def _emit(self, slug, frame, det, track, ts, reason, wanted, w, h,
              snap: Optional[dict] = None, boxes: Optional[list] = None) -> None:
        """One accepted observation, handed to the sink.

        THE CROP IS CUT HERE, not downstream, because this is the last place
        the full frame exists. Sending the frame instead would put 6 MB on the
        wire where ~200 KB will do, and would make the receiver repeat the
        bbox arithmetic that produced it.
        """
        x1, y1, x2, y2 = det.xyxy
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return

        plate = None
        if self._plates is not None and det.domain != PERSON_DOMAIN \
                and "plate" in wanted:
            # WHICH PIXELS GET READ. The best look at this vehicle so far is
            # usually not the frame that happened to trigger the record — a car
            # approaching produces steadily better looks and the policy fires
            # on the first acceptable one.
            best = self._best_frames.best(track.track_id) if track else None
            self.localiser_calls += 1
            try:
                plate = (self._plates.read(best.crop) if best is not None
                         else self._plates.read(crop))
            except Exception as exc:                       # noqa: BLE001
                log.debug("plate read failed for %s: %s", slug, exc)
                plate = None

        obs: Observation = {
            "camera": slug,
            "ts": ts,
            "domain": det.domain,
            "confidence": float(det.confidence),
            # The detector's own class name. For vehicles this IS the vehicle
            # type (car / truck / bus / motorcycle).
            "label": det.label,
            "plate": plate.text if plate else None,
            "plate_confidence": plate.confidence if plate else None,
            # Normalised 0-1, the same convention the zone config uses, so a
            # bbox means the same thing everywhere in the product.
            "bbox": [x1 / w, y1 / h, x2 / w, y2 / h],
            "tracker_id": track.track_id if track else None,
            "reason": reason,
        }
        self.observations_produced += 1
        if self._sink is not None:
            # The whole frame travels with the crop, encoded once per frame
            # however many objects in it are recorded. The crop is still what
            # CLIP embeds and what search shows; the frame is for the feed.
            if snap is None:
                snap = {}
            if "jpeg" not in snap:
                snap["jpeg"] = self._encode_snapshot(frame)
            if snap["jpeg"] and boxes:
                # Only ever alongside the picture they are drawn on.
                obs["frame_boxes"] = boxes
            self._sink(slug, crop, obs, frame=snap["jpeg"])

        # ── faces ───────────────────────────────────────────────────────────
        # AFTER the person observation, never instead of it. A face is an extra
        # row about the same moment, and the person row is the baseline the
        # product already earned — if face work fails, it costs the face.
        #
        # Cut from `crop`, which came out of the DECODED FRAME above. That is
        # the whole reason the embedding is computed in this service: the copy
        # Smart Search stores is JPEG quality 82, and measured on this corpus
        # re-encoding a face moves its vector by as much as identity does.
        if (self._faces is not None and getattr(self._faces, "active", False)
                and det.domain == PERSON_DOMAIN and FACE_DOMAIN in wanted):
            self.face_crops_searched += 1
            try:
                found = self._faces.read(crop)
            except Exception as exc:                           # noqa: BLE001
                log.debug("face read failed for %s: %s", slug, exc)
                found = []
            for face in found:
                fx1, fy1, fx2, fy2 = face.box
                face_obs: dict = {
                    "camera": slug,
                    "ts": ts,
                    "domain": FACE_DOMAIN,
                    # The FACE's own score, not the person detector's. They
                    # grade different things and averaging them would hide a
                    # confident person carrying an uncertain face.
                    "confidence": float(face.score),
                    "label": FACE_DOMAIN,
                    # Face box in FRAME coordinates: the face is found inside
                    # the person crop, so its offset has to be added back or
                    # every face would be drawn in the top-left of the frame.
                    "bbox": [(x1 + fx1) / w, (y1 + fy1) / h,
                             (x1 + fx2) / w, (y1 + fy2) / h],
                    # The person's track, so "every appearance of this person"
                    # can be asked across both domains.
                    "tracker_id": track.track_id if track else None,
                    "reason": reason,
                    "face_width_px": int(face.width_px),
                    # The vector AND what produced it. The index stores the
                    # model per row and scopes every query to the active one,
                    # so a half-upgraded fleet is caught at the door instead of
                    # mixing two vector spaces in one column.
                    "embedding_model": self._faces.model,
                    "embedding": [float(v) for v in face.embedding],
                }
                self.faces_emitted += 1
                if self._sink is not None:
                    # The ALIGNED face is what gets stored, because it is what
                    # the vector describes. A padded portrait would look better
                    # and mean something else. No frame: a face row keeps
                    # none — the person row of this moment already carries it.
                    self._sink(slug, face.aligned, face_obs)

    def _encode_snapshot(self, frame: np.ndarray) -> Optional[bytes]:
        """The whole frame, downscaled, as JPEG — for display, never search.

        Downscaled because the feed never needs 1080p and keeps frames for
        days: measured on this appliance, a busy 1920x1080 cam3 frame is
        ~130-170 KB at 1280 px wide. The box is NOT drawn in — the row carries
        the bbox, normalised, and the UI draws it over the picture, so the
        stored file is exactly what the camera saw.

        None when disabled (snapshot.frame_width = 0) or if encoding fails: a
        missing picture must cost the feed its image, never the observation.
        """
        width = int(self._cfg.snapshot.frame_width)
        if width <= 0:
            return None
        try:
            import cv2
            h, w = frame.shape[:2]
            img = frame
            if w > width:
                img = cv2.resize(frame, (width, max(1, round(h * width / w))),
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [
                cv2.IMWRITE_JPEG_QUALITY, int(self._cfg.snapshot.frame_quality)])
            if not ok:
                return None
            self.snapshots_encoded += 1
            return buf.tobytes()
        except Exception as exc:                                   # noqa: BLE001
            log.debug("snapshot encode failed: %s", exc)
            return None

    # ── observability ───────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        total = self.frames_queued or 1
        processed = self.frames_processed or 1
        return {
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "frames_queued": self.frames_queued,
            "frames_dropped": self.frames_dropped,
            "frames_evicted": self.frames_evicted,
            "evict_rate": round(self.frames_evicted / total, 4),
            "last_evicted_age_s": round(self.last_evicted_age, 2),
            "backlog_age_s": round(self.last_backlog_age, 2),
            "frames_processed": self.frames_processed,
            "motion_gate": {
                "frames_skipped_no_motion": self.frames_skipped_no_motion,
                "frames_gated_by_broker": self.frames_gated_by_broker,
                "detector_calls": self.detector_calls,
                "calls_per_processed_frame": round(
                    self.detector_calls / processed, 3),
                "per_camera": {k: g.snapshot() for k, g in self._gates.items()},
            },
            "detections_seen": self.detections_seen,
            # Split boxes collapsed before the tracker saw them. `seen` counts
            # what SURVIVED this, so the two never double-count.
            "detections_nested": self.detections_nested,
            "nested_containment": self._cfg.detect.nested_containment,
            "detections_tentative": self.detections_tentative,
            "crops_too_small": self.crops_too_small,
            "crops_suppressed_by_policy": self.crops_suppressed_by_policy,
            "observations_produced": self.observations_produced,
            "snapshots_encoded": self.snapshots_encoded,
            "localiser_calls": self.localiser_calls,
            "tracker": self._tracker.snapshot() if self._tracker else {"enabled": False},
            "indexing_policy": self._policy.snapshot(),
            "plate_candidates": self._best_frames.snapshot(),
            "faces": {
                "reader": (self._faces.snapshot()
                           if self._faces is not None else None),
                "emitted": self.faces_emitted,
                "person_crops_searched": self.face_crops_searched,
            },
            "last_error": self.last_error,
        }


class _Untracked:
    """Stand-in when tracking is disabled, so _process has one shape."""

    __slots__ = ("detection", "track")

    def __init__(self, detection) -> None:
        self.detection = detection
        self.track = None
