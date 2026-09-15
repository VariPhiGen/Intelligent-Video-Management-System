"""analytics_client.py — camera-registry → analytics-service sync.

WHAT ANALYTICS IS. Detection, plates, tracking and the indexing policy, split
out of Smart Search so that service does only what its name says: embed crops
and search the embeddings. Analytics finds the objects; Smart Search records
them. Both are fed by the same frame broker.

Same shape as nvr_client, motion_client, frames_client and smartsearch_sync:
the service's camera set is in-memory only, so a restart loses it and this loop
puts it back. All calls are best-effort — a down analytics service must never
fail a registry operation, and the next pass heals whatever was missed.

TWO CONSUMERS OF ONE DETECTION PASS. Analytics feeds Smart Search AND runs
the CPU Activity Engine — the AI activities configured in AI Config. So a
camera is analysed when EITHER wants it:

  * Smart Search's own rule, unchanged: enabled, opted into indexing, with at
    least one domain selected. Those domains are passed through, because
    analytics applies them — a person-only camera must not pay to crop, track
    and plate-read vehicles the index would drop anyway.
  * or it is enabled and has at least one activity configured. Its stored
    `analytics_config` travels with the registration, and its search domains
    are sent EMPTY when it is not indexed, so running an activity never starts
    indexing a camera the operator opted out of.

THE ACTIVITY CONFIGURATION IS AI CONFIG'S, VERBATIM. There is no second
configuration: analytics runs from the regions and activities this registry
stores. Drift is detected by fingerprint (the same function analytics
computes), and AI Config's save pushes immediately via `push_camera`.

This replaced DeepStream as the activity execution path. The DeepStream
projection still exists (services/deepstream_client.py) and is switched off by
default — see DEEPSTREAM_PROJECTION_ENABLED.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)

_client: Optional[httpx.AsyncClient] = None


def is_configured() -> bool:
    """False when no analytics service is deployed — which is the default.
    Analytics is opted into with --profile analytics, and the compose overlay
    is what sets this URL."""
    return bool(settings.analytics_api_url.strip())


def _base() -> str:
    return settings.analytics_api_url.rstrip("/")


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

def _stream_url(slug: str) -> str:
    # The relay, exactly as every other client here uses it. Analytics reads
    # frames from the broker in normal operation and never opens this URL, but
    # it is what its sampler fallback would use, and it never sees camera
    # credentials either way.
    return f"rtsp://{settings.nvr_record_host}:{settings.rtsp_port}/{slug}"


# ── The activity configuration ────────────────────────────────────────────────

def activity_config(analytics_config: Any) -> dict:
    """The parts of a stored analytics_config the activity engine runs on."""
    cfg = analytics_config if isinstance(analytics_config, Mapping) else {}
    return {"regions": dict(cfg.get("regions") or {}),
            "activities": list(cfg.get("activities") or [])}


def has_activities(analytics_config: Any) -> bool:
    return any(isinstance(a, Mapping) and a.get("type")
               for a in activity_config(analytics_config)["activities"])


def config_fingerprint(analytics_config: Any) -> str:
    """Identical to analytics/activities/engine.py's config_fingerprint.

    Both sides' tests pin the same literal digest, so a change to the canonical
    form on one side alone fails a test rather than re-pushing every camera on
    every reconcile pass.
    """
    canonical = json.dumps(activity_config(analytics_config), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


async def add_camera(slug: str, domains: Optional[list[str]] = None,
                     analytics_config: Optional[dict] = None) -> bool:
    """Start (or re-assert) analysing ``slug``. 409 (already there) is OK.

    `domains` is sent even when empty — that is how a camera analysed only for
    its activities tells analytics to feed Smart Search nothing.
    """
    params: list[tuple[str, str]] = [("rtsp_url", _stream_url(slug)),
                                     ("domains", ",".join(domains or []))]
    body = ({"analytics_config": activity_config(analytics_config)}
            if analytics_config is not None else None)
    try:
        resp = await _get_client().post(f"{_base()}/cameras/{slug}", params=params,
                                        json=body)
        if resp.status_code in (200, 409):
            log.info("analytics.sync.add.ok", slug=slug,
                     existed=resp.status_code == 409)
            return True
        log.warning("analytics.sync.add.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("analytics.sync.add.unreachable", slug=slug, error=str(exc))
    return False


async def remove_camera(slug: str) -> bool:
    """Stop analysing ``slug``. 404 (already gone) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}")
        if resp.status_code in (200, 404):
            log.info("analytics.sync.remove.ok", slug=slug,
                     missing=resp.status_code == 404)
            return True
        log.warning("analytics.sync.remove.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("analytics.sync.remove.unreachable", slug=slug,
                    error=str(exc))
    return False


async def list_cameras() -> Optional[dict[str, tuple[list[str], Optional[str]]]]:
    """Slug → (domains, activity-config fingerprint), or None if unreachable."""
    try:
        resp = await _get_client().get(f"{_base()}/cameras")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        out: dict[str, list[str]] = {}
        for c in body.get("cameras", []):
            slug = c.get("name")
            if slug is None:
                log.warning("analytics.sync.list.unexpected_shape",
                            keys=sorted(c)[:8])
                return None
            acts = c.get("activities") if isinstance(c.get("activities"), dict) else {}
            out[slug] = (list(c.get("domains") or []), acts.get("fingerprint"))
        return out
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("analytics.sync.list.unreachable", error=str(exc))
        return None


# ── Reconciliation ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Desired:
    """What analytics should hold for one camera."""
    domains: list[str]
    analytics_config: dict


def wants(enabled: bool, indexing: bool, domains, analytics_config) -> bool:
    """Does analytics consume this camera at all? Either consumer suffices."""
    if not enabled:
        return False
    return bool(indexing and domains) or has_activities(analytics_config)


def desired_sets(rows) -> tuple[dict[str, Desired], set[str]]:
    """(analyse → Desired, do-not-analyse) for registry rows.

    A pure function on purpose: this is the rule that decides which cameras
    cost a detector pass, and it is the one thing here worth pinning in a test.
    Rows are (slug, enabled, search_indexing, search_domains, analytics_config).
    """
    desired: dict[str, Desired] = {}
    absent: set[str] = set()
    for slug, enabled, indexing, domains, cfg in rows:
        if not wants(enabled, indexing, domains, cfg):
            absent.add(slug)
            continue
        # Smart Search's own rule, unchanged: no domains selected is the same
        # as not indexed. An activities-only camera indexes nothing.
        indexed = bool(indexing and domains)
        desired[slug] = Desired(list(domains) if indexed else [], activity_config(cfg))
    return desired, absent


async def reconcile(desired: dict[str, Desired], absent: set[str]) -> None:
    """Re-assert desired analytics state. Names the registry does not know
    about are left alone."""
    current = await list_cameras()
    if current is None:
        return  # service down; next pass retries

    for slug in sorted(set(desired) - set(current)):
        await add_camera(slug, desired[slug].domains, desired[slug].analytics_config)
    # Present but wrong: a domain or activity change made anywhere other than
    # the save handlers heals here. Re-asserted rather than removed and
    # re-added, because the reconcile loop runs in every uvicorn worker and a
    # remove+add pair from two of them interleaves into add/remove/add.
    for slug in sorted(set(desired) & set(current)):
        want = desired[slug]
        have_domains, have_fp = current[slug]
        want_fp = config_fingerprint(want.analytics_config)
        if sorted(have_domains) != sorted(want.domains) or have_fp != want_fp:
            log.info("analytics.sync.drifted", slug=slug,
                     had_domains=have_domains, want_domains=want.domains,
                     had_config=have_fp, want_config=want_fp)
            await add_camera(slug, want.domains, want.analytics_config)
    for slug in sorted(absent & set(current)):
        await remove_camera(slug)


async def fetch_activity_definitions() -> Optional[list]:
    """The CPU activity registry (GET /activities), or None if unreachable."""
    try:
        resp = await _get_client().get(f"{_base()}/activities")
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("analytics.activities.unreachable", error=str(exc))
        return None
    items = body.get("activities") if isinstance(body, dict) else None
    return items if isinstance(items, list) else None


async def sync_activity_catalog() -> Optional[dict]:
    """Copy the registry into the Activity Type catalog. None if unreachable."""
    from . import activity_catalog
    from ..db import AsyncSessionLocal

    raw = await fetch_activity_definitions()
    if raw is None:
        return None
    definitions = activity_catalog.parse_definitions(raw)
    async with AsyncSessionLocal() as db:
        return await activity_catalog.sync_catalog(db, definitions)


async def push_camera(slug: str, enabled: bool, search_indexing: bool,
                      search_domains, analytics_config) -> None:
    """Apply one camera's state to analytics now. Best-effort, never raises.

    Called when AI Config saves, so an activity change is live while the
    operator is still on the page instead of at the next reconcile pass.
    """
    if not (settings.analytics_sync_enabled and is_configured()):
        return
    desired, _ = desired_sets([(slug, enabled, search_indexing,
                                search_domains, analytics_config)])
    if slug in desired:
        await add_camera(slug, desired[slug].domains, desired[slug].analytics_config)
    else:
        await remove_camera(slug)


async def reconcile_loop() -> None:
    """Background task: periodic registry→analytics reconciliation."""
    # Local import to avoid a circular import at module load time.
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage

    log.info("analytics.sync.loop_started", interval=settings.nvr_sync_interval,
             analytics=settings.analytics_api_url)
    while True:
        try:
            # The registry first: AI Config offers what it lists.
            await sync_activity_catalog()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed sync retries next pass
            log.warning("analytics.activities.sync_error", error=str(exc))
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            Camera.slug, Camera.enabled,
                            Camera.search_indexing, Camera.search_domains,
                            Camera.analytics_config,
                        ).where(Camera.stage == CameraStage.REGISTERED.value)
                    )
                ).all()
            desired, absent = desired_sets(rows)
            await reconcile(desired, absent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("analytics.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.nvr_sync_interval)
