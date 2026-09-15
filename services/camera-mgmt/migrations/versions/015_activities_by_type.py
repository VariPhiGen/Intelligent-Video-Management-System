"""Group activities by detection type (align with DeepStream semantics.

Revision ID: 015
Revises: 014
Create Date: 2026-07-28

The DeepStream pipeline keys activities by detection TYPE, each type owning
several zones and one parameter block. The VMS used to store an array of
activity INSTANCES, each watching exactly one region (so the same type could
appear many times with different names/regions). This migration reshapes
every camera's stored analytics_config.activities in place to the grouped
shape: one object per type, with a `regions` list replacing the singular
`region`, and no more `id`/`name` (the catalog label is now the display
name).

Grouping rule, per camera:
  - group instances by `type`, in first-appearance order
  - `regions` = the union of the group's `region` values, de-duplicated,
    order preserved, dropping any region id that isn't a key of the config's
    `regions` map (a dangling reference)
  - `params` = the params of the FIRST instance in the group; if a later
    instance in the same group carries different params, that per-instance
    setting is dropped and a warning is printed (type schemas are shared
    across all regions of a type going forward, so there is no way to keep
    both without inventing a new field)
  - an instance whose `region` is null/missing contributes no region
  - a group left with zero regions after the above is dropped entirely
  - cameras with analytics_config NULL or '{}' are untouched

downgrade() is not implemented: reversing this would have to invent
per-instance params for regions that shared a group's single param block,
which cannot be done without data loss going the other way too.

Idempotency guard: a camera whose activities are ALL already in the grouped
shape (`regions` list, no `region` key) is left untouched — see
`_is_grouped`/the skip check in `upgrade()` for why this matters.
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_grouped(entry: dict) -> bool:
    """True if `entry` is already in the target shape: a `regions` list and
    no singular `region` key."""
    return "regions" in entry and "region" not in entry


def _convert_activities(slug: str, activities: list[dict], regions_map: dict) -> list[dict]:
    """Group one camera's activities by type. Handles a mix of the old
    per-instance shape (singular `region`) and the new grouped shape
    (`regions` list) in the same list — an already-grouped entry contributes
    its own `regions`/`params` to its type's group instead of being
    re-derived from a `region` key it doesn't have.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for inst in activities:
        atype = inst.get("type")
        if not atype:
            continue

        if _is_grouped(inst):
            region_ids = [r for r in (inst.get("regions") or []) if r in regions_map]
        else:
            region_id = inst.get("region")
            region_ids = [region_id] if region_id and region_id in regions_map else []
        params = inst.get("params") or {}

        if atype not in groups:
            groups[atype] = {"regions": [], "params": params}
            order.append(atype)
        group = groups[atype]

        for rid in region_ids:
            if rid not in group["regions"]:
                group["regions"].append(rid)

        if params != group["params"]:
            print(
                f"[015_activities_by_type] camera {slug}: dropping per-instance "
                f"params for type '{atype}' (differs from the first instance's)"
            )

    return [
        {"type": atype, "regions": groups[atype]["regions"], "params": groups[atype]["params"]}
        for atype in order
        if groups[atype]["regions"]
    ]


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, slug, analytics_config FROM cameras WHERE analytics_config IS NOT NULL")
    ).fetchall()

    for row_id, slug, cfg in rows:
        if isinstance(cfg, str):
            cfg = json.loads(cfg) if cfg else {}
        if not cfg:
            continue

        activities = cfg.get("activities")
        regions_map = cfg.get("regions") or {}
        if not activities:
            continue

        # Guard against re-running this migration against data it (or a
        # repair script reusing this loop) already converted: a converted
        # entry has `regions` and no `region`, so re-deriving a singular
        # `region` from it would silently read None for every instance and
        # drop every activity with no warning. Alembic won't normally
        # re-run upgrade(), but a rolled-back alembic_version or a reused
        # copy of this loop would — so this check stays even though it
        # looks redundant in the common case. A camera with a MIX of old
        # and already-grouped entries still goes through _convert_activities,
        # which leaves the already-grouped ones alone and converts the rest.
        if all(_is_grouped(a) for a in activities):
            print(f"[015_activities_by_type] camera {slug}: activities already grouped, skipping")
            continue

        new_activities = _convert_activities(slug, activities, regions_map)

        new_cfg = dict(cfg)
        new_cfg["activities"] = new_activities
        conn.execute(
            sa.text("UPDATE cameras SET analytics_config = :cfg WHERE id = :id"),
            {"cfg": json.dumps(new_cfg), "id": row_id},
        )


def downgrade() -> None:
    raise NotImplementedError(
        "015 downgrade unsupported: reversing grouped activities back to per-instance "
        "would have to invent per-instance params for regions that shared one group"
    )
