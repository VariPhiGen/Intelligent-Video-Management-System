"""modbus_tcp.py — relay boards that take Modbus RTU frames over a TCP socket.

Ported from ~/vms-actions/relay.py, which drives the site's Waveshare "Modbus
POE ETH Relay (B)" today: RTU framing (slave, function, data, CRC16) sent raw
over TCP to port 4196. That board ignores Modbus-TCP (MBAP) framing, so this is
RTU-over-TCP and nothing else.

  read coils   01 01 00 00 00 08 <crc>      -> 01 01 01 <bits> <crc>
  write coil   01 05 00 <ch-1> FF 00 <crc>  -> echo of the request

A READ never moves anything, which is what makes it safe to use as the health
check: the gateway can ask "are you there, and what is CH8 doing?" every few
seconds without ever clicking a relay.

One lock per board: these boards accept few sockets, and two transactions
interleaved on one would read each other's replies.
"""
from __future__ import annotations

import socket
import struct
import threading
from typing import Callable

from .errors import DeviceError, code_for

PROTOCOL = "modbus-tcp"
DEFAULT_PORT = 4196
DEFAULT_SLAVE = 1
DEFAULT_CHANNELS = 8

_locks: dict[tuple[str, int], threading.Lock] = {}
_locks_guard = threading.Lock()


def crc16(data: bytes) -> bytes:
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return struct.pack("<H", c)


def _lock(host: str, port: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((host, port), threading.Lock())


#: Opens a connection. Replaced in tests.
Connect = Callable[[tuple[str, int], float], socket.socket]


class RelayBoard:
    def __init__(self, host: str, port: int = DEFAULT_PORT, slave: int = DEFAULT_SLAVE,
                 *, timeout: float = 3.0, connect: Connect = socket.create_connection) -> None:
        if not 1 <= int(slave) <= 247:
            raise DeviceError("bad_slave")
        self.host, self.port, self.slave = host, int(port), int(slave)
        self.timeout = float(timeout)
        self._connect = connect

    @property
    def name(self) -> str:
        return f"{self.host}:{self.port}"

    def _txn(self, frame: bytes, expect: int) -> bytes:
        frame += crc16(frame)
        try:
            with _lock(self.host, self.port), self._connect((self.host, self.port), self.timeout) as s:
                s.settimeout(self.timeout)
                s.sendall(frame)
                buf = b""
                while len(buf) < expect:
                    chunk = s.recv(64)
                    if not chunk:
                        break
                    buf += chunk
        except DeviceError:
            raise
        except Exception as exc:                                  # noqa: BLE001
            raise DeviceError(code_for(exc), str(exc)) from exc
        if not buf:
            raise DeviceError("no_reply")
        # A Modbus exception reply is 5 bytes whatever was asked, so it is
        # recognised before the length check would call it truncated.
        if len(buf) >= 5 and buf[1] & 0x80 and crc16(buf[:3]) == buf[3:5]:
            raise DeviceError(f"modbus_exception_{buf[2]}")
        if len(buf) < expect or crc16(buf[:expect - 2]) != buf[expect - 2:expect]:
            raise DeviceError("bad_reply", buf.hex())
        return buf[:expect]

    def read_coils(self, count: int = DEFAULT_CHANNELS) -> list[bool]:
        count = max(1, min(int(count), 64))
        nbytes = (count + 7) // 8
        reply = self._txn(bytes([self.slave, 1, 0, 0, 0, count]), 5 + nbytes)
        bits = int.from_bytes(reply[3:3 + nbytes], "little")
        return [bool(bits >> i & 1) for i in range(count)]

    def read_channel(self, channel: int) -> bool:
        self._check(channel)
        return self.read_coils(max(channel, DEFAULT_CHANNELS))[channel - 1]

    def write_channel(self, channel: int, on: bool) -> None:
        self._check(channel)
        frame = bytes([self.slave, 5, 0, channel - 1, 0xFF if on else 0, 0])
        reply = self._txn(frame, 8)
        if reply[:6] != frame:                                    # function 05 echoes the request
            raise DeviceError("bad_echo", reply.hex())

    @staticmethod
    def _check(channel: int) -> None:
        if not 1 <= int(channel) <= 64:
            raise DeviceError("bad_channel")
