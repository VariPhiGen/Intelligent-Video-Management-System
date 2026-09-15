"""config.py — what this service detects, and how hard it tries.

DELIBERATELY NOT A COPY OF SMART SEARCH'S CONFIG. That file carries the
encoder, the store, retention and the query side, none of which exist here.
This service detects objects, reads plates, tracks them across frames and
decides which observations are worth recording. Everything downstream of the
crop belongs to Smart Search and is configured there.

The values are the ones measured for the pipeline this service takes over, so
behaviour does not change with the split.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Optional

import yaml


@dataclass
class ApiConfig:
    host: str = "127.0.0.1"     # loopback: reached only via the api proxy
    port: int = 8015


@dataclass
class SourceConfig:
    """Where frames come from: the broker, or our own RTSP session.

    BROKER IS THE DEFAULT HERE, unlike the older services. They had a decoder
    before the broker existed and keep it as the fallback; this service is new,
    so the single-decode path is simply how it works. `sampler` remains for a
    deployment that runs no broker.
    """
    frame_source: str = "broker"
    broker_url: str = "redis://127.0.0.1:6379/0"
    broker_channel_prefix: str = "vms.frames"
    #: Only consulted with frame_source=sampler; the broker owns the rate
    #: otherwise, for every consumer at once.
    max_sample_fps: float = 2.0


@dataclass
class DetectConfig:
    confidence: float = 0.35
    #: A crop smaller than this is not worth embedding, so it is not worth
    #: tracking either — see the size filter in pipeline._process.
    min_crop_width: int = 16
    min_crop_height: int = 32
    #: Bounded queue of FRAMES, not detections. Full evicts the OLDEST: for a
    #: forensic index a frame that has waited a minute is worth less than the
    #: one that just arrived, and both describe the same scene.
    queue_size: int = 64
    enabled: bool = True
    #: Drop a box lying this far inside another box of the SAME domain —
    #: intersection over the smaller area, not IoU. The detector's own NMS
    #: cannot catch these because a nested box has a LOW IoU; see
    #: analytics/nested_boxes.py for the measurement behind 0.9. 0 disables it.
    nested_containment: float = 0.9


@dataclass
class MotionConfig:
    """The gate. Only used with frame_source=sampler — in broker mode the
    canonical analysis arrives with the frame and this is never constructed."""
    enabled: bool = True
    threshold: int = 18
    min_area_fraction: float = 0.0002
    dilate_iterations: int = 2
    max_regions: int = 3
    region_padding: float = 0.15
    full_frame_fraction: float = 0.35
    scene_change_fraction: float = 0.55
    despeckle_iterations: int = 1


@dataclass
class TrackingConfig:
    enabled: bool = True
    max_association_cost: float = 1.75
    velocity_alpha: float = 0.5
    confirm_after_seconds: float = 1.0
    max_age_seconds: float = 3.0
    stationary_after_seconds: float = 4.0
    stationary_distance: float = 0.15
    max_tracks_per_key: int = 64
    index_tentative: bool = True


@dataclass
class IndexPolicyConfig:
    """WHICH observations earn a record. Runs before the crop leaves this
    service, so it governs Smart Search's cost as well as its contents."""
    suppression_enabled: bool = True
    displacement_heights: float = 1.5
    scale_change_ratio: float = 1.5
    scale_change_persist: int = 2
    heartbeat_seconds: float = 0
    quality_confidence_gain: float = 0.15
    #: No two records of one track closer than this, whatever changed — see
    #: indexing_policy.decide. 0 = off, the change-driven rules alone.
    min_interval_seconds: float = 0.0


@dataclass
class PlateConfig:
    enabled: bool = True
    model: str = "cct-xs-v2-global-model"
    localiser_weights: str = ""
    localiser_model: str = "yolo-v9-t-384-license-plate-end2end"
    localiser_confidence: float = 0.3
    min_confidence: float = 0.6
    min_length: int = 4
    region_padding: float = 0.12
    two_line_max_aspect: float = 2.2
    two_line_min_row_confidence: float = 0.80
    localiser_square_letterbox: bool = False


@dataclass
class LifecycleConfig:
    """When the weights load, and when they go away again.

    THE MODELS ARE INGEST-ONLY, SO THEY GO WHEN THE CAMERAS GO. That invariant
    is older than this service — it was written down in Smart Search's
    index/models.py, which owned the detector and the plate reader until
    `9f5a5ec` moved them here. The move brought the models and left the
    lifecycle behind, so an appliance with every camera's detection switched
    off went on holding a YOLO pass's worth of weights for ever. This is that
    lifecycle, back where the models now live.

    THE TIMER IS A DEBOUNCE, NOT A TUNING KNOB. camera-mgmt reconciles from
    every uvicorn worker, and a domain or URL change is a remove followed by an
    add, so the registered count touches zero routinely without anybody turning
    anything off. Anything below ~60 s starts unloading and reloading on
    ordinary registry traffic. Set to 0 to keep the pre-hibernation behaviour —
    load once on the first camera, hold for ever — which is what a permanently
    busy site wants.
    """
    idle_timeout_seconds: float = 300.0
    #: How often the reaper checks. Only bounds how LATE a release can be.
    poll_seconds: float = 10.0
    #: Backoff before a failed warm-up is tried again, while cameras are still
    #: registered. A detector that could not be built a minute ago is not much
    #: more likely to build now, and retrying every tick would spend a backend
    #: export attempt each time.
    retry_seconds: float = 60.0


@dataclass
class FaceConfig:
    """The face domain: a second model over every PERSON, the way the plate
    reader is a second model over every vehicle.

    NOT ON BY DEFAULT PER CAMERA — `search_domains` decides, and `face` is not
    in the default set. Measured on this site 2026-09-14: a usable face appears
    on ~4% of person passes overall, 17% on a close indoor camera and 0.2% on a
    distant one, so enabling it everywhere would collect biometric data that
    mostly cannot answer a query.
    """
    enabled: bool = True
    #: WHICH embedder, by name, from analytics/faces.py's registry. Must match
    #: the index's `faces.model`: the name travels with every observation and a
    #: mismatch is refused at the door, because two face models of the same
    #: width still share no vector space.
    model: str = "sface-2021dec"
    #: OpenCV zoo models in the shared models volume. Never committed.
    detector_weights: str = "/models/face_detection_yunet_2023mar.onnx"
    recogniser_weights: str = "/models/face_recognition_sface_2021dec.onnx"
    #: YuNet confidence. See analytics/faces.py — at 0.6 most detections are the
    #: back of a head; 36/36 sampled at 0.85 were real faces.
    score_threshold: float = 0.85
    #: Below this width SFace embeds an upscale of pixels never captured.
    min_width_px: int = 40
    #: More than this inside one person box is a crowd behind the person.
    max_per_crop: int = 2
    #: Skip crops with no colour in them. These models are trained on colour and
    #: measured 0 faces in 99 IR crops on this appliance, so an IR frame is
    #: spend with a known-zero return. False for a site that would rather pay
    #: the detector than trust that measurement on its own cameras.
    require_colour: bool = True


@dataclass
class ModelConfig:
    detector: str = "ultralytics"
    detector_weights: str = "/models/weights/yolov8n.pt"
    device: Optional[str] = None
    detector_backend: str = "auto"
    backend_attempts: int = 3
    models_dir: str = "/models"
    square_letterbox: bool = True


@dataclass
class SinkConfig:
    """Where finished observations go.

    NOT A DATABASE. This service writes no rows: it hands each accepted crop
    and its metadata to Smart Search, which embeds, deduplicates and stores.
    EMPTY URL = produce nothing, which is exactly what phase A runs as: detect,
    track and decide, report the counts on /health, and send nothing anywhere.
    That is what makes this service safe to deploy beside the existing pipeline
    before anything depends on it.
    """
    url: str = ""
    timeout_seconds: float = 10.0


@dataclass
class SnapshotConfig:
    """The whole frame each recorded observation came from, for the feed.

    Display only — the crop is what gets embedded. Downscaled to this width
    (never up) and JPEG-encoded once per frame; 0 sends no frame at all and the
    feed shows the crop instead."""
    frame_width: int = 1280
    frame_quality: int = 75


@dataclass
class ActivitiesConfig:
    """The CPU activity path (analytics/activity_pipeline.py).

    Every frame the broker samples for a camera with a running activity, with
    NO motion gate, through its own detector and tracker. `enabled: false`
    stops the path entirely; activity configuration is still accepted and
    reported.
    """
    enabled: bool = True
    #: Frames waiting for the activity detector. Full evicts the OLDEST.
    queue_size: int = 16


@dataclass
class EventsConfig:
    """Where CPU activity events go: the VMS ingest in camera-mgmt.

    `url` is the API ROOT (the sink appends /api/analytics/events). EMPTY =
    activities still run and are reported on /health, but their events are
    counted and dropped — nothing is written anywhere. The key is the VMS's
    internal service key (X-Internal-Key), the trust camera-mgmt gives its
    sibling services.
    """
    url: str = ""
    internal_api_key: str = ""
    timeout_seconds: float = 10.0
    queue_size: int = 1024
    batch_size: int = 50


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    index_policy: IndexPolicyConfig = field(default_factory=IndexPolicyConfig)
    plates: PlateConfig = field(default_factory=PlateConfig)
    faces: FaceConfig = field(default_factory=FaceConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    sink: SinkConfig = field(default_factory=SinkConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    activities: ActivitiesConfig = field(default_factory=ActivitiesConfig)
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str) -> "AppConfig":
        raw = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

        def sect(name, klass):
            known = {f.name for f in fields(klass)}
            given = raw.get(name) or {}
            return klass(**{k: v for k, v in given.items() if k in known})

        api = sect("api", ApiConfig)
        source = sect("source", SourceConfig)
        detect = sect("detect", DetectConfig)
        motion = sect("motion", MotionConfig)
        tracking = sect("tracking", TrackingConfig)
        index_policy = sect("index_policy", IndexPolicyConfig)
        plates = sect("plates", PlateConfig)
        faces = sect("faces", FaceConfig)
        models = sect("models", ModelConfig)
        sink = sect("sink", SinkConfig)
        lifecycle = sect("lifecycle", LifecycleConfig)
        snapshot = sect("snapshot", SnapshotConfig)
        events = sect("events", EventsConfig)
        activities = sect("activities", ActivitiesConfig)
        if "ANALYTICS_ACTIVITIES_ENABLED" in os.environ:
            activities.enabled = os.environ["ANALYTICS_ACTIVITIES_ENABLED"].lower() not in (
                "0", "false", "no")

        api.host = os.environ.get("ANALYTICS_BIND_HOST", api.host)
        api.port = int(os.environ.get("ANALYTICS_PORT", api.port))

        source.frame_source = os.environ.get(
            "ANALYTICS_FRAME_SOURCE", source.frame_source).strip().lower()
        if source.frame_source not in ("sampler", "broker"):
            raise ValueError(
                "ANALYTICS_FRAME_SOURCE must be 'sampler' or 'broker', "
                f"got {source.frame_source!r}")
        source.broker_url = os.environ.get(
            "ANALYTICS_BROKER_URL", os.environ.get("REDIS_URL", source.broker_url))

        detect.confidence = float(os.environ.get(
            "ANALYTICS_DETECT_CONFIDENCE", detect.confidence))
        detect.nested_containment = float(os.environ.get(
            "ANALYTICS_NESTED_CONTAINMENT", detect.nested_containment))
        if "ANALYTICS_INGEST_ENABLED" in os.environ:
            detect.enabled = os.environ["ANALYTICS_INGEST_ENABLED"].lower() not in (
                "0", "false", "no")

        models.detector_backend = os.environ.get(
            "ANALYTICS_DETECTOR_BACKEND", models.detector_backend).strip().lower()
        models.detector_weights = os.environ.get(
            "ANALYTICS_DETECTOR_WEIGHTS", models.detector_weights)
        models.models_dir = os.environ.get("ANALYTICS_MODELS_DIR", models.models_dir)

        plates.localiser_weights = os.environ.get(
            "ANALYTICS_PLATE_WEIGHTS", plates.localiser_weights)
        # Unset keeps the open baseline; explicitly empty disables plates.
        plates.localiser_model = os.environ.get(
            "ANALYTICS_PLATE_MODEL", plates.localiser_model)

        # Face models. A deployment varies the paths (a mounted volume) and,
        # rarely, the threshold — which is the one number that decides what the
        # feature means. See analytics/faces.py.
        faces.detector_weights = os.environ.get("ANALYTICS_FACE_DETECTOR_WEIGHTS",
                                                faces.detector_weights)
        faces.recogniser_weights = os.environ.get("ANALYTICS_FACE_WEIGHTS",
                                                  faces.recogniser_weights)
        faces.model = os.environ.get("ANALYTICS_FACE_MODEL", faces.model)
        if "ANALYTICS_FACE_SCORE" in os.environ:
            faces.score_threshold = float(os.environ["ANALYTICS_FACE_SCORE"])
        if "ANALYTICS_FACE_REQUIRE_COLOUR" in os.environ:
            faces.require_colour = os.environ[
                "ANALYTICS_FACE_REQUIRE_COLOUR"].lower() not in ("0", "false", "no")
        if "ANALYTICS_FACE_MIN_WIDTH" in os.environ:
            faces.min_width_px = int(os.environ["ANALYTICS_FACE_MIN_WIDTH"])
        if "ANALYTICS_FACES" in os.environ:
            faces.enabled = os.environ["ANALYTICS_FACES"].lower() not in (
                "0", "false", "no")
        sink.url = os.environ.get("ANALYTICS_SINK_URL", sink.url).rstrip("/")

        # The operator's rule, tunable without a rebuild: one look per track
        # per interval. 0 on both restores the change-driven rules alone.
        index_policy.min_interval_seconds = float(os.environ.get(
            "ANALYTICS_INDEX_MIN_INTERVAL_SECONDS", index_policy.min_interval_seconds))
        index_policy.heartbeat_seconds = float(os.environ.get(
            "ANALYTICS_INDEX_HEARTBEAT_SECONDS", index_policy.heartbeat_seconds))

        snapshot.frame_width = max(0, int(os.environ.get(
            "ANALYTICS_FRAME_WIDTH", snapshot.frame_width)))
        snapshot.frame_quality = min(95, max(30, int(os.environ.get(
            "ANALYTICS_FRAME_QUALITY", snapshot.frame_quality))))

        events.url = os.environ.get("ANALYTICS_EVENTS_URL", events.url).strip().rstrip("/")
        events.internal_api_key = os.environ.get(
            "ANALYTICS_INTERNAL_API_KEY",
            os.environ.get("INTERNAL_API_KEY", events.internal_api_key))

        # 0 disables hibernation; negative is the same thing said clumsily, so
        # it is clamped rather than rejected — this must never fail a start.
        if "ANALYTICS_IDLE_TIMEOUT_SECONDS" in os.environ:
            lifecycle.idle_timeout_seconds = max(0.0, float(
                os.environ["ANALYTICS_IDLE_TIMEOUT_SECONDS"]))

        return cls(api=api, source=source, detect=detect, motion=motion,
                   tracking=tracking, index_policy=index_policy, plates=plates,
                   faces=faces, models=models, sink=sink, lifecycle=lifecycle,
                   snapshot=snapshot, events=events, activities=activities,
                   log_level=os.environ.get("LOG_LEVEL", "INFO").upper())
