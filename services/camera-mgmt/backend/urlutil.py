"""urlutil.py — build a playable RTSP URL by injecting credentials.

ONVIF returns RTSP URIs without auth (e.g. rtsp://192.168.1.10:554/cam/...).  This
re-inserts the username/password (URL-encoded) into the netloc, matching the inline
credential convention the relay uses (rtsp://admin:p%40ssword@host/...) — note the
%40, since an '@' in a password would otherwise terminate the userinfo section.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, unquote, urlparse, urlunparse

# scheme://user:password@ — the password runs GREEDILY to the LAST '@' in the
# whitespace-delimited token. The first draft matched lazily to the first '@'
# and banned '/', trusting inject_credentials' URL-encoding — but that
# guarantee does not hold everywhere this is applied: POST /cameras stores an
# operator-typed rtsp_url verbatim (only the scheme is validated), so raw
# passwords like `my/pass` logged in full and `p@ss` leaked its tail as
# `***@ss@host`. Greedy-to-last-@ masks both; hosts never contain '@', so the
# final '@' is always the userinfo terminator. Known residue: a password with
# unencoded WHITESPACE still defeats this — no regex can tell where such a
# token ends inside arbitrary log text.
_USERINFO_PASSWORD_RE = re.compile(r"(\w+://[^:/@\s]*):(\S+)@")


def redact_credentials(text: str) -> str:
    """`text` with every URL-embedded password replaced by ``***``, for LOGS.

    Works on a bare URL or on arbitrary text that may contain one (an error
    body a proxy echoed back, an ffmpeg command line). The username is kept —
    it is what tells two credential sets apart when debugging — only the
    password is masked. Never feed the result back into anything that plays or
    stores the URL: it is a display form, not a URL.

    This exists because a composed relay source was logged verbatim
    (relay.add_path.ok carried rtsps://admin:p%40ss@...), which put camera
    passwords in plaintext logs on an appliance whose whole at-rest design
    encrypts them.
    """
    return _USERINFO_PASSWORD_RE.sub(r"\1:***@", text)


def inject_credentials(uri: str, user: Optional[str], password: Optional[str]) -> str:
    if not user:
        return uri
    p = urlparse(uri)
    userinfo = quote(user, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    host = p.hostname or ""
    netloc = f"{userinfo}@{host}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse(p._replace(netloc=netloc))


def split_credentials(uri: str) -> tuple[str, Optional[str], Optional[str]]:
    """Inverse of inject_credentials: return (credential-less uri, user, password).

    Lets a hand-entered URL with inline creds (rtsp://admin:pw@host/...) be stored
    the same way ONVIF results are — creds encrypted separately, url_raw clean.
    """
    p = urlparse(uri)
    user = unquote(p.username) if p.username else None
    password = unquote(p.password) if p.password else None
    host = p.hostname or ""
    netloc = host
    if p.port:
        netloc += f":{p.port}"
    return urlunparse(p._replace(netloc=netloc)), user, password


def host_of(uri: str) -> Optional[str]:
    """Host/IP portion of an RTSP URL, or None if it can't be parsed."""
    try:
        return urlparse(uri).hostname
    except ValueError:
        return None


def to_rtsps(uri: str) -> Optional[str]:
    """Same URL over the rtsps:// scheme, or None if it already is (or isn't RTSP).

    Used to retry a TLS-only camera: ONVIF GetStreamUri advertises rtsp:// even
    on devices whose :554 speaks nothing but TLS, so the URL a scan stores is
    unplayable until the scheme is corrected.
    """
    if not uri.startswith("rtsp://"):
        return None
    return "rtsps://" + uri[len("rtsp://"):]
