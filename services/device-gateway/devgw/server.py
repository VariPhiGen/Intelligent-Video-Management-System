"""server.py — the HTTP face of the gateway: one URL per device, two verbs.

THE CONTRACT the VMS relies on (services/camera-mgmt/.../bridge/webhook.py):

  POST <device-url>  {"command": "on", "params": {...}}   do it
  GET  <device-url>                                       health; NEVER moves anything

  200 {"ok": true,  "protocol": "modbus-tcp", "target": "...", "state": "on"}
  502 {"ok": false, "protocol": "modbus-tcp", "target": "...", "error": "timeout"}
  400 {"ok": false, ... "error": "unknown_command" | "bad_target" | ...}
  404 {"ok": false, "error": "unknown_protocol"}

`protocol` + `error` is what the trigger log and the device wall show, so a
failure always says WHICH protocol failed and WHY.

DEVICE URLS (the binding's address in the VMS):

  /relay/<host>[:port]/<channel>[?slave=1]
        a Modbus RTU-over-TCP relay board, one channel (1-based)
  /speaker/<group>:<port>[?probe=<speaker-ip>&probe_port=80&if=<local-ip>&sound=x.wav]
        a multicast paging speaker

Adding a protocol is a new module and one entry in ROUTES; the VMS is not
touched.

Binds to 127.0.0.1 by default: the api runs on the host network and reaches it
there, and nothing else should be able to open a gate.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import modbus_tcp, multicast
from .errors import DeviceError

log = logging.getLogger("devgw")

#: Command words the VMS sends (rule builder and device wall vocabularies),
#: mapped onto what a relay contact can do.
RELAY_ON = frozenset({"on", "open", "unlock", "flash", "trigger", "start"})
RELAY_OFF = frozenset({"off", "close", "lock", "stop"})
SPEAKER_PLAY = frozenset({"on", "announce", "play", "start", "flash", "trigger"})
SPEAKER_STOP = frozenset({"off", "stop"})


@dataclass
class Settings:
    sounds_dir: str = "/sounds"
    multicast_if: Optional[str] = None
    default_sound: str = multicast.TONE
    #: A relay channel switched on is switched off this long after, whatever
    #: else happens. The VMS's own pulse follow-up is the first line; this is
    #: the second, in a different process. 0 disables it.
    relay_max_on_s: float = 600.0
    #: Per device transaction. A relay on the local switch answers in ms; the
    #: VMS gives up on the whole call at 2 s, so this must stay well under it.
    timeout_s: float = 1.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            sounds_dir=os.environ.get("GATEWAY_SOUNDS_DIR", "/sounds"),
            multicast_if=os.environ.get("GATEWAY_MULTICAST_IF") or None,
            default_sound=os.environ.get("GATEWAY_DEFAULT_SOUND") or multicast.TONE,
            relay_max_on_s=float(os.environ.get("GATEWAY_RELAY_MAX_ON_S", "600")),
            timeout_s=float(os.environ.get("GATEWAY_TIMEOUT_S", "1")),
        )


@dataclass
class Result:
    status: int
    body: dict


@dataclass
class Gateway:
    settings: Settings = field(default_factory=Settings)
    board_factory: Callable[..., modbus_tcp.RelayBoard] = modbus_tcp.RelayBoard
    pager: multicast.Pager = field(default_factory=multicast.Pager)
    probe: Callable[..., None] = multicast.probe
    #: target -> last outcome, for GET /health. Bounded by the number of devices.
    seen: dict[str, dict] = field(default_factory=dict)
    _timers: dict[str, threading.Timer] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    # ── dispatch ─────────────────────────────────────────────────────────────
    def handle(self, method: str, path: str, body: Optional[dict] = None) -> Result:
        url = urlsplit(path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if method == "GET" and parts == ["health"]:
            return Result(200, {"ok": True, "devices": dict(self.seen)})
        if method == "GET" and parts == ["sounds"]:
            return Result(200, {"ok": True, "sounds": [multicast.TONE]
                                + multicast.list_sounds(self.settings.sounds_dir)})
        route = ROUTES.get(parts[0] if parts else "")
        if route is None:
            return Result(404, {"ok": False, "error": "unknown_protocol"})
        if method not in ("GET", "POST"):
            return Result(405, {"ok": False, "error": "method_not_allowed"})
        body = body or {}
        command = str(body.get("command") or "").strip().lower() if method == "POST" else None
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        return route(self, parts[1:], query, command, params)

    def _record(self, protocol: str, target: str, result: Result) -> Result:
        result.body.setdefault("protocol", protocol)
        result.body.setdefault("target", target)
        entry = self.seen.setdefault(f"{protocol}:{target}", {})
        entry["at"] = time.time()
        if result.body.get("ok"):
            entry.update(ok=True, last_ok=entry["at"], error=None,
                         state=result.body.get("state"))
        else:
            entry.update(ok=False, error=result.body.get("error"))
        level = logging.INFO if result.body.get("ok") else logging.WARNING
        log.log(level, "%s %s %s -> %s", protocol, target,
                result.body.get("command", "health"),
                result.body.get("state") if result.body.get("ok") else result.body.get("error"))
        return result

    # ── relay ────────────────────────────────────────────────────────────────
    def relay(self, args: list[str], q: dict, command: Optional[str], params: dict) -> Result:
        proto = modbus_tcp.PROTOCOL
        if len(args) != 2:
            return Result(400, {"ok": False, "protocol": proto, "error": "bad_target"})
        hostport, ch = args
        host, _, port = hostport.partition(":")
        try:
            channel = int(ch)
            board = self.board_factory(host, int(port or modbus_tcp.DEFAULT_PORT),
                                       int(q.get("slave", modbus_tcp.DEFAULT_SLAVE)),
                                       timeout=self.settings.timeout_s)
        except (ValueError, DeviceError):
            return Result(400, {"ok": False, "protocol": proto, "error": "bad_target"})
        target = f"{board.name}/ch{channel}"
        try:
            if command is None:                                   # GET: read only
                on = board.read_channel(channel)
                return self._record(proto, target, Result(200, {
                    "ok": True, "state": "on" if on else "off"}))
            if command in RELAY_ON:
                want = True
            elif command in RELAY_OFF:
                want = False
            else:
                return self._record(proto, target, Result(400, {
                    "ok": False, "command": command, "error": "unknown_command"}))
            board.write_channel(channel, want)
            self._arm_max_on(board, channel, target, want)
            return self._record(proto, target, Result(200, {
                "ok": True, "command": command, "state": "on" if want else "off"}))
        except DeviceError as exc:
            return self._record(proto, target, Result(502, {
                "ok": False, "command": command, "error": exc.code}))

    def _arm_max_on(self, board, channel: int, target: str, on: bool) -> None:
        with self._guard:
            old = self._timers.pop(target, None)
            if old:
                old.cancel()
            if not on or self.settings.relay_max_on_s <= 0:
                return

            def release() -> None:
                with self._guard:
                    if self._timers.get(target) is not timer:
                        return
                    del self._timers[target]
                try:
                    board.write_channel(channel, False)
                    log.warning("%s held on %.0fs: switched off by the gateway",
                                target, self.settings.relay_max_on_s)
                except DeviceError as exc:
                    log.error("%s max-on release failed: %s", target, exc.code)

            timer = threading.Timer(self.settings.relay_max_on_s, release)
            timer.daemon = True
            self._timers[target] = timer
            timer.start()

    # ── speaker ──────────────────────────────────────────────────────────────
    def speaker(self, args: list[str], q: dict, command: Optional[str], params: dict) -> Result:
        proto = multicast.PROTOCOL
        if len(args) != 1:
            return Result(400, {"ok": False, "protocol": proto, "error": "bad_target"})
        group, _, port = args[0].partition(":")
        try:
            port_n = int(port)
            if not group.split(".")[0].isdigit() or not 224 <= int(group.split(".")[0]) <= 239:
                raise ValueError
        except ValueError:
            return Result(400, {"ok": False, "protocol": proto, "error": "bad_target"})
        target = f"{group}:{port_n}"
        iface = q.get("if") or self.settings.multicast_if
        sound = str(params.get("sound") or q.get("sound") or self.settings.default_sound)
        try:
            if command is None:                                   # GET: health, sends nothing
                multicast.load_sound(self.settings.sounds_dir, sound)
                self.pager._sender(iface).close()
                if q.get("probe"):
                    self.probe(q["probe"], int(q.get("probe_port", 80)), self.settings.timeout_s)
                state = "playing" if self.pager.playing(group, port_n) else "ready"
                return self._record(proto, target, Result(200, {
                    "ok": True, "state": state, "probed": bool(q.get("probe"))}))
            if command in SPEAKER_STOP:
                self.pager.stop(group, port_n)
                return self._record(proto, target, Result(200, {
                    "ok": True, "command": command, "state": "ready"}))
            if command not in SPEAKER_PLAY:
                return self._record(proto, target, Result(400, {
                    "ok": False, "command": command, "error": "unknown_command"}))
            audio = multicast.load_sound(self.settings.sounds_dir, sound,
                                         float(params.get("gain", 1.0)))
            secs = self.pager.play(group, port_n, audio, iface=iface,
                                   repeat=int(params.get("repeat", q.get("repeat", 1))))
            return self._record(proto, target, Result(200, {
                "ok": True, "command": command, "state": "playing",
                "sound": sound, "seconds": round(secs, 1)}))
        except DeviceError as exc:
            return self._record(proto, target, Result(502, {
                "ok": False, "command": command, "error": exc.code}))


ROUTES: dict[str, Callable[..., Result]] = {
    "relay": Gateway.relay,
    "speaker": Gateway.speaker,
}


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vms-device-gateway"

        def _reply(self, result: Result) -> None:
            data = json.dumps(result.body).encode()
            self.send_response(result.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:                                 # noqa: N802
            self._reply(gw.handle("GET", self.path))

        def do_POST(self) -> None:                                # noqa: N802
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._reply(Result(400, {"ok": False, "error": "bad_json"}))
                return
            self._reply(gw.handle("POST", self.path, body))

        def log_message(self, fmt: str, *args: Any) -> None:     # the gateway logs outcomes
            pass

    return Handler


def serve(host: str, port: int, gw: Optional[Gateway] = None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(gw or Gateway(Settings.from_env())))
    httpd.daemon_threads = True
    return httpd
