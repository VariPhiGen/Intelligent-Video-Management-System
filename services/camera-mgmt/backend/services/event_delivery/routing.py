"""Subscription matching, cooldown and fan-out from the outbox to deliveries.

`matches` and the cooldown helpers are pure. `route_once` runs in the router
loop: one short transaction, no network I/O, serialised by an advisory lock so
that `feed_seq` — the pull feed's cursor — is stamped and committed in order.
"""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from ...models import EventCooldown, EventDelivery, EventDestination, EventOutbox

TYPES = ('ai.event.created', 'ai.event.closed', 'alert.raised', 'camera.offline', 'camera.online')
CHAT_KINDS = ('telegram', 'slack')
#: Destinations people read: a default cooldown so one incident is not a flood.
NOTIFY_KINDS = CHAT_KINDS + ('email',)
#: How long an event stays worth delivering, by kind, unless the destination
#: sets max_age_s. Email is read later than chat; SFTP is an evidence export
#: and keeps the longest the API allows.
DEFAULT_MAX_AGE = {'telegram': 120, 'slack': 120, 'email': 3600, 'sftp': 604800}
#: A queued notice the recipient has not been sent yet. `pending` is included so
#: a deferred "online" counts as what they will be told.
_TOLD = ('sent', 'sending', 'retrying', 'pending')


def matches(sub, env):
    if env['type'] not in sub['types']:
        return False
    f, d = sub.get('filters') or {}, env['data']
    if f.get('cameras') and env['camera']['id'] not in f['cameras']:
        return False
    if env['type'].startswith('ai.event.'):
        if d['activity'] == 'motion' and not f.get('include_motion') and 'motion' not in f.get('activities', []):
            return False
        if f.get('activities') and d['activity'] not in f['activities']:
            return False
    if env['type'] == 'alert.raised':
        if f.get('rules') and d['rule_id'] not in f['rules']:
            return False
        levels = {'info': 0, 'warning': 1, 'critical': 2}
        if levels.get(d['severity'], 0) < levels.get(f.get('min_severity'), 0):
            return False
    return True


def cooldown_key(sub_id, env):
    d = env['data']
    kind = 'camera.status' if env['type'].startswith('camera.') else d.get('activity') or d.get('rule_id') or env['type']
    return f"{sub_id}:{env['camera']['id']}:{kind}:{d.get('zone') or ''}"


def effective_cooldown(sub, kind):
    value = sub.get('cooldown_s')
    return value if value is not None else (60 if kind in NOTIFY_KINDS else 0)


def max_age(dest):
    return dest.get('max_age_s') or DEFAULT_MAX_AGE.get(dest['kind'], 86400)


async def _reserve(db, subs, kind, env, now):
    """Take each subscription's cooldown slot. Returns (any taken, earliest expiry).

    A subscription with no cooldown always counts as taken. When none is taken
    the second value says when the earliest slot frees up.
    """
    taken, keys = False, []
    for sub in subs:
        cd = effective_cooldown(sub, kind)
        if not cd:
            taken = True
            continue
        key = cooldown_key(sub['id'], env)
        keys.append(key)
        stmt = insert(EventCooldown).values(key=key, until=now + timedelta(seconds=cd))
        stmt = stmt.on_conflict_do_update(index_elements=['key'], set_={'until': stmt.excluded.until},
                                          where=EventCooldown.until <= now).returning(EventCooldown.key)
        taken = bool((await db.execute(stmt)).first()) or taken
    if taken or not keys:
        return True, None
    free_at = (await db.execute(select(func.min(EventCooldown.until)).where(EventCooldown.key.in_(keys)))).scalar()
    return False, free_at


async def _route_status(db, row, did, dest, subs, now):
    """Camera online/offline to a destination with a cooldown: (state, error, send_at).

    Online/offline is a STATE, not an occurrence. A plain cooldown dropped the
    "online" that follows a short outage, leaving the guard believing the camera
    was still dead. So a recipient is told about changes from what it was last
    told, and a change inside the cooldown is deferred to the end of the window
    rather than dropped. A flap that ends where it started sends nothing more.
    """
    status_rows = select(EventOutbox.seq).where(EventOutbox.camera_id == row.camera_id,
                                                EventOutbox.type.like('camera.%'))
    # A deferred notice that has not gone out yet is now out of date.
    await db.execute(update(EventDelivery).where(
        EventDelivery.destination_id == did, EventDelivery.state == 'pending',
        EventDelivery.next_attempt_at > now, EventDelivery.outbox_seq.in_(status_rows),
    ).values(state='suppressed', last_error='superseded'))
    told = (await db.execute(
        select(EventOutbox.type).join(EventDelivery, EventDelivery.outbox_seq == EventOutbox.seq)
        .where(EventDelivery.destination_id == did, EventDelivery.state.in_(_TOLD),
               EventOutbox.seq.in_(status_rows))
        .order_by(EventOutbox.seq.desc()).limit(1))).scalar()
    if told == row.type:
        return 'suppressed', 'unchanged', now
    taken, free_at = await _reserve(db, subs, dest['kind'], row.envelope, now)
    return 'pending', None, (now if taken else free_at)


async def route_once(db, *, now, limit=200):
    # Serialise routers even if leader election fails open: feed_seq must be
    # stamped by one writer at a time. No network under this lock.
    if not (await db.execute(text('SELECT pg_try_advisory_xact_lock(6531002)'))).scalar():
        return 0
    rows = (await db.execute(select(EventOutbox).where(EventOutbox.routed_at.is_(None))
            .order_by(EventOutbox.seq).limit(limit).with_for_update(skip_locked=True))).scalars().all()
    enabled = set((await db.execute(select(EventDestination.id).where(EventDestination.enabled.is_(True)))).scalars())
    for row in rows:
        for route in row.routes:
            dest, subs = route['destination'], route['subscriptions']
            did = UUID(dest['id'])
            if did not in enabled:
                continue
            send_at, error = now, None
            deadline = row.occurred_at + timedelta(seconds=max_age(dest))
            if deadline <= now:
                state = 'expired'
            elif row.type.startswith('camera.') and any(effective_cooldown(s, dest['kind']) for s in subs):
                state, error, send_at = await _route_status(db, row, did, dest, subs, now)
                # A deferred notice keeps its full freshness window from when it may go.
                deadline = max(deadline, send_at + timedelta(seconds=max_age(dest)))
            else:
                taken, _ = await _reserve(db, subs, dest['kind'], row.envelope, now)
                state, error = ('pending', None) if taken else ('suppressed', 'cooldown')
            await db.execute(insert(EventDelivery).values(
                outbox_seq=row.seq, destination_id=did, destination=dest,
                subscription_ids=[UUID(s['id']) for s in subs], state=state, attempts=0,
                deadline_at=deadline, next_attempt_at=send_at, last_error=error,
            ).on_conflict_do_nothing(index_elements=['outbox_seq', 'destination_id']))
        row.routed_at = now
        row.feed_seq = (await db.execute(text("SELECT nextval('event_outbox_feed_seq')"))).scalar()
    await db.commit()
    return len(rows)
