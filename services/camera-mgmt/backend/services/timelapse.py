"""timelapse.py — per-camera time-lapse: stills on a schedule, videos rendered from them.

WHAT IT IS. A camera with `timelapse_enabled` gets a still every
`interval_s` while its time-lapse schedule is active. The stills are what a
construction client wants to see — the tower rising over 45 days — and the
videos are those stills played back at 24/30 fps. It is OFF per camera by
default, like motion and Smart Search since migration 038: a newly added camera
records and does nothing else until somebody asks.

WHY STILLS AND NOT RECORDINGS. A still every 5 minutes for a 12-hour working
day is ~144 JPEGs ≈ 40 MB/day/camera. The same day of 1080p video is tens of GB.
Keeping 90 days of a project's progress is only affordable this way, and it does
not depend on recording being on at all: a camera kept live-only for privacy can
still produce a progress time-lapse.

WHERE A STILL COMES FROM — the relay, through services/snapshot.shared_snapshot,
never the camera (the single-pull rule). That path also shares its 10-second
cache with the config page's thumbnail, so a tick that lands while an operator
has the mask editor open costs no second FFmpeg.

PRIVACY MASKS ARE ALWAYS APPLIED, AND BEFORE ANYTHING IS STORED. The relay is
NOT masked (masks are burned in by the NVR's recording pipeline only — see
routers/cameras.update_privacy_masks), so a still taken from it carries exactly
what the masks exist to hide. `process()` blurs every mask polygon at native
resolution before the JPEG is re-encoded; there is no setting to turn that off,
and a mask added later does not need a re-capture of anything stored after it.

STORAGE. Stills and rendered videos go to the appliance object store (the
heatmap bucket, under their own `timelapse/` prefix) when one is configured,
which compose does by default; otherwise the bytes stay in the row, the same
fallback heatmaps use. Rows carry `expires_at`; the retention loop deletes the
object and the row together and also enforces the per-camera size cap.

WHO RUNS WHAT. Three loops, each under leader.run_as_leader so exactly one
replica captures, renders and sweeps:
  capture_loop    every few seconds: which enabled cameras are due, grab, process, store
  render_loop     one render at a time from the queue + the nightly "yesterday" render
  retention_loop  expired stills/videos, per-camera size caps, the disk guard
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import object_store
from . import schedule as schedule_svc

log = structlog.get_logger(__name__)

ROOT = "timelapse"

#: The schedule a newly switched-on camera gets: a working day, Monday–Saturday,
#: on the appliance's wall clock (the same clock and shape as the recording
#: schedule — services/schedule.py). Night and Sunday stills of an idle site
#: are what makes a time-lapse unwatchable.
DEFAULT_SCHEDULE: dict[str, Any] = {
    "mode": "weekly", "rules": [{"days": [0, 1, 2, 3, 4, 5], "start": "07:00", "end": "19:00"}],
}

#: Target height per resolution choice. None = keep the camera's own.
RESOLUTION_HEIGHT = {"original": None, "1080p": 1080, "720p": 720}
#: Rough JPEG size at quality 80 of a typical site scene, in KB — only for the
#: storage ESTIMATE shown before switching on, never for enforcement (the cap
#: uses the real stored bytes).
TYPICAL_KB = {"original": 450, "1080p": 260, "720p": 130}

#: Thumbnail used for the "unchanged" comparison: tiny and grey, so the diff is
#: about the scene and not about JPEG noise or a flickering street light.
_THUMB = (32, 18)

#: How long a frame-grab may wait on someone else's decode before this tick
#: gives up. Short on purpose: the next tick is seconds away.
_GRAB_WAIT_S = 3.0
#: After a failed grab (stream offline), don't try this camera again for this long.
_FAIL_BACKOFF_S = 30.0


class TimelapseConfig(BaseModel):
    """A camera's time-lapse settings. Every field has a default, so `{}` is a
    complete, sensible configuration — what a camera gets the first time someone
    flips the switch."""

    model_config = ConfigDict(extra="forbid")

    #: Seconds between stills: 10 s (a fast job, a short window) to 24 h (one a day).
    interval_s: int = Field(default=300, ge=10, le=86400)
    #: When to capture. None = around the clock. Same shape as recording_schedule.
    schedule: Optional[dict[str, Any]] = Field(default_factory=lambda: dict(DEFAULT_SCHEDULE))
    #: Drop near-black stills (night, a lens cap, an IR camera before dawn).
    skip_dark: bool = True
    dark_threshold: int = Field(default=18, ge=0, le=128)
    #: Drop a still that looks like the previous one. Off by default: on a slow
    #: job most of the day IS unchanged, and an operator who wants a steady
    #: cadence must not lose it silently.
    skip_unchanged: bool = False
    unchanged_threshold: float = Field(default=2.0, ge=0.1, le=50.0)
    #: PTZ cameras: the preset (token; a name is accepted for configs saved before
    #: the picker existed) this time-lapse is framed on. A still is kept only when
    #: the head is physically there — see preset_match(). That covers what the old
    #: lease check could not: a patrol moves the camera WITHOUT taking the operator
    #: lease, so "nobody holds the PTZ" never meant "the camera is at the preset".
    ptz_preset_only: Optional[str] = Field(default=None, max_length=64)
    #: How far (in ONVIF's normalised -1..1 units, per axis) the head may sit from
    #: the preset and still count as "at" it. 0.02 is ~3.6° of pan on a 360° head:
    #: loose enough for a head that parks a step off its stored value, tight enough
    #: that a neighbouring preset never passes.
    ptz_tolerance: float = Field(default=0.02, ge=0.001, le=0.5)
    resolution: Literal["original", "1080p", "720p"] = "1080p"
    jpeg_quality: int = Field(default=80, ge=40, le=95)
    #: How long stills are kept. Separate from footage retention: a 90-day
    #: progress record on a camera whose video is kept 7 days is the point.
    retention_days: int = Field(default=90, ge=1, le=3650)
    #: Per-camera ceiling; the oldest stills go first when it is exceeded.
    max_storage_mb: int = Field(default=5120, ge=50, le=1_000_000)
    #: Render yesterday's stills into a video every night.
    auto_render_daily: bool = False
    render_fps: Literal[24, 30] = 24
    render_overlay: bool = True
    render_deflicker: bool = False

    @field_validator("schedule")
    @classmethod
    def _valid_schedule(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if v in (None, {}):
            return None
        from ..models import RecordingSchedule        # the recording schedule's own validator
        return RecordingSchedule(**v).model_dump()


def builtin_defaults() -> dict[str, Any]:
    return TimelapseConfig().model_dump()


# ── site defaults (timelapse_settings, id=1) ─────────────────────────────────
#
# A camera stores only the keys it changes; everything else follows the SITE
# defaults, which are themselves stored as the keys that differ from the built-in
# TimelapseConfig(). So "every camera on 10 minutes" is one edit on the Defaults
# page, and a camera someone set to 1 minute keeps its minute.
#
# Read by four API workers and the capture leader, so it is cached per process and
# re-read every _SITE_TTL_S (and at once by the worker that saved it): a changed
# default reaches the capture loop within seconds, without a query per camera.

#: Settings that only mean something on ONE camera (a PTZ preset names a position
#: on that camera's head), so they are never a site default.
PER_CAMERA_ONLY = frozenset({"ptz_preset_only", "ptz_tolerance"})

#: Disk protection. Below floor_free_pct free on the disk that holds the stills,
#: or with stills + videos over budget_pct of that disk, time-lapse is switched
#: off on every camera with a remark. 15 % is well above the NVR's own 5 %
#: emergency floor, where it starts deleting the OLDEST FOOTAGE to keep recording:
#: stills must never be the reason recordings disappear.
GUARD_DEFAULTS: dict[str, float] = {"floor_free_pct": 15.0, "budget_pct": 20.0}

_SITE_TTL_S = 15.0
_site: dict[str, Any] = {"defaults": {}, "guard": {}, "paused": None, "at": -1e9}


def _site_from_row(row) -> None:
    from ..models import TimelapseSettings
    ok = isinstance(row, TimelapseSettings)
    _site.update(defaults=dict((row.defaults if ok else None) or {}), guard=dict((row.guard if ok else None) or {}),
                 paused=(row.paused if ok else None) or None, at=time.monotonic())


async def load_site(db: AsyncSession, force: bool = False) -> None:
    """Refresh this process's copy of the site settings (cheap when fresh)."""
    if not force and time.monotonic() - _site["at"] < _SITE_TTL_S:
        return
    from ..models import TimelapseSettings
    try:
        row = (await db.execute(select(TimelapseSettings).where(TimelapseSettings.id == 1))).scalar_one_or_none()
    except Exception as exc:                                   # noqa: BLE001 — keep the last good copy
        log.debug("timelapse.site_read_failed", error=str(exc)[:200])
        return
    _site_from_row(row)


async def site_row(db: AsyncSession):
    """The settings row, created on first write."""
    from ..models import TimelapseSettings
    row = (await db.execute(select(TimelapseSettings).where(TimelapseSettings.id == 1))).scalar_one_or_none()
    if not isinstance(row, TimelapseSettings):
        row = TimelapseSettings(id=1)
        db.add(row)
    return row


def site_defaults() -> TimelapseConfig:
    """The site's default settings (built-in ones where the site changed nothing)."""
    try:
        return TimelapseConfig(**(_site["defaults"] or {}))
    except ValidationError as exc:
        log.warning("timelapse.site_defaults_invalid_using_builtin", error=str(exc)[:300])
        return TimelapseConfig()


def guard_settings() -> dict[str, float]:
    g = dict(GUARD_DEFAULTS)
    for k, v in (_site["guard"] or {}).items():
        if k in g and isinstance(v, (int, float)):
            g[k] = float(v)
    return g


def paused_state() -> Optional[dict[str, Any]]:
    return _site["paused"] or None


def effective_config(raw: Optional[dict[str, Any]], base: Optional[TimelapseConfig] = None) -> TimelapseConfig:
    """A camera's own changes over the site defaults. A stored config that no
    longer validates (written by a newer build, edited by hand) falls back to the
    defaults rather than stopping the camera's capture — and says so in the log."""
    base = base or site_defaults()
    try:
        return TimelapseConfig(**{**base.model_dump(), **(raw or {})})
    except ValidationError as exc:
        log.warning("timelapse.config_invalid_using_defaults", error=str(exc)[:300])
        return base


def overrides_of(cfg: TimelapseConfig, base: Optional[TimelapseConfig] = None) -> dict[str, Any]:
    """What a camera stores: only the settings that differ from the defaults."""
    full, b = cfg.model_dump(), (base or site_defaults()).model_dump()
    return {k: v for k, v in full.items() if b.get(k) != v}


# ── bulk switch (Inventory → "Time-lapse on" for many cameras) ───────────────

#: Most cameras one bulk request may touch. Far above any one site (a tower job
#: is 4-20 cameras) and above the whole fleet of a typical appliance, yet small
#: enough that the one-transaction pass, its audit rows and the per-camera
#: result list stay a sub-second request rather than something a proxy times out.
BULK_MAX = 500

#: Settings a bulk request may NOT carry. A preset token names a position on ONE
#: camera's head — "preset 1" on the tower camera and "preset 1" on the gate
#: camera are unrelated places, so copying one token across a selection would
#: silently make every other PTZ camera fail its at-preset check (every still
#: skipped as off_preset). It is chosen per camera, from that camera's own list.
BULK_PER_CAMERA_ONLY = frozenset({"ptz_preset_only"})


@dataclass
class BulkPlan:
    """What a bulk switch does to ONE camera — decided without touching the DB,
    so the whole selection is planned (and every bad row found) before anything
    is written, and so the decision itself is testable as plain data."""
    result: Literal["enabled", "updated", "disabled", "already_enabled", "already_disabled"]
    enabled: bool
    config: dict[str, Any]
    changed: bool


def plan_bulk(*, was_enabled: bool, raw_config: Optional[dict[str, Any]], enabled: bool,
              patch: Optional[dict[str, Any]] = None) -> BulkPlan:
    """The per-camera outcome of a bulk switch, producing exactly what the
    camera's own Time-lapse tab would have saved for the same choices.

    WHY AN OVERLAY ON THE CAMERA'S OWN SETTINGS, NOT A REPLACEMENT. The tab
    loads effective_config(), lets the operator change some fields and PUTs the
    whole thing; a bulk dialog that changes "interval and hours" for twelve
    cameras is that same edit made twelve times. Replacing instead would wipe
    the per-camera choices the dialog never showed — a PTZ camera's preset, a
    retention someone lengthened for the client — which is precisely the
    surprise a bulk action must not have.

    WHY "NO SETTINGS" KEEPS WHAT IS THERE (idempotency). Enabling an
    already-enabled camera without settings is a no-op: its cadence memory is
    not reset and no audit row claims a change. A camera that was never
    configured stores `{}`, and effective_config({}) is the same all-defaults
    TimelapseConfig() the tab shows the first time, so "switch on" gives it the
    defaults — written out explicitly, as the tab's save does.

    WHY OFF NEVER TOUCHES SETTINGS. Switching off keeps the stored config as is,
    so the next "on" (bulk or per camera) resumes exactly what the camera had.

    The result is validated through TimelapseConfig, the same model the
    per-camera PUT uses — a bulk request cannot store anything the tab couldn't.
    """
    current = effective_config(raw_config).model_dump()
    if not enabled:
        if was_enabled:
            return BulkPlan("disabled", False, dict(raw_config or {}), True)
        return BulkPlan("already_disabled", False, dict(raw_config or {}), False)
    target = TimelapseConfig(**{**current, **patch}).model_dump() if patch else current
    stored = overrides_of(TimelapseConfig(**target))
    if not was_enabled:
        return BulkPlan("enabled", True, stored, True)
    if target != current:
        return BulkPlan("updated", True, stored, True)
    return BulkPlan("already_enabled", True, dict(raw_config or {}), False)


def bulk_notes(*, camera_enabled: bool, health_status: Optional[str], ptz_capable: bool,
               cfg: dict[str, Any], enabled: bool) -> list[str]:
    """Things the operator should know about a camera the switch DID apply to.

    WHY AN OFFLINE CAMERA IS STILL SWITCHED ON (a note, not a failure). Nothing
    about time-lapse needs the camera reachable at the moment of the switch: the
    setting lives in the registry, and capture_tick simply finds no frame, marks
    the camera "offline" and retries after _FAIL_BACKOFF_S. Refusing would make
    the operator come back and redo the camera later for no benefit — and a
    construction camera on a flaky 4G link is offline exactly when someone is
    setting the site up. The same holds for a camera that is disabled outright,
    except that capture_tick skips it entirely until it is enabled again.
    """
    if not enabled:
        return []
    notes: list[str] = []
    if not camera_enabled:
        notes.append("The camera itself is disabled — stills start once it is enabled again.")
    elif health_status and health_status != "connected":
        notes.append("Offline right now — capture starts on its own when the stream is back.")
    if ptz_capable and not cfg.get("ptz_preset_only"):
        # A patrol or an operator moves the head; without a preset every
        # position is kept, so the video jumps between views. The preset is
        # per camera (BULK_PER_CAMERA_ONLY), hence a pointer to the tab.
        notes.append("PTZ camera with no preset chosen — stills are kept wherever it points "
                     "(a patrol included). Pick its preset on the camera's Time-lapse tab.")
    return notes


# ── keys ─────────────────────────────────────────────────────────────────────

def _local(dt: datetime) -> datetime:
    """Appliance wall clock — the clock schedules, day folders and the page use."""
    return dt.astimezone()


def frame_key(slug: str, captured_at: datetime) -> str:
    """timelapse/{slug}/{YYYY-MM-DD}/{HHMMSS}.jpg — LOCAL date and time, for the
    same reason heatmap keys are local (object_store.heatmap_key): the schedule,
    the day list and the person reading the bucket during an incident all think
    in the site's clock. Seconds, because the interval can be 10 s."""
    when = _local(captured_at)
    return f"{ROOT}/{slug}/{when.strftime('%Y-%m-%d')}/{when.strftime('%H%M%S')}.jpg"


def render_key(slug: str, render_id: uuid.UUID) -> str:
    return f"{ROOT}/{slug}/renders/{render_id}.mp4"


def camera_prefix(slug: str) -> str:
    return f"{ROOT}/{slug}/"


# ── estimate ─────────────────────────────────────────────────────────────────

def _rule_minutes(rule: dict[str, Any]) -> int:
    start = schedule_svc._minutes(rule.get("start") or "00:00")
    end = schedule_svc._minutes(rule.get("end") or "24:00")
    return end - start if end > start else (1440 - start) + end


def active_minutes_per_day(schedule: Optional[dict[str, Any]]) -> float:
    """Average capture minutes per calendar day under a schedule."""
    if not schedule:
        return 1440.0
    per_week_days = 7.0 if schedule.get("mode") == "weekly" else 30.44
    total = sum(_rule_minutes(r) * len(r.get("days") or []) for r in schedule.get("rules") or [])
    return min(1440.0, total / per_week_days)


def estimate(cfg: TimelapseConfig) -> dict[str, Any]:
    """What switching this on will cost, before anyone switches it on."""
    frames_day = active_minutes_per_day(cfg.schedule) * 60.0 / cfg.interval_s
    kb = TYPICAL_KB[cfg.resolution] * (0.6 + 0.5 * (cfg.jpeg_quality - 40) / 55)
    mb_day = frames_day * kb / 1024.0
    total = min(mb_day * cfg.retention_days, float(cfg.max_storage_mb))
    days_until_cap = (cfg.max_storage_mb / mb_day) if mb_day > 0 else None
    return {"frames_per_day": round(frames_day), "mb_per_day": round(mb_day, 1),
            "mb_total": round(total), "cap_reached_after_days":
                round(days_until_cap) if days_until_cap is not None and days_until_cap < cfg.retention_days else None,
            "video_seconds_per_day": round(frames_day / cfg.render_fps, 1)}


# ── image processing (Pillow, run in a worker thread) ────────────────────────

@dataclass
class Processed:
    jpeg: bytes
    width: int
    height: int
    brightness: float
    thumb: bytes


def process(jpeg: bytes, *, masks: list, resolution: str, quality: int) -> Processed:
    """Masks → resize → measure → re-encode. Masks first and at native
    resolution, so a polygon drawn on the full frame lands where it was drawn."""
    from PIL import Image, ImageDraw, ImageFilter, ImageStat

    with Image.open(io.BytesIO(jpeg)) as src:
        im = src.convert("RGB")
    w, h = im.size
    if masks:
        stencil = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(stencil)
        for poly in masks:
            pts = [(float(x) * w, float(y) * h) for x, y in poly]
            if len(pts) >= 3:
                draw.polygon(pts, fill=255)
        # Heavy enough that a face or a plate is not recoverable at any zoom —
        # the same intent as the NVR's Gaussian blur over the stencil. A box blur
        # of the same radius on top removes the residual structure a single
        # Gaussian leaves at the edges of high-contrast text.
        radius = max(12, max(w, h) // 40)
        blurred = im.filter(ImageFilter.GaussianBlur(radius)).filter(ImageFilter.BoxBlur(radius))
        im = Image.composite(blurred, im, stencil)
    target = RESOLUTION_HEIGHT.get(resolution)
    if target and h > target:
        nw = max(2, int(round(w * target / h / 2)) * 2)
        im = im.resize((nw, target), Image.LANCZOS)
    gray = im.convert("L")
    brightness = float(ImageStat.Stat(gray).mean[0])
    thumb = gray.resize(_THUMB).tobytes()
    out = io.BytesIO()
    im.save(out, "JPEG", quality=int(quality), optimize=True)
    return Processed(out.getvalue(), im.size[0], im.size[1], brightness, thumb)


def scale_jpeg(data: bytes, width: int, quality: int = 78) -> bytes:
    """A smaller copy of a stored still (never larger). Masks are already burned
    into the stored JPEG, so a scaled copy is exactly as private as the original."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        if im.width <= width:
            return data
        h = max(1, round(im.height * width / im.width))
        out = io.BytesIO()
        im.convert("RGB").resize((width, h), Image.LANCZOS).save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()


def thumb_diff(a: bytes, b: bytes) -> float:
    """Mean absolute luma difference between two thumbnails (0–255)."""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# ── capture ──────────────────────────────────────────────────────────────────

_last_attempt: dict[uuid.UUID, float] = {}
_retry_after: dict[uuid.UUID, float] = {}
_prev_thumb: dict[uuid.UUID, bytes] = {}
_inflight: set[uuid.UUID] = set()


def due(now_ts: float, last_ts: Optional[float], interval_s: int) -> bool:
    """A still is due once a whole interval has passed since the last ATTEMPT
    (a skipped dark frame counts — otherwise a dark camera retries every tick)."""
    return last_ts is None or now_ts - last_ts >= interval_s - 0.5


async def _status(slug: str, result: str, detail: str = "") -> None:
    """Last outcome per camera in Valkey, so any replica's API can show why the
    newest still is old ("too dark", "PTZ being steered", "stream offline")."""
    try:
        from .. import redis_client
        r = await redis_client.get_redis()
        await r.set(f"timelapse:status:{slug}",
                    f"{result}|{int(time.time())}|{detail[:200]}", ex=86400 * 2)
    except Exception:                                          # noqa: BLE001 — status is best-effort
        pass


async def read_status(slug: str) -> Optional[dict[str, Any]]:
    try:
        from .. import redis_client
        r = await redis_client.get_redis()
        raw = await r.get(f"timelapse:status:{slug}")
    except Exception:                                          # noqa: BLE001
        return None
    if not raw:
        return None
    raw = raw.decode() if isinstance(raw, bytes) else str(raw)
    result, ts, detail = (raw.split("|", 2) + ["", ""])[:3]
    try:
        at = datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except ValueError:
        at = None
    return {"result": result, "at": at, "detail": detail}


async def _last_capture_ts(db: AsyncSession, camera_id: uuid.UUID) -> Optional[float]:
    from ..models import TimelapseFrame
    last = (await db.execute(select(func.max(TimelapseFrame.captured_at))
                             .where(TimelapseFrame.camera_id == camera_id))).scalar()
    return last.timestamp() if last else None


async def capture_tick(now: Optional[datetime] = None, sem: Optional[asyncio.Semaphore] = None) -> list[asyncio.Task]:
    """One pass: start a capture for every enabled camera that is due and inside
    its schedule. Returns the tasks it started (tests await them)."""
    from ..db import AsyncSessionLocal
    from ..models import Camera, CameraStage

    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    sem = sem or asyncio.Semaphore(settings.timelapse_capture_concurrency)
    started: list[asyncio.Task] = []
    async with AsyncSessionLocal() as db:
        await load_site(db)
        rows = (await db.execute(
            select(Camera.id, Camera.slug, Camera.name, Camera.timelapse_config, Camera.privacy_masks)
            .where(Camera.timelapse_enabled.is_(True), Camera.enabled.is_(True),
                   Camera.stage == CameraStage.REGISTERED.value))).all()
        for cam_id, slug, name, raw_cfg, masks in rows:
            if cam_id in _inflight or now_ts < _retry_after.get(cam_id, 0):
                continue
            cfg = effective_config(raw_cfg)
            if cam_id not in _last_attempt:
                # First sight since this replica became leader: resume the cadence
                # from what is stored, so a failover doesn't fire every camera at once.
                last = await _last_capture_ts(db, cam_id)
                if last is not None:
                    _last_attempt[cam_id] = last
            if not due(now_ts, _last_attempt.get(cam_id), cfg.interval_s):
                continue
            if not schedule_svc.is_active(cfg.schedule, _local(now)):
                continue
            _inflight.add(cam_id)
            started.append(asyncio.create_task(
                _capture_one(cam_id, slug, cfg, list(masks or []), now, sem), name=f"timelapse:{slug}"))
    return started


# ── PTZ: is the head at the preset? ──────────────────────────────────────────

#: Presets change rarely (an operator re-saving one); a camera's list is read
#: once per this many seconds, not on every still. A re-saved preset is picked
#: up within the TTL, and until then the worst case is a skipped still or two.
_PRESET_TTL_S = 600.0
_preset_cache: dict[str, tuple[float, list[dict]]] = {}


def _axis_delta(a: float, b: float, *, wraps: bool) -> float:
    d = abs(a - b)
    # Pan in ONVIF's generic space runs -1..1 around a full circle on a 360° head:
    # 0.99 and -0.99 are neighbours, not opposite ends. Tilt and zoom don't wrap.
    return min(d, 2.0 - d) if wraps and d > 1.0 else d


def preset_match(status: Optional[dict], preset: Optional[dict], tol: float) -> tuple[Optional[bool], str]:
    """(verdict, why) for "is the head at this preset right now?".

    True / False when it can be decided; None when it can't — the camera reports
    no position (several budget heads answer GetStatus with an empty Position) or
    the preset carries none. None is NOT a skip: the caller captures and says the
    preset was unverified, because a camera that can never report its position
    would otherwise never produce a single still, silently."""
    if not status or not preset:
        return None, "position not reported"
    moving = [str(status.get(k) or "").upper() for k in ("moving_pan_tilt", "moving_zoom")]
    if "MOVING" in moving:
        return False, "the camera is moving"
    checks = [("pan", True), ("tilt", False), ("zoom", False)]
    compared = 0
    for axis, wraps in checks:
        cur, want = status.get(axis), preset.get(axis)
        if cur is None or want is None:
            continue                     # zoom is often absent on fixed-lens PTZs — compare what both have
        compared += 1
        d = _axis_delta(float(cur), float(want), wraps=wraps)
        if d > tol:
            return False, f"{axis} is {d:.3f} off the preset"
    if compared == 0 or status.get("pan") is None or preset.get("pan") is None:
        return None, "position not reported"
    return True, "at the preset"


def find_preset(presets: list[dict], ref: str) -> Optional[dict]:
    for p in presets:
        if str(p.get("token") or "") == ref:
            return p
    for p in presets:                    # configs written before the picker held a typed name
        if str(p.get("name") or "").strip().lower() == ref.strip().lower():
            return p
    return None


async def _ptz_check(slug: str, ref: str, tol: float) -> tuple[Optional[bool], str]:
    """Read the head's position and the preset over ONVIF. Any ONVIF failure is
    "unknown" (None) — the stream still works, so the still is kept and marked
    unverified rather than the time-lapse going dark on a flaky SOAP endpoint."""
    from . import onvif_ptz, ptz_control
    try:
        target = await ptz_control._resolve_target(slug)    # noqa: SLF001 — the loop-safe resolver
        if target is None:
            return None, "no ONVIF credentials"
        ip, port, user, password, profile = target
        now = time.monotonic()
        hit = _preset_cache.get(slug)
        if hit is None or now - hit[0] > _PRESET_TTL_S:
            hit = (now, await onvif_ptz.get_presets(ip, port, user, password, profile))
            _preset_cache[slug] = hit
        preset = find_preset(hit[1], ref)
        if preset is None:
            return False, f"preset {ref!r} not found on the camera"
        status = await onvif_ptz.get_status(ip, port, user, password, profile)
    except asyncio.CancelledError:
        raise
    except Exception as exc:                                   # noqa: BLE001
        return None, f"ONVIF read failed: {str(exc)[:80]}"
    return preset_match(status, preset, tol)


async def _capture_one(cam_id: uuid.UUID, slug: str, cfg: TimelapseConfig, masks: list,
                       now: datetime, sem: asyncio.Semaphore) -> Optional[str]:
    """Grab → process → (skip?) → store. Returns the outcome (for tests and logs)."""
    from . import snapshot as snapshot_svc
    try:
        async with sem:
            if cfg.ptz_preset_only:
                from . import ptz_control
                if await ptz_control.current_holder(slug):
                    _last_attempt[cam_id] = now.timestamp()
                    await _status(slug, "ptz_steering", "an operator is moving the PTZ")
                    return "ptz_steering"
                at, why = await _ptz_check(slug, cfg.ptz_preset_only, cfg.ptz_tolerance)
                if at is False:
                    # Counted as an attempt: a patrol parks elsewhere for minutes, and
                    # retrying every tick would hammer the camera's SOAP endpoint.
                    _last_attempt[cam_id] = now.timestamp()
                    await _status(slug, "off_preset", why)
                    return "off_preset"
                unverified = at is None
            else:
                unverified, why = False, ""
            try:
                jpeg = await snapshot_svc.shared_snapshot(slug, wait_s=_GRAB_WAIT_S)
            except snapshot_svc.SnapshotBusy:
                return "busy"                                  # next tick tries again
            if not jpeg:
                _retry_after[cam_id] = now.timestamp() + _FAIL_BACKOFF_S
                await _status(slug, "offline", "no frame from the stream")
                return "offline"
            p = await asyncio.to_thread(process, jpeg, masks=masks, resolution=cfg.resolution,
                                        quality=cfg.jpeg_quality)
            _last_attempt[cam_id] = now.timestamp()
            if cfg.skip_dark and p.brightness < cfg.dark_threshold:
                await _status(slug, "dark", f"brightness {p.brightness:.0f} < {cfg.dark_threshold}")
                return "dark"
            prev = _prev_thumb.get(cam_id)
            _prev_thumb[cam_id] = p.thumb
            if cfg.skip_unchanged and prev is not None and thumb_diff(prev, p.thumb) < cfg.unchanged_threshold:
                await _status(slug, "unchanged", "the scene did not change")
                return "unchanged"
            await store_frame(cam_id, slug, now, p, cfg.retention_days)
            await _status(slug, "captured",
                          f"{p.width}x{p.height}" + (f" · preset not verified ({why})" if unverified else ""))
            return "captured"
    except asyncio.CancelledError:
        raise
    except Exception as exc:                                   # noqa: BLE001 — one camera never stops the loop
        _retry_after[cam_id] = now.timestamp() + _FAIL_BACKOFF_S
        log.warning("timelapse.capture_failed", slug=slug, error=str(exc)[:300])
        await _status(slug, "error", str(exc)[:200])
        return "error"
    finally:
        _inflight.discard(cam_id)


async def store_frame(cam_id: uuid.UUID, slug: str, captured_at: datetime, p: Processed,
                      retention_days: int) -> None:
    from ..db import AsyncSessionLocal
    from ..models import TimelapseFrame

    key, image = None, None
    if object_store.is_configured():
        key = frame_key(slug, captured_at)
        await asyncio.to_thread(object_store.put, key, p.jpeg, "image/jpeg")
    else:
        image = p.jpeg
    async with AsyncSessionLocal() as db:
        await db.execute(pg_insert(TimelapseFrame).values(
            camera_id=cam_id, captured_at=captured_at, local_day=_local(captured_at).date(),
            object_key=key, image=image, bytes=len(p.jpeg), width=p.width, height=p.height,
            brightness=round(p.brightness, 2),
            expires_at=captured_at + timedelta(days=retention_days),
        ).on_conflict_do_nothing(index_elements=["camera_id", "captured_at"]))
        await db.commit()


async def capture_loop() -> None:
    sem = asyncio.Semaphore(settings.timelapse_capture_concurrency)
    while True:
        try:
            if settings.timelapse_capture_enabled:
                await capture_tick(sem=sem)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001 — a failed pass retries next tick
            log.warning("timelapse.tick_failed", error=str(exc)[:300])
        await asyncio.sleep(settings.timelapse_tick_seconds)


def forget(cam_id: uuid.UUID) -> None:
    """Drop this replica's cadence memory for a camera (switched off, reconfigured)."""
    for d in (_last_attempt, _retry_after, _prev_thumb):
        d.pop(cam_id, None)


# ── reads ────────────────────────────────────────────────────────────────────

async def days(db: AsyncSession, camera_id: uuid.UUID, limit: int = 400) -> list[dict[str, Any]]:
    from ..models import TimelapseFrame
    rows = (await db.execute(
        select(TimelapseFrame.local_day, func.count(), func.coalesce(func.sum(TimelapseFrame.bytes), 0),
               func.min(TimelapseFrame.captured_at), func.max(TimelapseFrame.captured_at))
        .where(TimelapseFrame.camera_id == camera_id)
        .group_by(TimelapseFrame.local_day).order_by(TimelapseFrame.local_day.desc()).limit(limit))).all()
    return [{"day": d.isoformat(), "frames": int(n), "bytes": int(b),
             "first": f.isoformat() if f else None, "last": l.isoformat() if l else None}
            for d, n, b, f, l in rows]


async def frames_of_day(db: AsyncSession, camera_id: uuid.UUID, day: date) -> list[dict[str, Any]]:
    from ..models import TimelapseFrame
    rows = (await db.execute(
        select(TimelapseFrame.id, TimelapseFrame.captured_at, TimelapseFrame.width, TimelapseFrame.height,
               TimelapseFrame.brightness, TimelapseFrame.bytes)
        .where(TimelapseFrame.camera_id == camera_id, TimelapseFrame.local_day == day)
        .order_by(TimelapseFrame.captured_at.asc()))).all()
    return [{"id": i, "captured_at": c.isoformat(), "width": w, "height": h, "brightness": b, "bytes": n}
            for i, c, w, h, b, n in rows]


def _seconds_from_noon(dt: datetime) -> float:
    loc = _local(dt)
    return abs((loc.hour * 3600 + loc.minute * 60 + loc.second) - 12 * 3600)


async def daily(db: AsyncSession, camera_id: uuid.UUID, since: Optional[date] = None,
                limit: int = 400) -> list[dict[str, Any]]:
    """One still per day — the one nearest local noon (even light, no long
    shadows, the crew on site) — oldest first. This is what a progress strip or
    a cloud site-map replay wants: a 90-day build is 90 pictures, not
    13 000. Picked in Python over a narrow indexed scan (camera_id, local_day):
    "nearest noon" needs the appliance's zone, which SQL doesn't have (see
    TimelapseFrame.local_day), and 90 days × 144 stills is a few thousand
    small tuples."""
    from ..models import TimelapseFrame
    q = select(TimelapseFrame.local_day, TimelapseFrame.id, TimelapseFrame.captured_at,
               TimelapseFrame.width, TimelapseFrame.height).where(TimelapseFrame.camera_id == camera_id)
    if since is not None:
        q = q.where(TimelapseFrame.local_day >= since)
    rows = (await db.execute(q.order_by(TimelapseFrame.local_day.asc(), TimelapseFrame.captured_at.asc()))).all()
    best: dict[date, tuple] = {}
    for d, i, c, w, h in rows:
        cur = best.get(d)
        if cur is None or _seconds_from_noon(c) < _seconds_from_noon(cur[2]):
            best[d] = (d, i, c, w, h)
    out = [best[d] for d in sorted(best)][-limit:]
    return [{"day": d.isoformat(), "frame_id": i, "captured_at": c.isoformat(), "width": w, "height": h}
            for d, i, c, w, h in out]


async def totals(db: AsyncSession, camera_id: uuid.UUID) -> dict[str, Any]:
    from ..models import TimelapseFrame
    n, b, last = (await db.execute(
        select(func.count(), func.coalesce(func.sum(TimelapseFrame.bytes), 0), func.max(TimelapseFrame.captured_at))
        .where(TimelapseFrame.camera_id == camera_id))).one()
    return {"frames": int(n), "bytes": int(b), "last_captured_at": last.isoformat() if last else None}


async def frame_bytes(row) -> Optional[bytes]:
    if row.object_key:
        return await asyncio.to_thread(object_store.get, row.object_key)
    return bytes(row.image) if row.image is not None else None


# ── deletion: retention, caps, camera purge, subject erasure────────────────────

_PURGE_CHUNK = 500


async def _delete_frames(db: AsyncSession, rows: list) -> int:
    """Delete objects first, then rows: a row left pointing at a deleted object
    reads as a 404 and is retried by the next sweep; an object left with no row
    is unreachable storage forever."""
    from ..models import TimelapseFrame
    if not rows:
        return 0
    keys = [r.object_key for r in rows if r.object_key]
    if keys and object_store.is_configured():
        await asyncio.to_thread(object_store.delete_many, keys)
    await db.execute(delete(TimelapseFrame).where(TimelapseFrame.id.in_([r.id for r in rows])))
    await db.commit()
    return len(rows)


async def purge_expired(db: AsyncSession, now: Optional[datetime] = None) -> int:
    from ..models import TimelapseFrame
    now = now or datetime.now(timezone.utc)
    total = 0
    while True:
        rows = (await db.execute(select(TimelapseFrame.id, TimelapseFrame.object_key)
                                 .where(TimelapseFrame.expires_at < now).limit(_PURGE_CHUNK))).all()
        if not rows:
            return total
        total += await _delete_frames(db, rows)


async def enforce_caps(db: AsyncSession) -> int:
    """Oldest stills first, until each camera is back under its max_storage_mb."""
    from ..models import Camera, TimelapseFrame
    await load_site(db)
    per_cam = (await db.execute(
        select(TimelapseFrame.camera_id, func.sum(TimelapseFrame.bytes)).group_by(TimelapseFrame.camera_id))).all()
    removed = 0
    for cam_id, used in per_cam:
        raw = (await db.execute(select(Camera.timelapse_config).where(Camera.id == cam_id))).scalar()
        cap = effective_config(raw).max_storage_mb * 1024 * 1024
        over = int(used or 0) - cap
        while over > 0:
            rows = (await db.execute(select(TimelapseFrame.id, TimelapseFrame.object_key, TimelapseFrame.bytes)
                                     .where(TimelapseFrame.camera_id == cam_id)
                                     .order_by(TimelapseFrame.captured_at.asc()).limit(_PURGE_CHUNK))).all()
            if not rows:
                break
            batch, freed = [], 0
            for r in rows:
                batch.append(r)
                freed += r.bytes
                if freed >= over:
                    break
            removed += await _delete_frames(db, batch)
            over -= freed
    return removed


# ── disk guard ───────────────────────────────────────────────────────────────

def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def disk_path() -> str:
    p = settings.timelapse_disk_path
    return p if p and os.path.isdir(p) else "/"


async def disk_usage(db: AsyncSession) -> dict[str, Any]:
    """The stills' disk and what time-lapse holds on it, against the guard's limits.
    `over` + `reason` are what pauses capture and refuses switching it on."""
    from ..models import TimelapseFrame, TimelapseRender
    await load_site(db)
    try:
        du = await asyncio.to_thread(shutil.disk_usage, disk_path())
        total, free = int(du.total), int(du.free)
    except OSError as exc:
        log.warning("timelapse.disk_stat_failed", path=disk_path(), error=str(exc)[:200])
        total, free = 0, 0
    frames = int((await db.execute(select(func.coalesce(func.sum(TimelapseFrame.bytes), 0)))).scalar() or 0)
    renders = int((await db.execute(select(func.coalesce(func.sum(TimelapseRender.bytes), 0)))).scalar() or 0)
    g = guard_settings()
    used = frames + renders
    out: dict[str, Any] = {"total_bytes": total, "free_bytes": free, "timelapse_bytes": used, "frames_bytes": frames,
                           "renders_bytes": renders, **g, "over": False, "reason": ""}
    if total <= 0:
        return out                    # can't measure: never pause on a guess
    free_pct = 100.0 * free / total
    budget = total * g["budget_pct"] / 100.0
    out.update(free_pct=round(free_pct, 1), budget_bytes=int(budget))
    reasons = []
    if free_pct < g["floor_free_pct"]:
        reasons.append(f"the server disk is {100 - free_pct:.0f}% full, only {_gb(free)} free (time-lapse stops below "
                       f"{g['floor_free_pct']:g}% free so recordings are never deleted to make room for stills)")
    if used > budget:
        reasons.append(f"time-lapse is using {_gb(used)}, over its budget of {g['budget_pct']:g}% of the disk ({_gb(budget)})")
    if reasons:
        out.update(over=True, reason="; ".join(reasons))
    return out


async def guard_tick(db: AsyncSession, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Switch time-lapse off on every camera when the disk guard trips, and leave a
    remark saying when and why. Never deletes a still; never switches back on by
    itself (a site hovering at the limit would flap) — someone does, on the
    Time-lapse page, once there's room. Returns the pause record when it paused."""
    from ..models import Camera
    usage = await disk_usage(db)
    if not usage["over"]:
        return None
    cams = (await db.execute(select(Camera).where(Camera.timelapse_enabled.is_(True)))).scalars().all()
    if not cams:
        return None
    now = now or datetime.now(timezone.utc)
    row = await site_row(db)
    before = dict(row.paused or {})
    ids = list(dict.fromkeys([*(before.get("camera_ids") or []), *[str(c.id) for c in cams]]))
    pause = {"at": now.isoformat(), "reason": usage["reason"], "camera_ids": ids,
             "disk": {k: usage.get(k) for k in ("total_bytes", "free_bytes", "timelapse_bytes", "free_pct")}}
    for c in cams:
        c.timelapse_enabled = False
    row.paused = pause
    row.updated_at = now
    await db.commit()
    _site_from_row(row)
    for c in cams:
        forget(c.id)
    log.warning("timelapse.disk_guard_paused", cameras=len(cams), reason=usage["reason"])
    try:
        from . import audit as audit_svc
        await audit_svc.record(None, None, "timelapse.auto_paused", target="timelapse",
                               detail={"cameras": [c.slug for c in cams], "reason": usage["reason"]})
    except Exception as exc:                                   # noqa: BLE001 — the pause stands without its audit row
        log.warning("timelapse.disk_guard_audit_failed", error=str(exc)[:200])
    return pause


async def purge_expired_renders(db: AsyncSession, now: Optional[datetime] = None) -> int:
    from ..models import TimelapseRender
    now = now or datetime.now(timezone.utc)
    rows = (await db.execute(select(TimelapseRender.id, TimelapseRender.object_key)
                             .where(TimelapseRender.expires_at < now,
                                    TimelapseRender.status.in_(("done", "failed"))))).all()
    keys = [r.object_key for r in rows if r.object_key]
    if keys and object_store.is_configured():
        await asyncio.to_thread(object_store.delete_many, keys)
    if rows:
        await db.execute(delete(TimelapseRender).where(TimelapseRender.id.in_([r.id for r in rows])))
        await db.commit()
    return len(rows)


async def retention_loop() -> None:
    from ..db import AsyncSessionLocal
    while True:
        try:
            async with AsyncSessionLocal() as db:
                n = await purge_expired(db)
                c = await enforce_caps(db)
                r = await purge_expired_renders(db)
                # After the sweeps, so space they just freed counts.
                await guard_tick(db)
            if n or c or r:
                log.info("timelapse.retention.purged", expired=n, over_cap=c, renders=r)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001 — a failed sweep retries next period
            log.warning("timelapse.retention.failed", error=str(exc)[:300])
        await asyncio.sleep(300)


async def purge_camera(slug: str) -> None:
    """A deleted camera's stills and videos. Rows go with the camera
    (ON DELETE CASCADE); the objects don't, and nothing could reach them after."""
    if not slug or not object_store.is_configured():
        return
    try:
        keys = await asyncio.to_thread(object_store.list_keys, camera_prefix(slug), 1_000_000)
        await asyncio.to_thread(object_store.delete_many, keys)
    except Exception as exc:                                   # noqa: BLE001 — logged; the camera delete stands
        log.warning("timelapse.purge_camera_failed", slug=slug, error=str(exc)[:300])


async def erase_range(db: AsyncSession, camera_ref: Any, from_dt: datetime, to_dt: datetime) -> Optional[int]:
    """Subject erasure: every still captured in the window, and every video whose
    range overlaps it (a video IS the stills). None on failure, so the erasure
    record can say "could not erase" rather than "nothing to erase"."""
    from ..models import Camera, TimelapseFrame, TimelapseRender
    try:
        cam = None
        try:
            cam = await db.get(Camera, uuid.UUID(str(camera_ref)))
        except (ValueError, TypeError):
            pass
        if cam is None:
            cam = (await db.execute(select(Camera).where(Camera.slug == str(camera_ref)))).scalar_one_or_none()
        if cam is None:
            return 0
        total = 0
        while True:
            rows = (await db.execute(select(TimelapseFrame.id, TimelapseFrame.object_key)
                                     .where(TimelapseFrame.camera_id == cam.id,
                                            TimelapseFrame.captured_at >= from_dt,
                                            TimelapseFrame.captured_at <= to_dt).limit(_PURGE_CHUNK))).all()
            if not rows:
                break
            total += await _delete_frames(db, rows)
        renders = (await db.execute(select(TimelapseRender.id, TimelapseRender.object_key)
                                    .where(TimelapseRender.camera_id == cam.id,
                                           TimelapseRender.range_start <= to_dt,
                                           TimelapseRender.range_end >= from_dt))).all()
        keys = [r.object_key for r in renders if r.object_key]
        if keys and object_store.is_configured():
            await asyncio.to_thread(object_store.delete_many, keys)
        if renders:
            await db.execute(delete(TimelapseRender).where(TimelapseRender.id.in_([r.id for r in renders])))
            await db.commit()
        return total + len(renders)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("timelapse.erase_failed", camera=str(camera_ref), error=str(exc)[:300])
        return None


# ── rendering ────────────────────────────────────────────────────────────────

class Rejected(Exception):
    """A render request that can't be queued; the message is for the operator."""


_MAX_QUEUE = 20
_MAX_RANGE = timedelta(days=400)


async def enqueue_render(db: AsyncSession, camera, *, start: datetime, end: datetime, fps: int,
                         overlay: bool, deflicker: bool, kind: str = "manual",
                         requested_by: Optional[str] = None):
    from ..models import TimelapseFrame, TimelapseRender
    if end <= start:
        raise Rejected("The end must be after the start")
    if end - start > _MAX_RANGE:
        raise Rejected("A render can cover at most 400 days")
    if fps not in (24, 30):
        raise Rejected("fps must be 24 or 30")
    n = (await db.execute(select(func.count()).select_from(TimelapseFrame).where(
        TimelapseFrame.camera_id == camera.id, TimelapseFrame.captured_at >= start,
        TimelapseFrame.captured_at <= end))).scalar() or 0
    if n < 2:
        raise Rejected("There are fewer than 2 stills in that range")
    pending = (await db.execute(select(func.count()).select_from(TimelapseRender)
                                .where(TimelapseRender.status.in_(("queued", "running"))))).scalar() or 0
    if pending >= _MAX_QUEUE:
        raise Rejected("The render queue is full — try again when the current renders finish")
    row = TimelapseRender(id=uuid.uuid4(), camera_id=camera.id, kind=kind, range_start=start, range_end=end,
                          fps=fps, overlay=overlay, deflicker=deflicker, status="queued", frames=int(n),
                          requested_by=requested_by,
                          expires_at=datetime.now(timezone.utc) + timedelta(days=settings.timelapse_render_retention_days))
    db.add(row)
    await db.commit()
    return row


def encoder_args(nvenc: bool) -> list[str]:
    """H.264 for every browser. Same encoder pair as the NVR's clip export
    (services/nvr/api/encoders.py): NVENC when asked and present, libx264 otherwise."""
    if nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]


def render_command(frames_dir: str, out_path: str, *, fps: int, deflicker: bool, nvenc: bool) -> list[str]:
    """ffmpeg: numbered JPEGs → MP4. `format=yuv420p` because a 4:4:4 or 4:2:2
    H.264 file plays in nothing a client opens; `+faststart` so a browser can
    start playing before the whole file has arrived."""
    vf = ("deflicker=size=5:mode=am," if deflicker else "") + "format=yuv420p"
    return ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-framerate", str(int(fps)), "-i", os.path.join(frames_dir, "%06d.jpg"),
            "-vf", vf, *encoder_args(nvenc), "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out_path]


def sample_evenly(items: list, limit: int) -> list:
    """At most `limit` items, evenly spread, first and last always kept — a too-long
    range becomes a faster video rather than a refused one."""
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)]


def _prepare_frame(data: bytes, out_path: str, size: tuple[int, int], label: Optional[str]) -> None:
    from PIL import Image, ImageDraw, ImageFont
    with Image.open(io.BytesIO(data)) as src:
        im = src.convert("RGB")
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    if label:
        draw = ImageDraw.Draw(im)
        try:
            font = ImageFont.load_default(size=max(14, size[1] // 30))
        except TypeError:                                      # Pillow < 10.1: fixed-size bitmap font
            font = ImageFont.load_default()
        x, y = 12, size[1] - max(14, size[1] // 30) - 18
        bbox = draw.textbbox((x, y), label, font=font)
        draw.rectangle((bbox[0] - 6, bbox[1] - 4, bbox[2] + 6, bbox[3] + 4), fill=(0, 0, 0))
        draw.text((x, y), label, fill=(255, 255, 255), font=font)
    im.save(out_path, "JPEG", quality=90)


async def _set(render_id: uuid.UUID, **values) -> None:
    from ..db import AsyncSessionLocal
    from ..models import TimelapseRender
    async with AsyncSessionLocal() as db:
        await db.execute(update(TimelapseRender).where(TimelapseRender.id == render_id).values(**values))
        await db.commit()


async def _run_ffmpeg(cmd: list[str], render_id: uuid.UUID, total: int) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    last_write = 0.0
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="ignore").strip()
        if line.startswith("frame=") and total:
            try:
                done = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if time.monotonic() - last_write > 2:
                last_write = time.monotonic()
                await _set(render_id, progress=round(0.5 + 0.5 * min(1.0, done / total), 3))
    err = (await proc.stderr.read()).decode(errors="ignore") if proc.stderr else ""
    rc = await proc.wait()
    return rc == 0, err[-500:]


async def run_render(render_id: uuid.UUID) -> str:
    """Render one queued video. Returns the final status."""
    from ..db import AsyncSessionLocal
    from ..models import Camera, TimelapseFrame, TimelapseRender

    async with AsyncSessionLocal() as db:
        r = await db.get(TimelapseRender, render_id)
        if r is None or r.status != "queued":
            return r.status if r else "missing"
        cam = await db.get(Camera, r.camera_id)
        rows = (await db.execute(select(TimelapseFrame).where(
            TimelapseFrame.camera_id == r.camera_id, TimelapseFrame.captured_at >= r.range_start,
            TimelapseFrame.captured_at <= r.range_end).order_by(TimelapseFrame.captured_at.asc()))).scalars().all()
        r.status, r.progress = "running", 0.0
        await db.commit()
        fps, overlay, deflicker = r.fps, r.overlay, r.deflicker
    rows = sample_evenly(list(rows), settings.timelapse_max_render_frames)
    if cam is None or len(rows) < 2:
        await _set(render_id, status="failed", error="Not enough stills left in this range",
                   finished_at=datetime.now(timezone.utc))
        return "failed"

    workdir = tempfile.mkdtemp(prefix="timelapse-")
    try:
        # Every frame at ONE size (the first frame's, made even): a resolution change
        # mid-project would otherwise stop the encode at the first odd frame.
        first = rows[0]
        size = (max(2, (first.width or 1280) // 2 * 2), max(2, (first.height or 720) // 2 * 2))
        written = 0
        for i, row in enumerate(rows):
            data = await frame_bytes(row)
            if not data:
                continue                                      # aged out mid-render: skip, don't fail
            label = (f"{cam.name or cam.slug} · {_local(row.captured_at).strftime('%Y-%m-%d %H:%M')}"
                     if overlay else None)
            await asyncio.to_thread(_prepare_frame, data, os.path.join(workdir, f"{written:06d}.jpg"), size, label)
            written += 1
            if i % 50 == 0:
                await _set(render_id, progress=round(0.5 * i / len(rows), 3))
        if written < 2:
            raise RuntimeError("the stills in this range are no longer available")
        out = os.path.join(workdir, "out.mp4")
        encoder = "h264_nvenc" if settings.timelapse_nvenc else "libx264"
        ok, err = await _run_ffmpeg(render_command(workdir, out, fps=fps, deflicker=deflicker,
                                                   nvenc=settings.timelapse_nvenc), render_id, written)
        if not ok and settings.timelapse_nvenc:
            # Same per-job fallback as NVR clip export: a GPU hiccup costs CPU, not the render.
            encoder = "libx264"
            ok, err = await _run_ffmpeg(render_command(workdir, out, fps=fps, deflicker=deflicker, nvenc=False),
                                        render_id, written)
        if not ok:
            raise RuntimeError(f"ffmpeg failed: {err.strip()[:300] or 'no output'}")
        with open(out, "rb") as fh:
            video = fh.read()
        key, inline = None, None
        if object_store.is_configured():
            key = render_key(cam.slug, render_id)
            await asyncio.to_thread(object_store.put, key, video, "video/mp4")
        else:
            inline = video
        await _set(render_id, status="done", progress=1.0, frames=written, object_key=key, video=inline,
                   bytes=len(video), encoder=encoder, finished_at=datetime.now(timezone.utc), error=None)
        log.info("timelapse.render_done", render=str(render_id), frames=written, bytes=len(video))
        return "done"
    except asyncio.CancelledError:
        await _set(render_id, status="queued", progress=0.0)    # resumed by the next leader
        raise
    except Exception as exc:                                   # noqa: BLE001
        await _set(render_id, status="failed", error=str(exc)[:500], finished_at=datetime.now(timezone.utc))
        log.warning("timelapse.render_failed", render=str(render_id), error=str(exc)[:300])
        return "failed"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def enqueue_daily(now: Optional[datetime] = None) -> int:
    """Yesterday's video for every camera that asked for one, once, after 00:20
    local (so the last still of the day has certainly landed)."""
    from ..db import AsyncSessionLocal
    from ..models import Camera, TimelapseRender

    now = now or datetime.now(timezone.utc)
    local = _local(now)
    if local.hour == 0 and local.minute < 20:
        return 0
    today0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    y0, y1 = today0 - timedelta(days=1), today0 - timedelta(microseconds=1)
    made = 0
    async with AsyncSessionLocal() as db:
        await load_site(db)
        cams = (await db.execute(select(Camera).where(Camera.timelapse_enabled.is_(True)))).scalars().all()
        for cam in cams:
            cfg = effective_config(cam.timelapse_config)
            if not cfg.auto_render_daily:
                continue
            exists = (await db.execute(select(TimelapseRender.id).where(
                TimelapseRender.camera_id == cam.id, TimelapseRender.kind == "daily",
                TimelapseRender.range_start == y0))).first()
            if exists:
                continue
            try:
                await enqueue_render(db, cam, start=y0, end=y1, fps=cfg.render_fps, overlay=cfg.render_overlay,
                                     deflicker=cfg.render_deflicker, kind="daily", requested_by="schedule")
                made += 1
            except Rejected:
                continue                                       # fewer than 2 stills yesterday: nothing to make
    return made


async def render_loop() -> None:
    from ..db import AsyncSessionLocal
    from ..models import TimelapseRender

    # A render left `running` by a replica that died is queued again: the leader
    # lease moved, the half-written temp files went with the old container.
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(update(TimelapseRender).where(TimelapseRender.status == "running")
                             .values(status="queued", progress=0.0))
            await db.commit()
    except Exception as exc:                                   # noqa: BLE001
        log.warning("timelapse.render_requeue_failed", error=str(exc)[:300])
    last_daily = 0.0
    while True:
        try:
            if time.monotonic() - last_daily > 300:
                last_daily = time.monotonic()
                await enqueue_daily()
            async with AsyncSessionLocal() as db:
                nxt = (await db.execute(select(TimelapseRender.id).where(TimelapseRender.status == "queued")
                                        .order_by(TimelapseRender.created_at.asc()).limit(1))).scalar()
            if nxt is not None:
                await run_render(nxt)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001
            log.warning("timelapse.render_loop_failed", error=str(exc)[:300])
        await asyncio.sleep(5)


async def video_bytes(row) -> Optional[bytes]:
    if row.object_key:
        return await asyncio.to_thread(object_store.get, row.object_key)
    return bytes(row.video) if row.video is not None else None


def render_out(r) -> dict[str, Any]:
    return {"id": str(r.id), "camera_id": str(r.camera_id), "kind": r.kind,
            "range_start": r.range_start.isoformat(), "range_end": r.range_end.isoformat(),
            "fps": r.fps, "overlay": r.overlay, "deflicker": r.deflicker, "status": r.status,
            "progress": r.progress, "frames": r.frames, "bytes": r.bytes, "encoder": r.encoder,
            "error": r.error, "requested_by": r.requested_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "duration_s": round(r.frames / r.fps, 1) if r.fps else None}
