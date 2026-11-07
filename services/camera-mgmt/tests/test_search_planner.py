"""Planner semantics: LLM output is never an authoritative camera/time scope."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from backend.services.search_planner.schemas import (
    Clarify, NaturalQuery, PlannerConfig, PlannerError, Profile, SearchIntent, SettingsUpdate,
)
from backend.services.search_planner.resolver import resolve
from backend.services.search_planner import settings as config

NOW = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
CAMERAS = [SimpleNamespace(slug='gate-123', name='cam2', camera_metadata={}),
           SimpleNamespace(slug='yard-456', name='Yard', camera_metadata={'search_aliases': ['cam3']})]


def intent(**patch):
    data = dict(domain='people', visual_query='person wearing a red shirt', camera_refs=['cam2'],
                clarification=None, unsupported=[], time=dict(kind='none', source='', date=None,
                day_offset=None, start_time=None, end_date=None, end_day_offset=None,
                end_time=None, recent_minutes=None))
    time = patch.pop('time', {})
    data['time'].update(time)
    data.update(patch)
    return SearchIntent.model_validate(data)


def body(**patch):
    return NaturalQuery(query='person in red', timezone='Asia/Kolkata', domain='people', **patch)


def test_yesterday_point_is_local_and_uses_documented_tolerance():
    result = resolve(intent(time={'kind': 'point', 'source': 'yesterday at 10 PM',
        'day_offset': -1, 'start_time': '22:00:00'}), body(), CAMERAS, PlannerConfig(), NOW)
    assert result.camera_slugs == ['gate-123']
    assert result.time_from == '2026-10-25T16:25:00+00:00'
    assert result.time_to == '2026-10-25T16:35:00+00:00'
    assert '±5 minutes' in result.summary


def test_default_is_bounded_and_aliases_are_exact_not_camera_order():
    plan = resolve(intent(camera_refs=['cam3']), body(), CAMERAS, PlannerConfig(), NOW)
    assert plan.camera_slugs == ['yard-456']
    assert plan.time_from == '2026-10-25T07:00:00+00:00'
    assert 'default last 24 hours' in plan.summary


@pytest.mark.parametrize('patch', [
    {'camera_refs': ['unknown']}, {'camera_refs': ['cam1']}, {'domain': 'vehicles'},
    {'time': {'kind': 'point', 'source': 'at 10PM', 'start_time': '22:00'}},
    {'time': {'kind': 'none', 'source': 'yesterday'}},
    {'time': {'kind': 'day', 'source': 'yesterday', 'day_offset': -1, 'recent_minutes': 4}},
    {'time': {'kind': 'recent', 'source': 'last month', 'recent_minutes': 999999}},
    {'time': {'kind': 'point', 'source': 'yesterday at ten', 'day_offset': -1, 'start_time': '25:00'}},
    {'time': {'kind': 'point', 'source': 'tomorrow', 'day_offset': 1, 'start_time': '12:00'}},
])
def test_invalid_or_ambiguous_intents_never_expand_scope(patch):
    with pytest.raises(Clarify):
        resolve(intent(**patch), body(), CAMERAS, PlannerConfig(), NOW)


def test_duplicate_names_require_clarification():
    with pytest.raises(Clarify):
        resolve(intent(), body(), CAMERAS + [SimpleNamespace(slug='other', name='cam2')], PlannerConfig(), NOW)


def test_conflicting_camera_filter_is_not_overwritten():
    with pytest.raises(Clarify, match='conflicts'):
        resolve(intent(), body(camera='yard-456'), CAMERAS, PlannerConfig(), NOW)


def test_explicit_times_are_used_without_widening():
    b = body(time_from='2026-10-25T16:30:00Z', time_to='2026-10-25T16:31:00Z')
    plan = resolve(intent(), b, CAMERAS, PlannerConfig(), NOW)
    assert plan.time_to == '2026-10-25T16:31:00+00:00'
    assert 'default' not in plan.summary


@pytest.mark.parametrize('bounds', [
    {'time_from': '2026-10-25T16:30:00Z'},
    {'time_from': '2026-10-25T16:30:00', 'time_to': '2026-10-25T17:30:00Z'},
    {'time_from': '2026-10-25T17:30:00Z', 'time_to': '2026-10-25T16:30:00Z'},
    {'time_from': '2026-01-01T00:00:00Z', 'time_to': '2026-10-25T00:00:00Z'},
])
def test_invalid_explicit_bounds_are_rejected(bounds):
    with pytest.raises(Clarify):
        resolve(intent(), body(**bounds), CAMERAS, PlannerConfig(), NOW)


@pytest.mark.parametrize('day,clock', [('2026-03-08', '02:30'), ('2026-11-01', '01:30')])
def test_daylight_saving_nonexistent_and_repeated_times(day, clock):
    b = NaturalQuery(query='person', domain='people', timezone='America/New_York')
    with pytest.raises(Clarify, match='clock change'):
        resolve(intent(time={'kind': 'point', 'source': day, 'date': day, 'start_time': clock}),
                b, CAMERAS, PlannerConfig(), datetime(2026, 12, 1, tzinfo=timezone.utc))


def test_whole_day_across_dst_is_23_hours():
    b = NaturalQuery(query='person', domain='people', timezone='America/New_York')
    plan = resolve(intent(time={'kind': 'day', 'source': 'March 8', 'date': '2026-03-08'}),
                   b, CAMERAS, PlannerConfig(), NOW)
    assert (datetime.fromisoformat(plan.time_to) - datetime.fromisoformat(plan.time_from)).total_seconds() == 23 * 3600


def test_keys_are_encrypted_write_only_and_proposed_keys_are_tested(monkeypatch):
    from backend import crypto
    monkeypatch.setattr(config.settings, 'discovery_secret_key', 'test-encryption-key')
    crypto._fernet.cache_clear()
    try:
        update = SettingsUpdate(config=PlannerConfig(), keys={'openai': SecretStr('test-api-secret')})
        encrypted = config.encrypted_updates(None, update)
        assert 'test-api-secret' not in encrypted['openai']
        saved = SimpleNamespace(config=update.config.model_dump(), credentials=encrypted, tested_fingerprint=None)
        assert config.effective(saved).public['key_configured']['openai']
        assert 'test-api-secret' not in str(config.effective(saved).public)
        assert 'test-api-secret' not in str(update.model_dump())
        replacement = SettingsUpdate(config=PlannerConfig(provider='openai'), keys={'openai': SecretStr('new-key')})
        assert config.effective(saved, replacement).runtime.key.get_secret_value() == 'new-key'
    finally:
        crypto._fernet.cache_clear()


def test_omitted_key_is_preserved_empty_key_removes():
    saved = SimpleNamespace(credentials={'openai': 'opaque-encrypted-value'})
    assert config.encrypted_updates(saved, SettingsUpdate(config=PlannerConfig())) == saved.credentials
    assert config.encrypted_updates(saved, SettingsUpdate(config=PlannerConfig(), keys={'openai': ''})) == {}


def test_insecure_default_cannot_encrypt_provider_credentials(monkeypatch):
    monkeypatch.setattr(config.settings, 'discovery_secret_key', 'dev-insecure-change-me')
    with pytest.raises(PlannerError):
        config.encrypted_updates(None, SettingsUpdate(config=PlannerConfig(), keys={'openai': 'secret'}))


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://user:pw@host/v1', 'http://host/v1?key=x', 'http://[', 'http://host:999999'])
def test_local_endpoint_rejects_bad_urls_and_embedded_credentials(url):
    from backend.services.search_planner.providers.base import local_endpoint
    with pytest.raises(PlannerError):
        local_endpoint(url)


# ── Camera references: exact first, then a normalised spoken form ────────────

@pytest.mark.parametrize('ref', ['Camera 2', 'CAM-2', 'cam_02', ' cam 2 '])
def test_spoken_camera_forms_resolve_to_the_one_camera(ref):
    plan = resolve(intent(camera_refs=[ref]), body(), CAMERAS, PlannerConfig(), NOW)
    assert plan.camera_slugs == ['gate-123'] and plan.camera_scoped


def test_an_exact_name_wins_over_a_normalised_collision():
    cams = CAMERAS + [SimpleNamespace(slug='lobby', name='Cam 2', camera_metadata={})]
    plan = resolve(intent(camera_refs=['Cam 2']), body(), cams, PlannerConfig(), NOW)
    assert plan.camera_slugs == ['lobby']


def test_a_normalised_collision_is_a_clarification_listing_both():
    cams = [SimpleNamespace(slug='a', name='Cam 1', camera_metadata={}),
            SimpleNamespace(slug='b', name='cam-01', camera_metadata={})]
    with pytest.raises(Clarify, match='more than one') as exc:
        resolve(intent(camera_refs=['camera 1']), body(), cams, PlannerConfig(), NOW)
    assert exc.value.choices == ['Cam 1', 'cam-01']


def test_an_unknown_camera_offers_the_closest_real_names():
    with pytest.raises(Clarify, match='No available camera') as exc:
        resolve(intent(camera_refs=['yards']), body(), CAMERAS, PlannerConfig(), NOW)
    assert exc.value.choices[0] == 'Yard'


def test_unknown_camera_with_nothing_close_still_lists_cameras():
    with pytest.raises(Clarify) as exc:
        resolve(intent(camera_refs=['zzzz']), body(), CAMERAS, PlannerConfig(), NOW)
    assert set(exc.value.choices) == {'cam2', 'Yard'}


def test_no_camera_mentioned_is_an_unscoped_plan_with_a_short_summary():
    plan = resolve(intent(camera_refs=[]), body(), CAMERAS, PlannerConfig(), NOW)
    assert not plan.camera_scoped
    assert plan.summary.startswith('All cameras (2)')


def test_the_camera_filter_alone_scopes_the_plan():
    plan = resolve(intent(camera_refs=[]), body(camera='yard-456'), CAMERAS, PlannerConfig(), NOW)
    assert plan.camera_scoped and plan.camera_slugs == ['yard-456']


# ── Settings: fingerprint, effort, key bounds ────────────────────────────────

def _runtime(**patch):
    from backend.services.search_planner.providers.base import Runtime
    args = dict(provider='openai', model='m', base_url='', key=SecretStr('weak'), timeout=5, effort='low')
    args.update(patch)
    return Runtime(**args)


def test_fingerprint_is_keyed_by_the_deployment_secret(monkeypatch):
    import hashlib
    monkeypatch.setattr(config.settings, 'discovery_secret_key', 'secret-one')
    first = config.fingerprint(_runtime())
    assert 'weak' not in first
    assert first != hashlib.sha256(b'weak').hexdigest()
    monkeypatch.setattr(config.settings, 'discovery_secret_key', 'secret-two')
    assert config.fingerprint(_runtime()) != first


def test_fingerprint_covers_effort_so_a_change_needs_a_new_test():
    assert config.fingerprint(_runtime(effort='low')) != config.fingerprint(_runtime(effort='default'))


def test_profile_effort_reaches_the_runtime():
    cfg = PlannerConfig(provider='anthropic', profiles={'anthropic': Profile(model='m', reasoning_effort='default')})
    saved = SimpleNamespace(config=cfg.model_dump(), credentials={}, tested_fingerprint=None)
    assert config.effective(saved).runtime.effort == 'default'
    # Rows saved before the field existed default to low.
    legacy = cfg.model_dump()
    del legacy['profiles']['anthropic']['reasoning_effort']
    assert config.effective(SimpleNamespace(config=legacy, credentials={}, tested_fingerprint=None)).runtime.effort == 'low'


def test_oversized_key_is_rejected_by_the_request_model():
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='too long'):
        SettingsUpdate(config=PlannerConfig(), keys={'openai': 'x' * 4097})


def test_a_camera_outside_the_domain_set_says_why():
    retired = SimpleNamespace(slug='cam-7', name='Cam 7', camera_metadata={})
    with pytest.raises(Clarify, match='Cam 7 has no recordings') as exc:
        resolve(intent(camera_refs=['cam 7']), body(), CAMERAS, PlannerConfig(), NOW, unsearchable=[retired])
    assert 'Cam 7' not in exc.value.choices


def test_environment_cannot_take_over_the_ui_settings(monkeypatch):
    """The admin UI is the only source. Variables a host may set for other
    tools (OPENAI_API_KEY, ...) must not lock fields or swap the key."""
    for name, value in (('OPENAI_API_KEY', 'host-tooling-key'), ('ANTHROPIC_API_KEY', 'host-tooling-key'),
                        ('SEARCH_PLANNER_PROVIDER', 'anthropic'), ('SEARCH_PLANNER_ENABLED', 'false')):
        monkeypatch.setenv(name, value)
    from backend.config import Settings
    fresh = Settings()
    assert not any(hasattr(fresh, f) for f in ('openai_api_key', 'search_planner_provider'))
    cfg = PlannerConfig(enabled=True, provider='openai', profiles={'openai': Profile(model='m')})
    saved = SimpleNamespace(config=cfg.model_dump(), credentials={}, tested_fingerprint=None)
    eff = config.effective(saved)
    assert (eff.config.enabled, eff.runtime.provider, eff.runtime.key.get_secret_value()) == (True, 'openai', '')
    assert 'managed' not in eff.public
