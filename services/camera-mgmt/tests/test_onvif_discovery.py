"""onvif_probe.py + netscan.py — telling apart the three ways onboarding fails.

WHAT AN INSTALLER SEES. Discovery sorts every host it touched into one of three
buckets, and the bucket decides what the person on the ladder does next:

  ok           add the camera
  auth_failed  go and get the right password
  no_onvif     this is not a camera, or it is on a port you did not scan

Misfiling one is not a crash, it is an hour. A working camera with a wrong
password filed as `no_onvif` reads as "not a camera at all"; a printer filed as
`auth_failed` sends someone hunting for credentials that do not exist. The whole
classification rests on `is_auth_error`, which is substring matching over an
exception message from a vendor's SOAP stack — the loosest input in the product.

netscan's merge decides which hosts get probed at all. Its subtlety is that
WS-Discovery is LAN-wide multicast: it answers from everywhere, not just the
requested range. A scan scoped to one IP that reported every camera on the
network as "found" is the bug the CIDR filter exists for.

Hermetic: no multicast, no sockets, no onvif-zeep.

Run (from services/camera-mgmt): python -m pytest tests -q
"""
from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.services import netscan as ns  # noqa: E402
from backend.services.onvif_probe import is_auth_error  # noqa: E402


# ── The three-way classification ───────────────────────────────────────────

class TestAuthErrorClassification:
    """Real message shapes from vendor SOAP stacks. Each one that is not
    recognised sends a camera with a wrong password into the "not a camera"
    bucket, where nobody looks for it again."""

    @pytest.mark.parametrize("message", [
        "Sender not Authorized",
        "HTTP Error 401: Unauthorized",
        "Unauthorized",
        "ter:NotAuthorized",
        "Authentication failed",
        "auth failed",
        "Bad credentials supplied",
        "Invalid username or password",
        "The username or password is wrong",
    ])
    def test_a_credential_rejection_is_recognised(self, message):
        assert is_auth_error(Exception(message)) is True, (
            f"{message!r} would be filed as 'not a camera' instead of "
            f"'check the password'"
        )

    def test_recognition_is_case_insensitive(self):
        assert is_auth_error(Exception("SENDER NOT AUTHORIZED")) is True

    @pytest.mark.parametrize("message", [
        "Connection refused",
        "timed out",
        "No route to host",
        "Name or service not known",
        "Connection reset by peer",
        "HTTP Error 404: Not Found",
        "HTTP Error 500: Internal Server Error",
        "SSL: CERTIFICATE_VERIFY_FAILED",
    ])
    def test_a_transport_failure_is_not_an_auth_failure(self, message):
        # Filing these as auth_failed sends an installer hunting for a password
        # on a device that has no ONVIF service at all.
        assert is_auth_error(Exception(message)) is False, (
            f"{message!r} would be reported as a credentials problem"
        )

    def test_an_empty_message_is_not_an_auth_failure(self):
        assert is_auth_error(Exception()) is False

    def test_clock_skew_is_not_filed_as_a_credentials_problem(self):
        """"Wsse authorized time check failed" is a CLOCK problem, not a
        password one, and it is deliberately not in the marker list.

        The device rejected the WS-Security timestamp because its clock has
        drifted from the appliance's. Telling an installer to check the
        password would send them after the wrong thing entirely. The product
        handles it a layer up instead — `_probe_sync` passes `adjust_time=True`
        so the timestamp is synced to the device clock and the error does not
        arise. This test exists so that adding "authorized" to the marker list,
        which would catch it, has to be a deliberate decision rather than a
        tidy-up of the substring list.
        """
        assert is_auth_error(Exception("Wsse authorized time check failed")) is False

    def test_the_marker_list_is_matched_as_a_substring_of_the_whole_message(self):
        # zeep wraps the fault text in a longer sentence; the marker has to be
        # found inside it, not equal to it.
        assert is_auth_error(Exception(
            "Fault: ter:NotAuthorized: Sender not Authorized (subcode ter:NotAuthorized)"
        )) is True


# ── netscan: which hosts are even considered ───────────────────────────────

class TestNetworkMembership:
    def test_an_address_inside_the_range_is_included(self):
        net = ipaddress.ip_network("10.0.0.0/24")
        assert ns._in_network("10.0.0.5", net) is True

    def test_an_address_outside_the_range_is_excluded(self):
        net = ipaddress.ip_network("10.0.0.0/24")
        assert ns._in_network("10.0.1.5", net) is False

    def test_a_single_host_range_admits_only_that_host(self):
        # A "Manual IP" add becomes a /32. Anything else answering multicast
        # must not be reported as found by that scan.
        net = ipaddress.ip_network("10.0.0.5/32")
        assert ns._in_network("10.0.0.5", net) is True
        assert ns._in_network("10.0.0.6", net) is False

    @pytest.mark.parametrize("junk", ["", "not-an-ip", "10.0.0.256", "::gg"])
    def test_an_unparseable_address_is_excluded_rather_than_raising(self, junk):
        # One malformed XAddr from one device must not abort a whole scan.
        assert ns._in_network(junk, ipaddress.ip_network("10.0.0.0/24")) is False


# ── netscan: merging the sweep with WS-Discovery ───────────────────────────

@pytest.fixture
def scan(monkeypatch):
    """Drive `discover` with both discovery methods stubbed."""
    monkeypatch.setattr(ns.settings, "discovery_onvif_ports", [80, 8000, 8080])
    monkeypatch.setattr(ns.settings, "discovery_rtsp_port", 554)

    async def run(ws_hosts, sweep, cidr=None, extra=None,
                  local=(), configured=()):
        async def _ws():
            return dict(ws_hosts)

        async def _sweep(c):
            return {ip: set(ports) for ip, ports in sweep.items()}

        monkeypatch.setattr(ns, "ws_discovery", _ws)
        monkeypatch.setattr(ns, "tcp_sweep", _sweep)
        monkeypatch.setattr(ns, "_local_subnets", lambda: list(local))
        monkeypatch.setattr(ns, "_configured_subnets", lambda: list(configured))
        return {c.ip: c for c in await ns.discover(cidr, extra_subnets=extra)}

    return run


class TestDiscoveryMerge:
    @pytest.mark.asyncio
    async def test_a_swept_host_reports_its_open_ports(self, scan):
        out = await scan({}, {"10.0.0.5": [554, 8000]}, cidr="10.0.0.0/24")
        c = out["10.0.0.5"]
        assert c.rtsp_open is True
        assert c.onvif_ports == [8000]
        assert c.open_ports == [554, 8000]

    @pytest.mark.asyncio
    async def test_a_host_with_no_onvif_port_open_is_still_a_candidate(self, scan):
        # RTSP-only cameras exist and are added by URL rather than by ONVIF.
        out = await scan({}, {"10.0.0.5": [554]}, cidr="10.0.0.0/24")
        assert out["10.0.0.5"].rtsp_open is True

    @pytest.mark.asyncio
    async def test_multicast_answers_outside_the_requested_range_are_dropped(self, scan):
        # THE BUG THE FILTER EXISTS FOR. WS-Discovery is LAN-wide, so without
        # this a single-IP probe reports every camera on the network as found.
        out = await scan({"10.0.9.9": 80}, {"10.0.0.5": [554]}, cidr="10.0.0.0/24")
        assert "10.0.9.9" not in out
        assert "10.0.0.5" in out

    @pytest.mark.asyncio
    async def test_a_manual_single_ip_add_reports_only_that_ip(self, scan):
        out = await scan({"10.0.0.6": 80, "10.0.0.5": 80}, {}, cidr="10.0.0.5/32")
        assert set(out) == {"10.0.0.5"}

    @pytest.mark.asyncio
    async def test_a_multicast_only_camera_survives_with_no_swept_port(self, scan):
        # A host that answered a NetworkVideoTransmitter probe IS a camera,
        # even if the sweep found nothing open on it.
        out = await scan({"10.0.0.7": 8000}, {}, cidr="10.0.0.0/24")
        c = out["10.0.0.7"]
        assert c.onvif_ports[0] == 8000
        assert c.open_ports, "a confirmed camera was filtered out for having no open port"

    @pytest.mark.asyncio
    async def test_an_advertised_port_is_tried_first(self, scan):
        # The camera told us where its ONVIF service is; trying that before the
        # guesses is the difference between one connection and three.
        out = await scan({"10.0.0.5": 8080}, {"10.0.0.5": [80, 8080]},
                         cidr="10.0.0.0/24")
        assert out["10.0.0.5"].onvif_ports[0] == 8080

    @pytest.mark.asyncio
    async def test_an_advertised_port_is_not_duplicated(self, scan):
        out = await scan({"10.0.0.5": 8000}, {"10.0.0.5": [8000]},
                         cidr="10.0.0.0/24")
        assert out["10.0.0.5"].onvif_ports.count(8000) == 1

    @pytest.mark.asyncio
    async def test_a_portless_advertisement_falls_back_to_every_configured_port(self, scan):
        # Cameras routinely advertise on 80/443 with no explicit port in
        # XAddrs, which the regex cannot turn into a number.
        out = await scan({"10.0.0.7": None}, {}, cidr="10.0.0.0/24")
        assert out["10.0.0.7"].onvif_ports == [80, 8000, 8080]

    @pytest.mark.asyncio
    async def test_a_filtered_onvif_port_still_gets_probed(self, scan):
        # VMS-27: the sweep can miss a filtered ONVIF port while the camera is
        # perfectly reachable on it. WS-Discovery confirms the device, so fall
        # back to probing every configured port rather than giving up.
        out = await scan({"10.0.0.7": None}, {"10.0.0.7": [554]},
                         cidr="10.0.0.0/24")
        assert out["10.0.0.7"].onvif_ports == [80, 8000, 8080]

    @pytest.mark.asyncio
    async def test_a_host_with_nothing_at_all_is_not_a_candidate(self, scan):
        out = await scan({}, {"10.0.0.8": []}, cidr="10.0.0.0/24")
        assert out == {}


class TestZeroConfigSubnets:
    @pytest.mark.asyncio
    async def test_every_subnet_source_is_swept(self, scan):
        # The union is what makes zero-config work inside a Docker VM whose own
        # interfaces cannot see the camera LAN.
        swept: list = []

        out = await scan({}, {"10.0.0.5": [554]}, cidr=None,
                         local=["192.168.1.0/24"],
                         configured=["172.16.0.0/24"],
                         extra=["10.0.0.0/24"])
        assert "10.0.0.5" in out

    @pytest.mark.asyncio
    async def test_multicast_is_unfiltered_when_no_subnet_can_be_derived(self, scan):
        # Docker Desktop NAT: nothing to sweep, so announce-only multicast is
        # all there is — and filtering it against a range we do not have would
        # discard every result.
        out = await scan({"10.0.9.9": 80}, {}, cidr=None)
        assert "10.0.9.9" in out

    @pytest.mark.asyncio
    async def test_zero_config_does_not_filter_multicast_by_the_swept_subnets(self, scan):
        # A camera on a routed VLAN can answer multicast while sitting outside
        # every subnet the appliance derived. In zero-config there is no
        # requested range, so it is kept.
        out = await scan({"10.0.9.9": 80}, {"192.168.1.7": [554]}, cidr=None,
                         local=["192.168.1.0/24"])
        assert set(out) == {"10.0.9.9", "192.168.1.7"}
