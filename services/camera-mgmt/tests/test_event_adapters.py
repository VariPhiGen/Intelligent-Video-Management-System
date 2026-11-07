import hashlib
import hmac
import json
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
from uuid import uuid4
import httpx
import pytest
from backend.crypto import encrypt
from backend.services.event_delivery import netguard,media
from backend.services.event_delivery.adapters import webhook,telegram,slack,kafka
from backend.services.event_delivery.adapters.common import DeliveryError,format_text
from backend.services.event_delivery.envelope import build,CameraRef
from backend.services.event_delivery.schemas import DestinationIn


def test_slack_bot_configuration():
    d = DestinationIn(name='Slack', kind='slack', config={'mode':'bot','channel_id':'C0123456789'},
                      secret='xoxb-example-token', media=True)
    assert d.config['mode']=='bot'
    assert d.secret.get_secret_value()=='xoxb-example-token'
    for config, secret, media_enabled in [({}, 'xoxb-example-token', False),
                                         ({'mode':'bot'}, 'xoxb-example-token', True),
                                         ({}, 'https://hooks.slack.com/services/test', True),
                                         ({'mode':'bot','channel_id':'C0123456789'}, 'https://hooks.slack.com/services/test', True)]:
        with pytest.raises(ValueError):
            DestinationIn(name='Slack',kind='slack',config=config,secret=secret,media=media_enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize('frame',[None,b'jpeg'])
async def test_slack_bot_upload_or_text(monkeypatch,frame):
    requests=[]
    def handle(request):
        requests.append(request)
        path=request.url.path
        if path.endswith('getUploadURLExternal'):
            return httpx.Response(200,json={'ok':True,'file_id':'F123','upload_url':'https://files.slack.com/upload/v1/example'})
        if path.startswith('/upload/'):
            assert 'authorization' not in request.headers
            assert request.content==b'jpeg'
            return httpx.Response(200,text='ok')
        if path.endswith('completeUploadExternal'):
            body=json.loads(request.content)
            assert body['channel_id']=='C0123456789' and body['files'][0]['id']=='F123'
            assert body['blocks'][0]['text']['type']=='plain_text'
            return httpx.Response(200,json={'ok':True,'files':[{'id':'F123'}]})
        assert path.endswith('chat.postMessage')
        assert json.loads(request.content)['channel']=='C0123456789'
        return httpx.Response(200,json={'ok':True,'ts':'123.4'})
    monkeypatch.setattr(netguard,'client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    ref=await slack.send(dest('slack',{'mode':'bot','channel_id':'C0123456789'},'xoxb-test'),envelope(),frame)
    assert ref==('F123' if frame else '123.4')
    assert len(requests)==(3 if frame else 1)


@pytest.mark.asyncio
async def test_slack_bot_rejects_untrusted_upload_target(monkeypatch):
    def handle(request):
        assert request.url.host=='slack.com'
        return httpx.Response(200,json={'ok':True,'file_id':'F123','upload_url':'https://attacker.example/upload'})
    monkeypatch.setattr(netguard,'client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(DeliveryError,match='Invalid Slack upload target'):
        await slack.send(dest('slack',{'mode':'bot','channel_id':'C0123456789'},'xoxb-test'),envelope(),b'jpeg')


def envelope():
    return build('alert.raised','test','alert',CameraRef(uuid4(),'gate','Gate'),datetime.now(timezone.utc),datetime.now(timezone.utc),dict(rule_name='Intrusion',severity='critical'))

def dest(kind,config,secret='test-secret'):
    return SimpleNamespace(id=uuid4(),kind=kind,config=config,secret_enc=encrypt(secret),allow_insecure=True)

@pytest.fixture
def capture(monkeypatch):
    requests=[]; reply={'code':200,'json':{'ok':True,'result':{'message_id':42}},'headers':{}}
    def handle(request):
        requests.append(request)
        return httpx.Response(reply['code'],json=reply['json'],headers=reply['headers'])
    monkeypatch.setattr(netguard,'client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return requests,reply

@pytest.mark.asyncio
async def test_webhook_hmac_exact_body(capture):
    requests,_=capture;e=envelope()
    await webhook.send(dest('webhook',{'url':'https://10.0.0.1/hook','auth':'hmac'}),e)
    r=requests[0];ts,sig=r.headers['X-VMS-Signature'].split(',v1=')
    assert sig==hmac.new(b'test-secret',ts[2:].encode()+b'.'+r.content,hashlib.sha256).hexdigest()
    assert r.headers['Idempotency-Key']==e['message_id']

@pytest.mark.asyncio
@pytest.mark.parametrize('status,permanent',[(400,True),(302,True),(429,False),(503,False)])
async def test_webhook_errors_sanitized(capture,status,permanent):
    req,reply=capture;reply.update(code=status,headers={'Retry-After':'900'})
    with pytest.raises(DeliveryError) as err: await webhook.send(dest('webhook',{'url':'https://10.0.0.1/hook','auth':'bearer'}),envelope())
    assert err.value.permanent==permanent and err.value.retry_after==900
    assert 'test-secret' not in str(err.value) and '10.0.0.1' not in str(err.value)

@pytest.mark.asyncio
@pytest.mark.parametrize('frame',[None,b'jpeg'])
async def test_telegram_message_or_photo(capture,frame):
    req,_=capture
    ref=await telegram.send(dest('telegram',{'chat_id':'-100','topic_id':3},'123:ABC'),envelope(),frame)
    assert ref=='42' and req[0].url.path.endswith('sendPhoto' if frame else 'sendMessage')
    if frame: assert b'name="photo"' in req[0].content
    else: assert json.loads(req[0].content)['message_thread_id']==3

@pytest.mark.asyncio
async def test_telegram_ok_false_and_retry_after(capture):
    _,reply=capture;reply['json']={'ok':False,'error_code':429,'parameters':{'retry_after':17},'description':'123:ABC'}
    with pytest.raises(DeliveryError) as err: await telegram.send(dest('telegram',{'chat_id':'1'},'123:ABC'),envelope())
    assert err.value.retry_after==17 and '123:ABC' not in str(err.value)

@pytest.mark.asyncio
async def test_old_or_health_events_never_fetch_frame(monkeypatch):
    async def fail(*a,**kw): raise AssertionError('snapshot must not be called')
    monkeypatch.setattr(media.snapshot,'shared_snapshot',fail)
    e=envelope();e['occurred_at']=(datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat()
    assert await media.frame_for(e)==(None,'skipped')
    e=envelope();e['type']='camera.offline';assert await media.frame_for(e)==(None,'skipped')

@pytest.mark.asyncio
async def test_snapshot_failure_falls_back(monkeypatch):
    async def fail(*a,**kw): raise RuntimeError('offline')
    monkeypatch.setattr(media.snapshot,'shared_snapshot',fail)
    assert await media.frame_for(envelope())==(None,'unavailable')

@pytest.mark.asyncio
async def test_slack_no_unfurl_and_sanitized_error(capture):
    req,reply=capture;reply['code']=404
    with pytest.raises(DeliveryError) as err: await slack.send(dest('slack',{},'https://hooks.slack.com/services/secret'),envelope())
    body=json.loads(req[0].content)
    assert body['blocks'] and not body['unfurl_links'] and not body['unfurl_media']
    assert 'hooks.slack.com' not in str(err.value)

@pytest.mark.asyncio
async def test_kafka_reuses_producer_and_stable_headers(monkeypatch):
    producers=[]
    class Producer:
        def __init__(self,**kw): self.calls=[];self.stopped=False;producers.append(self)
        async def start(self): pass
        async def stop(self): self.stopped=True
        async def send_and_wait(self,*a,**kw): self.calls.append((a,kw));return SimpleNamespace(partition=0,offset=1)
    monkeypatch.setattr(kafka,'producer_factory',Producer)
    d=dest('kafka',{'bootstrap':'10.0.0.5:9092','topic':'events','security':'plaintext'})
    e=envelope();await kafka.send(d,e);await kafka.send(d,e)
    assert len(producers)==1
    assert producers[0].calls[0][1]['key']==e['camera']['id'].encode()
    assert ('message_id',e['message_id'].encode()) in producers[0].calls[0][1]['headers']
    d.config={**d.config,'topic':'new'};await kafka.send(d,e)
    assert len(producers)==2 and producers[0].stopped
    await kafka.close_all()

def test_text_site_timezone():
    e=envelope();e['occurred_at']='2026-10-27T12:00:00Z'
    assert '17:30:00 IST' in format_text(e,'Asia/Kolkata')

@pytest.mark.asyncio
async def test_connect_guard_pins_checked_address(monkeypatch):
    import asyncio,socket
    called=[]
    async def resolve(*a,**kw): return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.2.3.4',443))]
    async def dial(self,host,port,*a): called.append((host,port));return 'stream'
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    monkeypatch.setattr(netguard.AutoBackend,'connect_tcp',dial)
    assert await netguard.GuardedBackend().connect_tcp('receiver.example',443)=='stream'
    assert called==[('10.2.3.4',443)]

@pytest.mark.asyncio
async def test_connect_guard_rejects_rebinding_to_metadata(monkeypatch):
    import asyncio,socket
    async def resolve(*a,**kw): return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    with pytest.raises(ValueError): await netguard.GuardedBackend().connect_tcp('receiver.example',443)

@pytest.mark.asyncio
async def test_webhook_real_lan_socket():
    """Exercise httpx/httpcore's actual guarded transport, without the Internet."""
    import asyncio,socket
    host=socket.gethostbyname(socket.gethostname())
    if host.startswith('127.'): pytest.skip('test container requires a LAN address')
    received=asyncio.Future()
    async def handle(reader,writer):
        headers=await reader.readuntil(b'\r\n\r\n')
        length=int(next(x.split(b':',1)[1] for x in headers.split(b'\r\n') if x.lower().startswith(b'content-length:')))
        body=await reader.readexactly(length)
        received.set_result(json.loads(body))
        writer.write(b'HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n');await writer.drain();writer.close()
    server=await asyncio.start_server(handle,'0.0.0.0',0)
    try:
        port=server.sockets[0].getsockname()[1];e=envelope()
        await webhook.send(dest('webhook',{'url':f'http://{host}:{port}/events','auth':'none'}),e)
        assert (await asyncio.wait_for(received,2))['message_id']==e['message_id']
    finally: server.close();await server.wait_closed()
