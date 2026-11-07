"""Device gateway: the URL contract the VMS relies on, against fake devices.

Run (from services/device-gateway): python -m pytest -q
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from devgw import modbus_tcp, multicast  # noqa: E402
from devgw.server import Gateway, Settings, serve  # noqa: E402

crc16 = modbus_tcp.crc16


class FakeBoard:
    """An 8-channel RTU-over-TCP relay board, as a socket factory."""

    def __init__(self, behaviour: str = "ok") -> None:
        self.coils = [False] * 8
        self.behaviour = behaviour
        self.frames: list[bytes] = []

    def __call__(self, addr, timeout):
        if self.behaviour == "refused":
            raise ConnectionRefusedError("refused")
        if self.behaviour == "timeout":
            raise socket.timeout("timed out")
        return FakeSock(self)

    def reply(self, frame: bytes) -> bytes:
        self.frames.append(frame)
        if self.behaviour == "silent":
            return b""
        if self.behaviour == "garbage":
            return b"\x01\x02\x03"
        if self.behaviour == "exception":
            body = bytes([frame[0], frame[1] | 0x80, 2])
            return body + crc16(body)
        slave, fn = frame[0], frame[1]
        if fn == 1:
            count = frame[5]
            bits = sum(1 << i for i, on in enumerate(self.coils[:count]) if on)
            nbytes = (count + 7) // 8
            body = bytes([slave, 1, nbytes]) + bits.to_bytes(nbytes, "little")
            return body + crc16(body)
        if fn == 5:
            self.coils[frame[3]] = frame[4] == 0xFF
            return frame
        raise AssertionError(frame)


class FakeSock:
    def __init__(self, board: FakeBoard) -> None:
        self.board, self.out = board, b""

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.out = self.board.reply(data)

    def recv(self, n):
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def gateway(board: FakeBoard, **settings) -> Gateway:
    def factory(host, port, slave, *, timeout):
        return modbus_tcp.RelayBoard(host, port, slave, timeout=timeout, connect=board)
    return Gateway(Settings(**{"relay_max_on_s": 0, **settings}), board_factory=factory)


R = "/relay/192.168.1.200/8"


class TestRelay:
    def test_get_reads_the_channel_and_switches_nothing(self):
        board = FakeBoard()
        board.coils[7] = True
        r = gateway(board).handle("GET", R)
        assert r.status == 200 and r.body["state"] == "on"
        assert r.body["protocol"] == "modbus-tcp" and r.body["target"] == "192.168.1.200:4196/ch8"
        assert all(f[1] == 1 for f in board.frames), "health must only READ coils"

    @pytest.mark.parametrize("command,state", [("on", True), ("unlock", True), ("open", True),
                                               ("off", False), ("lock", False), ("close", False)])
    def test_vms_command_words_map_onto_the_contact(self, command, state):
        board = FakeBoard()
        board.coils[7] = not state
        r = gateway(board).handle("POST", R, {"command": command})
        assert r.status == 200 and board.coils[7] is state
        assert board.frames[-1] == bytes([1, 5, 0, 7, 0xFF if state else 0, 0]) + \
            crc16(bytes([1, 5, 0, 7, 0xFF if state else 0, 0]))

    def test_an_unknown_command_moves_nothing(self):
        board = FakeBoard()
        r = gateway(board).handle("POST", R, {"command": "announce"})
        assert r.status == 400 and r.body["error"] == "unknown_command" and board.frames == []

    @pytest.mark.parametrize("behaviour,code", [("refused", "connection_refused"),
                                                ("timeout", "timeout"), ("silent", "no_reply"),
                                                ("garbage", "bad_reply"),
                                                ("exception", "modbus_exception_2")])
    def test_every_failure_names_its_protocol_and_reason(self, behaviour, code):
        r = gateway(FakeBoard(behaviour)).handle("POST", R, {"command": "on"})
        assert r.status == 502
        assert (r.body["ok"], r.body["protocol"], r.body["error"]) == (False, "modbus-tcp", code)

    def test_port_and_slave_come_from_the_url(self):
        seen = {}

        def factory(host, port, slave, *, timeout):
            seen.update(host=host, port=port, slave=slave)
            return modbus_tcp.RelayBoard(host, port, slave, connect=FakeBoard())
        Gateway(Settings(relay_max_on_s=0), board_factory=factory).handle(
            "GET", "/relay/10.0.0.5:502/3?slave=7")
        assert seen == {"host": "10.0.0.5", "port": 502, "slave": 7}

    @pytest.mark.parametrize("path", ["/relay/192.168.1.200", "/relay/192.168.1.200/x",
                                      "/relay/h/1?slave=0"])
    def test_a_malformed_target_is_a_400(self, path):
        assert gateway(FakeBoard()).handle("GET", path).status == 400

    def test_the_max_on_timer_switches_a_channel_back_off(self):
        board = FakeBoard()
        gw = gateway(board, relay_max_on_s=0.05)
        gw.handle("POST", R, {"command": "on"})
        assert board.coils[7] is True
        gw._timers[next(iter(gw._timers))].join(1)
        assert board.coils[7] is False

    def test_an_off_cancels_the_max_on_timer(self):
        board = FakeBoard()
        gw = gateway(board, relay_max_on_s=30)
        gw.handle("POST", R, {"command": "on"})
        gw.handle("POST", R, {"command": "off"})
        assert gw._timers == {}

    def test_health_page_remembers_each_device(self):
        gw = gateway(FakeBoard())
        gw.handle("GET", R)
        bad = Gateway(Settings(relay_max_on_s=0), board_factory=lambda h, p, s, timeout: modbus_tcp.RelayBoard(
            h, p, s, connect=FakeBoard("refused")))
        bad.seen = gw.seen
        bad.handle("GET", "/relay/192.168.1.201/1")
        devices = gw.handle("GET", "/health").body["devices"]
        assert devices["modbus-tcp:192.168.1.200:4196/ch8"]["ok"] is True
        assert devices["modbus-tcp:192.168.1.201:4196/ch1"]["error"] == "connection_refused"


class FakeSender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, iface):
        if self.fail:
            from devgw.errors import DeviceError
            raise DeviceError("send_failed")
        return self

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        pass


S = "/speaker/239.1.1.10:10000"


def speaker_gw(tmp_path, sender=None, probe=None):
    pager = multicast.Pager(sleep=lambda s: None, sender=sender or FakeSender())
    gw = Gateway(Settings(sounds_dir=str(tmp_path)), pager=pager)
    if probe:
        gw.probe = probe
    return gw


class TestSpeaker:
    def test_play_streams_rtp_ulaw_to_the_group(self, tmp_path):
        sender = FakeSender()
        gw = speaker_gw(tmp_path, sender)
        r = gw.handle("POST", S, {"command": "on"})
        assert r.status == 200 and r.body["state"] == "playing" and r.body["sound"] == "tone"
        for t in threading.enumerate():
            if t.name.startswith("page-"):
                t.join(2)
        assert sender.sent and all(addr == ("239.1.1.10", 10000) for _, addr in sender.sent)
        assert all(len(d) == 12 + 160 and d[0] == 0x80 for d, _ in sender.sent)

    def test_a_missing_sound_is_named(self, tmp_path):
        r = speaker_gw(tmp_path).handle("POST", S, {"command": "on", "params": {"sound": "nope.wav"}})
        assert r.status == 502 and r.body["error"] == "sound_not_found"
        assert r.body["protocol"] == "multicast-rtp"

    def test_a_path_in_a_sound_name_is_refused(self, tmp_path):
        r = speaker_gw(tmp_path).handle("POST", S, {"command": "on", "params": {"sound": "../x.wav"}})
        assert r.body["error"] == "bad_sound_name"

    def test_health_sends_nothing(self, tmp_path):
        sender = FakeSender()
        r = speaker_gw(tmp_path, sender).handle("GET", S)
        assert r.status == 200 and r.body["state"] == "ready" and sender.sent == []

    def test_health_probes_the_speaker_when_told_its_ip(self, tmp_path):
        from devgw.errors import DeviceError

        def probe(host, port, timeout):
            raise DeviceError("speaker_timeout")
        r = speaker_gw(tmp_path, probe=probe).handle("GET", S + "?probe=192.168.1.102")
        assert r.status == 502 and r.body["error"] == "speaker_timeout"

    def test_not_a_multicast_group_is_a_400(self, tmp_path):
        assert speaker_gw(tmp_path).handle("GET", "/speaker/192.168.1.5:10000").status == 400

    def test_a_socket_that_cannot_open_is_reported(self, tmp_path):
        r = speaker_gw(tmp_path, FakeSender(fail=True)).handle("POST", S, {"command": "on"})
        assert r.status == 502 and r.body["error"] == "send_failed"


def test_an_unknown_protocol_is_a_404():
    assert Gateway().handle("GET", "/sip/100").status == 404


def test_over_real_http(tmp_path):
    """The same contract through the socket the VMS uses."""
    board = FakeBoard()
    httpd = serve("127.0.0.1", 0, gateway(board))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(base + R, data=json.dumps({"command": "on"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            assert json.load(resp)["state"] == "on"
        with urllib.request.urlopen(base + R) as resp:
            assert json.load(resp)["state"] == "on"
        bad = urllib.request.Request(base + R, data=b"[1]", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 400
    finally:
        httpd.shutdown()
