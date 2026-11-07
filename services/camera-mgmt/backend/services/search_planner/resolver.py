from __future__ import annotations

import difflib
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schemas import Clarify, NaturalQuery, PlannerConfig, SearchIntent, SearchPlan

UTC = timezone.utc


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Clarify('Choose a valid timezone before searching.') from None


def local_instant(day: date, clock: str, tz: ZoneInfo) -> datetime:
    try:
        if not re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?', clock):
            raise ValueError()
        naive = datetime.combine(day, time.fromisoformat(clock))
    except ValueError:
        raise Clarify('Specify a valid local clock time, including AM or PM.') from None
    candidates = set()
    for fold in (0, 1):
        utc = naive.replace(tzinfo=tz, fold=fold).astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            candidates.add(utc)
    if len(candidates) != 1:
        raise Clarify('That local time is ambiguous or does not exist during a clock change. Use the explicit time filters.')
    return candidates.pop()


def _day(value, offset, today):
    if (value is None) == (offset is None):
        raise Clarify('Specify one date for the requested time.')
    try:
        return date.fromisoformat(value) if value else today + timedelta(days=offset)
    except (ValueError, OverflowError, TypeError):
        raise Clarify('Specify a valid calendar date.') from None


def time_bounds(intent, tz, now, config):
    t = intent
    fields = t.model_dump(exclude={'kind', 'source'})
    allowed = {
        'none': set(), 'recent': {'recent_minutes'},
        'day': {'date', 'day_offset'},
        'point': {'date', 'day_offset', 'start_time'},
        'range': {'date', 'day_offset', 'start_time', 'end_date', 'end_day_offset', 'end_time'},
    }[t.kind]
    if any(v is not None and k not in allowed for k, v in fields.items()):
        raise Clarify('The time description is inconsistent. Specify an exact date and time range.')
    if t.kind == 'none':
        if t.source.strip():
            raise Clarify('Please clarify the time mentioned in your query.')
        return now - timedelta(hours=config.lookback_hours), now
    if not t.source.strip():
        raise Clarify('Please specify the date or time range to search.')
    if t.kind == 'recent':
        if t.recent_minutes is None or not 0 < t.recent_minutes <= config.max_window_days * 1440:
            raise Clarify('Specify a recent duration within the configured search limit.')
        return now - timedelta(minutes=t.recent_minutes), now
    day = _day(t.date, t.day_offset, now.astimezone(tz).date())
    if t.kind == 'day':
        return local_instant(day, '00:00', tz), local_instant(day + timedelta(days=1), '00:00', tz)
    if not t.start_time:
        raise Clarify('Specify the start time, including AM or PM.')
    start = local_instant(day, t.start_time, tz)
    if t.kind == 'point':
        delta = timedelta(minutes=config.point_minutes)
        return start - delta, start + delta
    if not t.end_time:
        raise Clarify('Specify an end time for the range.')
    end_day = day if t.end_date is None and t.end_day_offset is None else _day(
        t.end_date, t.end_day_offset, now.astimezone(tz).date())
    return start, local_instant(end_day, t.end_time, tz)


def _camera_key(name: str) -> str:
    """Spoken and written camera names differ in spacing, punctuation, the word
    "camera" and zero padding: "Camera 02", "cam-2" and "CAM_2" are one key.
    Only ever a fallback after an exact match, and a key shared by two cameras
    is a clarification, never a pick."""
    key = re.sub(r'camera', 'cam', name.casefold())
    key = re.sub(r'[^0-9a-z\u00c0-\uffff]+', '', key)
    return re.sub(r'(?<![0-9])0+(?=[0-9])', '', key)


def _camera_names(c) -> list[str]:
    aliases = (getattr(c, 'camera_metadata', None) or {}).get('search_aliases', [])
    if not isinstance(aliases, list):
        aliases = []
    return [n for n in (c.slug, c.name or '', *[a for a in aliases if isinstance(a, str)]) if n.strip()]


def _match_camera(ref: str, cameras: list) -> list:
    exact = ref.strip().casefold()
    found = [c for c in cameras if exact in {n.strip().casefold() for n in _camera_names(c)}]
    if found:
        return found
    key = _camera_key(ref)
    if not key:
        return []
    return [c for c in cameras if key in {_camera_key(n) for n in _camera_names(c)}]


def _suggestions(ref: str, cameras: list, matches: list) -> list[str]:
    """Names to offer when a reference did not pick exactly one camera: the
    tied cameras, else the closest names, else the first few by name."""
    label = lambda c: c.name or c.slug
    if matches:
        return sorted({label(c) for c in matches})[:10]
    by_key = {}
    for c in cameras:
        for n in _camera_names(c):
            by_key.setdefault(_camera_key(n), label(c))
    close = difflib.get_close_matches(_camera_key(ref), list(by_key), n=10, cutoff=0.5)
    return list(dict.fromkeys(by_key[k] for k in close)) or sorted({label(c) for c in cameras})[:10]


def _explicit(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(UTC)
    except ValueError:
        raise Clarify('Explicit dates must include their timezone.') from None


def resolve(intent: SearchIntent, body: NaturalQuery, cameras, config: PlannerConfig,
            now: datetime, *, unsearchable=()) -> SearchPlan:
    """`cameras` is what this domain's search covers; `unsearchable` are
    registered cameras it does not (no recordings), named only to explain why
    a reference to one cannot be searched."""
    tz = zone(body.timezone)
    if intent.clarification:
        raise Clarify(intent.clarification[:500])
    if intent.domain and intent.domain != body.domain:
        raise Clarify(f'This query describes {intent.domain}. Switch to the {intent.domain} tab or change the query.')
    visual = intent.visual_query.strip()
    if not visual or len(visual) > 500:
        raise Clarify('Describe the appearance of the person or vehicle to search for.')
    by_slug = {c.slug: c for c in cameras if c.slug}
    selected = set()
    for ref in intent.camera_refs:
        if not ref.strip():
            raise Clarify('Specify a camera name.')
        matches = _match_camera(ref, list(by_slug.values()))
        if not matches and (absent := _match_camera(ref, list(unsearchable))):
            raise Clarify(f'{absent[0].name or absent[0].slug} has no recordings, so this search cannot cover it.'
                          if len(absent) == 1 else 'None of the cameras with that name have recordings to search.',
                          _suggestions(ref, list(by_slug.values()), []))
        if len(matches) != 1:
            shown = ref.strip()[:80]
            reason = (f'"{shown}" matches more than one camera.' if matches
                      else f'No available camera is named "{shown}".')
            raise Clarify(f'{reason} Pick one with the camera filter, and remove or correct the name in the query.',
                          _suggestions(ref, list(by_slug.values()), matches))
        selected.add(matches[0].slug)
    if body.camera:
        if body.camera not in by_slug:
            raise Clarify('The selected camera is not available for this search.')
        if selected and selected != {body.camera}:
            raise Clarify('The camera in your query conflicts with the camera filter. Update one of them.')
        selected = {body.camera}
    scoped = bool(selected)
    if not scoped:
        selected = set(by_slug)
    start, end = time_bounds(intent.time, tz, now, config)
    if bool(body.time_from) != bool(body.time_to):
        raise Clarify('Set both explicit time filters or clear both.')
    if body.time_from and body.time_to:
        explicit = (_explicit(body.time_from), _explicit(body.time_to))
        if intent.time.kind != 'none' and (start, end) != explicit:
            raise Clarify('The time in your query conflicts with the time filters. Update one of them.')
        start, end = explicit
    if end <= start:
        raise Clarify('The end must be after the start. For overnight ranges include both dates.')
    if end - start > timedelta(days=config.max_window_days):
        raise Clarify(f'Choose a range of at most {config.max_window_days} days.')
    if start >= now:
        raise Clarify('Choose a time in the past to search recorded footage.')
    if not by_slug:
        names = 'No available cameras'
    elif scoped:
        names = ', '.join(by_slug[s].name or s for s in sorted(selected))
    else:
        names = f'All cameras ({len(by_slug)})'
    summary = f'{names} · {start.astimezone(tz):%Y-%m-%d %H:%M:%S} – {end.astimezone(tz):%Y-%m-%d %H:%M:%S} ({body.timezone})'
    if intent.time.kind == 'point' and not body.time_from:
        summary += f' · ±{config.point_minutes} minutes'
    elif intent.time.kind == 'none' and not body.time_from:
        summary += f' · default last {config.lookback_hours} hours'
    return SearchPlan(domain=body.domain, visual_query=visual, camera_slugs=sorted(selected),
                      camera_scoped=scoped,
                      time_from=start.isoformat(), time_to=end.isoformat(), summary=summary)
