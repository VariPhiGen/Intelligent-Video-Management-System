"""onvif_wsdl_transport.py — resolve the vendored ONVIF schema bundle offline.

WHY THIS EXISTS
---------------
zeep builds a SOAP client by fetching the WSDL and every schema it imports.
Left alone it does that over the network, at probe time, against onvif.org,
w3.org and oasis-open.org. That is unacceptable here for three reasons: camera
onboarding must work on an air-gapped LAN; a probe must not block on someone
else's CDN; and the fetched bytes would be unpinned. So the twelve files those
imports resolve to are vendored under ``backend/onvif_wsdl/``.

The obvious way to make a vendored bundle self-contained is to rewrite every
``schemaLocation`` in it to a local filename. That is what this repository did
until 2026-08-28, and it worked — but it meant the twelve files we redistribute
were *modified* copies of documents from three rights holders (ONVIF, OASIS,
W3C) whose licences are notice-and-disclaimer redistribution grants written for
specification texts, not software. Modification put us in the weakest possible
position on the least necessary point.

So the files are now **byte-identical to upstream** — verified by fetching all
twelve and diffing — and the localisation moved here, into code we own. The
schemas are redistributed exactly as published; only our loader is ours.

HOW
---
zeep resolves every import through ``Transport.load()``. lxml's own resolver and
XML Catalogs cannot intercept it, because zeep never hands the URL to lxml — it
fetches the bytes itself and parses them. Subclassing the transport is therefore
not merely one option among several; it is the only layer where this can be done
at all. It is also zeep's documented extension point.

Two kinds of reference have to be caught:

  * absolute — ``http://docs.oasis-open.org/wsn/b-2.xsd`` and friends, nine of
    them, written that way in the upstream schemas;
  * relative — upstream ``media.wsdl`` imports ``../../../ver10/schema/onvif.xsd``,
    which is correct when served from onvif.org's directory layout and resolves
    to nonsense (``/ver10/schema/onvif.xsd``) when the WSDL is loaded from a
    local path. zeep absolutises it before calling ``load()``, so by the time we
    see it the host is gone and only the path tail identifies it.

Matching on the URL *path* handles both in one rule, and every path below is
unique across the three hosts. Anything unmapped falls through to the real
network transport with a warning rather than an exception: an unrecognised
import is a bundle that has drifted, which should be visible and fixed, not a
camera that stops onboarding.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import structlog

log = structlog.get_logger(__name__)

# Directory holding the vendored bundle. Derived from the WSDL path the rest of
# the code already uses, so the image (which puts it at /app/backend/onvif_wsdl)
# and a bare source checkout both land on the right place.
_BUNDLE_DIR = Path(
    os.environ.get("ONVIF_WSDL_DIR")
    or os.path.dirname(os.environ.get("ONVIF_MEDIA2_WSDL", ""))
    or (Path(__file__).resolve().parent.parent / "onvif_wsdl")
)

# URL path → filename in the bundle. Keyed by path alone: see the module
# docstring for why that is what catches the relative ONVIF imports too.
_BY_PATH: dict[str, str] = {
    # ONVIF — referenced relatively by media.wsdl and by onvif.xsd
    "/ver10/schema/onvif.xsd": "onvif.xsd",
    "/ver10/schema/common.xsd": "common.xsd",
    # OASIS WS-Notification / WS-Resource
    "/wsn/b-2.xsd": "docs.oasis-open.org_wsn_b-2.xsd",
    "/wsn/t-1.xsd": "docs.oasis-open.org_wsn_t-1.xsd",
    "/wsrf/bf-2.xsd": "docs.oasis-open.org_wsrf_bf-2.xsd",
    # W3C
    "/2001/xml.xsd": "www.w3.org_2001_xml.xsd",
    "/2009/01/xml.xsd": "www.w3.org_2009_01_xml.xsd",
    "/2005/08/addressing/ws-addr.xsd": "www.w3.org_2005_08_addressing_ws-addr.xsd",
    "/2003/05/soap-envelope": "www.w3.org_2003_05_soap-envelope.xsd",
    "/2004/08/xop/include": "www.w3.org_2004_08_xop_include.xsd",
    "/2005/05/xmlmime": "www.w3.org_2005_05_xmlmime.xsd",
}


def _local_path(url: str) -> Path | None:
    """Return the vendored file backing ``url``, or None if we don't ship it."""
    if not url:
        return None
    path = urlparse(url).path or url
    name = _BY_PATH.get(path)
    if name is None:
        return None
    candidate = _BUNDLE_DIR / name
    return candidate if candidate.is_file() else None


def install(transport):
    """Make ``transport`` serve the vendored bundle from disk.

    Patches the instance rather than subclassing at import time, so this module
    stays importable without zeep present (the probe path imports zeep lazily,
    and the API must load on a host where the ONVIF extras are absent).
    """
    original_load = transport.load

    def load(url):
        local = _local_path(url)
        if local is not None:
            return local.read_bytes()
        if str(url).startswith(("http://", "https://")):
            log.warning(
                "onvif.wsdl.remote_fetch",
                url=url,
                hint="not in the vendored bundle — onboarding now depends on the network",
            )
        return original_load(url)

    transport.load = load
    return transport
