import hashlib
import hmac
import json
from dataclasses import dataclass

from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from ...config import settings
from ...crypto import decrypt, encrypt
from ...models import SearchPlannerSettings
from .providers.base import Runtime, local_endpoint
from .schemas import PlannerConfig, PlannerError, SettingsUpdate


async def row(db, lock=False):
    stmt = select(SearchPlannerSettings).where(SearchPlannerSettings.id == 1)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


@dataclass
class Effective:
    config: PlannerConfig
    runtime: Runtime
    public: dict


def effective(saved=None, proposed: SettingsUpdate | None = None) -> Effective:
    try:
        # The admin UI is the only source: provider, models and keys can be
        # switched there at any time without touching the deployment.
        cfg = proposed.config.model_copy(deep=True) if proposed else PlannerConfig.model_validate(saved.config if saved else {})
        credentials = (saved.credentials or {}) if saved else {}
        keys, configured = {}, {}
        for p in cfg.profiles:
            if proposed and p in proposed.keys:
                keys[p] = proposed.keys[p].get_secret_value().strip()
            else:
                # A key that no longer decrypts (rotated DISCOVERY_SECRET_KEY)
                # reads as not configured, so the admin can re-enter it.
                keys[p] = decrypt(credentials.get(p)) or ''
            configured[p] = bool(keys[p])
        profile = cfg.profiles[cfg.provider]
        runtime = Runtime(cfg.provider, profile.model.strip(), profile.base_url.strip(),
                          SecretStr(keys[cfg.provider]), cfg.timeout_seconds, profile.reasoning_effort)
        tested = bool(saved and saved.tested_fingerprint == fingerprint(runtime))
        return Effective(cfg, runtime, {'config': cfg.model_dump(), 'key_configured': configured,
                                        'tested': tested, 'assistant_tested': bool(saved and getattr(saved, 'assistant_tested_fingerprint', None) == assistant_fingerprint(runtime))})
    except ValidationError:
        raise PlannerError('Invalid saved planner settings. Ask an administrator to configure them.', 422) from None


def fingerprint(runtime):
    """Identifies the exact runtime a connection test passed against. Keyed
    with the deployment secret: a plain hash of an API key stored beside it
    could be brute-forced from a database dump when the key is weak (a local
    server's). Rotating the secret therefore asks for a fresh test, which a
    rotation needs anyway because the stored keys stop decrypting."""
    payload = json.dumps({
        'provider': runtime.provider, 'model': runtime.model, 'base_url': runtime.base_url,
        'key': runtime.key.get_secret_value(), 'effort': runtime.effort, 'schema': '1',
    }, sort_keys=True).encode()
    secret = hashlib.sha256(b'search-planner-fingerprint:' + settings.discovery_secret_key.encode()).digest()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def encrypted_updates(saved, proposed):
    creds = dict(saved.credentials or {}) if saved else {}
    for provider, secret in proposed.keys.items():
        value = secret.get_secret_value().strip()
        if value and settings.discovery_secret_key == 'dev-insecure-change-me':
            raise PlannerError('Set a secure DISCOVERY_SECRET_KEY before storing API keys.', 422)
        if value:
            creds[provider] = encrypt(value)
        else:
            creds.pop(provider, None)
    return creds


def validate_config(cfg):
    for p, profile in cfg.profiles.items():
        if p != 'qwen' and profile.base_url:
            raise PlannerError('Cloud providers use their official endpoints.', 422)
        if p == 'qwen' and profile.base_url:
            local_endpoint(profile.base_url)


def assistant_fingerprint(runtime):
    return hashlib.sha256((fingerprint(runtime) + f":assistant-v1:{runtime.timeout}").encode()).hexdigest()
