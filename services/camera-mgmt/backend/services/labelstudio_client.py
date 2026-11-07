"""labelstudio_client.py — push collected frames into a Label Studio project.

Two things here are counter-intuitive enough to be worth reading before
changing anything: the frames are UPLOADED rather than linked, and the API key
in the environment is usually not the token the API wants.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)

# Uploads are slow and large; the connect budget stays short so an unreachable
# host fails fast instead of hanging for five minutes on a typo'd URL.
_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

# Files per import request. Label Studio handles a multi-file import in one
# transaction, and 25 keeps each request's body bounded (a 1080p JPEG at
# quality 85 is ~300 KB, so ~7 MB per request) without paying request overhead
# per frame on a 20000-frame dataset.
_BATCH = 25


class LabelStudioError(Exception):
    """Something an operator can act on — a bad token, an unreachable host, a
    project that does not exist."""


def configured() -> bool:
    """Is there enough configuration to attempt an import at all?

    Blank is a legitimate state, not an error: the appliance collects frames
    perfectly well without Label Studio. The export route turns this into a 503
    with an explanation rather than letting the first request fail obscurely.
    """
    return bool(settings.labelstudio_url.strip() and settings.labelstudio_api_key.strip())


def _base() -> str:
    return settings.labelstudio_url.strip().rstrip("/")


# ── Authentication ────────────────────────────────────────────────────────────
#
# THE TOKEN IN THE ENVIRONMENT IS USUALLY A REFRESH TOKEN, NOT AN ACCESS TOKEN.
# What "Account & Settings -> Access Token" hands an operator on a modern Label
# Studio is a long-lived JWT refresh token; the API wants a short-lived access
# JWT minted from it. Older servers, and organisation-level tokens, use a
# `Token <key>` header instead and have no notion of refreshing.
#
# Rather than making the operator know which kind they pasted, probe all three
# once and remember which worked. The cached scheme also records whether it can
# renew itself, because that decides what happens on a 401 mid-upload.
_SCHEME_BEARER = "bearer"     # the key is already an access JWT / org token
_SCHEME_JWT = "jwt"           # the key is a refresh token; mint access JWTs
_SCHEME_LEGACY = "legacy"     # `Token <key>`; cannot renew

_auth: Optional[dict[str, Any]] = None


def reset_auth() -> None:
    """Drop the cached scheme and token. Used by the 401 retry path and by
    tests; also the right thing to call if the key is ever rotated at runtime."""
    global _auth
    _auth = None


async def _mint_access_token(client: httpx.AsyncClient, key: str) -> Optional[str]:
    """Exchange a refresh token for an access JWT, or None if it is not one."""
    try:
        resp = await client.post(f"{_base()}/api/token/refresh", json={"refresh": key})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        access = resp.json().get("access")
    except ValueError:
        return None
    return str(access) if access else None


async def _probe(client: httpx.AsyncClient) -> dict[str, Any]:
    """Find a working auth scheme, or raise with something actionable.

    GET /api/projects is the probe because it is the cheapest authenticated
    read that every Label Studio version exposes.
    """
    key = settings.labelstudio_api_key.strip()

    async def works(headers: dict[str, str]) -> bool:
        try:
            resp = await client.get(f"{_base()}/api/projects", headers=headers)
        except httpx.HTTPError as exc:
            raise LabelStudioError(
                f"Label Studio at {_base()} could not be reached: {exc}. "
                "Note the URL must be reachable from the API container — "
                "http://localhost:8080 points at the API itself, not at Label Studio."
            ) from exc
        return resp.status_code == 200

    if await works({"Authorization": f"Bearer {key}"}):
        log.info("labelstudio.auth", scheme=_SCHEME_BEARER)
        return {"scheme": _SCHEME_BEARER, "token": key, "renewable": False}

    access = await _mint_access_token(client, key)
    if access and await works({"Authorization": f"Bearer {access}"}):
        log.info("labelstudio.auth", scheme=_SCHEME_JWT)
        return {"scheme": _SCHEME_JWT, "token": access, "renewable": True}

    if await works({"Authorization": f"Token {key}"}):
        log.info("labelstudio.auth", scheme=_SCHEME_LEGACY)
        return {"scheme": _SCHEME_LEGACY, "token": key, "renewable": False}

    raise LabelStudioError(
        "Label Studio rejected the configured access token. Copy a fresh one "
        "from Account & Settings -> Access Token in Label Studio and set it as "
        "LABELSTUDIO_API_KEY."
    )


async def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    global _auth
    if _auth is None:
        _auth = await _probe(client)
    prefix = "Token" if _auth["scheme"] == _SCHEME_LEGACY else "Bearer"
    return {"Authorization": f"{prefix} {_auth['token']}"}


async def _renew(client: httpx.AsyncClient) -> bool:
    """Re-mint the access JWT after a 401. False if this scheme cannot renew.

    THE ACCESS JWT EXPIRES DURING LONG UPLOADS. A 20000-frame dataset is 800
    import requests over the better part of an hour, and an access token minted
    at the start will not survive it. Without this the import dies halfway
    through with a bare 401 and no indication that the remedy is automatic.
    """
    global _auth
    if _auth is None or not _auth.get("renewable"):
        return False
    access = await _mint_access_token(client, settings.labelstudio_api_key.strip())
    if not access:
        return False
    _auth = {**_auth, "token": access}
    log.info("labelstudio.auth.renewed")
    return True


async def _post_batch(
    client: httpx.AsyncClient, project_id: int, files: list[tuple[str, bytes]],
) -> httpx.Response:
    """POST one batch, re-minting and retrying exactly once on a 401.

    ONE MULTIPART FIELD NAME PER FILE. Label Studio reads the upload as a dict
    keyed by field name, so 25 parts all called "file" collapse to the LAST
    one: each request created one task and dropped the other 24, silently and
    with a 201. Measured on LS 1.23.0 — a 1901-frame dataset imported as 77
    tasks (one per request), every one of them frame index ≡ 24 mod 25. The
    frame's own filename is unique within a dataset, so it is the field name.
    """
    def payload():
        return [
            (name, (name, data,
                    mimetypes.guess_type(name)[0] or "image/jpeg"))
            for name, data in files
        ]

    url = f"{_base()}/api/projects/{project_id}/import"
    resp = await client.post(url, headers=await _headers(client), files=payload())
    if resp.status_code == 401 and await _renew(client):
        resp = await client.post(url, headers=await _headers(client), files=payload())
    return resp


def _count_tasks(body: Any, sent: int) -> int:
    """Task count from an import response, falling back to what we sent.

    The response shape has changed across Label Studio versions; the count is
    reporting only, so an unrecognised shape degrades to the number of files in
    the batch rather than failing an import that actually succeeded.
    """
    if isinstance(body, dict):
        for key in ("task_count", "tasks_count", "annotation_count"):
            value = body.get(key)
            if isinstance(value, int):
                return value
        tasks = body.get("tasks")
        if isinstance(tasks, list):
            return len(tasks)
    elif isinstance(body, list):
        return len(body)
    return sent


async def import_frames(project_id: int, frames_dir: str, frames: list[dict]) -> dict:
    """Upload the dataset's frames into an existing project.

    Returns ``{tasks, sent, missing_on_disk, missing_examples}``. The last two
    are the point: a frame the manifest lists but the directory no longer holds
    is skipped, and until this returned them the skip was a log line only — the
    import dialog reported "77 tasks" for a 1000-frame dataset as a clean
    success, with nothing to say where the other 923 went.

    UPLOAD THE IMAGES; DO NOT LINK TO s3://.

    The tempting alternative is to import tasks whose `$image` is the S3 URI the
    mirror just wrote. It renders only if the project's source storage is
    configured to presign exactly those keys — and when it is not, THE IMPORT
    STILL REPORTS SUCCESS while every task shows "There was an issue loading URL
    from $image value". A silent, after-the-fact failure on the one step whose
    whole purpose is making images visible to an annotator. Uploading has no
    such mode: HTTP 200 means Label Studio holds the bytes and serves them
    itself from /data/upload/.

    THIS NEVER CREATES A PROJECT. The operator names an existing project for
    every import, so frames always land somewhere already configured with the
    right label config. A project invented here would have no labels on it.

    The uploaded tasks are UNLABELLED, deliberately: a dataset is raw collected
    footage, and drawing the boxes is the point of sending it.
    """
    if not configured():
        raise LabelStudioError(
            "Label Studio is not configured. Set LABELSTUDIO_URL and "
            "LABELSTUDIO_API_KEY on the api service."
        )
    if not frames:
        raise LabelStudioError("This dataset has no frames to import.")

    root = Path(frames_dir)
    total = 0
    sent_files = 0
    missing_on_disk = 0
    missing_examples: list[str] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        batch: list[tuple[str, bytes]] = []
        for frame in frames:
            path = root / str(frame["filename"])
            try:
                batch.append((path.name, path.read_bytes()))
            except OSError:
                # The manifest and the directory have diverged — the volume was
                # recreated, or someone cleaned up by hand. Skip and report;
                # refusing the whole import would be worse for an operator who
                # lost one frame out of four hundred.
                missing_on_disk += 1
                if len(missing_examples) < 5:
                    missing_examples.append(path.name)
                continue
            if len(batch) >= _BATCH:
                total += await _send(client, project_id, batch)
                sent_files += len(batch)
                batch = []
        if batch:
            total += await _send(client, project_id, batch)
            sent_files += len(batch)

    if sent_files == 0:
        raise LabelStudioError(
            f"None of this dataset's {len(frames)} frames are readable at "
            f"{frames_dir}. Re-collect the dataset before importing."
        )
    if missing_on_disk:
        log.warning("labelstudio.frames_missing_on_disk",
                    project_id=project_id, missing=missing_on_disk,
                    examples=missing_examples)
    if total < sent_files:
        # Label Studio answered 201 for every batch but made fewer tasks than
        # files. It is how the one-field-name bug looked from here: logged as
        # a clean import of `tasks=77`. Say so wherever it is reported.
        log.warning("labelstudio.fewer_tasks_than_files", project_id=project_id,
                    files=sent_files, tasks=total)
    log.info("labelstudio.imported", project_id=project_id,
             files=sent_files, tasks=total, missing=missing_on_disk)
    return {
        "tasks": total,
        "sent": sent_files,
        "missing_on_disk": missing_on_disk,
        "missing_examples": missing_examples,
    }


async def _send(
    client: httpx.AsyncClient, project_id: int, batch: list[tuple[str, bytes]],
) -> int:
    resp = await _post_batch(client, project_id, batch)
    if resp.status_code in (200, 201):
        try:
            return _count_tasks(resp.json(), len(batch))
        except ValueError:
            return len(batch)

    detail = (resp.text or "")[:400]
    if resp.status_code == 404:
        raise LabelStudioError(
            f"Label Studio has no project {project_id}. Check the project id — "
            "it is the number in the project's URL."
        )
    if resp.status_code in (401, 403):
        reset_auth()
        raise LabelStudioError(
            "Label Studio rejected the access token while importing. Copy a "
            "fresh one from Account & Settings -> Access Token."
        )
    raise LabelStudioError(
        f"Label Studio refused the import (HTTP {resp.status_code}): {detail}"
    )
