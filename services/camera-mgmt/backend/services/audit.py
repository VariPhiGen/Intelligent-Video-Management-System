"""audit.py — the append-only, tamper-evident audit trail (DPDP Rule 6).

Every auditable action (login, export, config change, retention override, role
grant, …) is appended here as one row whose ``entry_hash`` folds in the previous
row's hash: ``sha256(prev_hash || canonical(payload))``. Recomputing that hash on
read tells us whether a row was altered after the fact — the "Verified /
Mismatch" the Audit log UI shows.

Design choices worth knowing:

  • **Isolation.** ``log_event`` opens its OWN session and transaction. Audit
    writing must never roll back the business operation it records, and a failed
    audit write must never surface as a 500 to the user — so it is caught and
    logged (``audit.write_failed``), never raised.
  • **Chain integrity under concurrency.** The append reads the last row's hash
    then inserts; two concurrent appends could otherwise both chain off the same
    parent. A transaction-scoped advisory lock serialises appenders. Volume is
    low (human actions), so the lock is not a bottleneck.
  • **Determinism.** ``ts`` is stamped here in UTC and passed explicitly, so the
    timestamp that was hashed equals the one stored. ``canonical`` sorts keys, so
    JSONB reordering of ``detail`` on read does not break verification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import Request
from sqlalchemy import select, text

from ..config import settings
from ..db import AsyncSessionLocal
from ..models import AuditLog
from ..redis_client import get_redis
from ..security import Principal
from . import keycloak_admin as kc

log = structlog.get_logger(__name__)

# Any stable 64-bit constant; identifies the advisory lock that serialises
# audit-chain appends. "AUDIT" in ASCII.
_CHAIN_LOCK = 0x4155444954

# How long a login-session id is remembered for dedupe. Longer than any Keycloak
# SSO session, so a still-active session never re-logs a "login" on page reload;
# ``sid`` is unique per login and never reused, so a long TTL only prevents
# duplicates, it never suppresses a genuine new login.
_SESSION_TTL = 7 * 86_400


# ── Hashing ───────────────────────────────────────────────────────────────────

def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev_hash: str, payload_json: str) -> str:
    return hashlib.sha256((prev_hash + payload_json).encode("utf-8")).hexdigest()


def _payload(
    *,
    ts: datetime,
    actor_type: str,
    actor: Optional[str],
    action: str,
    target: Optional[str],
    detail: dict,
    source_ip: Optional[str],
    outcome: str,
) -> dict[str, Any]:
    """The exact field set that gets hashed. Kept in one place so the write path
    and the verify path can never drift."""
    return {
        "ts": ts.isoformat(),
        "actor_type": actor_type,
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail or {},
        "source_ip": source_ip,
        "outcome": outcome,
    }


def entry_hash_for(row: AuditLog) -> str:
    """Recompute what a stored row's ``entry_hash`` SHOULD be from its columns."""
    return _hash(
        row.prev_hash or "",
        _canonical(
            _payload(
                ts=row.ts,
                actor_type=row.actor_type,
                actor=row.actor,
                action=row.action,
                target=row.target,
                detail=row.detail or {},
                source_ip=row.source_ip,
                outcome=row.outcome,
            )
        ),
    )


def is_intact(row: AuditLog) -> bool:
    """True if the row's own contents still hash to its stored ``entry_hash``
    (detects field-level tampering). Chain-linkage across rows is checked
    separately by :func:`verify_chain`."""
    return entry_hash_for(row) == row.entry_hash


# ── Request context helpers ───────────────────────────────────────────────────

def client_ip(request: Request | None) -> Optional[str]:
    """Best-effort caller IP: first X-Forwarded-For hop (behind the reverse
    proxy / tunnel), else the socket peer."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def actor_of(principal: Principal | None) -> tuple[str, Optional[str]]:
    """Map a Principal to (actor_type, actor). ``None`` → an unknown caller."""
    if principal is None:
        return "unknown", None
    if principal.kind == "user":
        return "user", principal.subject
    if principal.kind == "dev":
        return "user", "dev"
    if principal.kind == "service":
        return "service", "internal"
    return "unknown", principal.subject


# ── Append ────────────────────────────────────────────────────────────────────

async def log_event(
    *,
    action: str,
    actor_type: str = "user",
    actor: Optional[str] = None,
    target: Optional[str] = None,
    detail: Optional[dict] = None,
    source_ip: Optional[str] = None,
    outcome: str = "success",
    ts: Optional[datetime] = None,
) -> None:
    """Append one audit row. Best-effort: never raises, never touches the
    caller's DB session. ``ts`` defaults to now; pass it to preserve the real
    time of an event captured after the fact (e.g. a polled Keycloak event)."""
    ts = ts or datetime.now(timezone.utc)
    detail = detail or {}
    payload_json = _canonical(
        _payload(
            ts=ts, actor_type=actor_type, actor=actor, action=action,
            target=target, detail=detail, source_ip=source_ip, outcome=outcome,
        )
    )
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Serialise appenders so prev_hash read + insert is atomic.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CHAIN_LOCK}
                )
                prev_hash = (
                    await session.execute(
                        select(AuditLog.entry_hash).order_by(AuditLog.id.desc()).limit(1)
                    )
                ).scalar_one_or_none() or ""
                session.add(
                    AuditLog(
                        ts=ts,
                        actor_type=actor_type,
                        actor=actor,
                        action=action,
                        target=target,
                        detail=detail,
                        source_ip=source_ip,
                        outcome=outcome,
                        prev_hash=prev_hash or None,
                        entry_hash=_hash(prev_hash, payload_json),
                    )
                )
    except Exception as exc:  # noqa: BLE001 — audit must not break the operation
        log.warning("audit.write_failed", action=action, error=str(exc))


async def record(
    request: Request | None,
    principal: Principal | None,
    action: str,
    *,
    target: Optional[str] = None,
    detail: Optional[dict] = None,
    outcome: str = "success",
) -> None:
    """Convenience wrapper: derive actor + source IP from the request/principal,
    then :func:`log_event`."""
    actor_type, actor = actor_of(principal)
    await log_event(
        action=action,
        actor_type=actor_type,
        actor=actor,
        target=target,
        detail=detail,
        source_ip=client_ip(request),
        outcome=outcome,
    )


async def record_login_once(request: Request | None, principal: Principal | None) -> None:
    """Emit an ``auth.login`` row the first time a user's login session is seen.

    Called from ``/me`` (the SPA fetches it right after Keycloak hands back a
    token). ``/me`` also runs on every page reload and token refresh, so we
    dedupe on the Keycloak session id with a Redis ``SET NX`` — only the first
    request of a session records a login. Note this cannot see FAILED logins or
    lockouts: those never leave Keycloak. Capturing them needs a Keycloak event
    listener posting to ``/api/audit/ingest``.
    """
    if principal is None or principal.kind != "user" or not principal.session_id:
        return
    try:
        r = await get_redis()
        # NX: returns truthy only for the first caller in this session.
        fresh = await r.set(
            f"audit:session:{principal.session_id}", "1", ex=_SESSION_TTL, nx=True
        )
        if not fresh:
            return
    except Exception as exc:  # noqa: BLE001
        # Redis unreachable: prefer recording the login (a possible duplicate on
        # reload) over losing it. Audit completeness beats tidiness.
        log.warning("audit.login_dedupe_failed", error=str(exc))
    await record(request, principal, "auth.login")


# ── Full-chain verification ───────────────────────────────────────────────────

async def verify_chain(session) -> dict[str, Any]:
    """Walk the whole log in append order and check both invariants: every row's
    contents hash to its stored ``entry_hash``, and every row's ``prev_hash``
    equals the previous row's ``entry_hash``. Returns the first break, if any."""
    result = await session.execute(select(AuditLog).order_by(AuditLog.id.asc()))
    rows = result.scalars().all()
    prev = ""
    for row in rows:
        if (row.prev_hash or "") != prev or not is_intact(row):
            return {"ok": False, "count": len(rows), "first_broken_id": row.id}
        prev = row.entry_hash
    return {"ok": True, "count": len(rows), "first_broken_id": None}


# ── Keycloak event capture (failed logins / lockouts / logouts) ───────────────
#
# Authentication happens in Keycloak, so those events never reach our request
# handlers. Instead we poll Keycloak's events API and append the auth ones here.
# LOGIN success is already captured at /me (record_login_once), so we take only
# the events that hook can't see. Correctness under 4 uvicorn workers rests on a
# per-event Redis SET NX (each event ingested once regardless of how many workers
# poll); a persistent high-water mark stops old events being re-ingested.

_KC_EVENT_TYPES = ["LOGIN_ERROR", "LOGOUT"]
_KC_STORE_TYPES = ["LOGIN", "LOGIN_ERROR", "LOGOUT", "LOGOUT_ERROR"]
_KC_HIGHWATER = "audit:kc:high_water"
_KC_SEEN_TTL = 3600  # per-event dedupe window (seconds)

# Keycloak error codes → a short human phrase for the audit "Action" column.
_KC_ERROR_LABEL = {
    "invalid_user_credentials": "invalid credentials",
    "user_not_found": "unknown user",
    "user_temporarily_disabled": "account temporarily locked",
    "user_disabled": "account disabled",
    "account_temporarily_disabled": "account temporarily locked",
    "invalid_token": "invalid token",
    "expired_code": "expired code",
}
_KC_LOCKOUT_ERRORS = {
    "user_temporarily_disabled", "account_temporarily_disabled", "user_disabled",
}


def _kc_event_key(ev: dict) -> str:
    return (
        f"audit:kc:seen:{ev.get('time')}:{ev.get('type')}:"
        f"{ev.get('userId')}:{ev.get('sessionId')}:{ev.get('ipAddress')}"
    )


async def _ensure_kc_events_enabled() -> bool:
    """Turn on login-event storage in the realm if it isn't already. Returns True
    once storage is confirmed enabled, False if Keycloak couldn't be reached or
    updated (so the caller retries — Keycloak often boots AFTER this service, and
    a one-shot attempt at startup would silently leave events off). Idempotent and
    safe for all four workers to call; preserves any admin-event settings."""
    try:
        cfg = await kc.get_events_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("audit.kc_events_config_read_failed", error=str(exc))
        return False
    have = set(cfg.get("enabledEventTypes") or [])
    want = set(_KC_STORE_TYPES)
    if cfg.get("eventsEnabled") and want <= have:
        return True
    cfg["eventsEnabled"] = True
    cfg["eventsListeners"] = sorted({*(cfg.get("eventsListeners") or []), "jboss-logging"})
    cfg["enabledEventTypes"] = sorted(have | want)
    # Bound Keycloak's own event table — the poller drains events within seconds,
    # and our audit_log is the permanent record, so a week of headroom is plenty.
    cfg.setdefault("eventsExpiration", 7 * 86_400)
    try:
        await kc.set_events_config(cfg)
        log.info("audit.kc_events_enabled")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("audit.kc_events_enable_failed", error=str(exc))
        return False


# userId → username, resolved lazily and cached. Keycloak LOGOUT events carry a
# userId but no username in their details, so without this the actor column shows
# a raw UUID instead of who signed out.
_username_cache: dict[str, str] = {}


async def _resolve_username(user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    if user_id in _username_cache:
        return _username_cache[user_id]
    try:
        resp = await kc._req("GET", f"/users/{user_id}")
        if resp.status_code == 200:
            uname = resp.json().get("username")
            if uname:
                _username_cache[user_id] = uname
                return uname
    except Exception as exc:  # noqa: BLE001
        log.warning("audit.username_resolve_failed", user_id=user_id, error=str(exc))
    return user_id  # fall back to the id rather than dropping the actor


async def _ingest_kc_event(ev: dict) -> None:
    etype = ev.get("type")
    details = ev.get("details") or {}
    ts = (
        datetime.fromtimestamp(ev["time"] / 1000, tz=timezone.utc)
        if ev.get("time") else None
    )
    ip = ev.get("ipAddress")
    # Prefer the username Keycloak puts in details (present on LOGIN_ERROR);
    # LOGOUT only carries userId, so resolve it to a username.
    who = details.get("username") or await _resolve_username(ev.get("userId"))
    if etype == "LOGIN_ERROR":
        # Keycloak carries the reason in the top-level `error` field (e.g.
        # "invalid_user_credentials"); some flows also mirror it into details.
        err = ev.get("error") or details.get("error") or "login_failed"
        await log_event(
            action="auth.login_failed", actor_type="unknown", actor=who,
            source_ip=ip, outcome="failure", ts=ts,
            detail={
                "error": _KC_ERROR_LABEL.get(err, err),
                "locked": err in _KC_LOCKOUT_ERRORS,
            },
        )
    elif etype == "LOGOUT":
        await log_event(
            action="auth.logout", actor_type="user", actor=who, source_ip=ip, ts=ts,
        )


async def _poll_kc_events() -> None:
    try:
        events = await kc.get_events(types=_KC_EVENT_TYPES, max=200)
    except Exception as exc:  # noqa: BLE001
        log.warning("audit.kc_poll_failed", error=str(exc))
        return
    r = await get_redis()
    try:
        high = float(await r.get(_KC_HIGHWATER) or 0)
    except Exception:  # noqa: BLE001
        high = 0.0
    # Oldest-first so audit-row order tracks event order; skip anything at or
    # below the high-water mark (already ingested on an earlier poll).
    fresh = sorted((e for e in events if (e.get("time") or 0) > high),
                   key=lambda e: e.get("time") or 0)
    new_high = high
    for ev in fresh:
        try:
            first = await r.set(_kc_event_key(ev), "1", ex=_KC_SEEN_TTL, nx=True)
        except Exception:  # noqa: BLE001
            first = True  # Redis down: prefer a possible dup over a dropped event
        if first:
            await _ingest_kc_event(ev)
        new_high = max(new_high, ev.get("time") or 0)
    if new_high > high:
        try:
            await r.set(_KC_HIGHWATER, str(new_high))
        except Exception:  # noqa: BLE001
            pass


async def keycloak_events_loop() -> None:
    """Background task: ensure event storage is on (retrying until it is, since
    Keycloak may still be booting), then poll on an interval."""
    interval = max(10, settings.audit_kc_poll_interval)
    events_enabled = False
    while True:
        try:
            # Keep trying to enable until confirmed — a one-shot attempt loses the
            # race when Keycloak starts after us (observed in deployment).
            if not events_enabled:
                events_enabled = await _ensure_kc_events_enabled()
            await _poll_kc_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("audit.kc_loop_error", error=str(exc))
        await asyncio.sleep(interval)
