from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.routers import search, search_planner as routes
from backend.services.search_planner import settings as planner_settings, service
from backend.services.search_planner.schemas import PlannerConfig, PlannerError, Profile
from test_search_planner import intent, CAMERAS


@pytest.fixture
def admin(mint):
    return mint(roles=['admin'])


@pytest.fixture
def viewer(mint):
    return mint(roles=['viewer'])


@pytest.fixture
def planner(monkeypatch):
    cfg = PlannerConfig(enabled=True, profiles={'qwen': Profile(model='test')})
    runtime = SimpleNamespace(model='test')
    monkeypatch.setattr(planner_settings, 'row', AsyncMock(return_value=None))
    monkeypatch.setattr(planner_settings, 'effective', lambda *args: SimpleNamespace(
        config=cfg, runtime=runtime, public={'tested': True}))
    monkeypatch.setattr(search, '_registered', AsyncMock(return_value=CAMERAS))
    monkeypatch.setattr(routes.search_scope, 'recorder_cameras', AsyncMock(return_value=None))
    extract = AsyncMock(return_value=intent(camera_refs=['cam2'], time={
        'kind': 'recent', 'source': 'last ten minutes', 'recent_minutes': 10}))
    monkeypatch.setattr(service, 'extract', extract)
    execute = AsyncMock(return_value={'results': [], 'withheld': {}, 'matched': 0, 'truncated': False})
    monkeypatch.setattr(search, 'execute_people', execute)
    monkeypatch.setattr(routes.audit, 'record', AsyncMock())
    return extract, execute


@pytest.mark.asyncio
async def test_natural_route_reuses_existing_executor_with_resolved_scope(client, admin, planner):
    extract, execute = planner
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person on cam2 last ten minutes', 'domain': 'people', 'timezone': 'Asia/Kolkata'})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'results'
    assert execute.await_args.kwargs['camera_slugs'] == ['gate-123']
    query = execute.await_args.args[0]
    assert query.query == 'person wearing a red shirt'
    assert query.time_from and query.time_to
    assert 'SearchPlan' not in r.text


@pytest.mark.asyncio
async def test_unknown_camera_returns_clarification_without_search(client, admin, planner):
    planner[0].return_value = intent(camera_refs=['does-not-exist'])
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person on unknown camera', 'domain': 'people', 'timezone': 'UTC'})
    assert r.status_code == 200
    assert r.json()['status'] == 'clarification'
    planner[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_timezone_never_invokes_provider(client, admin, planner):
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person', 'domain': 'people', 'timezone': '../invalid'})
    assert r.json()['status'] == 'clarification'
    planner[0].assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_request_never_runs_vector_search(client, admin, planner):
    planner[0].return_value = intent(unsupported=['handing over a bag'])
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person handing over a bag', 'domain': 'people', 'timezone': 'UTC'})
    assert r.json()['status'] == 'unsupported'
    planner[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_planner_error_distinguished_from_search_index_failure(client, admin, planner):
    planner[0].side_effect = PlannerError('Provider unavailable')
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person', 'domain': 'people', 'timezone': 'UTC'})
    assert r.status_code == 503
    assert r.json()['detail']['code'] == 'planner_unavailable'
    planner[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_settings_require_admin_before_provider_use(client, viewer, planner):
    for path in ('settings', 'test'):
        r = await client.request('GET' if path == 'settings' else 'POST',
            '/api/search/planner/' + path, headers=viewer,
            **({} if path == 'settings' else {'json': {'config': {}}}))
        assert r.status_code == 403
    planner[0].assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthenticated_cannot_invoke_planner(client, planner):
    r = await client.post('/api/search/natural', json={
        'query': 'person', 'domain': 'people', 'timezone': 'UTC'})
    assert r.status_code == 401
    planner[0].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('test_only', [False, True])
async def test_failed_connection_never_persists_settings(monkeypatch, test_only):
    from unittest.mock import Mock
    from fastapi import HTTPException
    from backend.services.search_planner.schemas import SettingsUpdate
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(), add=Mock())
    cfg = PlannerConfig(enabled=True)
    monkeypatch.setattr(planner_settings, 'row', AsyncMock(return_value=None))
    monkeypatch.setattr(planner_settings, 'effective', lambda *args: SimpleNamespace(config=cfg, runtime=object()))
    monkeypatch.setattr(planner_settings, 'validate_config', lambda *args: None)
    monkeypatch.setattr(planner_settings, 'encrypted_updates', lambda *args: {})
    monkeypatch.setattr(planner_settings, 'fingerprint', lambda *args: 'new')
    monkeypatch.setattr(service, 'test_connection', AsyncMock(side_effect=PlannerError('Bad provider')))
    with pytest.raises(HTTPException) as exc:
        await routes.update_settings(SettingsUpdate(config=cfg), None, db, None, test_only=test_only)
    assert exc.value.status_code == 503
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_no_camera_in_query_searches_unscoped_like_explicit_search(client, admin, planner):
    planner[0].return_value = intent(camera_refs=[])
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person in red', 'domain': 'people', 'timezone': 'UTC'})
    assert r.json()['status'] == 'results'
    assert planner[1].await_args.kwargs['camera_slugs'] is None
    # The inventory fetched during interpretation is reused, not fetched again.
    assert planner[1].await_args.kwargs['searchable'] == (CAMERAS, None)


@pytest.mark.asyncio
async def test_vehicle_cameras_resolve_against_the_registry_not_the_recorder(client, admin, planner, monkeypatch):
    recorder = AsyncMock(return_value={})  # recorder up, recording nothing
    monkeypatch.setattr(routes.search_scope, 'recorder_cameras', recorder)
    vehicles = AsyncMock(return_value={'results': [], 'withheld': {}, 'matched': 0, 'truncated': False})
    monkeypatch.setattr(search, 'execute_vehicles', vehicles)
    planner[0].return_value = intent(domain='vehicles', visual_query='red car', camera_refs=['cam2'])
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'red car on cam2', 'domain': 'vehicles', 'timezone': 'UTC'})
    assert r.json()['status'] == 'results', r.text
    assert vehicles.await_args.kwargs['camera_slugs'] == ['gate-123']
    assert vehicles.await_args.kwargs['registered'] == CAMERAS
    recorder.assert_not_awaited()


@pytest.mark.asyncio
async def test_people_cameras_still_require_recordings(client, admin, planner, monkeypatch):
    monkeypatch.setattr(routes.search_scope, 'recorder_cameras', AsyncMock(return_value={'yard-456': {}}))
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person on cam2', 'domain': 'people', 'timezone': 'UTC'})
    assert r.json()['status'] == 'clarification'
    planner[1].assert_not_awaited()


@pytest.mark.asyncio
async def test_recorder_lookup_is_cancelled_when_interpretation_fails(client, admin, planner, monkeypatch):
    import asyncio
    cancelled = asyncio.Event()
    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
    monkeypatch.setattr(routes.search_scope, 'recorder_cameras', slow)
    async def fails(*args):
        await asyncio.sleep(0)  # let the inventory task start, as a real provider call would
        raise PlannerError('Provider unavailable')
    planner[0].side_effect = fails
    r = await client.post('/api/search/natural', headers=admin, json={
        'query': 'person', 'domain': 'people', 'timezone': 'UTC'})
    assert r.status_code == 503
    await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('keys', [
    {'openai': 'sk-LEAKCHECK' + 'x' * 5000},      # too long
    {'not-a-provider': 'sk-LEAKCHECK'},           # unknown provider
    {'openai': ['sk-LEAKCHECK']},                 # wrong type
])
async def test_rejected_settings_never_echo_the_key(client, admin, planner, keys):
    for method, path in (('PUT', 'settings'), ('POST', 'test')):
        r = await client.request(method, '/api/search/planner/' + path, headers=admin,
                                 json={'config': {}, 'keys': keys})
        assert r.status_code == 422
        assert 'LEAKCHECK' not in r.text
    planner[0].assert_not_awaited()


def _settings_db(order):
    from unittest.mock import Mock
    async def execute(*args):
        order.append('lock')
    async def rollback():
        order.append('rollback')
    return SimpleNamespace(execute=execute, rollback=rollback, commit=AsyncMock(), add=Mock())


def _patch_settings(monkeypatch, fingerprints, order):
    from backend.services.search_planner.schemas import SettingsUpdate
    cfg = PlannerConfig(enabled=True)
    monkeypatch.setattr(planner_settings, 'row', AsyncMock(return_value=None))
    monkeypatch.setattr(planner_settings, 'effective', lambda *args: SimpleNamespace(
        config=cfg, runtime=object(), public={'tested': True}))
    monkeypatch.setattr(planner_settings, 'validate_config', lambda *args: None)
    monkeypatch.setattr(planner_settings, 'encrypted_updates', lambda *args: {})
    monkeypatch.setattr(planner_settings, 'fingerprint', lambda *args: fingerprints.pop(0))
    async def tested(*args):
        order.append('provider')
    monkeypatch.setattr(service, 'test_connection', tested)
    monkeypatch.setattr(routes.audit, 'record', AsyncMock())
    return SettingsUpdate(config=cfg)


@pytest.mark.asyncio
async def test_provider_test_runs_with_no_transaction_or_lock_held(monkeypatch):
    order = []
    body = _patch_settings(monkeypatch, ['new', 'new'], order)
    await routes.update_settings(body, None, _settings_db(order), None)
    assert order == ['rollback', 'provider', 'lock']


@pytest.mark.asyncio
async def test_test_only_never_takes_the_lock(monkeypatch):
    order = []
    body = _patch_settings(monkeypatch, ['new'], order)
    assert await routes.update_settings(body, None, _settings_db(order), None, test_only=True) == {'ok': True}
    assert 'lock' not in order


@pytest.mark.asyncio
async def test_settings_changed_during_the_test_are_not_saved(monkeypatch):
    from fastapi import HTTPException
    order = []
    body = _patch_settings(monkeypatch, ['tested', 'changed-meanwhile'], order)
    db = _settings_db(order)
    with pytest.raises(HTTPException) as exc:
        await routes.update_settings(body, None, db, None)
    assert exc.value.status_code == 409
    db.commit.assert_not_awaited()
