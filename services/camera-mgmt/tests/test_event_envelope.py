from datetime import datetime, timezone
from uuid import uuid4
import pytest
from backend.services.event_delivery.envelope import *
from backend.services.event_delivery.routing import matches, cooldown_key, effective_cooldown
from backend.services.event_delivery.schemas import DestinationIn
from backend.services.event_delivery.netguard import check_target

T=datetime(2026,10,27,12,tzinfo=timezone.utc)
CAM=CameraRef(uuid4(),'gate','Gate')
ROW=dict(id=uuid4(),activity='intrusion',started_at=T,ended_at=None,confidence=None,attributes={'track_ids':[1],'plate':'TEST','zone_name':'Yard'})
def env(): return ai_event_envelope(ROW,'created',CAM,accepted_at=T,activity_label='Intrusion')

def test_wire_contract_and_allowlist(monkeypatch):
    monkeypatch.setattr(settings,'events_include_plates',False)
    e=env()
    assert e['message_id']==env()['message_id']
    assert e['occurred_at'].endswith('Z') and e['data']['confidence'] is None
    assert e['data']['attributes']=={'zone_name':'Yard'}
    monkeypatch.setattr(settings,'events_include_plates',True)
    assert env()['data']['attributes']['plate']=='TEST'
    assert message_id_for('a','1')!=message_id_for('b','1')

@pytest.mark.parametrize('new,previous,result',[('connected',None,None),('connected','connected',None),('error','disconnected',None),('disconnected','connected','camera.offline'),('connected','error','camera.online')])
def test_health_buckets(new,previous,result):
    e=camera_status_envelope(CAM,status_event_id=1,new=new,previous=previous,changed_at=T)
    assert (e['type'] if e else None)==result

def test_motion_and_scoped_filters():
    e=env();e['data']['activity']='motion'
    s={'types':['ai.event.created'],'filters':{}}
    assert not matches(s,e)
    s['filters']={'activities':['motion']};assert matches(s,e)
    s['filters']['cameras']=[str(uuid4())];assert not matches(s,e)
    assert effective_cooldown({},'telegram')==60
    assert effective_cooldown({'cooldown_s':0},'telegram')==0

def test_camera_flaps_share_cooldown():
    e=env();e['type']='camera.offline';e['data']={}
    a=cooldown_key('s',e);e['type']='camera.online'
    assert a==cooldown_key('s',e)

@pytest.mark.parametrize('url',['http://127.0.0.1/x','https://localhost/','https://[::1]/','https://169.254.169.254/','https://[::ffff:127.0.0.1]/','https://[fd00:ec2::254]/','https://user:pw@10.0.0.5/','https://[fe80::1]/'])
def test_blocked_targets(url):
    with pytest.raises(ValueError): check_target(url,allow_insecure=True)

def test_lan_and_explicit_http():
    check_target('https://10.0.0.5/hook')
    with pytest.raises(ValueError): check_target('http://10.0.0.5/hook')
    check_target('http://10.0.0.5/hook',allow_insecure=True)

def test_config_rejects_unknown_secret_fields_and_false_slack_host():
    with pytest.raises(ValueError): DestinationIn(name='x',kind='telegram',config={'chat_id':'1','token':'bad'})
    with pytest.raises(ValueError): DestinationIn(name='x',kind='slack',config={},secret='https://hooks.slack.com.evil/services/x')


def test_alert_without_rule_has_null_rule_id():
    from types import SimpleNamespace
    alert = SimpleNamespace(id=uuid4(), rule_id=None, rule_name='Manual', severity='info',
                            event_ids=[], triggered_at=datetime.now(timezone.utc))
    env = alert_envelope(alert, CameraRef(uuid4(), 'gate', 'Gate'),
                                  accepted_at=datetime.now(timezone.utc))
    assert env['data']['rule_id'] is None


def test_default_rate_follows_destination_kind():
    from backend.services.event_delivery.schemas import DestinationIn
    def rate(kind, config):
        return DestinationIn(name='x', kind=kind, config=config).rate_per_min
    assert rate('kafka', {'bootstrap': '10.0.0.9:9092', 'topic': 'vms'}) == 6000
    assert rate('webhook', {'url': 'https://10.0.0.5/hook'}) == 6000
    assert rate('telegram', {'chat_id': '-100123'}) == 20
    assert rate('slack', {}) == 60


def test_playback_address_validation():
    from backend.services.event_delivery.schemas import ControlIn
    assert ControlIn(enabled=True,feed_enabled=False,public_base_url=' https://vms.site.lan/ ').public_base_url=='https://vms.site.lan'
    assert ControlIn(enabled=True,feed_enabled=False,public_base_url='').public_base_url==''
    assert ControlIn(enabled=True,feed_enabled=False).public_base_url is None
    for bad in ('vms.site.lan','javascript:alert(1)','https://u:p@vms.lan','https://vms.lan/?x=1','https://vms.lan/#/x','ftp://vms.lan'):
        with pytest.raises(ValueError):
            ControlIn(enabled=True,feed_enabled=False,public_base_url=bad)


def test_no_playback_link_without_an_address():
    from backend.services.event_delivery.envelope import with_playback
    env=with_playback(build('camera.offline','x','camera_status',CameraRef(uuid4(),'gate','Gate'),datetime.now(timezone.utc),datetime.now(timezone.utc),{}),'')
    assert env['links']['playback'] is None
