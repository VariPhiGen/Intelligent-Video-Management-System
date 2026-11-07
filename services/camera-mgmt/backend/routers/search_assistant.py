"""Authenticated Assistant lifecycle. Feature availability is checked on every call."""
import asyncio
import hashlib
import json
import time
from uuid import UUID, uuid4
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..security import get_principal
from ..services import audit, policy
from ..services.search_planner import settings
from ..services.search_planner.resolver import zone
from ..services.search_planner.schemas import Clarify, PlannerError
from ..services.search_assistant import service, store
from ..services.search_assistant.schemas import CreateConversation, Turn
from . import search

router = APIRouter(prefix='/assistant', tags=['search-assistant'])


async def context(request, db):
    principal = await get_principal(request)
    await policy.require(principal, 'smart_search')
    eff = settings.effective(await settings.row(db))
    if not eff.config.assistant_enabled or not eff.public.get('assistant_tested'):
        raise HTTPException(503, 'The Assistant is disabled or needs its readiness test. Configure it in Query interpretation.')
    return principal, eff, await service.available(principal)


def unavailable():
    return HTTPException(503, 'Conversation storage is unavailable. Retry or start a new investigation.')


@router.post('/conversations')
async def create(body: CreateConversation, request: Request, db: AsyncSession = Depends(get_db)):
    principal, eff, allowed = await context(request, db)
    try:
        zone(body.timezone)
        state = await store.create(principal, body.timezone)
        cameras = await search._registered(db)
        activities = await service.catalog(db) if 'search_events' in allowed else {}
        suggestions = []
        from ..services.ai_events import configured_activities
        for camera in cameras:
            for activity in configured_activities(camera):
                if activity in activities:
                    suggestions.append(f"Count {activities[activity]['label']} events at {camera.name or camera.slug} today")
        return {**store.public(state), 'suggestions': suggestions[:4], 'available_tools': allowed}
    except Clarify as exc:
        raise HTTPException(422, exc.message) from None
    except RedisError:
        raise unavailable() from None


@router.get('/conversations/{ident}')
async def restore(ident: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    principal, eff, allowed = await context(request, db)
    try:
        state = await store.read(ident, principal)
        cameras = await search._registered(db)
        await service.revalidate(state, db, cameras, allowed)
        r = await store.get_redis()
        return {**store.public(state), 'progress': await r.get(store.key(ident)+':progress'), 'available_tools': allowed}
    except RedisError:
        raise unavailable() from None


@router.get('/conversations/{ident}/progress')
async def progress(ident: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    principal, eff, allowed = await context(request, db)
    try:
        await store.read(ident, principal)
        r = await store.get_redis()
        return {'progress': await r.get(store.key(ident)+':progress')}
    except RedisError:
        raise unavailable() from None


@router.delete('/conversations/{ident}')
async def clear(ident: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    # Clearing remains possible when an administrator has disabled the feature.
    principal = await get_principal(request)
    await policy.require(principal, 'smart_search')
    try:
        await store.read(ident, principal)
        r = await store.get_redis()
        await r.delete(store.key(ident), store.key(ident)+':progress')
        return {'ok': True}
    except RedisError:
        raise unavailable() from None


@router.post('/conversations/{ident}/turns')
async def turn(ident: UUID, body: Turn, request: Request, db: AsyncSession = Depends(get_db)):
    principal, eff, allowed = await context(request, db)
    lease = None
    trace = {'trace_id': str(uuid4())}
    state = None
    answer = {'status': 'error', 'message': 'The turn did not finish.', 'collections': []}
    request_id = str(body.request_id)
    replay = False
    digest = hashlib.sha256(json.dumps(body.model_dump(mode='json', exclude={'expected_version'}), sort_keys=True).encode()).hexdigest()
    try:
        state = await store.read(ident, principal)
        lease = await store.acquire(ident)
        # Read AFTER acquiring to reject a version that changed between awaits.
        state = await store.read(ident, principal)
        cameras = await search._registered(db)
        await service.revalidate(state, db, cameras, allowed)
        cached = state['requests'].get(request_id)
        if cached:
            answer = state['exchanges'][cached['index']]['answer']
            if cached['digest'] != digest:
                raise HTTPException(409, 'This request ID was already used for a different message.')
            replay = True  # Nothing runs again; audited as a replay, not a second search.
            return {**store.public(state), 'answer': state['exchanges'][cached['index']]['answer']}
        if state['version'] != body.expected_version:
            raise HTTPException(409, 'The investigation changed. Refresh before sending another turn.')
        activities = await service.catalog(db) if 'search_events' in allowed else {}
        # Freeze loaded registry metadata before releasing the transaction.
        cameras = [SimpleNamespace(**{k: v for k, v in c.__dict__.items() if not k.startswith('_')}) for c in cameras]
        await db.rollback()
        try:
            async with asyncio.timeout(60):
                answer, trace = await service.run(state, body, eff, principal, request, db, cameras, activities, allowed)
        except TimeoutError:
            answer = {'status': 'error', 'message': 'The turn reached its 60-second deadline. Retry or narrow the search.', 'collections': []}
        state['version'] += 1
        state['exchanges'].append({'request_id': request_id, 'message': body.message,
                                   'answer': answer, 'digest': digest})
        state['exchanges'] = state['exchanges'][-6:]
        state['requests'] = {e['request_id']: {'digest': e['digest'], 'index': i} for i, e in enumerate(state['exchanges'])}
        await store.save(state, lease)
        return {**store.public(state), 'answer': answer}
    except RedisError:
        raise unavailable() from None
    finally:
        # What this turn ran (service.run's trace), never the investigation's
        # standing filters: those would log a clarification turn as a search.
        await audit.record(request, principal, 'smartsearch.assistant.turn', target=str(ident), detail={
            'outcome': answer['status'], 'trace_id': trace['trace_id'],
            'operations': [] if replay else trace.get('operations', []), **({'replay': True} if replay else {})})
        service.log.info('smartsearch.assistant.turn', **trace, outcome=answer['status'], provider=eff.config.provider)
        if lease:
            try:
                await store.release(ident, lease)
            except RedisError:
                pass  # Lease expires; never replace a successful response with cleanup failure.
