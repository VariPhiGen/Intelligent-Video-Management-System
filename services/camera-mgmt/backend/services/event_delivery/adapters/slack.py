import re
from urllib.parse import urlsplit

from ....crypto import decrypt
from .. import netguard
from .common import format_text, check_response, DeliveryError

def api_result(response):
    check_response(response)
    try:
        result = response.json()
    except ValueError:
        raise DeliveryError('Invalid Slack response') from None
    if not result.get('ok'):
        # Only a provider error code is retained, never a token or response body.
        code = str(result.get('error', 'unknown_error'))
        if not re.fullmatch(r'[a-z_]+', code):
            code = 'unknown_error'
        raise DeliveryError(f'Slack rejected delivery ({code})',
                            permanent=code not in ('ratelimited', 'internal_error', 'fatal_error', 'service_unavailable'))
    return result

async def send_bot(client, token, config, text, blocks, frame):
    headers = {'Authorization': f'Bearer {token}'}
    channel = config['channel_id']
    if not frame:
        result = api_result(await client.post('https://slack.com/api/chat.postMessage',
            headers=headers, json={'channel': channel, 'text': text, 'blocks': blocks,
                                   'unfurl_links': False, 'unfurl_media': False}))
        return result.get('ts')
    upload = api_result(await client.post('https://slack.com/api/files.getUploadURLExternal',
        headers=headers, data={'filename': 'snapshot.jpg', 'length': str(len(frame))}))
    url = upload.get('upload_url', '')
    parts = urlsplit(url)
    # Send bytes only to Slack; never forward the bot token to the upload URL.
    if (parts.scheme != 'https' or parts.hostname != 'files.slack.com'
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise DeliveryError('Invalid Slack upload target', permanent=True)
    check_response(await client.post(url, content=frame, headers={'Content-Type': 'image/jpeg'}))
    result = api_result(await client.post('https://slack.com/api/files.completeUploadExternal',
        headers=headers, json={'files': [{'id': upload['file_id'], 'title': 'Event snapshot'}],
                               'channel_id': channel, 'blocks': blocks}))
    files = result.get('files') or []
    return str(files[0]['id']) if files else str(upload['file_id'])

async def send(dest,envelope,frame=None):
    try:
        url=decrypt(dest.secret_enc)
        if not url: raise DeliveryError('Slack credential unavailable',permanent=True)
        text=format_text(envelope)
        # Plain text prevents event metadata from injecting mentions or markup.
        blocks=[{'type':'section','text':{'type':'plain_text','text':text}}]
        link=envelope['links'].get('playback')
        if link:
            blocks.append({'type':'actions','elements':[{'type':'button','text':{'type':'plain_text','text':'Open playback'},'url':link}]})
        async with netguard.client() as client:
            if dest.config.get('mode') == 'bot':
                return await send_bot(client, url, dest.config, text, blocks, frame)
            response=await client.post(url,json={'text':text,'blocks':blocks,'unfurl_links':False,'unfurl_media':False})
        check_response(response)
        if response.text.strip()!='ok': raise DeliveryError('Slack rejected delivery',permanent=True)
    except DeliveryError: raise
    except Exception: raise DeliveryError('Slack connection failed') from None
