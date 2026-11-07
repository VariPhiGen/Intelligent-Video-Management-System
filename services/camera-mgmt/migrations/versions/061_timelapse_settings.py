"""Time-lapse site defaults + disk guard (one row), and cameras store only what they change.

Until now every camera stored its WHOLE time-lapse config, written out in full on
the first save, so there was no such thing as "the default" after day one: a
site that wanted every camera on 10 minutes had to edit every camera. Now:

  * timelapse_settings (id=1) holds the site's defaults, the disk-guard
    thresholds and the "paused by the disk guard" remark;
  * cameras.timelapse_config holds only the keys a camera changes on top of the
    defaults — so this migration rewrites every stored config down to the keys
    that differ from the built-in defaults (the site defaults start out equal to
    those, so nothing any camera captures changes).

BUILTIN below is TimelapseConfig() as of this migration, copied rather than
imported: a migration must keep meaning the same thing after the model changes.

Revision ID: 061
Revises: 060
"""
import json
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "061"
down_revision: Union[str, None] = "060"
branch_labels = None
depends_on = None

BUILTIN = {
    "interval_s": 300,
    "schedule": {"mode": "weekly", "rules": [{"days": [0, 1, 2, 3, 4, 5], "start": "07:00", "end": "19:00"}]},
    "skip_dark": True, "dark_threshold": 18, "skip_unchanged": False, "unchanged_threshold": 2.0,
    "ptz_preset_only": None, "ptz_tolerance": 0.02, "resolution": "1080p", "jpeg_quality": 80,
    "retention_days": 90, "max_storage_mb": 5120, "auto_render_daily": False, "render_fps": 24,
    "render_overlay": True, "render_deflicker": False,
}


def _overrides(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in BUILTIN or BUILTIN[k] != v}


def upgrade() -> None:
    bind = op.get_bind()
    if "timelapse_settings" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "timelapse_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("defaults", postgresql.JSONB(), nullable=True),
            sa.Column("guard", postgresql.JSONB(), nullable=True),
            sa.Column("paused", postgresql.JSONB(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_by", sa.String(255), nullable=True),
        )
    rows = bind.execute(sa.text("SELECT id, timelapse_config FROM cameras "
                                "WHERE timelapse_config IS NOT NULL AND timelapse_config::text <> '{}'")).fetchall()
    for cam_id, cfg in rows:
        cfg = cfg if isinstance(cfg, dict) else json.loads(cfg or "{}")
        slim = _overrides(cfg)
        if slim != cfg:
            bind.execute(sa.text("UPDATE cameras SET timelapse_config = CAST(:c AS jsonb) WHERE id = :i"),
                         {"c": json.dumps(slim), "i": cam_id})


def downgrade() -> None:
    # Write the full configs back, so an older build (which reads a stored config as complete) captures the same.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, timelapse_config FROM cameras")).fetchall()
    for cam_id, cfg in rows:
        cfg = cfg if isinstance(cfg, dict) else json.loads(cfg or "{}")
        if cfg:
            bind.execute(sa.text("UPDATE cameras SET timelapse_config = CAST(:c AS jsonb) WHERE id = :i"),
                         {"c": json.dumps({**BUILTIN, **cfg}), "i": cam_id})
    op.drop_table("timelapse_settings")
