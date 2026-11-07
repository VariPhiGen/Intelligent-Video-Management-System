"""Email through the site mail server (event_integration_settings.smtp).

"Sent" means the mail server ACCEPTED the message — a successful SMTP handoff,
not proof that it reached an inbox or that anyone read it. Recipients the
server refused are reported in provider_ref; when it refuses all of them the
delivery fails.

The Message-ID is derived from the event's message_id, so a retry after a lost
reply carries the same ID and mail clients can collapse the duplicate.
"""
import html
import re
import ssl
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate
from zoneinfo import ZoneInfo

from ....config import settings
from ....crypto import decrypt
from ....db import AsyncSessionLocal
from ....models import EventIntegrationSettings
from .. import netguard
from .common import DeliveryError

_ID_DOMAIN = re.compile(r'[^A-Za-z0-9.-]+')
_AUTH_HINT = ('Mail server rejected the login. Some cloud providers need an app password '
              'or an SMTP relay; OAuth sign-in is not supported.')


async def profile():
    """(smtp config dict, password) — or (None, None) when no mail server is set."""
    async with AsyncSessionLocal() as db:
        row = await db.get(EventIntegrationSettings, 1)
        if row is None or not row.smtp:
            return None, None
        return dict(row.smtp), decrypt(row.smtp_secret_enc) if row.smtp_secret_enc else None


def _title(env):
    d = env['data']
    return d.get('rule_name') or d.get('activity_label') or env['type'].replace('.', ' ').capitalize()


def _facts(env):
    """(label, value) rows shared by the text and HTML bodies."""
    d = env['data']
    at = datetime.fromisoformat(env['occurred_at'])
    local = at.astimezone(ZoneInfo(settings.site_timezone))
    rows = [('Event', _title(env)), ('Camera', env['camera']['name'])]
    if d.get('severity'):
        rows.append(('Severity', d['severity'].capitalize()))
    if d.get('zone'):
        rows.append(('Zone', d['zone']))
    rows += [('Time', f'{local:%d %b %Y %H:%M:%S %Z} ({env["occurred_at"]})'), ('Type', env['type']),
             ('Message ID', env['message_id'])]
    if env.get('_frame_caption'):
        rows.append(('Snapshot', env['_frame_caption']))
    return rows


def build_message(env, cfg, smtp, frame=None):
    d = env['data']
    severity = (d.get('severity') or '').upper()
    prefix = (cfg.get('subject_prefix') or '').strip()
    subject = ' '.join(p for p in (prefix, f"{severity + ' · ' if severity else ''}{_title(env)} — {env['camera']['name']}") if p)
    msg = EmailMessage()
    msg['Subject'] = subject[:250]
    msg['From'] = Address(display_name=smtp.get('from_name') or '', addr_spec=smtp['from_address'])
    msg['To'] = ', '.join(cfg['to'])
    if cfg.get('cc'):
        msg['Cc'] = ', '.join(cfg['cc'])
    msg['Date'] = formatdate(localtime=False)
    domain = _ID_DOMAIN.sub('-', settings.events_deployment_id or 'vms').strip('-.') or 'vms'
    msg['Message-ID'] = f"<{env['message_id']}@{domain}>"
    msg['X-VMS-Event-Type'] = env['type']
    facts = _facts(env)
    link = env['links'].get('playback')
    text = '\n'.join(f'{k}: {v}' for k, v in facts)
    if link:
        text += f'\n\nOpen playback (VMS login required): {link}'
    msg.set_content(text)
    rows = ''.join(f'<tr><th align="left" style="padding:2px 12px 2px 0">{html.escape(k)}</th>'
                   f'<td>{html.escape(str(v))}</td></tr>' for k, v in facts)
    body = f'<table>{rows}</table>'
    if link:
        body += f'<p><a href="{html.escape(link, quote=True)}">Open playback</a> (VMS login required)</p>'
    msg.add_alternative(f'<html><body>{body}</body></html>', subtype='html')
    if frame:
        msg.add_attachment(frame, maintype='image', subtype='jpeg', filename='snapshot.jpg')
    return msg


class _NamedContext(ssl.SSLContext):
    """Verifies the certificate against the configured NAME while the socket
    goes to the netguard-approved IP — aiosmtplib would otherwise check the IP."""
    name = None

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        return super().wrap_bio(incoming, outgoing, server_side, self.name or server_hostname, session)


def _tls_context(smtp):
    ctx = _NamedContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if smtp.get('ca_pem'):
        ctx.load_verify_locations(cadata=smtp['ca_pem'])
    else:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    if not smtp.get('verify_tls', True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.name = smtp['host'].strip('[]')
    return ctx


def smtp_client(**kwargs):
    import aiosmtplib
    return aiosmtplib.SMTP(**kwargs)


def _code_error(prefix, code):
    return DeliveryError(f'{prefix} ({code})', permanent=500 <= code < 600)


async def send(dest, envelope, frame=None):
    import aiosmtplib
    smtp, password = await profile()
    if not smtp:
        raise DeliveryError('Mail server is not configured', permanent=True)
    if smtp.get('username') and not password:
        raise DeliveryError('Mail server password unavailable', permanent=True)
    cfg = dest.config
    msg = build_message(envelope, cfg, smtp, frame)
    recipients = list(dict.fromkeys([*cfg['to'], *(cfg.get('cc') or []), *(cfg.get('bcc') or [])]))
    security = smtp.get('security', 'starttls')
    try:
        ip = await netguard.resolve(smtp['host'], smtp['port'])
        client = smtp_client(hostname=ip, port=smtp['port'], timeout=20,
                             use_tls=security == 'tls', start_tls=security == 'starttls',
                             tls_context=_tls_context(smtp) if security != 'none' else None)
        async with client:
            if smtp.get('username'):
                await client.login(smtp['username'], password)
            refused, reply = await client.send_message(msg, sender=smtp['from_address'], recipients=recipients)
    except aiosmtplib.SMTPAuthenticationError:
        raise DeliveryError(_AUTH_HINT, permanent=True) from None
    except aiosmtplib.SMTPRecipientsRefused as exc:
        codes = [r.code for r in exc.recipients]
        raise DeliveryError(f'Mail server refused every recipient ({", ".join(map(str, sorted(set(codes))))})',
                            permanent=all(500 <= c < 600 for c in codes)) from None
    except aiosmtplib.SMTPSenderRefused as exc:
        raise _code_error('Mail server refused the sender address', exc.code) from None
    except aiosmtplib.SMTPDataError as exc:
        raise _code_error('Mail server rejected the message', exc.code) from None
    except aiosmtplib.SMTPNotSupported:
        raise DeliveryError('Mail server does not support the configured security or login', permanent=True) from None
    except aiosmtplib.SMTPResponseException as exc:
        raise _code_error('Mail server error', exc.code) from None
    except ssl.SSLCertVerificationError:
        raise DeliveryError('Mail server TLS certificate is not trusted', permanent=True) from None
    except ValueError:
        raise DeliveryError('Mail server address rejected', permanent=True) from None
    except Exception:
        raise DeliveryError('Mail server connection failed') from None
    accepted = len(recipients) - len(refused)
    ref = f'Accepted by mail server for {accepted}/{len(recipients)}'
    if refused:
        ref += '; refused ' + ', '.join(f'{addr} ({r.code})' for addr, r in refused.items())
    return ref[:120]
