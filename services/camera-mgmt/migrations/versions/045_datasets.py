"""Training-image datasets for the Data Collection page.

Revision ID: 045
Revises: 044
Create Date: 2026-09-11

A DATASET IS A RECIPE, NOT A FOLDER.

The row is "camera X, 10:00-12:00, 200 frames". Collecting fills it in by
sampling that many JPEGs evenly across the window from the NVR's recordings.
Raising 200 to 500 and re-collecting re-samples the SAME window at a finer
interval — no re-recording needed, as long as the footage is still inside
retention. That is why the window and the target live here as first-class
columns rather than being implied by whatever happens to be on disk: the
recipe is the durable thing, and `frames`/`frames_dir` are just the most
recent evaluation of it.

DELETABLE, unlike `evidence_exports` or `audit_log`. These are working images
for model training, not a legal record, so there is no append-only trigger and
no hash chain. Collection and export are still written to the audit log,
because both turn recorded footage into stills that can leave the appliance.

THE 20000 CEILING IS LOAD CONTROL, NOT COSMETICS. Each frame is one
ffmpeg-backed /snapshot on the NVR, and that endpoint decodes from the segment
start on purpose (input seeking returns silently wrong frames on MPEG-TS — see
services/nvr/api/server.py). Measured on this appliance that is 0.18 s one
second into a segment and 2.58 s fifty-five seconds in, so ~1.3 s on average.
At the sampler's concurrency of 4 a 20000-frame target is nearly two hours of
continuous extraction competing with the live recording workers. An unbounded
target would be a self-inflicted denial of service.

No `recurrence` column. A weekly repeat rule was specified, but nothing would
have fired it — scheduled collection does not exist. A column the UI offers
and the backend never honours is worse than its absence, so it is left out
until a scheduler is actually built.
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

revision: str = "045"
down_revision: Union[str, None] = "044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# draft      — created, never collected
# collecting — a background task is sampling right now
# ready      — frames on disk, exportable
# failed     — the run produced nothing, or the worker died mid-run
# cancelled  — reserved; no route sets it yet
STATUSES = ("draft", "collecting", "ready", "failed", "cancelled")


def upgrade() -> None:
    if not _has_table("datasets"):
        op.create_table(
            "datasets",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="draft"),

            # ── The recipe ───────────────────────────────────────────────────────
            # camera_slug is the NVR camera name, which is the registry slug, which
            # is the join key downstream AI pipelines already use. Deliberately NOT
            # a foreign key to cameras.id: a dataset outlives the camera row it was
            # sampled from, and losing the training set because someone
            # decommissioned a camera would be the wrong behaviour.
            sa.Column("camera_slug", sa.Text(), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("target_frames", sa.Integer(), nullable=False),
            sa.Column("jpeg_quality", sa.Integer(), nullable=False, server_default="85"),

            # ── Outcome ──────────────────────────────────────────────────────────
            # `frames` is the manifest: one {filename, timestamp, sha256, bytes}
            # per extracted image. The export reads this, not the directory, so the
            # two can be compared and a half-written run is detectable.
            sa.Column("frames", postgresql.JSONB(), nullable=False,
                      server_default=sa.text("'[]'::jsonb")),
            # Written incrementally during a run (every 10 frames) so the UI can
            # show 47/200 instead of a spinner, and so `updated_at` doubles as a
            # liveness signal for the stale-run reaper.
            sa.Column("frames_collected", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("frames_missing", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("frames_dir", sa.Text(), nullable=True),
            sa.Column("bytes_total", sa.BigInteger(), nullable=True),
            sa.Column("build_error", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(255), nullable=True),

            # ── Label Studio export state — set once a push succeeds ─────────────
            sa.Column("labelstudio_project_id", sa.Integer(), nullable=True),
            sa.Column("labelstudio_task_count", sa.Integer(), nullable=True),
            sa.Column("labelstudio_exported_at", sa.DateTime(timezone=True), nullable=True),

            # ── S3 mirror state — set once an upload succeeds ────────────────────
            # Bucket and prefix are recorded per dataset rather than read from
            # settings, because they are chosen per import in the UI: one appliance
            # can feed several datasets to different destinations. Credentials
            # never appear here.
            sa.Column("s3_bucket", sa.Text(), nullable=True),
            sa.Column("s3_prefix", sa.Text(), nullable=True),
            sa.Column("s3_uploaded_count", sa.Integer(), nullable=True),
            sa.Column("s3_uploaded_at", sa.DateTime(timezone=True), nullable=True),

            sa.CheckConstraint(
                "status IN ('draft','collecting','ready','failed','cancelled')",
                name="datasets_status_check",
            ),
            sa.CheckConstraint("window_end > window_start", name="datasets_window_check"),
            sa.CheckConstraint(
                "target_frames > 0 AND target_frames <= 20000",
                name="datasets_target_check",
            ),
            sa.CheckConstraint("jpeg_quality BETWEEN 1 AND 95", name="datasets_quality_check"),
        )
    if not _has_index("datasets", "ix_datasets_status"):
        op.create_index("ix_datasets_status", "datasets", ["status"])
    if not _has_index("datasets", "ix_datasets_camera"):
        op.create_index("ix_datasets_camera", "datasets", ["camera_slug"])
    if not _has_index("datasets", "ix_datasets_created_at"):
        op.create_index("ix_datasets_created_at", "datasets", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_datasets_created_at", table_name="datasets")
    op.drop_index("ix_datasets_camera", table_name="datasets")
    op.drop_index("ix_datasets_status", table_name="datasets")
    op.drop_table("datasets")
