from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import httpx
import pytest
from pydantic import ValidationError

from backend.crypto import encrypt
from backend.models import QaSinkConfigBody, QaSinkConfigResponse, QaPublishOutbox
from backend.services import qa_sink
from backend.services.annotation_delivery import DeliveryError, post_webhook, serialize, retry_delay
from test_qa_sink_payload import _config, _payload, ANNOTATION_ID


def config(**kwargs):
    return _config(delivery_method='webhook', message_format='annotation_v1',
                   webhook_url='https://receiver.example/annotations', **kwargs)


def test_webhook_configuration_does_not_require_kafka():
    body = QaSinkConfigBody(enabled=True, delivery_method='webhook',
        webhook_url='https://receiver.example/annotations', s3_bucket='frames-bucket')
    assert body.bootstrap_servers == ''
    with pytest.raises(ValidationError):
        QaSinkConfigBody(enabled=True, delivery_method='webhook', s3_bucket='frames-bucket')


@pytest.mark.parametrize('url', ['http://receiver.example', 'https://user:pass@receiver.example',
    'https://receiver.example/?secret=x', 'https://receiver.example/#x', 'https://[bad', 'ftp://example.com'])
def test_invalid_endpoint(url):
    with pytest.raises(ValidationError):
        QaSinkConfigBody(webhook_url=url)


def test_general_schema_is_explicit_and_legacy_unchanged():
    payload = _payload()
    assert serialize(payload, 'review_legacy') is payload
    result = serialize(payload, 'annotation_v1')
    assert result['event_id'] == str(ANNOTATION_ID)
    assert result['frame'] == {'width': 1920, 'height': 1080}
    assert result['annotations'][0]['bbox']['values'] == [10, 10, 25, 25]
    assert result['annotations'][0]['bbox']['units'] == 'percent'
    assert result['media']['video_url'] is None
    assert 'absolute_bbox' not in result


def test_snapshot_preserves_destination_and_keeps_secrets_encrypted():
    cfg = config(webhook_token_enc=encrypt('private-token'))
    saved = qa_sink.snapshot(cfg)
    cfg.webhook_url = 'https://other.example'
    cfg.s3_bucket = 'other-bucket'
    recovered = qa_sink.destination_for(QaPublishOutbox(destination=saved))
    assert recovered.webhook_url == 'https://receiver.example/annotations'
    assert recovered.s3_bucket == 'vms-qa-frames'
    assert 'private-token' not in json.dumps(saved)
    public = QaSinkConfigResponse.from_row(cfg).model_dump_json()
    assert 'private-token' not in public and cfg.webhook_token_enc not in public


@pytest.mark.asyncio
async def test_webhook_posts_json_with_deduplication_and_auth():
    payload = serialize(_payload(), 'annotation_v1')
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer private-token'
        assert request.headers['Idempotency-Key'] == str(ANNOTATION_ID)
        assert json.loads(request.content) == payload
        return httpx.Response(202)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await post_webhook(config(webhook_token_enc=encrypt('private-token')), payload, client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,permanent', [(301,True),(400,True),(401,True),(403,True),
    (408,False),(429,False),(500,False),(503,False)])
async def test_failure_policy_and_redaction(status, permanent):
    def handler(request):
        return httpx.Response(status, headers={'Retry-After': '120'}, text='private-token')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DeliveryError) as exc:
            await post_webhook(config(), _payload(), client=client)
    assert exc.value.permanent == permanent
    assert exc.value.retry_after == 120
    assert 'private-token' not in str(exc.value)


@pytest.mark.asyncio
async def test_timeout_is_retryable_and_redacted():
    def handler(request):
        raise httpx.ReadTimeout('private-token')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DeliveryError) as exc:
            await post_webhook(config(), _payload(), client=client)
    assert not exc.value.permanent
    assert 'private-token' not in str(exc.value)


def test_retry_after_bounds():
    assert retry_delay('99999') == 600
    assert retry_delay('-1') == 0
    assert retry_delay('nonsense') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('error,state', [(None,'sent'),(DeliveryError('bad auth', permanent=True),'failed'),
    (DeliveryError('busy',retry_after=120),'pending')])
async def test_relay_uses_snapshot_and_never_opens_kafka_for_webhook(monkeypatch, error, state):
    cfg = config()
    entry = QaPublishOutbox(annotation_id=ANNOTATION_ID, destination=qa_sink.snapshot(cfg),
        payload=_payload(), state='pending', attempts=0)
    cfg.webhook_url = 'https://changed.example'
    monkeypatch.setattr(qa_sink, 'get_config', AsyncMock(return_value=cfg))
    producer = AsyncMock()
    monkeypatch.setattr(qa_sink, '_open_producer', producer)
    deliver = AsyncMock(side_effect=error)
    monkeypatch.setattr(qa_sink, '_deliver', deliver)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda:[entry]))), get=AsyncMock(return_value=None), commit=AsyncMock())
    await qa_sink.drain_once(db)
    producer.assert_not_awaited()
    assert deliver.await_args.args[1].webhook_url == 'https://receiver.example/annotations'
    assert entry.state == state
    if error and not error.permanent:
        assert (entry.next_attempt_at - datetime.now(timezone.utc)).total_seconds() > 110


@pytest.mark.asyncio
async def test_media_is_prepared_before_delivery(monkeypatch):
    order = []
    async def prepare(*args): order.append('media')
    async def send(*args): order.append('post')
    monkeypatch.setattr(qa_sink, '_prepare_media', prepare)
    monkeypatch.setattr(qa_sink, 'post_webhook', send)
    await qa_sink._deliver(None, config(), QaPublishOutbox(annotation_id=ANNOTATION_ID, payload=_payload()))
    assert order == ['media', 'post']


@pytest.mark.asyncio
async def test_erasure_uses_original_bucket(monkeypatch):
    entry = QaPublishOutbox(destination=qa_sink.snapshot(config()), image_key='original.jpg',
        labelled_image_key='labelled.jpg')
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda:[entry]))))
    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(qa_sink.s3_upload, 'delete_object', delete)
    assert await qa_sink.forget_images(db, [ANNOTATION_ID]) == 0
    assert all(call.args[0] == 'vms-qa-frames' for call in delete.await_args_list)


@pytest.mark.asyncio
async def test_http_settings_encrypt_preserve_remove_token(client, mint, db, monkeypatch):
    from backend.models import QaSinkConfig
    from backend.crypto import decrypt
    headers = mint(roles=['admin'])
    body = {'enabled': True, 'delivery_method': 'webhook', 'message_format': 'annotation_v1',
            'webhook_url': 'https://receiver.example/annotations', 'webhook_token': 'private-token',
            's3_bucket': 'frames-bucket'}
    response = await client.put('/api/qa-sink', headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['webhook_token_set'] is True
    assert 'private-token' not in response.text
    saved = next(row for row in db.added if isinstance(row, QaSinkConfig))
    assert saved.webhook_token_enc != 'private-token'
    assert decrypt(saved.webhook_token_enc) == 'private-token'
    monkeypatch.setattr(db, 'get', AsyncMock(return_value=saved))
    del body['webhook_token']
    response = await client.put('/api/qa-sink', headers=headers, json=body)
    assert response.status_code == 200
    assert decrypt(saved.webhook_token_enc) == 'private-token'
    body['webhook_token'] = ''
    response = await client.put('/api/qa-sink', headers=headers, json=body)
    assert response.status_code == 200
    assert saved.webhook_token_enc is None
