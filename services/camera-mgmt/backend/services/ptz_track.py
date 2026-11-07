"""ptz_track.py — where every PTZ camera is pointing, and the history of who moved it where.

KEC kec01, 2026-10-26: "keep a track that camera is on which position". A leader-only loop asks each PTZ camera
for its position (ONVIF GetStatus — a tiny SOAP call, through the same per-worker session pool the pad uses) and
compares it with that camera's presets (models.PtzPreset, tolerance as time-lapse's `preset_match`):

    at_preset   within tolerance of a preset ("📍 Gate")
    moving      the camera says it is moving
    off_preset  stopped somewhere that is not a preset (an operator left it zoomed in)
    unknown     no position reported (some makes never do) or the camera did not answer

Cadence: a camera someone is driving or watching the pad of (lease / warm marker), or that moved recently, every
POLL_HOT_S; every other one every POLL_IDLE_S — a dozen PTZ heads cost a SOAP call each per half minute.

THE VIEW (`ptz:view:<camera_id>` + Valkey channel `vms.ptz.view.<slug>`): which preset the camera is CONFIRMED to be
looking at, for everything that works per preset (AI zones next). "at_preset" from GetStatus is not enough — some
heads say "arrived" while still turning (alpha's i-PRO) — so on every arrival the picture is checked first
(`verify_arrival`: it has stopped changing, and it matches the preset's own picture). Until then, and whenever the
camera is moving or off a preset, the view is {"state": "none"}. Published only when it changes.

CURRENT state lives in Valkey (`ptz:pos:<camera_id>`, read by all workers for the live tiles); HISTORY in
Postgres (models.PtzPositionLog), one row per CHANGE — state, preset, or (off-preset) a different spot. The cause
comes from the newest mover hint (services/ptz_control.note_mover): operator / preset / home / patrol /
calibration; no fresh hint = "camera" (it moved by itself: its own web UI or tour, a reboot).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

log = structlog.get_logger(__name__)

POLL_HOT_S = 3.0
POLL_IDLE_S = 30.0
HOT_AFTER_MOVE_S = 60.0
TICK_S = 1.0
TOLERANCE = 0.02            # normalised units, the same default time-lapse uses for "at the preset"
OFF_PRESET_MOVED = 0.03     # an off-preset head that moved this far is a new spot (a new history row)
CURRENT_TTL_S = 600
RETENTION_DAYS = 90
_PRUNE_EVERY_S = 3600


def current_key(camera_id: Any) -> str:
    return f"ptz:pos:{camera_id}"


def _delta(a: Optional[float], b: Optional[float], wraps: bool) -> Optional[float]:
    if a is None or b is None:
        return None
    d = abs(float(a) - float(b))
    return min(d, 2.0 - d) if wraps else d


def classify(status: Optional[dict], presets: list[dict], tol: float = TOLERANCE) -> tuple[str, Optional[dict]]:
    """(state, preset) — pure. `presets` = [{token, name, pan, tilt, zoom}]; the NEAREST preset within `tol` on
    every axis both sides report wins (two presets a hair apart: the closer one)."""
    if not status or status.get("pan") is None and status.get("tilt") is None and status.get("zoom") is None:
        return "unknown", None
    moving = {str(status.get(k) or "").upper() for k in ("moving_pan_tilt", "moving_zoom")}
    if "MOVING" in moving:
        return "moving", None
    best, best_d = None, None
    for p in presets:
        worst, compared = 0.0, 0
        for axis, wraps in (("pan", True), ("tilt", False), ("zoom", False)):
            d = _delta(status.get(axis), p.get(axis), wraps)
            if d is None:
                continue
            compared += 1
            worst = max(worst, d)
        if compared and worst <= tol and (best_d is None or worst < best_d):
            best, best_d = p, worst
    return ("at_preset", best) if best is not None else ("off_preset", None)


def same_preset(prev: Optional[dict], preset: Optional[dict]) -> bool:
    """By VMS id when both sides have one (a token can be a different preset after a delete + save), else token."""
    a, b = (prev or {}).get("preset_id"), (preset or {}).get("id")
    if a and b:
        return str(a) == str(b)
    return (prev or {}).get("preset_token") == (preset or {}).get("token")


def changed(prev: Optional[dict], state: str, preset: Optional[dict], status: Optional[dict]) -> bool:
    """Does this observation start a new history row?"""
    if prev is None:
        return True
    if prev.get("state") != state:
        return True
    if state == "at_preset":
        return not same_preset(prev, preset)
    if state == "off_preset":
        moved = [_delta(prev.get(a), (status or {}).get(a), a == "pan") for a in ("pan", "tilt", "zoom")]
        return any(m is not None and m > OFF_PRESET_MOVED for m in moved)
    return False


def attribute(hint: Optional[dict], prev: Optional[dict], now_ts: float) -> tuple[Optional[str], Optional[str]]:
    """(cause, actor) for a change: the newest mover hint if it is fresh; else, when the previous row was
    "moving" with a cause, that same move finishing; else the camera itself."""
    if hint and now_ts - float(hint.get("at") or 0) <= HOT_AFTER_MOVE_S + 30:
        return hint.get("cause"), hint.get("actor")
    if prev and prev.get("state") == "moving" and prev.get("cause"):
        return prev.get("cause"), prev.get("actor")
    return "camera", None


async def current_for(camera_ids: list[str]) -> dict[str, dict]:
    from .. import redis_client
    if not camera_ids:
        return {}
    r = await redis_client.get_redis()
    raws = await r.mget([current_key(c) for c in camera_ids])
    out = {}
    for cid, raw in zip(camera_ids, raws):
        try:
            if raw:
                out[cid] = json.loads(raw)
        except ValueError:
            pass
    return out


def decide(prev: Optional[dict], status: Optional[dict], presets: list[dict], hint: Optional[dict],
           now: datetime) -> tuple[dict, Optional[dict]]:
    """Pure: (the new current state, the history row to write or None) for one reading."""
    state, preset = classify(status, presets)
    st = status or {}
    cur = {"state": state, "preset_token": (preset or {}).get("token"), "preset_id": (preset or {}).get("id"),
           "preset_name": (preset or {}).get("name"),
           "pan": st.get("pan"), "tilt": st.get("tilt"), "zoom": st.get("zoom"),
           "checked_at": now.isoformat(), "since": (prev or {}).get("since") or now.isoformat(),
           "cause": (prev or {}).get("cause"), "actor": (prev or {}).get("actor")}
    if not changed(prev, state, preset, status):
        return cur, None
    # First sight (no previous state — e.g. after a restart) is not a move anyone made.
    cause, actor = attribute(hint, prev, now.timestamp()) if prev is not None else (None, None)
    cur.update({"since": now.isoformat(), "cause": cause, "actor": actor})
    row = {"at": now, "state": state, "preset_token": cur["preset_token"], "preset_name": cur["preset_name"],
           "preset_id": _uuid_or_none(cur["preset_id"]),
           "pan": cur["pan"], "tilt": cur["tilt"], "zoom": cur["zoom"], "cause": cause, "actor": actor}
    return cur, row


def _uuid_or_none(v: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:                                  # HOME_TOKEN — home is no ptz_presets row
        return None


async def observe(camera_id: uuid.UUID, slug: str, status: Optional[dict], presets: list[dict],
                  now: Optional[datetime] = None) -> Optional[dict]:
    """Classify one reading, update the current state, write a history row on a change. Returns the row written
    or None."""
    from sqlalchemy import select

    from .. import redis_client
    from ..db import AsyncSessionLocal
    from ..models import PtzPositionLog
    from . import ptz_control

    now = now or datetime.now(timezone.utc)
    r = await redis_client.get_redis()
    raw = await r.get(current_key(camera_id))
    try:
        prev = json.loads(raw) if raw else None
    except ValueError:
        prev = None
    cur, row = decide(prev, status, presets, await ptz_control.last_mover(slug), now)
    if row is not None:
        async with AsyncSessionLocal() as db:
            last = (await db.execute(select(PtzPositionLog).where(PtzPositionLog.camera_id == camera_id)
                                     .order_by(PtzPositionLog.at.desc()).limit(1))).scalar_one_or_none()
            # After a restart (no current state in Valkey) the same state as the newest stored row is not a change.
            if prev is None and last is not None and last.state == row["state"] \
                    and last.preset_token == row["preset_token"] \
                    and (last.preset_id is None or row["preset_id"] is None or last.preset_id == row["preset_id"]):
                cur.update({"since": last.at.isoformat(), "cause": last.cause, "actor": last.actor})
                row = None
            else:
                db.add(PtzPositionLog(camera_id=camera_id, **row))
                await db.commit()
    await r.set(current_key(camera_id), json.dumps(cur), ex=CURRENT_TTL_S)
    await _track_view(r, camera_id, slug, cur)
    return row


async def _ptz_cameras() -> list[tuple[uuid.UUID, str]]:
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera
    async with AsyncSessionLocal() as db:
        cams = (await db.execute(select(Camera.id, Camera.slug, Camera.ptz_capabilities)
                                 .where(Camera.enabled.is_(True)))).all()
    return [(cid, slug) for cid, slug, caps in cams if (caps or {}).get("supported") is True]


HOME_TOKEN = "__home__"   # the camera's home position, when VMS knows where it is (routers/ptz._home_ref)


async def _presets_of(camera_id: uuid.UUID) -> list[dict]:
    """The camera's presets to classify against — plus HOME as one more, when VMS has recorded where it points.
    Without it, a camera sitting at its home read "Off preset" (2026-10-27): ONVIF can't read a home back, so it
    was never in the list. Listed last: a real preset at the same spot wins the tie (classify keeps the first)."""
    from sqlalchemy import select

    from ..db import AsyncSessionLocal
    from ..models import Camera
    from . import ptz_presets
    async with AsyncSessionLocal() as db:
        out = [{"id": str(p.id), "token": p.token, "name": p.name, "pan": p.pan, "tilt": p.tilt, "zoom": p.zoom}
               for p in await ptz_presets.rows(db, camera_id) if not p.missing_on_camera]
        meta = (await db.execute(select(Camera.camera_metadata).where(Camera.id == camera_id))).scalar_one_or_none()
    home = (meta or {}).get("ptz_home")
    if isinstance(home, dict) and home.get("pan") is not None and home.get("tilt") is not None:
        out.append({"id": HOME_TOKEN, "token": HOME_TOKEN, "name": "Home", "pan": home["pan"], "tilt": home["tilt"],
                    "zoom": home.get("zoom")})
    return out


async def _is_hot(slug: str) -> bool:
    from .. import redis_client
    from . import ptz_control
    r = await redis_client.get_redis()
    if await r.exists(f"ptz:lock:{slug}", f"ptz:warm:{slug}"):
        return True
    hint = await ptz_control.last_mover(slug)
    return bool(hint and time.time() - float(hint.get("at") or 0) < HOT_AFTER_MOVE_S)


async def _poll_one(camera_id: uuid.UUID, slug: str) -> str:
    from . import onvif_ptz, ptz_control
    target = await ptz_control._resolve_target(slug)    # noqa: SLF001 — the loop-safe resolver
    status = None
    if target is not None:
        ip, port, user, password, profile = target
        try:
            status = await onvif_ptz.get_status(ip, port, user, password, profile)
        except Exception as exc:                        # noqa: BLE001 — unreachable = "unknown", not a crash
            log.debug("ptz_track.status_failed", slug=slug, error=str(exc)[:200])
    await observe(camera_id, slug, status, await _presets_of(camera_id))
    state, _ = classify(status, [])
    return state


# ─── The confirmed view (what per-preset AI follows) ─────────────────────────

VIEW_TTL_S = CURRENT_TTL_S
VERIFY_STILL_TRIES = 5          # grabs, 1 s apart, for the picture to stop changing
VERIFY_MAX_SHIFT = 0.06         # the picture may sit this far (fraction of the frame) from the preset's own picture
VERIFY_ATTEMPTS = 3             # a picture that hadn't settled is checked again on the next readings, this many times
SETTLE_FAIL = "the picture didn't settle"
_verify_tasks: dict[str, asyncio.Task] = {}


def view_key(camera_id: Any) -> str:
    return f"ptz:view:{camera_id}"


def view_channel(slug: str) -> str:
    return f"vms.ptz.view.{slug}"


def view_for(cur: dict, verified_by: str, verified_at: datetime) -> dict:
    return {"state": "preset", "preset_id": cur.get("preset_id"), "preset_token": cur.get("preset_token"),
            "preset_name": cur.get("preset_name"), "arrived_at": cur.get("since"),
            "verified_at": verified_at.isoformat(), "verified_by": verified_by}


def no_view(reason: str, cur: Optional[dict] = None) -> dict:
    out = {"state": "none", "reason": reason}
    if cur and cur.get("state") == "at_preset":
        out.update({"preset_id": cur.get("preset_id"), "preset_name": cur.get("preset_name")})
    return out


REF_WIDTH = 1280              # stored reference pictures: wide enough to draw zones on, ~100 KB
REF_REFRESH_S = 24 * 3600     # a reference is refreshed from a confident match at most once a day
REF_REFRESH_CONF = 0.6        # … and only from a very sure one (≥ 60 % of the textured tiles agree)
REF_REFRESH_SHIFT = 0.01


def is_night(jpeg: Optional[bytes]) -> bool:
    """IR (night) pictures are grey: almost no colour. Day and night references are kept apart, because an IR
    picture compared with a daytime one rarely matches with confidence."""
    if not jpeg:
        return False
    import io

    from PIL import Image
    try:
        im = Image.open(io.BytesIO(jpeg)).convert("HSV").resize((64, 36))
    except Exception:                                   # noqa: BLE001
        return False
    sat = list(im.getdata(band=1))
    return (sum(sat) / max(1, len(sat))) < 12


def fit_to(jpeg: bytes, size: tuple[int, int]) -> bytes:
    """`jpeg` resized to exactly `size`. A sub stream is the same view scaled — 704×576 for a 16:9 sensor is the
    whole view squeezed — so stretching it back to the reference's size restores the geometry before comparing."""
    import io

    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if im.size == size:
        return jpeg
    buf = io.BytesIO()
    im.resize(size).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def shrink(jpeg: bytes, width: int = REF_WIDTH) -> bytes:
    import io

    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ── Same view? A vote of tiles, not one global match ──
# measure_shift answers "how far did the picture move" between two frames of one scene — calibration's question.
# Against a preset's stored picture the question is different: "is this the same view, even if part of the scene
# changed?" A crane gone from a quarter of the frame gave measure_shift a confident WRONG shift (0.55 of the frame,
# confidence 0.45 — tests/test_ptz_per_preset_ai.py), which would have kept the AI off at that stop. So: cut the
# picture into tiles and let every textured tile vote — does it line up within a few percent of where it was? A
# changed area loses its own votes and nothing else. "Elsewhere" needs POSITIVE evidence (many tiles agreeing at
# one other offset); anything unclear is "unknown" and falls back to the position check, which never blocks the AI.
TILES_X, TILES_Y = 8, 6
TILE_SEARCH = 0.03            # each tile may sit this far (fraction of the width) from where it was
TILE_MIN_TEXTURE = 8.0        # a detail tile flatter than this (sky, a wall) has no say
TILE_DISTINCT = 0.7           # a tile's best fit must beat its typical fit by this much to count as a match
VIEW_AGREE = 0.35             # share of the textured tiles that must agree for "same" (or "elsewhere")
VIEW_MIN_TILES = 6


def _detail_pair(reference: bytes, still: bytes, width: int = 256):
    import io

    from PIL import Image

    from .ptz_calibrate import _grey
    ref = Image.open(io.BytesIO(reference)).convert("L")
    cur = Image.open(io.BytesIO(still)).convert("L").resize(ref.size)
    return _grey(ref, width), _grey(cur, width)


def tile_votes(ref_d, cur_d, offset: tuple[float, float] = (0.0, 0.0),
               search: float = TILE_SEARCH) -> tuple[int, int, list[tuple[int, int]]]:
    """(tiles that line up near `offset`, textured tiles, their offsets in px) — pure, on two detail images of one
    size. `offset` (fraction of the frame, +x right / +y down) = where the scene is expected to have moved to."""
    from PIL import ImageChops, ImageStat
    w, h = ref_d.size
    tw, th = w // TILES_X, h // TILES_Y
    r = max(2, round(search * w))
    ox, oy = round(offset[0] * w), round(offset[1] * h)
    textured, found = 0, []
    for ty in range(TILES_Y):
        for tx in range(TILES_X):
            x0, y0 = tx * tw, ty * th
            tile = ref_d.crop((x0, y0, x0 + tw, y0 + th))
            if ImageStat.Stat(tile).stddev[0] < TILE_MIN_TEXTURE:
                continue
            fits = []
            for dy in range(-r, r + 1, 2):
                for dx in range(-r, r + 1, 2):
                    x, y = x0 + ox + dx, y0 + oy + dy
                    if x < 0 or y < 0 or x + tw > w or y + th > h:
                        continue
                    diff = ImageChops.difference(tile, cur_d.crop((x, y, x + tw, y + th)))
                    fits.append((ImageStat.Stat(diff).mean[0], dx, dy))
            if len(fits) < 4:
                continue                                # off the picture at this offset: no say either way
            textured += 1
            fits.sort()
            typical = fits[len(fits) // 2][0]
            best, bx, by = fits[0]
            # A best fit ON the edge of the search window means the real match lies outside it — not a vote.
            if typical > 0 and best <= TILE_DISTINCT * typical and abs(bx) < r - 1 and abs(by) < r - 1:
                found.append((bx, by))
    # And the votes must agree with EACH OTHER: one scene moves as one, so tiles lining up at scattered offsets
    # (repeating texture, noise) don't count.
    if found:
        mx = sorted(f[0] for f in found)[len(found) // 2]
        my = sorted(f[1] for f in found)[len(found) // 2]
        found = [f for f in found if abs(f[0] - mx) <= 3 and abs(f[1] - my) <= 3]
    agree = len(found)
    return agree, textured, found


def view_match(reference: bytes, still: bytes) -> tuple[str, dict]:
    """("same" | "elsewhere" | "unknown", {agree, shift}) — is `still` the view `reference` shows?"""
    import io

    from PIL import Image

    from .ptz_calibrate import _MIN_CONFIDENCE, measure_shift
    still = fit_to(still, Image.open(io.BytesIO(reference)).size)    # a squeezed sub stream back to the view's shape
    ref_d, cur_d = _detail_pair(reference, still)
    agree, textured, found = tile_votes(ref_d, cur_d)
    if textured < VIEW_MIN_TILES:
        return "unknown", {"agree": 0.0, "textured": textured}
    share = agree / textured
    if share >= VIEW_AGREE:
        xs, ys = sorted(f[0] for f in found), sorted(f[1] for f in found)
        w, h = ref_d.size
        shift = max(abs(xs[len(xs) // 2]) / w, abs(ys[len(ys) // 2]) / h)
        return "same", {"agree": round(share, 3), "shift": round(shift, 4), "textured": textured}
    sx, sy, conf = measure_shift(reference, still)
    if conf >= _MIN_CONFIDENCE and max(abs(sx), abs(sy)) > VERIFY_MAX_SHIFT:
        # measure_shift's x is the camera's pan direction — the scene moves the other way across the picture.
        moved, overlap, _ = tile_votes(ref_d, cur_d, offset=(-sx, sy))
        if overlap >= VIEW_MIN_TILES and moved / overlap >= VIEW_AGREE:
            return "elsewhere", {"agree": round(moved / overlap, 3), "shift": round(max(abs(sx), abs(sy)), 4),
                                 "textured": overlap}
    return "unknown", {"agree": round(share, 3), "textured": textured}


def scene_offset(reference: bytes, still: bytes) -> Optional[tuple[float, float, float]]:
    """(dx, dy, agree): where the scene of `reference` sits in `still`, as a fraction of the frame (+x right, +y down) —
    a point at (x, y) of the old picture is at (x + dx, y + dy) of the new one. None when it can't be told for sure
    (another place, another zoom, night against day, a blank scene). For a re-saved preset (services/ptz_view_changes):
    the zones follow the view by this much. Same evidence as view_match: many textured tiles agreeing on ONE offset."""
    import io

    from PIL import Image

    from .ptz_calibrate import _MIN_CONFIDENCE, measure_shift
    still = fit_to(still, Image.open(io.BytesIO(reference)).size)
    ref_d, cur_d = _detail_pair(reference, still)
    w, h = ref_d.size

    def settle(offset: tuple[float, float]) -> Optional[tuple[float, float, float]]:
        agree, textured, found = tile_votes(ref_d, cur_d, offset=offset)
        if textured < VIEW_MIN_TILES or agree / textured < VIEW_AGREE:
            return None
        mx = sorted(f[0] for f in found)[len(found) // 2]
        my = sorted(f[1] for f in found)[len(found) // 2]
        return round(offset[0] + mx / w, 4), round(offset[1] + my / h, 4), round(agree / textured, 3)

    near = settle((0.0, 0.0))
    if near is not None:
        return near
    sx, sy, conf = measure_shift(reference, still)
    if conf < _MIN_CONFIDENCE:
        return None
    return settle((-sx, sy))                            # measure_shift's x is the camera's pan; the scene goes the other way


async def verify_arrival(frame, reference, sleep=asyncio.sleep, info: Optional[dict] = None) -> tuple[bool, str]:
    """(confirmed?, how / why not). `frame()` → a JPEG of where the head is now. First the picture must stop changing
    (a head that claims to have arrived while still turning fails here), then — when the preset has a picture of its
    own — it must show the same scene. A comparison that can't be made (night against a daytime picture, a blank
    scene) falls back to the position + the still picture: the AI is better off running on a verified-still view
    than never running at night.

    `reference`: the preset's picture, or a function of the still picture choosing one (day or night). `info`, when
    given, receives {still, shift, conf} for the caller's reference upkeep."""
    from .ptz_calibrate import _MIN_CONFIDENCE, _STILL_MAX_SHIFT, measure_shift
    prev = await frame()
    still = None
    for _ in range(VERIFY_STILL_TRIES):
        await sleep(1.0)
        cur = await frame()
        if prev and cur:
            sx, sy, conf = await asyncio.to_thread(measure_shift, prev, cur)
            if conf >= _MIN_CONFIDENCE and abs(sx) <= _STILL_MAX_SHIFT and abs(sy) <= _STILL_MAX_SHIFT:
                still = cur
                break
        prev = cur or prev
    if still is None:
        return False, SETTLE_FAIL
    if info is not None:
        info["still"] = still
    if callable(reference):
        reference = reference(still)
    if not reference:
        return True, "position"
    verdict, got = await asyncio.to_thread(view_match, reference, still)
    if info is not None:
        info.update({"verdict": verdict, "conf": got.get("agree", 0.0), "shift": got.get("shift", 1.0)})
    if verdict == "same":
        return True, "picture"
    if verdict == "elsewhere":
        return False, f"the picture doesn't match the preset's (off by {round(100 * got['shift'])}% of the frame)"
    return True, "position"                             # can't tell from the picture (a changed scene, night, blank)


async def _set_view(r, camera_id: Any, slug: str, view: dict) -> None:
    """Write the view; publish it only when it CHANGED (the TTL refresh is silent)."""
    key = view_key(camera_id)
    raw = await r.get(key)
    try:
        old = json.loads(raw) if raw else None
    except ValueError:
        old = None
    same = old is not None and {k: v for k, v in old.items() if k != "verified_at"} == \
        {k: v for k, v in view.items() if k != "verified_at"}
    await r.set(key, json.dumps(view if not same else old), ex=VIEW_TTL_S)
    # Per-preset AI switches only when WHERE the camera looks changes — not on a new "why not" reason.
    moved_view = old is None or old.get("state") != view.get("state") or old.get("preset_id") != view.get("preset_id")
    if moved_view:
        from . import ptz_views
        await ptz_views.on_view_change(r, camera_id, slug, view)
    if not same:
        try:
            await r.publish(view_channel(slug), json.dumps({"camera_id": str(camera_id), "slug": slug, **view}))
        except Exception as exc:                        # noqa: BLE001 — subscribers re-read the key anyway
            log.debug("ptz_track.view_publish_failed", slug=slug, error=str(exc)[:200])


async def _track_view(r, camera_id: Any, slug: str, cur: dict) -> None:
    """Keep the confirmed view in step with the latest reading: an arrival (a new `since` at a preset) starts one
    picture check; anything else that isn't the confirmed preset clears the view at once."""
    cid = str(camera_id)
    raw = await r.get(view_key(camera_id))
    try:
        view = json.loads(raw) if raw else None
    except ValueError:
        view = None
    if cur.get("state") != "at_preset":
        task = _verify_tasks.pop(cid, None)
        if task and not task.done():
            task.cancel()
        await _set_view(r, camera_id, slug, no_view(cur.get("state") or "unknown"))
        return
    arrival = cur.get("since")
    if view and view.get("state") == "preset" and view.get("arrived_at") == arrival \
            and same_preset(view, {"id": cur.get("preset_id"), "token": cur.get("preset_token")}):
        await r.expire(view_key(camera_id), VIEW_TTL_S)    # still there, still confirmed
        return
    task = _verify_tasks.get(cid)
    if task and not task.done() and getattr(task, "arrival", None) == arrival:
        return                                          # this arrival is being checked
    if task and not task.done():
        task.cancel()
    attempts = int((view or {}).get("attempts") or 0) if (view or {}).get("arrival") == arrival else 0
    settled_failed = (view or {}).get("reason") == SETTLE_FAIL
    if not (view and view.get("state") == "none" and view.get("arrival") == arrival and view.get("reason") != "arriving"
            and not (settled_failed and attempts < VERIFY_ATTEMPTS)):
        await _set_view(r, camera_id, slug, {**no_view("arriving", cur), "arrival": arrival, "attempts": attempts + 1})
        t = asyncio.ensure_future(_confirm(camera_id, slug, dict(cur), attempts + 1))
        t.arrival = arrival                             # type: ignore[attr-defined]
        _verify_tasks[cid] = t


async def _confirm(camera_id: Any, slug: str, cur: dict, attempts: int = 1) -> None:
    """Check the picture after an arrival; publish the view if the camera is still at that same arrival."""
    from sqlalchemy import select

    from .. import redis_client
    from ..config import settings
    from ..db import AsyncSessionLocal
    from ..models import Camera, PtzPreset, sub_recording_name
    from . import snapshot as snapshot_svc
    from .tracks import sub_is_resolved
    try:
        refs: dict = {}
        pid = _uuid_or_none(cur.get("preset_id"))
        async with AsyncSessionLocal() as db:
            cam = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
            if pid is not None:
                got = (await db.execute(select(PtzPreset.reference, PtzPreset.reference_at, PtzPreset.reference_night,
                                               PtzPreset.reference_night_at, PtzPreset.thumbnail)
                                        .where(PtzPreset.id == pid))).first()
                if got is not None:
                    refs = dict(zip(("day", "day_at", "night", "night_at", "thumb"), got))

        def pick(still: bytes) -> Optional[bytes]:
            # The reference of the same light as the picture now; the small thumbnail when there is none yet.
            return (refs.get("night") if is_night(still) else refs.get("day")) or refs.get("thumb")
        # The sub stream when there is one: a 704×576 H.264 decode costs a fraction of the main's 2560×1440 HEVC, and
        # the check needs only the shape of the scene. RTSP (not HLS): HLS runs 2 s behind the head.
        paths = ([sub_recording_name(slug)] if cam is not None and sub_is_resolved(cam) else []) + [slug]

        async def frame():
            for p in paths:
                jpeg = await snapshot_svc.decode_snapshot(settings.internal_rtsp_url(p))
                if jpeg:
                    return jpeg
            return None

        info: dict = {}
        ok, how = await verify_arrival(frame, pick, info=info)
        r = await redis_client.get_redis()
        raw = await r.get(current_key(camera_id))
        now_cur = json.loads(raw) if raw else {}
        if now_cur.get("state") != "at_preset" or now_cur.get("since") != cur.get("since"):
            return                                      # it moved on meanwhile; the next arrival checks again
        view = view_for(cur, how, datetime.now(timezone.utc)) if ok else \
            {**no_view(how, cur), "arrival": cur.get("since"), "attempts": attempts}
        await _set_view(r, camera_id, slug, view)
        if not ok:
            log.info("ptz_track.arrival_not_confirmed", slug=slug, preset=cur.get("preset_name"), why=how)
        elif pid is not None and refs is not None:
            await _keep_reference(pid, slug, refs, how, info)
    except asyncio.CancelledError:
        raise
    except Exception as exc:                            # noqa: BLE001 — a failed check leaves the view "none"
        log.warning("ptz_track.arrival_check_failed", slug=slug, error=str(exc)[:200])


def reference_due(refs: dict, night: bool, how: str, info: dict, now: datetime) -> bool:
    """Pure: store a new reference for this light? When there is none yet (first confirmed arrival), or after a very
    sure match once the stored one is a day old — so a site that changes week by week keeps a current picture, taken
    only at moments the camera was certainly in place (a match within 1 % of the frame), so it can't drift."""
    slot, at = ("night", "night_at") if night else ("day", "day_at")
    if not refs.get(slot):
        return how in ("picture", "position")
    if how != "picture" or info.get("conf", 0) < REF_REFRESH_CONF or info.get("shift", 1) > REF_REFRESH_SHIFT:
        return False
    stored = refs.get(at)
    if stored is not None and stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored is None or (now - stored).total_seconds() >= REF_REFRESH_S


async def _keep_reference(pid: uuid.UUID, slug: str, refs: dict, how: str, info: dict) -> None:
    """Store the view's reference picture when due (reference_due) — from the MAIN stream, full view, shrunk to
    REF_WIDTH: it is what zones are drawn on."""
    from ..config import settings
    from ..db import AsyncSessionLocal
    from ..models import PtzPreset
    from . import snapshot as snapshot_svc
    try:
        night = is_night(info.get("still"))
        now = datetime.now(timezone.utc)
        if not reference_due(refs, night, how, info, now):
            return
        jpeg = await snapshot_svc.decode_snapshot(settings.internal_rtsp_url(slug))
        if not jpeg:
            return
        small = await asyncio.to_thread(shrink, jpeg)
        from . import ptz_presets, ptz_view_changes, ptz_views
        async with AsyncSessionLocal() as db:
            row = await db.get(PtzPreset, pid)
            if row is None:
                return
            # Re-stored in the camera's own settings: the first picture of the new view is what its zones get lined
            # up against (services/ptz_view_changes) — it stores the reference itself.
            handled, lined_up = await ptz_view_changes.after_first_picture(row, jpeg)
            if not handled:
                if night:
                    row.reference_night, row.reference_night_at = small, now
                else:
                    row.reference, row.reference_at = small, now
            if row.thumbnail is None:
                row.thumbnail, row.thumbnail_at = await asyncio.to_thread(ptz_presets.make_thumbnail, jpeg), now
            camera_id = row.camera_id
            await db.commit()
        log.info("ptz_track.reference_kept", slug=slug, preset=str(pid), night=night, how=how,
                 zones=(lined_up or {}).get("state"))
        if handled:                                     # its rules may run again (or stay paused, moved)
            await ptz_views.push_now(camera_id)
    except Exception as exc:                            # noqa: BLE001 — upkeep must never disturb tracking
        log.debug("ptz_track.reference_failed", slug=slug, error=str(exc)[:200])


async def current_view(camera_id: Any) -> Optional[dict]:
    from .. import redis_client
    r = await redis_client.get_redis()
    raw = await r.get(view_key(camera_id))
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


# How long history is kept (migration 064): operator moves — what an investigation needs, "who moved it" — and
# patrol steps, which at 20 s stops are ~6 rows a minute per camera (~400 MB a camera over 90 days, x2 with the
# patrol run log). Admin-editable on Cameras → PTZ.
RETENTION_DEFAULTS = {"operator_days": RETENTION_DAYS, "patrol_days": 14}
RETENTION_LIMITS = {"operator_days": (7, 365), "patrol_days": (1, 90)}


def clean_retention(raw: Optional[dict]) -> dict:
    out = dict(RETENTION_DEFAULTS)
    for k, (lo, hi) in RETENTION_LIMITS.items():
        v = (raw or {}).get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(max(lo, min(hi, v)))
    return out


async def retention() -> dict:
    from ..db import AsyncSessionLocal
    from ..models import PtzSettings
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(PtzSettings, 1)
        return clean_retention(row.history if row else None)
    except Exception as exc:                            # noqa: BLE001 — a missing table (pre-064) = defaults
        log.debug("ptz_track.retention_read_failed", error=str(exc)[:200])
        return dict(RETENTION_DEFAULTS)


async def prune(days: Optional[int] = None) -> int:
    """Delete history past its retention: patrol-caused rows after patrol_days, everything else after
    operator_days (`days` overrides both — tests and one-off cleanups)."""
    from sqlalchemy import delete, or_

    from ..db import AsyncSessionLocal
    from ..models import PtzPositionLog, PtzViewHistory
    keep = await retention()
    now = datetime.now(timezone.utc)
    op_days, patrol_days = (days, days) if days else (keep["operator_days"], keep["patrol_days"])
    async with AsyncSessionLocal() as db:
        a = await db.execute(delete(PtzPositionLog).where(
            PtzPositionLog.cause == "patrol", PtzPositionLog.at < now - timedelta(days=patrol_days)))
        b = await db.execute(delete(PtzPositionLog).where(
            or_(PtzPositionLog.cause.is_(None), PtzPositionLog.cause != "patrol"),
            PtzPositionLog.at < now - timedelta(days=op_days)))
        # The confirmed views (datasets at presets) last as long as the operator history.
        c = await db.execute(delete(PtzViewHistory).where(PtzViewHistory.at < now - timedelta(days=max(op_days, patrol_days))))
        await db.commit()
        return int(a.rowcount or 0) + int(b.rowcount or 0) + int(c.rowcount or 0)


async def loop() -> None:
    """Leader-only (main.py → leader.run_as_leader)."""
    polled: dict[str, float] = {}
    moving: set[str] = set()
    cams: list[tuple[uuid.UUID, str]] = []
    loaded_at, pruned_at = -1e9, -1e9
    while True:
        mono = time.monotonic()
        try:
            if mono - loaded_at > 60:
                cams, loaded_at = await _ptz_cameras(), mono
            if mono - pruned_at > _PRUNE_EVERY_S:
                pruned_at = mono
                n = await prune()
                if n:
                    log.info("ptz_track.pruned", rows=n)
            for cid, slug in cams:
                key = str(cid)
                hot = key in moving or await _is_hot(slug)
                if mono - polled.get(key, -1e9) < (POLL_HOT_S if hot else POLL_IDLE_S):
                    continue
                polled[key] = mono
                if await _poll_one(cid, slug) == "moving":
                    moving.add(key)
                else:
                    moving.discard(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                        # noqa: BLE001 — one bad tick must not stop tracking
            log.warning("ptz_track.tick_failed", error=str(exc)[:300])
        await asyncio.sleep(TICK_S)


# ─── Reading the history (PTZ tab History, the site-wide PTZ page, CSV) ──────

def _history_query(camera_ids: Optional[list], since: Optional[datetime], until: Optional[datetime],
                   states: Optional[list], causes: Optional[list], preset_token: Optional[str],
                   actor: Optional[str]):
    from sqlalchemy import select

    from ..models import PtzPositionLog
    q = select(PtzPositionLog)
    if camera_ids:
        q = q.where(PtzPositionLog.camera_id.in_(camera_ids))
    if since is not None:
        q = q.where(PtzPositionLog.at >= since)
    if until is not None:
        q = q.where(PtzPositionLog.at <= until)
    if states:
        q = q.where(PtzPositionLog.state.in_(states))
    if causes:
        # "unknown" in the filter = rows with no cause (first sight after a restart)
        real = [c for c in causes if c != "unknown"]
        conds = [PtzPositionLog.cause.in_(real)] if real else []
        if "unknown" in causes:
            conds.append(PtzPositionLog.cause.is_(None))
        from sqlalchemy import or_
        q = q.where(or_(*conds))
    if preset_token:
        q = q.where(PtzPositionLog.preset_token == preset_token)
    if actor:
        q = q.where(PtzPositionLog.actor.ilike(f"%{actor}%"))
    return q


def row_out(r: Any, names: Optional[dict] = None) -> dict:
    return {"camera_id": str(r.camera_id), "camera_name": (names or {}).get(str(r.camera_id)),
            "at": r.at.isoformat(), "state": r.state, "preset_token": r.preset_token,
            "preset_id": str(r.preset_id) if getattr(r, "preset_id", None) else None, "preset_name": r.preset_name,
            "pan": r.pan, "tilt": r.tilt, "zoom": r.zoom, "cause": r.cause, "actor": r.actor}


async def history(db, *, camera_ids=None, since=None, until=None, states=None, causes=None, preset_token=None,
                  actor=None, offset: int = 0, limit: int = 50) -> tuple[list, int]:
    """(rows newest first, total matching)."""
    from sqlalchemy import func, select

    from ..models import PtzPositionLog
    q = _history_query(camera_ids, since, until, states, causes, preset_token, actor)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(PtzPositionLog.at.desc(), PtzPositionLog.id.desc())
                             .offset(offset).limit(limit))).scalars().all()
    return list(rows), int(total)


async def position_at(db, camera_id, at: datetime) -> Optional[Any]:
    """The row in force at `at` = the newest change at or before it."""
    from sqlalchemy import select

    from ..models import PtzPositionLog
    return (await db.execute(select(PtzPositionLog).where(PtzPositionLog.camera_id == camera_id,
                                                          PtzPositionLog.at <= at)
                             .order_by(PtzPositionLog.at.desc()).limit(1))).scalar_one_or_none()


def time_split(rows_asc: list, since: datetime, until: datetime) -> list[dict]:
    """Pure: how long the camera spent where between since and until. `rows_asc` = change rows in time order,
    the first one being the row in force at `since` (if any). Returns [{key, label, seconds, pct}] biggest first;
    key = "preset:<token>" or the state."""
    spans: dict[str, dict] = {}
    total = max(0.0, (until - since).total_seconds())
    for i, r in enumerate(rows_asc):
        start = max(r["at"], since)
        end = min(rows_asc[i + 1]["at"], until) if i + 1 < len(rows_asc) else until
        secs = (end - start).total_seconds()
        if secs <= 0:
            continue
        if r["state"] == "at_preset":
            key, label = f"preset:{r.get('preset_token')}", r.get("preset_name") or f"Preset {r.get('preset_token')}"
        else:
            key, label = r["state"], {"off_preset": "Off preset", "moving": "Moving", "unknown": "Unknown"}.get(
                r["state"], r["state"])
        s = spans.setdefault(key, {"key": key, "label": label, "seconds": 0.0})
        s["seconds"] += secs
    covered = sum(s["seconds"] for s in spans.values())
    if total - covered > 1:
        spans["no_data"] = {"key": "no_data", "label": "No data", "seconds": total - covered}
    out = sorted(spans.values(), key=lambda s: -s["seconds"])
    for s in out:
        s["seconds"] = round(s["seconds"])
        s["pct"] = round(100 * s["seconds"] / total, 1) if total else 0.0
    return out


async def summary(db, camera_id, since: datetime, until: datetime) -> list[dict]:
    from sqlalchemy import select

    from ..models import PtzPositionLog
    first = await position_at(db, camera_id, since)
    rows = (await db.execute(select(PtzPositionLog).where(PtzPositionLog.camera_id == camera_id,
                                                          PtzPositionLog.at > since, PtzPositionLog.at <= until)
                             .order_by(PtzPositionLog.at.asc()))).scalars().all()
    seq = ([first] if first is not None else []) + list(rows)
    return time_split([{"at": r.at, "state": r.state, "preset_token": r.preset_token, "preset_name": r.preset_name}
                       for r in seq], since, until)


async def preset_visits(db, camera_id) -> dict[str, dict]:
    """{token: {"visits", "last_visit"}} — how often (and when last) the camera arrived at each preset."""
    from sqlalchemy import func, select

    from ..models import PtzPositionLog
    rows = (await db.execute(select(PtzPositionLog.preset_token, func.count(), func.max(PtzPositionLog.at))
                             .where(PtzPositionLog.camera_id == camera_id, PtzPositionLog.state == "at_preset")
                             .group_by(PtzPositionLog.preset_token))).all()
    return {t: {"visits": int(n), "last_visit": last.isoformat() if last else None} for t, n, last in rows if t}


CSV_COLUMNS = ("at", "camera_name", "state", "preset_name", "preset_token", "cause", "actor", "pan", "tilt", "zoom")


def to_csv(items: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for it in items:
        w.writerow([it.get(c) if it.get(c) is not None else "" for c in CSV_COLUMNS])
    return buf.getvalue()
