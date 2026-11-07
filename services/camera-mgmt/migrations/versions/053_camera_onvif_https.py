"""Per-camera ONVIF HTTPS-required flag.

RENUMBERED on the field -> ai merge (was 036); see backend/migrate.py.

Revision ID: 053
Revises: 052
Create Date: 2026-09-19

Most cameras answer plain-HTTP ONVIF and this column stays NULL forever. A
minority of firmwares reject at least one ONVIF operation over HTTP with a
SOAP TransModeError ("client may not support HTTPS mode") -- confirmed live
on a device whose GetCapabilities worked over both schemes but
GetSystemDateAndTime (fired by every ONVIF client build via adjust_time)
only answered over HTTPS, making PTZ/encoder/imaging/OSD 502 while the RTSP
stream (a separate, scheme-less path) played fine.

NULL/False is "use plain HTTP" (today's only behaviour, unchanged for every
existing row) -- see models.Camera.onvif_use_https and models.onvif_host().
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


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

revision: str = "053"
down_revision: Union[str, None] = "052"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    if not _has_column("cameras", "onvif_use_https"):
        op.add_column(
            "cameras",
            sa.Column("onvif_use_https", sa.Boolean(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("cameras", "onvif_use_https")
