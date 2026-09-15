"""entry_exit.py — Entry / Exit analytics: tripwire crossings counted over time.

The analytics engine's `entry_exit_WLE_logs` activity stores one event per
completed crossing, with `attributes.direction` "entry" or "exit" and
`attributes.tripwire_id` the tripwire's region id. Those individual records stay
in `analytics_events` as they are; this module answers the Events page's Entry /
Exit graph from them: how many of each, per time bucket, for one camera over a
chosen range. The counting happens in the database, so a 24-hour graph never
ships its individual records to the browser.

Earlier Entry / Exit records — "a person touched the tripwire" — carry no
direction and are never counted: which way those people went was not recorded.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVITY = "entry_exit_WLE_logs"
DIRECTIONS = ("entry", "exit")

#: The graph's ranges in minutes → bucket length in seconds: 24–30 buckets each,
#: and every range a whole number of buckets.
RANGES: dict[int, int] = {5: 10, 15: 30, 30: 60, 60: 120, 120: 300, 360: 900, 720: 1800, 1440: 3600}
DEFAULT_MINUTES = 60

#: Tripwire directions that say which way is Entry (see the activity's module).
ORIENTED = ("a2b", "b2a")


#: The furthest any clock is from UTC (UTC−12 … UTC+14), in minutes.
MAX_OFFSET_MINUTES = 14 * 60


@dataclass(frozen=True)
class Window:
    start: datetime     # inclusive
    end: datetime       # exclusive
    bucket_s: int
    minutes: int
    #: Seconds east of UTC of the clock whose round times the buckets start on.
    offset_s: int = 0

    @property
    def buckets(self) -> int:
        return self.minutes * 60 // self.bucket_s

    @property
    def first_slot(self) -> int:
        """The first bucket's number, as the count query numbers buckets."""
        return (int(self.start.timestamp()) + self.offset_s) // self.bucket_s


def window(minutes: int, now: datetime, offset_minutes: int = 0) -> Window:
    """The graph's range: `minutes` long, ending on the first bucket boundary at or after `now`.

    Boundaries are round times of the clock `offset_minutes` east of UTC — the
    viewer's — so in UTC+5:30 an hour's bucket runs 10:00–11:00 on the wall, not
    10:30–11:30.
    """
    bucket = RANGES[minutes]
    offset = offset_minutes * 60
    end = math.ceil((now.timestamp() + offset) / bucket) * bucket - offset
    start = end - minutes * 60
    return Window(datetime.fromtimestamp(start, tz=timezone.utc),
                  datetime.fromtimestamp(end, tz=timezone.utc), bucket, minutes, offset)


def tripwires(camera: Any) -> list[dict]:
    """The tripwires a camera's Entry / Exit activity watches, in configured order,
    each with whether it has an entry direction (one without is not counted)."""
    cfg = getattr(camera, "analytics_config", None) or {}
    if not isinstance(cfg, dict):
        return []
    regions = cfg.get("regions") or {}
    activity = next((a for a in cfg.get("activities") or []
                     if isinstance(a, dict) and a.get("type") == ACTIVITY), None)
    out = []
    for rid in (activity or {}).get("regions") or []:
        region = regions.get(rid)
        if isinstance(region, dict) and region.get("kind") == "tripwire":
            direction = region.get("direction")
            out.append({"id": rid, "name": region.get("name") or rid,
                        "direction": direction, "oriented": direction in ORIENTED})
    return out


_COUNTS = (
    "SELECT floor((extract(epoch FROM started_at) + CAST(:offset AS integer)) "
    "/ CAST(:bucket AS integer))::bigint AS slot, "
    "attributes->>'direction' AS direction, count(*) AS n "
    "FROM analytics_events "
    "WHERE camera_id = :camera_id AND activity = :activity "
    "AND started_at >= :start AND started_at < :end AND expires_at > now() "
    "AND attributes->>'direction' IN ('entry', 'exit')"
)


async def counts(db: AsyncSession, camera_id: uuid.UUID, win: Window,
                 tripwire: Optional[str] = None) -> list[tuple[int, str, int]]:
    """(bucket slot, direction, records) for one camera's Entry / Exit crossings in the window."""
    sql = _COUNTS + (" AND attributes->>'tripwire_id' = :tripwire" if tripwire else "") + \
        " GROUP BY slot, direction"
    params = {"bucket": win.bucket_s, "offset": win.offset_s, "camera_id": camera_id, "activity": ACTIVITY,
              "start": win.start, "end": win.end}
    if tripwire:
        params["tripwire"] = tripwire
    rows = (await db.execute(text(sql), params)).all()
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def series(rows: Iterable[tuple[int, str, int]], win: Window) -> dict:
    """Every bucket of the window with its Entry and Exit counts, and the totals —
    the sum of the buckets, so the graph and its totals can never disagree."""
    first = win.first_slot
    buckets = [{"start": _iso((first + i) * win.bucket_s - win.offset_s), "entry": 0, "exit": 0}
               for i in range(win.buckets)]
    for slot, direction, n in rows:
        i = slot - first
        if 0 <= i < len(buckets) and direction in DIRECTIONS:
            buckets[i][direction] += n
    return {
        "minutes": win.minutes,
        "bucket_seconds": win.bucket_s,
        "tz_offset": win.offset_s // 60,
        "start": _iso(int(win.start.timestamp())),
        "end": _iso(int(win.end.timestamp())),
        "totals": {d: sum(b[d] for b in buckets) for d in DIRECTIONS},
        "buckets": buckets,
    }
