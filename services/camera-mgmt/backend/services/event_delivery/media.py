import asyncio
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from ...config import settings
from ... import redis_client
from .. import snapshot

async def frame_for(envelope):
    at=datetime.fromisoformat(envelope['occurred_at'])
    age=(datetime.now(timezone.utc)-at).total_seconds()
    if envelope['type'].startswith('camera.') or not 0<=age<30 or not envelope['camera']['slug']:
        return None,'skipped'
    try:
        async with asyncio.timeout(8):
            slug=envelope['camera']['slug']
            frame=await snapshot.shared_snapshot(slug,wait_s=5)
            if not frame: return None,'unavailable'
            r=await redis_client.get_redis()
            meta=await r.get(f'stream:snapshot_meta:{slug}')
            stamp,digest=meta.split(':',1)
            if hashlib.sha256(frame).hexdigest()!=digest: return None,'unavailable'
            when=datetime.fromtimestamp(float(stamp),timezone.utc)
            offset=int((when-at).total_seconds())
            envelope['_frame_at']=when.isoformat()
            local=when.astimezone(ZoneInfo(settings.site_timezone))
            envelope['_frame_caption']=f"Frame captured around {local:%H:%M:%S %Z} ({abs(offset)} s {'after' if offset>=0 else 'before'} event)"
            return frame,'attached'
    except Exception:
        return None,'unavailable'
