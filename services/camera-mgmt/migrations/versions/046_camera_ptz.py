"""Per-camera ONVIF PTZ capability cache.

Revision ID: 046
Revises: 045
Create Date: 2026-09-14

NUMBERING — RENUMBERED ON MERGE DAY, and the other way round from the recipe
this file used to carry. That recipe was written when the ai branch's chain
ended at 034, and proposed renumbering ai's 033/034 pair upward so this side
could keep the ids "033" and "035". By the time the branches actually met, ai
had grown to 044 — every one of 033..044 a shipped, deployed id sitting in
some appliance's alembic_version. Rewriting twelve of those is not a
renumbering, it is a history rewrite, and it breaks exactly the appliances the
old recipe was trying to protect.

So the two PTZ/dataset migrations moved instead, onto the tail of ai's chain:

    033_datasets   → 045_datasets     revision "045", down_revision "044"
    035_camera_ptz → 046_camera_ptz   revision "046", down_revision "045"

Nothing on the ai side changed: its files, ids and
tests/test_activity_catalog_withdrawal.py (which loads 033/034 BY FILENAME)
are untouched, because the ids they name still mean what they always did.

Consequences for a running appliance:
  * An ai-migrated appliance (alembic_version 044) runs `alembic upgrade head`
    and gets 045 + 046. Nothing is re-run; both are pure DDL.
  * A deep-migrated appliance (alembic_version "035", the OLD id of this file)
    is the only awkward case, and it is a dev box, not a site: "035" now means
    ai's events_fleet_time_index. Such a box already has the datasets tables
    and cameras.ptz_capabilities, so the honest repair is to stamp it onto the
    merged chain and let the ai migrations it is missing apply:
      UPDATE alembic_version SET version_num = '032';
      -- drop the two objects this pair creates, since 045/046 will recreate them:
      DROP TABLE IF EXISTS datasets;   -- and its three indexes, see 045
      ALTER TABLE cameras DROP COLUMN IF EXISTS ptz_capabilities;
      alembic upgrade head

Already pre-solved on this side, no merge-day action needed: the datasets
mount in main.py carries include-level require_authenticated (ai's
test_router_authz_wiring demands every mount be guarded or allowlisted), and
this branch's test files, capability additions and CameraResponse fields all
pass ai's conftest-driven HTTP contract suites as-is.

PTZ was a decorative pad and a "soon" badge; this column is what turns it into
a feature that can only appear on cameras that actually pan.

The product rule is that a PTZ control never shows up on a fixed camera. That
rule needs an answer to "does this camera pan?" at list-render time, for every
camera at once — which rules out asking the device. So the answer is probed
once off the camera and cached here.

NULL is a real and distinct state: "never probed", which is NOT "no PTZ". A
camera whose credentials have never been entered has a NULL here and the UI
says it cannot tell yet, rather than asserting the camera is fixed. Every
existing row starts NULL and nothing about it changes until someone opens the
camera or re-enters its ONVIF credentials.

Shape (see models.Camera.ptz_capabilities and services/onvif_ptz.probe):

    {"supported": false, "reason": "Camera advertises no ONVIF PTZ service"}

    {"supported": true, "profile_token": "Profile_1", "node_token": "PTZNODE_1",
     "continuous": {"pan_tilt": true, "zoom": true},
     "relative":   {"pan_tilt": true, "zoom": false},
     "absolute":   {"pan_tilt": true, "zoom": true},
     "spaces_reported": true,
     "presets": {"supported": true, "max": 255},
     "home": {"supported": true},
     "aux_commands": ["tt:Wiper|On"]}
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# Idempotent on purpose (merge of `field` into the ai chain, 2026-10-07): appliances that ran the
# old `field` numbering already HAVE what this migration creates. backend/migrate.py re-stamps them onto
# the merged chain at 032 and replays forward, so every step that can meet an existing object checks first.
def _insp():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _insp().has_table(name)


def _has_column(table: str, column: str) -> bool:
    return _has_table(table) and any(c["name"] == column for c in _insp().get_columns(table))


def _has_index(table: str, index: str) -> bool:
    return _has_table(table) and any(i["name"] == index for i in _insp().get_indexes(table))

revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    if not _has_column("cameras", "ptz_capabilities"):
        op.add_column(
            "cameras",
            sa.Column("ptz_capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("cameras", "ptz_capabilities")
