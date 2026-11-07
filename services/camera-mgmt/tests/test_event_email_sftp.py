"""Email and SFTP event destinations, against REAL servers.

The adapters talk to an in-process aiosmtpd mail server (plain, STARTTLS with a
private CA, login) and an in-process asyncssh SFTP server. Nothing about the
protocols is mocked; only `netguard.resolve` is pointed at 127.0.0.1, because
the guard (rightly) refuses loopback — its own refusal is tested separately.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
import asyncio
import datetime as dt
import hashlib
import json
import socket
import ssl
from email import message_from_bytes, policy
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from backend.crypto import encrypt
from backend.services.event_delivery import netguard
from backend.services.event_delivery.adapters import email as email_adapter
from backend.services.event_delivery.adapters import sftp as sftp_adapter
from backend.services.event_delivery.adapters.common import DeliveryError
from backend.services.event_delivery.envelope import CameraRef, build
from backend.services.event_delivery.schemas import DestinationIn, SmtpIn

asyncssh = pytest.importorskip('asyncssh')
aiosmtpd = pytest.importorskip('aiosmtpd')

pytestmark = pytest.mark.asyncio


def envelope(**data):
    at = dt.datetime(2026, 10, 28, 9, 30, tzinfo=dt.timezone.utc)
    env = build('alert.raised', str(uuid4()), 'alert', CameraRef(uuid4(), 'gate-1', 'Gate <1>'), at, at,
                dict(rule_name='Intrusion', severity='critical', zone='North', **data))
    env['links'] = {'playback': 'https://vms.site.lan/#/playback?cam=gate-1&t=1'}
    return env


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def loopback(monkeypatch):
    async def resolve(host, port, timeout=15):
        return '127.0.0.1'
    monkeypatch.setattr(netguard, 'resolve', resolve)


# ── the guard itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '169.254.169.254', 'bad host', ''])
async def test_non_http_targets_refuse_local_and_bogus_hosts(host):
    with pytest.raises(ValueError):
        await netguard.resolve(host, 25)


async def test_lan_addresses_are_allowed():
    assert await netguard.resolve('192.168.1.20', 25) == '192.168.1.20'


# ── schemas ─────────────────────────────────────────────────────────────────

def dest_in(kind, config, **kw):
    return DestinationIn.model_validate(dict(name='x', kind=kind, config=config, **kw))


@pytest.mark.parametrize('addr', ['no-at', 'a@b\r\nBcc: evil@x.com', 'a,b@c.com', '<a@b.com>'])
async def test_email_addresses_cannot_split_headers(addr):
    with pytest.raises(ValueError):
        dest_in('email', {'to': [addr]})


async def test_email_destination_takes_media_and_needs_no_secret():
    d = dest_in('email', {'to': ['ops@site.lan'], 'cc': ['a@b.co']}, media=True)
    assert d.rate_per_min == 30 and d.config['bcc'] == []


FP = 'SHA256:' + 'A' * 43


@pytest.mark.parametrize('config,media', [
    ({'host': 'nas.lan', 'username': 'vms'}, False),                                     # no fingerprint
    ({'host': 'nas.lan', 'username': 'vms', 'host_key_fingerprint': 'MD5:aa'}, False),   # wrong format
    ({'host': 'nas.lan', 'username': 'vms', 'host_key_fingerprint': FP, 'root': '../etc'}, False),
    ({'host': '127.0.0.1', 'username': 'vms', 'host_key_fingerprint': FP}, False),
    ({'host': 'nas.lan', 'username': 'vms', 'host_key_fingerprint': FP, 'snapshot_required': True}, False),
])
async def test_sftp_config_is_refused(config, media):
    with pytest.raises(ValueError):
        dest_in('sftp', config, media=media)


async def test_sftp_key_must_be_a_usable_private_key():
    cfg = {'host': 'nas.lan', 'username': 'vms', 'host_key_fingerprint': FP, 'auth': 'key'}
    with pytest.raises(ValueError):
        dest_in('sftp', cfg, secret='not a key')
    key = asyncssh.generate_private_key('ssh-ed25519').export_private_key().decode()
    assert dest_in('sftp', cfg, secret=key).config['auth'] == 'key'


async def test_smtp_login_over_plaintext_is_refused():
    with pytest.raises(ValueError):
        SmtpIn.model_validate(dict(host='mail.lan', security='none', username='u', from_address='vms@site.lan'))
    SmtpIn.model_validate(dict(host='mail.lan', security='none', from_address='vms@site.lan'))  # LAN relay


# ── email: message shape ────────────────────────────────────────────────────

SMTP = dict(host='mail.test', port=25, security='none', from_address='vms@site.lan', from_name='Site VMS')


async def test_message_is_stable_escaped_and_carries_the_snapshot():
    env = envelope()
    env['_frame_caption'] = 'Frame captured around 15:00:01 IST (1 s after event)'
    cfg = {'to': ['ops@site.lan'], 'cc': ['sec@site.lan'], 'bcc': ['audit@site.lan'], 'subject_prefix': '[West]'}
    a = email_adapter.build_message(env, cfg, SMTP, b'\xff\xd8jpeg')
    b = email_adapter.build_message(env, cfg, SMTP, b'\xff\xd8jpeg')
    assert a['Message-ID'] == b['Message-ID'] == f"<{env['message_id']}@vms>"
    assert a['Subject'] == '[West] CRITICAL · Intrusion — Gate <1>'
    assert 'Bcc' not in a, 'Bcc recipients must not appear in the headers'
    html = a.get_body(('html',)).get_content()
    assert 'Gate &lt;1&gt;' in html and 'Open playback' in html
    text = a.get_body(('plain',)).get_content()
    assert '2026-10-28T09:30:00Z' in text and 'Frame captured around' in text
    jpegs = [p for p in a.iter_attachments() if p.get_filename() == 'snapshot.jpg']
    assert len(jpegs) == 1 and jpegs[0].get_content() == b'\xff\xd8jpeg'


# ── email: a real SMTP server ───────────────────────────────────────────────

def _tls_pair(tmp_path):
    """A private CA and a server cert for mail.test, as PEM paths + CA text."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'mail.test')]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('mail.test')]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (tmp_path / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    return str(tmp_path / 'cert.pem'), str(tmp_path / 'key.pem'), ca.public_bytes(serialization.Encoding.PEM).decode()


class Mailbox:
    def __init__(self, refuse=(), code='550 5.1.1 No such user'):
        self.refuse, self.code, self.messages = set(refuse), code, []

    async def handle_RCPT(self, server, session, env, address, rcpt_options):
        if address in self.refuse:
            return self.code
        env.rcpt_tos.append(address)
        return '250 OK'

    async def handle_DATA(self, server, session, env):
        self.messages.append(SimpleNamespace(rcpt=list(env.rcpt_tos), mail_from=env.mail_from,
                                             msg=message_from_bytes(env.content, policy=policy.default)))
        return '250 Message accepted'


@pytest.fixture
def smtp_server(loopback, monkeypatch, tmp_path):
    """Start an aiosmtpd server; `profile` serves the matching site settings."""
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult
    started = []

    def start(handler, *, starttls=False, login=None, **smtp):
        port = free_port()
        kw = {}
        ca = None
        if starttls:
            cert, key, ca = _tls_pair(tmp_path)
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert, key)
            kw.update(tls_context=ctx, require_starttls=True)
        if login:
            def auth(server, session, envelope, mechanism, data):
                ok = (data.login.decode(), data.password.decode()) == login
                return AuthResult(success=ok, handled=False)
            kw.update(authenticator=auth, auth_require_tls=starttls)
        c = Controller(handler, hostname='127.0.0.1', port=port, **kw)
        c.start()
        started.append(c)
        cfg = dict(SMTP, port=port, security='starttls' if starttls else 'none', **smtp)
        if ca:
            cfg.setdefault('ca_pem', ca)
        password = smtp.pop('password', None) or (login[1] if login else None)
        if login:
            cfg['username'] = cfg.get('username') or login[0]
        cfg.pop('password', None)

        async def profile():
            return cfg, password
        monkeypatch.setattr(email_adapter, 'profile', profile)
        return cfg
    yield start
    for c in started:
        c.stop()


def email_dest(**cfg):
    return SimpleNamespace(id=uuid4(), kind='email', config={'to': ['ops@site.lan'], 'cc': [], 'bcc': [], **cfg},
                           secret_enc=None, media=True, allow_insecure=False)


async def test_plain_relay_receives_the_event(smtp_server):
    box = Mailbox()
    smtp_server(box)
    ref = await email_adapter.send(email_dest(bcc=['audit@site.lan']), envelope(), b'\xff\xd8x')
    assert ref == 'Accepted by mail server for 2/2'
    got = box.messages[0]
    assert sorted(got.rcpt) == ['audit@site.lan', 'ops@site.lan'] and got.mail_from == 'vms@site.lan'
    assert 'Bcc' not in got.msg and 'Intrusion' in got.msg['Subject']


async def test_starttls_with_private_ca_and_login(smtp_server):
    box = Mailbox()
    smtp_server(box, starttls=True, login=('vms', 's3cret'))
    assert (await email_adapter.send(email_dest(), envelope())).startswith('Accepted')
    assert len(box.messages) == 1


async def test_untrusted_certificate_is_a_permanent_failure(smtp_server):
    smtp_server(Mailbox(), starttls=True, ca_pem=None)
    with pytest.raises(DeliveryError) as err:
        await email_adapter.send(email_dest(), envelope())
    assert err.value.permanent and 'not trusted' in str(err.value)


async def test_wrong_password_is_permanent_and_explains_providers(smtp_server):
    smtp_server(Mailbox(), starttls=True, login=('vms', 's3cret'), password='wrong')
    with pytest.raises(DeliveryError) as err:
        await email_adapter.send(email_dest(), envelope())
    assert err.value.permanent and 'app password' in str(err.value) and 'wrong' not in str(err.value)


async def test_some_recipients_refused_is_sent_and_reported(smtp_server):
    box = Mailbox(refuse={'gone@site.lan'})
    smtp_server(box)
    ref = await email_adapter.send(email_dest(to=['ops@site.lan', 'gone@site.lan']), envelope())
    assert ref == 'Accepted by mail server for 1/2; refused gone@site.lan (550)'
    assert box.messages[0].rcpt == ['ops@site.lan']


@pytest.mark.parametrize('code,permanent', [('550 5.1.1 No such user', True), ('451 4.3.0 Try later', False)])
async def test_every_recipient_refused_fails(smtp_server, code, permanent):
    smtp_server(Mailbox(refuse={'ops@site.lan'}, code=code))
    with pytest.raises(DeliveryError) as err:
        await email_adapter.send(email_dest(), envelope())
    assert err.value.permanent is permanent


async def test_no_mail_server_is_permanent(monkeypatch):
    async def profile(): return None, None
    monkeypatch.setattr(email_adapter, 'profile', profile)
    with pytest.raises(DeliveryError) as err:
        await email_adapter.send(email_dest(), envelope())
    assert err.value.permanent


async def test_unreachable_mail_server_is_retried(smtp_server):
    cfg = smtp_server(Mailbox())
    cfg['port'] = free_port()  # nothing listens there
    with pytest.raises(DeliveryError) as err:
        await email_adapter.send(email_dest(), envelope())
    assert not err.value.permanent


# ── SFTP: a real server ─────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sftp_server(loopback, tmp_path):
    """An asyncssh SFTP server chrooted to tmp_path/remote. Counts login attempts."""
    host_key = asyncssh.generate_private_key('ssh-ed25519')
    client_key = asyncssh.generate_private_key('ssh-ed25519')
    root = tmp_path / 'remote'
    root.mkdir()
    logins = []

    class Server(asyncssh.SSHServer):
        def connection_made(self, conn): self.conn = conn
        def begin_auth(self, username):
            self.conn.set_authorized_keys(asyncssh.import_authorized_keys(client_key.export_public_key().decode()))
            return True
        def password_auth_supported(self): return True
        def validate_password(self, username, password):
            logins.append(('password', username))
            return (username, password) == ('vms', 'pw')
        def public_key_auth_supported(self): return True

    port = free_port()
    server = await asyncssh.listen('127.0.0.1', port, server_host_keys=[host_key], server_factory=Server,
                                   sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)))
    yield SimpleNamespace(port=port, root=root, logins=logins, client_key=client_key,
                          fingerprint=host_key.get_fingerprint('sha256'))
    server.close()
    await server.wait_closed()


def sftp_dest(srv, secret='pw', **cfg):
    config = {**dict(host='nas.lan', port=srv.port, username='vms', auth='password', root='/site-west',
                     host_key_fingerprint=srv.fingerprint, snapshot_required=False), **cfg}
    return SimpleNamespace(id=uuid4(), kind='sftp', config=config, secret_enc=encrypt(secret), media=True,
                           allow_insecure=False)


def bundle_dir(srv, env):
    return srv.root / 'site-west' / '2026' / '10' / '28' / 'gate-1' / env['message_id']


async def test_bundle_lands_complete_with_manifest_hashes(sftp_server):
    env = envelope()
    env['_media'] = 'attached'
    ref = await sftp_adapter.send(sftp_dest(sftp_server), env, b'\xff\xd8jpeg')
    folder = bundle_dir(sftp_server, env)
    assert ref.endswith(env['message_id'])
    assert sorted(p.name for p in folder.iterdir()) == ['complete.json', 'event.json', 'snapshot.jpg']
    manifest = json.loads((folder / 'complete.json').read_text())
    for f in manifest['files']:
        assert hashlib.sha256((folder / f['name']).read_bytes()).hexdigest() == f['sha256']
    assert manifest['media'] == {'snapshot': 'attached', 'snapshot_required': False}
    assert json.loads((folder / 'event.json').read_text())['message_id'] == env['message_id']
    assert '_media' not in (folder / 'event.json').read_text()


async def test_retry_rewrites_identical_bytes_and_skips_a_complete_bundle(sftp_server):
    env = envelope()
    d = sftp_dest(sftp_server)
    await sftp_adapter.send(d, env, b'\xff\xd8jpeg')
    folder = bundle_dir(sftp_server, env)
    first = {p.name: p.read_bytes() for p in folder.iterdir()}
    # An interrupted earlier attempt: no manifest, a stale temp file.
    (folder / 'complete.json').unlink()
    (folder / '.event.json.part').write_bytes(b'partial')
    await sftp_adapter.send(d, env, b'\xff\xd8jpeg')
    again = {p.name: p.read_bytes() for p in folder.iterdir() if not p.name.startswith('.')}
    assert again == first
    # Complete already: nothing is rewritten.
    (folder / 'event.json').write_bytes(b'tampered')
    await sftp_adapter.send(d, env, b'\xff\xd8jpeg')
    assert (folder / 'event.json').read_bytes() == b'tampered'


async def test_changed_host_key_fails_before_any_credential_is_sent(sftp_server):
    other = asyncssh.generate_private_key('ssh-ed25519').get_fingerprint('sha256')
    with pytest.raises(DeliveryError) as err:
        await sftp_adapter.send(sftp_dest(sftp_server, host_key_fingerprint=other), envelope())
    assert err.value.permanent and sftp_server.fingerprint in str(err.value)
    assert sftp_server.logins == [], 'the password must not reach an unverified server'


async def test_wrong_password_is_permanent(sftp_server):
    with pytest.raises(DeliveryError) as err:
        await sftp_adapter.send(sftp_dest(sftp_server, secret='nope'), envelope())
    assert err.value.permanent and 'nope' not in str(err.value)


async def test_key_auth(sftp_server):
    key = sftp_server.client_key.export_private_key().decode()
    env = envelope()
    await sftp_adapter.send(sftp_dest(sftp_server, secret=key, auth='key'), env)
    assert (bundle_dir(sftp_server, env) / 'complete.json').exists()


async def test_required_snapshot_missing_uploads_but_fails(sftp_server):
    env = envelope()
    env['_media'] = 'unavailable'
    with pytest.raises(DeliveryError) as err:
        await sftp_adapter.send(sftp_dest(sftp_server, snapshot_required=True), env)
    assert err.value.permanent and 'required snapshot' in str(err.value)
    manifest = json.loads((bundle_dir(sftp_server, env) / 'complete.json').read_text())
    assert manifest['media'] == {'snapshot': 'unavailable', 'snapshot_required': True}


async def test_unreachable_sftp_is_retried(loopback):
    d = SimpleNamespace(id=uuid4(), kind='sftp', secret_enc=encrypt('pw'), media=False, allow_insecure=False,
                        config=dict(host='nas.lan', port=free_port(), username='vms', auth='password', root='.',
                                    host_key_fingerprint=FP, snapshot_required=False))
    with pytest.raises(DeliveryError) as err:
        await sftp_adapter.send(d, envelope())
    assert not err.value.permanent


async def test_probe_reports_the_fingerprint_without_logging_in(sftp_server):
    assert await sftp_adapter.probe_host_key('nas.lan', sftp_server.port) == sftp_server.fingerprint
    assert sftp_server.logins == []


async def test_a_server_with_several_host_keys_is_pinned_by_its_ed25519_key(loopback, tmp_path):
    """Found end to end against OpenSSH: with ed25519 AND RSA host keys, asyncssh
    negotiated RSA, so the ed25519 fingerprint an admin copies from `ssh` (or
    `ssh-keygen -lf`) was refused. Probe and upload must agree with OpenSSH's order."""
    ed = asyncssh.generate_private_key('ssh-ed25519')
    rsa = asyncssh.generate_private_key('ssh-rsa', key_size=2048)
    root = tmp_path / 'r'
    root.mkdir()

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username): return True
        def password_auth_supported(self): return True
        def validate_password(self, username, password): return password == 'pw'

    port = free_port()
    server = await asyncssh.listen('127.0.0.1', port, server_host_keys=[rsa, ed], server_factory=Server,
                                   sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)))
    try:
        fp = ed.get_fingerprint('sha256')
        assert await sftp_adapter.probe_host_key('nas.lan', port) == fp
        d = SimpleNamespace(id=uuid4(), kind='sftp', secret_enc=encrypt('pw'), media=False, allow_insecure=False,
                            config=dict(host='nas.lan', port=port, username='vms', auth='password', root='.',
                                        host_key_fingerprint=fp, snapshot_required=False))
        await sftp_adapter.send(d, envelope())
    finally:
        server.close()
        await server.wait_closed()
