"""The translator, with a fake serial port and no broker.

Run (from services/mqtt-modbus): python -m pytest tests -q

Nothing here opens a port or a socket. pyserial and aiomqtt are imported
lazily by the code under test, so this suite runs wherever pytest does.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import translator as t  # noqa: E402

_aio = pytest.mark.asyncio

ON_FRAME = bytes.fromhex("01 05 00 00 ff 00 8c 3a")
OFF_FRAME = bytes.fromhex("01 05 00 00 00 00 cd ca")


# ── The wire format ──────────────────────────────────────────────────────────

def test_crc_matches_the_published_reference_vector():
    # The textbook request "01 04 00 00 00 01" goes on the wire as
    # "01 04 00 00 00 01 31 CA": low CRC byte first.
    c = t.crc16(bytes.fromhex("010400000001"))
    assert bytes([c & 0xFF, c >> 8]) == bytes.fromhex("31ca")


def test_write_single_coil_frames_for_the_typical_board():
    """Slave 1, coil 0, 9600 8N1 — the cheap board. Bytes checked against an
    independent CRC implementation, not this one."""
    assert t.write_single_coil_frame(1, 0, True) == ON_FRAME
    assert t.write_single_coil_frame(1, 0, False) == OFF_FRAME


def test_coil_and_slave_are_range_checked():
    with pytest.raises(ValueError):
        t.write_single_coil_frame(0, 0, True)       # 0 is broadcast: no echo, no proof
    with pytest.raises(ValueError):
        t.write_single_coil_frame(1, 70000, True)


# ── The fake port ────────────────────────────────────────────────────────────

class FakePort:
    """Records what was written and answers the way a relay board does:
    function 05 echoes the request. `reply` overrides that for the failure
    tests."""

    def __init__(self, reply=None):
        self.written: list[bytes] = []
        self.reply = reply
        self.resets = 0

    def reset_input_buffer(self):
        self.resets += 1

    def write(self, data: bytes):
        self.written.append(bytes(data))

    def read(self, n: int) -> bytes:
        if self.reply is not None:
            return self.reply[:n]
        return self.written[-1][:n]


def test_a_write_is_verified_by_the_echo():
    port = FakePort()
    bus = t.SerialBus(port)
    bus.write_coil(1, 0, True)
    assert port.written == [ON_FRAME]
    assert port.resets == 1                     # stale bytes never pass as an echo


def test_a_short_reply_is_a_bus_error_and_no_state():
    bus = t.SerialBus(FakePort(reply=b"\x01\x05"))
    with pytest.raises(t.BusError):
        bus.write_coil(1, 0, True)


def test_a_different_reply_is_a_bus_error():
    """An exception frame (function | 0x80) or a reply from another slave
    means the coil did NOT move as asked."""
    bus = t.SerialBus(FakePort(reply=bytes.fromhex("01 85 01 83 50 00 00 00")))
    with pytest.raises(t.BusError):
        bus.write_coil(1, 0, True)


def test_dry_run_touches_no_port_and_records_the_write():
    bus = t.DryRunBus()
    bus.write_coil(1, 0, True)
    assert bus.writes == [(1, 0, True)]


# ── Command parsing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload,want", [
    ("on", True), ("ON", True), (" on\n", True), ("1", True), ("true", True),
    ("off", False), ("0", False), ("false", False),
    ('{"command": "on", "src": "vms"}', True), ('{"command":"off"}', False),
])
def test_accepted_payloads(payload, want):
    assert t.parse_command(payload) is want


@pytest.mark.parametrize("payload", [
    "", "open", "toggle", "2", "{", '{"cmd": "on"}', '{"command": "blast"}', "[1]",
])
def test_anything_else_is_ignored_not_guessed(payload):
    assert t.parse_command(payload) is None


# ── The translator ───────────────────────────────────────────────────────────

@_aio
async def test_a_command_writes_the_coil_and_returns_the_state_to_publish():
    port = FakePort()
    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=0)
    assert await tr.handle("on") == "on"
    assert await tr.handle("off") == "off"
    assert port.written == [ON_FRAME, OFF_FRAME]
    assert tr.state is False


@_aio
async def test_an_ignored_payload_writes_nothing_and_publishes_nothing():
    port = FakePort()
    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=0)
    assert await tr.handle("open") is None
    assert port.written == []


@_aio
async def test_a_bus_error_leaves_state_unknown_and_propagates():
    tr = t.Translator(t.SerialBus(FakePort(reply=b"")), slave=1, coil=0, max_on_s=0)
    with pytest.raises(t.BusError):
        await tr.handle("on")
    assert tr.state is None


@_aio
async def test_the_watchdog_writes_off_when_no_off_arrives():
    """The second line of defence, after the VMS's pulse follow-up: an ON
    with no OFF is turned off here, and the state is announced."""
    port = FakePort()
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=20, sleep=fake_sleep)
    announced = []

    async def on_watchdog(state):
        announced.append(state)

    tr.on_watchdog = on_watchdog
    await tr.handle("on")
    # The OFF goes through to_thread, so give it real time rather than a
    # fixed number of loop turns.
    for _ in range(200):
        if announced:
            break
        await asyncio.sleep(0.01)
    assert slept == [20]
    assert port.written == [ON_FRAME, OFF_FRAME]
    assert announced == ["off"]
    assert tr.state is False


@_aio
async def test_an_off_in_time_cancels_the_watchdog():
    port = FakePort()
    started = asyncio.Event()

    async def fake_sleep(s):
        started.set()
        await asyncio.Event().wait()            # holds until cancelled

    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=20, sleep=fake_sleep)
    await tr.handle("on")
    await started.wait()
    task = tr._watchdog
    await tr.handle("off")
    await asyncio.sleep(0)
    assert task.cancelled()
    assert tr._watchdog is None
    assert port.written == [ON_FRAME, OFF_FRAME]


@_aio
async def test_a_second_on_restarts_the_watchdog_rather_than_stacking():
    port = FakePort()
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        await asyncio.Event().wait()

    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=20, sleep=fake_sleep)
    await tr.handle("on")
    first = tr._watchdog
    await tr.handle("on")
    await asyncio.sleep(0)
    assert first.cancelled() and tr._watchdog is not first


@_aio
async def test_silence_never_raises():
    tr = t.Translator(t.SerialBus(FakePort(reply=b"")), slave=1, coil=0, max_on_s=0)
    await tr.silence("broker lost")             # BusError inside, swallowed with a log


# ── Configuration ────────────────────────────────────────────────────────────

def test_the_three_required_settings_are_required(monkeypatch):
    for k in ("MQTT_URL", "MODBUS_COMMAND_TOPIC", "MODBUS_STATE_TOPIC"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        t.parse_args([])


def test_command_and_state_topics_must_differ():
    with pytest.raises(SystemExit):
        t.parse_args(["--mqtt-url", "mqtt://b", "--command-topic", "x", "--state-topic", "x"])


def test_environment_is_the_default_and_flags_override(monkeypatch):
    monkeypatch.setenv("MQTT_URL", "mqtt://127.0.0.1:1883")
    monkeypatch.setenv("MODBUS_COMMAND_TOPIC", "vms/siren/site-siren/set")
    monkeypatch.setenv("MODBUS_STATE_TOPIC", "vms/siren/site-siren/state")
    monkeypatch.setenv("MODBUS_DRY_RUN", "1")
    monkeypatch.setenv("MODBUS_MAX_ON_S", "30")
    cfg = t.parse_args([])
    assert cfg.dry_run is True and cfg.max_on_seconds == 30 and cfg.slave == 1 and cfg.coil == 0
    cfg = t.parse_args(["--coil", "3", "--max-on-seconds", "45"])
    assert cfg.coil == 3 and cfg.max_on_seconds == 45


def test_tls_arguments_only_for_mqtts(monkeypatch):
    import types
    mod = types.ModuleType("aiomqtt")

    class TLSParameters:
        def __init__(self, **kw):
            self.kw = kw
    mod.TLSParameters = TLSParameters
    monkeypatch.setitem(sys.modules, "aiomqtt", mod)
    plain = t._mqtt_kwargs("mqtt://broker:1883", "", "", True)
    assert plain["port"] == 1883 and "tls_params" not in plain and "tls_insecure" not in plain
    import ssl
    off = t._mqtt_kwargs("mqtts://broker", "u", "p", False)
    assert off["port"] == 8883 and off["tls_params"].kw == {"cert_reqs": ssl.CERT_NONE}
    assert off["tls_insecure"] is True


# ── The session, with a stub aiomqtt ─────────────────────────────────────────

class _StubMessage:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


class _StubAiomqtt:
    """A client that connects, delivers the scripted messages, then drops the
    session the way aiomqtt does (the iterator raises)."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.published = []
        self.subscribed = []
        self.kwargs = None
        stub = self

        class Client:
            def __init__(self_, **kwargs):
                stub.kwargs = kwargs

            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

            async def publish(self_, topic, payload, qos=0, retain=False):
                stub.published.append((topic, payload, qos, retain))

            async def subscribe(self_, topic, qos=0):
                stub.subscribed.append((topic, qos))

            @property
            def messages(self_):
                async def gen():
                    for m in stub.incoming:
                        yield m
                    raise ConnectionError("Disconnected during message iteration")
                return gen()

        class TLSParameters:
            def __init__(self, **kw):
                self.kw = kw

        self.Client, self.TLSParameters = Client, TLSParameters


@pytest.fixture
def stub_session(monkeypatch, tmp_path):
    import types

    def make(incoming):
        stub = _StubAiomqtt(incoming)
        mod = types.ModuleType("aiomqtt")
        mod.Client, mod.TLSParameters = stub.Client, stub.TLSParameters
        monkeypatch.setitem(sys.modules, "aiomqtt", mod)
        monkeypatch.setattr(t, "HEARTBEAT", str(tmp_path / "hb"))
        return stub
    return make


def _cfg(**kw):
    return t.parse_args(["--mqtt-url", "mqtt://127.0.0.1:1883",
                         "--command-topic", "vms/siren/site-siren/set",
                         "--state-topic", "vms/siren/site-siren/state",
                         *kw.get("extra", [])])


@_aio
async def test_a_session_writes_off_first_then_serves_commands(stub_session, tmp_path):
    """Startup OFF is published retained before the subscription exists; a
    command writes the coil and publishes its state; the session ends when
    the broker drops and the heartbeat was touched while it was live."""
    stub = stub_session([_StubMessage("vms/siren/site-siren/set", b"on"),
                         _StubMessage("vms/siren/site-siren/set", b"open"),   # ignored
                         _StubMessage("vms/siren/site-siren/set", b"off")])
    port = FakePort()
    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=0)
    with pytest.raises(ConnectionError):
        await t.session(_cfg(), tr)
    assert stub.published == [
        ("vms/siren/site-siren/state", "off", 1, True),   # startup, retained
        ("vms/siren/site-siren/state", "on", 1, True),
        ("vms/siren/site-siren/state", "off", 1, True),
    ]
    assert stub.subscribed == [("vms/siren/site-siren/set", 1)]
    assert port.written == [OFF_FRAME, ON_FRAME, OFF_FRAME]   # the ignored one wrote nothing
    assert (tmp_path / "hb").exists()


@_aio
async def test_a_session_whose_startup_off_fails_does_not_serve(stub_session):
    """A board that does not echo the startup OFF is a bus that cannot be
    trusted: nothing is subscribed, no state is published."""
    stub = stub_session([_StubMessage("vms/siren/site-siren/set", b"on")])
    tr = t.Translator(t.SerialBus(FakePort(reply=b"")), slave=1, coil=0, max_on_s=0)
    with pytest.raises(t.BusError):
        await t.session(_cfg(), tr)
    assert stub.published == [] and stub.subscribed == []


@_aio
async def test_a_bus_refusal_mid_session_publishes_no_state(stub_session):
    stub = stub_session([_StubMessage("vms/siren/site-siren/set", b"on")])
    port = FakePort()
    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=0)
    # the startup OFF echoes; the ON gets a garbage reply
    real_read = port.read

    def read(n):
        return real_read(n) if port.written[-1] == OFF_FRAME else b"\x01\x85"
    port.read = read
    with pytest.raises(ConnectionError):
        await t.session(_cfg(), tr)
    assert [p[1] for p in stub.published] == ["off"]        # never claimed "on"
    assert tr.state is False                                 # last ACKNOWLEDGED state stands


@_aio
async def test_run_silences_on_broker_loss_and_reconnects(stub_session, monkeypatch):
    stub = stub_session([])                                  # connects, drops at once
    port = FakePort()
    tr = t.Translator(t.SerialBus(port), slave=1, coil=0, max_on_s=0)
    tr.state = True                                          # pretend it was on
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(t.asyncio, "sleep", fake_sleep)
    cfg = _cfg()
    with pytest.raises(asyncio.CancelledError):
        await t.run(cfg, tr)
    # startup OFF (session 1), OFF on loss, startup OFF (session 2), OFF on loss, then shutdown OFF
    assert port.written[:2] == [OFF_FRAME, OFF_FRAME]
    assert sleeps[0] == cfg.reconnect_s
    assert tr.on_watchdog is None                            # the dead client is not kept
