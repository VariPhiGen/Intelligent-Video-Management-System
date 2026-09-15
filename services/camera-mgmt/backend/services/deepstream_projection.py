"""Projection of the analytics registry onto the DeepStream config directory.

The DeepStream pipeline and this VMS always run on the same (air-gapped)
appliance, so the two are wired together through a shared directory rather
than over HTTP: this module writes one ``<slug>.json`` file per camera into
``settings.deepstream_config_dir``, which is bind-mounted into the pipeline
container at its ``config/camera_config``. The pipeline already scans that
directory on boot, so no network hop, no service account, and no startup
ordering between the two services is required — the config is simply on disk
before either process needs it.

The file content is whatever ``services/deepstream.py`` derives from
``Camera.analytics_config``; this module owns only *placement*, not shape.

**Projection, not patching.** ``project_all`` rewrites the full desired set
and removes orphans every time, so the directory converges on the database
from any starting state. A write that failed while the disk was full, a
hand-edit by someone debugging on site, or a change made while this process
was down is all repaired by the next sweep. That is why the request-path
hooks are allowed to fail silently (see ``project_now``) — correctness comes
from the sweep, not from any individual call landing.

**Single writer.** This VMS owns every file it writes here, marked with a
``_managed_by`` key. Files without that marker are left strictly alone —
never rewritten, never deleted — so the directory can still hold
hand-authored camera configs that predate this integration, and so a
half-configured install can't wipe the operator's existing pipeline setup.
The pipeline side must therefore treat the directory as read-only; if it
deletes a file itself, the next sweep simply puts it back.

**Coordinates stay normalized.** ``deepstream_config`` is called without
``width``/``height``, so region points are emitted as the stored 0–1 floats
and the pipeline scales them against its own muxer resolution at load time.
The VMS does not know (and must not have to be told) the pipeline's frame
size: duplicating it here would silently invalidate every zone on disk the
day someone changes the streammux config.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Camera, CameraStage
from .deepstream import deepstream_config

log = structlog.get_logger(__name__)

# Stamped into every file this module writes; the sole basis on which a file
# is considered ours to overwrite or delete.
_MARKER = "vms-camera-mgmt"

# Slugs become filenames. `validate_slug_format` already guarantees this
# shape on the write path, but this module reads storage directly (rows may
# predate that validator), and a slug containing a separator would escape the
# projection directory. Anything that doesn't match is skipped, not sanitized:
# a rewritten name wouldn't match what the pipeline expects anyway.
_SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Serializes the sweep across uvicorn workers. Per-file writes are atomic, so
# a race is never destructive, but two workers projecting different snapshots
# can briefly disagree about orphans. Advisory-locking the sweep (the same
# tool routers/cameras.py uses for slug reservation) removes that window.
_LOCK_KEY = 0x0D5C0F16  # arbitrary, fixed: "ds config"


def should_project(camera: Camera) -> bool:
    """Whether this camera gets a config file.

    Deliberately a Python predicate rather than only a SQL ``WHERE``: the
    query is an optimisation, this is the definition. Split across the two,
    the rule drifts the first time someone widens the query, and "why is a
    disabled camera still being analysed" is not a question anyone wants to
    answer from a pipeline log. Matches the predicate the
    ``GET /analytics/deepstream`` endpoints use, so the pull and file paths
    always agree on which cameras exist.
    """
    if camera.stage != CameraStage.REGISTERED.value or not camera.enabled:
        return False
    cfg = camera.analytics_config or {}
    return bool(cfg.get("regions") or cfg.get("activities"))


@dataclass(frozen=True)
class ProjectionResult:
    # Slugs whose file was created or rewritten, and whose file was deleted,
    # this sweep. deepstream_client pushes exactly these to the pipeline —
    # writing the file makes the change durable, pushing makes it live now.
    written_slugs: tuple[str, ...] = ()
    removed_slugs: tuple[str, ...] = ()
    unchanged: int = 0
    skipped: int = 0
    locked_out: bool = False

    @property
    def written(self) -> int:
        return len(self.written_slugs)

    @property
    def removed(self) -> int:
        return len(self.removed_slugs)

    def __str__(self) -> str:  # for log payloads
        return (f"written={self.written} unchanged={self.unchanged} "
                f"removed={self.removed} skipped={self.skipped}")


def _config_dir() -> Path:
    return Path(settings.deepstream_config_dir)


def _write_atomic(path: Path, payload: str) -> None:
    """Replace `path` with `payload` in one step.

    The pipeline polls this directory and reads whatever it finds, with no
    lock between us — writing in place would let it read a half-written file
    and drop the camera's config. Temp file in the *same* directory (rename
    is only atomic within a filesystem) plus fsync so the content, not just
    the directory entry, survives a power cut on an appliance nobody is
    watching."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leaving a stray dotfile behind would make it look like a config.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_ours(path: Path) -> bool:
    """Whether this module wrote `path`, and may therefore delete it.

    Unreadable or non-JSON files answer False: without positive proof of
    ownership we leave the file alone. Atomic writes mean a file we wrote is
    never truncated, so an unparseable file is someone else's problem, not a
    half-finished one of ours."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh).get("_managed_by") == _MARKER
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _serialize(envelope: dict) -> str:
    # Key order is left as built (deterministic, and readable in that order —
    # sensor_id/url first — which matters when the only debugging tool on an
    # air-gapped box is `cat`). Deterministic output is what makes the
    # unchanged-file comparison below a valid no-op test.
    return json.dumps(envelope, indent=2) + "\n"


async def project_all(db: AsyncSession) -> ProjectionResult:
    """Rewrite the config directory to match the registry. Returns counts.

    A camera is projected when it is registered, enabled, and has a non-empty
    analytics config — the same predicate the `GET /analytics/deepstream`
    endpoints use, so the pull and file paths can never disagree about which
    cameras exist. Cameras failing it are not written, and any file they left
    behind is removed: a camera that was disabled or had its last activity
    deleted must stop being analysed, and leaving a stale file would silently
    resurrect it on the pipeline's next restart.
    """
    directory = _config_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("deepstream.projection.dir_unavailable", dir=str(directory), error=str(exc))
        raise

    got_lock = (await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _LOCK_KEY}
    )).scalar()
    if not got_lock:
        # Another worker is mid-sweep on the same data; its result is ours.
        log.debug("deepstream.projection.skipped_locked")
        return ProjectionResult(locked_out=True)

    # The WHERE is a pre-filter; `should_project` is the authority (it re-checks
    # both clauses, so a future query change can't silently widen the set).
    result = await db.execute(
        select(Camera).where(
            Camera.stage == CameraStage.REGISTERED.value,
            Camera.enabled.is_(True),
        )
    )

    desired: dict[str, str] = {}
    skipped = 0
    for camera in result.scalars():
        if not should_project(camera):
            continue
        slug = camera.slug or ""
        if not _SAFE_SLUG.match(slug):
            log.warning("deepstream.projection.unsafe_slug", slug=slug, camera_id=str(camera.id))
            skipped += 1
            continue
        envelope = deepstream_config(camera)
        envelope["_managed_by"] = _MARKER
        desired[slug] = _serialize(envelope)

    written_slugs: list[str] = []
    removed_slugs: list[str] = []
    unchanged = 0

    for slug, payload in desired.items():
        path = directory / f"{slug}.json"
        try:
            # Only touch the file when the bytes differ. An unnecessary write
            # is not free: it makes this slug look changed, and the client
            # then re-pushes it to the pipeline, which rebuilds the camera's
            # tracking state for nothing.
            if path.exists() and path.read_text(encoding="utf-8") == payload:
                unchanged += 1
                continue
            _write_atomic(path, payload)
            written_slugs.append(slug)
        except OSError as exc:
            log.error("deepstream.projection.write_failed", slug=slug, error=str(exc))
            skipped += 1

    try:
        on_disk = list(directory.glob("*.json"))
    except OSError as exc:
        log.error("deepstream.projection.scan_failed", dir=str(directory), error=str(exc))
        on_disk = []

    for path in on_disk:
        if path.stem in desired or not _is_ours(path):
            continue
        try:
            path.unlink()
            removed_slugs.append(path.stem)
            log.info("deepstream.projection.removed", slug=path.stem)
        except OSError as exc:
            log.error("deepstream.projection.unlink_failed", path=str(path), error=str(exc))
            skipped += 1

    return ProjectionResult(
        written_slugs=tuple(written_slugs), removed_slugs=tuple(removed_slugs),
        unchanged=unchanged, skipped=skipped,
    )


async def project_now(reason: str) -> ProjectionResult:
    """Sweep on its own session, never raising into the caller.

    Callers reach this through `deepstream_client.sync_now`, which pushes the
    changed slugs to the running pipeline afterwards. The operator's write is
    already committed by the time we run, so a projection failure must not
    turn a successful save into an error response — it is logged and left for
    the client's reconcile loop to repair.
    """
    if not settings.deepstream_projection_enabled:
        return ProjectionResult()

    from ..db import AsyncSessionLocal  # local: avoids an import cycle at load

    try:
        async with AsyncSessionLocal() as db:
            res = await project_all(db)
        if not res.locked_out and (res.written or res.removed):
            log.info("deepstream.projection.applied", reason=reason,
                     written=res.written, removed=res.removed, skipped=res.skipped)
        return res
    except Exception as exc:  # noqa: BLE001 — must not surface to the request
        log.warning("deepstream.projection.failed", reason=reason, error=str(exc))
        return ProjectionResult()


def pipeline_path(slug: str) -> str:
    """A camera's config file as the *pipeline container* sees it.

    Same file, two mount points: this API writes it under
    `deepstream_config_dir` (/deepstream-config), the pipeline reads it under
    `deepstream_pipeline_config_dir` (/apps/…/config/camera_config). The
    pipeline's add/modify endpoints take a path and open it themselves, so
    they must be handed *their* view of it — sending ours yields a
    file-not-found against a path that plainly exists on the host, which is a
    genuinely confusing thing to debug.
    """
    return f"{settings.deepstream_pipeline_config_dir.rstrip('/')}/{slug}.json"
