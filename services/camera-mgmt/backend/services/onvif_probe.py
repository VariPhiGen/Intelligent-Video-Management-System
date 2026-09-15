"""onvif_probe.py — interrogate a single host over ONVIF.

Ports tried are only those the TCP sweep already found open, so connections are
fast and we never hang on filtered ports.  Classifies the outcome:

  ok          → returns device info + credential-less RTSP URIs (per profile)
  auth_failed → ONVIF answered but rejected the credentials (the "wrong password" list)
  no_onvif    → reachable but no usable ONVIF media service

onvif-zeep is synchronous (zeep/requests), so each probe runs in a worker thread.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog

from ..config import settings

log = structlog.get_logger(__name__)

_AUTH_MARKERS = (
    "not authorized",
    "notauthorized",
    "unauthorized",
    "401",
    "authentication",
    "auth failed",
    "sender not authorized",
    "bad credentials",
    "invalid username",
    "password",
)


def is_auth_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(marker in s for marker in _AUTH_MARKERS)


def insecure_transport():
    """Build a zeep Transport that skips TLS certificate verification.

    LAN cameras routinely serve their HTTPS ONVIF endpoints (e.g. media_service
    on :443) with self-signed certs. There's no PKI to validate them against, so
    verification always fails — disable it for the probe.
    """
    import os
    import tempfile

    import requests
    import urllib3
    from zeep.cache import SqliteCache
    from zeep.transports import Transport

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.verify = False
    # zeep's SqliteCache() defaults to $HOME/.cache/zeep, but the container runs
    # as a non-root user with no home dir (useradd --no-create-home), so that
    # path raises PermissionError and every probe would be misclassified as
    # no_onvif. Pin the cache to a writable temp dir instead.
    cache_path = os.path.join(tempfile.gettempdir(), "zeep-onvif-cache.db")
    # Hard per-request timeouts: zeep's default operation_timeout is None
    # (wait forever), so one hanging port — a slow web UI on an ONVIF-candidate
    # port, or Docker Desktop's NAT swallowing refusals on macOS — could
    # otherwise eat the whole probe budget ("ONVIF probe timed out").
    # Serve the vendored ONVIF/OASIS/W3C schemas from disk instead of fetching
    # them from onvif.org, w3.org and oasis-open.org at probe time. See
    # onvif_wsdl_transport for why this is done in the transport rather than by
    # rewriting the schemas themselves.
    from . import onvif_wsdl_transport

    return onvif_wsdl_transport.install(
        Transport(
            session=session, cache=SqliteCache(path=cache_path), timeout=8, operation_timeout=8
        )
    )


def _probe_sync(ip: str, ports: list[int], user: str, password: str) -> dict[str, Any]:
    from onvif import ONVIFCamera  # imported lazily so the module loads without onvif

    transport = insecure_transport()
    last_err: Optional[Exception] = None
    auth_seen = False

    for port in ports:
        try:
            # adjust_time: sync the WS-Security timestamp to the device clock
            #   (fixes "Wsse authorized time check failed" on clock-skewed cams).
            # transport: skip TLS verification for self-signed HTTPS ONVIF
            #   endpoints (e.g. media_service on :443), common on LAN cameras.
            cam = ONVIFCamera(
                ip, port, user, password, adjust_time=True, transport=transport
            )
            dev = cam.create_devicemgmt_service()
            info = dev.GetDeviceInformation()
            media = cam.create_media_service()

            profiles: list[dict[str, Any]] = []
            for profile in media.GetProfiles():
                try:
                    resp = media.GetStreamUri(
                        {
                            "StreamSetup": {
                                "Stream": "RTP-Unicast",
                                "Transport": {"Protocol": "RTSP"},
                            },
                            "ProfileToken": profile.token,
                        }
                    )
                    profiles.append(
                        {
                            "profile": getattr(profile, "Name", profile.token),
                            "token": profile.token,
                            "url_raw": resp.Uri,
                            "verified": None,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 — skip a bad profile, keep others
                    log.debug("onvif.profile.skip", ip=ip, error=str(exc))

            if not profiles:
                last_err = RuntimeError("ONVIF ok but no RTSP profiles returned")
                continue

            return {
                "status": "ok",
                "onvif_port": port,
                "vendor": getattr(info, "Manufacturer", None),
                "model": getattr(info, "Model", None),
                "firmware": getattr(info, "FirmwareVersion", None),
                "profiles": profiles,
            }
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if is_auth_error(exc):
                auth_seen = True
                # Auth failure is conclusive for this port; report it.
                return {"status": "auth_failed", "onvif_port": port, "error": str(exc)}
            continue

    if auth_seen:
        return {"status": "auth_failed", "error": str(last_err) if last_err else "auth failed"}
    return {"status": "no_onvif", "error": str(last_err) if last_err else "no ONVIF service"}


async def probe(ip: str, ports: list[int], user: str, password: str) -> dict[str, Any]:
    if not ports:
        return {"status": "no_onvif", "error": "no ONVIF ports open"}
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_sync, ip, ports, user, password),
            timeout=settings.discovery_onvif_timeout,
        )
    except asyncio.TimeoutError:
        return {"status": "no_onvif", "error": "ONVIF probe timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_onvif", "error": str(exc)}
