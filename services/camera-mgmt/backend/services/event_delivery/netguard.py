"""LAN targets allowed; local/metadata targets blocked at the actual TCP dial."""
import asyncio
import ipaddress
import socket
import logging
import re
from urllib.parse import urlsplit
import httpx
from httpcore._backends.auto import AutoBackend


def check_ip(value):
    ip=ipaddress.ip_address(value.split('%')[0])
    if getattr(ip,'ipv4_mapped',None):
        ip=ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved or str(ip)=='fd00:ec2::254':
        raise ValueError('Local, link-local and metadata targets are not allowed')
    return str(ip)

def check_target(url, *, allow_insecure=False):
    try:
        p=urlsplit(url)
        if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        if p.scheme=='http' and not allow_insecure:
            raise ValueError()
        if p.hostname.lower().rstrip('.') in ('localhost','metadata.google.internal') or '%' in p.hostname:
            raise ValueError()
        _=p.port
    except ValueError:
        raise ValueError('Use an HTTPS target without credentials; LAN HTTP requires explicit permission') from None
    try:
        ipaddress.ip_address(p.hostname)
    except ValueError:
        return
    check_ip(p.hostname)

_HOST=re.compile(r'^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$')

def check_host(host):
    """A bare hostname or IP for a non-HTTP target (SMTP, SFTP): same rules as check_target."""
    value=(host or '').strip()
    if value.startswith('[') and value.endswith(']'): value=value[1:-1]
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not _HOST.match(value) or value.lower().rstrip('.') in ('localhost','metadata.google.internal'):
            raise ValueError('Use a valid hostname or IP address') from None
        return value
    check_ip(value)
    return value

async def resolve(host, port, timeout=15):
    """Resolve once and return the first address that passes check_ip.

    Non-HTTP adapters dial this literal, never the name, so a DNS answer that
    changes between check and connect cannot reach a blocked address.
    """
    host=check_host(host)
    answers=await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM),timeout)
    addresses=list(dict.fromkeys(check_ip(answer[4][0]) for answer in answers))
    if not addresses:
        raise ValueError('Target has no usable addresses')
    return addresses[0]

class GuardedBackend(AutoBackend):
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # Resolve once, validate all answers, then dial a literal address. TLS still
        # uses the original hostname supplied by httpcore's start_tls (SNI).
        answers=await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM),timeout or 15)
        addresses=list(dict.fromkeys(check_ip(answer[4][0]) for answer in answers))
        if not addresses:
            raise ValueError('Target has no usable addresses')
        return await super().connect_tcp(addresses[0],port,timeout,local_address,socket_options)

class _HideSensitiveHTTP(logging.Filter):
    def filter(self, record):
        # httpx logs the complete URL at INFO, including bot tokens and Slack
        # webhook credentials. Never let its request diagnostic leave here.
        return False

logging.getLogger('httpx').addFilter(_HideSensitiveHTTP())
logging.getLogger('httpcore').setLevel(logging.WARNING)

def client(*, verify=True, timeout=15):
    transport=httpx.AsyncHTTPTransport(verify=verify,retries=0)
    transport._pool._network_backend=GuardedBackend()
    return httpx.AsyncClient(transport=transport,timeout=timeout,follow_redirects=False,trust_env=False)
