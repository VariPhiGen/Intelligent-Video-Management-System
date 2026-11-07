"""Read-only adapters; all SQL is composed here from typed, resolved filters."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select, tuple_
from fastapi import HTTPException
from starlette.requests import Request
from ...models import AnalyticsEvent, Camera
from ...config import settings
from .. import ai_events, search_scope
from ...routers import search

EVENT_TOOLS = {'count_events', 'search_events', 'describe_setup'}
ALL_TOOLS = ['search_people', 'search_vehicles', 'lookup_plate', 'search_events', 'count_events', 'describe_setup']


def event_query(plan, cameras):
    chosen = [c.id for c in cameras if plan['cameras'] is None or c.slug in plan['cameras']]
    f = ai_events.EventFilter(camera_ids=chosen, frm=datetime.fromisoformat(plan['time_from']),
                              to=datetime.fromisoformat(plan['time_to']))
    q = ai_events.events_query(f).limit(None).order_by(None)
    # Half-open windows keep adjacent comparisons/buckets disjoint.
    q = q.where(AnalyticsEvent.started_at < f.to)
    if plan.get('activities') is not None:
        q = q.where(AnalyticsEvent.activity.in_(plan['activities']))
    return q


async def describe(plan, cameras, *, coverage=False):
    selected = [c for c in cameras if plan['cameras'] is None or c.slug in plan['cameras']]
    if coverage:
        offset = plan.get('offset', 0)
        selected = selected[offset:offset+24]
    recorder = await search_scope.recorder_cameras()
    now_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    rows = []
    for c in selected:
        retained = search_scope.retained_window(recorder[c.slug], now_ms) if recorder is not None and c.slug in recorder else None
        rows.append({'camera': search._camera_ref(c), 'activities': ai_events.configured_activities(c),
                     'recording_enabled': bool(c.enabled and c.recording),
                     'search_indexing': bool(getattr(c, 'search_indexing', False)),
                     'search_domains': getattr(c, 'search_domains', []) or [],
                     'retention_minutes': c.retention_minutes or settings.nvr_retention_minutes or settings.nvr_default_retention_days*1440,
                     'retained_window_ms': retained,
                     'recorder_available': recorder is not None,
                     'recording_registered': c.slug in recorder if recorder is not None else None})
    if coverage:
        start = int(datetime.fromisoformat(plan['time_from']).timestamp()*1000)
        end = int(datetime.fromisoformat(plan['time_to']).timestamp()*1000)
        slots = asyncio.Semaphore(4)
        async def check(row):
            async with slots:
                spans = await search_scope._covered_spans(row['camera']['slug'], start, end) if recorder is not None else None
                row['recorded_seconds'] = None
                if spans is not None:
                    # Count the union of covered intervals, never double-count overlap.
                    total, last = 0, start
                    for a, b in sorted(spans):
                        lo, hi = max(start, a, last), min(end, b)
                        if hi > lo:
                            total += hi-lo
                            last = hi
                    row['recorded_seconds'] = total / 1000
                row['window_seconds'] = (end-start) / 1000
        await asyncio.gather(*(check(row) for row in rows))
    return rows


def scope_labels(scope, cameras, catalog):
    """Display names for the filter chips; the scope itself keeps slugs and keys."""
    names = {c.slug: c.name or c.slug for c in cameras}
    def label(values, lookup):
        return None if values is None else [lookup(v) for v in values]
    return {'cameras': label(scope.get('cameras'), lambda s: names.get(s, s)),
            'activities': label(scope.get('activities'), lambda k: (catalog.get(k) or {}).get('label', k))}


def group_label(group, value, cameras, catalog):
    if group == 'camera':
        return next((c.name or c.slug for c in cameras if c.slug == value), str(value))
    if group == 'activity':
        return (catalog.get(value) or {}).get('label', str(value))
    return str(value)


async def execute(plan, request, db, cameras, catalog):
    tool = plan['tool']
    scope = {k: plan.get(k) for k in ('cameras', 'activities', 'appearance', 'plate', 'time_from', 'time_to', 'timezone', 'grouping')}
    result = {'tool': tool, 'scope': scope, 'scope_labels': scope_labels(scope, cameras, catalog),
              'interpretations': plan.get('interpretations', []),
              'items': [], 'warnings': [], 'next_cursor': None, 'truncated': False}
    if tool in ('search_people', 'search_vehicles'):
        args = dict(query=plan['appearance'], time_from=plan['time_from'], time_to=plan['time_to'],
                    top_k=96, score_threshold=.15, sort_by='confidence' if plan['sort']=='relevance' else 'time',
                    sort_dir='asc' if plan['sort']=='earliest' else 'desc')
        if tool == 'search_people' and plan.get('example'):
            example = plan['example']
            verified = await search_scope.scope([example], candidates_of=lambda i: (i['camera']['slug'],),
                                                 timestamp_of=lambda i: i['when_ms'], cameras=cameras)
            if not verified.kept:
                raise HTTPException(410, 'The example is no longer available. Select another result.')
            image = await search.crop_image('person', example['id'], request)
            async def receive():
                return {'type': 'http.request', 'body': image.body, 'more_body': False}
            proxy = Request(request.scope, receive)
            data = await search.search_people_by_image(proxy, top_k=96, score_threshold=.15,
                camera=None, time_from=plan['time_from'], time_to=plan['time_to'], db=db,
                camera_slugs=plan['cameras'])
        elif tool == 'search_people':
            data = await search.execute_people(search.PeopleQuery(**args), request, db, camera_slugs=plan['cameras'])
        else:
            data = await search.execute_vehicles(search.VehiclesQuery(**args, plate=plan.get('plate') or None), request, db, camera_slugs=plan['cameras'])
        offset = plan.get('offset', 0)
        result['items'] = data['results'][offset:offset+24]
        result['truncated'] = bool(data.get('truncated'))
        result['next_cursor'] = str(offset+24) if len(data['results']) > offset+24 else None
        if data.get('withheld'):
            result['warnings'].append('Some indexed matches were withheld because their camera or recording is unavailable.')
        if data.get('truncated'):
            result['warnings'].append('Similarity search is limited to the best 96 matches. Narrow the filters for more precise results.')
    elif tool == 'lookup_plate':
        # The existing plate gateway accepts one camera. Extend it with an
        # internal scope parameter rather than filtering an unscoped top page.
        data = await search.search_plates(search.PlatesQuery(plate=plan['plate'], time_from=plan['time_from'],
            time_to=plan['time_to'], limit=200), request, db, camera_slugs=plan['cameras'])
        hits = [{**s, 'plate': g['plate']} for g in data['plates'] for s in g['sightings']]
        offset = plan.get('offset', 0)
        result['items'] = hits[offset:offset+24]
        result['next_cursor'] = str(offset+24) if len(hits)>offset+24 else None
        result['truncated'] = len(hits)>=200
        if data.get('plates_active') is False:
            result['warnings'].append('Plate reading is disabled in the index.')
    elif tool == 'describe_setup':
        result['setup'] = await describe(plan, cameras, coverage=True)
        total = sum(plan['cameras'] is None or c.slug in plan['cameras'] for c in cameras)
        result['setup_total'] = total
        offset = plan.get('offset', 0)
        result['next_cursor'] = str(offset+24) if total > offset+24 else None
    elif tool in ('search_events', 'count_events'):
        query = event_query(plan, cameras)
        if tool == 'count_events':
            base = query.subquery()
            total = (await db.execute(select(func.count(func.distinct(base.c.id))))).scalar_one()
            group = plan['grouping']
            result.update(count=total, count_unit='retained AI-event records', count_as_of=datetime.now(timezone.utc).isoformat(), groups=[])
            column = {'camera': base.c.camera_slug, 'activity': base.c.activity}.get(group)
            if group == 'zone':
                # Zone is camera metadata, distinct from an event's detection region.
                base = query.add_columns(Camera.camera_metadata['zone'].astext.label('camera_zone')).subquery()
                column = func.coalesce(base.c.camera_zone, 'Unassigned')
            if group in ('day', 'hour'):
                column = func.date_trunc(group, func.timezone(plan['timezone'], base.c.started_at))
            if column is not None:
                counted = func.count(func.distinct(base.c.id))
                # Time buckets read in time order; camera/zone/activity groups are
                # ranked, so "which camera had the most" is the first row.
                order = [column] if group in ('day', 'hour') else [counted.desc(), column]
                rows = (await db.execute(select(column.label('group'), counted.label('count'))
                                        .group_by(column).order_by(*order).limit(501))).all()
                result['groups'] = [{'label': group_label(group, r.group, cameras, catalog), 'count': r.count} for r in rows[:500]]
                result['truncated'] = len(rows)>500
        else:
            asc = plan['sort']=='earliest'
            if plan.get('cursor'):
                cur = ai_events.Cursor.decode(plan['cursor'])
                pair = tuple_(AnalyticsEvent.started_at, AnalyticsEvent.id)
                query = query.where(pair > tuple_(cur.started_at, cur.id) if asc else pair < tuple_(cur.started_at, cur.id))
            order = [AnalyticsEvent.started_at, AnalyticsEvent.id]
            query = query.order_by(*(c.asc() if asc else c.desc() for c in order)).limit(25)
            rows = (await db.execute(query)).mappings().all()
            result['items'] = [ai_events.shape(r, slug=r['camera_slug'], name=r['camera_name'], catalog=catalog) for r in rows[:24]]
            for item in result['items']:
                item.pop('attributes', None)  # Arbitrary producer payloads never enter conversation memory.
            if len(rows)>24:
                result['next_cursor'] = ai_events.Cursor(rows[23]['started_at'], rows[23]['id']).encode()
    else:
        raise ValueError('Unknown tool')
    if not result['items'] and not result.get('count') and tool != 'describe_setup':
        setup = await describe(plan, cameras)
        result['setup'] = setup
        if tool in ('search_people', 'search_vehicles') and setup and not any(c['search_indexing'] for c in setup):
            result['warnings'].append('Visual indexing is currently disabled on these cameras. Historical indexed data may differ.')
        if plan.get('activities') and not any(set(plan['activities']) & set(c['activities']) for c in setup):
            result['warnings'].append('These activities are not currently configured on the selected cameras; historical configuration may differ.')
        if setup and not any(c['recording_enabled'] for c in setup):
            result['warnings'].append('Recording is currently disabled on these cameras.')
        if any(not c['recorder_available'] for c in setup):
            result['warnings'].append('Recording availability could not be checked because the recorder is unavailable.')
        end = datetime.fromisoformat(plan['time_to'])
        if setup and all((datetime.now(timezone.utc)-end).total_seconds() > c['retention_minutes']*60 for c in setup):
            result['warnings'].append('This window is outside the configured recording retention.')
    result['evidence_ids'] = [str(i['id']) for i in result['items']]
    return result


async def revalidate(collections, db, cameras):
    """Restore never serves an expired/deleted evidence card from Redis."""
    for collection in collections:
        current = {c.slug for c in cameras}
        if collection.get('setup'):
            collection['setup'] = [c for c in collection['setup'] if c['camera']['slug'] in current]
        items = collection.get('items', [])
        if not items:
            continue
        if collection['tool'] == 'search_events':
            import uuid
            ids = [uuid.UUID(i['id']) for i in items]
            live = set(str(v) for v in (await db.execute(select(AnalyticsEvent.id).where(
                AnalyticsEvent.id.in_(ids), AnalyticsEvent.expires_at > func.now(),
                AnalyticsEvent.camera_id.in_([c.id for c in cameras])))).scalars())
            kept = [i for i in items if i['id'] in live]
        else:
            scoped = await search_scope.scope(items, candidates_of=lambda i: (i['camera']['slug'],),
                timestamp_of=lambda i: i['when_ms'], cameras=cameras)
            kept = [i.hit for i in scoped.kept]
        if len(kept) != len(items):
            collection.setdefault('warnings', []).append('Some evidence is no longer available. Run the search again for current results.')
        collection['items'] = kept
        collection['evidence_ids'] = [str(i['id']) for i in kept]
