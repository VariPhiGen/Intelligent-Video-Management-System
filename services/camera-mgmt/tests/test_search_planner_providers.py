import json

import httpx
import pytest
from pydantic import SecretStr

from backend.services.search_planner import service
from backend.services.search_planner.providers import ADAPTERS
from backend.services.search_planner.providers.base import Runtime
from backend.services.search_planner.schemas import PlannerError
from test_search_planner import intent


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['qwen', 'openai', 'anthropic'])
async def test_adapter_uses_native_schema_and_isolates_credentials(provider):
    payloads = []
    raw = intent(camera_refs=[]).model_dump_json()
    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if provider == 'openai':
            assert str(request.url) == 'https://api.openai.com/v1/responses'
            assert payload['text']['format']['strict'] is True
            assert payload['store'] is False
            return httpx.Response(200, json={'status': 'completed', 'output': [
                {'content': [{'type': 'output_text', 'text': raw}]}]})
        if provider == 'anthropic':
            assert str(request.url) == 'https://api.anthropic.com/v1/messages'
            assert request.headers['x-api-key'] == 'only-this-provider-key'
            assert 'Authorization' not in request.headers
            assert payload['output_config']['format']['type'] == 'json_schema'
            return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': raw}]})
        assert str(request.url) == 'http://local:8000/v1/chat/completions'
        assert payload['response_format']['json_schema']['strict'] is True
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]})
    runtime = Runtime(provider, 'chosen-model', 'http://local:8000/v1', SecretStr('only-this-provider-key'), 5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await ADAPTERS[provider](client, runtime, 'system', 'user') == raw
    assert payloads[0]['model'] == 'chosen-model'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 429, 500, 400])
async def test_errors_never_expose_provider_body(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={'error': 'SENSITIVE PROVIDER BODY'}))) as client:
        with pytest.raises(PlannerError) as exc:
            await ADAPTERS['openai'](client, Runtime('openai', 'model', '', SecretStr('secret'), 5), '', '')
    assert 'SENSITIVE' not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [
    {'status': 'incomplete'},
    {'status': 'completed', 'output': [{'content': [{'type': 'refusal'}]}]},
])
async def test_openai_incomplete_or_refused_output_is_not_a_plan(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
        with pytest.raises(PlannerError):
            await ADAPTERS['openai'](client, Runtime('openai', 'model', '', SecretStr('secret'), 5), '', '')


@pytest.mark.asyncio
async def test_malformed_extraction_has_one_correction_attempt(monkeypatch):
    count = 0
    async def broken(*args):
        nonlocal count
        count += 1
        return '{}'
    monkeypatch.setitem(ADAPTERS, 'qwen', broken)
    with pytest.raises(PlannerError, match='reliably'):
        await service.extract(Runtime('qwen', 'model', 'http://local/v1', SecretStr(''), 5), 'person', 'people', 'UTC')
    assert count == 2


@pytest.mark.asyncio
async def test_connection_test_rejects_structurally_valid_but_wrong_interpretation(monkeypatch):
    async def wrong(*args):
        return intent(domain='vehicles').model_dump_json()
    monkeypatch.setitem(ADAPTERS, 'qwen', wrong)
    with pytest.raises(PlannerError, match='sample'):
        await service.test_connection(Runtime('qwen', 'model', 'http://local/v1', SecretStr(''), 5))


@pytest.mark.asyncio
async def test_timeout_is_normalized(monkeypatch):
    async def timeout(*args):
        raise httpx.ReadTimeout('SENSITIVE connection details')
    monkeypatch.setitem(ADAPTERS, 'qwen', timeout)
    with pytest.raises(PlannerError, match='timed out') as exc:
        await service.extract(Runtime('qwen', 'model', 'http://local/v1', SecretStr(''), 5), 'person', 'people', 'UTC')
    assert exc.value.status == 504


async def _capture(provider, effort, response):
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response)
    runtime = Runtime(provider, 'model', '', SecretStr('k'), 5, effort)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        try:
            await ADAPTERS[provider](client, runtime, 'system', 'user')
        except PlannerError:
            pass
    return seen[0]


@pytest.mark.asyncio
async def test_low_effort_keeps_cloud_reasoning_short_and_budget_roomy():
    ok = {'stop_reason': 'end_turn', 'content': []}
    claude = await _capture('anthropic', 'low', ok)
    assert claude['output_config']['effort'] == 'low'
    assert claude['max_tokens'] >= 8192
    openai = await _capture('openai', 'low', {'status': 'completed', 'output': []})
    assert openai['reasoning'] == {'effort': 'low'}
    assert openai['max_output_tokens'] >= 8192


@pytest.mark.asyncio
async def test_model_default_effort_sends_no_effort_option():
    claude = await _capture('anthropic', 'default', {'stop_reason': 'end_turn', 'content': []})
    assert 'effort' not in claude['output_config']
    openai = await _capture('openai', 'default', {'status': 'completed', 'output': []})
    assert 'reasoning' not in openai


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,data', [
    ('anthropic', {'stop_reason': 'max_tokens', 'content': []}),
    ('openai', {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}),
])
async def test_exhausted_output_budget_says_what_to_change(provider, data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as client:
        with pytest.raises(PlannerError, match='reasoning effort') as exc:
            await ADAPTERS[provider](client, Runtime(provider, 'm', '', SecretStr('k'), 5), '', '')
    assert exc.value.status == 502
