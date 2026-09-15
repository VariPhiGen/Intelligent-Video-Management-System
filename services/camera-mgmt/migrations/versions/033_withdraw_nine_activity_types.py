"""Withdraw nine activity types from the catalog.

Revision ID: 033
Revises: 032
Create Date: 2026-09-12

These nine are removed from `analytics_activity_types` at the product's
request — they are site-specific detectors that this build should not offer
as general options:

    hairnet_detection             unattended_student_detection
    unauthorised_access           workforce_efficiency
    vehicle_interaction           running_detection
    ppe                           phone_usage_verified
    phone_usage_restricted_area

Unlike migration 017's removals, this is **not** a correctness fix. Every key
here resolves to a real class in `python_module/component/activities/` and
would run if configured; 017's nine had no implementation at all. Nothing in
the pipeline changes — the activity classes stay where they are, and a
hand-authored config on an appliance that wants one of them keeps working.
Only what the AI Config UI offers is narrowed, which is why downgrade()
restores these rows verbatim (schemas included) rather than approximating
them.

`ppe` is the one with history worth naming. 016 gave it a schema of
`frame_accuracy` alone, which made it a silent no-op; 017 added
`subcategory_mapping` and backfilled the stored configs. That repair is
preserved in `_WITHDRAWN_ROWS` below, so a downgrade returns the *working*
`ppe`, not 016's broken one.

Stored camera configs are migrated the same way 017 did it: activities on a
withdrawn type are dropped, and regions no surviving activity references go
with them. The type key is validated against the catalog on save
(`update_analytics_config`), so a camera left pointing at a withdrawn key
would 422 on its next save with nothing in the UI explaining why. On this
appliance the loop is a no-op — no camera has a non-empty `analytics_config`
as of this migration — but the same file runs on boxes where that is not
true.

downgrade() restores the catalog rows, not the camera configs. A config that
lost its `ppe` activity cannot be distinguished on the way back from one that
never had it, and re-inserting a guess is worse than leaving the operator to
re-add the activity in the UI, which is a two-click job.
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Snapshot of the rows as they stand at 032, taken from the appliance DB so a
# downgrade round-trips label, colour, sort_order and schema exactly. Held as
# JSON text rather than a Python literal because that is the form it was read
# in and the form it goes back as — no retyping, nothing to drift.
_WITHDRAWN_ROWS: list[dict[str, Any]] = json.loads(r"""
[
    {
        "key": "hairnet_detection",
        "label": "Hairnet",
        "color": "#52c77e",
        "sort_order": 19,
        "params_schema": [
            {
                "key": "hsv_target",
                "max": null,
                "min": null,
                "help": null,
                "kind": "json",
                "unit": null,
                "label": "HSV target",
                "default": {
                    "staff": [
                        110,
                        120,
                        120
                    ]
                }
            },
            {
                "key": "hsv_tolerance",
                "max": null,
                "min": null,
                "help": null,
                "kind": "json",
                "unit": null,
                "label": "HSV tolerance",
                "default": {
                    "staff": [
                        40,
                        40,
                        40
                    ]
                }
            },
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 200
            },
            {
                "key": "iou_threshold",
                "max": 1,
                "min": 0,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "IoU threshold",
                "default": 0.3
            }
        ]
    },
    {
        "key": "unattended_student_detection",
        "label": "Unattended student",
        "color": "#b388ff",
        "sort_order": 24,
        "params_schema": [
            {
                "key": "unattended_student_threshold_s",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Unattended student threshold",
                "default": 20
            },
            {
                "key": "student_margin_factor",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Student margin factor",
                "default": 1.0
            }
        ]
    },
    {
        "key": "unauthorised_access",
        "label": "Unauthorised access",
        "color": "#ffb020",
        "sort_order": 35,
        "params_schema": [
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 5
            }
        ]
    },
    {
        "key": "workforce_efficiency",
        "label": "Workforce efficiency",
        "color": "#b388ff",
        "sort_order": 18,
        "params_schema": []
    },
    {
        "key": "vehicle_interaction",
        "label": "Vehicle interaction",
        "color": "#52c77e",
        "sort_order": 13,
        "params_schema": [
            {
                "key": "vehicle_classes",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Vehicle classes",
                "default": [
                    "car",
                    "truck",
                    "bus",
                    "bike",
                    "other_moving_machinary"
                ]
            },
            {
                "key": "person_classes",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Person classes",
                "default": [
                    "person"
                ]
            },
            {
                "key": "motion_thr",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Motion threshold",
                "default": 5
            },
            {
                "key": "collision_angel_threshold",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Collision angle threshold",
                "default": 30
            },
            {
                "key": "vv_hor_interaction_percentage",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Vehicle-vehicle horizontal interaction",
                "default": 0.5
            },
            {
                "key": "vv_ver_interaction_percentage",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Vehicle-vehicle vertical interaction",
                "default": 0.7
            },
            {
                "key": "hor_interaction_percentage",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Horizontal interaction",
                "default": 0.5
            },
            {
                "key": "ver_interaction_percentage",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Vertical interaction",
                "default": 0.7
            }
        ]
    },
    {
        "key": "running_detection",
        "label": "Running",
        "color": "#4fd1c5",
        "sort_order": 15,
        "params_schema": [
            {
                "key": "speed_threshold",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Speed threshold",
                "default": 7.0
            },
            {
                "key": "max_history",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Max history",
                "default": 10
            },
            {
                "key": "line_draw_y",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Line draw Y positions",
                "default": [
                    0,
                    100,
                    190,
                    270,
                    350,
                    430,
                    510,
                    590,
                    670
                ]
            },
            {
                "key": "knwon_distance_m",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "m",
                "label": "Known distance",
                "default": 1.0
            }
        ]
    },
    {
        "key": "ppe",
        "label": "PPE",
        "color": "#ffb020",
        "sort_order": 0,
        "params_schema": [
            {
                "key": "frame_accuracy",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "frames",
                "label": "Frame accuracy",
                "default": 200
            },
            {
                "key": "subcategory_mapping",
                "max": null,
                "min": null,
                "help": null,
                "kind": "json",
                "unit": null,
                "label": "PPE classes to watch",
                "default": {
                    "foot": "Safety Shoes",
                    "head": "Helmet",
                    "hands": "Safety Gloves",
                    "no-vest": "Reflective Jacket"
                }
            }
        ]
    },
    {
        "key": "phone_usage_verified",
        "label": "Phone usage (verified)",
        "color": "#52c77e",
        "sort_order": 38,
        "params_schema": [
            {
                "key": "dwell_threshold_s",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Dwell threshold",
                "default": 40
            },
            {
                "key": "snapshot_interval_s",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Snapshot interval",
                "default": 2
            },
            {
                "key": "max_crops",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": null,
                "label": "Max crops",
                "default": 20
            },
            {
                "key": "service_url",
                "max": null,
                "min": null,
                "help": null,
                "kind": "text",
                "unit": null,
                "label": "Verification service URL",
                "default": "http://localhost:8600"
            },
            {
                "key": "request_timeout_s",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Request timeout",
                "default": 15
            }
        ]
    },
    {
        "key": "phone_usage_restricted_area",
        "label": "Phone usage (restricted area)",
        "color": "#b388ff",
        "sort_order": 37,
        "params_schema": [
            {
                "key": "phone_classes",
                "max": null,
                "min": null,
                "help": null,
                "kind": "list",
                "unit": null,
                "label": "Phone classes",
                "default": [
                    "phone",
                    "mobile",
                    "cell phone",
                    "mobile_phone",
                    "smartphone"
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
                "default": 0.3
            },
            {
                "key": "cooldown_s",
                "max": null,
                "min": null,
                "help": null,
                "kind": "number",
                "unit": "s",
                "label": "Cooldown",
                "default": 60
            }
        ]
    }
]
""")

_WITHDRAWN = [row["key"] for row in _WITHDRAWN_ROWS]


def _prune_regions(cfg: dict) -> bool:
    """Drop regions no surviving activity references. Returns whether it did.

    Same rule as 017: a region left behind still draws in the zone editor,
    implying analysis that no longer happens anywhere.
    """
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
                print(f"[033] camera {slug}: dropping activity '{act.get('type')}' "
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


def downgrade() -> None:
    conn = op.get_bind()
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
