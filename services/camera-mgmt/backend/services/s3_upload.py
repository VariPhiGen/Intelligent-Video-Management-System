"""s3_upload.py — mirror a dataset's frames to S3.

Runs ALONGSIDE the Label Studio import, not instead of it. The two destinations
answer different questions: Label Studio gets the images so they can be
annotated, S3 gets them so they are kept somewhere durable and reusable by
anything else that wants the training set later. Either can be re-run on its
own when the other succeeded.
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from typing import Any, NamedTuple, Optional

import structlog

from ..config import settings

log = structlog.get_logger(__name__)

# Parallel PUTs. S3 itself would take an order of magnitude more, but each
# in-flight upload holds a whole frame in memory and this box is simultaneously
# recording every camera and running the RTSP relay. Eight is generous against
# that, not against S3.
_CONCURRENCY = 8

# S3 bucket names are DNS labels: lowercase letters, digits, hyphens and dots,
# 3-63 characters, starting and ending alphanumeric. Validating here turns a
# typo into a sentence an operator can act on instead of a raw botocore
# ParamValidationError surfacing as a 500.
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class S3UploadError(Exception):
    """Something an operator can act on — a malformed bucket name, missing
    credentials, a bucket that does not exist or is in another region."""


def validate_bucket(name: str) -> str:
    """Return the bucket name, or raise S3UploadError explaining the rule broken."""
    value = (name or "").strip()
    if not value:
        raise S3UploadError("Enter an S3 bucket name.")
    if len(value) < 3 or len(value) > 63:
        raise S3UploadError(
            f"'{value}' is not a valid S3 bucket name — it must be 3 to 63 characters."
        )
    if not _BUCKET_RE.match(value):
        raise S3UploadError(
            f"'{value}' is not a valid S3 bucket name. Use lowercase letters, "
            "digits, hyphens and dots only, starting and ending with a letter "
            "or digit."
        )
    if ".." in value:
        raise S3UploadError(
            f"'{value}' is not a valid S3 bucket name — it cannot contain '..'."
        )
    return value


def normalise_prefix(prefix: Optional[str]) -> str:
    """Key prefix with no leading slash and exactly one trailing slash.

    An empty prefix stays empty — that is "the bucket root", which is a real
    choice — but anything non-empty gets its single trailing slash so callers
    can concatenate without thinking about it.
    """
    value = (prefix or "").strip().lstrip("/")
    if not value:
        return ""
    return value.rstrip("/") + "/"


def configured() -> bool:
    """Are S3 credentials present in the environment?

    False does NOT mean an upload will fail: boto3 also resolves an instance
    role or a mounted ~/.aws, both of which are the right answer on a deployed
    appliance. The UI uses this only to decide whether to say "using the
    instance role".
    """
    return bool(settings.aws_access_key_id.strip() and settings.aws_secret_access_key.strip())


class Credentials(NamedTuple):
    """One caller's own S3 identity, overriding the appliance environment.

    EXISTS BECAUSE TWO DESTINATIONS ARE NOT ONE DESTINATION. The environment
    key belongs to the Data Collection mirror and its training bucket. The QA
    review sink writes to a different bucket, owned by different people, whose
    objects have to be world-readable. Sharing a key means one of those two
    grants the other access it should not have.

    Any field may be blank; a blank one falls back to the environment, so a
    caller that only needs a different region does not have to restate the key.
    """

    access_key_id: str = ""
    secret_access_key: str = ""
    region: str = ""

    def complete(self) -> bool:
        return bool(self.access_key_id.strip() and self.secret_access_key.strip())


def _client(creds: Optional[Credentials] = None):
    """Build a boto3 S3 client. BLOCKING — only call inside asyncio.to_thread.

    `creds` overrides the environment for this one client. Passing None keeps
    the historical behaviour exactly: environment key if present, otherwise
    boto3's own resolution chain (instance role, mounted ~/.aws).

    boto3 is imported here rather than at module scope so this module still
    imports on a host where the package is absent; nothing else in the API
    needs it, and a missing optional dependency must not take the service down.
    """
    try:
        import boto3  # noqa: PLC0415 — deliberate: see docstring
    except ImportError as exc:
        raise S3UploadError(
            "The S3 mirror needs the boto3 package, which is not installed on "
            "this appliance."
        ) from exc

    kwargs: dict[str, Any] = {}
    # Blank credentials are deliberate and supported: boto3 then falls back to
    # an instance role or a mounted ~/.aws.
    if creds is not None and creds.complete():
        kwargs["aws_access_key_id"] = creds.access_key_id.strip()
        kwargs["aws_secret_access_key"] = creds.secret_access_key.strip()
    elif settings.aws_access_key_id.strip() and settings.aws_secret_access_key.strip():
        kwargs["aws_access_key_id"] = settings.aws_access_key_id.strip()
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key.strip()
    region = (creds.region.strip() if creds else "") or settings.aws_region.strip()
    if region:
        kwargs["region_name"] = region
    return boto3.client("s3", **kwargs)


def _friendly(exc: Exception) -> str:
    """Translate the botocore errors an operator actually hits.

    Two jobs. The obvious one is turning an error code into a sentence naming
    the setting to change. The less obvious one is KEEPING THE LOCAL PATH OUT:
    boto3's S3UploadFailedError embeds the full container-side filename, so the
    raw string surfaces `/dataset-frames/5-gate-1/frame_00003_...jpg` in the UI
    and in the audit detail — noise to an operator, and an internal layout
    detail that has no business being there.
    """
    name = type(exc).__name__
    text = str(exc)
    if "NoCredentialsError" in name or "PartialCredentials" in name:
        return ("No AWS credentials are available. Set AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY on the api service, or attach an "
                "instance role.")
    if "InvalidAccessKeyId" in text:
        return ("The AWS access key set on this appliance is not recognised by "
                "AWS. Check AWS_ACCESS_KEY_ID.")
    if "SignatureDoesNotMatch" in text:
        return ("The AWS secret key set on this appliance does not match its "
                "access key. Check AWS_SECRET_ACCESS_KEY.")
    if "ExpiredToken" in text or "InvalidToken" in text:
        return ("The AWS credentials have expired. Refresh them, or attach an "
                "instance role so they rotate automatically.")
    if "PermanentRedirect" in text or "the bucket you are attempting to access" in text:
        return ("The bucket is in a different region than AWS_REGION. Set "
                "AWS_REGION to the bucket's own region.")
    if "NoSuchBucket" in text:
        return "That bucket does not exist."
    if "AccessDenied" in text:
        return ("The credentials do not have permission to write to that "
                "bucket or prefix.")
    # Unrecognised: keep whatever AWS said, but drop boto3's
    # "Failed to upload <local path> to <key>: " preamble.
    marker = ": An error occurred"
    if marker in text:
        text = "An error occurred" + text.split(marker, 1)[1]
    return text[:400]


async def upload_frames(
    bucket: str, prefix: str, frames_dir: str, frames: list[dict],
) -> dict:
    """Mirror every frame to s3://bucket/prefix/. Returns a small report.

    EVERY boto3 CALL RUNS IN A WORKER THREAD. boto3 is synchronous, and calling
    it on the event loop would stall every other request served by this uvicorn
    worker for the whole length of the upload — a multi-minute freeze of the
    live view and the camera list, caused by a background mirror nobody was
    watching.
    """
    bucket = validate_bucket(bucket)
    prefix = normalise_prefix(prefix)
    if not frames:
        raise S3UploadError("This dataset has no frames to upload.")

    root = Path(frames_dir)
    try:
        client = await asyncio.to_thread(_client)
    except S3UploadError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface a sentence, not a traceback
        raise S3UploadError(_friendly(exc)) from exc

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    uploaded = 0
    skipped = 0
    # Counted apart from `skipped` (a PUT that failed): a frame the manifest
    # lists but the directory no longer holds is not an S3 problem, and
    # re-running the upload will never fix it — re-collecting will.
    missing_on_disk = 0
    lock = asyncio.Lock()
    first_error: list[str] = []

    def put(key: str, path: Path) -> None:
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={"ContentType": mimetypes.guess_type(path.name)[0] or "image/jpeg"},
        )

    async def one(frame: dict) -> None:
        nonlocal uploaded, skipped, missing_on_disk
        filename = str(frame["filename"])
        path = root / filename
        async with semaphore:
            if not path.exists():
                async with lock:
                    missing_on_disk += 1
                return
            try:
                await asyncio.to_thread(put, f"{prefix}{filename}", path)
            except Exception as exc:  # noqa: BLE001
                async with lock:
                    skipped += 1
                    if not first_error:
                        first_error.append(_friendly(exc))
                log.warning("dataset.s3.put_failed", bucket=bucket,
                            key=f"{prefix}{filename}", error=str(exc))
                return
            async with lock:
                uploaded += 1

    await asyncio.gather(*(one(f) for f in frames))

    if uploaded == 0:
        if first_error:
            raise S3UploadError(first_error[0])
        if missing_on_disk:
            raise S3UploadError(
                f"None of this dataset's {len(frames)} frames are on disk any "
                "more. Re-collect the dataset before uploading.")
        raise S3UploadError(
            f"None of this dataset's {len(frames)} frames could be uploaded.")

    if missing_on_disk:
        log.warning("dataset.s3.frames_missing_on_disk", bucket=bucket,
                    missing=missing_on_disk)
    log.info("dataset.s3.uploaded", bucket=bucket, prefix=prefix,
             uploaded=uploaded, skipped=skipped, missing=missing_on_disk)
    return {
        "bucket": bucket,
        "prefix": prefix,
        "uploaded": uploaded,
        "skipped": skipped,
        "missing_on_disk": missing_on_disk,
        # Present only when some frames failed while others succeeded — the
        # caller reports a partial mirror rather than a clean one.
        "error": first_error[0] if first_error else None,
    }


async def presign(bucket: str, key: str, expires: int = 3600) -> str:
    """A time-limited GET URL for one mirrored object."""
    bucket = validate_bucket(bucket)
    try:
        client = await asyncio.to_thread(_client)
        return await asyncio.to_thread(
            client.generate_presigned_url,
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
    except S3UploadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise S3UploadError(_friendly(exc)) from exc


async def upload_json(bucket: str, prefix: str, name: str, doc: dict) -> str:
    """Put one JSON document. Returns the key written.

    Same thread discipline as upload_frames: boto3 is synchronous, and calling
    it on the event loop would stall every other request this worker serves.
    """
    import json

    bucket = validate_bucket(bucket)
    prefix = normalise_prefix(prefix)
    key = f"{prefix}{name}" if prefix else name
    body = json.dumps(doc, indent=2).encode()

    client = await asyncio.to_thread(_client)
    await asyncio.to_thread(
        client.put_object, Bucket=bucket, Key=key, Body=body,
        ContentType="application/json")
    log.info("s3.json_uploaded", bucket=bucket, key=key, bytes=len(body))
    return key


async def upload_public_bytes(
    bucket: str, prefix: str, name: str, body: bytes,
    content_type: str = "image/jpeg", creds: Optional[Credentials] = None,
) -> dict:
    """Put one object and return the key plus a URL anyone can GET.

    PUBLIC-READ ON PURPOSE, which is the opposite of what the rest of this
    module does, so it is worth saying why rather than leaving it to be
    discovered.

    `presign` cannot serve this case. A presigned URL is a GET with a deadline,
    and SigV4 caps that deadline at seven days — but the consumer of this URL is
    a reviewer's browser opening a queue item whenever they get to it, and the
    URL is stored in somebody else's database for as long as the record lives.
    A link that works for a week and then silently 403s is worse than no link,
    because the failure arrives long after the change that caused it.

    THE TRADE BEING MADE: anyone holding the URL can fetch the frame. The key
    carries a UUID so it is unguessable in practice, but it is not access
    control, and the frames are stills of identifiable people. Scope the bucket
    policy to this prefix alone, and delete the object when the annotation it
    belongs to is deleted — `delete_object` below is the other half of that and
    is not optional.

    Some buckets reject `ACL` outright (Object Ownership set to bucket-owner-
    enforced, which is the default on buckets created since April 2023). That
    is not a failure: it means public access is granted by bucket policy
    instead, so the PUT is retried without the ACL and the object is still
    readable. Failing here would make this feature impossible on a
    correctly-configured modern bucket.
    """
    bucket = validate_bucket(bucket)
    prefix = normalise_prefix(prefix)
    key = f"{prefix}{name}" if prefix else name

    client = await asyncio.to_thread(_client, creds)

    def put(with_acl: bool) -> None:
        extra: dict[str, Any] = {"ContentType": content_type}
        if with_acl:
            extra["ACL"] = "public-read"
        client.put_object(Bucket=bucket, Key=key, Body=body, **extra)

    try:
        try:
            await asyncio.to_thread(put, True)
        except Exception as acl_exc:  # noqa: BLE001
            if not _is_acl_unsupported(acl_exc):
                raise
            log.info("qa_sink.s3.acl_unsupported_retrying_plain",
                     bucket=bucket, key=key)
            await asyncio.to_thread(put, False)
    except S3UploadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise S3UploadError(_friendly(exc)) from exc

    log.info("qa_sink.s3.put", bucket=bucket, key=key, bytes=len(body))
    return {"key": key, "url": public_url(
        bucket, key, region=creds.region if creds else "")}


def _is_acl_unsupported(exc: Exception) -> bool:
    """Is this the "this bucket does not do ACLs" refusal, or a real error?

    Matching on the error CODE rather than the message: botocore's wording
    changes between releases and an operator's locale, the codes do not.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"AccessControlListNotSupported",
                    "InvalidBucketAclWithObjectOwnership"}


def public_url(bucket: str, key: str, base: Optional[str] = None,
               region: str = "") -> str:
    """The URL a browser uses for an object published by `upload_public_bytes`.

    `base` overrides the virtual-hosted S3 host, which is what a deployment in
    front of CloudFront or a MinIO gateway needs. Without it the region matters:
    a bucket outside us-east-1 is reachable at the regionless host too, but only
    via a redirect, and some corporate proxies do not follow it on an <img>.
    """
    if base and base.strip():
        return f"{base.strip().rstrip('/')}/{key}"
    region = (region or "").strip() or settings.aws_region.strip()
    host = (f"{bucket}.s3.{region}.amazonaws.com" if region
            else f"{bucket}.s3.amazonaws.com")
    return f"https://{host}/{key}"


async def delete_object(bucket: str, key: str,
                        creds: Optional[Credentials] = None) -> bool:
    """Remove one object. True if it is gone, False if it could not be removed.

    Returns a bool rather than raising because every caller is a deletion sweep
    that must not abandon the rest of its batch over one stuck object — the
    same contract `nvr_client.delete_annotation` has, for the same reason.

    S3 DELETE is idempotent: deleting a key that is not there succeeds, which
    is the behaviour a retry wants.
    """
    try:
        bucket = validate_bucket(bucket)
        client = await asyncio.to_thread(_client, creds)
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("qa_sink.s3.delete_failed", bucket=bucket, key=key,
                    error=str(exc))
        return False
