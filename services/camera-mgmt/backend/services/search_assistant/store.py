"""Short-lived, owner/session-bound investigations and fenced turn leases."""
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from redis.exceptions import RedisError
from ...redis_client import get_redis

TTL = 1800
PREFIX = 'smartsearch:assistant:'
COMMIT = '''
if redis.call('get', KEYS[2]) ~= ARGV[1] then return 0 end
if redis.call('exists', KEYS[1]) == 0 then return 0 end
redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
'''
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1], KEYS[2]) else return 0 end"


def owner(principal):
    return hashlib.sha256(json.dumps([principal.kind, principal.subject, principal.session_id]).encode()).hexdigest()


def key(ident):
    return PREFIX + str(ident)


async def create(principal, timezone):
    ident = str(uuid4())
    state = {'id': ident, 'owner': owner(principal), 'timezone': timezone, 'version': 0,
             'confirmed': [], 'pending': None, 'exchanges': [], 'requests': {}, 'latest': []}
    r = await get_redis()
    await r.set(key(ident), json.dumps(state), ex=TTL)
    return state


async def read(ident, principal):
    r = await get_redis()
    raw = await r.get(key(ident))
    if not raw:
        raise HTTPException(410, 'This investigation expired. Start a new investigation.')
    state = json.loads(raw)
    if state['owner'] != owner(principal):
        raise HTTPException(404, 'Investigation not found.')
    await r.expire(key(ident), TTL)
    return state


async def acquire(ident):
    r = await get_redis()
    token = str(uuid4())
    if not await r.set(key(ident)+':lease', token, ex=75, nx=True):
        raise HTTPException(409, 'A turn is already running. Wait for it to finish.')
    return token


async def save(state, token):
    r = await get_redis()
    if not await r.eval(COMMIT, 2, key(state['id']), key(state['id'])+':lease', token, json.dumps(state), TTL):
        raise HTTPException(409, 'The investigation changed. Refresh before continuing.')


async def release(ident, token):
    r = await get_redis()
    await r.eval(RELEASE, 2, key(ident)+':lease', key(ident)+':progress', token)


async def progress(ident, stage):
    r = await get_redis()
    await r.set(key(ident)+':progress', stage, ex=75)


def public(state):
    return {**{k: state[k] for k in ('id', 'timezone', 'version')},
            'exchanges': [{k: e[k] for k in ('request_id', 'message', 'answer')} for e in state['exchanges']]}
