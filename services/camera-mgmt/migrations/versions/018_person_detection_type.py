"""Add `person_detection` to the activity catalog.

Revision ID: 018
Revises: 017
Create Date: 2026-08-03

Migration 017 aligned the catalog with the pipeline in one direction: every
key it left behind resolves to a real activity class, and that still holds —
all 23 catalog keys match a class today. It did not close the other
direction. The pipeline's registry
(`python_module/component/activities/__init__.py`, keyed on each class's
`name` attribute, not its filename) exposes 57 activities; the catalog offers
23. `person_detection` — plain "a person entered this zone", the most basic
thing an operator asks a camera to watch — was one of the 34 that could not
be selected at all.

Only this one type is added here. The other 33 each need their parameter keys
and defaults read off their own `setup()`/`run()`, which is a separate review
rather than a bulk insert; seeding them with empty schemas would repeat
exactly the mistake 017 was written to undo (`ppe` shipped without
`subcategory_mapping` and was a no-op for as long as it existed).

**`person_classes` default deliberately differs from the code's.**
`person_detection.py` falls back to `["person", "staff"]` when the parameter
is absent. The active detector's label file
(the active detector's label file, referenced by
`config/pgie/config_pgie_yolo_det.txt`) emits no `staff` class — it has
`person` and nothing else person-like — so the default here is `["person"]`.
Same rule 017 applied when it dropped the reference config's `face -> Face
Shield` PPE entry: a catalog default names classes the running model can
actually produce. An operator on a site running a model that *does* emit
`staff` (`09-01-26-trt.txt` has it) can add it back in the AI Config UI; the
parameter is a list field precisely so that is a UI edit, not a migration.

`min_confidence` is the activity's own name for the gate; the code also
accepts a legacy `confidence` alias, which is not exposed — offering two keys
for one threshold in the UI invites setting the one that loses. Default `0.0`
matches the code: no filtering unless the operator asks for it.

downgrade() removes the type. It does **not** rewrite cameras that reference
it — same stance 017 took: a config saved against this type is correct, and
those cameras keep detecting until an operator changes them (they just cannot
be re-saved while the key is missing from the catalog).
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _f(key: str, label: str, kind: str, default: Any, *,
       min: float | None = None, max: float | None = None,
       unit: str | None = None) -> dict:
    return {
        "key": key, "label": label, "kind": kind, "default": default,
        "min": min, "max": max, "unit": unit, "help": None,
    }


_KEY = "person_detection"
_LABEL = "Person detection"
_COLOR = "#4fd1c5"

# Read from activities/person_detection.py's run(); see the module docstring
# for why person_classes drops `staff`.
_SCHEMA = [
    _f("person_classes", "Person classes", "list", ["person"]),
    _f("min_confidence", "Minimum confidence", "number", 0.0, min=0, max=1),
]


def upgrade() -> None:
    conn = op.get_bind()

    exists = conn.execute(
        sa.text("SELECT 1 FROM analytics_activity_types WHERE key = :key"),
        {"key": _KEY},
    ).scalar()

    if exists:
        # Already added by hand through the admin catalog editor. Correct the
        # schema (ours is derived from the activity source) and leave the
        # operator's label/color alone — the rule 017 used for the same case.
        conn.execute(
            sa.text("UPDATE analytics_activity_types "
                    "SET params_schema = CAST(:schema AS jsonb) WHERE key = :key"),
            {"schema": json.dumps(_SCHEMA), "key": _KEY},
        )
        return

    next_sort = conn.execute(
        sa.text("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM analytics_activity_types")
    ).scalar_one()

    conn.execute(
        sa.text("INSERT INTO analytics_activity_types "
                "(key, label, color, sort_order, params_schema) "
                "VALUES (:key, :label, :color, :sort_order, CAST(:schema AS jsonb))"),
        {"key": _KEY, "label": _LABEL, "color": _COLOR,
         "sort_order": next_sort, "schema": json.dumps(_SCHEMA)},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM analytics_activity_types WHERE key = :key"),
        {"key": _KEY},
    )
