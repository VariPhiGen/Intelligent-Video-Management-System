"""errors.py — one exception type, carrying a stable code.

Every failure the gateway reports is `<protocol>/<code>`, e.g.
`modbus-tcp/timeout`. The VMS stores that string in the trigger log and on the
device wall and people filter on it, so codes are snake_case tokens, never
prose, and never change meaning once shipped.
"""
from __future__ import annotations

import errno
import socket


class DeviceError(Exception):
    """A device could not be reached or did not do what it was told."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def code_for(exc: BaseException) -> str:
    """The stable code for a socket-level failure."""
    if isinstance(exc, DeviceError):
        return exc.code
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, ConnectionResetError):
        return "connection_reset"
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "unreachable"
    if isinstance(exc, socket.gaierror):
        return "bad_host"
    return "io_error"
