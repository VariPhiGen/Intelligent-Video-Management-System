"""Withdraw seven more activity types; rename `entry_exit_WLE_logs`.

Revision ID: 034
Revises: 033
Create Date: 2026-09-12

Second pass of the same product decision 033 started. Seven more types leave
the catalog:

    person_fallen             loitering_detection
    last_person_leaving       first_person_entering
    fire_and_smoke            face_search
    Heatmap_overlay

Eight remain: `entry_exit_WLE_logs`, `stray_parking`, `people_gathering`,
`no_person_area`, `restricted_zone_entry`, `car_detection`, `idle_worker`,
`person_detection`.

As in 033, every key here resolves to a real class in
`python_module/component/activities/` and still runs if a config names it —
this narrows what AI Config offers, it does not remove a capability from the
pipeline. Two are worth naming because they are the last UI route to their
feature: `face_search` was the only per-camera way to turn on the pipeline's
face recognition (the SmartSearch service is unrelated — it does not read
this catalog), and `Heatmap_overlay` the only way to enable the heatmap
render. Both become hand-authored-config-only.

The rename is label-only: `entry_exit_WLE_logs` becomes "Entry / exit",
dropping the "+ wrong lane" half. **The key is deliberately unchanged.**
Stored camera configs reference types by key, and the admin catalog editor
makes a saved row's key read-only for exactly this reason — renaming it here
would orphan every config using it and buy nothing, since the key is never
shown to an operator. `label` is the only thing the UI renders.

Camera configs are migrated the same way 033 does it — activities on a
withdrawn type dropped, orphaned regions pruned — because the type key is
validated against the catalog on save. Still a no-op on this appliance (no
camera has a non-empty `analytics_config`), still needed on boxes where that
is not true.

downgrade() restores the seven rows verbatim from the snapshot below and puts
the old label back. It does not restore camera configs, for the reason 033
gives: a config that lost an activity is indistinguishable on the way back
from one that never had it.
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RENAMED_KEY = "entry_exit_WLE_logs"
_NEW_LABEL = "Entry / exit"
_OLD_LABEL = "Entry / exit + wrong lane"

# Snapshot of the rows as they stand at 033, read off the appliance DB so a
# downgrade round-trips label, colour, sort_order and schema exactly. JSON
# text rather than a Python literal: the form it was read in is the form it
# goes back as, with nothing retyped.
_WITHDRAWN_ROWS: list[dict[str, Any]] = json.loads(r"""
[
    {
        "key": "person_fallen",
        "label": "Person fallen",
        "color": "#ef5350",
        "sort_order": 17,
        "params_schema": [
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 60
            }
        ]
    },
    {
        "key": "loitering_detection",
        "label": "Loitering",
        "color": "#b388ff",
        "sort_order": 12,
        "params_schema": [
            {
                "key": "loitering_time",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Loitering time",
                "default": 200
            },
            {
                "key": "hsv_target",
                "max": null,
                "min": null,
                "help": null,
                "kind": "json",
                "unit": null,
                "label": "HSV target",
                "default": {}
            },
            {
                "key": "hsv_tolerance",
                "max": null,
                "min": null,
                "help": null,
                "kind": "json",
                "unit": null,
                "label": "HSV tolerance",
                "default": {}
            }
        ]
    },
    {
        "key": "last_person_leaving",
        "label": "Last person leaving",
        "color": "#ffb020",
        "sort_order": 22,
        "params_schema": [
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 50
            },
            {
                "key": "start_collection_time",
                "max": null,
                "min": null,
                "help": null,
                "kind": "text",
                "unit": null,
                "label": "Start collection time",
                "default": "16:00:00"
            },
            {
                "key": "trigger_alert_time",
                "max": null,
                "min": null,
                "help": null,
                "kind": "text",
                "unit": null,
                "label": "Trigger alert time",
                "default": "20:01:00"
            }
        ]
    },
    {
        "key": "first_person_entering",
        "label": "First person entering",
        "color": "#4fd1c5",
        "sort_order": 21,
        "params_schema": [
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 100
            }
        ]
    },
    {
        "key": "fire_and_smoke",
        "label": "Fire & smoke",
        "color": "#ef5350",
        "sort_order": 11,
        "params_schema": [
            {
                "key": "subcategory_mapping",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Subcategory mapping",
                "default": [
                    "fire",
                    "smoke"
                ]
            },
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 0
            },
            {
                "key": "last_frame_check",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Last frame check",
                "default": 2
            },
            {
                "key": "alert_interval",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Alert interval",
                "default": 60
            }
        ]
    },
    {
        "key": "face_search",
        "label": "Face search",
        "color": "#ef5350",
        "sort_order": 36,
        "params_schema": [
            {
                "key": "face_search_url",
                "max": null,
                "min": null,
                "help": null,
                "kind": "text",
                "unit": null,
                "label": "Face search URL",
                "default": "http://localhost:8010/collect"
            },
            {
                "key": "tracked_classes",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Tracked classes",
                "default": [
                    "face"
                ]
            },
            {
                "key": "min_confidence",
                "max": 1,
                "min": 0,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Minimum confidence",
                "default": 0.9
            },
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 5
            },
            {
                "key": "cooldown_s",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Cooldown",
                "default": 3600
            },
            {
                "key": "persist",
                "max": null,
                "min": null,
                "help": null,
                "kind": "bool",
                "unit": null,
                "label": "Persist matches",
                "default": true
            }
        ]
    },
    {
        "key": "Heatmap_overlay",
        "label": "Heatmap overlay",
        "color": "#ef5350",
        "sort_order": 23,
        "params_schema": [
            {
                "key": "classes_for_heatmap",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Classes for heatmap",
                "default": [
                    "cleaning_machinary"
                ]
            },
            {
                "key": "intensity",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Intensity",
                "default": 1.0
            },
            {
                "key": "radius",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Radius",
                "default": 40
            }
        ]
    }
]
""")

_WITHDRAWN = [row["key"] for row in _WITHDRAWN_ROWS]


def _prune_regions(cfg: dict) -> bool:
    """Drop regions no surviving activity references. Returns whether it did."""
    regions = cfg.get("regions") or {}
    if not regions:
        return False
    used = {rid for act in cfg.get("activities") or [] for rid in (act.get("regions") or [])}
    orphans = [rid for rid in regions if rid not in used]
    for rid in orphans:
        del regions[rid]
    return bool(orphans)


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Camera configs: drop activities on the withdrawn types ────────────
    withdrawn = set(_WITHDRAWN)
    rows = conn.execute(
        sa.text("SELECT id, slug, analytics_config FROM cameras "
                "WHERE analytics_config IS NOT NULL")
    ).fetchall()

    for row_id, slug, cfg in rows:
        if isinstance(cfg, str):
            cfg = json.loads(cfg) if cfg else {}
        if not cfg or not cfg.get("activities"):
            continue

        kept = [act for act in cfg["activities"] if act.get("type") not in withdrawn]
        if len(kept) == len(cfg["activities"]):
            continue
        for act in cfg["activities"]:
            if act.get("type") in withdrawn:
                print(f"[034] camera {slug}: dropping activity '{act.get('type')}' "
                      "(type withdrawn from the catalog)")

        new_cfg = dict(cfg)
        new_cfg["activities"] = kept
        new_cfg["regions"] = dict(cfg.get("regions") or {})
        _prune_regions(new_cfg)

        conn.execute(
            sa.text("UPDATE cameras SET analytics_config = :cfg WHERE id = :id"),
            {"cfg": json.dumps(new_cfg), "id": row_id},
        )

    # ── 2. Catalog: withdraw the types ───────────────────────────────────────
    conn.execute(
        sa.text("DELETE FROM analytics_activity_types WHERE key = ANY(:keys)"),
        {"keys": _WITHDRAWN},
    )

    # ── 3. Catalog: rename the survivor ──────────────────────────────────────
    # Guarded on the old label so a site that renamed this row by hand in the
    # admin editor keeps its own wording instead of being overwritten.
    conn.execute(
        sa.text("UPDATE analytics_activity_types SET label = :new "
                "WHERE key = :key AND label = :old"),
        {"new": _NEW_LABEL, "key": _RENAMED_KEY, "old": _OLD_LABEL},
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("UPDATE analytics_activity_types SET label = :old "
                "WHERE key = :key AND label = :new"),
        {"old": _OLD_LABEL, "key": _RENAMED_KEY, "new": _NEW_LABEL},
    )

    for row in _WITHDRAWN_ROWS:
        conn.execute(
            sa.text("INSERT INTO analytics_activity_types "
                    "(key, label, color, sort_order, params_schema) "
                    "VALUES (:key, :label, :color, :sort_order, CAST(:schema AS jsonb)) "
                    "ON CONFLICT (key) DO NOTHING"),
            {"key": row["key"], "label": row["label"], "color": row["color"],
             "sort_order": row["sort_order"],
             "schema": json.dumps(row["params_schema"])},
        )
