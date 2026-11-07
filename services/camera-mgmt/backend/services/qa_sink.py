"""qa_sink.py — publish operator-drawn annotations to an external QA queue.

An operator draws a box on playback; the outbox delivers it through Kafka or
an HTTPS webhook. Media preparation is shared. annotation_delivery handles the
general JSON format and webhook transport. The legacy payload builder below
preserves the contract of the existing external review application.

THREE RULES THE LEGACY REMOTE SCHEMA IMPOSES, none of them negotiable here:

  1. `org_img`, `video_url` and `org_img_local` are each UNIQUE and NOT NULL
     over there. An annotation has one image and no video at all, so the two
     it does not have still need values that are unique per annotation. They
     are derived from the annotation's UUID, which makes them unique for free
     and makes a redelivered message collide instead of duplicating — the
     remote unique index becomes our idempotency check.
  2. Bounding boxes travel as PERCENTAGES of the frame, top-left origin. Ours
     are absolute pixels, top-left origin (the same convention the COCO export
     uses), so the conversion is a divide — but it is a divide by a frame size
     that must be right, because nothing downstream can detect a box drawn at
     the wrong scale. It just looks like a bad annotation.
  3. `imgsz` is parsed by splitting on a COLON, not an "x". Their own README
     documents "1920x1080" and their code reads "1920:1080". The code wins.

WHY THE PAYLOAD TOPIC IS NOT THE KAFKA TOPIC. The message carries a `topic`
field that the consumer uses to decide what to do with the event, separately
from the Kafka topic it arrived on. Sending a payload topic the consumer does
not recognise is how this integration stays read-only over there: unrecognised
topics skip the consumer's vision-model check and skip its auto-approval, so
the annotation lands in the human review queue and waits, which is exactly what
it is for. A payload topic that IS recognised would hand an operator's
ground-truth box to a model for a second opinion. Do not "fix" this by making
the two topics match.

NETWORK I/O HAPPENS HERE, NOT IN THE REQUEST. `enqueue` writes one row in the
caller's transaction and returns. Fetching the frame, mirroring it to object
storage and producing to Kafka all happen in `relay_loop`, because this
appliance sits behind somebody else's uplink and an operator saving a box must
never wait on it — or lose the box to it.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog
from sqlalchemy import select

from ..crypto import decrypt
from ..db import AsyncSessionLocal
from ..models import (Annotation, Camera, LabelClass, QaPublishOutbox,
                      QaSinkConfig)
from . import image_label, nvr_client, s3_upload
from .annotation_delivery import DeliveryError, post_webhook, serialize

log = structlog.get_logger(__name__)

# How often the relay looks for work. Short enough that a reviewer sees a box
# within a minute of it being drawn, long enough that an appliance with nothing
# to publish is not opening a Kafka connection every few seconds.
_INTERVAL_SECONDS = 20

# Rows per pass. Caps how long one pass holds a producer open and how much of a
# backlog a single failure can stall behind it.
_BATCH = 25

# Footage cut either side of the annotated frame for the published clip.
_VIDEO_SECONDS_EACH_SIDE = 5

# After this many failures the row is parked as `failed` and stops consuming
# relay passes. It is not deleted: `failed` with `last_error` is a question an
# operator can answer, and a silently dropped annotation is not.
_MAX_ATTEMPTS = 10

# Retry backoff, doubling from 30s. Capped at ten minutes — an uplink that has
# been down for an hour is not helped by hour-long waits, and the cap keeps
# recovery prompt once it returns.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 600

# Stamped on the annotation when it is published. `posture.derived_stills`
# counts stills held with no deadline, and this is the sentence that answers
# "why is this one of them?".
KEPT_REASON = "Published to QA review"


# ── Configuration ────────────────────────────────────────────────────────────


async def get_config(db) -> Optional[QaSinkConfig]:
    """The singleton config row, or None on an appliance that has never set one."""
    return await db.get(QaSinkConfig, 1)


def credentials_for(config: QaSinkConfig) -> Optional[s3_upload.Credentials]:
    """The sink's own S3 identity, or None to sign with the environment key.

    None is the supported default, not a degraded one: an appliance whose
    dataset-mirror key already reaches the review bucket needs nothing here.
    A key id with no secret is treated as "no credentials" rather than as a
    half-built one — signing with a key and no secret cannot work, and falling
    back to something that does is better than failing on a field somebody
    cleared by accident.
    """
    key_id = (config.aws_access_key_id or "").strip()
    secret = decrypt(config.aws_secret_access_key_enc) or ""
    region = (config.aws_region or "").strip()
    if not (key_id and secret.strip()):
        # A region on its own is still worth carrying: the bucket may be in a
        # different one from the appliance default, and the region is baked
        # into the URL a reviewer's browser resolves.
        return s3_upload.Credentials(region=region) if region else None
    return s3_upload.Credentials(key_id, secret, region)


def is_publishable(config: Optional[QaSinkConfig]) -> bool:
    """Is this config complete enough that a publish could actually arrive?

    Mirrors the validator on QaSinkConfigBody, and exists separately because a
    row can also be made incomplete by a later edit to something it depends on.
    Checking here keeps `enqueue` from filling an outbox that cannot drain.
    """
    if config is None or not config.enabled:
        return False
    destination_ok = (bool(config.webhook_url)
                      if getattr(config, 'delivery_method', None) == 'webhook'
                      else bool(config.bootstrap_servers.strip() and config.kafka_topic.strip()))
    return destination_ok and bool((config.s3_bucket or "").strip())


# ── Building the message ─────────────────────────────────────────────────────


def image_key(config: QaSinkConfig, annotation_id: uuid.UUID) -> str:
    """Where this annotation's frame lives in the bucket.

    DETERMINISTIC, and that is the point: the key can be computed when the
    outbox row is written, long before the object exists, so the URL inside the
    frozen payload and the object the relay later uploads cannot disagree. A
    random key would have to be generated, stored and read back.
    """
    prefix = s3_upload.normalise_prefix(config.s3_prefix)
    return f"{prefix}{annotation_id.hex}.jpg"


def labelled_image_key(config: QaSinkConfig, annotation_id: uuid.UUID) -> str:
    """Where the box-burned copy lives. Deterministic, for the same reason
    `image_key` is: both URLs are frozen into the payload before either object
    exists, so neither may be a value that has to be generated and read back.
    """
    prefix = s3_upload.normalise_prefix(config.s3_prefix)
    return f"{prefix}{annotation_id.hex}_labelled.jpg"


def video_key(config: QaSinkConfig, annotation_id: uuid.UUID) -> Optional[str]:
    """Where this annotation's clip lives in the bucket, or None when video is off.

    Deterministic for the same reason `image_key` is: the URL is frozen into the
    payload at enqueue, before the clip exists. The prefix is its own setting
    rather than a suffix of `s3_prefix` because clips usually want a different
    bucket policy and lifecycle rule from stills.
    """
    prefix = s3_upload.normalise_prefix(config.s3_video_prefix)
    if not prefix:
        return None
    return f"{prefix}{annotation_id.hex}.mp4"


def subcategory_for(config: QaSinkConfig, class_key: str) -> str:
    """The remote label for one of our label classes.

    Falls back to the raw key when the map has no entry. The alternative —
    refusing to publish an unmapped class — hides the annotation entirely over
    a cosmetic mismatch; this way the reviewer sees `forklift` next to
    `PPE-No Helmet`, notices, and somebody fixes the map.
    """
    mapped = (config.class_map or {}).get(class_key)
    return mapped.strip() if isinstance(mapped, str) and mapped.strip() else class_key


def _no_video_url(annotation_id: uuid.UUID) -> str:
    return f"vms://annotation/{annotation_id}"


def build_payload(row: Annotation, config: QaSinkConfig, *,
                  sensor_id: str, color: Optional[str], url: str,
                  labelled_url: str,
                  video_url: Optional[str] = None) -> dict[str, Any]:
    """The message body, in the remote system's shape.

    Read the three numbered rules in the module docstring before changing any
    field here; most of them look replaceable and are not.
    """
    width = float(row.frame_width or 0) or 1.0
    height = float(row.frame_height or 0) or 1.0

    return {
        # Their `id` is a plain string primary key. Using ours makes the
        # message idempotent end to end: a redelivery hits their primary key
        # instead of creating a second review item for the same box.
        "id": str(row.id),
        "sensor_id": sensor_id,
        "org_img": url,
        # The clip's full S3 URL when video publishing is on. Otherwise — or
        # when the NVR had no footage, see `_upload_clip` — no video exists, but
        # unique and NOT NULL are still required, so this is a deliberately
        # non-HTTP identifier: honest about being unfetchable, rather than an
        # http:// URL to this appliance that no reviewer can reach and that
        # would look like a bug when it times out.
        "video_url": video_url or _no_video_url(row.id),
        # The box-burned copy. The reviewing system draws boxes in the browser
        # from the coordinates below, so `org_img` alone is enough to review —
        # but anything that leaves that browser (an export, a screenshot, a
        # downstream consumer reading this field) gets a flat JPEG with no
        # coordinates attached, and a frame with no box drawn on it does not
        # say what was annotated. A FULL URL, not a key: this column is what
        # their own approve path overwrites with an S3 URL, so a bare key here
        # would make the column mean two different things depending on which
        # system wrote it last.
        "org_img_local": labelled_url,
        # TRAILING UNDERSCORE IS LOAD-BEARING. The consumer splits this on "_"
        # and takes the last segment as a tracker id; an empty segment makes it
        # treat the event as untracked and skip a frame-persistence gate that
        # exists to suppress single detections. An operator's box IS a single
        # detection, and without this it can be suppressed before it is ever
        # stored.
        "datetimestamp_trackerid": f"{row.captured_at.isoformat()}_",
        # Colon, not "x". See rule 3.
        "imgsz": f"{row.frame_width}:{row.frame_height}",
        "severity": config.severity,
        "topic": config.payload_topic,
        "count": 1,
        "status": "LABELLED",
        "created_at": row.captured_at.isoformat(),
        "absolute_bbox": [{
            # Percentages of the frame, top-left origin. See rule 2.
            "xywh": [
                round(row.bbox_x / width * 100.0, 4),
                round(row.bbox_y / height * 100.0, 4),
                round(row.bbox_w / width * 100.0, 4),
                round(row.bbox_h / height * 100.0, 4),
            ],
            "class_name": row.class_key,
            # A human drew this box. Reporting a detector-like 0.9 would put a
            # fabricated number into somebody else's training data.
            "confidence": 1.0,
            "subcategory": subcategory_for(config, row.class_key),
            "color": color,
        }],
    }


# ── Enqueueing ───────────────────────────────────────────────────────────────


def snapshot(config):
    # Only encrypted credentials are copied. Never returned by the outbox API.
    return {column.name: deepcopy(getattr(config, column.name))
            for column in QaSinkConfig.__table__.columns
            if column.name not in ('id', 'updated_at', 'updated_by')}


def destination_for(entry):
    if not entry.destination:
        raise DeliveryError('Queued destination is missing; delivery is blocked', permanent=True)
    return QaSinkConfig(**entry.destination)


async def enqueue(db, row: Annotation, config: QaSinkConfig) -> QaPublishOutbox:
    """Queue one annotation for publication, in the CALLER'S transaction.

    Deliberately does no I/O. It is called from the request that saved the
    annotation, and the whole point of the outbox is that the operator's save
    does not depend on a broker being up.

    CLEARING THE EXPIRY IS PART OF PUBLISHING, not a separate courtesy. A loose
    annotation is deleted two days after capture by `annotation_store.
    purge_expired`; a review queue is not reliably drained in two days. Letting
    the frame expire under a reviewer would show them a broken image, so
    publishing is treated as what it is — a decision to keep the frame — and it
    is recorded with a reason so the kept-without-deadline count stays
    answerable.
    """
    sensor_id = await _sensor_id_for(db, row.camera_slug)
    color = await _class_color(db, row.class_key)
    key = image_key(config, row.id)
    labelled_key = labelled_image_key(config, row.id)
    creds = credentials_for(config)
    region = creds.region if creds else ""
    url = s3_upload.public_url(config.s3_bucket, key, config.public_base_url,
                               region=region)
    labelled_url = s3_upload.public_url(
        config.s3_bucket, labelled_key, config.public_base_url, region=region)
    vkey = video_key(config, row.id)
    video_url = (s3_upload.public_url(
        config.s3_bucket, vkey, config.public_base_url, region=region)
        if vkey else None)

    row.expires_at = None
    row.kept_reason = row.kept_reason or KEPT_REASON

    entry = QaPublishOutbox(
        annotation_id=row.id, destination=snapshot(config),
        payload=build_payload(row, config, sensor_id=sensor_id, color=color,
                              url=url, labelled_url=labelled_url,
                              video_url=video_url),
        image_key=key, labelled_image_key=labelled_key, video_key=vkey,
        state="pending", attempts=0,
        created_at=datetime.now(timezone.utc),
        next_attempt_at=datetime.now(timezone.utc))
    db.add(entry)
    return entry


async def _sensor_id_for(db, camera_slug: str) -> str:
    """The camera's external sensor UUID, or its slug when it has none.

    `sensor_id` is optional on a camera here and required over there. The slug
    is the better fallback than a blank: it is stable, unique per camera, and a
    reviewer can tell two cameras apart by it.
    """
    sensor = await db.scalar(
        select(Camera.sensor_id).where(Camera.slug == camera_slug))
    return str(sensor) if sensor else camera_slug


async def _class_color(db, class_key: str) -> Optional[str]:
    """The operator-chosen colour for this class, so their box renders in it."""
    cls = await db.get(LabelClass, class_key)
    return cls.color if cls else None


# ── Draining ─────────────────────────────────────────────────────────────────


def _producer_kwargs(config: QaSinkConfig) -> dict[str, Any]:
    brokers = [b.strip() for b in config.bootstrap_servers.split(",") if b.strip()]
    kwargs: dict[str, Any] = {
        "bootstrap_servers": brokers,
        # Fixed rather than derived from the deployment: this string shows up
        # in the broker's client list, and "which appliance is this?" is better
        # answered there by the source IP than by a name we would have to keep
        # in sync with however the site is labelled elsewhere.
        "client_id": "vms-onprem-annotations",
        # Every broker must have the record before we call it sent. This is a
        # queue of unrepeatable operator work; a fire-and-forget ack would let
        # a leader failover drop a box and mark it delivered.
        "acks": "all",
        "request_timeout_ms": 20000,
        "value_serializer": lambda v: json.dumps(v).encode("utf-8"),
    }
    if config.use_tls:
        kwargs["security_protocol"] = "SASL_SSL" if config.sasl_mechanism else "SSL"
    elif config.sasl_mechanism:
        kwargs["security_protocol"] = "SASL_PLAINTEXT"
    if config.sasl_mechanism:
        kwargs["sasl_mechanism"] = config.sasl_mechanism
        kwargs["sasl_plain_username"] = config.sasl_username or ""
        kwargs["sasl_plain_password"] = decrypt(config.sasl_password_enc) or ""
    return kwargs


async def _open_producer(config: QaSinkConfig):
    """A started aiokafka producer, or raise with a sentence an operator can read.

    aiokafka is imported lazily for the same reason boto3 is in s3_upload: an
    appliance that never configures a QA sink should not fail to start because
    an optional dependency is missing from its image.
    """
    try:
        from aiokafka import AIOKafkaProducer  # noqa: PLC0415 — see docstring
    except ImportError as exc:
        raise RuntimeError(
            "Publishing annotations through Kafka needs the aiokafka package, which is not "
            "installed on this appliance.") from exc

    producer = AIOKafkaProducer(**_producer_kwargs(config))
    await producer.start()
    return producer


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)),
                                 _BACKOFF_CAP_SECONDS))


async def _upload_clip(config: QaSinkConfig, entry: QaPublishOutbox,
                       annotation: Optional[Annotation], creds) -> None:
    """Cut a clip around the annotation and mirror it, or fall back to no video.

    A clip that cannot be produced does NOT fail the publish: the frame is the
    thing under review and the clip is context, so the message goes out with the
    same `vms://` placeholder an appliance without video publishing sends. The
    payload is rewritten (not mutated, so SQLAlchemy sees the JSONB change) and
    `video_key` cleared, so the stored row says what was actually delivered.
    """
    if not entry.video_key:
        return
    try:
        if annotation is None:
            raise nvr_client.ClipUnavailable(
                "The annotation row is gone, so there is no time to cut around.")
        half = timedelta(seconds=_VIDEO_SECONDS_EACH_SIDE)
        clip = await nvr_client.fetch_clip(
            annotation.camera_slug,
            (annotation.captured_at - half).isoformat(),
            (annotation.captured_at + half).isoformat())
        await s3_upload.upload_public_bytes(
            config.s3_bucket, config.s3_video_prefix,
            f"{uuid.UUID(str(entry.annotation_id)).hex}.mp4", clip.data,
            content_type="video/mp4", creds=creds)
    except (nvr_client.ClipUnavailable, s3_upload.S3UploadError) as exc:
        log.warning("qa_sink.video_skipped",
                    annotation_id=str(entry.annotation_id), error=str(exc))
        entry.payload = {**(entry.payload or {}),
                         "video_url": _no_video_url(entry.annotation_id)}
        entry.video_key = None


async def _prepare_media(config: QaSinkConfig,
                   entry: QaPublishOutbox,
                   annotation: Optional[Annotation] = None) -> None:
    """Mirror the frame, then produce the message. Raises on any failure.

    ORDER MATTERS AND IS THE OPPOSITE OF THE OBVIOUS ONE. The image goes to the
    bucket BEFORE the message goes to Kafka, so the URL in the message is live
    the moment a consumer reads it. Producing first would open a window —
    short, but exactly during the SSE push that pops the item onto a reviewer's
    screen — where the review item exists and its image 404s.

    Re-uploading on a retry is accepted rather than avoided. The key is
    deterministic so the PUT is idempotent, and one redundant upload of a
    single JPEG is cheaper than the bookkeeping needed to skip it.
    """
    annotation_hex = uuid.UUID(str(entry.annotation_id)).hex
    blob = await nvr_client.fetch_annotation_image(annotation_hex)
    if blob is None:
        # Distinguishable from a broker failure on purpose: this one is not
        # fixed by waiting, it is fixed by looking at the NVR.
        raise RuntimeError(
            "The NVR did not return the frame for this annotation.")

    creds = credentials_for(config)
    await s3_upload.upload_public_bytes(
        config.s3_bucket, config.s3_prefix, f"{annotation_hex}.jpg", blob,
        creds=creds)

    # The labelled copy. BOTH URLs were frozen into the payload at enqueue
    # time, so the object at the labelled key MUST exist by the time the
    # message is produced — otherwise the review item carries a link that 404s
    # forever. That is why a drawing failure falls back to storing the
    # original bytes under the labelled key rather than skipping the upload:
    # a copy without the box drawn is a worse image, but a dead URL is a
    # broken record, and the reviewer's browser draws the boxes itself anyway.
    boxes = (entry.payload or {}).get("absolute_bbox") or []
    try:
        labelled = image_label.draw_boxes(blob, boxes)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("qa_sink.label_failed",
                    annotation_id=str(entry.annotation_id), error=str(exc))
        labelled = blob
    await s3_upload.upload_public_bytes(
        config.s3_bucket, config.s3_prefix, f"{annotation_hex}_labelled.jpg",
        labelled, creds=creds)

    # Last, and before the message, for the same reason as the images: the URL
    # in the payload must be live when a consumer reads it.
    await _upload_clip(config, entry, annotation, creds)

async def _deliver(producer, config, entry, annotation=None):
    await _prepare_media(config, entry, annotation)
    payload = serialize(entry.payload, config.message_format or 'review_legacy')
    if config.delivery_method == 'webhook':
        await post_webhook(config, payload)
    else:
        await producer.send_and_wait(config.kafka_topic, value=payload,
            key=uuid.UUID(str(entry.annotation_id)).hex.encode('utf-8'))


async def drain_once(db) -> dict[str, int]:
    """One relay pass. Returns {sent, failed} — both zero when there is nothing to do.

    Each entry uses its snapshotted destination. Kafka connections are scoped
    to a delivery; webhooks do not open a Kafka connection. The current enabled
    setting remains a global pause switch.
    """
    config = await get_config(db)
    if not is_publishable(config):
        return {"sent": 0, "failed": 0}

    now = datetime.now(timezone.utc)
    due = (await db.execute(
        select(QaPublishOutbox)
        .where(QaPublishOutbox.state == "pending",
               QaPublishOutbox.next_attempt_at <= now)
        .order_by(QaPublishOutbox.created_at)
        .limit(_BATCH))).scalars().all()
    if not due:
        return {"sent": 0, "failed": 0}

    sent = failed = 0
    for entry in due:
        producer = None
        try:
            destination = destination_for(entry)
            if (destination.delivery_method or 'kafka') == 'kafka':
                producer = await _open_producer(destination)
            elif destination.delivery_method != 'webhook':
                raise DeliveryError('Unsupported delivery method', permanent=True)
            await _deliver(producer, destination, entry,
                           await db.get(Annotation, entry.annotation_id))
        except Exception as exc:
            # External exceptions can contain credential-bearing URLs. Store only
            # our normalized delivery errors; Kafka/media errors stay generic.
            error = str(exc) if isinstance(exc, DeliveryError) else 'Annotation delivery failed; check destination and media settings'
            _record_failure(entry, error, now)
            if isinstance(exc, DeliveryError):
                if exc.permanent:
                    entry.state = 'failed'
                elif exc.retry_after:
                    entry.next_attempt_at = max(entry.next_attempt_at,
                        now + timedelta(seconds=exc.retry_after))
            failed += 1
        else:
            entry.state = 'sent'
            entry.sent_at = datetime.now(timezone.utc)
            entry.last_error = None
            sent += 1
        finally:
            if producer is not None:
                try:
                    await producer.stop()
                except Exception:
                    log.warning('qa_sink.producer_stop_failed')

    await db.commit()
    if sent or failed:
        log.info("qa_sink.drained", sent=sent, failed=failed)
    return {"sent": sent, "failed": failed}


def _record_failure(entry: QaPublishOutbox, error: str, now: datetime) -> None:
    entry.attempts += 1
    entry.last_error = error[:2000]
    if entry.attempts >= _MAX_ATTEMPTS:
        # Parked, not deleted. An operator can see what happened and retry it.
        entry.state = "failed"
    else:
        entry.next_attempt_at = now + _backoff(entry.attempts)


async def relay_loop() -> None:
    """Drain the outbox forever. ONE worker — see main.py for why it is elected."""
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await drain_once(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("qa_sink.relay_failed", error=str(exc))
        await asyncio.sleep(_INTERVAL_SECONDS)


# ── Deletion ─────────────────────────────────────────────────────────────────


async def forget_images(db, annotation_ids: list[uuid.UUID]) -> int:
    """Delete the mirrored frames for annotations that are about to be removed.

    CALLED BEFORE THE ROWS GO, always. The outbox row carries the object key
    and is removed by the cascade the moment the annotation is deleted, so
    after the delete there is nothing left that knows which object to remove —
    the frame would sit in a public bucket with no record of why.

    RETURNS THE NUMBER IT COULD NOT DELETE, and callers are expected to care.
    A sweep must not abandon its batch over one stuck object, but an erasure
    request that reports success while a frame is still readable in a public
    bucket is a false statement in a compliance register — so the count comes
    back and `erase_range` folds it into its own failure total rather than
    this function deciding on its behalf.

    WHAT THIS CANNOT DO: the copy already delivered to the remote reviewer's
    database is beyond our reach. Erasure here removes our frame and our mirror
    and does not remove theirs. That gap is real, it is the price of publishing
    at all, and it is why publishing is an explicit, audited, capability-gated
    act rather than something every drawn box does automatically.
    """
    if not annotation_ids:
        return 0
    rows = (await db.execute(select(QaPublishOutbox)
        .where(QaPublishOutbox.annotation_id.in_(annotation_ids)))).scalars().all()
    stuck = 0
    for entry in rows:
        keys = {k for k in (entry.image_key, entry.labelled_image_key, entry.video_key) if k}
        if not keys:
            continue
        try:
            config = destination_for(entry)
        except DeliveryError:
            stuck += len(keys)
            continue
        creds = credentials_for(config)
        for key in keys:
            if not await s3_upload.delete_object(config.s3_bucket, key, creds=creds):
                stuck += 1
                log.warning('qa_sink.image_orphaned', key=key)
    return stuck
