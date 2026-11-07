import asyncio
import hashlib
import json
import socket
import ssl
from urllib.parse import urlsplit
from ....crypto import decrypt
from ..netguard import check_ip
from .common import body_bytes, DeliveryError

def producer_factory(**kwargs):
    from aiokafka import AIOKafkaProducer
    return AIOKafkaProducer(**kwargs)

_producers={}
_locks={}

async def close_all(keep=None):
    for key,(version,producer) in list(_producers.items()):
        if keep is not None and key in keep: continue
        _producers.pop(key,None)
        try: await asyncio.wait_for(producer.stop(),5)
        except Exception: pass

async def send(dest,envelope,frame=None):
    key=str(dest.id)
    async with _locks.setdefault(key,asyncio.Lock()):
        try:
            async with asyncio.timeout(20):
                cfg=dest.config
                version=hashlib.sha256(json.dumps([cfg,dest.secret_enc],sort_keys=True).encode()).hexdigest()
                old=_producers.get(key)
                if old and old[0]!=version:
                    _producers.pop(key)
                    await old[1].stop()
                    old=None
                if old is None:
                    for broker in cfg['bootstrap'].split(','):
                        p=urlsplit('https://'+broker.strip())
                        answers=await asyncio.get_running_loop().getaddrinfo(p.hostname,p.port,type=socket.SOCK_STREAM)
                        for answer in answers: check_ip(answer[4][0])
                    security=cfg.get('security','plaintext').upper()
                    kwargs=dict(bootstrap_servers=cfg['bootstrap'],acks='all',enable_idempotence=True,
                        request_timeout_ms=15000,security_protocol=security)
                    if security!='PLAINTEXT': kwargs['ssl_context']=ssl.create_default_context(cadata=cfg.get('ca_pem'))
                    if security=='SASL_SSL':
                        kwargs.update(sasl_mechanism=cfg.get('sasl_mechanism','PLAIN'),sasl_plain_username=cfg.get('username'),
                            sasl_plain_password=decrypt(dest.secret_enc))
                    producer=producer_factory(**kwargs)
                    try: await producer.start()
                    except BaseException:
                        try: await asyncio.wait_for(producer.stop(),2)
                        except Exception: pass
                        raise
                    _producers[key]=(version,producer)
                producer=_producers[key][1]
                result=await producer.send_and_wait(cfg['topic'],body_bytes(envelope),key=envelope['camera']['id'].encode(),
                    headers=[('message_id',envelope['message_id'].encode()),('type',envelope['type'].encode())])
                return f'{result.partition}:{result.offset}'
        except ValueError: raise DeliveryError('Kafka configuration rejected',permanent=True) from None
        except Exception as exc:
            permanent=type(exc).__name__ in ('AuthenticationFailedError','TopicAuthorizationFailedError','SaslAuthenticationFailedError')
            raise DeliveryError('Kafka authorization failed' if permanent else 'Kafka delivery failed',permanent=permanent) from None
