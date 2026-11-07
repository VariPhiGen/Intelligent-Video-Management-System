"""ha_settings.py — where the Home Assistant URL and token come from.

Two sources, one answer, in this order:

  ui    a row in integration_credentials, saved by an admin over
        /api/ha/connection. The token is Fernet ciphertext at rest.
  env   HA_URL / HA_TOKEN. Still honoured, so an appliance configured
        through .env keeps working; a saved row overrides it.
  none  neither. A supported deployment — the driver says `not_configured`.

Nothing here talks to HA. Saving and testing a connection is the router's job;
pushing the answer into each worker's driver is bridge.apply_ha_config.
"""
from __future__ import annotations

from datetime import datetime
from typing import NamedTuple, Optional

from ... import crypto
from ...config import settings
from ...models import IntegrationCredential

NAME = "homeassistant"


class HaConfig(NamedTuple):
    url: str
    token: Optional[str]
    source: str
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


async def load(db) -> HaConfig:
    row = await db.get(IntegrationCredential, NAME)
    if row is not None:
        # decrypt() answers None for a ciphertext the current key cannot read
        # (DISCOVERY_SECRET_KEY rotated): the panel then shows no token, which
        # is the truth — it has to be entered again.
        return HaConfig(row.url, crypto.decrypt(row.secret_enc), "ui",
                        row.updated_at, row.updated_by)
    if settings.ha_url:
        return HaConfig(settings.ha_url, settings.ha_token or None, "env")
    return HaConfig("", None, "none")


async def save(db, *, url: str, token: Optional[str], by: Optional[str],
               now: datetime) -> None:
    """Store the connection. `token=None` keeps the token already stored."""
    row = await db.get(IntegrationCredential, NAME)
    if row is None:
        row = IntegrationCredential(name=NAME)
        db.add(row)
    row.url = url
    if token is not None:
        row.secret_enc = crypto.encrypt(token)
    row.updated_at = now
    row.updated_by = by
    await db.commit()


async def clear(db) -> bool:
    """Forget the saved connection. Returns False if there was none."""
    row = await db.get(IntegrationCredential, NAME)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
