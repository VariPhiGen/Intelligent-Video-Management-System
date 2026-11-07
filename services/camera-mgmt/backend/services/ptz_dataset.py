"""ptz_dataset.py — PTZ phase 4: a dataset of a PTZ camera samples only while the camera was AT chosen presets.

A dataset is "camera X, 10:00-12:00, 200 frames", spread evenly over the window (services/dataset_build). For a PTZ
camera that mixes every preset's view with the moves between them — training images of a smeared pan, of the yard
when you wanted the gate. Here the window becomes the TIME AT the chosen presets:

    the camera's confirmed views in the window  →  intervals at those presets  →  frames spread over just that time

WHICH VIEWS. ptz_view_history (migration 068) holds every change of the CONFIRMED view — arrived at a preset and the
picture matched (services/ptz_track). For a window older than that history (footage recorded before phase 4), the
position log stands in: "at the preset" by the camera's own position report, 3 s after it got there, labelled
`position` so nobody mistakes it for a checked view.

THE EDGES. A view ends when the tracker notices the camera moving, a poll (~1-2 s) after the move began, so the last
END_MARGIN_S of every interval is left out; the start needs nothing (a view is only confirmed once the picture
settled). An interval shorter than MIN_INTERVAL_S is dropped: a 1-s touch of a preset is a fly-by, not a view.

Pure functions plus one DB read (`load_intervals`); dataset_build stays untouched apart from taking a planned list of
instants with what each one shows.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

END_MARGIN_S = 2.0
POSITION_SETTLE_S = 3.0
MIN_INTERVAL_S = 2.0


def _f(x: Any) -> str:
    return str(x) if x is not None else ""


def intervals(entries: Iterable[Mapping], start: datetime, end: datetime, wanted: set[str], *,
              at_state: str = "preset", settle_s: float = 0.0) -> list[dict]:
    """Time inside [start, end) at one of the `wanted` presets — pure. `entries` oldest first, each {at, state,
    preset_id, preset_name}; the entry in force at `start` (the last one before it) must be included. A state other
    than `at_state` ends an interval. Returns [{start, end, preset_id, preset_name}] oldest first."""
    rows = sorted((e for e in entries if e.get("at") is not None), key=lambda e: e["at"])
    out: list[dict] = []
    for i, e in enumerate(rows):
        nxt = rows[i + 1]["at"] if i + 1 < len(rows) else end
        if e.get("state") != at_state or _f(e.get("preset_id")) not in wanted:
            continue
        s = max(e["at"] + timedelta(seconds=settle_s), start)
        t = min(nxt - timedelta(seconds=END_MARGIN_S), end) if nxt < end else end
        if (t - s).total_seconds() >= MIN_INTERVAL_S:
            out.append({"start": s, "end": t, "preset_id": _f(e.get("preset_id")), "preset_name": e.get("preset_name") or ""})
    return out


def total_seconds(spans: list[dict]) -> float:
    return sum((s["end"] - s["start"]).total_seconds() for s in spans)


def spread(spans: list[dict], n: int) -> list[dict]:
    """`n` instants evenly over the joined time of `spans` — the midpoints of n equal slices, as plan_timestamps does
    over a plain window (never on an interval's edge). [{at, preset_id, preset_name}] in time order."""
    total = total_seconds(spans)
    if n <= 0 or total <= 0:
        return []
    step = total / n
    out, k, offset = [], 0, 0.0
    for s in spans:
        length = (s["end"] - s["start"]).total_seconds()
        while k < n:
            pos = step * (k + 0.5) - offset
            if pos >= length:
                break
            out.append({"at": s["start"] + timedelta(seconds=pos), "preset_id": s["preset_id"], "preset_name": s["preset_name"]})
            k += 1
        offset += length
    return out


def scan(spans: list[dict], every_s: float, max_scans: int = 20000) -> list[dict]:
    """Detection mode: an instant every `every_s` inside each span (first at half a step)."""
    out: list[dict] = []
    if every_s <= 0:
        return out
    for s in spans:
        t = s["start"] + timedelta(seconds=every_s / 2)
        while t < s["end"] and len(out) < max_scans:
            out.append({"at": t, "preset_id": s["preset_id"], "preset_name": s["preset_name"]})
            t += timedelta(seconds=every_s)
    return out


def per_preset(spans: list[dict]) -> list[dict]:
    """[{preset_id, preset_name, seconds, visits}] — what the form shows next to each preset."""
    acc: dict[str, dict] = {}
    for s in spans:
        a = acc.setdefault(s["preset_id"], {"preset_id": s["preset_id"], "preset_name": s["preset_name"], "seconds": 0.0, "visits": 0})
        a["seconds"] += (s["end"] - s["start"]).total_seconds()
        a["visits"] += 1
    return sorted(acc.values(), key=lambda a: -a["seconds"])


async def load_intervals(db, camera_id: uuid.UUID, start: datetime, end: datetime,
                         wanted: Optional[set[str]] = None) -> tuple[list[dict], str]:
    """The intervals at `wanted` presets (None = every preset) in [start, end), and how they were told:
    "picture" (confirmed views), "position" (the position log, for footage before the view history), or "" (none)."""
    from sqlalchemy import select

    from ..models import PtzPositionLog, PtzViewHistory

    def rows_of(model, state_col_value):
        return [{"at": r.at, "state": r.state, "preset_id": r.preset_id, "preset_name": r.preset_name} for r in state_col_value]

    first = (await db.execute(select(PtzViewHistory.at).where(PtzViewHistory.camera_id == camera_id)
                              .order_by(PtzViewHistory.at.asc()).limit(1))).scalar_one_or_none()
    if first is not None and first <= start:
        model, at_state, settle, source = PtzViewHistory, "preset", 0.0, "picture"
    else:
        model, at_state, settle, source = PtzPositionLog, "at_preset", POSITION_SETTLE_S, "position"
    before = (await db.execute(select(model).where(model.camera_id == camera_id, model.at < start)
                               .order_by(model.at.desc()).limit(1))).scalars().all()
    inside = (await db.execute(select(model).where(model.camera_id == camera_id, model.at >= start, model.at < end)
                               .order_by(model.at.asc()))).scalars().all()
    entries = rows_of(model, list(before) + list(inside))
    if wanted is None:
        wanted = {_f(e["preset_id"]) for e in entries if e.get("preset_id") is not None}
    spans = intervals(entries, start, end, wanted, at_state=at_state, settle_s=settle)
    return spans, (source if entries else "")
