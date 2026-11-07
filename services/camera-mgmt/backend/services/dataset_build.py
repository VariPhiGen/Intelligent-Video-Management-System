"""dataset_build.py — sample training JPEGs from a camera's NVR recordings.

Given a dataset recipe ("camera X, 10:00-12:00, 200 frames") this plans the
timestamps, pulls one snapshot per timestamp from the NVR, writes them to the
dataset's own directory and returns a manifest.

THE FETCHER IS INJECTED. `collect_frames` takes the snapshot function rather
than importing nvr_client, so the whole sampler is testable without an NVR,
without ffmpeg and without recorded footage — which is the only way the gap
handling and the pre-run cleanup below get tested at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

import structlog

log = structlog.get_logger(__name__)

# (slug, iso_timestamp, jpeg_quality) -> JPEG bytes, or None where the NVR
# holds no footage covering that instant.
# A fetcher answers with either the JPEG bytes (None meaning "no frame here"),
# or a ``(bytes|None, reason)`` pair that also names WHY there is none. The pair
# is what services/nvr_client.fetch_snapshot returns and is the only form that
# can tell a recording gap apart from an appliance that failed to extract; the
# bare form stays supported so a test can inject a plain lambda.
SnapshotOutcome = Union[Optional[bytes], "tuple[Optional[bytes], str]"]
SnapshotFetcher = Callable[[str, str, int], Awaitable[SnapshotOutcome]]

# In-flight snapshot requests per collection.
#
# This is one of TWO bounds and neither is redundant. The NVR caps snapshot
# extraction appliance-wide (api/server.py `_snapshot_semaphore`), which is what
# stops several concurrent collections from swamping the box. This one caps a
# SINGLE run, so one dataset cannot occupy every slot on the recorder for
# minutes at a time and starve an operator taking a snapshot in Playback.
#
# Raising it does not make a run faster: past the NVR's own cap the extra
# requests just queue there while holding sockets open here.
#
# TWO is measured, not guessed. On the reference appliance one snapshot costs
# ~8 s (HEVC seek, decode, JPEG encode) and the NVR kills any extraction that
# runs past its own ffmpeg budget. At four in flight every request on that box
# exceeded the budget and returned 500, so a run collected nothing while the
# footage was present the whole time. Callers may override per run.
_CONCURRENCY = 2

# How often the progress callback fires, in frames. Frequent enough that a UI
# progress bar moves and the row's `updated_at` keeps the stale-run reaper at
# bay; rare enough that a 20000-frame run is 2000 small UPDATEs, not 20000.
_PROGRESS_EVERY = 10

# How many timestamps to hand the NVR's /snapshot/batch at a time. Chosen to
# sit under the NVR's own _BATCH_MAX while still spanning several segments, so
# each decode is amortised over every frame wanted from that segment.
_BATCH_CHUNK = 120

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class DatasetBuildError(Exception):
    """The run produced nothing usable. Carries a message meant for an
    operator, not a stack trace."""


def safe_slug(slug: str) -> str:
    """Filesystem-safe form of a camera slug, for the dataset directory name."""
    return _UNSAFE.sub("_", slug or "") or "camera"


def plan_timestamps(
    window_start: datetime, window_end: datetime, target_frames: int,
) -> list[datetime]:
    """The instants to sample, evenly spaced across the window.

    MIDPOINTS, NOT ENDPOINTS. The obvious formula — ``start + k*span/(N-1)`` —
    puts the first and last frames exactly ON the window edges, which is
    precisely where the NVR is least likely to have a covering segment: a
    request for 10:00:00.000 needs a segment that started at or before
    10:00:00.000, and grooming and retention both chew at the front of a
    window. Sampling at the midpoint of each of N equal slices keeps every
    timestamp strictly inside the window, evenly spaced, and independent of N's
    parity.

    Pure by design: `models.dataset_interval_seconds` computes the spacing the
    UI displays from the same two inputs, so the number an operator reads and
    the frames this produces cannot disagree.
    """
    span = (window_end - window_start).total_seconds()
    step = span / target_frames
    return [
        window_start + timedelta(seconds=step * (k + 0.5))
        for k in range(target_frames)
    ]


def _iso_z(ts: datetime) -> str:
    """UTC ISO 8601 with a Z suffix.

    Not `+00:00`: these strings end up in filenames' sibling manifest and get
    read by downstream training tooling that is much happier with Z.
    """
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def plan_scan_timestamps(
    window_start: datetime, window_end: datetime, interval_seconds: float,
    max_scans: int = 20000,
) -> list[datetime]:
    """Instants to scan in detection mode: one every `interval_seconds`, each
    strictly inside the window (first at half an interval, for the same reason
    plan_timestamps uses midpoints)."""
    span = (window_end - window_start).total_seconds()
    if span <= 0 or interval_seconds <= 0:
        return []
    n = min(int(span // interval_seconds), max_scans)
    return [window_start + timedelta(seconds=interval_seconds * (k + 0.5))
            for k in range(max(n, 0))]


# JPEG bytes -> detections of the wanted classes only (empty = nothing found).
FrameDetector = Callable[[bytes], Awaitable[list]]


def frames_dir_for(dataset_id: int, camera_slug: str, out_root: str) -> str:
    """Every dataset owns exactly one directory, so deleting the row can delete
    its bytes in a single move."""
    return str(Path(out_root) / f"{dataset_id}-{safe_slug(camera_slug)}")


def _clear_previous_run(directory: Path) -> int:
    """Delete the previous run's frames. Returns how many were removed.

    RE-RUNS MUST START CLEAN. Collecting the same dataset again at a different
    target writes a different set of filenames — `frame_00007_*.jpg` from a
    500-frame run is a different instant than from a 200-frame one. Leaving the
    old files behind interleaves two runs in one directory, and the manifest
    then describes something other than what is on disk: the frame grid shows
    images the manifest does not list, and an S3 mirror walking the directory
    would upload frames from a sampling interval nobody asked for.

    Only `frame_*.jpg` is touched, so anything an operator put in the directory
    themselves survives.
    """
    removed = 0
    for stale in directory.glob("frame_*.jpg"):
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:
            # Not fatal: a file we cannot delete is at worst a stale extra, and
            # failing the whole run over it would be a worse trade.
            log.warning("dataset.cleanup_failed", path=str(stale), error=str(exc))
    return removed


async def collect_frames(
    dataset: dict[str, Any],
    out_root: str,
    fetcher: SnapshotFetcher,
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    concurrency: Optional[int] = None,
    batch_fetcher: Optional[Callable[..., Awaitable[dict]]] = None,
    detector: Optional[FrameDetector] = None,
) -> dict[str, Any]:
    """Sample the dataset's window and write the frames to disk.

    DETECTION MODE. With ``detector`` given, the window is scanned every
    ``dataset["scan_interval_seconds"]`` and a frame is written ONLY if the
    detector returns at least one detection; ``target_frames`` is then the cap
    on frames kept. Frames with nothing detected are counted in ``scanned`` but
    never touch disk. A detector error skips that frame (counted as missing).

    ``dataset`` needs: id, camera_slug, window_start, window_end, target_frames
    and jpeg_quality.

    Returns ``{frames_dir, frames, collected, missing, bytes_total}`` where
    ``frames`` is the manifest: one ``{filename, timestamp, sha256, bytes}``
    per image, in capture order.

    A MISSING FRAME IS COUNTED, NOT FATAL. Recordings have gaps — a camera
    dropped, the NVR restarted, retention groomed the front of the window — and
    aborting on the first one would throw away the hundreds of frames that did
    extract. A write OSError (disk full, unwritable volume) is treated the same
    way for the same reason. The run fails only if NOTHING was collected, which
    is the one case where continuing would leave an empty dataset marked ready.
    """
    dataset_id = int(dataset["id"])
    slug = str(dataset["camera_slug"])
    target = int(dataset["target_frames"])
    quality = int(dataset.get("jpeg_quality") or 85)

    directory = Path(frames_dir_for(dataset_id, slug, out_root))
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Distinct from a per-frame write failure: if the directory itself
        # cannot exist, every frame would fail and the run is already over.
        # PermissionError here almost always means the volume was created
        # without the image's chown (see Dockerfile.backend).
        raise DatasetBuildError(
            f"Could not create the frame directory {directory}: {exc}. "
            "Check that the dataset volume is writable by the api service."
        ) from exc

    _clear_previous_run(directory)

    # A PTZ dataset at chosen presets hands the instants in (services/ptz_dataset): only the time the camera was at
    # those presets, each with the preset it shows, which goes into the frame's manifest entry.
    plan = dataset.get("plan")
    frame_meta: dict[int, dict] = {}
    if plan is not None:
        timestamps = [p["at"] for p in plan]
        frame_meta = {i: {k: v for k, v in p.items() if k != "at" and v} for i, p in enumerate(plan)}
    elif detector is not None:
        timestamps = plan_scan_timestamps(
            dataset["window_start"], dataset["window_end"],
            float(dataset.get("scan_interval_seconds") or 2.0))
    else:
        timestamps = plan_timestamps(
            dataset["window_start"], dataset["window_end"], target)
    scanned = 0
    no_detection = 0

    # Index-keyed rather than appended, because the workers finish out of order
    # and the manifest must stay in capture order — the filenames encode an
    # ordering and a manifest that disagreed with them would be quietly wrong.
    collected: dict[int, dict[str, Any]] = {}
    missing = 0
    done = 0
    # Frames the batch pass already decoded for the chunk in flight. Drained
    # as they are consumed so it never holds more than one chunk.
    prefetched: dict[str, bytes] = {}
    # Why each absent frame was absent, so a run that collected nothing can say
    # which of the two very different causes it hit.
    reasons: dict[str, int] = {}
    in_flight = max(1, int(concurrency or _CONCURRENCY))
    semaphore = asyncio.Semaphore(in_flight)
    lock = asyncio.Lock()

    async def one(idx: int, ts: datetime) -> None:
        nonlocal missing, done, scanned, no_detection
        async with semaphore:
            if detector is not None and len(collected) >= target:
                # Cap reached: stop paying for snapshots and inference.
                async with lock:
                    done += 1
                return
            data: Optional[bytes] = None
            reason = "error"
            try:
                iso = _iso_z(ts)
                if iso in prefetched:
                    # Already decoded by the batch pass for this chunk. A key
                    # that is ABSENT there is not "no frame" -- the batch may
                    # have failed wholesale -- so only a present key short
                    # circuits, and anything else still asks per frame.
                    result = (prefetched.pop(iso), "ok")
                else:
                    result = await fetcher(slug, iso, quality)
                # Both shapes accepted — see SnapshotOutcome. A bare value
                # carries no diagnosis, so infer the only two it can express.
                if isinstance(result, tuple):
                    data, reason = result
                else:
                    data, reason = result, ("ok" if result else "gap")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad frame is not a bad run
                log.warning("dataset.frame_fetch_failed", dataset_id=dataset_id,
                            slug=slug, at=_iso_z(ts), error=str(exc))

            detections: list = []
            keep = True
            if data and detector is not None:
                try:
                    detections = await detector(data)
                    keep = bool(detections)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("dataset.detect_failed", dataset_id=dataset_id,
                                at=_iso_z(ts), error=str(exc))
                    data, reason, keep = None, "detector_error", False
                async with lock:
                    scanned += 1
                    if data and not keep:
                        no_detection += 1
                if data and keep:
                    async with lock:
                        if len(collected) >= target:
                            keep = False

            entry: Optional[dict[str, Any]] = None
            if data and keep:
                epoch_ms = int(ts.astimezone(timezone.utc).timestamp() * 1000)
                # The index sorts in capture order; the epoch keeps the frame
                # meaningful after it leaves this system, which is the entire
                # point of collecting it — a JPEG in a training set months from
                # now still says when it was taken.
                filename = f"frame_{idx:05d}_{epoch_ms}.jpg"
                try:
                    (directory / filename).write_bytes(data)
                    entry = {
                        "filename": filename,
                        "timestamp": _iso_z(ts),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }
                    if detector is not None:
                        entry["detections"] = detections
                    entry.update(frame_meta.get(idx) or {})
                except OSError as exc:
                    log.warning("dataset.frame_write_failed", dataset_id=dataset_id,
                                filename=filename, error=str(exc))

            async with lock:
                if entry is not None:
                    collected[idx] = entry
                elif detector is not None and data and not keep:
                    pass        # scanned, nothing of interest: neither kept nor missing
                else:
                    missing += 1
                    # A frame that was fetched but could not be written is a
                    # local failure, not the NVR's answer.
                    key = reason if data is None else "unwritable"
                    reasons[key] = reasons.get(key, 0) + 1
                done += 1
                should_report = progress is not None and (
                    done % _PROGRESS_EVERY == 0 or done == len(timestamps))
            if should_report:
                try:
                    await progress(len(collected), len(timestamps))
                except Exception as exc:  # noqa: BLE001 — progress is cosmetic
                    log.warning("dataset.progress_failed",
                                dataset_id=dataset_id, error=str(exc))

    # CHUNKED, so the batch pass has something to batch. The whole timeline at
    # once would mean holding every decoded frame in memory; one chunk at a
    # time bounds that to _BATCH_CHUNK frames while still letting a segment's
    # worth of offsets share a single decode.
    #
    # Detection still runs concurrently WITHIN a chunk -- the semaphore and the
    # ordering by index are untouched -- so the only thing that changed is
    # where the JPEG bytes came from.
    indexed = list(enumerate(timestamps))
    for start in range(0, len(indexed), _BATCH_CHUNK):
        chunk = indexed[start:start + _BATCH_CHUNK]
        if batch_fetcher is not None:
            try:
                prefetched.update(
                    await batch_fetcher(slug, [_iso_z(t) for _, t in chunk], quality))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — fall back to per frame
                log.warning("dataset.batch_fetch_failed", dataset_id=dataset_id,
                            slug=slug, error=str(exc))
        await asyncio.gather(*(one(i, ts) for i, ts in chunk))
        prefetched.clear()
        # Detection mode stops at the cap; without this the remaining chunks
        # would still pay for their decodes just to throw the frames away.
        if detector is not None and len(collected) >= target:
            break

    frames = [collected[i] for i in sorted(collected)]
    if not frames and detector is not None and no_detection > 0:
        raise DatasetBuildError(
            f"Scanned {scanned} frames for '{slug}' and the model found none of "
            "the selected classes. Try a lower confidence, a different class, "
            "or a window when the subject is present.")
    if not frames and reasons.get("detector_error"):
        raise DatasetBuildError(
            f"The detection model failed on {reasons['detector_error']} frames and "
            "nothing was kept. Check that the analytics service is running and the "
            "model is still uploaded.")
    if not frames:
        window = (f"for '{slug}' between {_iso_z(dataset['window_start'])} "
                  f"and {_iso_z(dataset['window_end'])}")
        # "No recordings" is a CLAIM ABOUT THE FOOTAGE, and it may only be made
        # when the NVR actually said the footage was absent. Every other answer
        # — a timeout, a 500, an unreachable NVR — means the recordings are
        # probably fine and the appliance could not hand them over. Conflating
        # the two sends an operator to audit retention for a load problem.
        failed = {r: n for r, n in reasons.items() if r not in ("gap", "ok")}
        if failed:
            detail = ", ".join(f"{n} {r}" for r, n in sorted(failed.items()))
            raise DatasetBuildError(
                f"No frames could be extracted {window}, and the NVR did not "
                f"report them as missing: {detail}. The recordings are most "
                "likely present — the appliance could not extract them in "
                "time. Retry with fewer frames, or when the box is less busy."
            )
        raise DatasetBuildError(
            f"No frames could be extracted {window}. "
            "The NVR holds no recordings covering this window — check that the "
            "window is still inside the camera's retention period and that "
            "recording was enabled at the time."
        )

    bytes_total = sum(int(f["bytes"]) for f in frames)
    log.info("dataset.collected", dataset_id=dataset_id, slug=slug,
             collected=len(frames), missing=missing, bytes_total=bytes_total,
             concurrency=in_flight, reasons=reasons or None)
    return {
        "frames_dir": str(directory),
        "frames": frames,
        "collected": len(frames),
        "missing": missing,
        "scanned": scanned,
        "bytes_total": bytes_total,
    }


def delete_frames_dir(frames_dir: Optional[str], out_root: str) -> None:
    """Remove a dataset's directory and everything in it.

    Refuses to touch anything outside ``out_root``. `frames_dir` is a stored
    column, and a path that escaped the root — however it got there — must not
    become an arbitrary recursive delete running as the api user.
    """
    if not frames_dir:
        return
    root = Path(out_root).resolve()
    try:
        target = Path(frames_dir).resolve()
    except OSError:
        return
    if target == root or root not in target.parents:
        log.warning("dataset.delete_outside_root", frames_dir=frames_dir, root=str(root))
        return
    import shutil
    shutil.rmtree(target, ignore_errors=True)
