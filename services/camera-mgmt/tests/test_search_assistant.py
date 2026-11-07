import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from backend.services.search_assistant import grounding, interpret, service, tools
from backend.services.search_assistant.schemas import Interpretation, Operation, output_schema
from backend.services.search_planner.schemas import Clarify, PlannerConfig, PlannerError
from backend.services.search_planner.providers.base import Runtime

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
CATALOG = {'intrusion': {'label': 'Intrusion', 'color': '#ff0000', 'search_aliases': ['trespass']},
           'idle_worker': {'label': 'Idle Worker', 'color': '#ff0000', 'search_aliases': []},
           'vehicle_stop': {'label': 'Vehicle Stop', 'color': '#ff0000', 'search_aliases': ['vehicle events']},
           'vehicle_entry': {'label': 'Vehicle Entry', 'color': '#ff0000', 'search_aliases': ['vehicle events']}}
CAMERAS = [SimpleNamespace(id=uuid4(), slug=f'gate-{i}', name=f'Gate {i}',
                           camera_metadata={'zone': 'Entrance'}, retention_minutes=90*1440,
                           enabled=True, recording=True, analytics_config={'activities': [{'type': 'intrusion'}]}) for i in (1, 2)]


def none_time(**kw):
    return {'kind': 'none', 'source': '', 'date': None, 'day_offset': None, 'start_time': None,
            'end_date': None, 'end_day_offset': None, 'end_time': None, 'recent_minutes': None, **kw}


def operation(**kwargs):
    return Operation.model_validate({'tool': 'count_events', 'cameras': {'action': 'keep', 'values': []},
        'zones': {'action': 'keep', 'values': []}, 'activities': {'action': 'keep', 'source': '', 'keys': []},
        'appearance': {'action': 'keep', 'value': ''}, 'plate': {'action': 'keep', 'value': ''},
        'time': {'action': 'keep', 'value': none_time()}, 'grouping': 'keep', 'sort': 'keep', 'subject': '', **kwargs})


def intent(op=None, **kwargs):
    return Interpretation.model_validate({'mode': 'new', 'operations': [op or operation()],
         'result_position': None, 'unsupported': [], 'clarification': None, **kwargs})


def ground(i, query, previous=None, **kw):
    return grounding.ground(i, query, previous or [], CAMERAS, CATALOG, PlannerConfig(), 'UTC', now=NOW, **kw)


def test_all_words_are_required_for_activity_match():
    assert interpret.every_word_match('idle wrker', 'Idle Worker')
    assert not interpret.every_word_match('person carrying ladder', 'Person')
    assert grounding.activity_matches('vehicle events', CATALOG) == (['vehicle_stop', 'vehicle_entry'], True)


def test_camera_references_do_not_silently_fuzzy_resolve():
    with pytest.raises(grounding.ChoiceNeeded) as exc:
        ground(intent(operation(cameras={'action': 'set', 'values': ['Gat 1']})), 'count events at Gat 1')
    assert exc.value.field == 'cameras'
    assert exc.value.options[0]['values'] == ['gate-1']


def test_unknown_activity_offers_ids_and_a_direct_override():
    op = operation(activities={'action': 'set', 'source': 'unauthorised entry', 'keys': []})
    with pytest.raises(grounding.ChoiceNeeded) as exc:
        ground(intent(op), 'count unauthorised entry events')
    assert all(c['id'] for c in exc.value.options)
    result = ground(intent(op), 'count unauthorised entry events', overrides={'0:activities:unauthorised entry': ['intrusion']})
    assert result[0]['activities'] == ['intrusion']


def test_catalog_synonym_is_visible():
    op = operation(activities={'action': 'set', 'source': 'unauthorised entry', 'keys': ['intrusion']})
    plan = ground(intent(op), 'count unauthorised entry events')[0]
    assert plan['activities'] == ['intrusion']
    assert 'Interpreted' in plan['interpretations'][0]


def test_leftover_constraint_blocks_counting_all_events():
    op = operation(activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']})
    with pytest.raises(Clarify, match='ladder'):
        ground(intent(op), 'count intrusion events involving a ladder')


def test_explicit_subject_blocks_execution():
    op = operation(subject='involving a ladder')
    with pytest.raises(Clarify, match='not supported'):
        ground(intent(op), 'count events involving a ladder')


def test_source_validation_preserves_punctuation():
    query = 'count events at [D25] Counter View yesterday'
    op = operation(cameras={'action': 'set', 'values': ['[D25] Counter View']},
        time={'action': 'set', 'value': none_time(kind='day', source='yesterday', day_offset=-1)})
    interpret.validate_sources(intent(op), query, CATALOG)
    with pytest.raises(ValueError):
        interpret.validate_sources(intent(op), 'count events at Counter', CATALOG)


def test_followups_add_remove_and_keep_filters():
    first = ground(intent(operation(cameras={'action': 'set', 'values': ['Gate 1']},
        activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']})), 'count intrusion events at Gate 1')
    both = ground(intent(operation(cameras={'action': 'add', 'values': ['Gate 2']}), mode='refine'), 'also Gate 2', first)
    assert both[0]['cameras'] == ['gate-1', 'gate-2']
    assert both[0]['activities'] == ['intrusion']
    removed = ground(intent(operation(cameras={'action': 'remove', 'values': ['Gate 2']}), mode='refine'), 'not Gate 2', both)
    assert removed[0]['cameras'] == ['gate-1']
    assert removed[0]['time_from'] == first[0]['time_from']


def test_self_contained_question_replaces_previous_filters():
    old = ground(intent(operation(cameras={'action': 'set', 'values': ['Gate 1']})), 'count events at Gate 1')
    op = operation(activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']},
        time={'action': 'set', 'value': none_time(kind='day', source='yesterday', day_offset=-1)})
    result = ground(intent(op, mode='refine'), 'count intrusion events yesterday', old)
    assert result[0]['cameras'] is None


def test_latest_search_defaults_to_full_retention():
    plan = ground(intent(operation(tool='search_events', sort='latest')), 'show latest events')[0]
    assert (NOW-datetime.fromisoformat(plan['time_from'])).days == 90
    count = ground(intent(), 'count events')[0]
    assert (NOW-datetime.fromisoformat(count['time_from'])).days == 1


def test_visual_event_correlation_is_rejected():
    op = operation(appearance={'action': 'set', 'value': 'red shirt'},
        activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']})
    with pytest.raises(Clarify, match='same object'):
        ground(intent(op), 'count intrusion events with red shirt')


def test_collectives_are_grouping_not_camera_names():
    op = operation(cameras={'action': 'set', 'values': ['all cameras']}, grouping='camera')
    plan = ground(intent(op), 'count events on all cameras')[0]
    assert plan['cameras'] is None and plan['grouping'] == 'camera'


@pytest.mark.parametrize('reply,expected', [('yes', 'ambiguous'), ('the second one', 'b'), ('option 1', 'a'),
    ('Gate 1', 'a'), ('no', 'cancel'), ('count falls today', None)])
def test_pending_clarification_typed_replies(reply, expected):
    assert service.typed_choice(reply, [{'id': 'a', 'label': 'Gate 1'}, {'id': 'b', 'label': 'Gate 2'}]) == expected


def test_only_one_choice_accepts_yes():
    assert service.typed_choice('yes', [{'id': 'a', 'label': 'Intrusion'}]) == 'a'


@pytest.mark.parametrize('plate', ['KA01AB1234', 'ka01ab1234', 'KA 01 AB 1234', '123456', 'ABC-123'])
def test_privacy_masks_plates_cameras_and_credentials(plate):
    query = f'Find plate {plate} at Gate 1 yesterday rtsp://user:pass@192.168.31.11/path'
    masked, mapping = interpret.redact(query, CAMERAS, True)
    assert plate not in masked and 'Gate 1' not in masked and '192.168' not in masked and 'pass@' not in masked
    assert interpret.restore(masked, mapping) == query


def test_schema_enum_has_only_live_activity_keys():
    assert output_schema(CATALOG)['$defs']['Activities']['properties']['keys']['items']['enum'] == list(CATALOG)


def test_event_query_is_retained_registered_scoped_and_half_open():
    plan = ground(intent(), 'count events')[0]
    plan['activities'] = ['intrusion']
    sql = str(tools.event_query(plan, CAMERAS).compile(dialect=postgresql.dialect()))
    assert 'expires_at > now()' in sql and 'cameras.stage =' in sql
    assert 'camera_id IN' in sql and 'activity IN' in sql
    assert 'started_at < ' in sql
    assert 'LIMIT' not in sql


@pytest.mark.asyncio
async def test_invalid_source_retries_once_without_search(monkeypatch):
    extract = AsyncMock(return_value=intent(operation(cameras={'action': 'set', 'values': ['invented']})).model_dump_json())
    monkeypatch.setitem(interpret.ADAPTERS, 'qwen', extract)
    with pytest.raises(PlannerError):
        await interpret.extract(Runtime('qwen', 'test', 'http://local', SecretStr(''), 5), 'count events', [], CAMERAS, CATALOG, 'UTC')
    assert extract.await_count == 2


@pytest.mark.asyncio
async def test_provider_receives_no_history_results_names_or_plate(monkeypatch):
    op = operation(plate={'action': 'set', 'value': 'PLATE_1'}, tool='lookup_plate')
    async def respond(client, runtime, system, user, schema):
        payload = json.loads(user)
        assert 'KA01AB1234' not in user and 'Gate 1' not in user and 'tool secret' not in user
        token = re.search(r'PLATE_\d+', payload['message']).group()
        op.plate.value = token
        return intent(op).model_dump_json()
    import re
    monkeypatch.setitem(interpret.ADAPTERS, 'openai', respond)
    result, _ = await interpret.extract(Runtime('openai', 'test', '', SecretStr('key'), 5), 'Find plate KA01AB1234',
        [{'tool': 'lookup_plate', 'plate': 'KA01AB1234', 'cameras': ['Gate 1'], 'reply': 'tool secret'}], CAMERAS, CATALOG, 'UTC')
    assert result.operations[0].plate.value == 'KA01AB1234'


@pytest.mark.parametrize('query', ['count people', 'count events involving people', 'count events yesterday', 'count events last hour'])
def test_omitted_subject_or_time_cannot_become_all_event_count(query):
    with pytest.raises(Clarify):
        ground(intent(), query)


@pytest.mark.asyncio
async def test_actual_orchestrator_keeps_empty_scope_and_followup_changes(monkeypatch):
    from backend.security import Principal
    from backend.services.search_assistant.schemas import Turn
    from backend.services.search_assistant import store
    state = {'id': str(uuid4()), 'timezone': 'UTC', 'confirmed': [], 'pending': None, 'latest': [], 'exchanges': []}
    op = operation(cameras={'action': 'set', 'values': ['Gate 1']},
                   activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']})
    parser = AsyncMock(return_value=(intent(op), {}))
    monkeypatch.setattr(interpret, 'extract', parser)
    monkeypatch.setattr(store, 'progress', AsyncMock())
    async def execute(plan, *args):
        return {'tool': plan['tool'], 'scope': plan, 'items': [], 'count': 0, 'warnings': [], 'next_cursor': None}
    executor = AsyncMock(side_effect=execute)
    monkeypatch.setattr(tools, 'execute', executor)
    eff = SimpleNamespace(runtime=object(), config=PlannerConfig())
    principal = Principal('operator', ['admin'], 'user')
    body = Turn(message='count intrusion events at Gate 1', request_id=uuid4(), expected_version=0)
    answer, _ = await service.run(state, body, eff, principal, None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert answer['status']=='results'
    assert state['confirmed'][0]['cameras'] == ['gate-1']
    assert executor.await_count == 1  # No automatic widening after the zero count.
    parser.return_value = (intent(operation(cameras={'action': 'add', 'values': ['Gate 2']}), mode='refine'), {})
    body.message = 'also Gate 2'
    await service.run(state, body, eff, principal, None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert state['confirmed'][0]['cameras'] == ['gate-1', 'gate-2']
    assert state['confirmed'][0]['activities'] == ['intrusion']


@pytest.mark.asyncio
async def test_clarification_choice_does_not_call_model(monkeypatch):
    from backend.security import Principal
    from backend.services.search_assistant.schemas import Turn
    from backend.services.search_assistant import store
    state = {'id': str(uuid4()), 'timezone': 'UTC', 'confirmed': [], 'pending': None, 'latest': [], 'exchanges': []}
    parser = AsyncMock(return_value=(intent(operation(cameras={'action': 'set', 'values': ['Gat 1']})), {}))
    monkeypatch.setattr(interpret, 'extract', parser)
    monkeypatch.setattr(store, 'progress', AsyncMock())
    executor = AsyncMock(return_value={'tool': 'count_events', 'items': [], 'count': 1})
    monkeypatch.setattr(tools, 'execute', executor)
    eff = SimpleNamespace(runtime=object(), config=PlannerConfig())
    principal = Principal('operator', ['admin'], 'user')
    body = Turn(message='count events at Gat 1', request_id=uuid4(), expected_version=0)
    answer, _ = await service.run(state, body, eff, principal, None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert answer['status']=='clarification'
    state['exchanges'].append({'answer': answer})
    body.choice_id = answer['choices'][0]['id']
    body.message = 'Gate 1'
    answer, _ = await service.run(state, body, eff, principal, None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert answer['status']=='results'
    assert parser.await_count == 1
    assert state['exchanges'][0]['answer']['selected_choice'] == body.choice_id


@pytest.mark.asyncio
async def test_partial_tool_failure_preserves_successful_collection(monkeypatch):
    from fastapi import HTTPException
    from backend.security import Principal
    from backend.services.search_assistant.schemas import Turn
    from backend.services.search_assistant import store
    state = {'id': str(uuid4()), 'timezone': 'UTC', 'confirmed': [], 'pending': None, 'latest': [], 'exchanges': []}
    parser = AsyncMock(return_value=(intent(operations=[operation(), operation(tool='search_events')]), {}))
    monkeypatch.setattr(interpret, 'extract', parser)
    monkeypatch.setattr(store, 'progress', AsyncMock())
    monkeypatch.setattr(tools, 'execute', AsyncMock(side_effect=[
        {'tool': 'count_events', 'items': [], 'count': 3}, HTTPException(503, 'provider secret')]))
    eff = SimpleNamespace(runtime=object(), config=PlannerConfig())
    body = Turn(message='count and show events', request_id=uuid4(), expected_version=0)
    answer, _ = await service.run(state, body, eff, Principal('test', ['admin'], 'user'), None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert answer['status']=='partial' and answer['collections'][0]['count']==3
    assert 'provider secret' not in str(answer)


def test_cloud_redaction_keeps_clock_times_interpretable():
    masked, mapping = interpret.redact('find a person at 10PM yesterday', CAMERAS, True)
    assert '10PM' in masked


@pytest.mark.asyncio
async def test_result_selection_uses_saved_evidence_id_without_provider(monkeypatch):
    from backend.security import Principal
    from backend.services.search_assistant.schemas import Turn
    state = {'id': str(uuid4()), 'timezone': 'UTC', 'confirmed': [], 'pending': None,
             'latest': [{'tool': 'search_people', 'items': [{'id': 'known', 'camera': {'slug': 'gate-1'}, 'when_ms': 1000000}]}], 'exchanges': []}
    parser = AsyncMock()
    monkeypatch.setattr(interpret, 'extract', parser)
    body = Turn(message='', request_id=uuid4(), expected_version=0, result_id='known', result_action='play')
    answer, _ = await service.run(state, body, SimpleNamespace(), Principal('test', ['admin'], 'user'), None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert answer['playback'] == {'camera': 'gate-1', 'start': 1000}
    parser.assert_not_awaited()
    body.result_id = 'missing'
    answer, _ = await service.run(state, body, SimpleNamespace(), Principal('test', ['admin'], 'user'), None, AsyncMock(), CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert answer['status'] == 'clarification'


@pytest.mark.asyncio
async def test_timezone_change_preserves_confirmed_utc_instants_without_model(monkeypatch):
    from backend.services.search_assistant.schemas import Turn
    state = {'id': str(uuid4()), 'timezone': 'UTC', 'confirmed': [{'tool': 'count_events', 'time_from': '2026-10-28T00:00:00+00:00'}],
             'pending': None, 'latest': [], 'exchanges': []}
    parser = AsyncMock()
    monkeypatch.setattr(interpret, 'extract', parser)
    body = Turn(request_id=uuid4(), expected_version=0, timezone='Asia/Kolkata')
    answer, _ = await service.run(state, body, None, None, None, None, CAMERAS, CATALOG, tools.ALL_TOOLS)
    assert state['timezone'] == 'Asia/Kolkata'
    assert state['confirmed'][0]['time_from'] == '2026-10-28T00:00:00+00:00'
    parser.assert_not_awaited()
    assert answer['status'] == 'results'


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['qwen', 'openai', 'anthropic'])
async def test_provider_adapters_use_assistant_schema_when_requested(provider):
    import httpx
    from backend.services.search_planner.providers import ADAPTERS
    wanted = output_schema(CATALOG)
    def handle(request):
        payload = json.loads(request.content)
        if provider == 'qwen':
            assert payload['response_format']['json_schema']['schema'] == wanted
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})
        if provider == 'openai':
            assert payload['text']['format']['schema'] == wanted
            return httpx.Response(200, json={'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': '{}'}]}]})
        assert payload['output_config']['format']['schema'] == wanted
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{}'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await ADAPTERS[provider](client, Runtime(provider, 'test', 'http://local/v1', SecretStr('test'), 5), '', '', wanted)


# ── Regressions found in review (29 Oct 2026) ───────────────────────────────

def day_op(**kwargs):
    return operation(time={'action': 'set', 'value': none_time(kind='day', source='yesterday', day_offset=-1)}, **kwargs)


@pytest.mark.parametrize('query,op', [
    ('what happened at Gate 1 yesterday', dict(tool='search_events', cameras={'action': 'set', 'values': ['Gate 1']})),
    ('give me intrusions at Gate 1 yesterday', dict(tool='search_events', cameras={'action': 'set', 'values': ['Gate 1']},
        activities={'action': 'set', 'source': 'intrusions', 'keys': ['intrusion']})),
    ('can you check intrusion at Gate 1 yesterday please', dict(tool='search_events', cameras={'action': 'set', 'values': ['Gate 1']},
        activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']})),
    ('where did intrusions happen yesterday?', dict(grouping='camera', activities={'action': 'set', 'source': 'intrusions', 'keys': ['intrusion']})),
    ('could you tell me what intrusions occurred yesterday', dict(tool='search_events', activities={'action': 'set', 'source': 'intrusions', 'keys': ['intrusion']})),
    ('hey, I want to see intrusions from yesterday thanks', dict(tool='search_events', activities={'action': 'set', 'source': 'intrusions', 'keys': ['intrusion']})),
])
def test_everyday_question_words_are_not_constraints(query, op):
    """Review: 'what happened at Gate 1 yesterday' asked the operator to clarify 'what'."""
    assert ground(intent(day_op(**op)), query)


def test_a_real_leftover_still_blocks_with_everyday_words_around_it():
    op = day_op(activities={'action': 'set', 'source': 'intrusions', 'keys': ['intrusion']})
    with pytest.raises(Clarify, match='ladder'):
        ground(intent(op), 'what intrusions involving a ladder happened yesterday')


@pytest.mark.parametrize('text', ['after 8.30pm', 'in the last 30mins', 'in last 48hrs', 'after 11:30 pm',
                                  'in the last 2h', 'within 15min', 'between 10am-6pm', 'at 0830hrs', 'since 9.15am'])
def test_cloud_redaction_never_hides_times_or_durations(text):
    """Review: 'after 8.30pm' reached cloud providers as 'after PLATE_1'."""
    masked, mapping = interpret.redact(f'count intrusions {text}', CAMERAS, True)
    assert text in masked and not any(t.startswith('PLATE') for t in mapping)


@pytest.mark.parametrize('text,plate', [('plate KA01AB1234 yesterday', 'KA01AB1234'),
                                        ('find MH12DE1433 after 8pm', 'MH12DE1433'),
                                        ('registration number 1234567 today', '1234567')])
def test_cloud_redaction_still_hides_plates(text, plate):
    masked, mapping = interpret.redact(text, CAMERAS, True)
    assert plate not in masked and plate in mapping.values()


def test_restore_is_exact_for_ten_or_more_placeholders():
    """Review: CAMERA_10 was restored as the first camera's name followed by '0'."""
    mapping = {f'CAMERA_{i}': f'Name{chr(64+i)}' for i in range(1, 13)}
    assert interpret.restore('CAMERA_10 and CAMERA_1 and CAMERA_12', mapping) == 'NameJ and NameA and NameL'


def test_the_word_that_does_not_make_a_question_a_refinement():
    """Review: 'intrusions that happened at Gate 1 yesterday' inherited the old activity."""
    old = ground(intent(operation(activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']})), 'count intrusion events')
    op = day_op(tool='search_events', cameras={'action': 'set', 'values': ['Gate 1']},
                activities={'action': 'set', 'source': 'idle worker', 'keys': ['idle_worker']})
    fresh = ground(intent(op, mode='refine'), 'show idle worker events that happened at Gate 1 yesterday', old)
    assert fresh[0]['activities'] == ['idle_worker']
    op = operation(tool='search_events', cameras={'action': 'set', 'values': ['Gate 1']})  # "that day" keeps the time
    plan = ground(intent(op, mode='refine'), 'what happened at Gate 1 that day', old)
    assert plan[0]['activities'] == ['intrusion'], '"that day" still refines'


@pytest.mark.asyncio
async def test_trace_names_only_what_this_turn_ran(monkeypatch):
    """Review: the audit used the investigation's confirmed filters, so a clarification
    turn was logged with the previous turn's searches."""
    monkeypatch.setattr(service.store, 'progress', AsyncMock())
    state = {'id': 'c', 'timezone': 'UTC', 'confirmed': [{'tool': 'count_events', 'cameras': ['gate-1']}],
             'latest': [], 'pending': None, 'exchanges': []}
    async def clarify(*a, **kw):
        return intent(mode='clarify', operations=[], clarification='Which camera?'), {'provider_retries': 0, 'token_usage': None}
    monkeypatch.setattr(service.interpret, 'extract', clarify)
    body = SimpleNamespace(message='which one', choice_id=None, timezone=None, result_id=None, result_action=None, remove_filter=None)
    eff = SimpleNamespace(runtime=None, config=PlannerConfig())
    answer, trace = await service.run(state, body, eff, None, None, None, CAMERAS, CATALOG, set(tools.ALL_TOOLS))
    assert answer['status'] == 'clarification' and trace['operations'] == []


def test_chips_and_groups_show_names_not_ids():
    """Review: chips read 'cameras: gate-1' and 'activities: idle_worker'."""
    labels = tools.scope_labels({'cameras': ['gate-1', 'gone'], 'activities': ['idle_worker'], 'plate': ''}, CAMERAS, CATALOG)
    assert labels == {'cameras': ['Gate 1', 'gone'], 'activities': ['Idle Worker']}
    assert tools.scope_labels({'cameras': None, 'activities': None}, CAMERAS, CATALOG) == {'cameras': None, 'activities': None}
    assert tools.group_label('camera', 'gate-2', CAMERAS, CATALOG) == 'Gate 2'
    assert tools.group_label('activity', 'vehicle_stop', CAMERAS, CATALOG) == 'Vehicle Stop'
    assert tools.group_label('day', '2026-10-28 00:00:00', CAMERAS, CATALOG) == '2026-10-28 00:00:00'


# ── The readiness evaluation, driven by a scripted model ────────────────────

from backend.services.search_assistant import evaluation  # noqa: E402


def _cams(action, *names):
    return {'action': action, 'values': list(names)}


def _act(source='intrusion', key='intrusion'):
    return {'action': 'set', 'source': source, 'keys': [key]}


def _when(source, **kw):
    return {'action': 'set', 'value': none_time(source=source, **kw)}


YESTERDAY = dict(kind='day', day_offset=-1)

#: What a capable model answers for each readiness question.
CORRECT = {
    'Find a person wearing a red shirt today': intent(operation(tool='search_people', appearance={'action': 'set', 'value': 'a red shirt'},
        time=_when('today', kind='day', day_offset=0))),
    'Find a red vehicle today': intent(operation(tool='search_vehicles', appearance={'action': 'set', 'value': 'red'},
        time=_when('today', kind='day', day_offset=0))),
    'Count intrusion events per camera yesterday': intent(operation(activities=_act(), grouping='camera', time=_when('yesterday', **YESTERDAY))),
    'Count intrusion events yesterday': intent(operation(activities=_act(), grouping='none', time=_when('yesterday', **YESTERDAY))),
    'Show the latest intrusion events': intent(operation(tool='search_events', activities=_act(), sort='latest')),
    'Count intrusion events involving a ladder': intent(mode='unsupported', operations=[], unsupported=['involving a ladder']),
    'also Gate 2': intent(operation(cameras=_cams('add', 'Gate 2')), mode='refine'),
    'not Gate 2': intent(operation(cameras=_cams('remove', 'Gate 2')), mode='refine'),
    'Show more': intent(mode='more', operations=[]),
    'Find plate KA01AB1234 yesterday': intent(operation(tool='lookup_plate', plate={'action': 'set', 'value': 'KA01AB1234'},
        time=_when('yesterday', **YESTERDAY))),
    'Which activities are configured on Gate 2?': intent(operation(tool='describe_setup', cameras=_cams('set', 'Gate 2'))),
    'Start over': intent(mode='reset', operations=[]),
    'What is the weather?': intent(mode='unsupported', operations=[], unsupported=['weather']),
    'What happened at Gate 1 yesterday?': intent(operation(tool='search_events', cameras=_cams('set', 'Gate 1'),
        time=_when('yesterday', **YESTERDAY))),
    'Count intrusion events after 8.30pm yesterday': intent(operation(activities=_act(), grouping='none',
        time=_when('after 8.30pm yesterday', kind='range', day_offset=-1, start_time='20:30:00', end_day_offset=-1, end_time='23:59:59'))),
    'Give me intrusion events from the last 30mins': intent(operation(tool='search_events', activities=_act(),
        time=_when('the last 30mins', kind='recent', recent_minutes=30))),
    'Which camera had the most intrusion events yesterday?': intent(operation(activities=_act(), grouping='camera',
        time=_when('yesterday', **YESTERDAY))),
    'Who entered Gate 1 yesterday?': intent(mode='unsupported', operations=[], unsupported=['identifying who entered']),
    'Show intrusion events that happened at Gate 1 yesterday': intent(operation(tool='search_events', cameras=_cams('set', 'Gate 1'),
        activities=_act(), time=_when('yesterday', **YESTERDAY)), mode='refine'),
}


def scripted(answers):
    async def extract(runtime, query, state, cameras, catalog, tz):
        return answers[query], {'provider_retries': 0, 'token_usage': None}
    return extract


@pytest.mark.asyncio
async def test_readiness_cases_ground_with_a_capable_model(monkeypatch):
    """Every readiness question must ground when the model answers it correctly —
    otherwise the gate fails providers for the backend's own bugs (the 'what' bug)."""
    monkeypatch.setattr(evaluation.interpret, 'extract', scripted(CORRECT))
    report = await evaluation.evaluate(None)
    assert report['ok'] and len(report['cases']) == len(CORRECT)


@pytest.mark.parametrize('query,wrong', [
    ('Count intrusion events after 8.30pm yesterday', intent(operation(activities=_act(), grouping='none', time=_when('yesterday', **YESTERDAY)))),
    ('Give me intrusion events from the last 30mins', intent(operation(tool='search_events', activities=_act(), time=_when('yesterday', **YESTERDAY)))),
    ('Which camera had the most intrusion events yesterday?', intent(operation(activities=_act(), grouping='none', time=_when('yesterday', **YESTERDAY)))),
    ('Who entered Gate 1 yesterday?', intent(operation(tool='search_events', cameras=_cams('set', 'Gate 1'), time=_when('yesterday', **YESTERDAY)))),
])
@pytest.mark.asyncio
async def test_readiness_fails_a_model_that_gets_a_new_case_wrong(monkeypatch, query, wrong):
    answers = dict(CORRECT, **{query: wrong})
    monkeypatch.setattr(evaluation.interpret, 'extract', scripted(answers))
    with pytest.raises(PlannerError):
        await evaluation.evaluate(None)


def test_ranking_words_are_answered_by_grouped_counts():
    op = day_op(activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']}, grouping='camera')
    assert ground(intent(op), 'which camera had the most intrusion events yesterday')
    op = day_op(activities={'action': 'set', 'source': 'intrusion', 'keys': ['intrusion']}, grouping='none')
    with pytest.raises(Clarify, match='most'):
        ground(intent(op), 'which camera had the most intrusion events yesterday')
