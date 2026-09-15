"""server.py — headless REST API for the Smart Search index service.

No auth of its own: binds 127.0.0.1 and is reached exclusively through
camera-mgmt's authenticated /api/search proxy — the same trust posture as the
NVR and the motion service.

PHASE 4 serves all six endpoints the VMS calls. Four come through
smartsearch_client; /{domain}/image and /detections are reached directly by
routers/search.py, which is exactly why they are easy to miss — omitting them
does not fail loudly, it produces a results grid with no thumbnails and a
dashboard that 502s.

When the text encoder is unavailable the search routes answer 503, never an
empty result set: "nothing matched" is the one answer a search must never give
wrongly.

THE MODELS COME AND GO UNDER THIS API. They are loaded when the first camera is
registered and released once the last has been gone a while (see
index/models.py), so a search on a quiet deployment loads the encoder as part of
serving it — measured at ~1.7 s warm, against the VMS's 30 s client timeout.
That is why the search routes no longer pre-check a boolean and 503: the load
attempt IS the check, and only a real failure reaches the client as 503.
`/health` reports the lifecycle separately from the capabilities, because
"ingest is enabled" and "ingest is running right now" are different questions
and answering the second in place of the first raises a fault that is not there.
"""
from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from index import hardware
from PIL import Image

from index.config import AppConfig
from index.engine import IndexEngine
from index.frame_boxes import clean as clean_frame_boxes
from index.models import ModelPool
from index.pipeline import ObservationInput
from index.domains import CAMERA_DOMAINS, DOMAINS, FACE, is_domain
from index.faces import MAX_QUERY_PHOTOS, fuse
from index.queries import EncoderUnavailable, PERSON, VEHICLES, QueryService
from index.store import Store

log = logging.getLogger("smartsearch.api")

#: A whole-frame JPEG for the feed is tens of KB. Anything near this is a
#: mistake rather than a picture, and is dropped rather than stored.
_MAX_FRAME_BYTES = 4 * 1024 * 1024


def _parse_window(from_: str, to: str) -> tuple[datetime, datetime]:
    """Strict ISO 8601 window for an erasure. Raises ValueError, never guesses.

    queries._parse_when returns None on unparseable input, which is right for an
    optional search filter — the filter is simply not applied. It is exactly
    wrong here: a dropped bound would silently widen an erasure to "all of time"
    or void it entirely, and either way the caller is told the erasure
    succeeded. So this one raises.

    A naive timestamp is read as UTC. The VMS always sends Z-suffixed times, but
    a hand-issued call might not, and comparing a naive value against timestamptz
    would otherwise resolve against whatever the database's timezone happens to
    be — an ambiguity an erasure cannot afford.
    """
    parsed = []
    for label, raw in (("from", from_), ("to", to)):
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label}={raw!r} is not an ISO 8601 timestamp") from exc
        parsed.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    start, end = parsed
    if end < start:
        raise ValueError(f"to ({to}) is before from ({from_})")
    return start, end


class PeopleQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=24, ge=1, le=500)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    sensor_id: Optional[str] = None
    sensor_ids: Optional[list[str]] = None
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    #: WHICH field orders the page, and in which direction. The other field is
    #: not consulted at all — see queries.Order. "time" + "asc" (oldest first)
    #: is also bounded to the last 30 days unless an explicit range says
    #: otherwise; see queries._OLDEST_FIRST_DAYS.
    sort_by: str = Field(default="time", pattern="^(time|confidence)$")
    sort_dir: str = Field(default="desc", pattern="^(desc|asc)$")


class VehiclesQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=24, ge=1, le=500)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    plate: Optional[str] = None
    vehicle_type: Optional[str] = None
    color: Optional[str] = None
    camera_ids: Optional[list[str]] = None
    #: ISO-8601 bounds. An explicit range REPLACES the recent window a search
    #: otherwise starts from — see queries._recent_first.
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    #: WHICH field orders the page, and in which direction. The other field is
    #: not consulted at all — see queries.Order. "time" + "asc" (oldest first)
    #: is also bounded to the last 30 days unless an explicit range says
    #: otherwise; see queries._OLDEST_FIRST_DAYS.
    sort_by: str = Field(default="time", pattern="^(time|confidence)$")
    sort_dir: str = Field(default="desc", pattern="^(desc|asc)$")



def _validated_embedding(meta: dict, active_model: Optional[str] = None) -> Optional[list]:
    """The producer-supplied vector, checked against the domain's dimension AND
    against the model this index is running.

    A domain that supplies its own vector MUST supply one: silently embedding
    it here instead would put a 512-dim CLIP vector in a 128-dim column and
    fail at the INSERT, where write_batch swallows failures by design — so the
    observation would vanish with nothing but a log line.

    THE MODEL NAME IS CHECKED, NOT JUST THE WIDTH, and that is the half that
    matters during an upgrade. Two face models can both be 128-dim and share no
    vector space at all; a fleet where analytics has been upgraded and the index
    has not would write vectors that INSERT cleanly, rank plausibly, and mean
    nothing. Rejecting at the door names both models, which is a fixable
    message rather than a slow corruption.
    """
    domain = str(meta.get("domain") or "")
    raw = meta.get("embedding")
    if raw is None:
        if is_domain(domain) and DOMAINS[domain].supplies_own_vector:
            raise HTTPException(
                422, f"domain {domain!r} must carry its own 'embedding'")
        return None
    sent_model = meta.get("embedding_model")
    if active_model and sent_model and sent_model != active_model:
        raise HTTPException(
            422, f"observation was embedded by {sent_model!r} but this index "
                 f"runs {active_model!r}. Vectors from two models are not "
                 f"comparable; upgrade both sides and re-index.")
    if not isinstance(raw, (list, tuple)):
        raise HTTPException(422, "'embedding' must be a list of numbers")
    if is_domain(domain):
        want = DOMAINS[domain].dims
        if len(raw) != want:
            raise HTTPException(
                422, f"'embedding' for {domain!r} must have {want} values, "
                     f"got {len(raw)}")
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"'embedding' is not numeric: {exc}") from exc


def create_app(engine: IndexEngine, config: AppConfig, store: Store,
               queries: QueryService, pool: ModelPool,
               retention=None) -> FastAPI:
    app = FastAPI(
        title="VMS Smart Search Index",
        description=(
            "Forensic person/vehicle search index: camera registry, ingest "
            "pipeline and the query API the VMS speaks."
        ),
        version="0.4.0",
    )

    @app.exception_handler(EncoderUnavailable)
    def _encoder_unavailable(request, exc: EncoderUnavailable) -> JSONResponse:
        """503, in one place. Every text search reaches the encoder through the
        pool, so every one of them can fail this way, and each route repeating
        the check is how one of them ends up not repeating it."""
        return JSONResponse(
            status_code=503,
            content={"detail": (
                f"{exc} Ingest and the camera registry are unaffected."
            )},
        )

    @app.get("/health")
    def health() -> dict:
        db = store.health()
        cameras = engine.snapshot()
        by_state: dict[str, int] = {}
        for c in cameras:
            by_state[c["state"]] = by_state.get(c["state"], 0) + 1
        producers = engine.producers_snapshot()
        # TOP-LEVEL, because a monitor should not have to know which nested key
        # to read. `status` stays "ok" on purpose: this process IS serving, and
        # search over what is already indexed works fine with no producer —
        # flipping it would also change behaviour in camera-mgmt, which gates
        # on status == "ok". A warning says the true thing without that.
        warnings = []
        if producers.get("state") != "RECEIVING":
            warnings.append({
                "code": producers["state"],
                "detail": producers.get("detail"),
                "impact": "the index is not growing; existing rows are still "
                          "searchable",
            })
        return {
            # "ok" means this process is serving. It deliberately does NOT mean
            # search works — see `capabilities`, which is the honest answer —
            # nor that anything is being INDEXED, which is `warnings`.
            "status": "ok",
            "warnings": warnings,
            "phase": "4-query",
            "total_cameras": len(cameras),
            "by_state": by_state,
            "capabilities": {
                # "will this happen", not "is it happening". A hibernating
                # service has both of these true and no models loaded.
                "camera_registry": True,
                "ingest": engine.ingest_enabled,
                "query_api": queries.available,
            },
            # "is it happening", and the honest answer when it is not. Carries
            # `inference`: the backend, runtime and RESOLVED device each model
            # loaded on. That was previously knowable only from a boot log line,
            # so "is this running on the GPU?" could not be answered over HTTP.
            "lifecycle": pool.snapshot(),
            # Whether anything is actually bounding the index. `retention_days`
            # under `config` is only the stamp put on new rows; this is the
            # sweep that acts on it, and the two were unconnected until the
            # thread existed. `enabled: false` here means the index grows
            # forever unless someone runs scripts/retention.py.
            "retention": retention.snapshot() if retention else None,
            "ingest": engine.pipeline.snapshot() if engine.pipeline else None,
            # Which decoder is feeding ingest. Reported even when the
            # pipeline is hibernating, because "where do frames come
            # from" is a question about configuration, not about
            # whether the models happen to be resident.
            "frame_source": engine.frame_source_snapshot(),
            # WHO IS FEEDING THIS INDEX. Reported next to the frame source
            # because together they answer the whole question: this service
            # pulls nothing, so if no producer is live the index cannot grow
            # however healthy everything else looks.
            "ingest_source": producers,
            "database": db,
            "config": {
                # NOT a sample rate any more: nothing here samples. Kept as
                # the value the analytics service is expected to run at, so a
                # mismatch between the two is visible in one place.
                "expected_source_fps": config.ingest.max_sample_fps,
                "dedup_window_seconds": config.ingest.dedup_window_seconds,
                "dedup_cosine_threshold": config.ingest.dedup_cosine_threshold,
                "retention_days": config.store.retention_days,
            },
        }

    @app.get("/diagnostics/hardware")
    def diagnostics_hardware() -> dict:
        """What this machine can run inference on, and what it is running on.

        SEPARATE FROM /health DELIBERATELY. The container healthcheck calls
        /health every 30 s with a 5 s timeout, so it must stay small and cheap;
        this probes runtimes and reads CPU flags, which is neither. It is also
        the endpoint that answers the operator question /health cannot: not
        "which device did we pick" but "which devices and runtimes were there
        to pick from".

        `fingerprint` is the identity a calibration profile will be valid for.
        Exposed now, ahead of calibration itself, because comparing two
        appliances' fingerprints is how "why is that one slower" gets answered.
        """
        return {
            "hardware": hardware.snapshot(),
            "fingerprint": hardware.fingerprint({
                "detector": config.models.detector_weights,
                **({"plate_localiser": config.plates.localiser_weights}
                   if config.plates.localiser_weights else {}),
            }).to_dict(),
            "inference": pool.inference_snapshot(),
            "gpu_utilisation": hardware.gpu_utilisation(),
        }

    @app.get("/cameras")
    def list_cameras() -> dict:
        cameras = engine.snapshot()
        return {"total": len(cameras), "cameras": cameras}

    @app.get("/cameras/{name}")
    def get_camera(name: str) -> dict:
        cam = engine.get_camera(name)
        if cam is None:
            raise HTTPException(404, f"Camera '{name}' not found")
        return engine.describe(cam)

    @app.post("/cameras/{name}")
    def add_camera(
        name: str,
        rtsp_url: str = Query(..., description="Relay URL to index from"),
        domains: Optional[list[str]] = Query(
            default=None,
            description="Any of " + " / ".join(CAMERA_DOMAINS)
                        + "; default person + vehicles",
        ),
        retention_days: Optional[int] = Query(
            default=None, ge=1, le=3650,
            description="How long crops from this camera stay searchable. "
                        "Omit to follow the appliance default. Should be the "
                        "camera's RECORDING retention: the index must not "
                        "outlive the footage it describes.",
        ),
    ) -> dict:
        # FROM THE REGISTRY. This filter was a literal set, and it silently
        # dropped `face` from every camera the VMS registered — the registry
        # accepted the camera, answered 200, and indexed two domains out of
        # three. Nothing logged, and the only symptom was an empty gallery.
        # A hard-coded list here is the fifth surface of the same bug class
        # index/domains.py exists to close; it is the one that still had to be
        # found by running the thing.
        #
        # CAMERA_DOMAINS, not ALL_DOMAINS: `plate` is a camera setting rather
        # than a table, so the storage list is one short of the vocabulary a
        # camera speaks. See index/domains.py.
        allowed = set(CAMERA_DOMAINS)
        picked = [d for d in (domains or []) if d in allowed]
        if domains and not picked:
            raise HTTPException(
                422, f"no valid domain in {domains}; expected any of {sorted(allowed)}"
            )
        # UPSERT, not create-or-409. The registry reconciles from four uvicorn
        # workers concurrently; an idempotent instruction makes that a
        # non-event, where create-or-409 plus re-add interleaved into a camera
        # that was registered but not being sampled.
        entry, outcome = engine.upsert_camera(
            name, rtsp_url, tuple(picked) if picked else ("person", "vehicles"),
            retention_days,
        )
        # AFTER the registry accepted it, and unconditionally rather than only
        # on outcome == "updated": four workers race here and only one of them
        # sees the transition, so gating the restamp on the outcome would drop
        # it whenever another worker won. set_camera_retention is a no-op once
        # the deadline already matches, which is what makes that affordable.
        restamp = store.set_camera_retention(name, retention_days)
        return {"status": "ok", "camera": name,
                "outcome": outcome, "domains": list(entry.domains),
                "retention_days": retention_days,
                # Surfaced so an operator who shortens retention can see the
                # existing rows move, rather than having to trust that they did.
                "rows_restamped": restamp["rows_restamped"],
                "retention_error": restamp["error"]}

    # ── the six endpoints the VMS calls ──────────────────────────────────────

    @app.post("/producers/{name}/heartbeat")
    async def producer_heartbeat(name: str, request: Request) -> dict:
        """A producer saying it is alive, whether or not it detected anything.

        THE POINT IS THE QUIET CASE. Observations stop for two very different
        reasons — nothing is happening, or nothing is running — and from this
        side they look identical. A camera watching an empty yard produces
        nothing all night and is perfectly healthy; a producer that was never
        started produces nothing and is a total outage. This is what tells
        them apart, so /health can say which one it is.
        """
        try:
            stats = await request.json()
        except Exception:                                    # noqa: BLE001
            stats = {}
        if not isinstance(stats, dict):
            stats = {}
        engine.note_producer(name, stats)
        return {"ok": True, "producer": name}

    # ── ingest from the analytics service ────────────────────────────────

    def _active_face_model() -> Optional[str]:
        """The face model this index is running, or None when face search is
        not set up here. Read from the pool WITHOUT forcing a load: ingest must
        not pull 37 MB of SFace off disk on the first observation, and a
        deployment with no face cameras never loads it at all."""
        return pool.configured_face_model

    @app.post("/observations")
    async def ingest_observations(request: Request) -> dict:
        """Take one detected object and index it.

        THE SEAM, AS AN ENDPOINT. Detection, plates, tracking and the indexing
        policy live in the analytics service now; everything below the crop —
        the CLIP pass, appearance deduplication, the crop on disk and the row —
        lives here, because it needs the encoder and the store. The caller has
        already decided this observation is worth recording.

        It reaches the SAME method the in-process path calls
        (IngestPipeline.ingest_observations), not a parallel copy, so the two
        cannot drift apart the way two motion implementations once did.

        PNG, NOT JPEG, on the wire. The crop is embedded before it is saved, so
        a lossy hop here would change the vector and therefore the dedup
        decision — the one thing that must stay identical while both paths run
        side by side. The crop is written to disk as JPEG afterwards exactly as
        before; only the transport is lossless.
        """
        pipeline = engine.pipeline
        if pipeline is None:
            # Warming, hibernating, or ingest disabled. 503 rather than a
            # silent 200: the caller must be able to retry rather than believe
            # an observation was stored.
            raise HTTPException(status_code=503, detail="ingest not ready")

        form = await request.form()
        raw = form.get("meta")
        upload = form.get("crop")
        if raw is None or upload is None:
            raise HTTPException(status_code=422,
                                detail="expected 'meta' and 'crop' parts")
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"bad meta: {exc}")

        for required in ("camera", "ts", "domain"):
            if required not in meta:
                raise HTTPException(status_code=422,
                                    detail=f"meta is missing {required!r}")

        try:
            crop = Image.open(io.BytesIO(await upload.read()))
            crop.load()
            crop = crop.convert("RGB")
        except Exception as exc:                            # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"bad crop: {exc}")

        # The whole frame, for the Recent-detections feed. Optional — an older
        # producer sends none — and never a reason to refuse the observation:
        # a frame that is too big or not a JPEG is dropped, and the row keeps
        # its crop.
        frame_jpeg = None
        frame_part = form.get("frame")
        if frame_part is not None and hasattr(frame_part, "read"):
            data = await frame_part.read()
            if data and len(data) <= _MAX_FRAME_BYTES and data[:2] == b"\xff\xd8":
                frame_jpeg = data

        item = ObservationInput(
            slug=str(meta["camera"]),
            crop=crop,
            ts=float(meta["ts"]),
            domain=str(meta["domain"]),
            confidence=float(meta.get("confidence", 0.0)),
            label=str(meta.get("label") or meta["domain"]),
            plate=meta.get("plate"),
            plate_confidence=meta.get("plate_confidence"),
            bbox=tuple(meta.get("bbox") or (0.0, 0.0, 1.0, 1.0)),
            tracker_id=meta.get("tracker_id"),
            frame_jpeg=frame_jpeg,
            # Every object the frame held, for the feed to draw. Only with the
            # picture they belong to, and rebuilt field by field on the way in
            # — see index/frame_boxes.py.
            frame_boxes=clean_frame_boxes(meta.get("frame_boxes")) if frame_jpeg else None,
            # Faces arrive already embedded — SFace, 128-dim, computed by the
            # producer off the RAW frame. Validated here rather than at the
            # INSERT because a wrong-length vector is a producer bug and should
            # be answered 422 to the producer, not logged as a database error
            # three layers down. See index/domains.py for why it is not
            # embedded on this side.
            embedding=_validated_embedding(meta, _active_face_model()),
            embedding_model=(str(meta["embedding_model"])
                             if meta.get("embedding_model") else None),
            face_width_px=(int(meta["face_width_px"])
                           if meta.get("face_width_px") is not None else None),
        )
        written = pipeline.ingest_observations([item])
        # written == 0 is a NORMAL outcome, not a failure: appearance dedup
        # rejected it as an object already represented in this window. Saying
        # so lets the caller graph the split rather than guess at it.
        return {"accepted": True, "rows_written": written,
                "deduped": written == 0}

    @app.post("/person/search")
    def person_search(body: PeopleQuery) -> dict:
        return {"results": queries.search_people(
            query=body.query, top_k=body.top_k,
            score_threshold=body.score_threshold,
            sensor_id=body.sensor_id, sensor_ids=body.sensor_ids,
            time_from=body.time_from, time_to=body.time_to,
            sort_by=body.sort_by, sort_dir=body.sort_dir,
        )}

    # 8 MB: an annotation crop is tens of KB; even a full 4K frame as JPEG fits
    # several times over. Anything bigger is a mistake, not a query.
    _MAX_QUERY_IMAGE_BYTES = 8 * 1024 * 1024

    @app.post("/person/search_by_image")
    async def person_search_by_image(
        request: Request,
        top_k: int = Query(default=24, ge=1, le=500),
        score_threshold: float = Query(default=0.0, ge=0.0, le=1.0),
        sensor_id: Optional[str] = Query(default=None),
        sensor_ids: Optional[list[str]] = Query(default=None),
        time_from: Optional[str] = Query(default=None),
        time_to: Optional[str] = Query(default=None),
        sort_by: str = Query(default="time", pattern="^(time|confidence)$"),
        sort_dir: str = Query(default="desc", pattern="^(desc|asc)$"),
    ) -> dict:
        """Search-by-example: the request BODY is the query image (raw JPEG/PNG
        bytes, not multipart — deliberately, so this service needs no multipart
        parser), filters ride the query string like /plates/search. Same vector
        space, same SQL, same scoring as /person/search — see
        queries.search_people_by_image."""
        image = await request.body()
        if not image:
            raise HTTPException(400, "empty body — send the query image as raw bytes")
        if len(image) > _MAX_QUERY_IMAGE_BYTES:
            raise HTTPException(413, "query image exceeds 8 MB")
        try:
            results = queries.search_people_by_image(
                image=image, top_k=top_k, score_threshold=score_threshold,
                sensor_id=sensor_id, sensor_ids=sensor_ids,
                time_from=time_from, time_to=time_to,
                sort_by=sort_by, sort_dir=sort_dir,
            )
        except ValueError as exc:   # undecodable bytes — caller's fault, not an outage
            raise HTTPException(400, str(exc)) from exc
        return {"results": results}

    def _photo_name(n: int, single: bool) -> str:
        return "image" if single else f"photo {n}"

    async def _read_query_photos(request: Request) -> list[bytes]:
        """The query photos: the raw body for one, multipart `photos` for several.

        BOTH SHAPES, so the VMS and this index can be upgraded in either order.
        A VMS that predates multi-photo search sends one photo as the body, as
        it always has, and gets the answer it always got. (The person image
        search stays body-only; python-multipart is already a dependency here.)
        """
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            # One file past the limit is parsed so "too many photos" is reported
            # as that; a flood of parts still stops inside the parser.
            form = await request.form(max_files=MAX_QUERY_PHOTOS + 1, max_fields=8)
            try:
                images: list[bytes] = []
                for part in form.getlist("photos"):
                    if isinstance(part, str):
                        raise HTTPException(400, "each `photos` part must be a file")
                    images.append(await part.read())
            finally:
                await form.close()
        else:
            body = await request.body()
            images = [body] if body else []
        if not images:
            raise HTTPException(400, "empty body — send the query image as raw bytes, "
                                     f"or up to {MAX_QUERY_PHOTOS} as multipart `photos` parts")
        if len(images) > MAX_QUERY_PHOTOS:
            raise HTTPException(400, f"at most {MAX_QUERY_PHOTOS} photos per face search")
        single = len(images) == 1
        for n, image in enumerate(images, start=1):
            if not image:
                raise HTTPException(400, f"{_photo_name(n, single)} is empty")
            if len(image) > _MAX_QUERY_IMAGE_BYTES:
                raise HTTPException(413, f"query {_photo_name(n, single)} exceeds 8 MB")
        return images

    @app.post("/faces/search_by_image")
    async def faces_search_by_image(
        request: Request,
        top_k: int = Query(default=24, ge=1, le=500),
        score_threshold: float = Query(default=0.0, ge=0.0, le=1.0),
        camera_ids: Optional[list[str]] = Query(default=None),
        time_from: Optional[str] = Query(default=None),
        time_to: Optional[str] = Query(default=None),
        min_width_px: int = Query(default=0, ge=0, le=4096),
    ) -> dict:
        """Search by photograph. Body is the raw image, like /person/search_by_image
        — or, for up to MAX_QUERY_PHOTOS photos of the SAME person, multipart
        `photos` parts, pooled into one query by faces.fuse. The response then
        reports each photo (`photos`: was a face found and used) and
        `agreement`, the least similar pair, because two different people
        averaged together rank plausibly and match neither.

        THE ONLY QUERY THIS DOMAIN HAS. There is no text route for faces and
        there will not be one: the vectors are SFace, which has no text
        encoder, so a phrase — and above all a NAME — cannot be turned into a
        face vector. The person domain had to grow a warning for name-shaped
        queries because its scores looked plausible; here the shape of the API
        is the warning.

        `faces_detected` is returned even when the answer is empty. An operator
        who uploads a photo with no findable face, or a face smaller than the
        floor, must be told THAT rather than "no matches" — they are different
        problems and only one of them is about the index.
        """
        images = await _read_query_photos(request)
        encoder = pool.face_encoder()
        if encoder is None or not encoder.active:
            # 503, not an empty 200: face search is unavailable here, which is
            # not the same answer as "nobody matches".
            raise HTTPException(503, "face search is not available on this "
                                     "deployment (models not installed)")
        import cv2
        import numpy as _np
        single = len(images) == 1
        chosen = []
        photos: list[dict] = []
        faces_detected = 0
        for n, image in enumerate(images, start=1):
            try:
                frame = cv2.imdecode(_np.frombuffer(image, dtype=_np.uint8),
                                     cv2.IMREAD_COLOR)
            except Exception as exc:                              # noqa: BLE001
                raise HTTPException(
                    400, f"could not decode {_photo_name(n, single)}: {exc}") from exc
            if frame is None:
                # A broken upload is refused, naming the photo. A photo that
                # decodes but holds no face is NOT — that is a hard photograph,
                # and the rest of the set can still answer.
                raise HTTPException(400, f"could not decode {_photo_name(n, single)}")
            faces = encoder.detect(frame)
            faces_detected += len(faces)
            best = faces[0] if faces else None
            # One report per photo, in upload order, so the operator is told
            # WHICH photograph was no use, not merely that one was not.
            photos.append({"photo": n, "faces_detected": len(faces),
                           "used": best is not None,
                           "score": best.score if best else None,
                           "width_px": best.width_px if best else None})
            if best is not None:
                chosen.append(best)
        if not chosen:
            where = "the uploaded photo" if single else f"any of the {len(images)} photos"
            return {"results": [], "faces_detected": 0,
                    "photos": photos, "photos_used": 0,
                    "detail": f"no face found in {where} at "
                              f"score >= {encoder.score_threshold:.2f} and "
                              f"width >= {encoder.min_width_px}px"}
        # Each photo contributes its best face; with one photo this is that
        # face's own vector. See faces.fuse for the rule and its limits.
        vector, agreement = fuse([f.embedding for f in chosen])
        results = queries.search_faces(
            vector=vector, top_k=top_k, score_threshold=score_threshold,
            camera_ids=camera_ids, time_from=time_from, time_to=time_to,
            min_width_px=min_width_px, model=encoder.name,
        )
        return {"results": results, "faces_detected": faces_detected,
                "model": encoder.name,
                "query_face": {"score": chosen[0].score,
                               "width_px": chosen[0].width_px},
                "photos": photos, "photos_used": len(chosen),
                "agreement": agreement}

    @app.get("/faces/recent")
    def faces_recent(
        camera_ids: Optional[list[str]] = Query(default=None),
        limit: int = Query(default=60, ge=1, le=200),
        min_width_px: int = Query(default=0, ge=0, le=4096),
    ) -> dict:
        """The gallery the Faces tab opens on, before anyone uploads anything."""
        # Scoped to the model a search could actually use, when one is loaded.
        # With no encoder the gallery still lists what was collected — browsing
        # history must not require the model files to be present.
        return {"results": queries.recent_faces(
            limit=limit, camera_ids=camera_ids, min_width_px=min_width_px,
            model=pool.configured_face_model)}

    @app.get("/faces/stats")
    def faces_stats(camera_ids: Optional[list[str]] = Query(default=None)) -> dict:
        out = queries.stats(FACE, camera_ids)
        # Lets the UI say "face search is not set up here" rather than showing
        # an empty gallery that looks like a site where nobody has a face.
        out["faces_available"] = pool.faces_available
        out["model"] = pool.configured_face_model
        # Rows per model: during a re-index this is the progress bar, and
        # outside one it should have exactly one key.
        try:
            out["rows_by_model"] = queries.face_models()
        except Exception:                                         # noqa: BLE001
            # A stats call must not fail because of a reporting extra.
            out["rows_by_model"] = None
        return out

    @app.post("/vehicles/search")
    def vehicles_search(body: VehiclesQuery) -> dict:
        return {"results": queries.search_vehicles(
            query=body.query, top_k=body.top_k,
            score_threshold=body.score_threshold, plate=body.plate,
            vehicle_type=body.vehicle_type, color=body.color,
            camera_ids=body.camera_ids,
            time_from=body.time_from, time_to=body.time_to,
            sort_by=body.sort_by, sort_dir=body.sort_dir,
        )}

    @app.get("/plates/search")
    def plates_search(
        plate: str = Query(..., min_length=1, max_length=32),
        camera_ids: Optional[list[str]] = Query(default=None),
        time_from: Optional[str] = Query(default=None),
        time_to: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> dict:
        """Plate lookup. NO encoder required — see queries.search_plates. This
        is the one search that keeps working when the model cannot be loaded."""
        return queries.search_plates(
            plate=plate, camera_ids=camera_ids,
            time_from=time_from, time_to=time_to, limit=limit,
        )

    @app.get("/person/stats")
    def person_stats(sensor_ids: Optional[list[str]] = Query(default=None)) -> dict:
        return queries.stats(PERSON, sensor_ids)

    @app.get("/vehicles/stats")
    def vehicles_stats(camera_ids: Optional[list[str]] = Query(default=None)) -> dict:
        # camera_ids, not sensor_ids: the vehicles domain names it differently
        # on the wire and the client sends the name this domain expects.
        return queries.stats(VEHICLES, camera_ids)

    @app.get("/{domain}/image")
    def crop_image(domain: str, id: str = Query(...)) -> Response:
        # Registry, not a literal pair: a domain that stores crops but is not
        # listed here has rows nobody can see the image for. See index/domains.py.
        if not is_domain(domain):
            raise HTTPException(404, "unknown domain")
        path = queries.crop_path(domain, id)
        if path is None:
            raise HTTPException(404, "no such crop")
        # The row outlived its file — retention deletes rows first, so this
        # window is small but real. 404 is the truth; a 500 would suggest the
        # service is broken when the crop is simply gone.
        #
        # CHECKED, not caught. This was a `try: FileResponse(...) except
        # OSError:` and the except was dead code: FileResponse does not stat
        # the path when it is constructed, only when Starlette streams it —
        # which happens after the handler has returned, far outside the try.
        # Every missing crop was therefore a 500. A stat still leaves a window
        # (the file can vanish between here and the send) but it is orders of
        # magnitude narrower, and it is the one this endpoint actually hits.
        #
        # SIZE, not just existence. A crop written before a power cut can be on
        # disk at zero bytes (80 of them on this appliance before writer.py made
        # the write durable), and FileResponse serves those as a 200 with an
        # empty body — which renders as a broken image and reads downstream as
        # "the index is fine, this detection just looks like nothing". A 404
        # says the same thing as a deleted crop, which is what it is.
        if not os.path.isfile(path):
            raise HTTPException(404, "crop file is no longer on disk")
        try:
            size = os.path.getsize(path)
        except OSError:
            raise HTTPException(404, "crop file is no longer on disk")
        if size == 0:
            raise HTTPException(404, "crop file is empty")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/{domain}/frame")
    def frame_image(domain: str, id: str = Query(...)) -> Response:
        """The whole frame a detection came from, for the Recent-detections feed.

        The box is drawn by the UI from the row's bbox; this file is exactly
        what the camera saw. 404 for a row written before frames were kept, or
        whose frame has aged out (store.frame_retention_days) — both ordinary,
        and the UI falls back to the crop for either.

        Existence is checked HERE rather than trusted to FileResponse, which
        only opens the file while sending and turns a missing one into a 500.
        Frames age out routinely, so that would be a steady stream of errors
        for the normal case.
        """
        if domain not in (PERSON, VEHICLES):
            raise HTTPException(404, "unknown domain")
        path = queries.frame_path(domain, id)
        if path is None:
            raise HTTPException(404, "no frame for this detection")
        if not os.path.isfile(path):
            raise HTTPException(404, "frame has aged out")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/detections")
    def detections(
        since_ms: int = Query(default=0, ge=0),
        limit: int = Query(default=60, ge=1, le=500),
        tz_offset_min: int = Query(default=0, ge=-900, le=900),
        camera_ids: Optional[list[str]] = Query(default=None),
        domain: Optional[list[str]] = Query(
            default=None, description="Narrow the FEED to these domains. The "
                                      "summary is always over all of them."),
    ) -> dict:
        picked = [d for d in (domain or []) if d in (PERSON, VEHICLES)]
        return queries.detections(
            since_ms=since_ms, limit=limit, tz_offset_min=tz_offset_min,
            camera_ids=camera_ids, domains=picked or None,
        )

    @app.delete("/cameras/{name}")
    def remove_camera(name: str) -> dict:
        """Unregister a camera from ingest.

        Deliberately leaves indexed rows alone: a camera removed from the
        registry (renamed, moved, decommissioned) must not silently destroy the
        footage index built from it. Erasing what a camera recorded is
        DELETE /erase, which is an explicit, audited act — see below.
        """
        if not engine.remove_camera(name):
            raise HTTPException(404, f"Camera '{name}' not found")
        return {"status": "ok", "camera": name}

    @app.delete("/erase")
    def erase(
        camera: str = Query(..., description="VMS camera slug"),
        from_: str = Query(..., alias="from", description="ISO 8601 start, inclusive"),
        to: str = Query(..., description="ISO 8601 end, inclusive"),
    ) -> dict:
        """Scoped erasure: destroy every indexed crop for one camera in a window.

        This is the search index's half of a data-subject erasure. The VMS
        erases NVR footage for the same camera and window; without this the
        recording is gone while the crops cut from it stay searchable and stay
        on disk, and the request completes reporting success. The whole point of
        indexing in-house was that this is now ours to delete.

        Not symmetric with the NVR's erasure, and it does not need to be. The
        NVR preserves segments that only PARTIALLY overlap the window, because a
        segment is a file covering a span and cannot be cut without re-encoding.
        A crop is a point in time: it is inside the window or it is not, so
        nothing here is skipped or approximated.

        Idempotent, so a retry after a partial failure is safe.

        `crops_failed` > 0 means rows are gone but images remain on disk — an
        INCOMPLETE erasure. It is reported rather than raised because the row
        deletions did happen and a caller that retries should not undo them; the
        caller decides, and the VMS treats it as a failed erasure.
        """
        try:
            start, end = _parse_window(from_, to)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            result = store.erase_range(camera, start, end)
        except Exception as exc:
            # Never a 200. A caller reading this response decides whether to
            # tell a data subject their footage is destroyed.
            log.exception("erase failed camera=%s", camera)
            raise HTTPException(503, f"Erasure failed: {exc}") from exc
        return {
            "status": "ok" if not result["crops_failed"] else "incomplete",
            "camera": camera, "from": start.isoformat(), "to": end.isoformat(),
            **result,
        }

    return app
