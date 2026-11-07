"""Admin-only configuration and diagnostics for optional event exports."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from ..db import get_db
from ..crypto import encrypt, decrypt
from ..models import EventDestination, EventSubscription, EventDelivery, EventOutbox, EventIntegrationSettings, Camera, AlertRule, AnalyticsActivityType
from ..security import require_role
from ..services import audit as audit_svc
from ..services.event_delivery.schemas import DestinationIn, DestinationPatch, SubscriptionIn, SubscriptionPatch, ControlIn, TestIn, SmtpIn, HostKeyProbeIn, MEDIA_KINDS
from ..services.event_delivery.outbox import destination_snapshot
from ..services.event_delivery.envelope import build, CameraRef, public_base_url, with_playback
from ..services.event_delivery.routing import max_age
from ..services.event_delivery.adapters import ADAPTERS, sftp as sftp_adapter
from ..services.event_delivery.media import frame_for
from ..services.annotation_delivery import DeliveryError

router=APIRouter(tags=['event-integrations'],dependencies=[Depends(require_role('admin'))])
ADMIN=require_role('admin')

def now(): return datetime.now(timezone.utc)

def public(dest):
    return {**{k:getattr(dest,k) for k in ('id','name','kind','config','enabled','media','allow_insecure',
        'rate_per_min','max_age_s','last_success_at','last_error_at','last_error','created_at','updated_at')},'has_secret':bool(dest.secret_enc)}

def subscription(row):
    return {k:getattr(row,k) for k in ('id','destination_id','name','types','filters','cooldown_s','enabled')}

async def get_row(db,cls,id):
    row=await db.get(cls,id)
    if row is None: raise HTTPException(404,'Not found')
    return row

async def save(db,request,principal,action,id):
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409,'Name already exists or referenced item changed') from None
    await audit_svc.record(request,principal,action,target=str(id),detail={})

_FIELD_HINTS={'from_address':'Enter a valid sender email address','host_key_fingerprint':'Host key fingerprint must look like SHA256:… (43 characters)',
    'username':'Enter a valid username','to':'Add at least one recipient'}

def reason(exc,default):
    """An operator-facing reason for a validation failure, never the input.

    Only our own validators' messages (static text) and fixed per-field hints
    are returned: Pydantic's own messages can quote the submitted value, which
    may be a credential."""
    errors=exc.errors() if isinstance(exc,ValidationError) else []
    for e in errors:
        if e.get('type')=='value_error' and e.get('ctx',{}).get('error'):
            return str(e['ctx']['error'])
    for e in errors:
        for part in e.get('loc',()):
            if part in _FIELD_HINTS: return _FIELD_HINTS[part]
    return default

def validate_destination(data):
    try: body=DestinationIn.model_validate(data)
    except (ValidationError,ValueError) as exc:
        # Never return Pydantic input values: they can contain credentials.
        raise HTTPException(422,reason(exc,'Invalid destination configuration, target or credential')) from None
    cfg=body.config
    needs=body.kind in ('telegram','slack','sftp') or (body.kind=='webhook' and cfg['auth']!='none') or (body.kind=='kafka' and cfg['security']=='sasl_ssl')
    if body.enabled and needs and (not body.secret or not body.secret.get_secret_value()):
        raise HTTPException(422,'A credential is required before enabling this destination')
    if body.enabled and body.kind=='kafka' and cfg['security']=='sasl_ssl' and not cfg.get('username'):
        raise HTTPException(422,'Kafka SASL username is required')
    return body

@router.get('/event-integrations/settings')
async def get_settings(db=Depends(get_db)):
    from ..config import settings
    row=await get_row(db,EventIntegrationSettings,1)
    # public_base_url is what the UI saved; environment_base_url is the fallback
    # used while it is empty, shown so the admin knows links already work.
    return dict(enabled=row.enabled,feed_enabled=row.feed_enabled,environment_enabled=settings.events_enabled,
        public_base_url=row.public_base_url or '',environment_base_url=settings.events_public_base_url,
        retention_days=settings.events_retention_days)

def smtp_public(row):
    return dict(configured=bool(row.smtp),**(row.smtp or {}),has_password=bool(row.smtp_secret_enc))

@router.get('/event-integrations/smtp')
async def get_smtp(db=Depends(get_db)):
    return smtp_public(await get_row(db,EventIntegrationSettings,1))

@router.put('/event-integrations/smtp')
async def put_smtp(body:dict,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    try: valid=SmtpIn.model_validate(body)
    except (ValidationError,ValueError) as exc:
        raise HTTPException(422,reason(exc,'Invalid mail server settings')) from None
    row=await get_row(db,EventIntegrationSettings,1)
    row.smtp=valid.model_dump(exclude={'password'})
    if valid.password is not None:
        row.smtp_secret_enc=encrypt(valid.password.get_secret_value()) if valid.password.get_secret_value() else None
    if valid.username and not row.smtp_secret_enc:
        await db.rollback()
        raise HTTPException(422,'Enter the mail server password for this login')
    await save(db,request,principal,'event_integrations.smtp_updated',1)
    return smtp_public(row)

@router.delete('/event-integrations/smtp')
async def delete_smtp(request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=await get_row(db,EventIntegrationSettings,1)
    row.smtp=None;row.smtp_secret_enc=None
    await save(db,request,principal,'event_integrations.smtp_deleted',1)
    return smtp_public(row)

@router.post('/event-integrations/sftp-host-key')
async def sftp_host_key(body:HostKeyProbeIn,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    """What the server presents, for the admin to compare with the server's own
    fingerprint before saving. Nothing is stored or trusted here."""
    await audit_svc.record(request,principal,'event_destination.host_key_probed',target=body.host,detail={'port':body.port})
    await db.commit()
    try: seen=await sftp_adapter.probe_host_key(body.host,body.port)
    except ValueError: raise HTTPException(422,'Use a valid, non-local host') from None
    except Exception: seen=None
    if not seen: raise HTTPException(502,'Could not read a host key from that server')
    return {'fingerprint':seen}

@router.put('/event-integrations/settings')
async def put_settings(body:ControlIn,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=await get_row(db,EventIntegrationSettings,1)
    row.enabled=body.enabled; row.feed_enabled=body.feed_enabled
    if body.public_base_url is not None: row.public_base_url=body.public_base_url or None
    await save(db,request,principal,'event_integrations.updated',1)
    return await get_settings(db)

@router.get('/event-integrations/options')
async def options(db=Depends(get_db)):
    cameras=(await db.execute(select(Camera).where(Camera.slug.isnot(None)))).scalars().all()
    rules=(await db.execute(select(AlertRule))).scalars().all()
    acts=(await db.execute(select(AnalyticsActivityType))).scalars().all()
    return dict(cameras=[dict(id=str(c.id),name=c.name or c.slug) for c in cameras],
        rules=[dict(id=str(r.id),name=r.name) for r in rules],activities=[dict(id=a.key,name=a.label) for a in acts])

@router.get('/event-destinations')
async def destinations(db=Depends(get_db)):
    return [public(d) for d in (await db.execute(select(EventDestination).order_by(EventDestination.created_at))).scalars()]

@router.post('/event-destinations',status_code=201)
async def create_destination(body:dict,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    body=validate_destination({**body, "enabled": False})
    data=body.model_dump(exclude={'secret'})
    row=EventDestination(id=uuid.uuid4(),**data,secret_enc=encrypt(body.secret.get_secret_value()) if body.secret else None)
    db.add(row)
    await save(db,request,principal,'event_destination.created',row.id)
    await db.refresh(row)
    return public(row)

@router.get('/event-destinations/{id}')
async def destination(id:uuid.UUID,db=Depends(get_db)):
    return public(await get_row(db,EventDestination,id))

@router.patch('/event-destinations/{id}')
async def update_destination(id:uuid.UUID,body:DestinationPatch,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=await get_row(db,EventDestination,id)
    data={k:getattr(row,k) for k in ('name','kind','config','enabled','media','allow_insecure','rate_per_min','max_age_s')}
    data['secret']=decrypt(row.secret_enc)
    data.update(body.model_dump(exclude_unset=True))
    validated=validate_destination(data)
    if validated.enabled and validated.kind=='email' and not (await get_row(db,EventIntegrationSettings,1)).smtp:
        raise HTTPException(422,'Set up the mail server before enabling an email destination')
    for k,v in validated.model_dump(exclude={'secret'}).items(): setattr(row,k,v)
    if 'secret' in body.model_fields_set:
        row.secret_enc=encrypt(validated.secret.get_secret_value()) if validated.secret else None
    row.updated_at=now()
    await save(db,request,principal,'event_destination.updated',id)
    return public(row)

@router.delete('/event-destinations/{id}')
async def delete_destination(id:uuid.UUID,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=await get_row(db,EventDestination,id)
    await db.delete(row)
    await save(db,request,principal,'event_destination.deleted',id)
    return {'ok':True}

@router.post('/event-destinations/{id}/test')
async def test_destination(id:uuid.UUID,body:TestIn,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=await get_row(db,EventDestination,id)
    cam=CameraRef(uuid.UUID(int=0),'','Synthetic test camera')
    if body.include_live_frame:
        if row.kind not in MEDIA_KINDS or not body.camera_id: raise HTTPException(422,'Select a camera for the live frame test')
        camera=await get_row(db,Camera,body.camera_id)
        cam=CameraRef(camera.id,camera.slug,camera.name or camera.slug)
    # SFTP keeps a bundle per message_id and skips one already complete, so each
    # SFTP test needs its own id; elsewhere the fixed id lets receivers dedupe.
    subject='test' if row.kind!='sftp' else f'test-{uuid.uuid4()}'
    env=build('alert.raised',subject,'alert',cam,now(),now(),dict(rule_name='VMS test notification',severity='info',rule_id='test',event_ids=[]))
    with_playback(env,public_base_url(await db.get(EventIntegrationSettings,1)))
    target=SimpleNamespace(**destination_snapshot(row))
    # Audit the explicit request before any external side effect. No real frame
    # is sent unless the admin explicitly selected it in this request.
    await audit_svc.record(request,principal,'event_destination.tested',target=str(id),detail={'live_frame':body.include_live_frame})
    await db.commit()
    try:
        async with asyncio.timeout(35):
            frame,label=await frame_for(env) if body.include_live_frame else (None,'skipped')
            env['_media']=label if target.media or row.kind=='telegram' else 'disabled'
            ref=await ADAPTERS[target.kind](target,env,frame)
        return dict(ok=True,error=None,provider_ref=ref,media=label)
    except DeliveryError as exc: return dict(ok=False,error=str(exc),provider_ref=None)
    except Exception: return dict(ok=False,error='Test delivery failed',provider_ref=None)

@router.get('/event-destinations/{id}/status')
async def destination_status(id:uuid.UUID,db=Depends(get_db)):
    row=await get_row(db,EventDestination,id)
    counts=(await db.execute(select(EventDelivery.state,func.count()).where(EventDelivery.destination_id==id,
        EventDelivery.created_at>=now()-timedelta(days=1)).group_by(EventDelivery.state))).all()
    d=dict(counts)
    queued=(await db.execute(select(func.count()).select_from(EventDelivery).where(EventDelivery.destination_id==id,
        EventDelivery.state.in_(['pending','sending','retrying'])))).scalar()
    return dict(queued=queued,sent_24h=d.get('sent',0),failed_24h=d.get('failed',0),
        last_success_at=row.last_success_at,last_error_at=row.last_error_at,last_error=row.last_error)

@router.get('/event-subscriptions')
async def subscriptions(db=Depends(get_db)):
    return [subscription(s) for s in (await db.execute(select(EventSubscription).order_by(EventSubscription.created_at))).scalars()]

@router.post('/event-subscriptions',status_code=201)
async def create_subscription(body:SubscriptionIn,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    await get_row(db,EventDestination,body.destination_id)
    data=body.model_dump(); data['filters']=body.filters.model_dump(mode='json')
    row=EventSubscription(id=uuid.uuid4(),**data); db.add(row)
    await save(db,request,principal,'event_subscription.created',row.id)
    return subscription(row)

@router.patch('/event-subscriptions/{id}')
async def update_subscription(id:uuid.UUID,body:SubscriptionPatch,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=await get_row(db,EventSubscription,id)
    data=subscription(row); data.pop('id'); data.update(body.model_dump(exclude_unset=True))
    try: valid=SubscriptionIn.model_validate(data)
    except ValidationError: raise HTTPException(422,'Invalid subscription filters') from None
    for k,v in valid.model_dump().items(): setattr(row,k,v)
    row.filters=valid.filters.model_dump(mode='json'); row.updated_at=now()
    await save(db,request,principal,'event_subscription.updated',id)
    return subscription(row)

@router.delete('/event-subscriptions/{id}')
async def delete_subscription(id:uuid.UUID,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    await db.delete(await get_row(db,EventSubscription,id))
    await save(db,request,principal,'event_subscription.deleted',id)
    return {'ok':True}

@router.get('/event-deliveries')
async def deliveries(destination:uuid.UUID|None=None,state:str|None=None,before:int|None=None,
    limit:int=Query(50,ge=1,le=200),db=Depends(get_db)):
    q=select(EventDelivery,EventOutbox,EventDestination.name).join(EventOutbox,EventDelivery.outbox_seq==EventOutbox.seq).join(EventDestination,EventDelivery.destination_id==EventDestination.id)
    if destination: q=q.where(EventDelivery.destination_id==destination)
    if state: q=q.where(EventDelivery.state==state)
    if before is not None: q=q.where(EventDelivery.id<before)
    rows=(await db.execute(q.order_by(EventDelivery.id.desc()).limit(limit+1))).all()
    items=[]
    for d,e,name in rows[:limit]:
        items.append({**{k:getattr(d,k) for k in ('id','destination_id','state','attempts','last_error','provider_ref','media','created_at','sent_at','subscription_ids','deadline_at')},
            'type':e.type,'camera':e.envelope['camera'],'destination_name':name,'message_id':str(e.message_id)})
    return {'items':items,'next':items[-1]['id'] if len(rows)>limit else None}

@router.post('/event-deliveries/{id}/retry')
async def retry_delivery(id:int,request:Request,db=Depends(get_db),principal=Depends(ADMIN)):
    row=(await db.execute(select(EventDelivery).where(EventDelivery.id==id).with_for_update())).scalar_one_or_none()
    if row is None: raise HTTPException(404,'Not found')
    if row.state not in ('failed','expired'): raise HTTPException(409,'Only failed or expired deliveries can be retried')
    dest=await get_row(db,EventDestination,row.destination_id)
    if not dest.enabled: raise HTTPException(409,'Enable the destination before retrying')
    row.state='pending';row.attempts=0;row.next_attempt_at=now()
    row.deadline_at=now()+timedelta(seconds=max_age(row.destination));row.last_error=None
    row.provider_ref=None;row.media=None  # the earlier attempt's receipt no longer describes this one
    row.lease_owner=None;row.lease_until=None
    await save(db,request,principal,'event_delivery.retried',id)
    return {'ok':True}
