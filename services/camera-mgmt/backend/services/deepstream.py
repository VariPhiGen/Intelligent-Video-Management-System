"""DeepStream pipeline adapter.

Translates the VMS's own analytics contract (``_analytics_contract`` /
``GET /analytics/config``, which stays exactly as authored — it is the
source of truth, not this module) into the envelope the customer's
existing DeepStream pipeline expects. Everything here is derived on the
fly from ``Camera.analytics_config``; nothing is stored.

One faithfulness gap, called out explicitly because it is the single place
this adapter cannot match the pipeline's sample config: a VMS **tripwire**
region is a straight line — exactly 2 points plus a crossing ``direction``
(``a2b``/``b2a``/``both``). The pipeline's sample envelope has no line
primitive; its ``zones`` are all closed polygons. This adapter emits a
tripwire's 2 points under ``zones`` exactly like any other shape (the
pipeline must either accept a 2-point "zone" as a line, or expose a real
line primitive of its own) and additionally surfaces the crossing
direction via ``parameters.tripwire_directions`` (``{zone_key: "a2b"}``)
so the information isn't silently dropped — but the shape alone still
can't express directionality on its own. A ``null`` direction (not set by
the operator) is omitted from that map entirely rather than emitted as a
literal ``null``.

Schedule defaults are intentionally asymmetric and that asymmetry is
preserved on purpose: a **missing** `active_hours` key means "all day"
(``00:00:00``–``23:59:59``), and a **missing** `active_days` key means
"all seven days" — but an *explicitly empty* `active_days: []` is emitted
verbatim as `[]`, because the CMM UI genuinely lets an operator untick
every day, and inventing seven days there would invert their intent. The
two cases (key absent vs. key present-but-empty) are deliberately not
collapsed.

Coordinate scaling note: normalized `1.0` maps to `width`/`height`
themselves (one past the last valid pixel index), not `width - 1`. This
matches the customer's own sample envelope, which represents a full-frame
zone on a 1080-wide frame as
``[[0,0],[1080,0],[1080,720],[0,720]]`` — i.e. the far edge is the frame
dimension, not dimension-minus-one. Do not "fix" this later.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from ..config import settings
from ..models import Camera

log = structlog.get_logger(__name__)

# Canonical Mon–Sun order; stored `active_days` may be in any order/case.
_DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Keys the pipeline's own envelope owns; a catalog detector param using
# either name would silently clobber what this adapter emits.
_RESERVED_PARAM_KEYS = ("time_window", "tripwire_directions")

# The one activity whose zones are line-crossing definitions rather than
# areas. The pipeline feeds them to nvdsanalytics (which counts crossings in
# hardware) instead of to the activity's own per-frame code, so its zones take
# a different shape — see _lane_zone.
_LINE_CROSSING_ACTIVITY = "entry_exit_WLE_logs"

# How far the synthesized direction segment extends either side of the line,
# in normalized units. Only its orientation is read, never its length.
_DIRECTION_LEN = 0.05

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


def _parse_time(value: Any, *, default: str) -> str:
    """Parse a stored time-of-day value into a strict `HH:MM:SS` string.

    Accepts `H:MM`, `HH:MM`, `H:MM:SS`, `HH:MM:SS`. Never raises: anything
    missing or unparseable falls back to `default` (the caller supplies the
    correct all-day bound — `00:00:00` for start, `23:59:59` for end)."""
    if isinstance(value, str):
        m = _TIME_RE.match(value.strip())
        if m:
            hour, minute, second = m.group(1), m.group(2), m.group(3) or "00"
            return f"{int(hour):02d}:{minute}:{second}"
    return default


def _time_window(params: dict[str, Any], *, slug: str, activity_type: str) -> dict[str, Any]:
    """Derive the pipeline's universal `time_window` from the built-in
    schedule keys (`active_hours` / `active_days`).

    A missing/null `active_hours` means "all day". A missing `active_days`
    key means "all seven days"; an explicitly-empty list stays `[]`. Both
    times are parsed defensively — a malformed value never raises, it just
    falls back to that bound's all-day default and logs a warning so the
    substitution is visible rather than silently reinterpreted."""
    active_hours = params.get("active_hours")
    if active_hours:
        start_raw = active_hours.get("start") if isinstance(active_hours, dict) else None
        end_raw = active_hours.get("end") if isinstance(active_hours, dict) else None
        start = _parse_time(start_raw, default="00:00:00")
        end = _parse_time(end_raw, default="23:59:59")
        if not (isinstance(start_raw, str) and _TIME_RE.match(start_raw.strip() or "")):
            log.warning(
                "deepstream.active_hours.invalid_start",
                slug=slug, activity_type=activity_type, value=start_raw,
            )
        if not (isinstance(end_raw, str) and _TIME_RE.match(end_raw.strip() or "")):
            log.warning(
                "deepstream.active_hours.invalid_end",
                slug=slug, activity_type=activity_type, value=end_raw,
            )
    else:
        start, end = "00:00:00", "23:59:59"

    if "active_days" in params:
        stored_days = params.get("active_days") or []
        stored_lower = {str(d).strip().lower()[:3] for d in stored_days}
        days = [d.lower() for d in _DAY_ORDER if d.lower()[:3] in stored_lower]
    else:
        days = [d.lower() for d in _DAY_ORDER]

    return {"start": start, "end": end, "days": days}


def _lane_zone(points: list, direction: str | None) -> dict | None:
    """Turn a 2-point tripwire into the pipeline's `lanes` line-crossing pair.

    nvdsanalytics needs two segments per crossing line: the line itself, and a
    second segment whose orientation says which way across it counts as
    "Entry". The VMS stores only the line (A→B) plus a direction enum, so the
    second segment is synthesized here as the line's normal through its
    midpoint, flipped for `b2a`.

    **The A→B ↔ normal convention is a choice, not a derivation.** Nothing in
    the VMS defines which side of a tripwire is "in" — the operator draws a
    line and picks A→B or B→A, with no geometric cue in the UI. So the two
    settings are mirror images by construction: if the counts come out
    swapped on site, switching the direction dropdown is the whole fix. That
    is also why this is worth emitting at all rather than waiting for a
    spec — the failure mode is a one-click correction, whereas emitting
    nothing (the previous behaviour) means entry/exit counting silently never
    runs.

    `both` maps to the same geometry as `a2b`: the pipeline always writes an
    Entry *and* an Exit counter for a line, so both directions are counted
    either way, and the enum only decides which one is labelled Entry.

    Returns None for anything that isn't a usable 2-point line.
    """
    if not isinstance(points, (list, tuple)) or len(points) != 2:
        return None
    try:
        (ax, ay), (bx, by) = (float(points[0][0]), float(points[0][1])), \
                             (float(points[1][0]), float(points[1][1]))
    except (TypeError, ValueError, IndexError):
        return None

    dx, dy = bx - ax, by - ay
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return None  # degenerate: both endpoints identical, no normal exists

    # Unit normal to A→B. Computed in normalized space, so it is not exactly
    # perpendicular once the frame's aspect ratio is applied — harmless,
    # because nvdsanalytics only takes the sign of this vector against the
    # line, and no aspect ratio flips that sign for a non-degenerate line.
    nx, ny = dy / length, -dx / length
    if direction == "b2a":
        nx, ny = -nx, -ny

    mx, my = (ax + bx) / 2, (ay + by) / 2
    return {
        "lanes": {
            # L1 is the direction segment, L2 the line itself — the order the
            # pipeline's _build_nvds_line_strings expects.
            "L1": [[mx - nx * _DIRECTION_LEN, my - ny * _DIRECTION_LEN],
                   [mx + nx * _DIRECTION_LEN, my + ny * _DIRECTION_LEN]],
            "L2": [[ax, ay], [bx, by]],
        }
    }


def deepstream_config(
    camera: Camera, *, width: int | None = None, height: int | None = None
) -> dict:
    """Emit the DeepStream pipeline envelope for one camera.

    Region points are normalized (0–1, as stored) when `width`/`height`
    are omitted; scaled to integer pixel coordinates
    (`round(x * width)`, `round(y * height)`) when both are given — note
    normalized 1.0 lands on `width`/`height` themselves, matching the
    pipeline's own sample (see module docstring). The caller is
    responsible for rejecting `coords=pixels` requests that omit either
    dimension — this function just does whichever conversion its
    arguments imply.

    Defensive against malformed stored config, since it reads storage
    directly rather than through the validated write path: an activity
    with no `type`, or a region id an activity references that no longer
    exists in `regions`, is skipped rather than raising — zone numbering
    closes up over any skipped region so there are no gaps.
    """
    cfg = camera.analytics_config or {}
    regions: dict[str, dict] = cfg.get("regions", {})
    activities: list[dict] = cfg.get("activities", [])
    slug = camera.slug or ""

    def _points(region_id: str) -> list[list[float]] | list[list[int]]:
        pts = regions.get(region_id, {}).get("points", [])
        if width is None or height is None:
            return pts
        return [[round(x * width), round(y * height)] for x, y in pts]

    activities_data: dict[str, Any] = {}
    for act in activities:
        act_type = act.get("type")
        if not act_type:
            log.warning("deepstream.activity.missing_type", slug=slug)
            continue

        zones: dict[str, Any] = {}
        tripwire_directions: dict[str, str] = {}
        zone_num = 0
        for rid in act.get("regions", []):
            if rid not in regions:
                log.warning(
                    "deepstream.activity.unknown_region",
                    slug=slug, activity_type=act_type, region_id=rid,
                )
                continue
            region = regions[rid]
            is_tripwire = region.get("kind") == "tripwire"

            if act_type == _LINE_CROSSING_ACTIVITY:
                # This activity's zones are consumed by the pipeline's
                # nvdsanalytics generator, which understands only `lanes`.
                # A polygon here has no line to cross, so it would be silently
                # dropped downstream — say so rather than emit a shape that
                # cannot work.
                lane = _lane_zone(region.get("points", []), region.get("direction")) \
                    if is_tripwire else None
                if lane is not None and width is not None and height is not None:
                    lane = {"lanes": {
                        name: [[round(x * width), round(y * height)] for x, y in seg]
                        for name, seg in lane["lanes"].items()
                    }}
                if lane is None:
                    log.warning(
                        "deepstream.activity.not_a_tripwire",
                        slug=slug, activity_type=act_type, region_id=rid,
                        kind=region.get("kind"),
                        detail="entry/exit counting needs a 2-point tripwire",
                    )
                    continue
                zone_num += 1
                zones[f"zone{zone_num}"] = lane
                if region.get("direction") is not None:
                    tripwire_directions[f"zone{zone_num}"] = region["direction"]
                continue

            zone_num += 1
            zone_key = f"zone{zone_num}"
            zones[zone_key] = _points(rid)
            if is_tripwire and region.get("direction") is not None:
                tripwire_directions[zone_key] = region["direction"]

        params = act.get("params", {}) or {}
        detector_params = {
            k: v for k, v in params.items() if k not in ("active_hours", "active_days")
        }
        clobbered = [k for k in _RESERVED_PARAM_KEYS if k in detector_params]
        if clobbered:
            log.warning(
                "deepstream.params.reserved_key_stripped",
                slug=slug, activity_type=act_type, keys=clobbered,
            )
            detector_params = {k: v for k, v in detector_params.items() if k not in _RESERVED_PARAM_KEYS}

        parameters: dict[str, Any] = {
            **detector_params,
            "time_window": _time_window(params, slug=slug, activity_type=act_type),
        }
        if tripwire_directions:
            parameters["tripwire_directions"] = tripwire_directions

        activities_data[act_type] = {"zones": zones, "parameters": parameters}

    return {
        "sensor_id": camera.slug,
        "media_name": camera.name,
        "type": "rtsp",
        "url": settings.local_rtsp_url(camera.slug),
        "enable": "1" if camera.enabled else "0",
        # Declares which space the zone coordinates are in, so the consumer
        # never has to guess from their magnitude. The pipeline scales
        # `normalized` against its own muxer resolution on load; hand-authored
        # configs carry no such key and are read as pixels, as they always were.
        "coords": "pixels" if (width is not None and height is not None) else "normalized",
        "active_activities": list(activities_data),
        "activities_data": activities_data,
    }
