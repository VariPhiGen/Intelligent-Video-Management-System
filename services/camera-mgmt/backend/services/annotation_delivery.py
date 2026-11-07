"""Delivery adapters; no HTTP response bodies, URLs or credentials in errors."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..crypto import decrypt


class DeliveryError(Exception):
    def __init__(self, message, *, permanent=False, retry_after=0):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


def retry_delay(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, min(600, seconds))


def serialize(payload, message_format):
    if message_format == 'review_legacy':
        return payload
    if message_format != 'annotation_v1':
        raise DeliveryError('Unsupported annotation message format', permanent=True)
    width, height = map(int, payload['imgsz'].split(':'))
    return {
        'schema_version': '1', 'event_type': 'annotation.created',
        'event_id': payload['id'], 'annotation_id': payload['id'],
        'camera_id': payload['sensor_id'], 'captured_at': payload['created_at'],
        'severity': payload['severity'],
        'frame': {'width': width, 'height': height},
        'annotations': [{'class_key': box['class_name'], 'label': box['subcategory'],
                         'bbox': {'format': 'xywh', 'units': 'percent', 'origin': 'top-left',
                                  'values': box['xywh']}, 'source': 'operator'}
                        for box in payload['absolute_bbox']],
        'media': {'original_image_url': payload['org_img'],
                  'annotated_image_url': payload['org_img_local'],
                  'video_url': None if payload['video_url'].startswith('vms:') else payload['video_url']},
    }


async def post_webhook(config, payload, *, client=None):
    headers = {'Idempotency-Key': str(payload.get('event_id') or payload['id'])}
    if config.webhook_token_enc:
        token = decrypt(config.webhook_token_enc)
        if not token:
            raise DeliveryError('Webhook credential cannot be decrypted', permanent=True)
        headers['Authorization'] = 'Bearer ' + token
    async def send(http):
        try:
            async with asyncio.timeout(20):
                async with http.stream('POST', config.webhook_url, json=payload,
                                       headers=headers, follow_redirects=False, timeout=15) as response:
                    status = response.status_code
                    if 200 <= status < 300:
                        return
                    raise DeliveryError(f'Webhook returned HTTP {status}',
                        permanent=not (status in (408, 429) or status >= 500),
                        retry_after=retry_delay(response.headers.get('Retry-After')))
        except (httpx.HTTPError, TimeoutError):
            raise DeliveryError('Webhook connection failed or timed out') from None
    if client is not None:
        await send(client)
    else:
        async with httpx.AsyncClient(trust_env=False) as http:
            await send(http)
