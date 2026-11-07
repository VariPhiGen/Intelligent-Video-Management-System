from ....crypto import decrypt
from .. import netguard
from .common import format_text, check_response, DeliveryError

async def send(dest,envelope,frame=None):
    try:
        token=decrypt(dest.secret_enc)
        if not token: raise DeliveryError('Telegram credential unavailable',permanent=True)
        data={'chat_id':dest.config['chat_id']}
        if dest.config.get('topic_id'): data['message_thread_id']=dest.config['topic_id']
        text=format_text(envelope)
        async with netguard.client() as client:
            if frame:
                data['caption']=text
                response=await client.post(f'https://api.telegram.org/bot{token}/sendPhoto',data=data,
                    files={'photo':('frame.jpg',frame,'image/jpeg')})
            else:
                data.update(text=text,disable_web_page_preview=True)
                response=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json=data)
        try: result=response.json()
        except ValueError:
            check_response(response)
            raise DeliveryError('Invalid Telegram response') from None
        if not result.get('ok'):
            code=result.get('error_code',response.status_code)
            raise DeliveryError(f'Telegram rejected delivery ({code})',permanent=400<=code<500 and code not in (408,429),
                retry_after=max(0,float((result.get('parameters') or {}).get('retry_after',0))))
        check_response(response)
        return str(result['result']['message_id'])
    except DeliveryError: raise
    except Exception: raise DeliveryError('Telegram connection failed') from None
