"""search_scope.py — which index hits this VMS is allowed to return.

The CLIP index is shared with the analytics appliance and is not scoped to this
VMS. It holds crops from cameras configured by hand on the appliance (a label
like ``cmajet``, or a bare sensor UUID), rows scanned out of object storage with
no camera identity at all, and crops from cameras since removed from the
registry. None of those can be opened in Playback, because this VMS has no
footage for a camera it does not record.

The scoping therefore lives here rather than in the browser. A hit is returned
only if it traces all the way to retained footage:

  1. it resolves to a camera in this registry,
  2. the recorder knows that camera,
  3. it carries a timestamp,
  4. that timestamp is inside the retained window, and
  5. a segment actually covers it.

Rules 1-4 are arithmetic over data already in hand. Rule 5 costs one recorder
call per distinct camera on the page, not one per hit.

Dropped hits are counted by reason and reported alongside the results, so a
short page is explainable rather than mysterious.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx
import structlog

from ..config import settings
from ..models import Camera

log = structlog.get_logger(__name__)

# Recorder segment length. The retained window is padded by one chunk at the
# front (grooming trims there, and `earliest` moves while a page is open).
CHUNK_SECONDS = 120

# How close to the recorder's `latest` a hit is trusted without consulting gaps.
# The pipeline indexes a crop within seconds; the recorder only publishes a
# segment once it closes, and indexes it later still — so the newest footage
# legitimately reads as "not covered yet". Inside this margin the window rule
# stands, rather than hiding live hits that become playable a minute later.
FRESH_MARGIN_SECONDS = 3 * CHUNK_SECONDS

DROP_REASONS = ("unmapped", "not_recorded", "no_timestamp", "outside_retention", "in_gap")


@dataclass
class Scoped:
    """One hit that survived, with the registry camera it belongs to."""

    hit: dict
    camera: Camera
    when_ms: int


@dataclass
class ScopeResult:
    kept: list[Scoped] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def _norm(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def build_index(cameras: Iterable[Camera]) -> dict[str, Camera]:
    """Every identifier a hit might carry → the camera it means.

    A VMS-provisioned camera is published to the pipeline under its slug
    (``_analytics_contract`` emits ``sensor_id: camera.slug``), but an
    appliance-authored config can use the camera UUID, the registry's own
    external ``sensor_id``, or the display name. All four resolve.
    """
    index: dict[str, Camera] = {}
    for cam in cameras:
        for key in (cam.slug, str(cam.id) if cam.id else None,
                    str(cam.sensor_id) if cam.sensor_id else None, cam.name):
            k = _norm(key)
            # First writer wins, so a display-name collision can never steal a
            # slug that already resolves.
            if k and k not in index:
                index[k] = cam
    return index


def resolve(index: dict[str, Camera], *candidates: Any) -> Optional[Camera]:
    for candidate in candidates:
        key = _norm(candidate)
        if key and key in index:
            return index[key]
    return None


def _to_ms(value: Any) -> Optional[int]:
    """Recorder timestamps arrive as epoch seconds or ISO-8601; hits carry ISO
    strings (people) or epoch milliseconds (vehicles)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Epoch seconds vs milliseconds: anything below this is not a plausible
        # millisecond timestamp for footage (it would be 1970).
        return int(value * 1000) if value < 1e11 else int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def retained_window(row: dict, now_ms: int) -> Optional[tuple[int, int]]:
    """The recorder's retained span for a camera, in epoch ms.

    The upper bound is *now* for a camera that is still recording: the index
    runs ahead of the segment indexer, so the freshest — and most useful — hits
    would otherwise fall outside a window that is merely stale.
    """
    earliest = _to_ms(row.get("earliest"))
    latest = _to_ms(row.get("latest"))
    if earliest is None or latest is None:
        return None
    upper = max(latest, now_ms) if row.get("recording") else latest
    return earliest - CHUNK_SECONDS * 1000, upper


async def recorder_cameras() -> Optional[dict[str, dict]]:
    """The recorder's inventory, keyed by camera slug.

    ``None`` means the recorder could not be reached — the caller degrades to
    the registry check rather than emptying the page on a transient failure.
    """
    url = f"{settings.nvr_api_url.rstrip('/')}/cameras"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("search.recorder_inventory_failed", error=str(exc))
        return None
    return {c["name"]: c for c in (body.get("cameras") or []) if c.get("name")}


async def _covered_spans(slug: str, from_ms: int, to_ms: int) -> Optional[list[tuple[int, int]]]:
    """Segment-backed spans for a camera over a window, or ``None`` if the
    recorder could not answer (in which case the hit keeps its coarse verdict)."""
    url = f"{settings.nvr_api_url.rstrip('/')}/coverage"
    params = {
        "camera": slug,
        "from": datetime.fromtimestamp(from_ms / 1000, timezone.utc).isoformat(),
        "to": datetime.fromtimestamp(to_ms / 1000, timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            cov = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("search.coverage_failed", camera=slug, error=str(exc))
        return None

    earliest, latest = _to_ms(cov.get("earliest")), _to_ms(cov.get("latest"))
    if earliest is None or latest is None:
        return []
    start, end = max(from_ms, earliest), min(to_ms, latest)
    if end <= start:
        return []

    spans: list[tuple[int, int]] = [(start, end)]
    for gap in cov.get("gaps") or []:
        gs, ge = _to_ms(gap.get("start")), _to_ms(gap.get("end"))
        if gs is None or ge is None:
            continue
        nxt: list[tuple[int, int]] = []
        for a, b in spans:
            if ge <= a or gs >= b:
                nxt.append((a, b))
                continue
            if gs > a:
                nxt.append((a, gs))
            if ge < b:
                nxt.append((ge, b))
        spans = nxt
    return spans


async def scope(
    hits: Iterable[dict],
    *,
    candidates_of,
    timestamp_of,
    cameras: list[Camera],
    check_coverage: bool = True,
) -> ScopeResult:
    """Apply the five rules to a page of hits.

    ``candidates_of`` and ``timestamp_of`` adapt the two hit shapes (people
    carry a sensor id plus a camera name; vehicles carry only ``camera_id``).
    """
    result = ScopeResult()
    index = build_index(cameras)
    recorder = await recorder_cameras()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for hit in hits:
        camera = resolve(index, *candidates_of(hit))
        if camera is None:
            result.drop("unmapped")
            continue
        when_ms = _to_ms(timestamp_of(hit))
        if when_ms is None:
            result.drop("no_timestamp")
            continue
        if recorder is not None:
            row = recorder.get(camera.slug)
            if row is None:
                result.drop("not_recorded")
                continue
            window = retained_window(row, now_ms)
            if window is None:
                result.drop("not_recorded")
                continue
            if not window[0] <= when_ms <= window[1]:
                result.drop("outside_retention")
                continue
        result.kept.append(Scoped(hit=hit, camera=camera, when_ms=when_ms))

    if not check_coverage or recorder is None or not result.kept:
        return result

    # Rule 5, batched: one coverage call per distinct camera, over the span its
    # own hits cover. Hits too fresh for the segment indexer skip the check.
    # Positions into `result.kept`, so removal needs no object identity.
    bycam: dict[str, list[int]] = {}
    for pos, item in enumerate(result.kept):
        row = recorder.get(item.camera.slug) or {}
        latest = _to_ms(row.get("latest"))
        if latest is not None and item.when_ms >= latest - FRESH_MARGIN_SECONDS * 1000:
            continue
        bycam.setdefault(item.camera.slug, []).append(pos)
    if not bycam:
        return result

    async def _gaps_for(slug: str, positions: list[int]) -> set[int]:
        times = [result.kept[p].when_ms for p in positions]
        spans = await _covered_spans(
            slug, min(times) - CHUNK_SECONDS * 1000, max(times) + CHUNK_SECONDS * 1000
        )
        if spans is None:  # recorder could not answer — keep the coarse verdict
            return set()
        return {
            p for p in positions
            if not any(a <= result.kept[p].when_ms <= b for a, b in spans)
        }

    in_gap: set[int] = set()
    for found in await asyncio.gather(*(_gaps_for(s, p) for s, p in bycam.items())):
        in_gap |= found

    if in_gap:
        result.dropped["in_gap"] = result.dropped.get("in_gap", 0) + len(in_gap)
        result.kept = [i for p, i in enumerate(result.kept) if p not in in_gap]
    return result
