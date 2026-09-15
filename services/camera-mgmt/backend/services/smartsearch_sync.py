"""smartsearch_sync.py — camera-registry → Smart Search index service sync.

NOT to be confused with ``smartsearch_client.py``, which QUERIES an index. This
module tells an index which cameras to build itself from. One is the read side,
one is the write side, and they can point at different deployments: a VMS may
query a remote index it does not feed.

Mirrors ``motion_client``: the index service holds its camera set in memory, so
a restart there loses everything and the reconcile loop below re-asserts it
within one interval.

Cameras are indexed FROM THE RELAY (rtsp://<host>:<rtsp_port>/<slug>) — the same
single-pull rule as recording and motion, so indexing adds zero load on the
physical camera and the service never sees camera credentials.

All calls are best-effort: a down index service must never fail a registry
operation.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx
import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)
_client: Optional[httpx.AsyncClient] = None


def _base() -> str:
    return settings.smartsearch_index_url.rstrip("/")


def is_configured() -> bool:
    """An empty URL is a supported deployment — no index is being fed from here
    — not a misconfiguration. Same rule as smartsearch_client."""
    return bool(settings.smartsearch_index_url.strip())


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


def _stream_url(slug: str) -> str:
    return f"rtsp://{settings.nvr_record_host}:{settings.rtsp_port}/{slug}"


def effective_retention_days(retention_days: Optional[int]) -> int:
    """The camera's recording retention as a CONCRETE number of days.

    Resolved here rather than pushed as None-means-default, because "default"
    means two different numbers on either side of this call: the NVR falls back
    to ``nvr_default_retention_days`` and the index to its own
    ``SEARCH_RETENTION_DAYS``, and nothing keeps those two in step. An appliance
    dropped to a two-day recording policy would keep thirty days of crops cut
    from that footage — the index outliving the recording it describes, which is
    the exact failure this sync exists to prevent. Sending the resolved figure
    makes the index follow the footage by construction.
    """
    return retention_days or settings.nvr_default_retention_days


async def add_camera(slug: str, domains: Optional[list[str]] = None,
                     retention_days: Optional[int] = None) -> bool:
    """Start indexing ``slug`` from its relay URL. 409 (already there) is OK."""
    params: list[tuple[str, str]] = [("rtsp_url", _stream_url(slug))]
    for d in domains or []:
        params.append(("domains", d))
    params.append(("retention_days", str(effective_retention_days(retention_days))))
    try:
        resp = await _get_client().post(f"{_base()}/cameras/{slug}", params=params)
        if resp.status_code in (200, 409):
            log.info("smartsearch.sync.add.ok", slug=slug, existed=resp.status_code == 409)
            return True
        log.warning("smartsearch.sync.add.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("smartsearch.sync.add.unreachable", slug=slug, error=str(exc))
    return False


async def remove_camera(slug: str) -> bool:
    """Stop indexing ``slug``. 404 (already gone) is OK."""
    try:
        resp = await _get_client().delete(f"{_base()}/cameras/{slug}")
        if resp.status_code in (200, 404):
            log.info("smartsearch.sync.remove.ok", slug=slug,
                     missing=resp.status_code == 404)
            return True
        log.warning("smartsearch.sync.remove.failed", slug=slug,
                    status=resp.status_code, detail=resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("smartsearch.sync.remove.unreachable", slug=slug, error=str(exc))
    return False


async def restart_camera(slug: str, domains: Optional[list[str]] = None,
                         retention_days: Optional[int] = None) -> bool:
    """Re-assert a camera's desired state.

    No longer a remove+add: ``add_camera`` is an upsert upstream, so this is one
    idempotent instruction. The pair was a race with four reconcile loops
    running, and it also dropped the capture thread on a mere domain change.
    """
    return await add_camera(slug, domains, retention_days)


async def list_cameras() -> Optional[dict[str, tuple[list[str], Optional[int]]]]:
    """Slug → (indexed domains, retention days), or None if unreachable.

    Returns the STATE, not just the names: reconcile has to be able to notice a
    camera that is present but indexing the wrong things, or holding them for
    the wrong length of time. Comparing name sets alone made both kinds of
    change heal only via the immediate push from the update handler, which is
    precisely the path reconcile exists to back up.
    """
    try:
        resp = await _get_client().get(f"{_base()}/cameras")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return {c["name"]: (list(c.get("domains") or []), c.get("retention_days"))
                for c in body.get("cameras", [])}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("smartsearch.sync.list.unreachable", error=str(exc))
        return None


async def reconcile(desired: dict[str, tuple[list[str], Optional[int]]],
                    absent: set[str]) -> None:
    """Re-assert desired index state. Names the registry does not know about are
    left alone — another VMS may be feeding the same index."""
    current = await list_cameras()
    if current is None:
        return  # service down; next pass retries

    for slug in sorted(set(desired) - set(current)):
        await add_camera(slug, desired[slug][0], desired[slug][1])
    # Present but wrong: a domain change made anywhere other than the update
    # handler — or one whose immediate push failed — heals here.
    #
    # RE-ASSERTED, not removed and re-added. The reconcile loop runs in every
    # uvicorn worker (four of them), so several loops act on the same drift at
    # once; a remove+add pair from two of them interleaves into add/remove/add
    # and leaves the camera registered but unsampled. add_camera is an upsert
    # upstream, so repeating it concurrently is a non-event.
    for slug in sorted(set(desired) & set(current)):
        had_domains, had_retention = current[slug]
        want_domains, want_retention = desired[slug]
        want_retention = effective_retention_days(want_retention)
        # Retention drift is checked alongside domain drift because it has the
        # same failure shape and a worse consequence: a push that failed leaves
        # the index holding crops past the deadline the camera advertises, and
        # nothing else in the system would ever notice. `None` on the index side
        # means it is running on its own default rather than on this camera's
        # policy — drift, not a match, however the numbers happen to line up.
        if (sorted(had_domains) != sorted(want_domains)
                or had_retention != want_retention):
            log.info("smartsearch.sync.state_drifted", slug=slug,
                     had_domains=had_domains, want_domains=want_domains,
                     had_retention=had_retention, want_retention=want_retention)
            await add_camera(slug, want_domains, want_retention)
    for slug in sorted(absent & set(current)):
        await remove_camera(slug)


async def reconcile_loop() -> None:
    """Background task: periodic registry→index reconciliation.

    Desired set is every ENABLED, REGISTERED camera that has not opted OUT
    (migration 030, default true). An opt-OUT rather than an opt-in, because
    search covering only some cameras is worse than search covering all of them:
    a nil result reads as "this person was never recorded" rather than "this
    camera was never indexed". The SPA carries `search_indexing` on every camera
    so it can tell those two apart wherever results are shown.
    """
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage

    log.info("smartsearch.sync.loop_started", interval=settings.nvr_sync_interval,
             index=_base())
    while True:
        try:
            async with AsyncSessionLocal() as db:
                rows = (
                    await db.execute(
                        select(
                            Camera.slug, Camera.enabled, Camera.search_indexing,
                            Camera.search_domains, Camera.retention_days,
                        ).where(Camera.stage == CameraStage.REGISTERED.value)
                    )
                ).all()
            # No domains selected is the same as not indexed: a camera that
            # contributes nothing should not sit in the index looking active.
            desired = {slug: (list(dom or []), ret)
                       for slug, en, idx, dom, ret in rows if en and idx and dom}
            absent = {slug for slug, en, idx, dom, ret in rows
                      if not (en and idx and dom)}
            await reconcile(desired, absent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the loop must survive anything
            log.warning("smartsearch.sync.loop_error", error=str(exc))
        await asyncio.sleep(settings.nvr_sync_interval)
