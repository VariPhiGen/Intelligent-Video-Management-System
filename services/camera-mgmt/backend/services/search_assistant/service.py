import asyncio
import copy
import re
import time
from uuid import uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ...models import AnalyticsActivityType
from .. import audit, policy
from ..search_planner.schemas import Clarify, PlannerError
from . import grounding, interpret, store, tools
from .schemas import Interpretation

log = structlog.get_logger(__name__)


async def catalog(db):
    rows = (await db.execute(select(AnalyticsActivityType))).scalars().all()
    return {t.key: {'label': t.label, 'color': t.color,
                    'search_aliases': t.search_aliases or []} for t in rows}


async def available(principal):
    return [t for t in tools.ALL_TOOLS if t not in tools.EVENT_TOOLS or await policy.allowed(principal, 'ai_analytics')]


async def revalidate(state, db, cameras, allowed):
    # A revoked tool invalidates the conversation; never reveal old result text.
    current_slugs = {c.slug for c in cameras}
    if any(p.get('cameras') is not None and not set(p['cameras']).issubset(current_slugs) for p in state['confirmed']):
        raise HTTPException(409, 'A camera in this investigation is no longer available. Start a new investigation.')
    used = {p['tool'] for p in state['confirmed']}
    for exchange in state['exchanges']:
        used.update(c['tool'] for c in exchange['answer'].get('collections', []))
    if not used.issubset(allowed):
        raise HTTPException(403, 'Permissions changed. Start a new investigation.')
    for exchange in state['exchanges']:
        await tools.revalidate(exchange['answer'].get('collections', []), db, cameras)
    await tools.revalidate(state.get('latest', []), db, cameras)


def typed_choice(message, choices):
    text = message.strip().casefold().rstrip('.!?')
    if text in ('no', 'neither', 'none', 'cancel'):
        return 'cancel'
    exact = [c['id'] for c in choices if c['label'].casefold() == text]
    if len(exact) == 1:
        return exact[0]
    if text in ('yes', 'okay', 'ok', 'sure'):
        return choices[0]['id'] if len(choices)==1 else 'ambiguous'
    ordinal = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5}
    match = re.fullmatch(r'(?:the |option )?(\d+|first|second|third|fourth|fifth)(?: one)?', text)
    if match:
        raw = match.group(1)
        n = int(raw) if raw.isdigit() else ordinal[raw]
        return choices[n-1]['id'] if 0<n<=len(choices) else 'ambiguous'
    return None


def summary(collections):
    parts = []
    for c in collections:
        if c.get('error'):
            parts.append(c['error'])
        elif c['tool']=='count_events':
            parts.append(f"{c['count']} retained AI-event records.")
        elif c['tool']=='describe_setup':
            parts.append(f"Configuration for {len(c.get('setup', []))} cameras.")
        else:
            parts.append(f"{len(c['items'])} matches in available data.")
    return ' '.join(parts)


def choice_answer(pending):
    return {'status': 'clarification', 'message': pending['message'],
            'choices': [{k: c[k] for k in ('id', 'label')} for c in pending['options']], 'collections': []}


async def run(state, body, eff, principal, request, db, cameras, activities, allowed):
    started = time.monotonic()
    # `operations` is what THIS turn ran — the audit record. A clarification or
    # choice-only turn runs nothing and must not be logged as the previous search.
    trace = {'trace_id': str(uuid4()), 'provider_retries': 0, 'token_usage': None, 'operations': []}
    answer = {'status': 'results', 'message': '', 'collections': []}
    plans = []
    message = body.message.strip()
    pending = state.get('pending')
    choice_id = body.choice_id or (typed_choice(message, pending['options']) if pending else None)
    try:
        if body.timezone:
            from ..search_planner.resolver import zone
            zone(body.timezone)
            state['timezone'] = body.timezone
            state['pending'] = None
            pending = None
            for plan in state['confirmed']:
                plan['timezone'] = body.timezone
            if not message:
                answer['message'] = f'Timezone set to {body.timezone}. Existing search windows keep the same instants; new clock times use this timezone.'
                return answer, trace
        # Explicit timezone names must not disappear into a model time.source
        # while the backend continues using a different investigation timezone.
        for mentioned in re.findall(r'\b(?:UTC|GMT|[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b', message):
            from ..search_planner.resolver import zone
            try:
                zone(mentioned)
            except Clarify:
                continue  # Camera labels can contain slashes; they are not timezones.
            equivalent = mentioned == state['timezone'] or {mentioned, state['timezone']} <= {'UTC', 'GMT'}
            if not equivalent:
                raise Clarify(f'Set the investigation timezone to {mentioned} using the timezone field, then repeat the search.')
        if body.result_id:
            selected = next(((c, i) for c in state['latest'] for i in c.get('items', []) if str(i['id']) == body.result_id), None)
            if selected is None or body.result_action not in ('play', 'similar'):
                raise Clarify('Select an available result from the latest results again.')
            answer = await follow_result(state, *selected, body.result_action, principal, request, db)
            trace['operations'] = followed(state, selected[1], body.result_action)
            return answer, trace
        if body.remove_filter:
            if not state['confirmed']:
                raise Clarify('Run a search first.')
            plans = copy.deepcopy(state['confirmed'])
            field = body.remove_filter
            for plan in plans:
                if field == 'time':
                    plan.pop('time_from', None)
                    plan.pop('time_to', None)
                plan[field] = None if field in ('cameras', 'activities') else ''
                plan.update(cursor=None, offset=0)
            state['pending'] = None
            if field in ('appearance', 'plate', 'time'):
                state.update(confirmed=plans, latest=[])
                answer['message'] = f'{field.capitalize()} filter cleared. Enter the next search or a replacement filter.'
                return answer, trace
        elif pending and choice_id:
            if choice_id == 'cancel':
                state['pending'] = None
                answer['message'] = 'Clarification cleared. Enter a new search.'
                return answer, trace
            if choice_id == 'ambiguous':
                return choice_answer(pending), trace
            choice = next((c for c in pending['options'] if c['id'] == choice_id), None)
            if choice is None:
                raise Clarify('That choice is no longer available. Select a current choice.')
            pending['overrides'][f"{pending['operation']}:{pending['field']}:{pending['phrase']}"] = choice['values']
            intent = Interpretation.model_validate(pending['intent'])
            for exchange in state['exchanges']:
                if any(c['id'] == choice_id for c in exchange['answer'].get('choices', [])):
                    exchange['answer']['selected_choice'] = choice_id
            if pending['field']=='activities':
                # Row locks prevent concurrent accepted suggestions losing each other.
                for key in choice['values']:
                    row = (await db.execute(select(AnalyticsActivityType).where(AnalyticsActivityType.key==key).with_for_update())).scalar_one_or_none()
                    if row and pending['phrase'] not in (row.search_aliases or []):
                        row.suggested_aliases = list(dict.fromkeys([*(row.suggested_aliases or []), pending['phrase']]))[-32:]
                await db.commit()
            plans = grounding.ground(intent, pending['query'], state['confirmed'], cameras, activities, eff.config, state['timezone'], overrides=pending['overrides'])
            state['pending'] = None
        else:
            if body.choice_id:
                raise Clarify('That clarification has expired. Ask again.')
            state['pending'] = None
            if message.casefold().rstrip('!? .') in ('hello', 'hi', 'hey', 'help', 'what can you do', 'what can i search'):
                answer['message'] = 'I can search people, vehicles and plates' + (', retrieve and count stored AI events, and describe camera setup.' if 'count_events' in allowed else '.')
                return answer, trace
            if not message:
                raise Clarify('Enter a question to search.')
            await store.progress(state['id'], 'Interpreting…')
            mark = time.monotonic()
            intent, metrics = await interpret.extract(eff.runtime, message, state['confirmed'], cameras, activities, state['timezone'])
            trace.update(metrics, provider_ms=round((time.monotonic()-mark)*1000))
            if intent.mode=='reset':
                state.update(confirmed=[], latest=[], pending=None)
                answer['message'] = 'Investigation filters cleared.'
                return answer, trace
            if intent.mode=='unsupported' or intent.unsupported:
                answer.update(status='unsupported', message='This search cannot answer: ' + ', '.join(intent.unsupported or ['the requested question']))
                return answer, trace
            if intent.mode=='clarify':
                raise Clarify(intent.clarification or 'Specify a camera, time and supported search subject.')
            if intent.mode in ('play', 'similar'):
                flat = [(c, i) for c in state['latest'] for i in c.get('items', [])]
                pos = intent.result_position or 0
                if not 1 <= pos <= len(flat):
                    raise Clarify('Select an available result from the latest results again.')
                collection, item = flat[pos-1]
                answer = await follow_result(state, collection, item, intent.mode, principal, request, db)
                trace['operations'] = followed(state, item, intent.mode)
                return answer, trace
            if intent.mode=='more':
                if set(interpret.words(message)) - {'show', 'more', 'next', 'page', 'results', 'please', 'the'}:
                    raise Clarify('Apply the new filters first, then ask for the next page.')
                plans = copy.deepcopy(state['confirmed'])
                if not plans or len(plans)!=len(state['latest']):
                    raise Clarify('Run a search first.')
                for plan, collection in zip(plans, state['latest']):
                    if not collection.get('next_cursor'):
                        raise Clarify('There are no more results in this search. Refine the filters.')
                    if plan['tool']=='search_events':
                        plan['cursor'] = collection['next_cursor']
                    else:
                        plan['offset'] = int(collection['next_cursor'])
            else:
                mark = time.monotonic()
                plans = grounding.ground(intent, message, state['confirmed'], cameras, activities, eff.config, state['timezone'])
                trace['grounding_ms'] = round((time.monotonic()-mark)*1000)
        if any(p['tool'] not in allowed for p in plans):
            raise HTTPException(403, 'Your role cannot use the requested Assistant tool.')
        if not plans:
            raise Clarify('Choose a supported search to run.')
        collections = []
        for plan in plans:
            stage = 'Counting events…' if plan['tool']=='count_events' else 'Checking camera setup…' if plan['tool']=='describe_setup' else 'Searching cameras…'
            await store.progress(state['id'], stage)
            mark = time.monotonic()
            try:
                async with asyncio.timeout(max(.1, min(20, 58-(time.monotonic()-started)))):
                    collection = await tools.execute(plan, request, db, cameras, activities)
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    raise
                collection = {'tool': plan['tool'], 'scope': plan, 'items': [], 'error': 'This search service is unavailable. Retry this operation.', 'warnings': [], 'next_cursor': None}
                await db.rollback()
            except TimeoutError:
                await db.rollback()
                collection = {'tool': plan['tool'], 'scope': plan, 'items': [], 'error': 'This operation timed out. Narrow the scope or retry.', 'warnings': [], 'next_cursor': None}
            except SQLAlchemyError:
                await db.rollback()
                collection = {'tool': plan['tool'], 'scope': plan, 'items': [], 'error': 'Event storage is unavailable. Retry this operation.', 'warnings': [], 'next_cursor': None}
            collections.append(collection)
            trace.setdefault('tools', []).append({'tool': plan['tool'], 'duration_ms': round((time.monotonic()-mark)*1000)})
        failures = sum('error' in c for c in collections)
        answer.update(status='error' if failures==len(collections) else 'partial' if failures else 'results',
                      message=summary(collections), collections=collections)
        state.update(confirmed=plans, latest=collections)
        trace['operations'] = [audited(p) for p in plans]
        return answer, trace
    except grounding.ChoiceNeeded as exc:
        # Carry prior resolved choices forward if there is another ambiguity.
        state['pending'] = {'message': exc.message, 'options': exc.options, 'intent': intent.model_dump(),
                            'query': pending['query'] if pending and choice_id else message,
                            'overrides': pending['overrides'] if pending and choice_id else {},
                            'operation': exc.operation, 'field': exc.field, 'phrase': exc.phrase}
        return choice_answer(state['pending']), trace
    except Clarify as exc:
        answer.update(status='clarification', message=exc.message)
        return answer, trace
    except PlannerError as exc:
        answer.update(status='error', message=exc.message)
        return answer, trace
    finally:
        trace['total_ms'] = round((time.monotonic()-started)*1000)


def audited(plan):
    return {k: plan.get(k) for k in ('tool', 'cameras', 'activities', 'time_from', 'time_to')}


def followed(state, item, action):
    if action == 'play':
        return [{'tool': 'playback', 'cameras': [item['camera']['slug']], 'evidence_id': str(item['id'])}]
    return [{**audited(state['confirmed'][0]), 'example_id': str(item['id'])}]


async def follow_result(state, collection, item, action, principal, request, db):
    if action == 'play':
        await policy.require(principal, 'playback_search')
        return {'status': 'results', 'message': 'Open the selected result in playback.', 'collections': [],
                'playback': item.get('playback') or {'camera': item['camera']['slug'], 'start': item['when_ms']//1000}}
    if collection['tool'] != 'search_people':
        raise Clarify('Search by example is available for person results. Select a person result.')
    plan = {**collection['scope'], 'tool': 'search_people', 'sort': 'relevance', 'grouping': 'none',
            'cursor': None, 'offset': 0, 'appearance': '',
            'example': {k: item[k] for k in ('id', 'camera', 'when_ms')}}
    from ...routers import search
    cameras = await search._registered(db)
    await store.progress(state['id'], 'Searching for visually similar people…')
    out = await tools.execute(plan, request, db, cameras, {})
    state.update(confirmed=[plan], latest=[out])
    return {'status': 'results', 'message': f"{len(out['items'])} visually similar person matches. Similarity does not establish identity.", 'collections': [out]}
