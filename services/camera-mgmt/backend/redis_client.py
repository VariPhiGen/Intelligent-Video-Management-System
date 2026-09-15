from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as aioredis

from .config import settings

_redis: Optional[aioredis.Redis] = None

# ── TTL constants ─────────────────────────────────────────────────────────────
HEALTH_TTL_SEC = 120          # stream health expires if not refreshed within 2 min
RECONNECT_TTL_SEC = 86_400    # reconnect counter lives 24 h then auto-resets
SNAPSHOT_LOCK_TTL_SEC = 30    # single-flight guard; only a crashed worker should let it expire
SNAPSHOT_CACHE_TTL_SEC = 10   # a decoded frame is reusable by every consumer for this long


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# ── Stream health ─────────────────────────────────────────────────────────────

async def set_stream_health(slug: str, status: str, details: dict[str, Any]) -> None:
    r = await get_redis()
    payload: dict[str, Any] = {
        "status": status,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        **details,
    }
    await r.setex(f"stream:health:{slug}", HEALTH_TTL_SEC, json.dumps(payload))


async def get_stream_health(slug: str) -> Optional[dict[str, Any]]:
    r = await get_redis()
    raw = await r.get(f"stream:health:{slug}")
    return json.loads(raw) if raw else None


# ── Reconnect counters ────────────────────────────────────────────────────────

async def inc_reconnect_count(slug: str) -> int:
    r = await get_redis()
    key = f"stream:reconnect:{slug}"
    count = await r.incr(key)
    await r.expire(key, RECONNECT_TTL_SEC)
    return int(count)


async def get_reconnect_count(slug: str) -> int:
    r = await get_redis()
    val = await r.get(f"stream:reconnect:{slug}")
    return int(val) if val else 0


async def reset_reconnect_count(slug: str) -> None:
    r = await get_redis()
    await r.delete(f"stream:reconnect:{slug}")


# ── Backoff state ─────────────────────────────────────────────────────────────

async def set_backoff(slug: str, next_retry_at: datetime) -> None:
    r = await get_redis()
    ttl = max(1, int((next_retry_at - datetime.now(tz=timezone.utc)).total_seconds()) + 120)
    await r.setex(f"stream:backoff:{slug}", ttl, next_retry_at.isoformat())


async def get_backoff(slug: str) -> Optional[datetime]:
    r = await get_redis()
    val = await r.get(f"stream:backoff:{slug}")
    if val is None:
        return None
    return datetime.fromisoformat(val)


async def clear_backoff(slug: str) -> None:
    r = await get_redis()
    await r.delete(f"stream:backoff:{slug}")


# ── Re-stamp flag (broken source timestamps) ─────────────────────────────────
#
# Some recorders emit a backward jump in the RTP timestamp on a fixed cycle —
# the appliance's :5551 Dahua unit wraps a 32-bit *microsecond* counter every
# 2**32 us (71 m 35 s), which MediaMTX's H265 DTS extractor reads as a seek
# backwards and answers by tearing down the HLS muxer. Affected cameras get an
# FFmpeg re-stamp hop in front of them (see relay._path_config).
#
# The flag lives in Redis so every uvicorn worker builds the same path config,
# and is mirrored into cameras.camera_metadata so it survives a restart and is
# visible to an operator. No TTL: the recorder's firmware does not heal on its
# own, and an expiring flag would flap the camera through a re-register every
# time it lapsed.

async def set_restamp(slug: str, enabled: bool) -> None:
    r = await get_redis()
    key = f"stream:restamp:{slug}"
    if enabled:
        await r.set(key, "1")
    else:
        await r.delete(key)


async def get_restamp(slug: str) -> bool:
    r = await get_redis()
    return await r.exists(f"stream:restamp:{slug}") == 1


async def inc_hls_fail_count(slug: str) -> int:
    """Consecutive polls where the RTSP path was ready but HLS would not serve."""
    r = await get_redis()
    key = f"stream:hls_fail:{slug}"
    count = await r.incr(key)
    # Long enough to span several poll intervals, short enough that an isolated
    # blip ages out instead of accumulating toward the threshold over hours.
    await r.expire(key, 900)
    return count


async def reset_hls_fail_count(slug: str) -> None:
    r = await get_redis()
    await r.delete(f"stream:hls_fail:{slug}")


# ── Snapshot single-flight + cache ────────────────────────────────────────────
#
# One FFmpeg decode per camera at a time (the lock), and its JPEG shared with
# everyone who asks in the next few seconds (the cache). Several parts of the UI
# want the same frame at once — the config card thumbnail, then the privacy /
# analytics editor the moment the operator opens a tab — and before the cache
# existed the second one was rejected outright and drew on a blank frame.

async def acquire_snapshot_lock(slug: str) -> bool:
    """Returns True if the caller owns the decode; False if one is already running.

    Losers should wait on `get_cached_snapshot` rather than fail — the winner
    publishes the frame for them. Always pair with `release_snapshot_lock`.
    """
    r = await get_redis()
    key = f"stream:snapshot_lock:{slug}"
    result = await r.set(key, "1", ex=SNAPSHOT_LOCK_TTL_SEC, nx=True)
    return result is not None


async def release_snapshot_lock(slug: str) -> None:
    r = await get_redis()
    await r.delete(f"stream:snapshot_lock:{slug}")


async def get_cached_snapshot(slug: str) -> Optional[bytes]:
    r = await get_redis()
    raw = await r.get(f"stream:snapshot_jpeg:{slug}")
    if not raw:
        return None
    try:
        # The shared client is decode_responses=True, so JPEG bytes ride as b64.
        return base64.b64decode(raw)
    except (ValueError, TypeError):
        return None


async def set_cached_snapshot(slug: str, jpeg: bytes) -> None:
    r = await get_redis()
    await r.set(
        f"stream:snapshot_jpeg:{slug}",
        base64.b64encode(jpeg).decode("ascii"),
        ex=SNAPSHOT_CACHE_TTL_SEC,
    )


# ── Bulk cleanup ──────────────────────────────────────────────────────────────

async def delete_stream_keys(slug: str) -> None:
    r = await get_redis()
    keys = [
        f"stream:health:{slug}",
        f"stream:reconnect:{slug}",
        f"stream:backoff:{slug}",
        f"stream:snapshot_lock:{slug}",
        f"stream:snapshot_jpeg:{slug}",
        f"stream:restamp:{slug}",
        f"stream:hls_fail:{slug}",
    ]
    if keys:
        await r.delete(*keys)
