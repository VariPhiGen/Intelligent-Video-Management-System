"""mqtt-modbus — one coil, one topic, one process on the bus.

The VMS speaks only MQTT (services/camera-mgmt/backend/services/bridge). This
is the translator between that and a Modbus RTU relay board: it subscribes to
one command topic, writes one coil, and publishes the coil's state back on one
state topic. For a single siren that is the whole job, and Home Assistant would
be a second platform to administer for one coil write. When the site grows a
gate and meters, swap this for HA — the VMS side does not change; that is what
the driver port was for.

WHAT ARRIVES on the command topic is whatever the binding renders: the bare
command by default (`on` / `off`), or the operator's payload_template. This
accepts the bare word, 1/0, true/false, and a JSON object carrying `command`.
Anything else is logged and ignored — a relay must never guess.

WHAT IS PUBLISHED on the state topic is the bare state, retained, so the VMS's
subscriber fills peripherals.last_state with it and the wall shows the siren
as `on` only after the coil write was acknowledged by the board.

SAFETY, IN ORDER OF IMPORTANCE. A siren that sticks on is a real incident.
  1. Wire the siren normally-open and put a hardware watchdog or timer relay in
     series. Nothing in software substitutes for that. See README.md.
  2. --max-on-seconds (MODBUS_MAX_ON_S, default 60): the coil is written OFF
     this long after any ON, whether or not an `off` ever arrives. The VMS's
     own pulse follow-up (pending_actions) is the first line; this is the
     second, in a different process, against a different failure.
  3. The coil is written OFF on startup, on broker loss and on shutdown — if
     this process cannot hear the VMS, the siren is silent, not sounding.

--dry-run logs "would write coil N = ON" instead of touching a port, and
publishes state exactly as it would for real, so the whole chain — rule →
alert → MQTT → here → state → wall — can be run before a wire is connected.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import ssl
import sys
import time
from typing import Callable, Optional

log = logging.getLogger("mqtt-modbus")

# Touched only while a broker session is live — on connect, on every message
# and every HEARTBEAT_S in between; the compose healthcheck reads its age. A
# translator that lost the broker and is sitting in backoff stops touching it
# and goes unhealthy, which is the honest state. (It was first touched on
# every reconnect ATTEMPT too, which reported healthy through a broker outage.)
HEARTBEAT = os.environ.get("HEARTBEAT_FILE", "/tmp/mqtt-modbus.heartbeat")
HEARTBEAT_S = 30


# ── Modbus RTU, function 05 only ─────────────────────────────────────────────

def crc16(data: bytes) -> int:
    """Modbus CRC-16 (poly 0xA001, init 0xFFFF), low byte first on the wire."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def write_single_coil_frame(slave: int, coil: int, on: bool) -> bytes:
    """Function 05. 0xFF00 = on, 0x0000 = off; anything else is illegal."""
    if not 1 <= slave <= 247:
        raise ValueError(f"slave id {slave} outside 1..247")
    if not 0 <= coil <= 0xFFFF:
        raise ValueError(f"coil {coil} outside 0..65535")
    pdu = bytes([slave, 0x05, coil >> 8, coil & 0xFF]) + (b"\xff\x00" if on else b"\x00\x00")
    c = crc16(pdu)
    return pdu + bytes([c & 0xFF, c >> 8])


class BusError(RuntimeError):
    """The board did not acknowledge the write. The state is UNKNOWN, and the
    caller must not publish one."""


class SerialBus:
    """A pyserial port, or anything with write()/read()/reset_input_buffer().

    Function 05's response is an echo of the request, so a write is verified
    by reading 8 bytes back and comparing. A short or different reply is a
    BusError: the relay may or may not have moved and nothing here will say
    it did.
    """

    def __init__(self, port, *, name: str = "serial") -> None:
        self._port = port
        self.name = name

    @classmethod
    def open(cls, device: str, baud: int, timeout_s: float) -> "SerialBus":
        import serial  # only a site with hardware needs pyserial importable
        return cls(serial.Serial(device, baudrate=baud, bytesize=8, parity="N",
                                 stopbits=1, timeout=timeout_s), name=device)

    def write_coil(self, slave: int, coil: int, on: bool) -> None:
        frame = write_single_coil_frame(slave, coil, on)
        self._port.reset_input_buffer()
        self._port.write(frame)
        reply = self._port.read(len(frame))
        if reply != frame:
            raise BusError(f"coil {coil} on={on}: sent {frame.hex()} got {reply.hex() or 'nothing'}")


class DryRunBus:
    """Logs the write and performs nothing. Never raises."""

    name = "dry-run"

    def __init__(self) -> None:
        self.writes: list = []

    def write_coil(self, slave: int, coil: int, on: bool) -> None:
        self.writes.append((slave, coil, on))
        log.warning("DRY RUN: would write slave %d coil %d = %s  (%s)",
                    slave, coil, "ON" if on else "OFF",
                    write_single_coil_frame(slave, coil, on).hex(" "))


# ── The translation ──────────────────────────────────────────────────────────

def parse_command(payload: str) -> Optional[bool]:
    """True for on, False for off, None for anything this must not act on."""
    s = payload.strip()
    if s.startswith("{"):
        try:
            s = str(json.loads(s).get("command", "")).strip()
        except (ValueError, AttributeError):
            return None
    s = s.lower()
    if s in ("on", "1", "true"):
        return True
    if s in ("off", "0", "false"):
        return False
    return None


class Translator:
    """Owns the coil. One writer, one lock, one watchdog."""

    def __init__(self, bus, *, slave: int, coil: int, max_on_s: float,
                 sleep: Callable = asyncio.sleep) -> None:
        self.bus = bus
        self.slave, self.coil, self.max_on_s = slave, coil, max_on_s
        self.state: Optional[bool] = None       # None = not known to be anything
        self._lock = asyncio.Lock()
        self._watchdog: Optional[asyncio.Task] = None
        self._sleep = sleep

    async def set(self, on: bool, *, why: str) -> str:
        """Write the coil; returns the state string to publish. Raises BusError."""
        async with self._lock:
            await asyncio.to_thread(self.bus.write_coil, self.slave, self.coil, on)
            self.state = on
            log.info("coil %d = %s (%s)", self.coil, "ON" if on else "OFF", why)
            if self._watchdog is not None:
                self._watchdog.cancel()
                self._watchdog = None
            if on and self.max_on_s > 0:
                self._watchdog = asyncio.get_running_loop().create_task(self._expire())
        return "on" if on else "off"

    async def _expire(self) -> None:
        await self._sleep(self.max_on_s)
        self._watchdog = None
        try:
            state = await self.set(False, why=f"watchdog: no off within {self.max_on_s:g}s")
        except BusError as exc:
            log.error("WATCHDOG OFF FAILED — check the hardware timer: %s", exc)
            return
        if self.on_watchdog is not None:
            try:
                await self.on_watchdog(state)
            except Exception as exc:                            # noqa: BLE001
                # The coil IS off; only the announcement failed. The next
                # session's startup OFF republishes the state.
                log.warning("watchdog state not published: %s", exc)

    on_watchdog: Optional[Callable] = None      # set by run(): publishes the state

    async def handle(self, payload: str) -> Optional[str]:
        """A command topic message. Returns the state to publish, or None."""
        want = parse_command(payload)
        if want is None:
            log.warning("ignored command payload %r", payload[:80])
            return None
        return await self.set(want, why="command")

    async def silence(self, why: str) -> None:
        """OFF, and never raise: this runs on the failure paths."""
        try:
            await self.set(False, why=why)
        except BusError as exc:
            log.error("could not silence on %s: %s", why, exc)


# ── MQTT session ─────────────────────────────────────────────────────────────

def _mqtt_kwargs(url: str, username: str, password: str, tls_verify: bool) -> dict:
    from urllib.parse import urlparse
    import aiomqtt
    u = urlparse(url)
    tls = u.scheme == "mqtts"
    kw: dict = {"hostname": u.hostname or "127.0.0.1",
                "port": u.port or (8883 if tls else 1883),
                "username": username or None, "password": password or None}
    # Same rule as the VMS driver: TLS arguments only when TLS is in use —
    # paho refuses tls_insecure without an SSL context, even when False.
    if tls:
        kw["tls_params"] = aiomqtt.TLSParameters(cert_reqs=None if tls_verify else ssl.CERT_NONE)
        if not tls_verify:
            kw["tls_insecure"] = True
    return kw


async def session(cfg: argparse.Namespace, tr: Translator) -> None:
    """One broker session: OFF first, then serve commands until it drops."""
    import aiomqtt
    async with aiomqtt.Client(**_mqtt_kwargs(cfg.mqtt_url, cfg.mqtt_username,
                                             cfg.mqtt_password, cfg.mqtt_tls_verify)) as client:
        log.info("connected to %s; command=%s state=%s", cfg.mqtt_url,
                 cfg.command_topic, cfg.state_topic)

        async def publish_state(state: str) -> None:
            await client.publish(cfg.state_topic, state, qos=1, retain=True)

        async def _handle(message) -> None:
            payload = message.payload
            text = (payload.decode("utf-8", "replace")
                    if isinstance(payload, (bytes, bytearray)) else str(payload))
            try:
                state = await tr.handle(text)
            except BusError as exc:
                log.error("bus refused: %s", exc)
                return
            if state is not None:
                await publish_state(state)

        tr.on_watchdog = publish_state
        # A known state before anything is accepted. If the board does not
        # answer, no state is published and the wall stays `unknown`.
        try:
            await publish_state(await tr.set(False, why="startup"))
        except BusError as exc:
            log.error("startup OFF failed, bus not trusted: %s", exc)
            raise
        await client.subscribe(cfg.command_topic, qos=1)
        _heartbeat()
        beat = asyncio.get_running_loop().create_task(_heartbeat_loop())
        try:
            async for message in client.messages:
                _heartbeat()
                await _handle(message)
        finally:
            beat.cancel()


def _heartbeat() -> None:
    try:
        with open(HEARTBEAT, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        _heartbeat()


async def run(cfg: argparse.Namespace, tr: Translator) -> None:
    while True:
        try:
            await session(cfg, tr)
        except asyncio.CancelledError:
            await tr.silence("shutdown")
            raise
        except Exception as exc:                                # noqa: BLE001
            log.warning("session ended: %s; reconnecting in %ss", exc, cfg.reconnect_s)
        tr.on_watchdog = None                   # that client is gone
        # The VMS's `then: off` cannot reach a translator with no broker.
        await tr.silence("broker lost")
        await asyncio.sleep(cfg.reconnect_s)


# ── Configuration ────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", default=_truthy(_env("MODBUS_DRY_RUN", "0")),
                   help="log coil writes instead of performing them (MODBUS_DRY_RUN=1)")
    p.add_argument("--port", default=_env("MODBUS_PORT", "/dev/ttyUSB0"))
    p.add_argument("--baud", type=int, default=int(_env("MODBUS_BAUD", "9600")))
    p.add_argument("--slave", type=int, default=int(_env("MODBUS_SLAVE_ID", "1")))
    p.add_argument("--coil", type=int, default=int(_env("MODBUS_COIL", "0")))
    p.add_argument("--serial-timeout", type=float, default=float(_env("MODBUS_TIMEOUT_S", "1")))
    p.add_argument("--max-on-seconds", type=float, default=float(_env("MODBUS_MAX_ON_S", "60")),
                   help="write OFF this long after any ON regardless (0 disables; do not)")
    p.add_argument("--mqtt-url", default=_env("MQTT_URL"))
    p.add_argument("--mqtt-username", default=_env("MQTT_USERNAME"))
    p.add_argument("--mqtt-password", default=_env("MQTT_PASSWORD"))
    p.add_argument("--mqtt-tls-verify", type=_truthy, default=_truthy(_env("MQTT_TLS_VERIFY", "true")))
    p.add_argument("--command-topic", default=_env("MODBUS_COMMAND_TOPIC"))
    p.add_argument("--state-topic", default=_env("MODBUS_STATE_TOPIC"))
    p.add_argument("--reconnect-s", type=float, default=float(_env("MODBUS_RECONNECT_S", "5")))
    cfg = p.parse_args(argv)
    missing = [n for n in ("mqtt_url", "command_topic", "state_topic") if not getattr(cfg, n)]
    if missing:
        p.error("required: " + ", ".join(f"--{m.replace('_', '-')}" for m in missing))
    if cfg.command_topic == cfg.state_topic:
        p.error("command and state topics must differ, or every state echo is a command")
    return cfg


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    cfg = parse_args(argv)
    if cfg.dry_run:
        bus = DryRunBus()
        log.warning("DRY RUN: no port will be opened; state is still published")
    else:
        bus = SerialBus.open(cfg.port, cfg.baud, cfg.serial_timeout)   # raises if absent, on purpose
        log.info("opened %s at %d 8N1, slave %d coil %d", cfg.port, cfg.baud, cfg.slave, cfg.coil)
    tr = Translator(bus, slave=cfg.slave, coil=cfg.coil, max_on_s=cfg.max_on_seconds)
    try:
        asyncio.run(run(cfg, tr))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
