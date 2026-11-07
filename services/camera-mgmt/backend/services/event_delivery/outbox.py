"""Write one outbox row per fact, inside the caller's transaction.

Called from the ingest, the rule engine and the health monitor, always before
their commit, so the fact and its outbox row land together or not at all.

NO GLOBAL LOCK. 065 serialised every enqueue on one advisory lock so the feed
cursor could follow `seq`; that lock deadlocked against the health check and
serialised all ingest. The feed now pages on `feed_seq`, which the router
stamps in commit order (migration 066), so enqueue needs no ordering of its own.
"""
from datetime import datetime
from uuid import UUID

from sqlalchemy import event, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...config import settings
from ...models import EventDestination, EventIntegrationSettings, EventOutbox, EventSubscription
from .envelope import public_base_url, with_playback
from .routing import matches

#: db.info key for the per-transaction route cache. An ingest batch of 200 rows
#: used to re-read every subscription 200 times.
_CACHE = 'event_delivery.routes'


async def control(db):
    return await db.get(EventIntegrationSettings, 1)


async def enabled(db):
    """Global pause: the environment switch and the admin switch must both allow it."""
    if not settings.events_enabled:
        return False
    cfg = await control(db)
    return cfg is None or cfg.enabled


def destination_snapshot(dest):
    return {k: (str(getattr(dest, k)) if k == 'id' else getattr(dest, k)) for k in
            ('id', 'name', 'kind', 'config', 'secret_enc', 'media', 'allow_insecure',
             'rate_per_min', 'max_age_s')}


async def _load(db):
    """(enabled pairs, feed on, any destination enabled, link base), once per transaction."""
    info = getattr(db, 'info', None)
    cached = info.get(_CACHE) if info is not None else None
    if cached is not None:
        return cached
    cfg = await control(db)
    pairs = (await db.execute(
        select(EventSubscription, EventDestination)
        .join(EventDestination, EventSubscription.destination_id == EventDestination.id)
        .where(EventSubscription.enabled.is_(True), EventDestination.enabled.is_(True))
    )).all()
    subs = [(dict(id=str(sub.id), types=sub.types, filters=sub.filters, cooldown_s=sub.cooldown_s),
             destination_snapshot(dest)) for sub, dest in pairs]
    feed = bool(settings.events_feed_enabled or (cfg and cfg.feed_enabled))
    any_dest = bool(pairs) or (await db.execute(
        select(EventDestination.id).where(EventDestination.enabled.is_(True)).limit(1))).first() is not None
    cached = (subs, feed, any_dest, public_base_url(cfg))
    if info is not None:
        info[_CACHE] = cached
    return cached


@event.listens_for(Session, 'after_transaction_end')
def _forget(session, transaction):
    # Per TRANSACTION, not per session: a long-lived session (the health
    # monitor's, a test's) must see a destination enabled after its last commit.
    session.info.pop(_CACHE, None)


async def enqueue(db, envelope):
    """Insert the outbox row for `envelope`. Never commits; a duplicate is a no-op.

    Writes nothing when integrations are paused, or when no destination is
    enabled and the pull feed is off — "no integrations" means no rows.
    """
    if envelope is None or not await enabled(db):
        return
    subs, feed, any_dest, base = await _load(db)
    if not (any_dest or feed):
        return
    with_playback(envelope, base)
    routes = {}
    for sub, dest in subs:
        if matches(sub, envelope):
            route = routes.setdefault(dest['id'], dict(destination=dest, subscriptions=[]))
            route['subscriptions'].append(sub)
    # Recipients and encrypted configuration are captured at acceptance: no
    # backfill to newly enabled destinations, no redirection on later edits.
    await db.execute(insert(EventOutbox).values(
        message_id=UUID(envelope['message_id']), type=envelope['type'],
        subject_id=envelope['subject']['id'], camera_id=UUID(envelope['camera']['id']),
        envelope=envelope, routes=list(routes.values()), created_at=func.clock_timestamp(),
        occurred_at=datetime.fromisoformat(envelope['occurred_at']),
    ).on_conflict_do_nothing(index_elements=['message_id']))
