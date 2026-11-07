"""ptz_view_changes.py — PTZ phase 3: a preset's zones follow its view when the preset changes, and outlive a delete.

A preset's zones (services/ptz_views) are drawn on the picture of WHERE THE PRESET POINTS. Three things change that:

  RE-SAVED IN VMS ("save current view" over an existing preset). The new picture is lined up against the old one
  (ptz_track.scene_offset — many tiles agreeing on one offset):
      the same view (moved < 1 % of the frame)            → nothing to do
      slid by up to AUTO_SHIFT_MAX, zones whole in frame  → zones moved by the same amount, rules KEEP RUNNING
                                                            ("shifted": shown, can be accepted or undone)
      slid further / a zone cut by the picture's edge     → zones moved as a starting point, rules PAUSED ("review")
      zoom changed / can't be lined up (another place,    → zones left as they were, rules PAUSED ("review")
      night against a day picture, a blank scene)
  RE-STORED IN THE CAMERA'S OWN WEB PAGE (seen by ptz_presets.sync as a stored position that moved). No picture of
  the new view yet: rules PAUSED ("awaiting_picture") and the old pictures put aside; the first confirmed arrival
  takes a new picture (ptz_track._keep_reference) and the same lining-up runs then.
  DELETED. The zones, rules and picture go to ptz_view_archive instead of with the row, and can be copied onto another
  preset (always "review": another preset is another view, even when the offset is measured).

Paused, not guessed: zones in the wrong place give false alarms or miss real ones, so a preset whose view moved more
than a little runs nothing until someone has looked. Saving the zones (the editor shows them on the NEW picture) or
"Zones are right" ends the review.
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

SAME_VIEW = 0.01          # a re-save that moved the view less than this (fraction of the frame) changes nothing
AUTO_SHIFT_MAX = 0.08     # zones follow a slide up to this far and keep running; further → paused for review
MIN_INSIDE = 0.6          # a zone keeping less of its area than this inside the new picture → review
MOVED_OUTSIDE = 0.04      # ptz_presets.sync: a stored position this far off = re-stored outside VMS (2 × the
                          # "at preset" tolerance: a VMS save records GetStatus, the list reports the preset — a hair apart)
ZOOM_CHANGED = 0.02

PAUSED = ("review", "awaiting_picture")


def paused(review: Any) -> bool:
    """Do this preset's rules wait for someone to check its zones?"""
    return isinstance(review, dict) and review.get("state") in PAUSED


# ── geometry (pure) ──────────────────────────────────────────────────────────

def _area(pts: list) -> float:
    return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                   for i in range(len(pts)))) / 2 if len(pts) >= 3 else 0.0


def clip_to_frame(pts: list) -> list:
    """A polygon cut to the picture (0..1 on both axes) — Sutherland–Hodgman against the four edges."""
    def cut(poly, inside, cross):
        out = []
        for i, cur in enumerate(poly):
            prev = poly[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def at_x(x):
        return lambda a, b: [x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])]

    def at_y(y):
        return lambda a, b: [a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y]
    poly = [list(p) for p in pts]
    for inside, cross in ((lambda p: p[0] >= 0, at_x(0.0)), (lambda p: p[0] <= 1, at_x(1.0)),
                          (lambda p: p[1] >= 0, at_y(0.0)), (lambda p: p[1] <= 1, at_y(1.0))):
        if not poly:
            break
        poly = cut(poly, inside, cross)
    return [[round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)] for x, y in poly]


def shift_config(cfg: dict, dx: float, dy: float) -> tuple[dict, list[str], list[str]]:
    """(the config with every zone and tripwire moved by (dx, dy), regions that no longer fit, zones cut short) — pure.
    A zone is cut to the picture; one that keeps less than MIN_INSIDE of its area, or a tripwire with an end off the
    picture, "no longer fits" (kept, clamped, so the editor still shows it)."""
    out = copy.deepcopy(cfg or {})
    lost, trimmed = [], []
    for rid, region in (out.get("regions") or {}).items():
        pts = [[p[0] + dx, p[1] + dy] for p in region.get("points") or []]
        if region.get("kind") == "tripwire":
            if any(not (0 <= x <= 1 and 0 <= y <= 1) for x, y in pts):
                lost.append(region.get("name") or rid)
            region["points"] = [[round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)] for x, y in pts]
            continue
        before = _area(pts)
        cut = clip_to_frame(pts)
        if len(cut) < 3 or before <= 0 or _area(cut) < MIN_INSIDE * before or len(cut) > 64:
            lost.append(region.get("name") or rid)
            cut = [[round(min(1.0, max(0.0, x)), 4), round(min(1.0, max(0.0, y)), 4)] for x, y in pts]
        elif _area(cut) < 0.999 * before:
            trimmed.append(region.get("name") or rid)
        region["points"] = cut
    return out, lost, trimmed


def plan(cfg: Optional[dict], offset: Optional[tuple[float, float]], zoom_changed: bool) -> tuple[Optional[dict], Optional[str], str]:
    """(new config, review state or None, what happened) for a view that changed by `offset` — pure."""
    if not cfg:
        return cfg, None, ""
    if zoom_changed:
        return cfg, "review", "the zoom changed — zones can't be moved to match; check them on the new picture"
    if offset is None:
        return cfg, "review", "the new picture couldn't be lined up with the old one; check the zones on it"
    dx, dy = offset
    moved = max(abs(dx), abs(dy))
    if moved < SAME_VIEW:
        return cfg, None, ""
    new, lost, trimmed = shift_config(cfg, dx, dy)
    how = "zones moved " + ", ".join(p for p in (_dir(dx, "right", "left"), _dir(dy, "down", "up")) if p) + \
        " with the view"
    if lost:
        return new, "review", f"{how}, but {', '.join(lost[:4])} no longer fit{'s' if len(lost) == 1 else ''} the picture"
    if trimmed:
        # Part of the area is now outside the picture — nobody can watch it from this preset; someone should know.
        return new, "review", f"{how}; part of {', '.join(trimmed[:4])} is now outside the picture"
    if moved > AUTO_SHIFT_MAX:
        return new, "review", f"{how} — a big move, so check them before they run"
    return new, "shifted", how


def _dir(v: float, pos: str, neg: str) -> str:
    return f"{round(abs(v) * 100)}% {pos if v > 0 else neg}" if abs(v) >= 0.005 else ""


def record(state: str, note: str, reason: str, by: Optional[str], offset, zoom_changed: bool,
           previous_config: Optional[dict]) -> dict:
    return {"state": state, "note": note, "reason": reason, "by": by,
            "at": datetime.now(timezone.utc).isoformat(),
            "offset": [round(offset[0], 4), round(offset[1], 4)] if offset else None,
            "zoom_changed": bool(zoom_changed), "previous_config": previous_config}


def review_out(review: Any) -> Optional[dict]:
    """What the UI gets (not the previous config itself — only whether there is one to go back to)."""
    if not isinstance(review, dict):
        return None
    return {k: review.get(k) for k in ("state", "note", "reason", "by", "at", "offset", "zoom_changed")} | \
        {"can_undo": review.get("previous_config") is not None and review.get("state") == "shifted",
         "paused": paused(review)}


def zoom_moved(old: Optional[float], new: Optional[float]) -> bool:
    return old is not None and new is not None and abs(float(old) - float(new)) > ZOOM_CHANGED


def position_moved(row: Any, pan: Any, tilt: Any, zoom: Any) -> bool:
    """Did the camera's stored position for this preset change (re-stored outside VMS)? Only axes both sides know."""
    from .ptz_track import _delta
    for axis, new, wraps in (("pan", pan, True), ("tilt", tilt, False), ("zoom", zoom, False)):
        d = _delta(getattr(row, axis, None), new, wraps)
        if d is not None and d > MOVED_OUTSIDE:
            return True
    return False


# ── applying it to a preset row ──────────────────────────────────────────────

def old_picture(row: Any, night: bool) -> Optional[bytes]:
    """The old view's picture of the same light as the new one (day against day), else whatever there is."""
    same, other = (row.reference_night, row.reference) if night else (row.reference, row.reference_night)
    return same or other or row.thumbnail


async def apply_new_view(row: Any, new_jpeg: bytes, *, old: Optional[bytes], zoom_changed: bool, reason: str,
                         by: Optional[str]) -> Optional[dict]:
    """The preset now shows `new_jpeg` (full frame); `old` = the picture its zones were drawn on. Move/pause the zones
    (plan), store the new picture as the reference of its light and drop the other light's (it shows the old view).
    Mutates `row`; the caller commits. Returns the review record (None = nothing changed for the zones)."""
    from . import ptz_track
    small = await asyncio.to_thread(ptz_track.shrink, new_jpeg)
    night = ptz_track.is_night(small)
    offset = None
    if old is not None and not zoom_changed:
        got = await asyncio.to_thread(ptz_track.scene_offset, old, small)
        offset = (got[0], got[1]) if got else None
    cfg = row.analytics_config
    new_cfg, state, note = plan(cfg, offset, zoom_changed)
    moved = zoom_changed or offset is None or max(abs(offset[0]), abs(offset[1])) >= SAME_VIEW
    now = datetime.now(timezone.utc)
    if moved:
        row.previous_reference = old if cfg else None   # the "before" of a review; nothing to review without zones
        row.reference = row.reference_night = row.reference_at = row.reference_night_at = None
    if night:
        row.reference_night, row.reference_night_at = small, now
    else:
        row.reference, row.reference_at = small, now
    if state is None:
        if isinstance(row.analytics_review, dict) and row.analytics_review.get("state") == "awaiting_picture":
            row.analytics_review = None                 # re-stored outside VMS, but to the same view after all
        return None
    row.analytics_config = new_cfg
    row.analytics_review = record(state, note, reason, by, offset, zoom_changed, cfg)
    log.info("ptz_view_changes.view_changed", preset=str(row.id), state=state, note=note)
    return row.analytics_review


def mark_moved_outside(row: Any) -> None:
    """ptz_presets.sync saw the camera's stored position move: pause the zones until the first confirmed arrival
    takes a picture to line them up against. The old pictures are put aside — checking an arrival against them would
    fail at the new view, so the AI and the patrol would never confirm it."""
    if not row.analytics_config:
        row.reference = row.reference_night = row.reference_at = row.reference_night_at = None
        row.thumbnail = row.thumbnail_at = None
        return
    old = row.reference or row.reference_night or row.thumbnail
    if old is not None:
        row.previous_reference = old
    row.reference = row.reference_night = row.reference_at = row.reference_night_at = None
    row.thumbnail = row.thumbnail_at = None
    row.analytics_review = record("awaiting_picture", "re-stored in the camera's own settings — the zones will be lined "
                                  "up with the new view at its next visit", "re-stored outside VMS", None, None, False,
                                  row.analytics_config)


async def after_first_picture(row: Any, new_jpeg: bytes) -> tuple[bool, Optional[dict]]:
    """The first picture of a view re-stored outside VMS (a confirmed arrival, or "Take picture now"): line the zones
    up against it now. (handled? — then the picture is stored too —, the new review or None = the zones run again)."""
    if not (isinstance(row.analytics_review, dict) and row.analytics_review.get("state") == "awaiting_picture"):
        return False, None
    old = row.previous_reference
    row.analytics_review = None
    return True, await apply_new_view(row, new_jpeg, old=old, zoom_changed=False, reason="re-stored outside VMS",
                                      by=None)


# ── delete → archive, and back ───────────────────────────────────────────────

def archive_row(row: Any, by: Optional[str], reason: str):
    """A PtzViewArchive for a preset about to be deleted, or None when it had no zones or rules (nothing to keep)."""
    from ..models import PtzViewArchive
    cfg = row.analytics_config
    if not cfg or not ((cfg.get("regions") or {}) or (cfg.get("activities") or [])):
        return None
    return PtzViewArchive(id=uuid.uuid4(), camera_id=row.camera_id, preset_id=row.id, token=row.token, name=row.name,
                          pan=row.pan, tilt=row.tilt, zoom=row.zoom, analytics_config=cfg,
                          picture=row.reference or row.reference_night or row.thumbnail, reason=reason[:255],
                          archived_by=by)


def archive_out(a: Any) -> dict:
    from .ptz_presets import ai_summary
    return {"id": str(a.id), "name": a.name, "token": a.token, "ai": ai_summary(a.analytics_config),
            "reason": a.reason, "archived_by": a.archived_by, "has_picture": a.picture is not None,
            "archived_at": a.archived_at.isoformat() if a.archived_at else None}


async def copy_from_archive(row: Any, archived: Any, by: Optional[str]) -> dict:
    """Put an archived view's zones + rules on preset `row` (replacing its own). Lined up against the preset's picture
    when both have one — but always "review": another preset is another view, however well the pictures match."""
    from . import ptz_track
    cfg = archived.analytics_config
    target = row.reference or row.reference_night or row.thumbnail
    offset = None
    if archived.picture and target:
        got = await asyncio.to_thread(ptz_track.scene_offset, archived.picture, target)
        offset = (got[0], got[1]) if got else None
    new_cfg, _state, note = plan(cfg, offset, zoom_moved(archived.zoom, row.zoom))
    if not note:
        note = "the pictures line up"
    own = row.analytics_config
    row.analytics_config = new_cfg
    row.previous_reference = archived.picture
    row.analytics_review = record("review", f"copied from deleted preset “{archived.name}”: {note}",
                                  f"copied from “{archived.name}”", by, offset, zoom_moved(archived.zoom, row.zoom),
                                  own)
    return row.analytics_review
