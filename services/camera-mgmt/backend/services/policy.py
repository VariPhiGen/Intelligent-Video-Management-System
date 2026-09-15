"""policy.py — editable per-role permission policy (the Roles & permissions matrix).

The ROLES are Keycloak realm roles (admin / supervisor / operator / viewer /
dpo) — identity stays in Keycloak. What this module adds is the POLICY layer:
which product capabilities each role may use, editable by admins from the UI
and enforced server-side on the endpoints that can honor it.

Semantics:
  • admin (and service/dev principals) always have every capability — the
    matrix cannot lock out administration.
  • every other role gets the stored policy, falling back to the defaults
    below (which mirror the product's historical behavior for the roles that
    predate the matrix).
  • Capabilities marked ui_only are gated in the frontend only (e.g. live
    view: the HLS fan-out is MediaMTX-side and unauthenticated today, so a
    server-side deny would be a false promise — the metadata says so).
"""
from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import select

from ..security import Principal, get_principal

log = structlog.get_logger(__name__)

# Capability catalogue: id → metadata (labels used by the matrix UI).
CAPABILITIES: dict[str, dict[str, Any]] = {
    "live_view": {
        "label": "Live view",
        "hint": "Watch live streams (grid, expanded, wall)",
        "ui_only": True,   # HLS is served by MediaMTX without auth today
    },
    "playback_search": {
        "label": "Playback & search",
        "hint": "Recorded footage: timelines, clips, snapshots",
        "ui_only": False,
    },
    "export_reports": {
        "label": "Evidence & report export",
        "hint": "Excel uptime reports and NVR recording reports",
        "ui_only": False,
    },
    "motion_ack": {
        "label": "Acknowledge / reset motion alarms",
        "hint": "Re-arm a TRIGGERED camera before its cooldown",
        "ui_only": False,
    },
    # camera_manage is the write half of camera_view: onboarding and per-camera
    # configuration. Enforced server-side (not ui_only) so granting it actually
    # opens the cameras CRUD + discovery routes, not just the buttons. Deleting a
    # camera stays admin-only — it can purge recorded footage — as does the
    # global detection-type vocabulary.
    "camera_manage": {
        "label": "Add & manage cameras",
        "hint": "Onboard cameras and edit camera configuration — masks, zones, schedule, device settings. Deleting a camera stays admin-only.",
        "ui_only": False,
    },
    # ── Page-visibility capabilities (gate which nav surfaces a role sees) ────
    # ui_only means the SPA shows/hides the page and no route rejects the data —
    # either because the page reads endpoints gated by another capability
    # (AI Analytics reads the smart_search-gated detections) or because its
    # writes ride a different capability (map placements need camera_manage).
    # Camera *writes* are gated by camera_manage above, independently of who can
    # see the inventory.
    #
    # `soon` = the page does not exist; `preview` = the page is built and
    # navigable but its data is still demo. Keep both honest: a stale badge on
    # this matrix is worse than no badge, because admins grant against it.
    "camera_view": {
        "label": "Cameras & configuration",
        "hint": "See the camera inventory and per-camera configuration page (managing cameras needs the 'Add & manage cameras' permission)",
        "ui_only": True,
    },
    # Enforced server-side since Smart Search moved behind /api/search/*: the
    # index is no longer reachable from the browser, so denying the capability
    # denies the data, not just the nav item.
    "smart_search": {
        "label": "Smart Search",
        "hint": "Natural-language search across cameras",
        "ui_only": False,
    },
    "map_view": {
        "label": "Map",
        "hint": "Site map with live camera placements, motion heatmap and floor plans (editing the plan needs 'Add & manage cameras')",
        "ui_only": True,
    },
    "peripherals": {
        "label": "Peripherals",
        "hint": "Device wall, automation rules and trigger log for barriers, lights and relays. The inventory is real; device state and control are not — no Home-Assistant bridge exists, so nothing here switches a device.",
        "ui_only": True,
        # Still preview: the page is navigable and its inventory is real, but
        # the control half it advertises has no backend. Drop this the day a
        # bridge ships, not before.
        "preview": True,
    },
    # The write half of `peripherals`: editing the device inventory. Enforced
    # server-side (not ui_only) so granting it actually opens the CRUD routes.
    # Device *control* is not gated here because it does not exist yet — there
    # is no bridge to switch anything.
    "peripheral_manage": {
        "label": "Add & manage peripherals",
        "hint": "Edit the peripheral inventory — devices, locations and the addresses a Home-Assistant bridge will bind to. Switching a device is not possible on any role until that bridge ships.",
        "ui_only": False,
    },
    # Enforced server-side (not ui_only) since the Events tab reads AI activity
    # events from /api/analytics/events: those are records of people at places
    # and times, and hiding the tab is not the same as refusing the data.
    "ai_analytics": {
        "label": "AI Analytics",
        "hint": "AI detection dashboards, AI activity events and the per-camera analytics configuration",
        "ui_only": False,
    },
}

EDITABLE_ROLES = ("supervisor", "operator", "viewer", "dpo")


def register_capability(
    key: str,
    *,
    label: str,
    hint: str,
    ui_only: bool = False,
    defaults: dict[str, bool] | None = None,
) -> None:
    """Add a capability the core does not itself define.

    Called by an optional extension at startup (see backend/extensions). The
    core deliberately does not name these: `dsr_manage` and `evidence_export`
    were written into the catalogue and the matrix below until 2026-08-28, which
    meant open-core code named commercially-licensed features and the matrix UI
    offered toggles for routes that did not exist.

    `defaults` maps role → bool for EDITABLE_ROLES; roles left out default to
    False, which is the safe direction for a capability that grants access.

    Registration is startup-only and idempotent per key. Two things fall out of
    CAPABILITIES being the source of truth, and both are wanted: `_merged` drops
    stored values for unknown keys, so a policy row left over from a build that
    had the extension is ignored rather than honoured; and `set_policy` rejects
    an unknown capability with a 422 rather than storing something inert.
    """
    if key in CAPABILITIES:
        log.warning("policy.capability_already_registered", capability=key)
        return
    CAPABILITIES[key] = {"label": label, "hint": hint, "ui_only": ui_only}
    for role in EDITABLE_ROLES:
        DEFAULT_POLICY[role][key] = bool((defaults or {}).get(role, False))
    global _cache
    _cache = None
    log.info("policy.capability_registered", capability=key)

# Defaults for the roles that predate the matrix (operator/dpo) mirror pre-policy
# behavior: reads open to all, motion reset admin-only. Supervisor is an operator
# who may also acknowledge alarms; viewer is live-only. The page-visibility caps
# default per role below (all editable in the matrix); admin always has every cap.
#   camera_view   → Supervisor only (managing cameras is a Manage-section task)
#   camera_manage → Supervisor only (the shift lead who onboards a camera when no
#                   admin is on site); an admin can grant it to any role
#   peripherals   → Supervisor only (physical control is sensitive)
#   map/search/ai → the working roles; viewer stays live-only
# Extensions add their own rows to each role via register_capability(); a
# capability absent from this literal is absent from the product.
DEFAULT_POLICY: dict[str, dict[str, bool]] = {
    "supervisor": {"live_view": True, "playback_search": True,  "export_reports": True,  "motion_ack": True,
                   "camera_view": True,  "camera_manage": True,  "smart_search": True,  "map_view": True,  "peripherals": True, "peripheral_manage": True,
                   "ai_analytics": True},
    "operator":   {"live_view": True, "playback_search": True,  "export_reports": True,  "motion_ack": False,
                   "camera_view": False, "camera_manage": False, "smart_search": True,  "map_view": True,  "peripherals": False, "peripheral_manage": False,
                   "ai_analytics": True},
    "viewer":     {"live_view": True, "playback_search": False, "export_reports": False, "motion_ack": False,
                   "camera_view": False, "camera_manage": False, "smart_search": False, "map_view": True,  "peripherals": False, "peripheral_manage": False,
                   "ai_analytics": False},
    "dpo":        {"live_view": True, "playback_search": True,  "export_reports": True,  "motion_ack": False,
                   "camera_view": False, "camera_manage": False, "smart_search": True,  "map_view": True,  "peripherals": False, "peripheral_manage": False,
                   "ai_analytics": True},
}

_cache: dict[str, dict[str, bool]] | None = None
_cache_at: float = 0.0
_CACHE_TTL = 15.0


def _merged(stored: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    for role in EDITABLE_ROLES:
        base = dict(DEFAULT_POLICY[role])
        base.update({k: bool(v) for k, v in (stored.get(role) or {}).items() if k in CAPABILITIES})
        out[role] = base
    return out


async def get_policies() -> dict[str, dict[str, bool]]:
    """Effective policy map for the editable roles (defaults merged), cached."""
    global _cache, _cache_at
    if _cache is not None and time.monotonic() - _cache_at < _CACHE_TTL:
        return _cache
    from ..db import AsyncSessionLocal
    from ..models import RolePolicy

    stored: dict[str, dict[str, bool]] = {}
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(RolePolicy))).scalars().all()
            stored = {r.role: r.policy or {} for r in rows}
    except Exception as exc:  # noqa: BLE001 — policy read must never take the API down
        log.warning("policy.read_failed_using_defaults", error=str(exc))
    _cache = _merged(stored)
    _cache_at = time.monotonic()
    return _cache


async def set_policy(role: str, policy: dict[str, bool]) -> dict[str, bool]:
    if role not in EDITABLE_ROLES:
        raise HTTPException(422, detail=f"Role '{role}' has a fixed policy (admin always has full access)")
    unknown = [k for k in policy if k not in CAPABILITIES]
    if unknown:
        raise HTTPException(422, detail=f"Unknown capabilit{'ies' if len(unknown) > 1 else 'y'}: {', '.join(unknown)}")
    from ..db import AsyncSessionLocal
    from ..models import RolePolicy

    async with AsyncSessionLocal() as db:
        row = await db.get(RolePolicy, role)
        if row is None:
            row = RolePolicy(role=role, policy={})
            db.add(row)
        row.policy = {**DEFAULT_POLICY[role], **{k: bool(v) for k, v in policy.items()}}
        await db.commit()
    global _cache
    _cache = None  # invalidate
    log.info("policy.updated", role=role, policy=policy)
    return (await get_policies())[role]


async def allowed(principal: Principal, capability: str) -> bool:
    """Does this principal hold the capability? admin/service/dev always do."""
    if principal.kind in ("service", "dev") or "admin" in principal.roles:
        return True
    policies = await get_policies()
    return any(
        policies.get(role, {}).get(capability, False)
        for role in principal.roles
    )


async def require(principal: Principal, capability: str) -> None:
    if not await allowed(principal, capability):
        meta = CAPABILITIES.get(capability, {})
        raise HTTPException(
            403,
            # No screen is named: where permissions are edited depends on the
            # build (in-product, or the defaults plus the Keycloak console).
            detail=f"Your role doesn't have the '{meta.get('label', capability)}' permission — ask an administrator to grant it",
        )


def require_capability(capability: str):
    """Dependency factory: caller must hold ``capability`` (admin / service / dev
    always do). The policy-matrix analogue of ``security.require_role`` — use it
    on a route that should be gated by a *grantable* permission rather than a
    fixed role, so an admin can delegate the route without a code change."""

    async def _checker(request: Request) -> Principal:
        principal = await get_principal(request)
        await require(principal, capability)
        return principal

    return _checker


async def effective_for(principal: Principal) -> dict[str, bool]:
    """The principal's effective capability map (drives UI gating via /me)."""
    if principal.kind in ("service", "dev") or "admin" in principal.roles:
        return {cap: True for cap in CAPABILITIES}
    policies = await get_policies()
    return {
        cap: any(policies.get(r, {}).get(cap, False) for r in principal.roles)
        for cap in CAPABILITIES
    }
