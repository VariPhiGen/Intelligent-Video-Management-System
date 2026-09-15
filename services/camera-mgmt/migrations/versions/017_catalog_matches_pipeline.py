"""Make the activity catalog match what the DeepStream pipeline can run.

Revision ID: 017
Revises: 016
Create Date: 2026-07-31

The catalog accumulated two kinds of entry the pipeline cannot act on, and
`ppe` — the one type a camera actually uses — was configured in a way that
made it a no-op. All three are fixed here, because they share one root cause:
the catalog was seeded from documents (migration 014's placeholders, an
activities reference JSON) rather than from the pipeline's activity classes,
which are the only thing that runs.

**1. PPE could never fire.** `activities/ppe.py` builds its detection class
list from `parameters.subcategory_mapping` and flags a person overlapping one
of those classes. Migration 016 gave `ppe` a schema of `frame_accuracy` alone,
so the CMM never emitted a mapping, `ppe_objects` was `{}`, and the per-frame
loop found nothing to iterate — the zone was projected correctly and evaluated
against an empty class list forever. The mapping is added here with the
defaults from the working hand-authored config on the appliance, restricted to
classes the active detector actually emits (the detector's label file
has no `face` class, so the reference config's `face -> Face Shield` entry is
dropped rather than copied).

Note the mapping's semantics before changing the default: keys are the *bare
body part* classes (`head`, `hands`, `foot`) and `no-vest`. A detection means
the PPE item is MISSING; the value is the label the violation is reported
under.

**2. Nine placeholder keys duplicated real activities under invented names.**
Migration 014 seeded generic keys — `fire`, `fall`, `crowd`, `intrusion`,
`unauthorized`, `face`, `phone`, `vehicle`, plus 016's `idle_person` — none of
which is a class name in `python_module/component/activities/`. Selecting one
in AI Config produced a config the pipeline loaded and then dropped with
`Activity class not found`, with nothing surfaced to the operator. Each is
remapped to the activity that actually implements it; three targets were
already in the catalog (`fire_and_smoke`, `person_fallen`, `people_gathering`)
and the rest are inserted below, with parameter keys and defaults read from
each activity's own `setup()`/`run()` — nothing invented or renamed, same rule
migration 016 set.

**3. Nine keys have no implementation at all** (`climbing`, `desk_occupancy`,
`perimeter_monitoring`, `person_violations`, `resource_utilization`,
`time_based_unauthorized_access`, `traffic_overspeeding`,
`traffic_overspeeding_distancewise`, `workplace_area_occupancy`). No pipeline
activity answers to these names and none is a rename of one that does, so they
are removed outright. An operator picking one today gets silence, which is
strictly worse than not offering it.

Stored camera configs are migrated with the catalog, because the type key is
validated against the catalog on save (`update_analytics_config`): leaving a
camera pointing at a deleted key would make its next save 422 with no way for
the operator to see why. Activities on a remapped type move to the replacement
(merging into it if the camera already had both); activities on a removed type
are dropped along with regions that no other activity uses.

Params are **filled, never stripped**: any key the type's schema defines and
the stored params lack is added at its default (this is what actually repairs
`ppe` on existing cameras — the schema change alone only affects the UI for
configs saved afterwards). Keys the schema does not mention are left in place.
They are inert — the pipeline reads parameters by name — and removing them
would mean betting that every schema here is exhaustive for its activity,
which for something like `face_search` (sixteen tunables, six exposed) it
deliberately is not.

downgrade() restores the catalog rows so the table round-trips, but not the
camera configs that referenced them: a config remapped from `intrusion` to
`restricted_zone_entry` is now correct, and reverting it would re-break the
camera to match a catalog nobody wants back.
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same palette 014/016 cycled; only applied to newly inserted types.
_PALETTE = ["#4fd1c5", "#ffb020", "#ef5350", "#b388ff", "#52c77e", "#ff8a65"]


def _f(key: str, label: str, kind: str, default: Any, *,
       min: float | None = None, max: float | None = None,
       unit: str | None = None) -> dict:
    return {
        "key": key, "label": label, "kind": kind, "default": default,
        "min": min, "max": max, "unit": unit, "help": None,
    }


# Bare-body-part classes from the active detector's label file; a detection
# means the corresponding PPE item is absent. Values are the violation labels.
_PPE_SUBCATEGORY = {
    "head": "Helmet",
    "hands": "Safety Gloves",
    "foot": "Safety Shoes",
    "no-vest": "Reflective Jacket",
}

# `ppe`'s corrected schema. frame_accuracy is carried over from 016 unchanged.
_PPE_SCHEMA = [
    _f("frame_accuracy", "Frame accuracy", "number", 200, unit="frames"),
    _f("subcategory_mapping", "PPE classes to watch", "json", _PPE_SUBCATEGORY),
]

# Real activity classes the catalog was missing — the targets the placeholder
# keys below remap onto. Parameter keys/defaults are read from each activity's
# own source, not from the reference JSON (which predates several of them).
_ADDED: list[tuple[str, str, list[dict]]] = [
    # activities/restricted_zone_entry.py
    ("restricted_zone_entry", "Restricted zone entry", [
        _f("cooldown_s", "Cooldown", "number", 60, unit="s"),
    ]),
    # activities/unauthorised_access.py
    ("unauthorised_access", "Unauthorised access", [
        _f("frame_accuracy", "Frame accuracy", "number", 5, unit="frames"),
    ]),
    # activities/face_search.py — exposes the operational tunables; the crop
    # quality gates (blur_min, bright_*, min_box_*) keep their code defaults.
    ("face_search", "Face search", [
        _f("face_search_url", "Face search URL", "text", "http://localhost:8010/collect"),
        _f("tracked_classes", "Tracked classes", "list", ["face"]),
        _f("min_confidence", "Minimum confidence", "number", 0.90, min=0, max=1),
        _f("frame_accuracy", "Frame accuracy", "number", 5, unit="frames"),
        _f("cooldown_s", "Cooldown", "number", 3600, unit="s"),
        _f("persist", "Persist matches", "bool", True),
    ]),
    # activities/phone_usage_restricted_area.py — detector-only.
    ("phone_usage_restricted_area", "Phone usage (restricted area)", [
        _f("phone_classes", "Phone classes", "list",
           ["phone", "mobile", "cell phone", "mobile_phone", "smartphone"]),
        _f("min_confidence", "Minimum confidence", "number", 0.3, min=0, max=1),
        _f("cooldown_s", "Cooldown", "number", 60, unit="s"),
    ]),
    # activities/phone_usage_verified.py — same event, confirmed by the VLM
    # service before it is raised. Already in use by a hand-authored config.
    ("phone_usage_verified", "Phone usage (verified)", [
        _f("dwell_threshold_s", "Dwell threshold", "number", 40, unit="s"),
        _f("snapshot_interval_s", "Snapshot interval", "number", 2, unit="s"),
        _f("max_crops", "Max crops", "number", 20),
        _f("service_url", "Verification service URL", "text", "http://localhost:8600"),
        _f("request_timeout_s", "Request timeout", "number", 15, unit="s"),
    ]),
    # activities/car_detection.py
    ("car_detection", "Car detection", [
        _f("car_classes", "Car classes", "list", ["car"]),
        _f("min_count", "Minimum count", "number", 1),
        _f("emit_every_n_frames", "Emit every N frames", "number", 30, unit="frames"),
    ]),
    # activities/idle_worker.py — reads idle_threshold WITHOUT a default
    # (KeyError if absent), so this field is what makes the type usable.
    ("idle_worker", "Idle worker", [
        _f("idle_threshold", "Idle threshold", "number", 600, unit="s"),
    ]),
]

# Placeholder key -> the activity class that implements it, or None when the
# pipeline has nothing of the kind and the key is simply withdrawn.
_REPLACEMENTS: dict[str, str | None] = {
    "fire": "fire_and_smoke",
    "fall": "person_fallen",
    "crowd": "people_gathering",
    "intrusion": "restricted_zone_entry",
    "unauthorized": "unauthorised_access",
    "face": "face_search",
    "phone": "phone_usage_restricted_area",
    "vehicle": "car_detection",
    "idle_person": "idle_worker",
    "climbing": None,
    "desk_occupancy": None,
    "perimeter_monitoring": None,
    "person_violations": None,
    "resource_utilization": None,
    "time_based_unauthorized_access": None,
    "traffic_overspeeding": None,
    "traffic_overspeeding_distancewise": None,
    "workplace_area_occupancy": None,
}

# Restored verbatim by downgrade() so the table round-trips. (key, label,
# color, params_schema) — 014's nine minus `ppe`, which survives 017, plus
# 016's implementation-less additions.
_REMOVED_ROWS: list[tuple[str, str, str, list[dict]]] = [
    ("fire", "Fire & Smoke", "#ef5350", []),
    ("unauthorized", "Unauthorized Access", "#4fd1c5", []),
    ("intrusion", "Intrusion", "#b388ff", []),
    ("fall", "Fall Detection", "#52c77e", []),
    ("crowd", "Crowd", "#ff8a65", []),
    ("face", "Face Recognition", "#64b5f6", []),
    ("phone", "Phone Usage", "#f06292", []),
    ("vehicle", "Vehicle", "#a1887f", []),
    ("idle_person", "Idle person", "#4fd1c5",
     [_f("idle_threshold", "Idle threshold", "number", 120, unit="s")]),
    ("desk_occupancy", "Desk occupancy", "#ffb020", []),
    ("resource_utilization", "Resource utilization", "#ef5350", []),
    ("time_based_unauthorized_access", "Time-based unauthorized access", "#b388ff", []),
    ("workplace_area_occupancy", "Workplace area occupancy", "#52c77e", []),
    ("person_violations", "Person violations", "#ff8a65", []),
    ("perimeter_monitoring", "Perimeter monitoring", "#4fd1c5", []),
    ("climbing", "Climbing", "#ffb020", []),
    ("traffic_overspeeding", "Traffic overspeeding", "#ef5350",
     [_f("speed_threshold", "Speed threshold", "number", 60)]),
    ("traffic_overspeeding_distancewise", "Traffic overspeeding (distance)", "#b388ff",
     [_f("speed_threshold", "Speed threshold", "number", 60),
      _f("distance_threshold", "Distance threshold", "number", 100)]),
]


def _migrate_activities(
    slug: str, activities: list[dict], schemas: dict[str, list[dict]]
) -> tuple[list[dict], bool]:
    """Remap/drop activities by type, then fill in missing param defaults.

    Returns the new list and whether anything changed. Merging matters when a
    camera already had BOTH a placeholder and its replacement (e.g. `fire` and
    `fire_and_smoke`): the grouped shape migration 015 established allows only
    one entry per type, so the placeholder's regions are folded into the
    existing entry rather than producing a duplicate the UI cannot render.
    """
    out: list[dict] = []
    by_type: dict[str, dict] = {}
    changed = False

    for act in activities:
        atype = act.get("type")
        if atype in _REPLACEMENTS:
            replacement = _REPLACEMENTS[atype]
            changed = True
            if replacement is None:
                print(f"[017] camera {slug}: dropping activity '{atype}' "
                      "(no pipeline implementation)")
                continue
            print(f"[017] camera {slug}: '{atype}' -> '{replacement}'")
            act = dict(act, type=replacement)
            atype = replacement

        if atype in by_type:
            # Fold into the entry already holding this type.
            target = by_type[atype]
            for rid in act.get("regions") or []:
                if rid not in target["regions"]:
                    target["regions"].append(rid)
            print(f"[017] camera {slug}: merged duplicate '{atype}' "
                  "(params of the first entry kept)")
            continue

        act = dict(act)
        act["regions"] = list(act.get("regions") or [])
        by_type[atype] = act
        out.append(act)

    # Fill schema defaults. Existing values always win — this repairs configs
    # saved before a schema gained a field (the `ppe` case) without reverting
    # anything an operator deliberately set.
    for act in out:
        schema = schemas.get(act.get("type")) or []
        params = dict(act.get("params") or {})
        for field in schema:
            key = field.get("key")
            if key and key not in params:
                params[key] = field.get("default")
                changed = True
        act["params"] = params

    return out, changed


def _prune_regions(cfg: dict) -> bool:
    """Drop regions no surviving activity references. Returns whether it did.

    Only reached when an activity was removed: a region left behind still
    draws on the operator's zone editor, implying analysis that no longer
    happens anywhere.
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

    # ── 1. Catalog: fix `ppe`, add the missing real types ────────────────────
    conn.execute(
        sa.text("UPDATE analytics_activity_types "
                "SET params_schema = CAST(:schema AS jsonb) WHERE key = 'ppe'"),
        {"schema": json.dumps(_PPE_SCHEMA)},
    )

    existing = set(
        conn.execute(sa.text("SELECT key FROM analytics_activity_types")).scalars().all()
    )
    next_sort = conn.execute(
        sa.text("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM analytics_activity_types")
    ).scalar_one()

    palette_i = 0
    for key, label, schema in _ADDED:
        if key in existing:
            # Already present (a re-run, or an admin added it by hand): only
            # the schema is ours to correct, same rule migration 016 used.
            conn.execute(
                sa.text("UPDATE analytics_activity_types "
                        "SET params_schema = CAST(:schema AS jsonb) WHERE key = :key"),
                {"schema": json.dumps(schema), "key": key},
            )
            continue
        conn.execute(
            sa.text("INSERT INTO analytics_activity_types "
                    "(key, label, color, sort_order, params_schema) "
                    "VALUES (:key, :label, :color, :sort_order, CAST(:schema AS jsonb))"),
            {
                "key": key, "label": label,
                "color": _PALETTE[palette_i % len(_PALETTE)],
                "sort_order": next_sort, "schema": json.dumps(schema),
            },
        )
        palette_i += 1
        next_sort += 1

    # ── 2. Camera configs: remap/drop, then backfill params ──────────────────
    # Read the schemas back so the backfill uses whatever is actually stored,
    # including any an admin edited — not this file's idea of them.
    schemas = {
        key: (json.loads(schema) if isinstance(schema, str) else schema) or []
        for key, schema in conn.execute(
            sa.text("SELECT key, params_schema FROM analytics_activity_types")
        ).fetchall()
    }

    rows = conn.execute(
        sa.text("SELECT id, slug, analytics_config FROM cameras "
                "WHERE analytics_config IS NOT NULL")
    ).fetchall()

    for row_id, slug, cfg in rows:
        if isinstance(cfg, str):
            cfg = json.loads(cfg) if cfg else {}
        if not cfg or not cfg.get("activities"):
            continue

        new_cfg = dict(cfg)
        new_cfg["regions"] = dict(cfg.get("regions") or {})
        new_cfg["activities"], changed = _migrate_activities(
            slug, cfg["activities"], schemas
        )
        if _prune_regions(new_cfg):
            changed = True
        if not changed:
            continue

        conn.execute(
            sa.text("UPDATE cameras SET analytics_config = :cfg WHERE id = :id"),
            {"cfg": json.dumps(new_cfg), "id": row_id},
        )

    # ── 3. Catalog: withdraw the keys nothing implements ─────────────────────
    # Last, so the loop above could still read a schema for a key it remapped.
    conn.execute(
        sa.text("DELETE FROM analytics_activity_types WHERE key = ANY(:keys)"),
        {"keys": list(_REPLACEMENTS)},
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("DELETE FROM analytics_activity_types WHERE key = ANY(:keys)"),
        {"keys": [key for key, _, _ in _ADDED]},
    )
    conn.execute(
        sa.text("UPDATE analytics_activity_types SET params_schema = CAST(:schema AS jsonb) "
                "WHERE key = 'ppe'"),
        {"schema": json.dumps([_f("frame_accuracy", "Frame accuracy", "number", 200,
                                  unit="frames")])},
    )

    next_sort = conn.execute(
        sa.text("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM analytics_activity_types")
    ).scalar_one()
    for key, label, color, schema in _REMOVED_ROWS:
        conn.execute(
            sa.text("INSERT INTO analytics_activity_types "
                    "(key, label, color, sort_order, params_schema) "
                    "VALUES (:key, :label, :color, :sort_order, CAST(:schema AS jsonb)) "
                    "ON CONFLICT (key) DO NOTHING"),
            {"key": key, "label": label, "color": color,
             "sort_order": next_sort, "schema": json.dumps(schema)},
        )
        next_sort += 1
