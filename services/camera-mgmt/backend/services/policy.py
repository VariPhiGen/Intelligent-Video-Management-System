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
    # Moving a PTZ camera is an operational action with a physical consequence:
    # it changes what the site records, and what it stops recording. Hence a
    # capability of its own, enforced server-side (the /ptz routes check it),
    # not folded into camera_manage — an operator who tracks someone across a
    # forecourt is not thereby allowed to reconfigure the camera. Creating and
    # deleting PRESETS is the other way round: that is configuration and stays
    # on camera_manage.
    "ptz_control": {
        "label": "PTZ control",
        "hint": "Pan, tilt, zoom and recall presets on cameras that support it. Creating or deleting presets needs 'Add & manage cameras'.",
        "ui_only": False,
    },
    # Who moved which camera, when, to where (2026-10-27, user ask: "remove history of camera control from all
    # other users except admin"). Off for every role by default — admin always has it — and grantable here.
    # Enforced server-side: the history / time-at-preset / patrol-log routes refuse without it, and the
    # current-position answers drop the "moved by" name. Where a camera points NOW stays visible to everyone
    # who can see the camera: operators need it to work.
    "ptz_history": {
        "label": "PTZ movement history",
        "hint": "Who moved which PTZ camera, when and where to: position history, time-at-preset, patrol run log, preset visit counts and the 'moved by' name. Admins always have it.",
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
        "hint": "Device wall, automation rules and trigger log for barriers, lights and relays. Device state comes from the bridge (MQTT, Home Assistant or webhook); a device with no binding yet reads as unknown.",
        "ui_only": True,
        # No longer preview: the bridge (services/bridge) shipped, so the wall
        # shows state a device actually reported and control has a backend.
    },
    # The write half of `peripherals`: editing the device inventory and its
    # bridge bindings. Enforced server-side (not ui_only) so granting it
    # actually opens the CRUD routes. Device *control* is a separate authority
    # (`peripheral_control` below).
    "peripheral_manage": {
        "label": "Add & manage peripherals",
        "hint": "Edit the peripheral inventory — devices, locations and the bridge binding (MQTT topic, Home Assistant entity or webhook) each one is reached through. Does not include switching a device.",
        "ui_only": False,
    },
    # ── Automation rules and the alerts they raise (migration 039) ───────────
    # Four capabilities, and the split between them is the safety story.
    # Reading the trigger log, closing an alert, editing a rule and being able
    # to arm one are four different authorities, and collapsing any pair of
    # them would hand somebody a power they were not granted.
    "alerts_view": {
        "label": "Alerts",
        "hint": "See alerts raised by automation rules, and the detections behind them",
        "ui_only": True,
    },
    "alerts_ack": {
        "label": "Acknowledge & resolve alerts",
        "hint": "Acknowledge an alert, record whether it was a true or false positive, and place an evidence hold",
        "ui_only": False,
    },
    "rule_manage": {
        "label": "Manage automation rules",
        "hint": "Create and edit automation rules, arm them, and disarm the engine. Arming is what lets a rule act on a device — editing a log-only rule is not.",
        "ui_only": False,
    },
    # Enforced server-side by routers/peripherals.py on the manual-control
    # routes. Only a device whose binding the bridge has verified can be
    # switched, and every switch is audited.
    "peripheral_control": {
        "label": "Switch a peripheral",
        "hint": "Physically operate a device through its bridge binding — open a barrier, unlock a door, pulse a siren. Separate from editing the inventory: those are different authorities.",
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
    # Server-enforced, not a page toggle. Collection turns recorded footage
    # into a durable pile of stills on disk and can push them to an EXTERNAL
    # annotation tool, so hiding the nav item would be a false promise — this
    # has to gate /api/datasets itself.
    "data_collection": {
        "label": "Data Collection",
        "hint": "Sample training images from recordings and import them into Label Studio",
        "ui_only": False,
    },
    # Time-lapse (services/timelapse.py, migration 057). Two capabilities, not
    # one and not camera_manage: switching it on starts a durable record of a
    # camera's view that deliberately outlives its footage, which an admin may
    # delegate to a site lead without handing over camera configuration; and
    # WATCHING progress is grantable without opening recorded footage. Both are
    # enforced by /api/timelapse itself — routers/timelapse.py explains why a
    # download needs only timelapse_view (the NVR clip-export precedent).
    "timelapse_view": {
        "label": "Time-lapse",
        "hint": "See time-lapse stills, play and download time-lapse videos, request a render",
        "ui_only": False,
    },
    "timelapse_manage": {
        "label": "Manage time-lapse",
        "hint": "Switch a camera's time-lapse on or off, change its settings and the default settings every camera follows, turn time-lapse back on after the disk guard paused it, delete stills and videos. The disk protection limits stay admin-only.",
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
#   ptz_control   → Supervisor + Operator (aiming a camera is console work);
#                   viewer stays look-only, DPO oversees rather than operates
#   camera_view   → Supervisor only (managing cameras is a Manage-section task)
#   camera_manage → Supervisor only (the shift lead who onboards a camera when no
#                   admin is on site); an admin can grant it to any role
#   peripherals   → Supervisor only (physical control is sensitive)
#   map/search/ai → the working roles; viewer stays live-only
#   timelapse     → view: Supervisor + Operator; manage: Supervisor (switching it
#                   on starts a 90-day record — the shift lead's call, not the
#                   console's); viewer and DPO get it only when granted
# Extensions add their own rows to each role via register_capability(); a
# capability absent from this literal is absent from the product.
DEFAULT_POLICY: dict[str, dict[str, bool]] = {
    "supervisor": {"live_view": True, "playback_search": True,  "export_reports": True,  "motion_ack": True,  "ptz_control": True, "ptz_history": False,
                   "camera_view": True,  "camera_manage": True,  "smart_search": True,  "map_view": True,  "peripherals": True, "peripheral_manage": True,
                   "ai_analytics": True, "data_collection": True,
                   "alerts_view": True,  "alerts_ack": True,  "rule_manage": True,  "peripheral_control": True,
                   "timelapse_view": True, "timelapse_manage": True},
    "operator":   {"live_view": True, "playback_search": True,  "export_reports": True,  "motion_ack": False, "ptz_control": True, "ptz_history": False,
                   "camera_view": False, "camera_manage": False, "smart_search": True,  "map_view": True,  "peripherals": False, "peripheral_manage": False,
                   "ai_analytics": True, "data_collection": False,
                   "alerts_view": True,  "alerts_ack": True,  "rule_manage": False, "peripheral_control": False,
                   "timelapse_view": True, "timelapse_manage": False},
    "viewer":     {"live_view": True, "playback_search": False, "export_reports": False, "motion_ack": False, "ptz_control": False, "ptz_history": False,
                   "camera_view": False, "camera_manage": False, "smart_search": False, "map_view": True,  "peripherals": False, "peripheral_manage": False,
                   "ai_analytics": False, "data_collection": False,
                   "alerts_view": True,  "alerts_ack": False, "rule_manage": False, "peripheral_control": False,
                   "timelapse_view": False, "timelapse_manage": False},
    "dpo":        {"live_view": True, "playback_search": True,  "export_reports": True,  "motion_ack": False, "ptz_control": False, "ptz_history": False,
                   "camera_view": False, "camera_manage": False, "smart_search": True,  "map_view": True,  "peripherals": False, "peripheral_manage": False,
                   "ai_analytics": True, "data_collection": False,
                   "alerts_view": False, "alerts_ack": False, "rule_manage": False, "peripheral_control": False,
                   "timelapse_view": False, "timelapse_manage": False},
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
