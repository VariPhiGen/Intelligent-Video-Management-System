"""Opt-in verification of the real Redis fencing scripts against scratch Redis."""
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from backend.security import Principal
from backend.services.search_assistant import store

pytestmark = pytest.mark.skipif(not os.environ.get('ASSISTANT_TEST_REDIS'), reason='requires isolated scratch Redis')


@pytest.mark.asyncio
async def test_fenced_save_owner_expiry_delete_and_release(monkeypatch):
    import redis.asyncio as redis
    r = redis.from_url(os.environ['ASSISTANT_TEST_REDIS'], decode_responses=True)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(store, 'get_redis', AsyncMock(return_value=r))
    principal = Principal('test-'+str(uuid4()), ['admin'], 'user', 'test-session')
    state = await store.create(principal, 'UTC')
    key = store.key(state['id'])
    try:
        token = await store.acquire(state['id'])
        state['version'] = 1
        await store.save(state, token)
        assert (await store.read(state['id'], principal))['version'] == 1
        assert 1790 < await r.ttl(key) <= 1800
        with pytest.raises(HTTPException):
            await store.acquire(state['id'])
        with pytest.raises(HTTPException):
            await store.save(state, 'not-the-owner')
        # A late release cannot remove the next worker's progress.
        await store.progress(state['id'], 'Counting events…')
        await store.release(state['id'], 'not-the-owner')
        assert await r.get(key+':progress') == 'Counting events…'
        await store.release(state['id'], token)
        assert await r.get(key+':progress') is None
        token = await store.acquire(state['id'])
        await r.delete(key)
        with pytest.raises(HTTPException):
            await store.save(state, token)
    finally:
        await r.delete(key, key+':lease', key+':progress')
        await r.aclose()
