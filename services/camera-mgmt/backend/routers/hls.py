"""hls.py — unauthenticated streaming reverse-proxy to MediaMTX's HLS server.

Live preview is low-latency HLS served by MediaMTX (deploy/mediamtx.yml,
hlsAddress :8988). On the LAN the SPA hits that port directly over HTTP; but
over HTTPS — and crucially through a single-port front door (a Cloudflare
tunnel forwards only 443) — the SPA requests the SAME-ORIGIN path
``/hls/<slug>/index.m3u8`` (see frontend-react/src/lib/api.ts hlsSrc). Caddy's
TLS profile routes that to MediaMTX, but when the tunnel points straight at this
api (the documented single app port), nothing served ``/hls/*`` and every
preview 404'd. This router closes that gap so the tunnel needs no extra hop.

Design notes:
  • Unauthenticated, matching the existing HLS design — Caddy proxies /hls/*
    with no auth and MediaMTX itself is open (hlsAllowOrigin "*"). The player
    sets a plain <video>/hls.js src with no token, so auth here would break it.
    The streams are live-preview only; recorded footage stays behind /api/nvr.
  • STREAMED, not buffered. Low-latency HLS relies on blocking playlist reloads
    (CAN-BLOCK-RELOAD=YES; the player long-polls with _HLS_msn / _HLS_part query
    params and MediaMTX holds the response until that part exists) — so the
    query string is forwarded verbatim and the body is streamed as it arrives.
  • Range is forwarded for segment/part byte-range requests some players issue.
"""
from __future__ import annotations

from typing import AsyncIterator

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..config import settings

log = structlog.get_logger(__name__)
router = APIRouter(tags=["hls"])

# Generous read timeout: a blocking LL-HLS playlist reload is held open by
# MediaMTX until the requested part is ready. connect stays short — MediaMTX is
# loopback and either up or not.
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Request headers worth forwarding upstream; everything else (host, cookies,
# auth, hop-by-hop) is dropped.
_KEEP_REQ = {"range", "accept", "accept-encoding", "if-none-match", "if-modified-since"}
# Response headers the player needs; content-length is intentionally dropped so
# a streamed (chunked) body can't mismatch a declared length.
_KEEP_RESP = {
    "content-type", "cache-control", "accept-ranges", "content-range",
    "etag", "last-modified", "content-encoding",
}


@router.api_route("/{path:path}", methods=["GET", "HEAD"])
async def proxy(path: str, request: Request) -> StreamingResponse:
    """Proxy /hls/<path> → <MEDIAMTX_HLS_URL>/<path>, streaming the response and
    forwarding the query string (LL-HLS blocking-reload params) verbatim."""
    url = f"{settings.mediamtx_hls_url.rstrip('/')}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() in _KEEP_REQ}

    client = httpx.AsyncClient(timeout=_TIMEOUT)
    req = client.build_request(
        request.method, url, params=request.query_params, headers=headers
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        log.warning("hls.proxy.unreachable", path=path, error=str(exc))
        return StreamingResponse(
            iter((b'{"detail":"Live preview relay is unreachable"}',)),
            status_code=502,
            media_type="application/json",
        )

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() in _KEEP_RESP
    }
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
