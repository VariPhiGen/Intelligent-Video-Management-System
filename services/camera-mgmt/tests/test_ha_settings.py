"""Where the Home Assistant connection comes from, and how it is stored.

A saved connection (ui) wins over HA_URL/HA_TOKEN (env), which win over
nothing. The token is Fernet ciphertext at rest — never the plaintext.

Run (from services/camera-mgmt): python -m pytest tests/test_ha_settings.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import crypto  # noqa: E402
from backend.services.bridge import ha_settings  # noqa: E402

_aio = pytest.mark.asyncio

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
TOKEN = "eyJ-ha-long-lived-token-for-settings-tests"


class FakeDB:
    """One-table session: enough for get / add / delete / commit."""

    def __init__(self):
        self.rows = {}
        self.commits = 0

    async def get(self, model, key):
        return self.rows.get(key)

    def add(self, row):
        self.rows[row.name] = row

    async def delete(self, row):
        self.rows.pop(row.name, None)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def env(monkeypatch):
    def _set(url="", token=""):
        monkeypatch.setattr(ha_settings.settings, "ha_url", url)
        monkeypatch.setattr(ha_settings.settings, "ha_token", token)
    _set()
    return _set


@_aio
async def test_nothing_configured_is_none(env):
    cfg = await ha_settings.load(FakeDB())
    assert cfg.source == "none" and cfg.url == "" and cfg.token is None


@_aio
async def test_the_environment_still_works_with_nothing_saved(env):
    """An appliance configured through .env must keep working across 057."""
    env("http://127.0.0.1:8123", TOKEN)
    cfg = await ha_settings.load(FakeDB())
    assert (cfg.source, cfg.url, cfg.token) == ("env", "http://127.0.0.1:8123", TOKEN)


@_aio
async def test_a_saved_connection_wins_over_the_environment(env):
    env("http://old:8123", "env-token")
    db = FakeDB()
    await ha_settings.save(db, url="http://127.0.0.1:8123", token=TOKEN,
                           by="admin", now=NOW)
    cfg = await ha_settings.load(db)
    assert (cfg.source, cfg.url, cfg.token) == ("ui", "http://127.0.0.1:8123", TOKEN)
    assert cfg.updated_by == "admin" and cfg.updated_at == NOW


@_aio
async def test_the_token_is_stored_encrypted(env):
    db = FakeDB()
    await ha_settings.save(db, url="http://h:8123", token=TOKEN, by="a", now=NOW)
    stored = db.rows[ha_settings.NAME].secret_enc
    assert stored and TOKEN not in stored
    assert crypto.decrypt(stored) == TOKEN


@_aio
async def test_saving_without_a_token_keeps_the_stored_one(env):
    """So a URL can be corrected without pasting the token again."""
    db = FakeDB()
    await ha_settings.save(db, url="http://h:8123", token=TOKEN, by="a", now=NOW)
    await ha_settings.save(db, url="http://h2:8123", token=None, by="a", now=NOW)
    cfg = await ha_settings.load(db)
    assert cfg.url == "http://h2:8123" and cfg.token == TOKEN


@_aio
async def test_clearing_falls_back_to_the_environment(env):
    env("http://envhost:8123", "env-token")
    db = FakeDB()
    await ha_settings.save(db, url="http://h:8123", token=TOKEN, by="a", now=NOW)
    assert await ha_settings.clear(db) is True
    assert (await ha_settings.load(db)).source == "env"
    assert await ha_settings.clear(db) is False


@_aio
async def test_a_token_the_key_cannot_read_is_no_token(env):
    """DISCOVERY_SECRET_KEY rotated: the ciphertext is unreadable. The panel
    must say no token is set — the truth — rather than send garbage to HA."""
    db = FakeDB()
    await ha_settings.save(db, url="http://h:8123", token=TOKEN, by="a", now=NOW)
    db.rows[ha_settings.NAME].secret_enc = "gAAAAA-not-a-valid-fernet-token"
    cfg = await ha_settings.load(db)
    assert cfg.source == "ui" and cfg.token is None
