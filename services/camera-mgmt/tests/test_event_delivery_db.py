"""Real PostgreSQL acceptance tests. EVENTS_TEST_DB must name an empty scratch DB."""
import asyncio
import os
from pathlib import Path
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest
from sqlalchemy import select,text,func
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from backend.models import EventDestination,EventSubscription,EventOutbox,EventDelivery,EventIntegrationSettings,Camera
from backend.services.event_delivery import envelope,outbox,routing,sender
from backend.services.annotation_delivery import DeliveryError
from backend.services import ai_events,health
from backend.routers.event_feed import page

URL=os.getenv('EVENTS_TEST_DB')
pytestmark=pytest.mark.skipif(not URL,reason='EVENTS_TEST_DB must be an empty scratch database')
CAM=envelope.CameraRef(uuid4(),'test-gate','Test gate')

def now(): return datetime.now(timezone.utc)

def event(i=None,kind='camera.offline',at=None):
    return envelope.build(kind,str(i or uuid4()),'camera_status',CAM,at or now(),now(),{'status':'disconnected'})

@pytest.fixture(scope='module',autouse=True)
def migrated():
    if not URL: return
    async def check():
        engine=create_async_engine(URL)
        async with engine.connect() as c:
            assert (await c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"))).scalar()==0,'Refusing nonempty scratch DB'
        await engine.dispose()
    asyncio.run(check())
    from alembic.config import Config
    from alembic import command
    root=Path(__file__).resolve().parents[1]
    previous=os.environ.get('DATABASE_URL');os.environ['DATABASE_URL']=URL
    cfg=Config(str(root/'alembic.ini'));cfg.set_main_option('script_location',str(root/'migrations'))
    try:
        command.upgrade(cfg,'head');command.downgrade(cfg,'064');command.upgrade(cfg,'head')
    finally:
        if previous is None: os.environ.pop('DATABASE_URL',None)
        else: os.environ['DATABASE_URL']=previous


def run(fn):
    async def go():
        engine=create_async_engine(URL)
        factory=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with factory() as db:
                await db.execute(text('TRUNCATE event_destinations, event_outbox, event_cooldowns, analytics_events, camera_status_events CASCADE'))
                await db.execute(text('UPDATE event_integration_settings SET enabled=true,feed_enabled=false,purged_through=0,public_base_url=NULL'))
                if not await db.get(Camera,CAM.id):
                    await db.execute(text("INSERT INTO cameras(id,slug,name,rtsp_url,stage) VALUES (:id,:slug,:name,'rtsp://10.0.0.1/live','registered')"),dict(id=CAM.id,slug=CAM.slug,name=CAM.name))
                await db.commit()
                await fn(db,factory)
        finally: await engine.dispose()
    asyncio.run(go())

async def destination(db,kind='webhook',subs=1):
    d=EventDestination(id=uuid4(),name=str(uuid4()),kind=kind,config={'url':'https://10.0.0.5/hook'},enabled=True,media=False,allow_insecure=False,rate_per_min=60)
    db.add(d);await db.flush()
    for _ in range(subs):
        db.add(EventSubscription(id=uuid4(),destination_id=d.id,name='All',types=list(routing.TYPES),filters={},enabled=True))
    await db.commit();return d

async def deliveries(db):
    return (await db.execute(select(EventDelivery).order_by(EventDelivery.id))).scalars().all()

def test_no_destinations_no_outbox_and_rollback():
    async def go(db,_):
        await outbox.enqueue(db,event());await db.commit()
        assert (await db.execute(select(func.count()).select_from(EventOutbox))).scalar()==0
        await destination(db)
        await outbox.enqueue(db,event());await db.rollback()
        assert (await db.execute(select(func.count()).select_from(EventOutbox))).scalar()==0
    run(go)

def test_overlap_flapping_and_machine_fanout():
    async def go(db,_):
        chat=await destination(db,'telegram',2);machine=await destination(db)
        for kind in ('camera.offline','camera.online','camera.offline'):
            await outbox.enqueue(db,event(kind=kind))
        await db.commit()
        assert await routing.route_once(db,now=now())==3
        assert await routing.route_once(db,now=now())==0
        rows=await deliveries(db)
        assert [d.state for d in rows if d.destination_id==chat.id]==['pending','suppressed','suppressed']
        assert all(len(d.subscription_ids)==2 for d in rows if d.destination_id==chat.id)
        assert [d.state for d in rows if d.destination_id==machine.id]==['pending']*3
    run(go)

def test_recipients_and_endpoint_snapshotted_no_backfill():
    async def go(db,_):
        d=await destination(db)
        await outbox.enqueue(db,event());await db.commit()
        d.config={'url':'https://10.0.0.6/changed'}
        await destination(db)
        await routing.route_once(db,now=now())
        rows=await deliveries(db)
        assert len(rows)==1 and rows[0].destination['config']['url']=='https://10.0.0.5/hook'
    run(go)

def test_ingest_duplicate_close_preserves_original_fields():
    async def go(db,_):
        await destination(db)
        row=dict(id=uuid4(),camera_id=CAM.id,activity='intrusion',zone='Original',started_at=now(),ended_at=None,
            confidence=None,object_class='person',attributes={},source='cpu',expires_at=now()+timedelta(hours=1))
        a=await ai_events.record(db,[row],cameras={CAM.id:CAM});assert a.counts['inserted']==1
        a=await ai_events.record(db,[row],cameras={CAM.id:CAM});assert a.counts['duplicates']==1
        a=await ai_events.record(db,[{**row,'ended_at':now(),'zone':'Wrong'}],cameras={CAM.id:CAM});assert a.counts['closed']==1
        rows=(await db.execute(select(EventOutbox).order_by(EventOutbox.seq))).scalars().all()
        assert [r.type for r in rows]==['ai.event.created','ai.event.closed']
        assert rows[1].envelope['data']['zone']=='Original'
    run(go)

def test_lease_recovery_stale_owner_and_retry_after():
    async def go(db,_):
        await destination(db);e=event();await outbox.enqueue(db,e);await db.commit();t=now()
        await routing.route_once(db,now=t)
        a=(await sender.claim(db,'A',now=t))[0]
        b=(await sender.claim(db,'B',now=t+timedelta(seconds=70)))[0]
        assert a.id==b.id and b.attempts==2
        assert not await sender.finalise(db,a.id,'A',ok=True,now=t+timedelta(seconds=71))
        assert await sender.finalise(db,b.id,'B',ok=False,error=DeliveryError('busy',retry_after=90),now=t+timedelta(seconds=71))
        await db.refresh(b)
        assert b.state=='retrying' and b.next_attempt_at>=t+timedelta(seconds=161)
        stored=await db.get(EventOutbox,b.outbox_seq);assert stored.envelope['message_id']==e['message_id']
    run(go)

def test_stale_chat_expires_and_machine_survives():
    async def go(db,_):
        chat=await destination(db,'telegram');machine=await destination(db)
        await outbox.enqueue(db,event(at=now()-timedelta(seconds=200)));await db.commit()
        await routing.route_once(db,now=now())
        rows=await deliveries(db)
        assert next(d for d in rows if d.destination_id==chat.id).state=='expired'
        claimed=await sender.claim(db,'owner',now=now());assert len(claimed)==1 and claimed[0].destination_id==machine.id
    run(go)

def test_success_failure_and_global_pause():
    async def go(db,_):
        dest=await destination(db);await outbox.enqueue(db,event());await db.commit();await routing.route_once(db,now=now())
        control=await db.get(EventIntegrationSettings,1);control.enabled=False;await db.commit()
        assert await sender.claim(db,'owner',now=now())==[]
        control.enabled=True;await db.commit();t=now()
        row=(await sender.claim(db,'owner',now=t))[0]
        assert await sender.finalise(db,row.id,'owner',ok=True,provider_ref='42',media='attached',now=t+timedelta(seconds=1))
        await db.refresh(row);await db.refresh(dest)
        assert row.state=='sent' and row.provider_ref=='42' and dest.last_success_at
    run(go)

def test_feed_pagination_filters_and_retention():
    async def go(db,_):
        control=await db.get(EventIntegrationSettings,1);control.feed_enabled=True;await db.commit()
        for kind in ('camera.offline','camera.online','camera.offline'): await outbox.enqueue(db,event(kind=kind))
        await db.commit();await routing.route_once(db,now=now())
        a=await page(db,0,'camera.online','',1);assert not a['items'] and a['next']>0
        b=await page(db,a['next'],'camera.online','',1);assert len(b['items'])==1
        c=await page(db,b['next'],'camera.online','',1);assert not c['items'] and c['next']>b['next']
        await db.execute(text("UPDATE event_outbox SET created_at=now()-interval '8 days'"));await db.commit()
        assert await sender.sweep(db,now=now())==3
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc: await page(db,a['next'],'','',100)
        assert exc.value.status_code==410
    run(go)

def test_health_restart_and_real_transitions(monkeypatch):
    async def go(db,factory):
        await destination(db);monkeypatch.setattr(health,'AsyncSessionLocal',factory)
        await db.execute(text("UPDATE cameras SET health_status='unknown' WHERE id=:id"),{'id':CAM.id});await db.commit()
        for status in ('connected','unknown','connected','disconnected','unknown','error','connected'):
            await health._update_db_health(CAM.slug,status,update_last_seen=False)
        types=(await db.execute(select(EventOutbox.type).order_by(EventOutbox.seq))).scalars().all()
        assert types==['camera.offline','camera.online']
    run(go)

@pytest.mark.parametrize('mode',['log_only','armed'])
def test_alert_outbox_even_when_disarmed(monkeypatch,mode):
    from backend.services import rules_engine
    from backend.models import AlertRule,Alert
    from conftest import StubRedis
    redis=StubRedis()
    async def get_redis(): return redis
    monkeypatch.setattr(rules_engine,'get_redis',get_redis)
    rules_engine._local_cooldown.clear()
    async def go(db,_):
        await destination(db)
        rule=AlertRule(id=uuid4(),name=str(uuid4()),enabled=True,mode=mode,activity='intrusion',scope_kind='any',
            severity='warning',conditions={},cooldown_s=0,max_event_age_s=30,actions=[],created_at=now(),updated_at=now())
        db.add(rule);await db.commit()
        async def load(db): return [rule],False
        monkeypatch.setattr(rules_engine,'_load_rules',load)
        if mode=='armed': await rules_engine.set_arm(False,by='test',reason='test')
        ev=dict(id=uuid4(),camera_id=CAM.id,camera_slug=CAM.slug,camera_name=CAM.name,activity='intrusion',started_at=now(),attributes={},confidence=.9)
        ids=await rules_engine.evaluate(db,[ev],now=now())
        assert len(ids)==1
        assert await db.get(Alert,ids[0]) is not None
        stored=(await db.execute(select(EventOutbox))).scalar_one()
        assert stored.type=='alert.raised' and stored.envelope['data']['alert_id']==str(ids[0])
    run(go)

def test_delivery_unique_and_permanent_failure():
    from sqlalchemy.exc import IntegrityError
    async def go(db,_):
        await destination(db);await outbox.enqueue(db,event());await db.commit();await routing.route_once(db,now=now())
        row=(await sender.claim(db,'owner',now=now()))[0]
        values=dict(outbox_seq=row.outbox_seq,destination_id=row.destination_id,destination=row.destination,
            subscription_ids=row.subscription_ids,deadline_at=row.deadline_at)
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(EventDelivery(**values));await db.flush()
        await sender.finalise(db,row.id,'owner',ok=False,error=DeliveryError('Rejected',permanent=True),now=now())
        await db.refresh(row);assert row.state=='failed'
    run(go)

def test_slow_receiver_does_not_delay_other_receiver(monkeypatch):
    async def go(db,factory):
        a=await destination(db);b=await destination(db)
        await outbox.enqueue(db,event());await db.commit();await routing.route_once(db,now=now())
        entered=asyncio.Event();release=asyncio.Event();fast=asyncio.Event()
        async def adapter(dest,env,frame):
            if dest.id==str(a.id):
                entered.set();await release.wait();raise DeliveryError('Unavailable')
            fast.set();return 'ok'
        monkeypatch.setattr(sender,'AsyncSessionLocal',factory)
        monkeypatch.setitem(sender.ADAPTERS,'webhook',adapter)
        task=asyncio.create_task(sender.send_once())
        await asyncio.wait_for(entered.wait(),2);await asyncio.wait_for(fast.wait(),2)
        assert not task.done()
        release.set();assert await task==2
        rows=await deliveries(db)
        assert next(r for r in rows if r.destination_id==a.id).state=='retrying'
        assert next(r for r in rows if r.destination_id==b.id).state=='sent'
    run(go)

def test_sse_resumes_from_last_event_id():
    from backend.routers.event_feed import stream
    class Request:
        headers={}
        async def is_disconnected(self): return False
    async def go(db,_):
        cfg=await db.get(EventIntegrationSettings,1);cfg.feed_enabled=True;await db.commit()
        await outbox.enqueue(db,event());await outbox.enqueue(db,event());await db.commit()
        await routing.route_once(db,now=now())
        rows=(await db.execute(select(EventOutbox).order_by(EventOutbox.feed_seq))).scalars().all()
        request=Request();request.headers={'last-event-id':str(rows[0].feed_seq)}
        expected=rows[1].feed_seq
        response=await stream(request,after=0,types='',cameras='',db=db)
        gen=response.body_iterator
        first=await anext(gen)
        assert f'id: {expected}\n' in first and 'event: camera.offline' in first
        await gen.aclose()
    run(go)

def test_idle_destinations_do_not_starve_queued_receiver():
    async def go(db,_):
        for _ in range(22): await destination(db,subs=0)
        target=await destination(db)
        await outbox.enqueue(db,event());await db.commit();await routing.route_once(db,now=now())
        rows=await sender.claim(db,'owner',now=now(),limit=1)
        assert len(rows)==1 and rows[0].destination_id==target.id
    run(go)


# ── Regressions found in review (2026-10-27) ────────────────────────────────

def test_machine_destination_is_not_capped_at_one_per_second(monkeypatch):
    # 065 claimed one delivery per destination per 1 s tick: 6 of 50 in 5 s.
    async def go(db,factory):
        d=await destination(db);d.rate_per_min=6000;await db.commit()      # the webhook default since 066
        for _ in range(50): await outbox.enqueue(db,event())
        await db.commit();await routing.route_once(db,now=now())
        async def fast(dest,env,frame): return None
        async def on(_db): return True
        monkeypatch.setitem(sender.ADAPTERS,'webhook',fast)
        monkeypatch.setattr(sender,'AsyncSessionLocal',factory)
        monkeypatch.setattr(sender.outbox,'enabled',on)
        task=asyncio.create_task(sender.sender_loop())
        await asyncio.sleep(3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        async with factory() as s:
            sent=(await s.execute(select(func.count()).select_from(EventDelivery).where(EventDelivery.state=='sent'))).scalar()
        assert sent==50
    run(go)

def test_failed_send_hands_the_rest_of_the_run_back(monkeypatch):
    # Strict order: nothing may overtake a delivery that is waiting to retry.
    async def go(db,factory):
        d=await destination(db);d.rate_per_min=6000;await db.commit()
        for _ in range(3): await outbox.enqueue(db,event())
        await db.commit();await routing.route_once(db,now=now())
        calls=[]
        async def failing(dest,env,frame):
            calls.append(env['message_id']);raise DeliveryError('Receiver down')
        monkeypatch.setitem(sender.ADAPTERS,'webhook',failing)
        monkeypatch.setattr(sender,'AsyncSessionLocal',factory)
        rows=await sender.claim(db,'owner',now=now())
        assert len(rows)==3
        await sender.deliver_batch(rows,'owner')
        assert len(calls)==1
        db.expire_all()
        states=[(d.state,d.attempts,d.lease_owner) for d in await deliveries(db)]
        assert states[0][:2]==('retrying',1)
        assert states[1:]==[('pending',0,None),('pending',0,None)]
        assert await sender.claim(db,'owner',now=now())==[]   # head is waiting; nothing jumps it
    run(go)

def test_short_outage_tells_chat_the_camera_is_back():
    # 065 shared one cooldown between offline and online, so a 10 s dropout sent
    # "offline" and suppressed "online": the guard believed the camera was dead.
    async def go(db,_):
        await destination(db,'telegram')
        t0=now()
        await outbox.enqueue(db,event(kind='camera.offline',at=t0));await db.commit()
        await routing.route_once(db,now=t0)
        await outbox.enqueue(db,event(kind='camera.online',at=t0+timedelta(seconds=10)));await db.commit()
        await routing.route_once(db,now=t0+timedelta(seconds=10))
        off,on=await deliveries(db)
        assert off.state=='pending' and on.state=='pending'
        assert on.next_attempt_at>=t0+timedelta(seconds=59)        # deferred to the end of the window
        assert on.deadline_at>on.next_attempt_at                     # and still fresh when it goes
    run(go)

def test_flap_that_ends_where_it_started_sends_nothing_more():
    async def go(db,_):
        await destination(db,'telegram')
        t0=now()
        for i,kind in enumerate(('camera.offline','camera.online','camera.offline','camera.online','camera.offline')):
            t=t0+timedelta(seconds=5*i)
            await outbox.enqueue(db,event(kind=kind,at=t));await db.commit()
            await routing.route_once(db,now=t)
        states=[(d.state,d.last_error) for d in await deliveries(db)]
        assert states[0]==('pending',None)
        assert [s for s,_ in states[1:]]==['suppressed']*4
        assert states[-1]==('suppressed','unchanged')               # still offline: already told
    run(go)

def test_flap_that_ends_online_is_delivered_once():
    async def go(db,_):
        await destination(db,'telegram')
        t0=now()
        for i,kind in enumerate(('camera.offline','camera.online','camera.offline','camera.online')):
            t=t0+timedelta(seconds=5*i)
            await outbox.enqueue(db,event(kind=kind,at=t));await db.commit()
            await routing.route_once(db,now=t)
        live=[d for d in await deliveries(db) if d.state=='pending']
        assert len(live)==2
        last=(await db.execute(select(EventOutbox).where(EventOutbox.seq==live[-1].outbox_seq))).scalar_one()
        assert last.type=='camera.online'
    run(go)

def test_machine_destination_still_gets_every_status_change():
    async def go(db,_):
        await destination(db)
        for kind in ('camera.offline','camera.online','camera.offline'):
            await outbox.enqueue(db,event(kind=kind))
        await db.commit();await routing.route_once(db,now=now())
        assert [d.state for d in await deliveries(db)]==['pending']*3
    run(go)

def test_health_check_and_ingest_do_not_deadlock():
    # 065: health held the camera row and waited for a global advisory lock that
    # ingest held while it waited on the same row (FK) -> "deadlock detected".
    async def go(db,factory):
        await destination(db)
        async with factory() as ingest, factory() as checker:
            await outbox.enqueue(ingest,event())
            await checker.execute(text('SELECT id FROM cameras WHERE id=:i FOR UPDATE'),{'i':CAM.id})
            pending=asyncio.create_task(outbox.enqueue(checker,event()))
            await asyncio.sleep(0.3)
            done=asyncio.create_task(ingest.execute(text(
                "INSERT INTO analytics_events(id,camera_id,activity,started_at,expires_at,source) "
                "VALUES (:id,:c,'intrusion',now(),now()+interval '1 day','test')"),{'id':str(uuid4()),'c':CAM.id}))
            await asyncio.wait_for(pending,5);await checker.commit()
            await asyncio.wait_for(done,5);await ingest.commit()
        assert (await db.execute(select(func.count()).select_from(EventOutbox))).scalar()==2
    run(go)

def test_feed_cursor_never_skips_a_row_that_commits_late():
    # seq is assigned at INSERT; a reader paging on seq passed a lower seq that
    # committed after a higher one. feed_seq is stamped by the router in order.
    async def go(db,factory):
        cfg=await db.get(EventIntegrationSettings,1);cfg.feed_enabled=True;await db.commit()
        async with factory() as slow, factory() as fast:
            late=event();await outbox.enqueue(slow,late)            # lower seq, not committed
            early=event();await outbox.enqueue(fast,early);await fast.commit()
            await routing.route_once(db,now=now())
            first=await page(db,0,'','',100)
            assert [i['message_id'] for i in first['items']]==[early['message_id']]
            await slow.commit()
        await routing.route_once(db,now=now())
        second=await page(db,first['next'],'','',100)
        assert [i['message_id'] for i in second['items']]==[late['message_id']]
    run(go)

def test_route_cache_sees_a_destination_enabled_after_commit():
    async def go(db,_):
        await outbox.enqueue(db,event());await db.commit()          # caches "no destinations"
        await destination(db)                                        # commits
        await outbox.enqueue(db,event());await db.commit()
        assert (await db.execute(select(func.count()).select_from(EventOutbox))).scalar()==1
    run(go)

def test_permanent_failure_does_not_hold_up_the_run(monkeypatch):
    # A refused target is never retried, so the run must carry on past it.
    async def go(db,factory):
        d=await destination(db);d.rate_per_min=6000;await db.commit()
        for _ in range(3): await outbox.enqueue(db,event())
        await db.commit();await routing.route_once(db,now=now())
        async def refused(dest,env,frame): raise DeliveryError('Destination target rejected',permanent=True)
        monkeypatch.setitem(sender.ADAPTERS,'webhook',refused)
        monkeypatch.setattr(sender,'AsyncSessionLocal',factory)
        await sender.deliver_batch(await sender.claim(db,'owner',now=now()),'owner')
        db.expire_all()
        assert [d.state for d in await deliveries(db)]==['failed']*3
    run(go)

def test_playback_address_saved_in_ui_overrides_environment(monkeypatch):
    monkeypatch.setattr(envelope.settings,'events_public_base_url','https://env.example.lan')
    async def go(db,_):
        await destination(db)
        await outbox.enqueue(db,event(at=datetime(2026,10,28,5,0,tzinfo=timezone.utc)));await db.commit()
        cfg=await db.get(EventIntegrationSettings,1);cfg.public_base_url='https://vms.site.lan';await db.commit()
        await outbox.enqueue(db,event(at=datetime(2026,10,28,5,0,tzinfo=timezone.utc)));await db.commit()
        cfg.public_base_url=None;await db.commit()
        links=[r.envelope['links']['playback'] for r in (await db.execute(select(EventOutbox).order_by(EventOutbox.seq))).scalars()]
        assert links==['https://env.example.lan/#/playback?cam=test-gate&t=1793163600',
                       'https://vms.site.lan/#/playback?cam=test-gate&t=1793163600']
    run(go)


# ── email / SFTP: the event's snapshot is taken once (068 event_media) ───────

def alert(at=None):
    return envelope.build('alert.raised',str(uuid4()),'alert',CAM,at or now(),now(),
        dict(rule_name='Intrusion',severity='critical',rule_id=None,event_ids=[]))

async def media_destination(db,kind):
    d=EventDestination(id=uuid4(),name=str(uuid4()),kind=kind,enabled=True,media=True,allow_insecure=False,rate_per_min=6000,
        config={'to':['ops@site.lan'],'cc':[],'bcc':[]} if kind=='email' else
               {'host':'nas.lan','port':22,'username':'vms','auth':'password','root':'.','host_key_fingerprint':'SHA256:'+'A'*43,'snapshot_required':False})
    db.add(d);await db.flush()
    db.add(EventSubscription(id=uuid4(),destination_id=d.id,name='All',types=list(routing.TYPES),filters={},enabled=True))
    await db.commit();return d

def test_a_retry_reuses_the_first_snapshot_never_a_newer_one(monkeypatch):
    """The finding behind event_media: a retry must not attach a later live frame."""
    from backend.services.event_delivery import evidence,media
    async def go(db,factory):
        await media_destination(db,'email');await media_destination(db,'sftp')
        await outbox.enqueue(db,alert());await db.commit()
        await routing.route_once(db,now=now())
        frames=iter([b'first-frame',b'later-frame',b'even-later'])
        async def live(env):
            env['_frame_caption']='Frame captured around 09:30:01 UTC (1 s after event)'
            env['_frame_at']=now().isoformat()
            return next(frames),'attached'
        monkeypatch.setattr(media,'frame_for',live)
        monkeypatch.setattr(evidence,'AsyncSessionLocal',factory)
        monkeypatch.setattr(sender,'AsyncSessionLocal',factory)
        got=[]
        async def flaky(dest,env,frame):
            got.append((dest.kind,frame,env.get('_frame_caption'),env.get('_media')))
            if len(got)==1: raise DeliveryError('Mail server connection failed')
        monkeypatch.setitem(sender.ADAPTERS,'email',flaky);monkeypatch.setitem(sender.ADAPTERS,'sftp',flaky)
        for _ in range(2):
            for row in await sender.claim(db,'owner',now=now()+timedelta(minutes=5) if got else now()):
                await sender.deliver(row,'owner')
        assert [g[1] for g in got]==[b'first-frame']*3, got
        assert all(g[2].startswith('Frame captured') and g[3]=='attached' for g in got)
        stored=(await db.execute(text('SELECT state,size,sha256,captured_at FROM event_media'))).all()
        assert len(stored)==1 and stored[0][0]=='ready' and stored[0][1]==len(b'first-frame') and stored[0][3] is not None
        db.expire_all()
        assert sorted(d.media for d in await deliveries(db))==['attached','attached']
    run(go)

def test_no_frame_is_final_too(monkeypatch):
    from backend.services.event_delivery import evidence,media
    async def go(db,factory):
        await media_destination(db,'email')
        await outbox.enqueue(db,alert());await db.commit()
        seq=(await db.execute(select(EventOutbox.seq))).scalar()
        answers=iter([(None,'skipped'),(b'late','attached')])
        async def live(env): return next(answers)
        monkeypatch.setattr(media,'frame_for',live);monkeypatch.setattr(evidence,'AsyncSessionLocal',factory)
        assert await evidence.snapshot_for(seq,alert())==(None,'skipped',None)
        assert await evidence.snapshot_for(seq,alert())==(None,'skipped',None)
    run(go)

def test_spool_cap_stops_new_snapshots_and_rows_go_with_their_event(monkeypatch):
    from backend.services.event_delivery import evidence,media
    async def go(db,factory):
        await media_destination(db,'sftp')
        for _ in range(2): await outbox.enqueue(db,alert())
        await db.commit()
        a,b=(await db.execute(select(EventOutbox.seq).order_by(EventOutbox.seq))).scalars().all()
        monkeypatch.setattr(evidence.settings,'events_media_spool_mb',1)
        async def live(env): return b'x'*(1024*1024),'attached'
        monkeypatch.setattr(media,'frame_for',live);monkeypatch.setattr(evidence,'AsyncSessionLocal',factory)
        monkeypatch.setattr(evidence.settings,'events_media_max_bytes',2*1024*1024)
        assert (await evidence.snapshot_for(a,alert()))[1]=='attached'
        assert (await evidence.snapshot_for(b,alert()))[1]=='spool_full'
        await db.execute(text('DELETE FROM event_outbox WHERE seq=:s'),{'s':a});await db.commit()
        assert (await db.execute(text('SELECT outbox_seq FROM event_media'))).scalars().all()==[b]
    run(go)

def test_email_and_sftp_routing_defaults():
    async def go(db,_):
        mail=await media_destination(db,'email');sftp=await media_destination(db,'sftp')
        t=now()
        for _ in range(2): await outbox.enqueue(db,alert(at=t))
        await db.commit();await routing.route_once(db,now=t)
        rows=await deliveries(db)
        assert [d.state for d in rows if d.destination_id==mail.id]==['pending','suppressed']   # 60 s cooldown
        assert [d.state for d in rows if d.destination_id==sftp.id]==['pending','pending']      # every event kept
        by={d.destination_id:d.deadline_at for d in rows}
        assert by[mail.id]-t==timedelta(hours=1) and by[sftp.id]-t==timedelta(days=7)
    run(go)

def test_concurrent_destinations_take_one_snapshot(monkeypatch):
    """Seen end to end: three destinations of one event made three live-frame requests."""
    from backend.services.event_delivery import evidence,media
    async def go(db,factory):
        await media_destination(db,'email')
        await outbox.enqueue(db,alert());await db.commit()
        seq=(await db.execute(select(EventOutbox.seq))).scalar()
        calls=[]
        async def live(env):
            calls.append(1);await asyncio.sleep(0.3);return b'frame-%d'%len(calls),'attached'
        monkeypatch.setattr(media,'frame_for',live);monkeypatch.setattr(evidence,'AsyncSessionLocal',factory)
        got=await asyncio.gather(*(evidence.snapshot_for(seq,alert()) for _ in range(3)))
        assert len(calls)==1 and {g[0] for g in got}=={b'frame-1'}
    run(go)
