import re
import ssl
from typing import Literal
from uuid import UUID
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from .netguard import check_host, check_target

EventType=Literal['ai.event.created','ai.event.closed','alert.raised','camera.offline','camera.online']
Kind=Literal['webhook','telegram','slack','kafka','email','sftp']
DEFAULT_RATES={'webhook':6000,'kafka':6000,'telegram':20,'slack':60,'email':30,'sftp':600}
#: Kinds that can carry the event snapshot.
MEDIA_KINDS=('telegram','slack','email','sftp')
# No whitespace, commas or angle brackets: nothing that can split a header.
EMAIL=r'^[^@\s<>,;"]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$'
FINGERPRINT=r'^SHA256:[A-Za-z0-9+/]{43}=?$'
class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')

class WebhookConfig(Strict):
    url: str = Field(max_length=2048)
    auth: Literal['none','bearer','hmac']='none'
    ca_pem: str | None = Field(default=None,max_length=32768)
    timeout_s: int = Field(default=15,ge=1,le=20)
class TelegramConfig(Strict):
    chat_id: str = Field(min_length=1,max_length=120,pattern=r'^[-@\w]+$')
    topic_id: int | None = Field(default=None,ge=1)
class SlackConfig(Strict):
    mode: Literal['webhook','bot']='webhook'
    channel_id: str | None = Field(default=None,pattern=r'^[CG][A-Z0-9]{8,}$',max_length=120)
class KafkaConfig(Strict):
    bootstrap: str = Field(min_length=1,max_length=2048)
    topic: str = Field(min_length=1,max_length=249,pattern=r'^[a-zA-Z0-9._-]+$')
    security: Literal['plaintext','ssl','sasl_ssl']='plaintext'
    sasl_mechanism: Literal['PLAIN','SCRAM-SHA-256','SCRAM-SHA-512']='PLAIN'
    username: str | None = Field(default=None,max_length=256)
    ca_pem: str | None = Field(default=None,max_length=32768)

class EmailConfig(Strict):
    to: list[str]=Field(min_length=1,max_length=50)
    cc: list[str]=Field(default_factory=list,max_length=50)
    bcc: list[str]=Field(default_factory=list,max_length=50)
    subject_prefix: str | None=Field(default=None,max_length=60,pattern=r'^[^\r\n]*$')
    @model_validator(mode='after')
    def addresses(self):
        for a in (*self.to,*self.cc,*self.bcc):
            if not re.fullmatch(EMAIL,a): raise ValueError('Invalid email address')
        return self
class SftpConfig(Strict):
    host: str=Field(min_length=1,max_length=253)
    port: int=Field(default=22,ge=1,le=65535)
    username: str=Field(min_length=1,max_length=128,pattern=r'^[^\s:/]+$')
    auth: Literal['password','key']='password'
    root: str=Field(default='.',min_length=1,max_length=512)
    host_key_fingerprint: str=Field(pattern=FINGERPRINT)
    snapshot_required: bool=False
    @model_validator(mode='after')
    def paths(self):
        check_host(self.host)
        if any(ord(c)<32 for c in self.root) or '..' in self.root.split('/'):
            raise ValueError('Remote folder must not contain ".." or control characters')
        return self

class DestinationIn(Strict):
    name: str = Field(min_length=1,max_length=120)
    kind: Kind
    config: dict
    secret: SecretStr | None = None
    enabled: bool=False
    media: bool=False
    allow_insecure: bool=False
    # None = the kind's default (DEFAULT_RATES). 6000/min covers a busy site's
    # machine feed; 20/min is Telegram's per-group limit.
    rate_per_min: int | None=Field(default=None,ge=1,le=60000)
    max_age_s: int | None=Field(default=None,ge=1,le=604800)

    @model_validator(mode='after')
    def validate_config(self):
        if self.rate_per_min is None:
            self.rate_per_min=DEFAULT_RATES[self.kind]
        cls={'webhook':WebhookConfig,'telegram':TelegramConfig,'slack':SlackConfig,'kafka':KafkaConfig,
             'email':EmailConfig,'sftp':SftpConfig}[self.kind]
        self.config=cls.model_validate(self.config).model_dump()
        if self.kind=='webhook':
            check_target(self.config['url'],allow_insecure=self.allow_insecure)
        if self.kind=='kafka':
            for broker in self.config['bootstrap'].split(','):
                p=urlsplit('https://'+broker.strip())
                if not p.port or p.path or p.query:
                    raise ValueError('Kafka brokers must be host:port pairs')
                check_target('https://'+broker.strip())
        ca=self.config.get('ca_pem')
        if ca:
            try: ssl.create_default_context(cadata=ca)
            except Exception: raise ValueError('Invalid CA certificate') from None
        value=self.secret.get_secret_value() if self.secret else None
        if self.kind=='slack' and value:
            if self.config['mode']=='bot':
                if not re.fullmatch(r'xoxb-[A-Za-z0-9-]+',value):
                    raise ValueError('Use a Slack Bot User OAuth Token (xoxb-)')
            else:
                p=urlsplit(value)
                if p.scheme!='https' or p.hostname!='hooks.slack.com' or not p.path.startswith('/services/') or p.username or p.password or p.query or p.fragment or p.port not in (None,443):
                    raise ValueError('Use a Slack incoming webhook URL')
        if self.kind=='slack':
            if self.config['mode']=='bot' and not self.config['channel_id']:
                raise ValueError('Slack bot delivery requires a channel ID')
            if self.media and self.config['mode']!='bot':
                raise ValueError('Slack images require bot delivery')
        if self.kind=='telegram' and value and not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',value):
            raise ValueError('Invalid Telegram bot token')
        if self.media and self.kind not in MEDIA_KINDS:
            raise ValueError('Camera frames are supported by Telegram, Slack, email and SFTP only')
        if self.kind=='sftp':
            if self.config['snapshot_required'] and not self.media:
                raise ValueError('A required snapshot needs "attach snapshot" turned on')
            if value and self.config['auth']=='key':
                import asyncssh
                try: asyncssh.import_private_key(value)
                except Exception: raise ValueError('Use an unencrypted OpenSSH or PEM private key') from None
        return self

class DestinationPatch(Strict):
    name: str | None=None
    config: dict | None=None
    secret: SecretStr | None=None
    enabled: bool | None=None
    media: bool | None=None
    allow_insecure: bool | None=None
    rate_per_min: int | None=None
    max_age_s: int | None=None

class Filters(Strict):
    cameras: list[UUID]=Field(default_factory=list,max_length=1000)
    activities: list[str]=Field(default_factory=list,max_length=200)
    rules: list[UUID]=Field(default_factory=list,max_length=1000)
    min_severity: Literal['info','warning','critical']='info'
    include_motion: bool=False

class SubscriptionIn(Strict):
    destination_id: UUID
    name: str=Field(min_length=1,max_length=120)
    types: list[EventType]=Field(min_length=1,max_length=5)
    filters: Filters=Field(default_factory=Filters)
    cooldown_s: int | None=Field(default=None,ge=0,le=86400)
    enabled: bool=True
class SubscriptionPatch(Strict):
    name: str | None=None
    types: list[EventType] | None=None
    filters: Filters | None=None
    cooldown_s: int | None=None
    enabled: bool | None=None
class ControlIn(Strict):
    enabled: bool
    feed_enabled: bool
    #: Omitted: unchanged. '' clears it (back to EVENTS_PUBLIC_BASE_URL).
    public_base_url: str | None=Field(default=None,max_length=512)
    @model_validator(mode='after')
    def base_url(self):
        url=(self.public_base_url or '').strip().rstrip('/')
        if url:
            parts=urlsplit(url)
            if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password \
                    or parts.query or parts.fragment or '#' in url:
                raise ValueError('Public address must be an http(s) URL such as https://vms.example.lan')
        if self.public_base_url is not None:
            self.public_base_url=url
        return self
class SmtpIn(Strict):
    host: str=Field(min_length=1,max_length=253)
    port: int=Field(default=587,ge=1,le=65535)
    security: Literal['starttls','tls','none']='starttls'
    verify_tls: bool=True
    ca_pem: str | None=Field(default=None,max_length=32768)
    username: str | None=Field(default=None,max_length=256)
    #: Omitted: keep the saved one. '' removes it.
    password: SecretStr | None=None
    from_address: str=Field(pattern=EMAIL)
    from_name: str | None=Field(default=None,max_length=120,pattern=r'^[^\r\n]*$')
    @model_validator(mode='after')
    def check(self):
        check_host(self.host)
        if self.username and self.security=='none':
            raise ValueError('A mail server login needs STARTTLS or TLS, so the password is not sent in clear')
        if self.ca_pem:
            try: ssl.create_default_context(cadata=self.ca_pem)
            except Exception: raise ValueError('Invalid CA certificate') from None
        return self
class HostKeyProbeIn(Strict):
    host: str=Field(min_length=1,max_length=253)
    port: int=Field(default=22,ge=1,le=65535)
class TestIn(Strict):
    include_live_frame: bool=False
    camera_id: UUID | None=None
