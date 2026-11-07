"""multicast.py — paging speakers that play RTP audio sent to a multicast group.

Ported from ~/vms-actions/speaker.py. A paging speaker is configured (in its own
web UI) to LISTEN on a group such as 239.1.1.10:10000; anything sent there as
RTP G.711 u-law at 8 kHz plays. There is no session and no reply, so:

  * "on" / "announce" starts streaming a sound in the background and returns
    at once — the VMS must not wait the length of a siren for an HTTP answer;
  * "off" stops whatever is streaming to that group;
  * HEALTH cannot ask the speaker anything over multicast. It checks that the
    sound exists and the gateway can send, and — if the binding names the
    speaker's own IP (`probe`) — that the speaker answers a TCP connect on its
    web port. Without a probe the wall can only say "ready to send", which is
    the honest limit of a one-way protocol.

The interface matters: a multicast datagram goes out of the interface the
kernel's routes pick, which on this appliance is the Wi-Fi uplink, not the PoE
switch. GATEWAY_MULTICAST_IF (or `?if=`) names the local IP to send from.
"""
from __future__ import annotations

import audioop  # noqa: deprecated in 3.12, present; the image pins 3.12
import math
import os
import socket
import struct
import threading
import time
import wave
from typing import Callable, Optional

from .errors import DeviceError, code_for

PROTOCOL = "multicast-rtp"
FRAME = 160                      # 20 ms of 8 kHz u-law
TONE = "tone"                    # built-in siren when no sound file is named


def tone(seconds: float = 3.0) -> bytes:
    """A two-pitch siren, generated, so a fresh install has something to play."""
    out = bytearray()
    for i in range(int(seconds * 8000)):
        f = 900.0 if (i // 2000) % 2 == 0 else 650.0
        out += struct.pack("<h", int(12000 * math.sin(2 * math.pi * f * i / 8000)))
    return audioop.lin2ulaw(bytes(out), 2)


def load_sound(sounds_dir: str, name: Optional[str], gain: float = 1.0) -> bytes:
    """A wav from the sounds folder as 8 kHz mono u-law. `tone` or empty = built-in."""
    if not name or name == TONE:
        return tone()
    if "/" in name or "\\" in name or name.startswith("."):
        raise DeviceError("bad_sound_name")
    path = os.path.join(sounds_dir, name)
    if not os.path.isfile(path):
        raise DeviceError("sound_not_found")
    try:
        with wave.open(path) as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise DeviceError("bad_sound", str(exc)) from exc
    if ch == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if rate != 8000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
    if gain != 1.0:
        pcm = audioop.mul(pcm, 2, gain)
    return audioop.lin2ulaw(pcm, 2)


def list_sounds(sounds_dir: str) -> list[str]:
    try:
        return sorted(f for f in os.listdir(sounds_dir) if f.lower().endswith(".wav"))
    except OSError:
        return []


def probe(host: str, port: int = 80, timeout: float = 2.0) -> None:
    """Does the speaker answer at all? A TCP connect to its web port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except Exception as exc:                                      # noqa: BLE001
        raise DeviceError("speaker_" + code_for(exc), str(exc)) from exc


def open_sender(iface: Optional[str]) -> socket.socket:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        if iface:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        return s
    except OSError as exc:
        raise DeviceError("send_failed", str(exc)) from exc


class Pager:
    """One stream at a time per group. A new play replaces the one running."""

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep,
                 sender: Callable[[Optional[str]], socket.socket] = open_sender) -> None:
        self._sleep, self._sender = sleep, sender
        self._streams: dict[str, threading.Event] = {}
        self._guard = threading.Lock()

    def playing(self, group: str, port: int) -> bool:
        with self._guard:
            ev = self._streams.get(f"{group}:{port}")
            return ev is not None and not ev.is_set()

    def stop(self, group: str, port: int) -> None:
        with self._guard:
            ev = self._streams.pop(f"{group}:{port}", None)
        if ev:
            ev.set()

    def play(self, group: str, port: int, audio: bytes, *, iface: Optional[str] = None,
             repeat: int = 1, wait: bool = False) -> float:
        """Start streaming; returns the length in seconds. Raises before starting
        if the socket cannot be opened, so the caller learns of it."""
        sock = self._sender(iface)
        key = f"{group}:{port}"
        stop = threading.Event()
        with self._guard:
            old = self._streams.get(key)
            self._streams[key] = stop
        if old:
            old.set()
        repeat = max(1, min(int(repeat), 20))

        def run() -> None:
            seq, ts, ssrc = 0, 0, int.from_bytes(os.urandom(4), "big")
            t0, sent = time.monotonic(), 0
            try:
                for _ in range(repeat):
                    for i in range(0, len(audio), FRAME):
                        if stop.is_set():
                            return
                        frame = audio[i:i + FRAME].ljust(FRAME, b"\xff")
                        header = struct.pack("!BBHII", 0x80, 0x80 if seq == 0 else 0,
                                             seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
                        sock.sendto(header + frame, (group, int(port)))
                        seq, ts, sent = seq + 1, ts + FRAME, sent + 1
                        self._sleep(max(0.0, t0 + sent * 0.02 - time.monotonic()))
            except OSError:
                pass
            finally:
                sock.close()
                with self._guard:
                    if self._streams.get(key) is stop:
                        del self._streams[key]
                stop.set()

        if wait:
            run()
        else:
            threading.Thread(target=run, name=f"page-{key}", daemon=True).start()
        return repeat * len(audio) / 8000.0
