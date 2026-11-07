"""object_store.py — the appliance's own S3 bucket, for heatmap overlay images.

WHY AN OBJECT STORE AT ALL. A heatmap overlay is a ~300 KB JPEG and a camera
keeps 17 of them a day (16 half-hourly snapshots plus the session's final
picture). Those are rows that exist only to hold bytes nobody queries on, and
Postgres is the wrong place for them: every backup, every replica and every
`pg_dump` carries them. The object store holds the bytes; the row holds the
key, the times and the retention deadline — which is what `object_key` was
reserved for in migration 041.

WHAT DOES NOT GO HERE. The `live` picture stays in its row. It is one per
session, replaced every 30 seconds, and never read once the next one lands;
putting it here would be ~2,880 PUT/DELETE pairs per camera per day for an
object with a 30-second life. See services/heatmaps.py.

BOTO3, NOT A MINIO CLIENT. boto3 is already a dependency (services/s3_upload.py
mirrors datasets with it), MinIO speaks S3, and using one client means one set
of credentials handling and one failure taxonomy. Imported lazily for the same
reason s3_upload does it: the module must still load where boto3 is absent.

OFF BY DEFAULT. With no endpoint configured `is_configured()` is False and
heatmaps keep their bytes in the row exactly as they always have. That is what
makes this safe to deploy before anything is migrated: the store is additive
until something writes to it.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

import structlog

from ..config import settings

log = structlog.get_logger(__name__)

#: Everything this service writes lives under one prefix, so the bucket can be
#: shared later without a second bucket or a lifecycle rule that has to know
#: about us.
ROOT = "heatmaps"

_client = None
_lock = threading.Lock()
_bucket_ready = False


class ObjectStoreError(Exception):
    """The store could not be reached or refused the operation.

    Deliberately distinct from `heatmaps.Rejected`: that one means "this upload
    is malformed, repeating it will not help" and answers 422. This one is
    transient — the store is down, the bucket is missing — and must answer 5xx
    so the producer retries rather than dropping a picture it cannot make again.
    """


def is_configured() -> bool:
    """False when no object store is deployed, which is the default."""
    return bool((settings.heatmap_s3_endpoint or "").strip())


def _get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import boto3          # imported late: keeps the seam honest
                from botocore.config import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=settings.heatmap_s3_endpoint.strip(),
                    aws_access_key_id=settings.heatmap_s3_access_key or None,
                    aws_secret_access_key=settings.heatmap_s3_secret_key or None,
                    # MinIO ignores the region but botocore insists on one.
                    region_name="us-east-1",
                    config=Config(
                        signature_version="s3v4",
                        # The store is a container on the same bridge network;
                        # a slow response means it is gone, not busy, and the
                        # caller is holding a request open.
                        connect_timeout=3, read_timeout=15,
                        retries={"max_attempts": 2},
                    ),
                )
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests, and for a settings reload."""
    global _client, _bucket_ready
    with _lock:
        _client = None
        _bucket_ready = False


def bucket() -> str:
    return (settings.heatmap_s3_bucket or "vms-heatmaps").strip()


def ensure_bucket() -> None:
    """Create the bucket if it is not there. Idempotent, and cached after the
    first success so it is not an extra round trip on every upload.

    The compose stack also creates it at startup (`minio_init`); doing it here
    as well means a store that was wiped, or pointed somewhere new, heals on
    the next write instead of failing until someone restarts the stack.
    """
    global _bucket_ready
    if _bucket_ready:
        return
    client = _get_client()
    name = bucket()
    try:
        client.head_bucket(Bucket=name)
    except Exception:                                          # noqa: BLE001
        try:
            client.create_bucket(Bucket=name)
            log.info("object_store.bucket_created", bucket=name)
        except Exception as exc:                               # noqa: BLE001
            # A concurrent creator is a success, not a failure; anything else
            # is worth surfacing.
            if "BucketAlreadyOwnedByYou" not in str(exc) and \
                    "BucketAlreadyExists" not in str(exc):
                raise ObjectStoreError(f"cannot create bucket {name!r}: {exc}") from exc
    _bucket_ready = True


# ── keys ─────────────────────────────────────────────────────────────────────

def heatmap_key(camera_slug: str, session_id, captured_at: datetime,
                kind: str) -> str:
    """The object key for one heatmap picture.

        heatmaps/{camera_slug}/{YYYY-MM-DD}/{session_id}/{HHMM}.jpg
        heatmaps/{camera_slug}/{YYYY-MM-DD}/{session_id}/final.jpg

    DETERMINISTIC ON PURPOSE. The same picture pushed twice — a retry, a
    replayed session — resolves to the same key and overwrites rather than
    accumulating a second copy, which is the object-store half of the
    (session_id, kind, captured_at) uniqueness the row already has.

    THE DATE AND TIME ARE LOCAL, not UTC, and that is deliberate. The operator
    sets `active_hours` on the local clock, the snapshot boundaries are floored
    on the local clock, and the
    historical API filters by local calendar day. A key in UTC would disagree
    with all three: a 09:00 snapshot would be filed as `0330.jpg`, and a session
    that begins before local midnight would land in the previous day's folder
    while the API listed it under today. Everything an operator reads now says
    the same thing, and `heatmaps/cam1-m7cx/2026-09-20/…/0900.jpg` means what it
    looks like. The row still stores UTC; only the path is local.

    The camera SLUG rather than its uuid, because these paths get read by
    people during an incident and `cam1-m7cx/2026-09-25/` is legible where a
    uuid is not. Slugs are unique and stable in this registry.
    """
    when = captured_at.astimezone()          # the appliance's own clock
    stem = "final" if kind == "final" else when.strftime("%H%M")
    return (f"{ROOT}/{camera_slug}/{when.strftime('%Y-%m-%d')}/"
            f"{session_id}/{stem}.jpg")


def camera_date_prefix(camera_slug: str, day: datetime) -> str:
    """Everything one camera captured on one LOCAL date."""
    return f"{ROOT}/{camera_slug}/{day.astimezone().strftime('%Y-%m-%d')}/"


# ── operations ───────────────────────────────────────────────────────────────

def put(key: str, data: bytes, content_type: str = "image/jpeg") -> str:
    """Store `data` at `key`. Returns the key. Overwrites, so retries are safe."""
    ensure_bucket()
    try:
        _get_client().put_object(Bucket=bucket(), Key=key, Body=data,
                                 ContentType=content_type)
    except Exception as exc:                                   # noqa: BLE001
        raise ObjectStoreError(f"put {key!r} failed: {exc}") from exc
    return key


def get(key: str) -> Optional[bytes]:
    """The object's bytes, or None if it is not there.

    A missing object is None rather than an error because it is a NORMAL
    outcome: retention deletes objects and rows in two steps, and a reader can
    arrive between them. The caller answers 404, which is the truth.
    """
    try:
        resp = _get_client().get_object(Bucket=bucket(), Key=key)
        return resp["Body"].read()
    except Exception as exc:                                   # noqa: BLE001
        text = str(exc)
        if "NoSuchKey" in text or "404" in text or "Not Found" in text:
            return None
        raise ObjectStoreError(f"get {key!r} failed: {exc}") from exc


def size(key: str) -> Optional[int]:
    """The object's size in bytes, or None if it is not there."""
    try:
        return int(_get_client().head_object(Bucket=bucket(), Key=key)["ContentLength"])
    except Exception as exc:                                   # noqa: BLE001
        text = str(exc)
        if "NoSuchKey" in text or "404" in text or "Not Found" in text:
            return None
        raise ObjectStoreError(f"head {key!r} failed: {exc}") from exc


def delete(key: str) -> None:
    """Remove one object. Deleting something already gone is a success."""
    try:
        _get_client().delete_object(Bucket=bucket(), Key=key)
    except Exception as exc:                                   # noqa: BLE001
        raise ObjectStoreError(f"delete {key!r} failed: {exc}") from exc


def delete_many(keys: Iterable[str]) -> int:
    """Remove up to a whole retention sweep's worth of objects.

    Returns how many the store reported deleting. Never raises for an
    individual key: retention must make progress even if one object is
    unreachable, and the row is what decides whether it is still referenced.
    S3 caps a batch delete at 1000 keys, so this chunks.
    """
    keys = [k for k in keys if k]
    if not keys:
        return 0
    client = _get_client()
    name = bucket()
    removed = 0
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i + 1000]
        try:
            resp = client.delete_objects(
                Bucket=name,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )
            removed += len(chunk) - len(resp.get("Errors") or [])
            for err in resp.get("Errors") or []:
                log.warning("object_store.delete_failed", key=err.get("Key"),
                            code=err.get("Code"), message=err.get("Message"))
        except Exception as exc:                               # noqa: BLE001
            log.warning("object_store.delete_batch_failed", count=len(chunk),
                        error=str(exc))
    return removed


def list_keys(prefix: str, limit: int = 1000) -> list[str]:
    """Keys under `prefix`, oldest first by name.

    Lexical order is chronological here by construction: the key carries
    HHMM, so a day's listing sorts into the timeline the page draws. `final`
    sorts after every HHMM, which is also where it belongs.
    """
    try:
        paginator = _get_client().get_paginator("list_objects_v2")
        out: list[str] = []
        for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
            out.extend(o["Key"] for o in page.get("Contents") or [])
            if len(out) >= limit:
                break
        return sorted(out)[:limit]
    except Exception as exc:                                   # noqa: BLE001
        raise ObjectStoreError(f"list {prefix!r} failed: {exc}") from exc


def health() -> dict:
    """What /health can say about the store without pretending to test it hard."""
    if not is_configured():
        return {"configured": False}
    try:
        _get_client().head_bucket(Bucket=bucket())
        return {"configured": True, "bucket": bucket(),
                "endpoint": settings.heatmap_s3_endpoint, "reachable": True}
    except Exception as exc:                                   # noqa: BLE001
        return {"configured": True, "bucket": bucket(),
                "endpoint": settings.heatmap_s3_endpoint,
                "reachable": False, "error": str(exc)[:200]}
