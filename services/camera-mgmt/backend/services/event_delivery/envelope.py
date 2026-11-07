from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import quote
from uuid import UUID, uuid5
from ...config import settings

EVENTS_NS = UUID('86528668-55f9-44c1-9a3e-9c28d8896ab9')
ATTRIBUTE_ALLOWLIST = frozenset({'zone_name','person_count','people_in_zone','bbox',
    'gathering_since','sustained_s','parked_since','parked_s','direction',
    'tripwire_id','tripwire_name','completed_at','crossing_s'})

class CameraRef(NamedTuple):
    id: UUID
    slug: str
    name: str

def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00','Z') if value else None

def message_id_for(type, subject_id):
    return uuid5(EVENTS_NS, f'{type}:{subject_id}')

def public_base_url(cfg):
    """The saved UI value, else EVENTS_PUBLIC_BASE_URL, else '' (no links)."""
    return ((cfg and cfg.public_base_url) or settings.events_public_base_url or '').rstrip('/')

def playback_link(base, slug, at):
    return f'{base}/#/playback?cam={quote(slug, safe="")}&t={int(at.timestamp())}' if base and slug else None

def with_playback(envelope, base):
    """Fill links.playback from `base`. The link is set at acceptance, so it is
    stored with the event and every destination gets the same one."""
    at = envelope.get('occurred_at')
    link = playback_link(base, envelope['camera']['slug'], datetime.fromisoformat(at.replace('Z', '+00:00'))) if at else None
    envelope['links'] = dict(envelope.get('links') or {}, playback=link)
    return envelope

def build(type, subject_id, kind, camera, at, accepted_at, data):
    return dict(schema_version='1', message_id=str(message_id_for(type,subject_id)), type=type,
        occurred_at=iso(at), accepted_at=iso(accepted_at), deployment_id=settings.events_deployment_id,
        subject=dict(kind=kind,id=str(subject_id)),
        camera=dict(id=str(camera.id),slug=camera.slug,name=camera.name or camera.slug),data=data,
        links=dict(playback=None))

def ai_event_envelope(row, transition, camera, *, accepted_at, activity_label):
    keys = ATTRIBUTE_ALLOWLIST | ({'plate'} if settings.events_include_plates else set())
    data = {k:row.get(k) for k in ('activity','zone','confidence','object_class')}
    data.update(event_id=str(row['id']),activity_label=activity_label,
        started_at=iso(row['started_at']),ended_at=iso(row.get('ended_at')),
        attributes={k:v for k,v in (row.get('attributes') or {}).items() if k in keys})
    return build(f'ai.event.{transition}',str(row['id']), 'event',camera,
        (row.get('ended_at') or row['started_at']) if transition=='closed' else row['started_at'],accepted_at,data)

def alert_envelope(alert, camera, *, accepted_at):
    return build('alert.raised',str(alert.id),'alert',camera,alert.triggered_at,accepted_at,
        dict(alert_id=str(alert.id),rule_id=str(alert.rule_id) if alert.rule_id else None,rule_name=alert.rule_name,
             severity=alert.severity,event_ids=[str(i) for i in alert.event_ids],triggered_at=iso(alert.triggered_at)))

def bucket(status):
    return {'connected':'online','disconnected':'offline','error':'offline'}.get(status)

def camera_status_envelope(camera, *, status_event_id, new, previous, changed_at):
    if not bucket(previous) or not bucket(new) or bucket(previous)==bucket(new):
        return None
    return build('camera.'+bucket(new),str(status_event_id),'camera_status',camera,changed_at,changed_at,
        dict(status=new,previous_status=previous,changed_at=iso(changed_at)))
