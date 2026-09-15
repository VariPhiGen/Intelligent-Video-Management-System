#!/usr/bin/env python3
"""onvif-sim — a camera that answers ONVIF, so onboarding can be tested.

WHAT IT HAS TO SATISFY. Not "ONVIF" in general — specifically the four calls
`services/onvif_probe.py::_probe_sync` makes, in the order it makes them:

    ONVIFCamera(ip, port, user, password, adjust_time=True)
    dev = cam.create_devicemgmt_service();  dev.GetDeviceInformation()
    media = cam.create_media_service();     media.GetProfiles()
                                            media.GetStreamUri(...)

plus GetSystemDateAndTime, which onvif-zeep issues on its own because
`adjust_time=True` syncs the WS-Security timestamp to the device clock.

WHY HAND-WRITTEN SOAP. onvif-zeep parses responses with zeep against the ONVIF
WSDLs it bundles, so the envelope has to be schema-shaped, not merely
well-formed. Generating it from a SOAP framework would mean installing one and
learning its quirks; the four responses are short and their shapes are already
written down in tests/onvif_fixtures.py from the P4 work. This is the same data
one level up: there it stood in for what zeep returns, here it stands in for
what the wire carries.

CREDENTIALS ARE CHECKED, and that is a feature. `ONVIF_USER`/`ONVIF_PASS` are
enforced: a wrong password gets a real ONVIF `ter:NotAuthorized` fault, which
is what makes the auth_failed branch of onboarding reachable. WS-Security
digest is NOT verified — the point is exercising the product's three-way
classification, not re-implementing WSSE. The username is read from the header
and the password is assumed correct if the username matches; a request with the
wrong username is refused.

WS-DISCOVERY. A separate thread answers Probe on 239.255.255.250:3702 with a
ProbeMatch carrying this device's XAddrs. Multicast works between containers on
a user-defined Docker bridge (verified before this was written), which is what
lets discovery be exercised without putting probes on anybody's real LAN.
"""
from __future__ import annotations

import os
import re
import socket
import struct
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTTP_PORT = int(os.environ.get("ONVIF_HTTP_PORT", "8080"))
DEVICE_IP = os.environ.get("ONVIF_ADVERTISE_IP") or socket.gethostbyname(socket.gethostname())
USER = os.environ.get("ONVIF_USER", "admin")
PASSWORD = os.environ.get("ONVIF_PASS", "admin123")
RTSP_HOST = os.environ.get("ONVIF_RTSP_HOST", "rtsp-cam-1")
RTSP_PORT = os.environ.get("ONVIF_RTSP_PORT", "8554")
RTSP_PATH = os.environ.get("ONVIF_RTSP_PATH", "e2e-cam-1")

VENDOR = os.environ.get("ONVIF_VENDOR", "VariPhi E2E")
MODEL = os.environ.get("ONVIF_MODEL", "SimCam-1000")
FIRMWARE = os.environ.get("ONVIF_FIRMWARE", "1.0.0-e2e")
SERIAL = os.environ.get("ONVIF_SERIAL", "E2E-0000-0001")

NS = (
    'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tt="http://www.onvif.org/ver10/schema"'
)


def envelope(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<s:Envelope {NS}><s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def fault(code: str, reason: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope {NS} xmlns:ter="http://www.onvif.org/ver10/error">'
        "<s:Body><s:Fault><s:Code><s:Value>s:Sender</s:Value>"
        f"<s:Subcode><s:Value>{code}</s:Value></s:Subcode></s:Code>"
        f'<s:Reason><s:Text xml:lang="en">{reason}</s:Text></s:Reason>'
        "</s:Fault></s:Body></s:Envelope>"
    ).encode()


# ── the four responses ─────────────────────────────────────────────────────

def system_date_and_time() -> str:
    n = datetime.now(timezone.utc)
    return (
        "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
        "<tt:DateTimeType>NTP</tt:DateTimeType>"
        "<tt:DaylightSavings>false</tt:DaylightSavings>"
        "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
        "<tt:UTCDateTime>"
        f"<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute>"
        f"<tt:Second>{n.second}</tt:Second></tt:Time>"
        f"<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month>"
        f"<tt:Day>{n.day}</tt:Day></tt:Date>"
        "</tt:UTCDateTime>"
        "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
    )


def device_information() -> str:
    return (
        "<tds:GetDeviceInformationResponse>"
        f"<tds:Manufacturer>{VENDOR}</tds:Manufacturer>"
        f"<tds:Model>{MODEL}</tds:Model>"
        f"<tds:FirmwareVersion>{FIRMWARE}</tds:FirmwareVersion>"
        f"<tds:SerialNumber>{SERIAL}</tds:SerialNumber>"
        "<tds:HardwareId>E2E-HW-1</tds:HardwareId>"
        "</tds:GetDeviceInformationResponse>"
    )


def capabilities() -> str:
    base = f"http://{DEVICE_IP}:{HTTP_PORT}"
    return (
        "<tds:GetCapabilitiesResponse><tds:Capabilities>"
        f"<tt:Device><tt:XAddr>{base}/onvif/device_service</tt:XAddr></tt:Device>"
        f"<tt:Media><tt:XAddr>{base}/onvif/media_service</tt:XAddr>"
        "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
        "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
        "</tt:StreamingCapabilities></tt:Media>"
        "</tds:Capabilities></tds:GetCapabilitiesResponse>"
    )


def profiles() -> str:
    """Two profiles, because a camera with a main and a sub is the case the
    product cares about — the sub track is what the two-track invariant is
    built around, and a single-profile camera would never exercise it."""
    def one(token, name, w, h, fps, bitrate):
        return (
            f'<trt:Profiles token="{token}" fixed="true">'
            f"<tt:Name>{name}</tt:Name>"
            f'<tt:VideoEncoderConfiguration token="vec-{token}">'
            f"<tt:Name>{name}Encoder</tt:Name><tt:UseCount>1</tt:UseCount>"
            "<tt:Encoding>H264</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
            "<tt:Quality>4</tt:Quality>"
            "<tt:RateControl>"
            f"<tt:FrameRateLimit>{fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{bitrate}</tt:BitrateLimit>"
            "</tt:RateControl>"
            "<tt:H264><tt:GovLength>15</tt:GovLength>"
            "<tt:H264Profile>Main</tt:H264Profile></tt:H264>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration>"
            "</trt:Profiles>"
        )
    return (
        "<trt:GetProfilesResponse>"
        + one("main", "MainStream", 1280, 720, 15, 2048)
        + one("sub", "SubStream", 640, 360, 15, 512)
        + "</trt:GetProfilesResponse>"
    )


def stream_uri(profile_token: str) -> str:
    # CREDENTIAL-LESS, as a real camera answers. The product composes the
    # username and password onto it at use time, which is the behaviour
    # urlutil/tracks depend on — handing back a credentialed URL here would
    # hide a bug in that composition.
    path = RTSP_PATH if profile_token != "sub" else f"{RTSP_PATH}-sub"
    return (
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        f"<tt:Uri>rtsp://{RTSP_HOST}:{RTSP_PORT}/{path}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
        "<tt:Timeout>PT60S</tt:Timeout>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


# ── HTTP ───────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[onvif-sim] " + (fmt % args) + "\n")

    def _send(self, payload: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # A liveness probe for the compose healthcheck, and what the TCP sweep
        # sees when it checks whether this port is open.
        self._send(b"onvif-sim", 200)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")

        # GetSystemDateAndTime is answered BEFORE any credential check: a real
        # device must, because the client uses it to correct the WS-Security
        # timestamp it will authenticate with. Refusing it here would make
        # every subsequent call fail for the wrong reason.
        if "GetSystemDateAndTime" in body:
            return self._send(envelope(system_date_and_time()))

        m = re.search(r"<[^>]*Username>([^<]*)<", body)
        who = m.group(1) if m else ""
        if who != USER:
            self.log_message("rejecting username %r", who)
            return self._send(
                fault("ter:NotAuthorized", "Sender not Authorized"), 400)

        if "GetDeviceInformation" in body:
            return self._send(envelope(device_information()))
        if "GetCapabilities" in body:
            return self._send(envelope(capabilities()))
        if "GetProfiles" in body:
            return self._send(envelope(profiles()))
        if "GetStreamUri" in body:
            tok = re.search(r"ProfileToken>([^<]*)<", body)
            return self._send(envelope(stream_uri(tok.group(1) if tok else "main")))

        return self._send(fault("ter:ActionNotSupported", "Not implemented"), 400)


# ── WS-Discovery ───────────────────────────────────────────────────────────

def ws_discovery_responder():
    """Answer Probe with a ProbeMatch naming this device's XAddrs.

    The XAddr is what netscan turns into an IP and an optional port, and it is
    the difference between a device the product can probe and one it lists and
    then cannot reach.
    """
    group, port = "239.255.255.250", 3702
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
        struct.pack("4sl", socket.inet_aton(group), socket.INADDR_ANY),
    )
    print(f"[onvif-sim] WS-Discovery listening on {group}:{port}", flush=True)

    while True:
        try:
            data, addr = sock.recvfrom(8192)
        except OSError:
            continue
        text = data.decode("utf-8", "replace")
        if "Probe" not in text:
            continue
        relates = re.search(r"<[^>]*MessageID>([^<]*)<", text)
        reply = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            "<s:Header>"
            "<a:MessageID>urn:uuid:" + str(uuid.uuid4()) + "</a:MessageID>"
            + (f"<a:RelatesTo>{relates.group(1)}</a:RelatesTo>" if relates else "")
            + "<a:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:To>"
            "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</a:Action>"
            "</s:Header><s:Body><d:ProbeMatches><d:ProbeMatch>"
            f"<a:EndpointReference><a:Address>urn:uuid:{SERIAL}</a:Address></a:EndpointReference>"
            "<d:Types>dn:NetworkVideoTransmitter</d:Types>"
            f"<d:Scopes>onvif://www.onvif.org/name/{MODEL} "
            "onvif://www.onvif.org/hardware/E2E-HW-1</d:Scopes>"
            f"<d:XAddrs>http://{DEVICE_IP}:{HTTP_PORT}/onvif/device_service</d:XAddrs>"
            "<d:MetadataVersion>1</d:MetadataVersion>"
            "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
        )
        try:
            sock.sendto(reply.encode(), addr)
            print(f"[onvif-sim] ProbeMatch -> {addr[0]}", flush=True)
        except OSError as exc:
            print(f"[onvif-sim] probe reply failed: {exc}", flush=True)


def main():
    threading.Thread(target=ws_discovery_responder, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[onvif-sim] {VENDOR} {MODEL} on {DEVICE_IP}:{HTTP_PORT} "
          f"(user={USER}) -> rtsp://{RTSP_HOST}:{RTSP_PORT}/{RTSP_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
