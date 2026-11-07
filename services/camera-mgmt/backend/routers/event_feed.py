"""Authenticated cursor replay and SSE. No broker required."""
import asyncio
import json
import time
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from ..config import settings
from ..db import get_db, AsyncSessionLocal
from ..models import EventOutbox, EventIntegrationSettings
from ..security import require_role
from ..services.event_delivery.routing import TYPES

router=APIRouter(tags=['event-feed'],dependencies=[Depends(require_role('admin','integration'))])

async def page(db,after,types,cameras,limit):
    cfg=await db.get(EventIntegrationSettings,1)
    if not settings.events_enabled or (cfg and not cfg.enabled) or not (settings.events_feed_enabled or (cfg and cfg.feed_enabled)):
        raise HTTPException(409,'Event feed is disabled')
    if after and cfg and after<cfg.purged_through:
        oldest=(await db.execute(select(func.min(EventOutbox.feed_seq)))).scalar()
        raise HTTPException(410,dict(oldest=oldest,purged_through=cfg.purged_through))
    selected=set(types.split(',')) if types else set()
    if selected-set(TYPES): raise HTTPException(422,'Unknown event type')
    cams=set(cameras.split(',')) if cameras else set()
    # Scan a bounded contiguous window, advancing across filtered-out records.
    # Clients must save next even for an empty items array. The cursor is
    # feed_seq, stamped by the serialised router in commit order, so a reader
    # can never pass a row that commits later (migration 066).
    rows=(await db.execute(select(EventOutbox).where(EventOutbox.feed_seq>after).order_by(EventOutbox.feed_seq).limit(limit))).scalars().all()
    items=[{**r.envelope,'cursor':r.feed_seq} for r in rows if (not selected or r.type in selected) and (not cams or str(r.camera_id) in cams)]
    return dict(items=items,next=rows[-1].feed_seq if rows else after)

@router.get('/events/feed')
async def feed(after:int=Query(0,ge=0),types:str='',cameras:str='',limit:int=Query(100,ge=1,le=500),db=Depends(get_db)):
    return await page(db,after,types,cameras,limit)

@router.get('/event-stream')
async def stream(request:Request,after:int=Query(0,ge=0),types:str='',cameras:str='',db=Depends(get_db)):
    try: cursor=int(request.headers.get('last-event-id',after))
    except ValueError: raise HTTPException(422,'Invalid Last-Event-ID') from None
    if cursor<0: raise HTTPException(422,'Invalid Last-Event-ID')
    initial=await page(db,cursor,types,cameras,100)
    await db.rollback()
    async def events():
        nonlocal cursor
        result=initial; started=time.monotonic(); heartbeat=started
        # Reconnect periodically so authentication is re-evaluated.
        while time.monotonic()-started<300 and not await request.is_disconnected():
            for item in result['items']:
                seq=item.pop('cursor')
                yield f"id: {seq}\nevent: {item['type']}\ndata: {json.dumps(item,separators=(',',':'))}\n\n"
            cursor=result['next']
            if time.monotonic()-heartbeat>=15:
                yield f'id: {cursor}\n: heartbeat\n\n'; heartbeat=time.monotonic()
            await asyncio.sleep(1)
            try:
                async with AsyncSessionLocal() as session: result=await page(session,cursor,types,cameras,100)
            except HTTPException as exc:
                yield f'event: reset\ndata: {json.dumps({"status":exc.status_code,"detail":exc.detail})}\n\n'
                return
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
