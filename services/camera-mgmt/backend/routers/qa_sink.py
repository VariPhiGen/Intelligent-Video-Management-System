"""The QA review sink: where drawn boxes go, and whether any of them got there.

ADMIN ONLY, every route. This form decides that frames of identifiable people
leave the appliance for a system nobody here administers, and that the copies
which arrive there cannot be recalled by an erasure request raised on this box.
That is not an operator setting, and `data_collection` — which governs the
training pile that stays local — is the wrong gate for it.

The outbox routes exist because an outbox nobody can see is worse than no
outbox. A relay that quietly accumulates `failed` rows looks identical, from
the Data Collection page, to a relay with nothing to do.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt
from ..db import get_db
from ..models import (QaPublishOutbox, QaSinkConfig, QaSinkConfigBody,
                      QaSinkConfigResponse)
from ..security import Principal, require_role
from ..services import audit as audit_svc
from ..services import qa_sink, s3_upload

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/qa-sink", tags=["qa-sink"])

_ADMIN = require_role("admin")


@router.get("", response_model=QaSinkConfigResponse)
async def get_qa_sink(db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(_ADMIN)):
    return QaSinkConfigResponse.from_row(await qa_sink.get_config(db))


@router.put("", response_model=QaSinkConfigResponse)
async def put_qa_sink(body: QaSinkConfigBody, request: Request,
                      db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(_ADMIN)):
    """Create or replace the single config row.

    The bucket name is validated HERE rather than at first publish. A typo'd
    bucket saved today fails inside the relay loop days later, by which time
    the sentence explaining it is in a log nobody is reading and the operator's
    memory of what they typed is gone.
    """
    if body.s3_bucket:
        try:
            s3_upload.validate_bucket(body.s3_bucket)
        except s3_upload.S3UploadError as exc:
            raise HTTPException(422, str(exc)) from exc

    row = await db.get(QaSinkConfig, 1)
    created = row is None
    if row is None:
        row = QaSinkConfig(id=1)
        db.add(row)

    row.delivery_method = body.delivery_method
    row.message_format = body.message_format
    row.webhook_url = body.webhook_url
    if body.webhook_token is not None:
        row.webhook_token_enc = encrypt(body.webhook_token) if body.webhook_token else None
    row.enabled = body.enabled
    row.bootstrap_servers = body.bootstrap_servers.strip()
    row.kafka_topic = body.kafka_topic.strip()
    row.payload_topic = body.payload_topic.strip()
    row.severity = body.severity.strip() or "low"
    row.sasl_mechanism = body.sasl_mechanism
    row.sasl_username = body.sasl_username
    row.use_tls = body.use_tls
    row.s3_bucket = (body.s3_bucket or "").strip() or None
    row.s3_prefix = s3_upload.normalise_prefix(body.s3_prefix)
    row.public_base_url = (body.public_base_url or "").strip() or None
    # normalise_prefix("") is "", and "" means off — stored as NULL so the two
    # spellings of "no video" are one state.
    row.s3_video_prefix = (
        s3_upload.normalise_prefix(body.s3_video_prefix) or None)
    row.aws_region = (body.aws_region or "").strip() or None
    row.class_map = {k: v.strip() for k, v in body.class_map.items()}
    row.updated_by = principal.subject
    row.updated_at = datetime.now(timezone.utc)

    # Omitted password means "leave it alone", so an edit to the class map
    # cannot silently break authentication. Clearing it is still possible, by
    # sending an empty string — an explicit act with an explicit value.
    if body.sasl_password is not None:
        row.sasl_password_enc = encrypt(body.sasl_password) if body.sasl_password else None
    # Same contract for BOTH halves of the S3 credential — neither is echoed
    # back by the GET, so neither can be round-tripped by the form, so an
    # omitted field has to mean "keep" or every unrelated edit would wipe it.
    if body.aws_access_key_id is not None:
        row.aws_access_key_id = body.aws_access_key_id.strip() or None
    if body.aws_secret_access_key is not None:
        row.aws_secret_access_key_enc = (
            encrypt(body.aws_secret_access_key)
            if body.aws_secret_access_key else None)
    # Clearing either half retires the other with it. A key ID with no secret
    # cannot sign anything, and a secret with no key ID is an orphaned
    # credential sitting encrypted in a row nothing reads — so the pair is
    # kept whole rather than left in a state that silently does nothing.
    if not row.aws_access_key_id or not row.aws_secret_access_key_enc:
        if (row.aws_access_key_id or row.aws_secret_access_key_enc) and not (
                body.aws_access_key_id == "" or body.aws_secret_access_key == ""):
            # Half a pair arrived with nothing stored to complete it. Refusing
            # here is the difference between "that field is wrong" and an
            # AccessDenied surfacing in a relay log an hour later — which is
            # precisely how this feature's first real failure was diagnosed.
            raise HTTPException(
                422, "Enter both the AWS access key ID and its secret access "
                     "key, or clear both to use this appliance's own "
                     "credentials.")
        row.aws_access_key_id = None
        row.aws_secret_access_key_enc = None
    # A mechanism that was turned off should not leave a live credential behind.
    if row.sasl_mechanism is None:
        row.sasl_username = None
        row.sasl_password_enc = None

    await db.commit()
    await db.refresh(row)

    await audit_svc.record(
        request, principal,
        "qa_sink.created" if created else "qa_sink.updated",
        target=row.kafka_topic or "qa-sink",
        # The destination is the point of the record. Credentials never are.
        detail={"enabled": row.enabled, "delivery_method": row.delivery_method, "message_format": row.message_format,
                "bootstrap_servers": row.bootstrap_servers,
                "kafka_topic": row.kafka_topic,
                "payload_topic": row.payload_topic,
                "s3_bucket": row.s3_bucket,
                # THAT the sink has its own identity is part of the
                # destination and belongs in the record. WHICH one does not:
                # the audit log is read far more widely than this form, and
                # the key ID is hidden there for the same reason it is hidden
                # here.
                "aws_uses_environment": not row.aws_access_key_id,
                "mapped_classes": len(row.class_map or {})})
    return QaSinkConfigResponse.from_row(row)


@router.get("/outbox")
async def outbox_status(
    state: str | None = Query(default=None, pattern="^(pending|sent|failed)$"),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(_ADMIN),
):
    """Counts by state, plus the most recent rows.

    `failed` first in the listing regardless of the filter would be clever and
    wrong — an administrator checking "did my config work" wants the newest
    rows whatever their state, and a stuck relay shows up in the counts.
    """
    counts = {state_name: 0 for state_name in ("pending", "sent", "failed")}
    for name, total in (await db.execute(
            select(QaPublishOutbox.state, func.count())
            .group_by(QaPublishOutbox.state))).all():
        counts[name] = total

    where = [QaPublishOutbox.state == state] if state else []
    rows = (await db.execute(
        select(QaPublishOutbox).where(*where)
        .order_by(QaPublishOutbox.created_at.desc())
        .limit(limit))).scalars().all()

    return {
        "counts": counts,
        "entries": [{
            "annotation_id": r.annotation_id.hex,
            "state": r.state,
            "attempts": r.attempts,
            "last_error": r.last_error,
            "created_at": r.created_at,
            "next_attempt_at": r.next_attempt_at,
            "sent_at": r.sent_at,
        } for r in rows],
    }


@router.post("/outbox/{annotation_id}/retry")
async def retry_entry(annotation_id: uuid.UUID, request: Request,
                      db: AsyncSession = Depends(get_db),
                      principal: Principal = Depends(_ADMIN)):
    """Put one parked row back in the queue, attempts reset to zero.

    Resetting rather than continuing the count is deliberate: a row is retried
    after somebody fixed the thing that broke it, so it deserves the full
    allowance again. Keeping the old count would park it after one more
    failure and make the fix look ineffective.
    """
    row = await db.get(QaPublishOutbox, annotation_id)
    if row is None:
        raise HTTPException(404, "Nothing queued for that annotation")
    if row.state == "sent":
        raise HTTPException(
            409, "That annotation has already been published to the external system.")

    row.state = "pending"
    row.attempts = 0
    row.last_error = None
    row.next_attempt_at = datetime.now(timezone.utc)
    await db.commit()

    await audit_svc.record(request, principal, "qa_sink.retried",
                           target=annotation_id.hex, detail={})
    return {"annotation_id": annotation_id.hex, "state": row.state}
