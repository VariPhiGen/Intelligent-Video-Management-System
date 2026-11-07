import hashlib
import hmac
import ssl
import time
from ....crypto import decrypt
from .. import netguard
from .common import body_bytes, check_response, DeliveryError

async def send(dest, envelope, frame=None):
    cfg=dest.config
    try:
        netguard.check_target(cfg['url'],allow_insecure=dest.allow_insecure)
        body=body_bytes(envelope)
        headers={'Content-Type':'application/json','Idempotency-Key':envelope['message_id'],'X-VMS-Event-Type':envelope['type']}
        secret=decrypt(dest.secret_enc)
        auth=cfg.get('auth','none')
        if auth!='none' and not secret:
            raise DeliveryError('Destination credential unavailable',permanent=True)
        if auth=='bearer': headers['Authorization']='Bearer '+secret
        if auth=='hmac':
            ts=str(int(time.time()))
            digest=hmac.new(secret.encode(),ts.encode()+b'.'+body,hashlib.sha256).hexdigest()
            headers['X-VMS-Signature']=f't={ts},v1={digest}'
        verify=ssl.create_default_context(cadata=cfg['ca_pem']) if cfg.get('ca_pem') else True
        async with netguard.client(verify=verify,timeout=cfg.get('timeout_s',15)) as client:
            response=await client.post(cfg['url'],content=body,headers=headers)
        check_response(response)
    except DeliveryError: raise
    except ValueError: raise DeliveryError('Destination target rejected',permanent=True) from None
    except Exception: raise DeliveryError('Webhook connection failed') from None
