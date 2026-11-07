"""nvr_client.py — camera-registry → NVR recording sync.

The NVR service records whatever cameras it is told about at runtime
(POST/DELETE /cameras/{name}), but that state is **in-memory only** — an NVR
restart loses every runtime-added camera. So this module provides both:

  • event hooks   — called from the camera CRUD endpoints (create/update/delete)
  • reconcile_loop — a background task that periodically re-asserts the desired
    state (enabled registry cameras present, disabled/deleted ones absent)

Cameras are recorded FROM THE RELAY (rtsp://<SERVER_IP>:<RTSP_PORT>/<slug>), so
each physical camera keeps exactly one upstream connection. The NVR camera name
is the slug — the same ``nvr_camera_name`` join key downstream AI pipelines use.

All calls are best-effort: a down NVR must never fail a registry operation
(mirrors the DB-commits-before-relay-calls convention in routers/cameras.py);
the reconcile loop heals any missed sync on its next pass.
"""
from __future__ import annotations

import asyncio
import io
import tarfile
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import structlog

from ..config import settings
from . import retention_units

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)

_client: Optional[httpx.AsyncClient] = None


def _base() -> str:
    return settings.nvr_api_url.rstrip("/")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ── Primitive operations (idempotent) ─────────────────────────────────────────

def _record_url(slug: str) -> str:
    # Loopback by default: NVR and MediaMTX share the host, and this keeps
    # recording working even if SERVER_IP is wrong or the box changes networks.
    return f"rtsp://{settings.nvr_record_host}:{settings.rtsp_port}/{slug}"


async def add_camera(
    slug: str,
    retention_minutes: int | None = None,
    masks: list | None = None,
    groom_after_days: int | None = None,
) -> bool:
    """Start recording ``slug`` from its relay URL. 409 (already exists) is OK.

    ``masks`` (normalized polygons) opts the camera into the NVR's masked
    recording path — the region is burned into every segment (re-encode).

    ``groom_after_days`` is sent only when the camera has its own override:
    None omits it and the NVR's appliance groom default applies, 0 means never
    groom this track, >0 is a threshold in days. The test is `is not None`, not
    truthiness — 0 is the sub track's whole point (grooming rewrites footage
    keyframe-only, which is what a scrub track exists not to be) and a truthy
    test dropped it, handing the sub the appliance default instead.
    """
    import json as _json

    params = {
        "rtsp_url": _record_url(slug),
        "retention_minutes": retention_units.effective_retention_minutes(
            retention_minutes),
    }
    if groom_after_days is not None:
        params["groom_after_days"] = groom_after_days
    if masks:
        params["masks"] = _json.dumps(masks)
    try:
        resp = await _get_client().post(f"{_base()}/cameras/{slug}", params=params)
        if resp.status_code in (200, 409):
            log.info("nvr.sync.add.ok", slug=slug, existed=resp.status_code == 409)
            return True
        log.warning("nvr.sync.add.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.add.unreachable", slug=slug, error=str(exc))
    return False


async def set_retention(slug: str, retention_minutes: int | None) -> bool:
    """Push a camera's retention policy in place (None = appliance default).

    Unlike ``add_camera`` this works on an already-recording camera (the NVR's
    add endpoint 409s without touching retention) and on cameras with no live
    worker, whose footage keeps aging out under the policy.

    Minutes, not days: the NVR sweeps on the same unit, and the appliance
    default is resolved HERE rather than left to the NVR so that the number the
    registry believes it set is the number the recorder deletes on. The two
    services read the default from different files, and a None that each one
    fills in for itself is how they drift.
    """
    minutes = retention_units.effective_retention_minutes(retention_minutes)
    try:
        resp = await _get_client().put(
            f"{_base()}/cameras/{slug}/retention",
            params={"retention_minutes": minutes},
        )
        if resp.status_code == 200:
            log.info("nvr.sync.retention.ok", slug=slug, minutes=minutes)
            return True
        log.warning("nvr.sync.retention.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.retention.unreachable", slug=slug, error=str(exc))
    return False


async def set_groom(slug: str, groom_after_days: int | None) -> bool:
    """Push a camera's groom-after override in place. Same live-or-not
    semantics as set_retention.

    None clears the override back to the NVR's appliance default, 0 means never
    groom this track, >0 is a threshold in days. The parameter is OMITTED to
    clear rather than sent as 0: collapsing both onto 0 (`groom_after_days or 0`)
    made "never" and "use the default" the same request, so a sub track could
    not opt out of grooming at all.
    """
    params: dict[str, int] = {}
    if groom_after_days is not None:
        params["groom_after_days"] = groom_after_days
    try:
        resp = await _get_client().put(
            f"{_base()}/cameras/{slug}/groom", params=params
        )
        if resp.status_code == 200:
            log.info("nvr.sync.groom.ok", slug=slug,
                     policy=("default" if groom_after_days is None
                             else "never" if groom_after_days == 0 else groom_after_days))
            return True
        log.warning("nvr.sync.groom.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.groom.unreachable", slug=slug, error=str(exc))
    return False


async def invalidate_codec_cache(slug: str) -> bool:
    """Tell the NVR that this recording name's codec may have changed.

    The NVR caches codec/resolution per recording name to avoid an ffprobe on
    every playlist. A sub track re-resolved onto a different profile keeps its
    name and changes its codec, so the cached answer becomes a lie — playback
    serves raw HEVC through the stream-copy path and the player goes black with
    nothing logged. Call this from anywhere that repoints a track's source.

    Best-effort like the other sync ops: the NVR's own TTL bounds the damage if
    this call is lost, which is exactly what the TTL is there for.
    """
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}/codec-cache")
        if resp.status_code in (200, 404):
            log.info("nvr.sync.codec_cache.invalidated", slug=slug)
            return True
        log.warning("nvr.sync.codec_cache.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.codec_cache.unreachable", slug=slug, error=str(exc))
    return False


async def remove_camera(slug: str) -> bool:
    """Stop recording ``slug``. 404 (already gone) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}")
        if resp.status_code in (200, 404):
            log.info("nvr.sync.remove.ok", slug=slug, missing=resp.status_code == 404)
            return True
        log.warning("nvr.sync.remove.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.remove.unreachable", slug=slug, error=str(exc))
    return False


async def purge_recordings(slug: str) -> bool:
    """Permanently delete all recorded footage for ``slug``. 404 (no footage) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}/recordings")
        if resp.status_code in (200, 404):
            log.info("nvr.sync.purge.ok", slug=slug, missing=resp.status_code == 404)
            return True
        log.warning("nvr.sync.purge.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.sync.purge.unreachable", slug=slug, error=str(exc))
    return False


async def erase_range(slug: str, from_iso: str, to_iso: str,
                      missing_ok: bool = False) -> Optional[dict[str, Any]]:
    """Scoped erasure: delete segments fully inside [from_iso, to_iso].

    Returns the NVR's payload (deleted segment list, bytes freed, preserved
    boundary overlaps) on success, or None on any failure. Unlike the
    best-effort sync ops above, the caller MUST treat None as a hard failure:
    a caller that reports erasure as complete has made a claim about deleted
    footage, and it has to be true.

    ``missing_ok`` turns the NVR's 404 (this recording name has no indexed
    footage) into an empty success instead of a failure. It exists for the sub
    track: most cameras have never recorded one, and "there is nothing here to
    erase" is a complete answer to an erasure request, not a failed one. Only
    404 is softened — unreachable and refused stay hard failures, because those
    are the cases where footage may still exist and we cannot see it.
    """
    try:
        resp = await _get_client().delete(
            f"{_base()}/cameras/{slug}/recordings/range",
            params={"from": from_iso, "to": to_iso},
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404 and missing_ok:
            log.info("nvr.erase.no_such_track", slug=slug)
            return {"status": "ok", "camera": slug, "from": from_iso, "to": to_iso,
                    "segments_deleted": 0, "bytes_freed": 0, "deleted": [],
                    "skipped_partial_overlap": []}
        log.warning("nvr.erase.failed", slug=slug, status=resp.status_code,
                    detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("nvr.erase.unreachable", slug=slug, error=str(exc))
    return None


async def erase_range_all_tracks(
    slug: str, from_iso: str, to_iso: str
) -> Optional[dict[str, Any]]:
    """Scoped erasure across EVERY recording track of a camera.

    A camera with a sub track holds two copies of the same time window under two
    recording names. Erasing only `<slug>` left a full low-res copy of exactly
    the footage a data principal asked to have deleted sitting on disk, while
    the erasure register and the audit record both said fulfilled — the erasure
    was not partial, the claim about it was false.

    BOTH tracks are erased with ``missing_ok``, the main included: a main whose
    segments all aged out (shorter retention than the sub, or a prior erasure)
    404s while `<slug>_sub` still holds the window — aborting on that 404 left
    the low-res copy in place with the register saying fulfilled, the exact bug
    the whole-camera purge fan-out fixed. "This track has no footage there" is
    a complete answer for either track; unreachable and refused stay hard
    failures. The returned payload is the main's, with the sub's deletions
    folded into the totals and its segment list appended, so a caller embedding
    this in an audit record reports what was actually removed across both
    tracks. None if EITHER track failed — a half-done erasure must never read
    as done.
    """
    from ..models import sub_recording_name

    main = await erase_range(slug, from_iso, to_iso, missing_ok=True)
    if main is None:
        return None
    sub = await erase_range(sub_recording_name(slug), from_iso, to_iso,
                            missing_ok=True)
    if sub is None:
        log.error("nvr.erase.sub_track_failed", slug=slug,
                  detail="main erased; low-res copy may remain")
        return None
    return {
        **main,
        "segments_deleted": main.get("segments_deleted", 0) + sub.get("segments_deleted", 0),
        "bytes_freed": main.get("bytes_freed", 0) + sub.get("bytes_freed", 0),
        "deleted": [*main.get("deleted", []), *sub.get("deleted", [])],
        "skipped_partial_overlap": [*main.get("skipped_partial_overlap", []),
                                    *sub.get("skipped_partial_overlap", [])],
        # Named per track so the audit record shows the sub was considered at
        # all — "0 segments" and "never asked" have to stay distinguishable
        # years later, the same reason the search index reports [].
        "tracks_erased": {slug: main.get("segments_deleted", 0),
                          sub_recording_name(slug): sub.get("segments_deleted", 0)},
    }


class ClipUnavailable(Exception):
    """The NVR could not produce the requested clip.

    Carries the NVR's own reason. This exists because the previous contract —
    return None on ANY failure — erased the difference between "there is no
    footage in that window", "the NVR is down" and "you asked for more than
    this NVR will extract". The caller could only report the first two, so a
    3-hour export refused for exceeding a limit was shown to the operator as
    missing footage, and they went looking for recordings that were there.
    """


def _refusal_reason(resp: "httpx.Response") -> str:
    """A sentence from the NVR's error body, or a bare status if it has none."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        parts = [str(detail[k]) for k in ("error", "detail") if detail.get(k)]
        if parts:
            return " — ".join(parts)
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, list) and detail:                  # pydantic 422
        first = detail[0]
        if isinstance(first, dict) and first.get("msg"):
            loc = ".".join(str(p) for p in first.get("loc", [])[-1:])
            return f"{loc}: {first['msg']}" if loc else str(first["msg"])
    return f"HTTP {resp.status_code}"


@dataclass(frozen=True)
class Clip:
    """An extracted clip and what the recorder said about the window it came from.

    `coverage` is the fraction of the REQUESTED window the recorder actually
    held segments for. The NVR omits the header when coverage is full, so a
    missing header means 1.0 — but an UNPARSEABLE one means "it told us
    something we did not understand", which must never be read as full
    coverage: that is the direction that produces a false attestation.
    """
    data: bytes
    coverage: Optional[float]
    warning: Optional[str] = None


def _parse_coverage(raw: str) -> Optional[float]:
    """None when the header cannot be trusted, never a guess."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("nvr.clip.coverage_unparseable", raw=raw)
        return None
    if not 0.0 <= value <= 1.0:
        log.warning("nvr.clip.coverage_out_of_range", raw=raw)
        return None
    return value


async def fetch_clip(slug: str, from_iso: str, to_iso: str) -> "Clip":
    """Pull an MP4 clip for slug over [from_iso, to_iso] from the NVR's /clip
    endpoint (remux; stream-copy). Returns a Clip, or raises
    ClipUnavailable with a reason an operator can act on.

    The /clip endpoint centres on a timestamp with before/after, so we map the
    window onto that: centre = midpoint, before/after = half-span.
    """
    from datetime import datetime
    try:
        f = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
        t = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClipUnavailable(
            f"Clip window for {slug} is not a valid timestamp range "
            f"({from_iso}..{to_iso})."
        ) from exc
    span = (t - f).total_seconds()
    if span <= 0:
        raise ClipUnavailable(
            f"Clip window for {slug} ends at or before it starts "
            f"({from_iso}..{to_iso})."
        )
    half = span / 2.0
    centre = f + (t - f) / 2
    centre_iso = centre.isoformat().replace("+00:00", "Z")

    # The read budget must EXCEED the NVR's own ffmpeg budget, which scales with
    # the clip. If this timed out first we would report a transport failure for
    # a request the NVR was still serving correctly — the client's own deadline
    # masquerading as the recorder's fault. 300 s was a constant sized for the
    # short clips /clip used to allow.
    read_timeout = max(300.0, 120.0 + 1.5 * span)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=5.0)
        ) as client:
            resp = await client.get(f"{_base()}/clip", params={
                "camera": slug, "timestamp": centre_iso, "before": half, "after": half,
            })
    except httpx.HTTPError as exc:
        log.warning("nvr.clip.unreachable", slug=slug, error=str(exc))
        raise ClipUnavailable(
            f"The NVR could not be reached while extracting {slug} "
            f"{from_iso}..{to_iso}: {exc}"
        ) from exc

    if resp.status_code == 200:
        # The recorder has ALREADY measured whether it actually held footage
        # across the whole requested window (server.py `_compute_coverage`) and
        # says so in these headers. Dropping them here is what left the exported
        # evidence certificate asserting "system operating properly" from a
        # literal.
        cov_raw = resp.headers.get("X-NVR-Coverage")
        coverage = 1.0 if cov_raw is None else _parse_coverage(cov_raw)
        return Clip(data=resp.content, coverage=coverage,
                    warning=resp.headers.get("X-NVR-Warning"))

    reason = _refusal_reason(resp)
    log.warning("nvr.clip.failed", slug=slug, status=resp.status_code, reason=reason)
    raise ClipUnavailable(
        f"The NVR could not extract {slug} {from_iso}..{to_iso}: {reason}"
    )


async def list_cameras() -> Optional[set[str]]:
    """Names with an ACTIVE recording worker, or None if the NVR is unreachable.

    The NVR's /cameras lists every camera with *indexed footage* — including
    ones whose worker died with an NVR restart (runtime cameras are in-memory).
    Only entries carrying an ``rtsp_url`` have a live worker, so filter on it:
    reconcile must re-add index-only cameras or recording silently stays off.
    """
    try:
        resp = await _get_client().get(f"{_base()}/cameras")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return {c["name"] for c in body.get("cameras", []) if c.get("rtsp_url")}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("nvr.sync.list.unreachable", error=str(exc))
        return None


# ── Reconciliation ────────────────────────────────────────────────────────────

async def reconcile(
    enabled: dict[str, tuple[list | None, int | None, int | None]],
    disabled_slugs: set[str],
) -> None:
    """Re-assert desired NVR state.

    • every enabled registry camera is recording (add is idempotent);
      ``enabled`` maps slug → (privacy masks, retention_minutes,
      groom_after_days) so a re-add after an NVR restart keeps the camera on
      the masked recording path and on its own retention/grooming policies
      (all of which live only in NVR memory)
    • registry cameras that are disabled/deleted are not recording

    Cameras the NVR knows about but the registry doesn't (manual cameras.yaml
    entries) are left alone — the registry only manages its own.

    Keys here are RECORDING NAMES, not slugs: a camera with an enabled sub track
    contributes both `<slug>` and `<slug>_sub`. Comparing slugs alone would read
    `<slug>_sub` as a stranger and leave it alone forever — including after the
    sub is switched off, which would keep recording a stream nobody wants.
    """
    current = await list_cameras()
    if current is None:
        return  # NVR down; next pass will retry

    for slug in sorted(set(enabled) - current):
        masks, retention, groom = enabled[slug]
        await add_camera(slug, retention_minutes=retention, masks=masks,
                         groom_after_days=groom)
    for slug in sorted(disabled_slugs & current):
        await remove_camera(slug)


async def reconcile_loop() -> None:
    """Background task: periodic registry→NVR reconciliation.

    Heals NVR restarts (runtime cameras are in-memory) and any hook call that
    failed while the NVR was briefly unreachable.
    """
    # Local import to avoid a circular import at module load time.
    from sqlalchemy import select

    from types import SimpleNamespace

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage, sub_recording_name
    from . import schedule as schedule_svc
    from . import tracks
    from .tracks import tracks_for

    log.info("nvr.sync.loop_started", interval=settings.nvr_sync_interval)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            Camera.slug, Camera.enabled, Camera.recording,
                            Camera.privacy_masks, Camera.recording_schedule,
                            Camera.retention_minutes, Camera.groom_after_days,
                            Camera.rtsp_url, Camera.sub_track,
                        ).where(Camera.stage == CameraStage.REGISTERED.value)
                    )
                ).all()
            # A camera records iff it is enabled AND its recording flag is on
            # AND its schedule (None = 24/7) is in-window right now. This loop
            # is the schedule enforcer: it starts/stops workers at window
            # edges, so schedule granularity = nvr_sync_interval (default 60s).
            # Desired state is per TRACK, and the two tracks do NOT share a
            # lifecycle.
            #
            # The sub keeps its own, much shorter retention. It exists so that
            # scrubbing recent footage is fast; the main is what evidence is
            # drawn from. Keeping both for the same period doubles the storage
            # cost of the feature well past the point it buys anything, and once
            # the sub ages out playback simply falls back to the main — the NVR
            # checks coverage per track, so an expired sub is a track with no
            # footage at that time.
            #
            # The sub is also NOT groomed. Grooming rewrites footage
            # keyframe-only, which plays as a slideshow — precisely destroying
            # the one thing a scrub track is for. `0` disables it.
            #
            # It DOES inherit the privacy masks, and that is not optional: a
            # mask exists to redact a region, and an unmasked sub of a masked
            # camera would publish exactly what the mask hides, at a URL the
            # same operators can reach. It costs a re-encode on the sub as well
            # (the recorder burns masks in), which is the price of the mask
            # meaning what it says.
            enabled: dict[str, tuple] = {}
            disabled: set[str] = set()
            for (slug, en, rec, masks, sched, retention, groom,
                 url, sub_track) in rows:
                on = en and rec and schedule_svc.is_active(sched)
                cam = SimpleNamespace(slug=slug, rtsp_url=url, sub_track=sub_track)
                sub_name = sub_recording_name(slug)
                names = [t.recording_name for t in tracks_for(cam)]
                if on:
                    for name in names:
                        if name == sub_name:
                            enabled[name] = (masks,
                                             tracks.sub_retention_minutes(cam, retention),
                                             0)
                        else:
                            # `or None`: 0 means "never groom" on the wire now,
                            # but on a MAIN camera it has always meant "no
                            # override" (routers/cameras.py stores it that way).
                            enabled[name] = (masks, retention, groom or None)
                    if sub_name not in names:
                        disabled.add(sub_name)
                else:
                    disabled.add(slug)
                    disabled.add(sub_recording_name(slug))
            await reconcile(enabled, disabled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("nvr.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.nvr_sync_interval)


# ── Snapshot sampling (Data Collection, migration 045) ────────────────────────
#
# The NVR's /snapshot endpoint decodes from the segment start on purpose: input
# seeking (`-ss` before `-i`) returns silently wrong frames on MPEG-TS, which
# its own comment documents with measurements. So a single snapshot costs
# 0.18 s one second into a segment and 2.58 s fifty-five seconds in, and the
# endpoint now queues behind a semaphore on the NVR side as well.
#
# _TIMEOUT (10 s) is therefore much too short here, and the failure mode is
# nastier than a slow request: fetch_snapshot returns None for "no recording
# covers this instant", so a timeout would be indistinguishable from a genuine
# recording gap. It would land in the dataset's `frames_missing` count — the
# one number an operator reads to judge whether their window had coverage — and
# quietly overstate it. Hence a dedicated budget, and a log line that names a
# timeout as a timeout.
_SNAPSHOT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

# A batch decodes whole segments, so it is slower per REQUEST than /snapshot
# even though it is far faster per FRAME. 240 frames at roughly a second each,
# plus headroom for a box that is also recording.
_BATCH_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


async def fetch_snapshots_batch(slug: str, at_isos: list[str],
                                quality: int = 85) -> dict[str, bytes]:
    """``{iso: jpeg}`` for as many of ``at_isos`` as the NVR could produce.

    ONE DECODE PER SEGMENT instead of one per frame. `/snapshot` re-opens and
    re-decodes a segment from frame zero for every timestamp asked of it, so
    sampling a 60 s segment every 2 s decodes it thirty times; this asks once
    and takes all thirty frames out of that pass. Measured on the GPU
    appliance: 1.12 s/frame against 16.06 s/frame, byte-identical frames.

    MISSING KEYS ARE THE NORMAL ANSWER, exactly as a None is from
    fetch_snapshot: recordings have gaps, and a timestamp the NVR could not
    produce is simply absent here rather than being filled with a neighbouring
    frame. The caller counts absences as gaps.

    Raises nothing. A transport failure returns an empty dict and the caller
    falls back to per-frame fetches, so a batch path that is unavailable costs
    speed and never correctness.
    """
    if not at_isos:
        return {}
    try:
        async with httpx.AsyncClient(timeout=_BATCH_TIMEOUT) as client:
            resp = await client.post(f"{_base()}/snapshot/batch", json={
                "camera": slug, "timestamps": list(at_isos), "quality": quality,
            })
        if resp.status_code != 200:
            log.warning("nvr.snapshot_batch.refused", slug=slug,
                        status=resp.status_code, asked=len(at_isos),
                        detail=resp.text[:200])
            return {}
        out: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    out[member.name] = handle.read()
        return out
    except Exception as exc:  # noqa: BLE001 — speed is optional, the run is not
        log.warning("nvr.snapshot_batch.failed", slug=slug,
                    asked=len(at_isos), error=str(exc))
        return {}


async def fetch_snapshot(slug: str, at_iso: str,
                         quality: int = 85) -> tuple[Optional[bytes], str]:
    """``(jpeg_or_None, reason)`` for slug's recording at ``at_iso``.

    The reason is what lets the sampler tell a recording gap ("gap") apart from
    an appliance that could not deliver ("timeout", "unreachable", "refused",
    "empty"). Returning a bare None for both is how a busy box gets reported to
    an operator as missing footage — see services/dataset_build.collect_frames.

    NONE IS THE NORMAL ANSWER, NOT AN ERROR. Recordings have gaps — a camera
    drop, an NVR restart, retention grooming the front of a window — and the
    sampler counts them rather than aborting. Only a caller that gets None for
    *every* timestamp has a real problem, and that is the sampler's own
    judgement to make (see services/dataset_build.py).

    Raises nothing: transport failures and refusals are logged and reported as
    None, because a dataset run must survive a flaky NVR the same way it
    survives a gap.
    """
    try:
        async with httpx.AsyncClient(timeout=_SNAPSHOT_TIMEOUT) as client:
            resp = await client.get(f"{_base()}/snapshot", params={
                "camera": slug, "timestamp": at_iso, "quality": quality,
            })
    except httpx.TimeoutException as exc:
        # Deliberately distinct from the 404 path below: this is the appliance
        # being too slow, not the footage being absent, and conflating the two
        # is how frames_missing starts lying.
        log.warning("nvr.snapshot.timeout", slug=slug, at=at_iso, error=str(exc))
        return None, "timeout"
    except httpx.HTTPError as exc:
        log.warning("nvr.snapshot.unreachable", slug=slug, at=at_iso, error=str(exc))
        return None, "unreachable"

    if resp.status_code == 200:
        data = resp.content
        if not data:
            log.warning("nvr.snapshot.empty", slug=slug, at=at_iso)
            return None, "empty"
        return data, "ok"

    if resp.status_code == 404:
        # Gap, unknown camera, or outside the recorded range. All three mean
        # "no frame here" to the sampler; the coverage pre-check (camera_range)
        # is what tells an operator which it was, before they start a run.
        log.debug("nvr.snapshot.no_coverage", slug=slug, at=at_iso)
        return None, "gap"

    log.warning("nvr.snapshot.failed", slug=slug, at=at_iso,
                status=resp.status_code, reason=_refusal_reason(resp))
    return None, "refused"


async def camera_range(slug: str) -> Optional[tuple[str, str]]:
    """(earliest, latest) ISO timestamps the NVR holds for slug, or None.

    Backs the Data Collection coverage check, so an operator is warned that a
    window falls outside retention BEFORE starting a run that would spend
    minutes extracting nothing. None means "cannot say" — no recordings, or the
    NVR is unreachable — and the caller must present that as unknown rather
    than as an empty range.
    """
    try:
        resp = await _get_client().get(f"{_base()}/cameras/{slug}/range")
    except httpx.HTTPError as exc:
        log.warning("nvr.range.unreachable", slug=slug, error=str(exc))
        return None

    if resp.status_code != 200:
        log.debug("nvr.range.unavailable", slug=slug, status=resp.status_code)
        return None
    try:
        body = resp.json()
        earliest, latest = body["earliest"], body["latest"]
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("nvr.range.unparseable", slug=slug, error=str(exc))
        return None
    if not earliest or not latest:
        return None
    return str(earliest), str(latest)


# ── Annotations ─────────────────────────────────────────────────────────────
# The NVR owns the pixels; camera-mgmt owns the index. These are the four calls
# that keep the two in step. Every one of them answers None/False rather than
# raising on an unreachable NVR, because the callers must be able to decide
# NOT to write (or NOT to drop) a row — a failure that looks like success here
# is how a ghost row or a false erasure gets created.

async def save_annotation(form: dict[str, str],
                          files: dict[str, Any]) -> Optional[dict]:
    """Forward an operator's annotation to the NVR. Returns its metadata.

    None on any failure. The caller must not write an index row for a file that
    may not exist.
    """
    try:
        resp = await _get_client().post(f"{_base()}/annotations",
                                        data=form, files=files)
    except httpx.RequestError as exc:
        log.warning("nvr.annotation.unreachable", error=str(exc))
        return None
    if resp.status_code >= 400:
        log.warning("nvr.annotation.refused", status=resp.status_code,
                    body=resp.text[:200])
        return None
    return resp.json()


async def delete_annotation(annotation_id: str) -> bool:
    """Delete one annotation's image and sidecar. True if the NVR confirmed.

    The NVR's delete is idempotent, so "already gone" comes back True — which
    is what lets a retried sweep finish instead of stranding the row.
    """
    try:
        resp = await _get_client().delete(
            f"{_base()}/annotations/{annotation_id}")
    except httpx.RequestError as exc:
        log.warning("nvr.annotation.delete_unreachable", error=str(exc))
        return False
    if resp.status_code >= 400:
        log.warning("nvr.annotation.delete_refused", status=resp.status_code)
        return False
    return True


async def list_annotations(camera: Optional[str] = None,
                           since: Optional[float] = None
                           ) -> Optional[list[dict]]:
    """Every sidecar the NVR holds. None = the NVR could not be asked.

    None and [] mean different things to the adoption pass: "could not look"
    must not be mistaken for "nothing to adopt".
    """
    params: dict[str, Any] = {}
    if camera:
        params["camera"] = camera
    if since is not None:
        params["since"] = since
    try:
        resp = await _get_client().get(f"{_base()}/annotations", params=params)
    except httpx.RequestError as exc:
        log.warning("nvr.annotation.list_unreachable", error=str(exc))
        return None
    if resp.status_code >= 400:
        log.warning("nvr.annotation.list_refused", status=resp.status_code)
        return None
    return resp.json().get("annotations", [])


async def fetch_annotation_image(annotation_id: str) -> Optional[bytes]:
    """The stored frame's bytes, or None if the NVR has no such annotation."""
    try:
        resp = await _get_client().get(
            f"{_base()}/annotations/{annotation_id}/image")
    except httpx.RequestError as exc:
        log.warning("nvr.annotation.image_unreachable", error=str(exc))
        return None
    return resp.content if resp.status_code < 400 else None
