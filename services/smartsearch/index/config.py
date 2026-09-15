"""Configuration for the Smart Search index service.

Same shape as the motion service's: YAML for defaults, environment for the few
things a deployment actually varies. Cameras are NOT configured here — the
registry pushes them at runtime.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8013


@dataclass
class IngestConfig:
    # Sampling is capped, not fixed. Phase 0 measured a 190x spread in delivered
    # frame rate across cameras on one appliance (0.08 fps to 15 fps), so this
    # is "no more often than", never "this often".
    #
    # NOTHING HERE SAMPLES ANY MORE, so this is no longer a rate this service
    # applies. SEARCH_FRAME_SOURCE is `none` and that is its only valid value:
    # detection moved to services/analytics, which posts finished observations.
    # The only readers left are /health's `expected_source_fps` and
    # scripts/capacity.py.
    #
    # What it means now is the rate the FRAME SOURCE is expected to run at,
    # kept so that a real mismatch is visible in one place — which means it has
    # to track the broker. It did not: frames moved 2.0 -> 5.0 on 2026-09-10
    # and this stayed at 2.0, so /health reported a mismatch that was not real.
    # tests/test_compose_defaults.py now holds the two together.
    #
    # WHY 5, MEASURED RATHER THAN CHOSEN, AND FOR THE TRACKER ALONE. Replaying
    # the same 16 NVR segments 2026-09-07, 1 -> 2 FPS dropped the tracker cost
    # at p99 from 3.57 to 1.63 (walking pairs p95 7.88 -> 1.96) while distinct
    # objects found rose only 89 -> 91 against detections 969 -> 1989. frames
    # then measured 2 -> 5 the same way: association failure 14.9% -> 9.2%
    # recorded and 16.0% -> 12.9% live, for about 1.4x broker CPU.
    # SAMPLING FASTER DOES NOT FIND MORE OBJECTS; it keeps identities.
    max_sample_fps: float = 5.0
    #: Where frames come from: "sampler" (decode our own RTSP session, the
    #: behaviour that has always shipped) or "broker" (consume vms_frames,
    #: which decoded once and already ran the canonical motion analysis).
    #:
    #: SAMPLER IS STILL THE DEFAULT. The broker path is wired and verified but
    #: not yet the shipped posture: switching it on makes indexing depend on a
    #: service that did not exist last week, and that is a decision to take
    #: deliberately per deployment rather than to inherit from an upgrade.
    #:
    #: In broker mode max_sample_fps is IGNORED — the broker decides the rate
    #: for every consumer, which is the point of having one decoder.
    #: sampler | broker | none.
    #:
    #: `none` is THE END STATE OF THE ANALYTICS SPLIT, and it is not the same
    #: as disabling ingest. This service stops pulling frames and stops
    #: detecting; it keeps the encoder, the deduplicator and the store, and
    #: receives finished observations from the analytics service through
    #: POST /observations. Disabling ingest instead would tear down the very
    #: pipeline that endpoint needs and answer 503 to every observation.
    frame_source: str = "none"
    #: Where the broker publishes its notices. Frames themselves never travel
    #: this way; a notice is ~400 bytes saying which shm slot to read.
    broker_url: str = "redis://127.0.0.1:6379/0"
    broker_channel_prefix: str = "vms.frames"
    min_crop_width: int = 16
    min_crop_height: int = 32
    detect_confidence: float = 0.35
    # Appearance-deduplication window and threshold. Phase 0 measured purity
    # 0.983 and ARI 0.946 against a 15 FPS tracker reference, stable across
    # 0.80-0.88 — so 0.85 sits in the middle of a flat region, not on a cliff.
    dedup_window_seconds: float = 30.0
    dedup_cosine_threshold: float = 0.85
    # ── object identity and indexing ─────────────────────────────────────────
    # The 10s/1.5-height temporal gate that used to live here has been split in
    # two: index/tracking.py answers "is this the same object?" and
    # index/indexing_policy.py answers "does this observation earn a row?".
    # See TrackingConfig and IndexPolicyConfig below. The gate's association
    # logic was not thrown away — it is the tracker's matcher.
    #: Bounded queue between the capture threads and the single inference
    #: worker. Full means the newest frame is dropped and counted — see
    #: pipeline.py for why dropping beats blocking.
    queue_size: int = 64
    enabled: bool = True


#: Named sensitivity presets for the SmartSearch motion gate, as
#: (threshold, min_area_fraction). Borrowed as an idea from the motion
#: service's low/medium/high vocabulary — NOT its numbers, which are a
#: whole-frame changed-pixel fraction and mean something different from a
#: per-contour area.
#:
#: "high" IS NOT "better". Measured 2026-08-31 on a 10-minute clip: ungated
#: indexing found 8 people, the gate at 25/0.0008 found 7, and 18/0.0002 found
#: all 8 — but 15/0.0001 found only 6. More sensitivity puts more crops in
#: front of the deduplicator, which then merges two people into one. Recall is
#: not monotonic in sensitivity, so do not "tune up to be safe".
_SENSITIVITY: dict[str, tuple[int, float]] = {
    "low": (25, 0.0008),      # measured: lost 1 of 8 people
    "medium": (18, 0.0002),   # measured: found all 8 — the shipped default
    "high": (15, 0.0001),     # measured: found 6 of 8. Here for completeness.
}


@dataclass
class TrackingConfig:
    """Object identity. Answers "is this the same physical object?" and nothing
    else — whether an observation earns a row is IndexPolicyConfig's business.

    EVERY PARAMETER IS IN SECONDS OR OBJECT HEIGHTS, NEVER FRAMES OR PIXELS.
    That is deliberate and load-bearing: it is what stops a change of sample
    rate from silently re-tuning the tracker, and it is exactly where Norfair
    and ByteTrack would have fought us, since both count in frames.
    """
    enabled: bool = True
    #: Maximum association cost for a detection to continue a track. The cost
    #: is a 4-vector norm — bottom-centre displacement over object size, plus
    #: width and height ratio terms. See index/tracking.py.
    #:
    #: MEASURED 2026-09-07 on 16 replayed NVR segments at 2 FPS: real
    #: continuations sit at p50 0.07, p90 0.55, p95 0.79, p99 1.63. 1.75 covers
    #: about 99% without reaching far enough to grab a neighbour. At 1 FPS the
    #: same coverage needed >3.5, which is why the rate went up.
    max_association_cost: float = 1.75
    #: Exponential smoothing on velocity, in units per SECOND. Not a Kalman
    #: filter: at 2 FPS the useful part of prediction is "roughly where is it
    #: heading", and the measured association margin above does not justify a
    #: covariance model. Revisit if the sweep shows association failures that
    #: prediction quality would have caught.
    #: UNRESOLVED. The sweep found no benefit — fragmentation 1.41 at 0.0
    #: rising to 1.49 at 1.0 — but 76% of the trace's pairs are near-stationary,
    #: where prediction cannot help and its noise can hurt, and only 79 pairs
    #: are fast enough to test the case it exists for. Kept middling and
    #: flagged; the one parameter here still on principle, not evidence.
    velocity_alpha: float = 0.5
    #: How long a track must keep matching before it is called CONFIRMED. With
    #: index_tentative true this labels state for observability and lifecycle
    #: reporting; it is NOT a recall gate, and the sweep showed indexing
    #: identical at 0.0, 1.0 and 2.0 s because of that. Keep it as the honest
    #: answer to "has this been seen more than once", not as a filter.
    confirm_after_seconds: float = 1.0
    #: How long a track survives with no matching detection. Short on purpose —
    #: a wrong re-association is worse for search than an extra track, because
    #: it merges two people into one identity.
    max_age_seconds: float = 3.0
    #: Below this displacement for this long, a track is STATIONARY.
    stationary_after_seconds: float = 4.0
    stationary_distance: float = 0.15
    #: Hard ceiling on live tracks per (camera, domain). Tracks expire on a
    #: timer; this is the backstop for a camera that somehow produces hundreds
    #: of simultaneous detections.
    max_tracks_per_key: int = 64
    #: Index a track's first observation BEFORE it is confirmed.
    #:
    #: TRUE, AND THE REPLAY IS WHY. The design proposed false, on the argument
    #: that two hits before indexing removes detector flicker. Swept against
    #: 1,989 real detections it removed far more than flicker:
    #:
    #:     tentative NOT indexed   91.3% reduction   31.3% OF TRACKS NEVER
    #:                                               INDEXED AT ALL
    #:     tentative indexed       87.0% reduction    0.0% never indexed
    #:
    #: A track that never produces an observation is a person no search can
    #: return, and nearly a third of them is not a rounding error. Four points
    #: of embedding saving is not worth that, so the first sight of an object
    #: is always recorded and CLIP dedup absorbs whatever flicker follows —
    #: which is the job it already does.
    #:
    #: Set false only with the lost-track number from your own footage in hand.
    index_tentative: bool = True


@dataclass
class IndexPolicyConfig:
    """Whether an observation of an ALREADY-IDENTIFIED object earns a row.

    Kept apart from TrackingConfig on purpose. Conflating the two is what made
    the old temporal gate hard to reason about: it re-indexed on window expiry
    because the window was simultaneously its identity timeout and its
    indexing interval.
    """
    #: The escape hatch. False indexes every observation of every confirmed
    #: track — the behaviour before any suppression existed.
    suppression_enabled: bool = True
    #: Re-index once the object has moved this far FROM WHERE IT WAS LAST
    #: INDEXED — not from where it was last seen. The old gate could not tell
    #: the difference, because its entries followed the object; a tracker
    #: separates the two positions, which is the main gain here.
    displacement_heights: float = 1.5
    #: Re-index when the object's height changes by this factor either way
    #: (1.5x or 1/1.5x). An object that approached or receded is a materially
    #: different crop, which is what a forensic search is looking at.
    scale_change_ratio: float = 1.5
    #: HOW MANY CONSECUTIVE OBSERVATIONS THE SCALE CHANGE MUST SURVIVE before
    #: it earns a row. 1 restores the previous behaviour.
    #:
    #: 2, BECAUSE THE DETECTOR'S BOX IS NOISIER THAN THE THRESHOLD. Measured
    #: 2026-09-07 over 14 minutes of continuous footage: consecutive same-track
    #: height ratios run p50 1.03 / p95 1.64 / p99 2.55, so 6.1% of ordinary
    #: frame-to-frame pairs already clear 1.5x on noise alone. Worse, 83.7% of
    #: multi-observation tracks have heights that OSCILLATE rather than move in
    #: one direction — the box is not converging on the object, it is rattling
    #: around it. A single bad box then becomes the anchor and manufactures a
    #: row: one real case was a legs-only detection at confidence 0.54, after
    #: which an ordinary full-body box read as a 1.61x "scale change".
    #:
    #: Requiring persistence is the cheapest discrimination available, and it
    #: is the one idea worth taking from Frigate's stationary classifier
    #: (CHANGED_FRAMES_TO_FLIP = 2): a single frame's disagreement is not
    #: evidence, a change that survives two is. Measured against the
    #: alternatives on the same footage:
    #:
    #:     variant                 scale fires  rows  good-coverage
    #:     1.5x, no persistence             70   229      23.9%
    #:     2.0x threshold                   25   207      21.4%
    #:     smoothed heights                 35   205      22.2%
    #:     1.5x + persist 2                 43   200      22.4%   <- this
    #:
    #: It removes the most rows AND keeps more forensic coverage than either
    #: raising the threshold or smoothing the heights.
    #:
    #: NOT Frigate's pixel classifier, which was measured and rejected: its NCC
    #: check only discriminates for objects already STATIONARY, and at our
    #: fire points it scored median 0.262 against a 0.85 threshold — it would
    #: have vetoed 0 of 27 duplicates. See the note in indexing_policy.py.
    scale_change_persist: int = 2
    #: Re-index a visible object at least this often regardless, bounding
    #: worst-case absence from search for something that never moves.
    #:
    #: OFF BY DEFAULT, ON EVIDENCE. The mechanism is kept and still works — set
    #: any positive number to enable it — but measured over 14 minutes of
    #: CONTINUOUS single-camera footage (2,292 detections, 148 tracks) it earns
    #: nothing at any useful setting:
    #:
    #:     heartbeat  indexed  rows  coverage  fires
    #:         60 s       322   230     30.9%      1   <- the old default
    #:         30 s       326   232     31.3%      7
    #:         10 s       389   244     36.1%     57
    #:          0         321   229     30.8%      0   <- default now
    #:
    #: Two reasons it does not pay. Track lifetimes are short — median 2.5 s,
    #: longest 99 s, and only 3 of 148 tracks could ever reach 60 s — because
    #: TrackingConfig.max_age_seconds ends a track after a 3 s detection gap.
    #: And where it does fire its observations convert to rows at 22% against a
    #: 71% baseline, because CLIP dedup rejects them as visually identical: a
    #: heartbeat fires precisely when nothing has changed.
    #:
    #: What it was for — keeping a motionless object findable — is already done
    #: by the first-sight rule, which is never skipped. Re-enable if a site has
    #: genuinely long-lived tracks (a car park, say) where the lifetime
    #: distribution above does not hold.
    heartbeat_seconds: float = 0.0
    #: Re-index when the detector is meaningfully more confident AND the crop
    #: is no smaller — a better look at the same object. Matters for ANPR and
    #: for future face work, where crop quality is the whole game.
    quality_confidence_gain: float = 0.15


# ─────────────────────────────────────────────────────────────────────────────
# VESTIGIAL, AND DELIBERATELY LEFT IN PLACE FOR ONE RELEASE.
#
# MotionConfig, TrackingConfig and IndexPolicyConfig are no longer read by
# anything in this service: detection, the motion gate, the tracker and the
# indexing policy moved to services/analytics, and their values live in
# services/analytics/config.yaml now.
#
# They are kept so an existing .env or config.yaml carrying these keys still
# loads instead of failing on an unknown section, and so an operator who greps
# for a setting finds this note rather than silence. Tuning them HERE does
# nothing; tune them in the analytics service.
#
# Remove them once deployments have rolled past the split.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MotionConfig:
    """The gate in front of the detector. Defaults are deliberately sensitive:
    a missed person is a search that returns nothing, which is the failure this
    product cannot afford. Tune UP per site if a camera floods."""
    enabled: bool = True
    threshold: int = 18                  # per-pixel intensity delta
    #: Contour area, as a fraction of the downscaled frame. Area, not a
    #: whole-frame changed-pixel ratio: a distant figure makes a small but real
    #: contour and would never cross a global threshold.
    min_area_fraction: float = 0.0002
    dilate_iterations: int = 2
    #: Beyond this many regions, one full-frame inference is cheaper than N
    #: crops — the detector resizes any input to its own resolution.
    max_regions: int = 3
    region_padding: float = 0.15
    full_frame_fraction: float = 0.35    # regions covering more -> whole frame
    scene_change_fraction: float = 0.55  # lights/IR/camera moved -> whole frame
    #: Morphological opening passes on the difference mask before dilation.
    #: Removes isolated sensor pixels that would otherwise dilate into regions
    #: and trip max_regions into a full-frame pass. Measured on a synthetic
    #: mask (400 speckle pixels + one 4x9 distant figure): 252 regions passing
    #: min_area before, 1 after, and the figure survived both. 0 disables.
    #:
    #: This is NOT the motion service's noise floor. That service counts
    #: non-zero pixels after THRESH_TOZERO, so its floor is its threshold; this
    #: gate is already binary at `threshold` above, and a floor beneath it
    #: produces a bit-identical mask — verified 2026-09-07.
    despeckle_iterations: int = 1
    #: Optional named preset, applied over `threshold` and `min_area_fraction`
    #: when set. None keeps the explicit values above, which are the measured
    #: ones. See _SENSITIVITY below before reaching for "high".
    sensitivity: Optional[str] = None


@dataclass
class PlateConfig:
    """Number-plate reading. Two stages, and the localiser is REQUIRED —
    recognition alone returns empty strings on whole-vehicle crops, and returns
    them confidently, so there is no threshold that rescues it."""
    enabled: bool = True
    #: fast-plate-ocr, MIT, ONNX, ~3 MB. Downloads to the models volume.
    model: str = "cct-xs-v2-global-model"
    #: Operator-supplied plate-detection weights, loaded through ultralytics.
    #: This is the SITE-TUNED override — a model trained for one deployment's
    #: plates and angles beats the generic baseline below, and it is where a
    #: paid, region-trained model plugs in. EMPTY = use localiser_model.
    #: Weights are a binary the packaging harness would refuse, so they are
    #: never shipped — only pointed at.
    localiser_weights: str = ""
    #: The OPEN BASELINE: an open-image-models plate detector (MIT, ONNX),
    #: auto-downloaded on first use like the recogniser above — so plate
    #: reading works on a first start with no operator-supplied file. Only
    #: consulted when localiser_weights is empty. Set EXPLICITLY EMPTY to turn
    #: plate reading off entirely (/health then says inactive).
    localiser_model: str = "yolo-v9-t-384-license-plate-end2end"
    localiser_confidence: float = 0.3
    #: Best looks retained per vehicle track for plate reading. A car
    #: approaching produces steadily better crops, and the one worth reading is
    #: rarely the first that cleared the indexing policy.
    candidate_frames: int = 3
    #: HARD byte ceiling across all tracks, all cameras. Crops are kept at
    #: source resolution — a plate is small and downscaling would defeat the
    #: feature — so the budget is met by holding FEWER, never smaller. Without
    #: this a busy forecourt grows the process without limit.
    candidate_max_bytes: int = 64 * 1024 * 1024
    #: Square-letterbox the LOCALISER, as models.square_letterbox does for the
    #: detector. DEFAULT FALSE, AND DELIBERATELY NOT MATCHING THE DETECTOR.
    #:
    #: The detector's normalisation was validated on real detections before it
    #: was turned on. Nothing equivalent exists for ANPR: the two-line
    #: thresholds below were fitted to 109 real reads under RECTANGULAR
    #: preprocessing, and square padding changes which plate regions get boxed
    #: and how much margin they carry — margin being exactly what the
    #: recogniser needs, per the measurements at the top of index/plates.py.
    #:
    #: So this stays off until the localiser+OCR parity harness has been run on
    #: real plate crops with the operator's own ANPR weights. An exported
    #: localiser candidate is fixed-input and therefore square whatever this
    #: says; that difference is part of the CANDIDATE'S accuracy profile for the
    #: gate to accept or reject, and is not something to impose on production
    #: first so that a benchmark compares more neatly.
    localiser_square_letterbox: bool = False
    #: Grades how well a real plate was read. Does NOT reject non-plates — see
    #: the measurements at the top of index/plates.py.
    min_confidence: float = 0.6
    #: The actual non-plate rejection: too few characters decoded.
    min_length: int = 4
    #: Margin added around the localiser's box before recognition. A tight crop
    #: silently loses the first and last character — measured, see plates.py.
    region_padding: float = 0.12
    #: Below this width/height a plate region is treated as TWO-LINE and its
    #: rows are read separately. Measured across 109 real reads on this site:
    #: 93 fell under 2.2 and 69 clustered at ~1.5 (stacked truck plates), while
    #: single-line regions sat at 2.5-6.5. Stacked plates are the norm here, not
    #: an edge case. 0 disables two-line handling entirely.
    two_line_max_aspect: float = 2.2
    #: A row must beat this to be trusted enough to REPLACE the whole-region
    #: read. Deliberately far above min_confidence: overriding an answer the
    #: model already accepted needs more evidence than accepting one. Measured:
    #: good rows scored 0.96-1.00, the one row that invented a character 0.679.
    two_line_min_row_confidence: float = 0.80


@dataclass
class LifecycleConfig:
    """When the heavy models are held, and when they are given back.

    The service is a registry, an ingest pipeline and a query API in one
    process. Only the ingest half is expensive, and only while cameras are
    registered — so the weights are loaded when the first camera arrives and
    released once the last one has been gone for `idle_timeout_seconds`.

    THE TIMER IS NOT A TUNING KNOB, IT IS A DEBOUNCE. A camera's URL change is
    a remove followed by an add, and camera-mgmt reconciles from four workers
    at once, so the registered count touches zero routinely without anybody
    turning anything off. Anything below ~60 s starts unloading and reloading
    on ordinary registry traffic. Set to 0 to keep the old behaviour — load
    once, hold forever — which is what a permanently busy site wants.
    """
    idle_timeout_seconds: float = 300.0
    #: How often the reaper checks. Only bounds how late a release can be.
    poll_seconds: float = 10.0


@dataclass
class ModelConfig:
    # "ultralytics" today. Swapping to an Apache-2.0 detector is one class in
    # index/detector.py plus this string — see the note there about AGPL.
    detector: str = "ultralytics"
    detector_weights: str = "/models/weights/yolov8n.pt"
    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"
    #: None = pick CUDA when present, CPU otherwise. "cpu" forces the fallback.
    device: str | None = None
    embed_batch_size: int = 32
    #: Which detector implementation to load: "auto" | "openvino" | "torch".
    #:
    #: AUTO MEANS OPENVINO ON CPU, and that is a measured default rather than a
    #: preference. On the reference appliance, live 1920x1080 camera frames,
    #: 15 rounds with the warm-up discarded:
    #:
    #:     torch      p50 40.8ms  p95 62.0ms   23.9 fps
    #:     openvino   p50 17.6ms  p95 21.8ms   55.4 fps    <-- 2.3x, steadier
    #:     onnx       p50 89.4ms  p95 215.9ms  10.0 fps    <-- 2.2x SLOWER
    #:
    #: ONNX Runtime is deliberately not an option here. It matched torch on a
    #: developer laptop and was twice as slow in the container, which is
    #: precisely the kind of result that makes "ONNX is faster on CPU" an
    #: unsafe thing to assume.
    #:
    #: AUTO PICKS TORCH ON CUDA. The OpenVINO build in this image reports only
    #: a CPU device, so a GPU deployment has nothing to gain and a torch/cuda
    #: path that is already far past what the workload needs.
    detector_backend: str = "auto"
    #: How many times the preferred backend may fail to load before the service
    #: stops trying it and stays on torch for the life of the process.
    #:
    #: FAILURE NEVER COSTS INGEST. A failed OpenVINO load falls straight through
    #: to torch inside the same warm-up, so the pipeline comes up either way;
    #: the count only decides how many future warm-ups still attempt the
    #: preferred backend. A container restart clears it, which is the right
    #: reset for a machine that has been changed.
    backend_attempts: int = 3
    #: Where derived artifacts (the OpenVINO IR) are cached. The models volume,
    #: beside the weights that ultralytics downloads.
    models_dir: str = "/models"
    #: Letterbox every frame to a SQUARE input instead of ultralytics' default
    #: rectangular padding. This exists so a backend can be swapped without
    #: changing what gets detected.
    #:
    #: MEASURED 2026-09-04. An exported artifact (ONNX, OpenVINO) has a FIXED
    #: 640x640 input and must pad square; the torch path pads only to a stride
    #: multiple, so an 1080x810 frame becomes 640x480. The two regimes see
    #: different images and find different objects. On ultralytics' bus.jpg at
    #: conf 0.35 the torch default found 4 detections and every exported
    #: backend found 5 — the extra one a partially-visible person at the frame
    #: edge, at 0.436. On a wide 405x1080 frame it was 2 against 3.
    #:
    #: Setting this reproduces the exported behaviour on torch EXACTLY (0.890,
    #: 0.883, 0.878, 0.843, 0.436 against ONNX's identical figures), so backend
    #: choice becomes a pure performance decision. Note that neither
    #: imgsz=640 nor imgsz=(640,640) does this — only disabling rectangular
    #: inference does, and exported models ignore the flag entirely because
    #: their input shape is already fixed.
    #:
    #: THE COST IS REAL AND IN TWO PLACES. A square letterbox processes ~33%
    #: more pixels for a 4:3 frame, and the extra marginal detections it finds
    #: become extra crops, embeddings and stored rows. False keeps the historic
    #: rectangular behaviour and gives up backend-swap neutrality.
    square_letterbox: bool = True


@dataclass
class StoreConfig:
    dsn: str = "postgresql://search:search_secret@127.0.0.1:5434/smartsearch"
    crop_dir: str = "/data/search/crops"
    # fsync each crop before its row can reference it. ~5 ms per crop on NVMe
    # against 0.5 ms without — the price of the two stores agreeing after a
    # power cut, since the database fsyncs its commit and a JPEG otherwise sits
    # in the page cache. See writer.py for the 80 zero-byte crops that bought
    # this default. False keeps the atomic rename (a torn write still cannot be
    # referenced) and drops only the durability half.
    crop_fsync: bool = True
    retention_days: int = 30
    # How often the in-service sweep deletes expired rows and their crops.
    # Hourly matches the NVR's retention cadence. 0 disables the thread, for a
    # deployment that runs scripts/retention.py from cron instead — the reads
    # stay filtered either way, so disabling it costs disk, never correctness.
    retention_sweep_seconds: int = 3600
    # Run the orphan sweep (crop files no row references) every Nth expiry
    # sweep — 24 is daily at the default interval. It walks the whole crop tree,
    # so it is not worth doing hourly. 0 disables it.
    retention_orphan_every_n: int = 24
    # Where to ask what footage still exists. Empty disables the coverage sweep
    # entirely — coverage is the only thing that makes deleting by it safe, so
    # "no NVR configured" must mean "do not sweep", never "nothing is recorded".
    nvr_url: str = "http://127.0.0.1:8009"
    # Reconcile the index against the recordings every Nth expiry sweep — 24 is
    # daily at the default interval. Costs an HTTP round trip per camera, and
    # the drift it corrects appears at restarts rather than continuously, so it
    # does not want to be hourly. 0 disables it.
    retention_coverage_every_n: int = 24
    # Whole frames for the Recent-detections feed are kept this many days —
    # less than rows, since the feed shows recent detections and a frame is
    # tens of KB against a crop's ~5. 0 keeps frames as long as their rows.
    frame_retention_days: int = 7


@dataclass
class FaceConfig:
    """Face search: YuNet detects, SFace embeds, and both come from OpenCV.

    OFF UNLESS A CAMERA ASKS. Unlike person and vehicle indexing, this is not a
    site-wide default and should not become one: measured over this appliance's
    own footage (2026-09-14, 61,762 stored person crops), a usable face appears
    on ~4% of person passes site-wide, 17% on a close indoor camera and 0.2% on
    a distant one. Switching it on everywhere would collect biometric data that
    mostly cannot answer a query, which is the wrong trade under data-protection
    law as well as the wrong use of disk.
    """
    enabled: bool = True
    #: WHICH EMBEDDER, by name, from index/faces.py's registry. A name rather
    #: than a path because the width, the alignment and the preprocessing travel
    #: together with the model — pointing `recogniser_weights` at a different
    #: model's file would load geometry from one and weights from another.
    #: Changing this is a re-index; rows carry the model that produced them and
    #: queries are scoped to the active one, so a swap loses results rather than
    #: returning wrong ones.
    model: str = "sface-2021dec"
    #: OpenCV zoo model files, in the shared models volume. Never committed:
    #: the packaging harness refuses binaries, so they are pointed at.
    detector_weights: str = "/models/face_detection_yunet_2023mar.onnx"
    recogniser_weights: str = "/models/face_recognition_sface_2021dec.onnx"
    #: YuNet confidence. 0.85, not the zoo demo's 0.9 and emphatically not the
    #: 0.6 default: at 0.6 the detections are mostly the back of a head (30.7%
    #: of crops "have a face"), at 0.85 a 36/36 hand check were real faces, at
    #: 0.9 the rate collapses to 2.6%. This number IS the feature's precision.
    score_threshold: float = 0.85
    #: Faces narrower than this are not stored. SFace embeds a 112x112 aligned
    #: face, so below ~40px the input is an upscale of pixels that were never
    #: captured — measured rank-1 retrieval 24% under 40px against 40% at
    #: 64-111px. Keeping them would fill the index with rows that cannot answer.
    min_width_px: int = 40
    #: Cap per person crop. More than one face inside one person box is almost
    #: always a second person standing behind, and the detector is better used
    #: on the next crop than on the crowd in this one.
    max_per_crop: int = 2


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    index_policy: IndexPolicyConfig = field(default_factory=IndexPolicyConfig)
    plates: PlateConfig = field(default_factory=PlateConfig)
    faces: FaceConfig = field(default_factory=FaceConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str) -> "AppConfig":
        raw: dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

        api = ApiConfig(**(raw.get("api") or {}))
        ingest = IngestConfig(**(raw.get("ingest") or {}))
        motion = MotionConfig(**(raw.get("motion") or {}))
        if motion.sensitivity:
            preset = _SENSITIVITY.get(str(motion.sensitivity).strip().lower())
            if preset is None:
                raise ValueError(
                    f"motion.sensitivity must be one of {sorted(_SENSITIVITY)}, "
                    f"got {motion.sensitivity!r}")
            motion.threshold, motion.min_area_fraction = preset
        tracking = TrackingConfig(**(raw.get("tracking") or {}))
        index_policy = IndexPolicyConfig(**(raw.get("index_policy") or {}))
        plates = PlateConfig(**(raw.get("plates") or {}))
        faces = FaceConfig(**(raw.get("faces") or {}))
        lifecycle = LifecycleConfig(**(raw.get("lifecycle") or {}))
        models = ModelConfig(**(raw.get("models") or {}))
        store = StoreConfig(**(raw.get("store") or {}))
        log_level = str((raw.get("logging") or {}).get("level", "INFO")).upper()

        # Environment wins over the file — the file is the shipped default and
        # the environment is what a deployment set deliberately.
        api.host = os.environ.get("SMARTSEARCH_BIND_HOST", api.host)
        api.port = int(os.environ.get("SMARTSEARCH_PORT", api.port))
        store.dsn = os.environ.get("SEARCHDB_URL", store.dsn)
        store.crop_dir = os.environ.get("SEARCH_CROP_DIR", store.crop_dir)
        if "SEARCH_CROP_FSYNC" in os.environ:
            store.crop_fsync = os.environ["SEARCH_CROP_FSYNC"].lower() not in (
                "0", "false", "no"
            )
        store.retention_days = int(
            os.environ.get("SEARCH_RETENTION_DAYS", store.retention_days)
        )
        store.retention_sweep_seconds = int(
            os.environ.get("SEARCH_RETENTION_SWEEP_SECONDS",
                           store.retention_sweep_seconds)
        )
        store.retention_orphan_every_n = int(
            os.environ.get("SEARCH_RETENTION_ORPHAN_EVERY_N",
                           store.retention_orphan_every_n)
        )
        store.nvr_url = os.environ.get("NVR_URL", store.nvr_url)
        store.retention_coverage_every_n = int(
            os.environ.get("SEARCH_RETENTION_COVERAGE_EVERY_N",
                           store.retention_coverage_every_n)
        )
        store.frame_retention_days = int(
            os.environ.get("SEARCH_FRAME_RETENTION_DAYS", store.frame_retention_days)
        )
        log_level = os.environ.get("LOG_LEVEL", log_level).upper()
        models.device = os.environ.get("SEARCH_DEVICE", models.device) or None
        if "SEARCH_MOTION_SENSITIVITY" in os.environ:
            name = os.environ["SEARCH_MOTION_SENSITIVITY"].strip().lower()
            if name not in _SENSITIVITY:
                raise ValueError(
                    f"SEARCH_MOTION_SENSITIVITY must be one of "
                    f"{sorted(_SENSITIVITY)}, got {name!r}")
            motion.sensitivity = name
            motion.threshold, motion.min_area_fraction = _SENSITIVITY[name]
        if "SEARCH_MOTION_GATE" in os.environ:
            motion.enabled = os.environ["SEARCH_MOTION_GATE"].lower() not in (
                "0", "false", "no"
            )
        # Sample rate. The one knob that changes how much work every stage
        # downstream is handed, so it is an environment switch: a site that
        # cannot afford 2 FPS can drop to 1 with a restart, no rebuild.
        ingest.max_sample_fps = float(
            os.environ.get("SEARCH_SAMPLE_FPS", ingest.max_sample_fps))
        ingest.frame_source = os.environ.get(
            "SEARCH_FRAME_SOURCE", ingest.frame_source).strip().lower()
        if ingest.frame_source != "none":
            # LOUD, NOT SILENT. `sampler` and `broker` were this service's own
            # decoders; both moved to services/analytics along with the
            # detector, so accepting either would leave a service that
            # registers cameras, reports healthy, and indexes nothing.
            raise ValueError(
                f"SEARCH_FRAME_SOURCE must be 'none', got "
                f"{ingest.frame_source!r}. Detection moved to the analytics "
                "service: run it with --profile analytics and layer "
                "docker-compose.analytics.yml. This service now receives "
                "finished observations on POST /observations.")
        ingest.broker_url = os.environ.get(
            "SEARCH_BROKER_URL", os.environ.get("REDIS_URL", ingest.broker_url))
        # ── tracking ─────────────────────────────────────────────────────────
        if "SEARCH_TRACKING_ENABLED" in os.environ:
            tracking.enabled = os.environ["SEARCH_TRACKING_ENABLED"].lower()                 not in ("0", "false", "no")
        tracking.max_association_cost = float(os.environ.get(
            "SEARCH_TRACK_MAX_COST", tracking.max_association_cost))
        tracking.max_age_seconds = float(os.environ.get(
            "SEARCH_TRACK_MAX_AGE", tracking.max_age_seconds))
        tracking.confirm_after_seconds = float(os.environ.get(
            "SEARCH_TRACK_CONFIRM_AFTER", tracking.confirm_after_seconds))
        tracking.velocity_alpha = float(os.environ.get(
            "SEARCH_TRACK_VELOCITY_ALPHA", tracking.velocity_alpha))
        if "SEARCH_TRACK_INDEX_TENTATIVE" in os.environ:
            tracking.index_tentative = os.environ[
                "SEARCH_TRACK_INDEX_TENTATIVE"].lower() not in ("0", "false", "no")
        # ── indexing policy ──────────────────────────────────────────────────
        # THE ESCAPE HATCH, and it must keep working: false here indexes every
        # observation of every confirmed track, which is what an operator needs
        # when they suspect suppression is hiding something.
        if "SEARCH_INDEX_SUPPRESSION_ENABLED" in os.environ:
            index_policy.suppression_enabled = os.environ[
                "SEARCH_INDEX_SUPPRESSION_ENABLED"].lower() not in ("0", "false", "no")
        index_policy.displacement_heights = float(os.environ.get(
            "SEARCH_INDEX_DISPLACEMENT", index_policy.displacement_heights))
        index_policy.heartbeat_seconds = float(os.environ.get(
            "SEARCH_INDEX_HEARTBEAT", index_policy.heartbeat_seconds))
        index_policy.scale_change_ratio = float(os.environ.get(
            "SEARCH_INDEX_SCALE_CHANGE", index_policy.scale_change_ratio))
        index_policy.scale_change_persist = int(os.environ.get(
            "SEARCH_INDEX_SCALE_PERSIST", index_policy.scale_change_persist))
        if "SEARCH_INGEST_ENABLED" in os.environ:
            ingest.enabled = os.environ["SEARCH_INGEST_ENABLED"].lower() not in (
                "0", "false", "no"
            )

        plates.localiser_weights = os.environ.get(
            "SEARCH_PLATE_WEIGHTS", plates.localiser_weights
        )
        # Unset keeps the open baseline; explicitly empty disables plates.
        plates.localiser_model = os.environ.get(
            "SEARCH_PLATE_MODEL", plates.localiser_model
        )
        # Preprocessing, as environment switches, because these are the two
        # settings most likely to need changing on a running appliance without
        # a rebuild: square letterboxing is the one change in this work that
        # alters what gets detected, so an operator who does not like what it
        # does to their detection rate needs to be able to put it back in the
        # time it takes to restart a container.
        models.detector_backend = os.environ.get(
            "SEARCH_DETECTOR_BACKEND", models.detector_backend).strip().lower()
        models.backend_attempts = int(
            os.environ.get("SEARCH_BACKEND_ATTEMPTS", models.backend_attempts))
        models.models_dir = os.environ.get("SEARCH_MODELS_DIR", models.models_dir)
        if "SEARCH_SQUARE_LETTERBOX" in os.environ:
            models.square_letterbox = os.environ[
                "SEARCH_SQUARE_LETTERBOX"].lower() not in ("0", "false", "no")
        # Separate switch, and separate default (off). The localiser's square
        # regime is NOT validated for ANPR — see PlateConfig — so this exists to
        # run that validation on a real site, not to turn it on casually.
        if "SEARCH_PLATE_SQUARE_LETTERBOX" in os.environ:
            plates.localiser_square_letterbox = os.environ[
                "SEARCH_PLATE_SQUARE_LETTERBOX"].lower() not in ("0", "false", "no")
        # Face search. The weights are the only thing a deployment usually
        # varies (a mounted models volume), and the threshold is the one number
        # that changes what the feature means — see index/faces.py.
        faces.detector_weights = os.environ.get("SEARCH_FACE_DETECTOR_WEIGHTS",
                                                faces.detector_weights)
        faces.recogniser_weights = os.environ.get("SEARCH_FACE_WEIGHTS",
                                                  faces.recogniser_weights)
        faces.model = os.environ.get("SEARCH_FACE_MODEL", faces.model)
        if "SEARCH_FACE_SCORE" in os.environ:
            faces.score_threshold = float(os.environ["SEARCH_FACE_SCORE"])
        if "SEARCH_FACE_MIN_WIDTH" in os.environ:
            faces.min_width_px = int(os.environ["SEARCH_FACE_MIN_WIDTH"])
        if "SEARCH_FACES" in os.environ:
            faces.enabled = os.environ["SEARCH_FACES"].lower() not in (
                "0", "false", "no"
            )
        if "SEARCH_IDLE_TIMEOUT" in os.environ:
            lifecycle.idle_timeout_seconds = float(os.environ["SEARCH_IDLE_TIMEOUT"])
        return cls(api=api, ingest=ingest, motion=motion, plates=plates,
                   faces=faces, lifecycle=lifecycle, models=models, store=store,
                   tracking=tracking, index_policy=index_policy,
                   log_level=log_level)
