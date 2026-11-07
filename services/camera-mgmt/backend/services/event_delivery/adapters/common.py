import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from ...annotation_delivery import DeliveryError
from ....config import settings


def body_bytes(envelope):
    return json.dumps(envelope,separators=(',',':'),ensure_ascii=False).encode()

def retry_after(value):
    try: return max(0,float(value))
    except (ValueError,TypeError):
        try: return max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
        except (ValueError,TypeError,OverflowError): return 0

def check_response(response):
    if 200<=response.status_code<300:
        return
    code=response.status_code
    raise DeliveryError(f'Receiver returned HTTP {code}',permanent=not(code in (408,429) or code>=500),
        retry_after=retry_after(response.headers.get('Retry-After')))

def format_text(env, tz=None):
    d=env['data']; when=datetime.fromisoformat(env['occurred_at']).astimezone(ZoneInfo(tz or settings.site_timezone))
    title=d.get('rule_name') or d.get('activity_label') or env['type'].replace('.',' ').capitalize()
    severity=(d.get('severity') or '').upper()
    text=f"{severity+' · ' if severity else ''}{title} — {env['camera']['name']}\n{when:%H:%M:%S %Z, %d %b %Y}"
    if d.get('zone'): text+=f"\nZone: {d['zone']}"
    if env.get('_frame_caption'): text+='\n'+env['_frame_caption']
    if env['links'].get('playback'): text+='\nOpen playback: '+env['links']['playback']
    return text[:1024]
