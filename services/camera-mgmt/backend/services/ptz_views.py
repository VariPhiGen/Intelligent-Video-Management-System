"""ptz_views.py — per-preset AI: each preset of a PTZ camera is a fixed-camera view.

A preset has its own zones, tripwires and rules (ptz_presets.analytics_config, the same shape as a fixed camera's
cameras.analytics_config). Once ANY preset of a camera has a rule, that camera's AI FOLLOWS ITS PRESETS:

    confirmed at preset P (services/ptz_track's view: the picture checked)  → P's zones + rules
    moving / between presets / at a preset without rules / not confirmed  → nothing (no detection at all —
    / at a preset whose view changed and waits for a check (phase 3)        the engine skips a camera with no work)

The camera-wide config is then not run (it still exists, and runs again if every preset's rules are removed).

HOW THE ENGINE IS TOLD. Exactly as for a fixed camera: the camera's config is handed to the CPU analytics service
(analytics_client.add_camera). On every view change the effective config is pushed (`on_view_change`, from the
tracker's leader loop), and the reconcile loop in every worker computes the same effective config from the same
Valkey view, so it re-asserts rather than fights it. The camera stays registered with an empty config between
presets (`keep`), so arriving at the next stop is a config swap, not a re-attach. DeepStream is untouched for now:
it keeps receiving the camera-wide config.

WHICH PRESET AN EVENT BELONGS TO. Events arrive a moment after they happen, possibly after the camera moved on, so
each view change is also kept in a short Valkey list (`view_log`), and an event is matched to the view in force at
its started_at — its zone named from THAT preset's regions ("Gate · Zone 1").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Optional

import structlog

log = structlog.get_logger(__name__)

EMPTY = {"regions": {}, "activities": []}
VIEW_LOG_LEN = 20


def paused(review: Any) -> bool:
    from .ptz_view_changes import paused as _paused
    return _paused(review)


def has_rules(cfg: Any) -> bool:
    from .analytics_client import has_activities
    return has_activities(cfg)


def follows_presets(presets: Iterable[Mapping]) -> bool:
    """Does this camera's AI follow its presets? (any preset with a rule)"""
    return any(has_rules(p.get("analytics_config")) for p in presets)


def find_preset(presets: Iterable[Mapping], view: Optional[Mapping]) -> Optional[Mapping]:
    if not view or view.get("state") != "preset":
        return None
    pid, token = view.get("preset_id"), view.get("preset_token")
    for p in presets:
        if pid and str(p.get("id")) == str(pid):
            return p
    for p in presets:
        if not pid and token and p.get("token") == token:
            return p
    return None


def effective(camera_cfg: Any, presets: list[Mapping], view: Optional[Mapping]) -> tuple[dict, Optional[Mapping]]:
    """(the config the engine should run now, the preset it is) — pure. Not following presets: the camera's own."""
    if not follows_presets(presets):
        return (camera_cfg if isinstance(camera_cfg, Mapping) else {}), None
    hit = find_preset(presets, view)
    # A preset whose view changed under its zones and waits for a check (services/ptz_view_changes) runs nothing:
    # zones in the wrong place give false alarms or miss real ones.
    if hit is not None and has_rules(hit.get("analytics_config")) and not paused(hit.get("analytics_review")):
        return dict(hit["analytics_config"]), hit
    return dict(EMPTY), None


async def presets_ai(db, camera_ids: Optional[list] = None) -> dict[str, list[dict]]:
    """{camera_id: [{id, token, name, analytics_config, analytics_review}]} for presets that have a config (any camera,
    or these)."""
    from sqlalchemy import select

    from ..models import PtzPreset
    q = select(PtzPreset.id, PtzPreset.camera_id, PtzPreset.token, PtzPreset.name, PtzPreset.analytics_config,
               PtzPreset.analytics_review) \
        .where(PtzPreset.analytics_config.is_not(None), PtzPreset.missing_on_camera.is_(False))
    if camera_ids:
        q = q.where(PtzPreset.camera_id.in_(camera_ids))
    out: dict[str, list[dict]] = {}
    for pid, cid, token, name, cfg, review in (await db.execute(q)).all():
        out.setdefault(str(cid), []).append({"id": str(pid), "token": token, "name": name, "analytics_config": cfg,
                                             "analytics_review": review})
    return out


# ── the view log: which preset was in force when ─────────────────────────────

def view_log_key(camera_id: Any) -> str:
    return f"ptz:viewlog:{camera_id}"


async def note_view(r, camera_id: Any, view: Mapping, at: Optional[datetime] = None) -> None:
    at = at or datetime.now(timezone.utc)
    entry = {"state": view.get("state"), "preset_id": view.get("preset_id"), "preset_token": view.get("preset_token"),
             "preset_name": view.get("preset_name"), "from": at.isoformat()}
    await r.lpush(view_log_key(camera_id), json.dumps(entry))
    await r.ltrim(view_log_key(camera_id), 0, VIEW_LOG_LEN - 1)


def view_at(entries: list[Mapping], at: datetime) -> Optional[Mapping]:
    """The view in force at `at` — pure. `entries` newest first, as kept. None = older than the log."""
    for e in entries:
        try:
            start = datetime.fromisoformat(str(e.get("from")))
        except ValueError:
            continue
        if start <= at:
            return e
    return None


async def view_log(r, camera_id: Any) -> list[dict]:
    out = []
    for raw in await r.lrange(view_log_key(camera_id), 0, VIEW_LOG_LEN - 1):
        try:
            out.append(json.loads(raw))
        except (TypeError, ValueError):
            pass
    return out


# ── telling the engine ───────────────────────────────────────────────────────

async def push_now(camera_id: Any, view: Optional[Mapping] = None) -> None:
    """Hand the camera's effective config to CPU analytics now (a view change, a preset's rules saved)."""
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera
    from . import analytics_client, ptz_track
    async with AsyncSessionLocal() as db:
        cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
        if cam is None:
            return
        presets = (await presets_ai(db, [cam.id])).get(str(cam.id), [])
    if view is None:
        view = await ptz_track.current_view(cam.id)
    cfg, _ = effective(cam.analytics_config, presets, view)
    await analytics_client.push_camera(cam.slug, cam.enabled, cam.search_indexing, cam.search_domains, cfg,
                                       keep=follows_presets(presets))


async def record_view(camera_id: Any, view: Mapping, at: Optional[datetime] = None) -> None:
    """Keep the change in ptz_view_history (migration 068): a dataset sampling only at presets reads its window's
    confirmed views from there — the Valkey log holds only the last 20 changes."""
    import uuid as _uuid

    from ..db import AsyncSessionLocal
    from ..models import PtzViewHistory
    pid = view.get("preset_id")
    try:
        pid = _uuid.UUID(str(pid)) if pid else None
    except ValueError:
        pid = None
    async with AsyncSessionLocal() as db:
        db.add(PtzViewHistory(camera_id=_uuid.UUID(str(camera_id)), at=at or datetime.now(timezone.utc),
                              state=str(view.get("state") or "none")[:16], preset_id=pid,
                              preset_token=(str(view.get("preset_token"))[:128] if view.get("preset_token") else None),
                              preset_name=(str(view.get("preset_name"))[:64] if view.get("preset_name") else None),
                              verified_by=(str(view.get("verified_by"))[:16] if view.get("verified_by") else None)))
        await db.commit()


async def on_view_change(r, camera_id: Any, slug: str, view: Mapping) -> None:
    """The tracker confirmed a new view (or lost one): log it, and switch the engine's config if the camera's AI
    follows its presets. Best-effort — the reconcile loop heals a missed push within one interval."""
    now = datetime.now(timezone.utc)
    try:
        await note_view(r, camera_id, view, now)
    except Exception as exc:                            # noqa: BLE001
        log.debug("ptz_views.log_failed", slug=slug, error=str(exc)[:200])
    try:
        await record_view(camera_id, view, now)
    except Exception as exc:                            # noqa: BLE001 — a missing table (pre-068) or a DB blip
        log.debug("ptz_views.history_failed", slug=slug, error=str(exc)[:200])
    try:
        await push_now(camera_id, view)
    except Exception as exc:                            # noqa: BLE001
        log.warning("ptz_views.push_failed", slug=slug, error=str(exc)[:200])


async def effective_rows(db, rows: list) -> tuple[list, set[str]]:
    """The reconcile loop's registry rows (slug, enabled, indexing, domains, cfg, camera_id) with each
    preset-following camera's cfg replaced by its effective one; plus the slugs to keep registered regardless."""
    from . import ptz_track
    ai = await presets_ai(db)
    out, keep = [], set()
    for slug, enabled, indexing, domains, cfg, cid in rows:
        presets = ai.get(str(cid), [])
        if presets and follows_presets(presets):
            cfg, _ = effective(cfg, presets, await ptz_track.current_view(cid))
            keep.add(slug)
        out.append((slug, enabled, indexing, domains, cfg))
    return out, keep


# ── events: which preset, and its zones ──────────────────────────────────────

async def event_resolver(db, cameras: list) -> Optional[Any]:
    """For ai_events.prepare: a function (event, camera) → (camera as of the event's view, preset or None), for the
    cameras that follow presets; None when none of them do."""
    from .. import redis_client
    ai = await presets_ai(db, [c.id for c in cameras])
    following = {str(c.id): ai[str(c.id)] for c in cameras if follows_presets(ai.get(str(c.id), []))}
    if not following:
        return None
    r = await redis_client.get_redis()
    logs = {cid: await view_log(r, cid) for cid in following}

    def resolve(ev, camera):
        presets = following.get(str(camera.id))
        if presets is None:
            return camera, None
        view = view_at(logs.get(str(camera.id)) or [], ev.started_at)
        cfg, preset = effective(camera.analytics_config, presets, view)
        proxy = SimpleNamespace(id=camera.id, slug=getattr(camera, "slug", None), analytics_config=cfg,
                                retention_minutes=getattr(camera, "retention_minutes", None))
        return proxy, preset
    return resolve
