"""Export each event as a file bundle to an SFTP server (NAS, evidence store).

    <root>/2026/10/28/<camera-slug>/<message_id>/
      event.json        the envelope
      snapshot.jpg      when the destination attaches media and one exists
      complete.json     uploaded LAST: names, sizes and SHA-256 of the others

A consumer treats a folder without complete.json as still arriving. Every file
is written under a temporary name and renamed into place, and the bytes are a
pure function of the stored event and its stored snapshot, so a retry rewrites
the same bundle and a folder that already has complete.json is left alone.
The date path is the event's UTC date — stable, whatever the site timezone.

The server's host key must match the SHA-256 fingerprint the admin entered;
it is checked before any password or key is offered, and an unknown or changed
key is a permanent failure, never trusted automatically.
"""
import asyncio
import hashlib
import json
import logging
import posixpath
import re
from datetime import datetime, timezone

from ....crypto import decrypt
from .. import netguard
from .common import DeliveryError

logging.getLogger('asyncssh').setLevel(logging.WARNING)

_SAFE = re.compile(r'[^A-Za-z0-9._-]+')
_NO_TRUSTED_KEYS = ([], [], [])  # asyncssh then asks validate_host_public_key
# OpenSSH's own host-key preference, so the key we pin is the one `ssh` shows.
# A server holds several (ed25519, ecdsa, rsa); left to itself asyncssh picked
# RSA, refusing the ed25519 fingerprint an admin had copied. The probe and the
# upload use this same order, so a fetched fingerprint always matches.
HOST_KEY_ALGS = ['ssh-ed25519', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
                 'rsa-sha2-512', 'rsa-sha2-256']


def fingerprint_of(key):
    return key.get_fingerprint('sha256')


def _client_class():
    import asyncssh

    class Pinned(asyncssh.SSHClient):
        def __init__(self, expected):
            self.expected, self.seen = expected, None

        def validate_host_public_key(self, host, addr, port, key):
            self.seen = fingerprint_of(key)
            return self.expected is not None and self.seen == self.expected

    return Pinned


def public_envelope(env):
    return {k: v for k, v in env.items() if not k.startswith('_')}


def remote_dir(root, env):
    at = datetime.fromisoformat(env['occurred_at']).astimezone(timezone.utc)
    camera = _SAFE.sub('-', env['camera'].get('slug') or '').strip('-.') or 'system'
    return posixpath.join(root or '.', f'{at:%Y}', f'{at:%m}', f'{at:%d}', camera, env['message_id'])


def bundle(env, frame=None, media_label='disabled', snapshot_required=False):
    """[(name, bytes)] in upload order, complete.json last. Deterministic."""
    files = [('event.json', json.dumps(public_envelope(env), sort_keys=True, indent=2, ensure_ascii=False).encode())]
    if frame:
        files.append(('snapshot.jpg', frame))
    manifest = dict(schema_version='1', message_id=env['message_id'], type=env['type'],
                    files=[dict(name=n, size=len(b), sha256=hashlib.sha256(b).hexdigest()) for n, b in files],
                    media=dict(snapshot='attached' if frame else media_label, snapshot_required=snapshot_required))
    files.append(('complete.json', json.dumps(manifest, sort_keys=True, indent=2).encode()))
    return files


async def _connect(cfg, secret, client):
    import asyncssh
    ip = await netguard.resolve(cfg['host'], cfg['port'])
    auth = dict(password=secret, client_keys=None, preferred_auth='password,keyboard-interactive') if cfg.get('auth', 'password') == 'password' \
        else dict(client_keys=[asyncssh.import_private_key(secret)], preferred_auth='publickey')
    return await asyncssh.connect(ip, cfg['port'], username=cfg['username'], known_hosts=_NO_TRUSTED_KEYS,
                                  client_factory=lambda: client, config=None, agent_path=None,
                                  server_host_key_algs=HOST_KEY_ALGS,
                                  connect_timeout=15, login_timeout=15, **auth)


async def _put(sftp, path, data):
    """Write `data` to a temporary name, then move it over `path`."""
    import asyncssh
    tmp = posixpath.join(posixpath.dirname(path), '.' + posixpath.basename(path) + '.part')
    async with sftp.open(tmp, 'wb') as f:
        await f.write(data)
    try:
        await sftp.posix_rename(tmp, path)
    except asyncssh.SFTPOpUnsupported:
        # Plain SFTP rename refuses to overwrite; a retry may meet an earlier copy.
        if await sftp.exists(path):
            await sftp.remove(path)
        await sftp.rename(tmp, path)


async def probe_host_key(host, port):
    """The SHA-256 fingerprint the server presents — for the admin to CONFIRM, not to trust."""
    import asyncssh
    client = _client_class()(None)
    ip = await netguard.resolve(host, port)
    try:
        async with asyncio.timeout(15):
            conn = await asyncssh.connect(ip, port, username='fingerprint-probe', known_hosts=_NO_TRUSTED_KEYS,
                                          client_factory=lambda: client, config=None, agent_path=None,
                                  server_host_key_algs=HOST_KEY_ALGS,
                                          client_keys=None, password=None)
            conn.close()
    except Exception:
        pass
    return client.seen


async def send(dest, envelope, frame=None):
    import asyncssh
    cfg = dest.config
    secret = decrypt(dest.secret_enc)
    if not secret:
        raise DeliveryError('SFTP credential unavailable', permanent=True)
    label = envelope.get('_media', 'disabled')
    required = bool(cfg.get('snapshot_required'))
    folder = remote_dir(cfg.get('root'), envelope)
    files = bundle(envelope, frame, label, required)
    client = _client_class()(cfg['host_key_fingerprint'])
    try:
        async with asyncio.timeout(60):
            conn = await _connect(cfg, secret, client)
            async with conn, conn.start_sftp_client() as sftp:
                if not await sftp.exists(posixpath.join(folder, 'complete.json')):
                    await sftp.makedirs(folder, exist_ok=True)
                    for name, data in files:
                        await _put(sftp, posixpath.join(folder, name), data)
    except asyncssh.HostKeyNotVerifiable:
        seen = f' (server presented {client.seen})' if client.seen else ''
        raise DeliveryError(f'SFTP host key does not match the configured fingerprint{seen}', permanent=True) from None
    except asyncssh.PermissionDenied:
        raise DeliveryError('SFTP login rejected', permanent=True) from None
    except asyncssh.SFTPPermissionDenied:
        raise DeliveryError('Permission denied writing to the remote folder', permanent=True) from None
    except (asyncssh.SFTPNoSpaceOnFilesystem, asyncssh.SFTPQuotaExceeded):
        raise DeliveryError('Remote disk is full') from None
    except asyncssh.SFTPError as exc:
        raise DeliveryError(f'SFTP server error ({exc.code})') from None
    except (asyncssh.KeyImportError, ValueError):
        raise DeliveryError('SFTP configuration or key rejected', permanent=True) from None
    except Exception:
        raise DeliveryError('SFTP connection failed') from None
    if required and not frame:
        raise DeliveryError(f'Uploaded without the required snapshot ({label})', permanent=True)
    return folder[-120:]
