"""netscan.py — find candidate camera hosts on the LAN.

Two complementary methods, merged by IP:
  1. WS-Discovery — UDP multicast Probes to 239.255.255.250:3702; ONVIF devices
     reply with ProbeMatch messages whose XAddrs reveal IP + ONVIF port. Sent a
     few times (multicast is lossy) out of every interface (multi-NIC boxes).
  2. TCP sweep — an async connect scan of every host in a CIDR across RTSP (554)
     and the common ONVIF ports.  Catches cameras that don't answer WS-Discovery.
     The CIDR is either caller-supplied or, in zero-config mode, derived from the
     appliance's own interface subnets (_local_subnets) — so an auto scan sweeps
     the LAN it sits on instead of relying on multicast the camera may not send.

No third-party scanner needed; runs inside the host-networked container which sits
on the camera subnet.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import struct
import time
import uuid
from typing import Optional

import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_WS_MULTICAST = "239.255.255.250"
_WS_PORT = 3702

_PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header>"
    "<w:MessageID>uuid:{msgid}</w:MessageID>"
    '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
    '<w:Action e:mustUnderstand="true">'
    "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
    "</e:Header>"
    "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
    "</e:Envelope>"
)

_XADDR_RE = re.compile(rb"https?://([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?::(\d+))?")
_SIOCGIFADDR = 0x8915     # Linux ioctl: get an interface's IPv4 address
_SIOCGIFNETMASK = 0x891B  # Linux ioctl: get an interface's IPv4 netmask

# Never auto-derive a sweep from these interfaces: loopback and Docker's own
# bridges/veths, whose 172.x/16 nets are container plumbing, not camera LANs.
_SKIP_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap")
# A zero-config sweep is bounded to a /24 (254 hosts). A real interface mask can
# be a /16 (65k hosts) — sweeping that on every scan would take minutes, so we
# clamp to the /24 around the host address instead.
_MAX_SWEEP_PREFIX = 24


def _iface_ipv4(name: str, code: int) -> Optional[str]:
    """One read-only IPv4 ioctl (SIOCGIFADDR / SIOCGIFNETMASK) on `name`, as a
    dotted-quad string. No root, no third-party dependency. None on any failure
    (interface down, v6-only, non-Linux)."""
    try:
        import fcntl  # Linux only
    except ImportError:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(s.fileno(), code, struct.pack("256s", name[:15].encode()))
        return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None
    finally:
        s.close()


def _local_ipv4s() -> list[str]:
    """Non-loopback IPv4 address of every local interface, so a WS-Discovery
    probe can go out each NIC (an appliance may have a separate camera VLAN).

    Returns [] on non-Linux or any failure, and the caller then falls back to a
    single send on the OS default-route interface (the original behaviour).
    """
    try:
        ips: list[str] = []
        for _idx, name in socket.if_nameindex():
            if name == "lo":
                continue
            ip = _iface_ipv4(name, _SIOCGIFADDR)
            if ip and not ip.startswith("127."):
                ips.append(ip)
        return ips
    except Exception:  # noqa: BLE001 — enumeration is best-effort
        return []


def _local_subnets() -> list[str]:
    """Private IPv4 subnet (CIDR) behind each real local interface, so a
    zero-config scan can sweep the network the appliance is actually on rather
    than a hardcoded default. Docker bridges (by interface name) and non-private
    addresses are skipped; anything larger than a /24 is clamped to the /24
    around the host address so an auto scan never fans across tens of thousands
    of hosts. [] on non-Linux / failure — the caller then keeps the
    WS-Discovery-only behaviour.
    """
    try:
        nets: list[str] = []
        seen: set[str] = set()
        for _idx, name in socket.if_nameindex():
            if name.startswith(_SKIP_IFACE_PREFIXES):
                continue
            ip = _iface_ipv4(name, _SIOCGIFADDR)
            mask = _iface_ipv4(name, _SIOCGIFNETMASK)
            if not ip or not mask or ip.startswith("127."):
                continue
            try:
                if not ipaddress.ip_address(ip).is_private:
                    continue
                net = ipaddress.ip_interface(f"{ip}/{mask}").network
            except ValueError:
                continue
            if net.prefixlen < _MAX_SWEEP_PREFIX:
                net = ipaddress.ip_network(f"{ip}/{_MAX_SWEEP_PREFIX}", strict=False)
            cidr = str(net)
            if cidr not in seen:
                seen.add(cidr)
                nets.append(cidr)
        return nets
    except Exception:  # noqa: BLE001 — enumeration is best-effort
        return []


def _configured_subnets() -> list[str]:
    """Operator-pinned zero-config sweep ranges (DISCOVERY_SUBNETS), for
    setups where interface derivation can't see the camera LAN — a macOS
    Docker Desktop / OrbStack VM sits on a virtual subnet, so deriving from
    the container's interfaces sweeps the wrong network. Invalid entries are
    skipped with a warning; [] when unset."""
    nets: list[str] = []
    for part in settings.discovery_subnets.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            log.warning("netscan.bad_configured_subnet", cidr=part)
            continue
        nets.append(part)
    return nets


def _ws_discovery_sync(timeout: float, probes: int) -> dict[str, Optional[int]]:
    """Blocking WS-Discovery. Sends `probes` multicast Probes (fresh MessageID
    each, so cameras that dedupe by ID still re-answer) out of EVERY interface,
    then collects ProbeMatch replies until the overall deadline. Multicast is
    lossy and single-NIC/single-shot misses cameras — this covers both.

    Returns {ip: onvif_port|None}.
    """
    found: dict[str, Optional[int]] = {}
    ifaces = _local_ipv4s()  # [] → default-route interface
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
        deadline = time.monotonic() + timeout

        def _emit() -> None:
            msg = _PROBE_TEMPLATE.format(msgid=uuid.uuid4()).encode("utf-8")
            if ifaces:
                for ip in ifaces:
                    try:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                        sock.sendto(msg, (_WS_MULTICAST, _WS_PORT))
                    except OSError:
                        pass  # one bad interface must not stop the others
            else:
                try:
                    sock.sendto(msg, (_WS_MULTICAST, _WS_PORT))
                except OSError:
                    pass

        # Send the probe rounds up front, spaced but well inside the deadline;
        # replies buffer in the socket while we send, then we drain them.
        for i in range(max(1, probes)):
            _emit()
            if i < probes - 1:
                time.sleep(min(0.35, max(0.0, deadline - time.monotonic())))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            sender_ip = addr[0]
            m = _XADDR_RE.search(data)
            if m:
                ip = m.group(1).decode()
                port = int(m.group(2)) if m.group(2) else None
                # Don't let a later port-less reply clobber a port we already have.
                if ip not in found or (port and not found[ip]):
                    found[ip] = port
            else:
                found.setdefault(sender_ip, None)
    except Exception as exc:  # noqa: BLE001
        log.warning("netscan.wsdiscovery.error", error=str(exc))
    finally:
        sock.close()
    if ifaces:
        log.debug("netscan.wsdiscovery.interfaces", count=len(ifaces))
    return found


async def ws_discovery() -> dict[str, Optional[int]]:
    return await asyncio.to_thread(
        _ws_discovery_sync,
        settings.discovery_wsdiscovery_timeout,
        settings.discovery_wsdiscovery_probes,
    )


async def _probe_port(ip: str, port: int, sem: asyncio.Semaphore, results: dict[str, set]) -> None:
    async with sem:
        try:
            fut = asyncio.open_connection(ip, port)
            reader, writer = await asyncio.wait_for(fut, timeout=settings.discovery_tcp_timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            results.setdefault(ip, set()).add(port)
        except Exception:
            pass


async def tcp_sweep(cidrs: str | list[str]) -> dict[str, set]:
    """Connect-scan every host in one or more CIDRs across RTSP + ONVIF ports.

    Multiple CIDRs (zero-config mode derives one per real interface) share a
    SINGLE concurrency budget — one semaphore across all of them — so an
    appliance with several camera VLANs can't multiply the fan-out.
    """
    if isinstance(cidrs, str):
        cidrs = [cidrs]
    ports = sorted({settings.discovery_rtsp_port, *settings.discovery_onvif_ports})
    sem = asyncio.Semaphore(settings.discovery_scan_concurrency)
    results: dict[str, set] = {}
    # Hosts deduped across CIDRs — zero-config mode can merge overlapping
    # ranges (interface-derived + registry-learned), and each host must be
    # probed once.
    hosts = {
        str(host)
        for cidr in cidrs
        for host in ipaddress.ip_network(cidr, strict=False).hosts()
    }
    tasks = [
        _probe_port(host, port, sem, results)
        for host in hosts
        for port in ports
    ]
    await asyncio.gather(*tasks)
    return results


def _in_network(ip: str, net) -> bool:
    """True if `ip` falls inside `net`. Unparseable addresses are excluded."""
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


class Candidate:
    __slots__ = ("ip", "onvif_ports", "rtsp_open", "open_ports")

    def __init__(self, ip: str):
        self.ip = ip
        self.onvif_ports: list[int] = []
        self.rtsp_open: bool = False
        self.open_ports: list[int] = []


async def discover(cidr: Optional[str],
                   extra_subnets: Optional[list[str]] = None) -> list[Candidate]:
    """Discover candidate camera hosts.

    With a CIDR: WS-Discovery + a TCP sweep of that range (catches cameras that
    don't answer WS-Discovery). Without one — zero-config mode — WS-Discovery
    PLUS a TCP sweep of the union of every subnet source available:
      • the subnet(s) derived from the appliance's own interfaces
        (_local_subnets — the Linux appliance turnkey path)
      • ``extra_subnets`` the caller learned elsewhere (the scan manager passes
        the /24s of every camera already in the registry, so once ANY camera
        has ever been found, auto scans cover its network — this is what makes
        zero-config work inside macOS Docker/OrbStack VMs, whose interfaces
        can't see the camera LAN)
      • DISCOVERY_SUBNETS, an operator escape hatch for routed camera VLANs
        no other source can see
    Most cameras ship with WS-Discovery off, so multicast alone finds only the
    few that announce themselves; the sweep is what lets a genuinely new camera
    show up without the operator having to type an IP range. Falls back to
    WS-Discovery-only when every source is empty.
    """
    ws_task = asyncio.create_task(ws_discovery())
    if cidr:
        sweep_task = asyncio.create_task(tcp_sweep(cidr))
        ws_hosts, sweep = await asyncio.gather(ws_task, sweep_task)
        # WS-Discovery is a LAN-wide multicast — it answers from everywhere, not
        # just the requested range. When the caller scoped the scan to a CIDR
        # (including the /32 a "Manual IP" add turns into), anything outside it
        # is not a result of *this* scan and must be dropped, or the UI reports
        # every camera on the network as "found" for a single-IP probe.
        net = ipaddress.ip_network(cidr, strict=False)
        ws_hosts = {
            ip: port for ip, port in ws_hosts.items()
            if _in_network(ip, net)
        }
    else:
        subnets = sorted({*_configured_subnets(), *_local_subnets(),
                          *(extra_subnets or [])})
        if subnets:
            log.info("netscan.autosubnet", subnets=subnets)
            sweep_task = asyncio.create_task(tcp_sweep(subnets))
            ws_hosts, sweep = await asyncio.gather(ws_task, sweep_task)
        else:
            # No derivable subnet (non-Linux / Docker Desktop NAT): the original
            # zero-config behaviour — announce-only multicast, unfiltered.
            ws_hosts = await ws_task
            sweep = {}

    onvif_set = set(settings.discovery_onvif_ports)
    onvif_fallback = list(settings.discovery_onvif_ports)
    candidates: dict[str, Candidate] = {}

    for ip, ports in sweep.items():
        c = candidates.setdefault(ip, Candidate(ip))
        c.open_ports = sorted(ports)
        c.rtsp_open = settings.discovery_rtsp_port in ports
        c.onvif_ports = sorted(p for p in ports if p in onvif_set)

    # Fold in WS-Discovery. A host that answered a NetworkVideoTransmitter Probe
    # is a confirmed ONVIF device, so it must survive even when there's no sweep
    # (zero-config mode) and no explicit port in its XAddrs — cameras routinely
    # advertise on the default 80/443, which the regex can't turn into a number.
    for ip, port in ws_hosts.items():
        c = candidates.setdefault(ip, Candidate(ip))
        if port:  # explicit :port in XAddrs → try it first
            if port in c.onvif_ports:
                c.onvif_ports.remove(port)
            c.onvif_ports.insert(0, port)
            if port not in c.open_ports:
                c.open_ports = sorted({*c.open_ports, port})
        if not c.onvif_ports:
            # Port-less XAddr, or a sweep that missed a filtered ONVIF port
            # (VMS-27): fall back to probing every configured ONVIF port — the
            # same fallback probe_device()/_onvif_ports_for() already rely on.
            c.onvif_ports = list(onvif_fallback)
        if not c.open_ports:
            # No TCP-verified port, but WS-Discovery confirms it's a camera —
            # keep it past the filter below with its ONVIF ports as candidates.
            c.open_ports = list(c.onvif_ports)

    result = [c for c in candidates.values() if c.open_ports]
    log.info(
        "netscan.discover.done",
        cidr=cidr,
        ws_count=len(ws_hosts),
        sweep_count=len(sweep),
        candidates=len(result),
    )
    return result
