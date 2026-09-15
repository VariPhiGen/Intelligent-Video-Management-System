"""Per-type detector parameter schemas for the CMM activity catalog.

Revision ID: 016
Revises: 015
Create Date: 2026-07-28

The activity-type catalog (migration 014) had only key/label/color — AI
Config rendered five generic parameter fields for every type regardless of
what the DeepStream pipeline actually expects. This migration adds
`params_schema` (a JSONB list of `{key, label, kind, default, min, max, unit,
help}` objects — the wire shape of `backend.models.ParamField`) describing
each type's DETECTOR parameters, and seeds it with the pipeline's real 26
types (its own config is authoritative for keys/defaults — nothing here is
invented or renamed).

`params_schema` describes detector parameters ONLY. The schedule
(active_hours/active_days) stays built-in on every activity and is never part
of a type's schema. A type genuinely without extra settings gets `[]` —
meaningful ("no extra settings"), not absent data.

Seeding is an UPSERT, not a wholesale replace:
  - key absent from the table  -> INSERT (label/color/sort_order/schema)
  - key already present        -> UPDATE params_schema ONLY; label/color/
                                   sort_order are left exactly as the admin
                                   (or migration 014) set them
This matters because the live catalog already has nine pristine defaults
from 014, one of which ('ppe') is also in the pipeline's 26 and already
referenced by a real camera's stored activities — upserting means that
camera's type keeps its original label/colour and keeps working. The other 25
types are new keys, so they're plain inserts; their colours cycle the same
six-colour ZONE_COLORS palette migration 014 used, and sort_order continues
from the current max instead of colliding with the existing nine.

downgrade() drops the column, which drops the schemas with it — the catalog
still round-trips to the five-generic-field UI Task 2 replaces.
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same palette migration 014's ZONE_COLORS cycled through; only used for
# NEWLY inserted types (existing keys keep their own colour untouched).
_PALETTE = ["#4fd1c5", "#ffb020", "#ef5350", "#b388ff", "#52c77e", "#ff8a65"]


def _f(key: str, label: str, kind: str, default: Any, *,
       min: float | None = None, max: float | None = None,
       unit: str | None = None) -> dict:
    return {
        "key": key, "label": label, "kind": kind, "default": default,
        "min": min, "max": max, "unit": unit, "help": None,
    }


_VEHICLE_CLASSES = ["car", "truck", "bus", "bike", "other_moving_machinary"]

# (key, label, params_schema) in pipeline-config order. Keys/defaults are
# verbatim from the pipeline's own config — do not rename or "improve" them.
_TYPES: list[tuple[str, str, list[dict]]] = [
    ("ppe", "PPE", [
        _f("frame_accuracy", "Frame accuracy", "number", 200, unit="frames"),
    ]),
    ("entry_exit_WLE_logs", "Entry / exit + wrong lane", [
        _f("wrong_lane", "Wrong lane", "bool", True),
        _f("entry_exit", "Entry / exit", "bool", True),
        _f("subcategory_mapping", "Subcategory mapping", "list", ["car", "truck", "person"]),
        _f("frame_send_sec", "Frame send interval", "number", 10, unit="s"),
    ]),
    ("stray_parking", "Stray parking", [
        _f("min_overlap", "Minimum overlap", "number", 0.15, min=0, max=1),
        _f("required_frames", "Required frames", "number", 2400, unit="frames"),
        _f("vehicle_classes", "Vehicle classes", "list", _VEHICLE_CLASSES),
    ]),
    ("fire_and_smoke", "Fire & smoke", [
        _f("subcategory_mapping", "Subcategory mapping", "list", ["fire", "smoke"]),
        _f("frame_accuracy", "Frame accuracy", "number", 0, unit="frames"),
        _f("last_frame_check", "Last frame check", "number", 2, unit="frames"),
        _f("alert_interval", "Alert interval", "number", 60, unit="s"),
    ]),
    ("loitering_detection", "Loitering", [
        _f("loitering_time", "Loitering time", "number", 200, unit="s"),
        _f("hsv_target", "HSV target", "json", {}),
        _f("hsv_tolerance", "HSV tolerance", "json", {}),
    ]),
    ("vehicle_interaction", "Vehicle interaction", [
        _f("vehicle_classes", "Vehicle classes", "list", _VEHICLE_CLASSES),
        _f("person_classes", "Person classes", "list", ["person"]),
        _f("motion_thr", "Motion threshold", "number", 5),
        _f("collision_angel_threshold", "Collision angle threshold", "number", 30),
        _f("vv_hor_interaction_percentage", "Vehicle-vehicle horizontal interaction", "number", 0.5),
        _f("vv_ver_interaction_percentage", "Vehicle-vehicle vertical interaction", "number", 0.7),
        _f("hor_interaction_percentage", "Horizontal interaction", "number", 0.5),
        _f("ver_interaction_percentage", "Vertical interaction", "number", 0.7),
    ]),
    ("people_gathering", "People gathering", [
        _f("person_limit", "Person limit", "number", 4),
        _f("last_time", "Last time", "number", 30),
        _f("frame_accuracy", "Frame accuracy", "number", 1800, unit="frames"),
    ]),
    ("running_detection", "Running", [
        _f("speed_threshold", "Speed threshold", "number", 7.0),
        _f("max_history", "Max history", "number", 10),
        _f("line_draw_y", "Line draw Y positions", "list", [0, 100, 190, 270, 350, 430, 510, 590, 670]),
        _f("knwon_distance_m", "Known distance", "number", 1.0, unit="m"),
    ]),
    ("idle_person", "Idle person", [
        _f("idle_threshold", "Idle threshold", "number", 120, unit="s"),
    ]),
    ("person_fallen", "Person fallen", [
        _f("frame_accuracy", "Frame accuracy", "number", 60, unit="frames"),
    ]),
    ("workforce_efficiency", "Workforce efficiency", []),
    ("hairnet_detection", "Hairnet", [
        _f("hsv_target", "HSV target", "json", {"staff": [110, 120, 120]}),
        _f("hsv_tolerance", "HSV tolerance", "json", {"staff": [40, 40, 40]}),
        _f("frame_accuracy", "Frame accuracy", "number", 200, unit="frames"),
        _f("iou_threshold", "IoU threshold", "number", 0.3, min=0, max=1),
    ]),
    ("no_person_area", "No-person area", [
        _f("frame_accuracy", "Frame accuracy", "number", 20, unit="frames"),
    ]),
    ("first_person_entering", "First person entering", [
        _f("frame_accuracy", "Frame accuracy", "number", 100, unit="frames"),
    ]),
    ("last_person_leaving", "Last person leaving", [
        _f("frame_accuracy", "Frame accuracy", "number", 50, unit="frames"),
        _f("start_collection_time", "Start collection time", "text", "16:00:00"),
        _f("trigger_alert_time", "Trigger alert time", "text", "20:01:00"),
    ]),
    ("Heatmap_overlay", "Heatmap overlay", [
        _f("classes_for_heatmap", "Classes for heatmap", "list", ["cleaning_machinary"]),
        _f("intensity", "Intensity", "number", 1.0),
        _f("radius", "Radius", "number", 40),
    ]),
    ("unattended_student_detection", "Unattended student", [
        _f("unattended_student_threshold_s", "Unattended student threshold", "number", 20, unit="s"),
        _f("student_margin_factor", "Student margin factor", "number", 1.0),
    ]),
    ("desk_occupancy", "Desk occupancy", []),
    ("resource_utilization", "Resource utilization", []),
    ("time_based_unauthorized_access", "Time-based unauthorized access", []),
    ("workplace_area_occupancy", "Workplace area occupancy", []),
    ("person_violations", "Person violations", []),
    ("perimeter_monitoring", "Perimeter monitoring", []),
    ("climbing", "Climbing", []),
    ("traffic_overspeeding", "Traffic overspeeding", [
        _f("speed_threshold", "Speed threshold", "number", 60),
    ]),
    ("traffic_overspeeding_distancewise", "Traffic overspeeding (distance)", [
        _f("speed_threshold", "Speed threshold", "number", 60),
        _f("distance_threshold", "Distance threshold", "number", 100),
    ]),
]


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "analytics_activity_types",
        sa.Column(
            "params_schema", JSONB, nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    existing_keys = set(
        conn.execute(sa.text("SELECT key FROM analytics_activity_types")).scalars().all()
    )
    max_sort = conn.execute(
        sa.text("SELECT COALESCE(MAX(sort_order), -1) FROM analytics_activity_types")
    ).scalar_one()

    next_sort = max_sort + 1
    palette_i = 0
    for key, label, schema in _TYPES:
        schema_json = json.dumps(schema)
        if key in existing_keys:
            # Upsert rule: never touch label/color/sort_order of a type the
            # admin (or 014's seed) already defined — only its params_schema.
            conn.execute(
                sa.text(
                    "UPDATE analytics_activity_types "
                    "SET params_schema = CAST(:schema AS jsonb) WHERE key = :key"
                ),
                {"schema": schema_json, "key": key},
            )
        else:
            color = _PALETTE[palette_i % len(_PALETTE)]
            palette_i += 1
            conn.execute(
                sa.text(
                    "INSERT INTO analytics_activity_types "
                    "(key, label, color, sort_order, params_schema) "
                    "VALUES (:key, :label, :color, :sort_order, CAST(:schema AS jsonb))"
                ),
                {
                    "key": key, "label": label, "color": color,
                    "sort_order": next_sort, "schema": schema_json,
                },
            )
            next_sort += 1


def downgrade() -> None:
    op.drop_column("analytics_activity_types", "params_schema")
