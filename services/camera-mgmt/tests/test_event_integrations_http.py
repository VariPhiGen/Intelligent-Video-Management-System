from uuid import uuid4
import pytest
from backend.models import EventDestination
from backend.crypto import encrypt
from backend.services.event_delivery.adapters import ADAPTERS

pytestmark=pytest.mark.asyncio

def auth(mint,role='admin'): return mint(roles=[role])

async def test_operator_cannot_configure_or_read_feed(client,mint):
    for path in ('/event-destinations','/event-subscriptions','/event-deliveries','/events/feed','/event-stream'):
        assert (await client.get('/api'+path,headers=auth(mint,'operator'))).status_code==403

async def test_secret_not_returned_and_creation_disabled(client,mint):
    r=await client.post('/api/event-destinations',headers=auth(mint),json=dict(name='Test',kind='telegram',config={'chat_id':'1'},secret='123:ABC',enabled=True))
    assert r.status_code==201,r.text
    body=r.json();assert body['has_secret'] and not body['enabled']
    assert 'secret' not in body and 'secret_enc' not in body and '123:ABC' not in r.text

async def test_invalid_target_does_not_echo_secret(client,mint):
    r=await client.post('/api/event-destinations',headers=auth(mint),json=dict(name='Bad',kind='webhook',config={'url':'https://169.254.169.254/'},secret='private-secret'))
    assert r.status_code==422 and 'private-secret' not in r.text

async def test_synthetic_test_only(client,mint,db,monkeypatch):
    row=EventDestination(id=uuid4(),name='Test',kind='telegram',config={'chat_id':'1'},secret_enc=encrypt('123:ABC'),enabled=False,media=True,allow_insecure=False,rate_per_min=60)
    db.add(row);seen=[]
    async def send(dest,env,frame): seen.append((env,frame));return '42'
    monkeypatch.setitem(ADAPTERS,'telegram',send)
    r=await client.post(f'/api/event-destinations/{row.id}/test',headers=auth(mint),json={})
    assert r.status_code==200 and r.json()['ok'],r.text
    assert seen[0][0]['subject']['id']=='test' and seen[0][1] is None

async def test_integration_role_reaches_disabled_feed(client,mint):
    r=await client.get('/api/events/feed',headers=auth(mint,'integration'))
    assert r.status_code==409,r.text


# ── email / SFTP (068) ──────────────────────────────────────────────────────

from backend.models import EventIntegrationSettings  # noqa: E402

def site(db,**kw):
    row=EventIntegrationSettings(id=1,enabled=True,feed_enabled=False,purged_through=0,**kw);db.add(row);return row

async def test_mail_server_password_is_write_only(client,mint,db):
    row=site(db)
    r=await client.put('/api/event-integrations/smtp',headers=auth(mint),json=dict(host='mail.site.lan',port=587,
        username='vms',password='smtp-secret',from_address='vms@site.lan'))
    assert r.status_code==200,r.text
    assert r.json()['has_password'] and 'smtp-secret' not in r.text and 'password' not in row.smtp
    r=await client.get('/api/event-integrations/smtp',headers=auth(mint))
    assert r.json()['configured'] and 'smtp-secret' not in r.text

async def test_mail_server_errors_say_why(client,mint,db):
    site(db)
    r=await client.put('/api/event-integrations/smtp',headers=auth(mint),json=dict(host='mail.site.lan',security='none',
        username='vms',password='pw',from_address='vms@site.lan'))
    assert r.status_code==422 and 'STARTTLS or TLS' in r.json()['detail']
    r=await client.put('/api/event-integrations/smtp',headers=auth(mint),json=dict(host='mail.site.lan',from_address='nope'))
    assert r.status_code==422 and r.json()['detail']=='Enter a valid sender email address'

async def test_email_cannot_be_enabled_without_a_mail_server(client,mint,db):
    site(db)
    row=EventDestination(id=uuid4(),name='Ops mail',kind='email',config={'to':['ops@site.lan'],'cc':[],'bcc':[]},
        secret_enc=None,enabled=False,media=False,allow_insecure=False,rate_per_min=30)
    db.add(row)
    r=await client.patch(f'/api/event-destinations/{row.id}',headers=auth(mint),json={'enabled':True})
    assert r.status_code==422 and 'mail server' in r.json()['detail']

async def test_sftp_destination_reasons_never_echo_the_secret(client,mint):
    r=await client.post('/api/event-destinations',headers=auth(mint),json=dict(name='NAS',kind='sftp',
        config={'host':'nas.lan','username':'vms','host_key_fingerprint':'abc'},secret='sftp-secret'))
    assert r.status_code==422 and 'SHA256' in r.json()['detail'] and 'sftp-secret' not in r.text

async def test_host_key_probe_returns_fingerprint_only(client,mint,monkeypatch):
    from backend.services.event_delivery.adapters import sftp
    async def probe(host,port): return 'SHA256:'+'B'*43
    monkeypatch.setattr(sftp,'probe_host_key',probe)
    r=await client.post('/api/event-integrations/sftp-host-key',headers=auth(mint),json={'host':'nas.lan'})
    assert r.status_code==200 and r.json()=={'fingerprint':'SHA256:'+'B'*43}
    assert (await client.post('/api/event-integrations/sftp-host-key',headers=auth(mint,'operator'),json={'host':'nas.lan'})).status_code==403
