"""crypto.py — symmetric encryption for camera credentials at rest.

Camera passwords entered in the portal are stored Fernet-encrypted in the
discovery DB.  The Fernet key is derived deterministically from
DISCOVERY_SECRET_KEY (SHA-256 → urlsafe base64) so operators only manage one
secret.  Rotating the secret invalidates stored credentials (they'd need re-entry),
which is acceptable for this use case.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.discovery_secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    if plaintext is None or plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None
