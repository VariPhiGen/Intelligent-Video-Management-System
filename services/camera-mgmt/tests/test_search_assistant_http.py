import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.routers import search_assistant as routes
from backend.services.search_assistant import service, store
from backend.services.search_planner.schemas import PlannerConfig
from test_search_assistant import CAMERAS, CATALOG


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def get(self, key):
        return self.values.get(key)

    async def expire(self, key, seconds):
        self.expiries[key] = seconds

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def eval(self, script, n, *args):
        if script == store.COMMIT:
            key, lock, token, data, ttl = args
            if self.values.get(lock) != token or key not in self.values:
                return 0
            await self.set(key, data, ex=ttl)
            return 1
        key, progress, token = args
        if self.values.get(key) == token:
            await self.delete(key, progress)
            return 1
        return 0


@pytest.fixture
def rig(monkeypatch):
    redis = MemoryRedis()
    monkeypatch.setattr(store, 'get_redis', AsyncMock(return_value=redis))
    monkeypatch.setattr(routes.settings, 'row', AsyncMock(return_value=None))
    eff = SimpleNamespace(config=PlannerConfig(assistant_enabled=True), public={'assistant_tested': True}, runtime=SimpleNamespace(provider='qwen'))
    monkeypatch.setattr(routes.settings, 'effective', lambda *a: eff)
    monkeypatch.setattr(routes.search, '_registered', AsyncMock(return_value=CAMERAS))
    monkeypatch.setattr(service, 'catalog', AsyncMock(return_value=CATALOG))
    monkeypatch.setattr(routes.audit, 'record', AsyncMock())
    run = AsyncMock(return_value=({'status': 'results', 'message': '0 records.', 'collections': []}, {'trace_id': 'trace'}))
    monkeypatch.setattr(service, 'run', run)
    return redis, eff, run


async def create(client, headers):
    response = await client.post('/api/search/assistant/conversations', headers=headers, json={'timezone': 'UTC'})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_disabled_and_permission_gates(client, mint, rig):
    viewer = mint(roles=['viewer'])
    r = await client.post('/api/search/assistant/conversations', headers=viewer, json={'timezone': 'UTC'})
    assert r.status_code == 403
    rig[1].config.assistant_enabled = False
    r = await client.post('/api/search/assistant/conversations', headers=mint(roles=['admin']), json={'timezone': 'UTC'})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_owner_isolation_restore_expiry(client, mint, rig):
    admin = mint(roles=['admin'], subject='owner')
    c = await create(client, admin)
    path = '/api/search/assistant/conversations/'+c['id']
    r = await client.get(path, headers=mint(roles=['admin'], subject='other'))
    assert r.status_code == 404
    r = await client.get(path, headers=admin)
    assert r.status_code == 200 and 'owner' not in r.json()
    await rig[0].delete(store.key(c['id']))
    assert (await client.get(path, headers=admin)).status_code == 410


@pytest.mark.asyncio
async def test_version_and_idempotency_and_conflicting_request_id(client, mint, rig):
    admin = mint(roles=['admin'])
    c = await create(client, admin)
    path = '/api/search/assistant/conversations/'+c['id']+'/turns'
    body = {'message': 'count events', 'expected_version': 0, 'request_id': str(uuid4())}
    r = await client.post(path, headers=admin, json=body)
    assert r.status_code == 200, r.text
    assert r.json()['version'] == 1
    retry = await client.post(path, headers=admin, json=body)
    assert retry.status_code == 200 and retry.json()['answer'] == r.json()['answer']
    assert rig[2].await_count == 1
    assert (await client.post(path, headers=admin, json={**body, 'message': 'different'})).status_code == 409
    assert (await client.post(path, headers=admin, json={**body, 'request_id': str(uuid4())})).status_code == 409


@pytest.mark.asyncio
async def test_active_turn_lease_refuses_concurrent_request(client, mint, rig):
    admin = mint(roles=['admin'])
    c = await create(client, admin)
    await rig[0].set(store.key(c['id'])+':lease', 'another-turn')
    r = await client.post('/api/search/assistant/conversations/'+c['id']+'/turns', headers=admin,
        json={'message': 'count events', 'expected_version': 0, 'request_id': str(uuid4())})
    assert r.status_code == 409
    rig[2].assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_failure_returns_restart_message(client, mint, rig, monkeypatch):
    monkeypatch.setattr(store, 'get_redis', AsyncMock(side_effect=RedisConnectionError()))
    r = await client.post('/api/search/assistant/conversations', headers=mint(roles=['admin']), json={'timezone': 'UTC'})
    assert r.status_code == 503 and 'storage' in r.text


@pytest.mark.asyncio
async def test_every_executed_turn_is_audited_without_question(client, mint, rig):
    admin = mint(roles=['admin'])
    c = await create(client, admin)
    await client.post('/api/search/assistant/conversations/'+c['id']+'/turns', headers=admin,
        json={'message': 'private operator question', 'expected_version': 0, 'request_id': str(uuid4())})
    assert routes.audit.record.await_args.args[2] == 'smartsearch.assistant.turn'
    assert 'private operator question' not in repr(routes.audit.record.await_args.kwargs)


@pytest.mark.asyncio
async def test_permission_revocation_invalidates_old_results():
    state = {'confirmed': [{'tool': 'count_events', 'cameras': None}], 'exchanges': [], 'latest': []}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await service.revalidate(state, AsyncMock(), CAMERAS, ['search_people'])
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_different_login_session_cannot_restore_context(client, mint, rig):
    first = mint(roles=['admin'], session_id='first-session')
    c = await create(client, first)
    second = mint(roles=['admin'], session_id='second-session')
    r = await client.get('/api/search/assistant/conversations/'+c['id'], headers=second)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_enabling_requires_assistant_evaluation_before_save(monkeypatch):
    from unittest.mock import Mock
    from backend.routers import search_planner
    from backend.services.search_planner.schemas import SettingsUpdate, PlannerError
    eff = SimpleNamespace(config=PlannerConfig(assistant_enabled=True), runtime=object(), public={})
    monkeypatch.setattr(search_planner.planner_settings, 'row', AsyncMock(return_value=None))
    monkeypatch.setattr(search_planner.planner_settings, 'effective', lambda *a: eff)
    monkeypatch.setattr(search_planner.planner_settings, 'fingerprint', lambda *a: 'single-query')
    monkeypatch.setattr(search_planner.planner_settings, 'assistant_fingerprint', lambda *a: 'assistant-config')
    monkeypatch.setattr(search_planner.planner_settings, 'encrypted_updates', lambda *a: {})
    monkeypatch.setattr(search_planner.audit, 'record', AsyncMock())
    evaluation = AsyncMock(side_effect=PlannerError('Assistant failed evaluation', 422))
    monkeypatch.setattr(search_planner.assistant_evaluation, 'evaluate', evaluation)
    db = AsyncMock()
    db.add = Mock()
    body = SettingsUpdate(config=eff.config)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await search_planner.update_settings(body, None, db, None)
    db.commit.assert_not_awaited()
    evaluation.side_effect = None
    await search_planner.update_settings(body, None, db, None)
    assert db.add.call_args.args[0].assistant_tested_fingerprint == 'assistant-config'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_audit_records_this_turns_operations_and_replays_once(client, mint, rig):
    admin = mint(roles=['admin'])
    c = await create(client, admin)
    ran = [{'tool': 'count_events', 'cameras': ['gate-1'], 'activities': ['intrusion'], 'time_from': 'a', 'time_to': 'b'}]
    rig[2].return_value = ({'status': 'results', 'message': '', 'collections': []}, {'trace_id': 't', 'operations': ran})
    body = {'message': 'count intrusions', 'expected_version': 0, 'request_id': str(uuid4())}
    path = '/api/search/assistant/conversations/'+c['id']+'/turns'
    await client.post(path, headers=admin, json=body)
    detail = routes.audit.record.await_args.kwargs['detail']
    assert detail['operations'] == ran and not detail.get('replay')
    await client.post(path, headers=admin, json=body)
    detail = routes.audit.record.await_args.kwargs['detail']
    assert detail['replay'] is True and detail['operations'] == []
