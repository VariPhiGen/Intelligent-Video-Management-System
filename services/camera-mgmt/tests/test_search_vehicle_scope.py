"""Vehicle search scoping: the explicit search is unfiltered upstream (so
unmapped rows still surface as withheld); only a planner scope narrows it."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.routers import search

CAMERAS = [SimpleNamespace(slug='gate-123', name='Gate', id=1),
           SimpleNamespace(slug='yard-456', name='Yard', id=2)]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(search, 'get_principal', AsyncMock(return_value=object()))
    monkeypatch.setattr(search.policy_svc, 'require', AsyncMock())
    monkeypatch.setattr(search, '_registered', AsyncMock(return_value=CAMERAS))
    monkeypatch.setattr(search.audit_svc, 'record', AsyncMock())
    monkeypatch.setattr(search.search_scope, 'scope', AsyncMock(return_value=search.search_scope.ScopeResult()))
    vehicles = AsyncMock(return_value=[])
    monkeypatch.setattr(search.index_client, 'search_vehicles', vehicles)
    return vehicles


def query(**patch):
    return search.VehiclesQuery(query='red car', **patch)


@pytest.mark.asyncio
async def test_explicit_search_is_unfiltered_upstream(index):
    await search.execute_vehicles(query(), None, None)
    assert index.await_args.kwargs['camera_ids'] is None


@pytest.mark.asyncio
async def test_named_camera_still_narrows_upstream(index):
    await search.execute_vehicles(query(camera='yard-456'), None, None)
    assert index.await_args.kwargs['camera_ids'] == ['yard-456']


@pytest.mark.asyncio
async def test_planner_scope_narrows_upstream_and_scoping(index):
    await search.execute_vehicles(query(), None, None, camera_slugs=['gate-123'])
    assert index.await_args.kwargs['camera_ids'] == ['gate-123']
    assert search.search_scope.scope.await_args.kwargs['cameras'] == [CAMERAS[0]]


@pytest.mark.asyncio
async def test_empty_planner_scope_never_becomes_an_unfiltered_search(index):
    out = await search.execute_vehicles(query(), None, None, camera_slugs=['retired-cam'])
    index.assert_not_awaited()
    assert out['results'] == [] and out['matched'] == 0


@pytest.mark.asyncio
async def test_caller_supplied_registry_skips_the_lookup(index):
    await search.execute_vehicles(query(), None, None, registered=CAMERAS[:1])
    search._registered.assert_not_awaited()
