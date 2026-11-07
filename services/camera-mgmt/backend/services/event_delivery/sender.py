"""Claim due deliveries under leases, send them, and record the outcome.

ONE BATCH IN FLIGHT PER DESTINATION. `claim` hands out a run of consecutive
deliveries for a destination and refuses that destination again until the run
is finished, so a slow receiver delays only itself. 065 handed out ONE delivery
per destination per one-second tick, which capped every receiver — a Kafka
topic included — at about one event a second.

ORDER. Machine destinations (webhook, Kafka) get strict outbox order: a run
stops at the first delivery that is not yet due, and if one send fails the rest
of the run is put back untouched, so nothing overtakes a retry. Chat
destinations take whatever is due, because a deferred "camera online" must not
hold up an intrusion alert behind it.

LEASES. Every claimed row carries a lease; the batch renews the leases of the
rows it has not reached yet before each send. A worker that dies mid-batch
leaves rows whose leases lapse and are reclaimed — same message_id, so the
receiver can deduplicate.
"""
import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import structlog
from sqlalchemy import and_, delete, exists, func, or_, select, update

from ...config import settings
from ...db import AsyncSessionLocal
from ...models import EventCooldown, EventDelivery, EventDestination, EventIntegrationSettings, EventOutbox
from ..annotation_delivery import DeliveryError
from . import evidence, media, outbox, routing
from .adapters import ADAPTERS, kafka

log = structlog.get_logger(__name__)

MACHINE_KINDS = ('webhook', 'kafka')
MAX_ATTEMPTS = 10
BATCH_MAX = 50
#: Concurrent batches per sender process. Leases are the cross-process limit.
MAX_ACTIVE = 20
_ACTIVE = ('pending', 'retrying', 'sending')


def now_utc():
    return datetime.now(timezone.utc)


def batch_size(dest):
    """About two seconds of the destination's rate, at least one, at most BATCH_MAX."""
    return max(1, min(BATCH_MAX, int(dest.rate_per_min * 2 / 60)))


def _due(now):
    return or_(
        and_(EventDelivery.state.in_(['pending', 'retrying']),
             func.least(EventDelivery.next_attempt_at, EventDelivery.deadline_at) <= now),
        and_(EventDelivery.state == 'sending', EventDelivery.lease_until <= now),
    )


async def claim(db, owner, *, now, limit=50):
    """Claim up to one run per destination. Returns the rows, grouped by destination in order."""
    if not await outbox.enabled(db):
        return []
    mine = EventDelivery.destination_id == EventDestination.id
    has_due = exists(select(EventDelivery.id).where(mine, _due(now)))
    in_flight = exists(select(EventDelivery.id).where(
        mine, EventDelivery.state == 'sending', EventDelivery.lease_until > now))
    dests = (await db.execute(
        select(EventDestination)
        .where(EventDestination.enabled.is_(True), has_due, ~in_flight,
               or_(EventDestination.next_send_at.is_(None), EventDestination.next_send_at <= now))
        .order_by(EventDestination.next_send_at.asc().nullsfirst(), EventDestination.id)
        .limit(limit).with_for_update(skip_locked=True))).scalars().all()
    claimed = []
    for dest in dests:
        want, strict = batch_size(dest), dest.kind in MACHINE_KINDS
        q = (select(EventDelivery)
             .where(EventDelivery.destination_id == dest.id, EventDelivery.state.in_(_ACTIVE))
             .order_by(EventDelivery.outbox_seq).limit(want * 4).with_for_update())
        if not strict:
            q = q.where(_due(now))
        run = []
        for row in (await db.execute(q)).scalars():
            if row.deadline_at <= now:
                row.state, row.last_error = 'expired', 'Freshness deadline passed'
                row.lease_owner = row.lease_until = None
                continue
            if row.attempts >= MAX_ATTEMPTS:
                row.state, row.last_error = 'failed', 'Maximum delivery attempts reached'
                row.lease_owner = row.lease_until = None
                continue
            if row.state == 'sending' and row.lease_until and row.lease_until > now:
                break
            if row.state != 'sending' and row.next_attempt_at > now:
                break  # strict order: nothing overtakes a delivery still waiting to retry
            row.state, row.lease_owner = 'sending', owner
            row.lease_until = now + timedelta(seconds=settings.events_lease_s)
            row.attempts += 1
            run.append(row)
            if len(run) >= want:
                break
        if run:
            dest.next_send_at = now + timedelta(seconds=len(run) * 60 / dest.rate_per_min)
            claimed.extend(run)
    await db.commit()
    return claimed


async def finalise(db, delivery_id, owner, *, ok, error=None, provider_ref=None, media=None, now):
    """Record one outcome. False when the lease was lost — the row is someone else's now."""
    existing = await db.get(EventDelivery, delivery_id)
    if existing is None:
        return False
    # Same lock order as claim: destination, then delivery.
    dest = (await db.execute(select(EventDestination).where(
        EventDestination.id == existing.destination_id).with_for_update())).scalar_one_or_none()
    row = (await db.execute(select(EventDelivery).where(
        EventDelivery.id == delivery_id, EventDelivery.lease_owner == owner,
        EventDelivery.state == 'sending', EventDelivery.lease_until > now).with_for_update())).scalar_one_or_none()
    if row is None:
        return False
    if ok:
        row.state, row.sent_at, row.last_error = 'sent', now, None
        row.provider_ref, row.media = provider_ref, media
        if dest:
            dest.last_success_at, dest.last_error = now, None
    else:
        error = error or DeliveryError('Delivery failed')
        row.last_error, row.media = str(error)[:300], media
        delay = max(min(600, 2 ** min(row.attempts, 10)) * random.uniform(.8, 1.2), error.retry_after or 0)
        row.next_attempt_at = now + timedelta(seconds=delay)
        if error.permanent or row.attempts >= MAX_ATTEMPTS:
            row.state = 'failed'
        else:
            row.state = 'expired' if row.next_attempt_at >= row.deadline_at else 'retrying'
        if dest:
            dest.last_error_at, dest.last_error = now, row.last_error
    row.lease_owner = row.lease_until = None
    await db.commit()
    return True


async def _renew(ids, owner):
    async with AsyncSessionLocal() as db:
        await db.execute(update(EventDelivery).where(
            EventDelivery.id.in_(ids), EventDelivery.lease_owner == owner, EventDelivery.state == 'sending',
        ).values(lease_until=now_utc() + timedelta(seconds=settings.events_lease_s)))
        await db.commit()


async def _release(ids, owner):
    """Put unsent rows of a stopped batch back exactly as they were before the claim."""
    if not ids:
        return
    async with AsyncSessionLocal() as db:
        await db.execute(update(EventDelivery).where(
            EventDelivery.id.in_(ids), EventDelivery.lease_owner == owner, EventDelivery.state == 'sending',
        ).values(state='pending', attempts=EventDelivery.attempts - 1, lease_owner=None, lease_until=None))
        await db.commit()


async def deliver(row, owner):
    """Send one claimed row: 'sent', 'failed' (permanent), 'retry', or None.

    None means the destination was paused (or the event swept) after the claim;
    the caller hands the row back untouched instead of burning an attempt.
    """
    # Every step owns its own DB session; HTTP runs outside any transaction.
    async with AsyncSessionLocal() as db:
        dest = await db.get(EventDestination, row.destination_id)
        if not dest or not dest.enabled or not await outbox.enabled(db):
            return None
        event = await db.get(EventOutbox, row.outbox_seq)
        if event is None:
            return None
        env = dict(event.envelope)
    target = SimpleNamespace(**row.destination)
    frame, label, error, ref = None, 'skipped', None, None
    try:
        async with asyncio.timeout(35):
            if target.kind == 'telegram' and target.media:
                frame, label = await media.frame_for(env)
            elif target.kind in ('slack', 'email', 'sftp') and target.media:
                # The stored snapshot, never a fresh one: see evidence.py.
                frame, label, caption = await evidence.snapshot_for(row.outbox_seq, env)
                if caption:
                    env['_frame_caption'] = caption
            elif target.kind == 'sftp':
                label = 'disabled'
            env['_media'] = label
            if now_utc() >= row.deadline_at:
                raise DeliveryError('Freshness deadline passed')
            ref = await ADAPTERS[target.kind](target, env, frame)
    except DeliveryError as exc:
        error = exc
    except Exception:
        error = DeliveryError('Delivery connection failed')
    async with AsyncSessionLocal() as db:
        await finalise(db, row.id, owner, ok=error is None, error=error,
                       provider_ref=ref, media=label, now=now_utc())
    if error is None:
        return 'sent'
    return 'failed' if error.permanent else 'retry'


async def deliver_batch(rows, owner):
    """Send a destination's run in order; stop at a retryable failure and hand the rest back.

    A PERMANENT failure (target refused, credential rejected) is never retried,
    so nothing can overtake it — the run carries on. Stopping there instead
    walked a misconfigured destination's backlog at one row per tick.
    """
    for i, row in enumerate(rows):
        if i:
            await _renew([r.id for r in rows[i:]], owner)
        outcome = await deliver(row, owner)
        if outcome is None:
            await _release([r.id for r in rows[i:]], owner)
            return
        if outcome == 'retry':
            await _release([r.id for r in rows[i + 1:]], owner)
            return


def _batches(rows):
    runs = {}
    for row in rows:
        runs.setdefault(row.destination_id, []).append(row)
    return list(runs.values())


async def _kafka_keep(db):
    if not await outbox.enabled(db):
        return set()
    return set(str(x) for x in (await db.execute(select(EventDestination.id).where(
        EventDestination.enabled.is_(True), EventDestination.kind == 'kafka'))).scalars())


async def send_once():
    owner = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        rows = await claim(db, owner, now=now_utc())
        keep = await _kafka_keep(db)
    await kafka.close_all(keep)
    await asyncio.gather(*(deliver_batch(run, owner) for run in _batches(rows)))
    return len(rows)


async def sweep(db, *, now):
    """Drop outbox rows past retention (deliveries cascade) and spent cooldowns."""
    cfg = await db.get(EventIntegrationSettings, 1)
    cutoff = now - timedelta(days=settings.events_retention_days)
    gone = (await db.execute(
        delete(EventOutbox).where(EventOutbox.seq.in_(
            select(EventOutbox.seq).where(EventOutbox.created_at < cutoff).order_by(EventOutbox.seq).limit(5000)))
        .returning(EventOutbox.feed_seq))).scalars().all()
    stamped = [s for s in gone if s is not None]
    if cfg and stamped:
        # The feed's retention watermark: a cursor below it has missed data.
        cfg.purged_through = max(cfg.purged_through, max(stamped))
    await db.execute(delete(EventCooldown).where(EventCooldown.until < now))
    await db.commit()
    return len(gone)


async def router_loop():
    last_sweep = 0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                if await outbox.enabled(db):
                    await routing.route_once(db, now=now_utc())
                if asyncio.get_running_loop().time() - last_sweep > 3600:
                    await sweep(db, now=now_utc())
                    last_sweep = asyncio.get_running_loop().time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning('event_router.failed', error=type(exc).__name__)
        await asyncio.sleep(settings.events_router_interval_s)


async def sender_loop():
    active = set()
    try:
        while True:
            claimed = 0
            try:
                for task in list(active):
                    if task.done():
                        active.remove(task)
                        try:
                            task.result()
                        except Exception as exc:  # noqa: BLE001
                            log.warning('event_sender.delivery_failed', error=type(exc).__name__)
                owner = str(uuid.uuid4())
                room = MAX_ACTIVE - len(active)
                async with AsyncSessionLocal() as db:
                    rows = await claim(db, owner, now=now_utc(), limit=room) if room > 0 else []
                    keep = await _kafka_keep(db)
                await kafka.close_all(keep)
                for run in _batches(rows):
                    active.add(asyncio.create_task(deliver_batch(run, owner)))
                claimed = len(rows)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning('event_sender.failed', error=type(exc).__name__)
            # Work in hand: come straight back for the next run once a batch
            # frees its destination. Idle: the configured poll interval.
            await asyncio.sleep(0.1 if claimed or active else settings.events_sender_interval_s)
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        await kafka.close_all()
