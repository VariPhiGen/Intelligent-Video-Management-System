"""The event's snapshot, taken once and kept for every delivery that wants it.

`media.frame_for` asks the camera for its picture NOW. That suits a Telegram
ping sent seconds after the event, but an email retried in ten minutes or an
SFTP export retried tomorrow would attach a later picture to an earlier event.
So email and SFTP go through here: the first delivery that needs a frame takes
it (still subject to frame_for's 30-second freshness rule) and stores the bytes,
their hash and their capture caption in event_media; every later attempt and
every other destination reuses that row, whatever it holds — including "no
frame", which is final.

The row cascades with its outbox row, so the retention sweep cleans it up and
no delivery can outlive its media. A spool cap stops new captures once the
table is full, so a receiver outage cannot grow the database without bound.
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from ...config import settings
from ...db import AsyncSessionLocal
from ...models import EventMedia
from . import media

#: Labels recorded on the delivery (EventDelivery.media, String(16)).
ATTACHED = 'attached'
#: First key of the per-event capture lock (routing.py uses 6531002).
_LOCK = 6531003


async def _stored(db, seq):
    return await db.get(EventMedia, seq, populate_existing=True)


def _result(row):
    if row.state == 'ready' and row.content:
        return row.content, ATTACHED, row.caption
    return None, row.state, row.caption


async def snapshot_for(seq, envelope):
    """(jpeg bytes | None, label, caption | None) for outbox row `seq`.

    Captures at most once per event. Destinations fanned out from one event
    are sent concurrently; a per-event advisory lock makes the others wait for
    the first capture instead of each decoding the camera (seen end to end:
    three destinations, three live-frame requests).
    """
    async with AsyncSessionLocal() as db:
        await db.execute(text('SELECT pg_advisory_xact_lock(:a, :b)'), {'a': _LOCK, 'b': seq % 2_147_483_647})
        row = await _stored(db, seq)
        if row is not None:
            await db.commit()
            return _result(row)
        used = (await db.execute(select(func.coalesce(func.sum(EventMedia.size), 0)))).scalar()
        values = dict(outbox_seq=seq, created_at=datetime.now(timezone.utc))
        if used >= settings.events_media_spool_mb * 1024 * 1024:
            values.update(state='spool_full')
        else:
            env = dict(envelope)
            frame, label = await media.frame_for(env)
            if frame and len(frame) > settings.events_media_max_bytes:
                frame, label = None, 'unavailable'
            if frame:
                values.update(state='ready', source='live_frame', caption=env.get('_frame_caption'),
                              captured_at=datetime.fromisoformat(env['_frame_at']) if env.get('_frame_at') else None,
                              sha256=hashlib.sha256(frame).hexdigest(),
                              size=len(frame), content=frame)
            else:
                values.update(state=label)
        await db.execute(insert(EventMedia).values(**values).on_conflict_do_nothing(index_elements=['outbox_seq']))
        await db.commit()
        return _result(await _stored(db, seq))
