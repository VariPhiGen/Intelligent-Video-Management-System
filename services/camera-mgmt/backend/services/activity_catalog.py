"""activity_catalog.py — the Activity Type catalog follows the CPU activity registry.

WHO OWNS WHAT.

  CPU activity registry     which activities exist and can run, the zones each
  (analytics service,       takes, and every setting's definition — key, label,
   GET /activities)         kind, default, unit, bounds, allowed values.
  analytics_activity_types  a synced copy of that, kept here so the website
                            works while analytics is down and old events keep
                            their labels — plus what an administrator owns:
                            label, colour, order.
  cameras.analytics_config  the values for one camera.

`sync_catalog` copies the registry in. It never deletes a row: a key the
registry stops listing becomes `unregistered`, because stored camera configs
and historical events still name it. It never overwrites label, colour or order
of an existing row — those are the administrator's.

`validate_activities` is what AI Config's save is checked against: only an
`available` activity can be added (one already stored is kept even if it is no
longer available, so an unrelated save cannot silently delete it), each takes
the zones its rule allows, and every setting must be a value its code accepts.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping

import structlog
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import _HEX_COLOR, _TYPE_KEY, AnalyticsActivityType, ParamField

log = structlog.get_logger(__name__)

AVAILABLE = "available"
HOLD = "hold"
UNREGISTERED = "unregistered"

ZONE_OPTIONAL = "optional"
ZONE_REQUIRED = "required"
ZONE_TRIPWIRE = "tripwire"


class RegistryDefinition(BaseModel):
    """One activity as the analytics service publishes it."""

    key: str
    label: str = Field(min_length=1, max_length=120)
    description: str = ""
    color: str
    status: Literal["available", "hold"]
    zone_rule: Literal["optional", "required", "tripwire"]
    version: int = Field(default=1, ge=1)
    params_schema: list[ParamField] = Field(default_factory=list, max_length=32)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _TYPE_KEY.match(v):
            raise ValueError("invalid activity key")
        return v

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not _HEX_COLOR.match(v):
            raise ValueError("color must be a #RRGGBB hex string")
        return v


def parse_definitions(raw: Iterable[Any]) -> list[RegistryDefinition]:
    """The published definitions that validate. One malformed entry is logged
    and skipped; it must not stop every other activity from syncing."""
    out: list[RegistryDefinition] = []
    for item in raw:
        try:
            out.append(RegistryDefinition.model_validate(item))
        except Exception as exc:  # noqa: BLE001
            log.warning("activity_catalog.definition_invalid",
                        key=(item or {}).get("key") if isinstance(item, Mapping) else None,
                        error=str(exc)[:300])
    return out


async def sync_catalog(db: AsyncSession, definitions: list[RegistryDefinition]) -> dict:
    """Make the catalog match the registry. Returns counts."""
    rows = {r.key: r for r in (await db.execute(select(AnalyticsActivityType))).scalars().all()}
    now = datetime.now(timezone.utc)
    next_order = max((r.sort_order for r in rows.values()), default=-1) + 1
    added = updated = withdrawn = 0
    seen: set[str] = set()
    for d in definitions:
        seen.add(d.key)
        schema = [f.model_dump() for f in d.params_schema]
        row = rows.get(d.key)
        if row is None:
            db.add(AnalyticsActivityType(
                key=d.key, label=d.label, color=d.color, sort_order=next_order,
                params_schema=schema, status=d.status, zone_rule=d.zone_rule,
                description=d.description or None, definition_version=d.version,
                synced_at=now,
            ))
            next_order += 1
            added += 1
            continue
        changed = (row.params_schema != schema or row.status != d.status
                   or row.zone_rule != d.zone_rule
                   or (row.description or "") != (d.description or "")
                   or row.definition_version != d.version)
        row.params_schema = schema
        row.status = d.status
        row.zone_rule = d.zone_rule
        row.description = d.description or None
        row.definition_version = d.version
        row.synced_at = now
        updated += int(changed)
    for key, row in rows.items():
        if key not in seen and row.status != UNREGISTERED:
            row.status = UNREGISTERED
            row.synced_at = now
            withdrawn += 1
    await db.commit()
    counts = {"registered": len(seen), "added": added, "updated": updated, "withdrawn": withdrawn}
    if added or updated or withdrawn:
        log.info("activity_catalog.synced", **counts)
    return counts


def validate_params(schema: Iterable[Mapping], params: Mapping) -> list[str]:
    """Problems with a camera's setting values against a type's params_schema.

    A setting the camera does not store is fine — the runtime uses its default.
    Keys the schema does not declare are not judged here (AI Config drops them).
    """
    errors: list[str] = []
    for f in schema:
        if f.get("configurable") is False:
            continue
        key = f.get("key")
        label = f.get("label") or key
        if key not in params or params[key] is None:
            continue
        value = params[key]
        kind = f.get("kind")
        if kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value):
                errors.append(f"{label} must be a number")
            elif f.get("integer") and float(value) != int(value):
                errors.append(f"{label} must be a whole number")
            elif f.get("min") is not None and value < f["min"]:
                errors.append(f"{label} must be at least {f['min']:g}")
            elif f.get("max") is not None and value > f["max"]:
                errors.append(f"{label} must be at most {f['max']:g}")
        elif kind == "bool":
            if not isinstance(value, bool):
                errors.append(f"{label} must be true or false")
        elif kind == "text":
            if not isinstance(value, str):
                errors.append(f"{label} must be text")
        elif kind == "enum":
            options = f.get("options") or []
            if value not in options:
                errors.append(f"{label} must be one of {options}")
        elif kind == "list":
            if not isinstance(value, list):
                errors.append(f"{label} must be a list")
                continue
            options = f.get("options")
            unsupported = [v for v in value if options is not None and v not in options]
            if unsupported:
                errors.append(f"{label}: {unsupported} not supported (choose from {options})")
            elif f.get("min_items") is not None and len(value) < f["min_items"]:
                errors.append(f"{label} needs at least {f['min_items']} selected")
    return errors


def validate_activities(catalog: Mapping[str, Any], stored_config: Mapping,
                        activities: Iterable[Any], regions: Mapping[str, Any]) -> list[str]:
    """Problems with an AI Config save against the catalog. Unknown keys are the
    caller's (the long-standing 422 names them)."""
    stored_types = {a.get("type") for a in (stored_config or {}).get("activities") or []
                    if isinstance(a, Mapping)}
    errors: list[str] = []
    for act in activities:
        row = catalog.get(act.type)
        if row is None:
            continue
        label = row.label
        if row.status != AVAILABLE:
            if act.type not in stored_types:
                why = ("it is on hold" if row.status == HOLD
                       else "it is not part of the running analytics engine")
                errors.append(f"‘{label}’ cannot be added: {why}")
            # Already on this camera: kept as stored, so an unrelated save
            # never deletes it. It does not run either way.
            continue
        kinds = [getattr(regions.get(rid), "kind", None) for rid in act.regions]
        zones, wires = kinds.count("zone"), kinds.count("tripwire")
        if row.zone_rule == ZONE_REQUIRED and zones == 0:
            errors.append(f"‘{label}’ needs at least one zone")
        elif row.zone_rule == ZONE_OPTIONAL and wires and not zones:
            errors.append(f"‘{label}’ watches zones, not tripwires")
        elif row.zone_rule == ZONE_TRIPWIRE and zones:
            errors.append(f"‘{label}’ watches tripwires, not zones")
        elif row.zone_rule == ZONE_TRIPWIRE and not wires:
            errors.append(f"‘{label}’ needs at least one tripwire")
        errors.extend(f"‘{label}’: {e}" for e in validate_params(row.params_schema or [], act.params))
    return errors
