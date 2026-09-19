"""vid_jobs + vid_jobs_failed (video pipeline, plan T1)

Revision ID: 0001_video_jobs
Revises:
Create Date: 2026-09-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_video_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vid_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("source_uri", sa.String(512), nullable=False),
        sa.Column("source_sha256", sa.String(64)),
        sa.Column("preset", postgresql.JSONB, nullable=False),
        sa.Column("output_uri", sa.String(512)),
        sa.Column("metadata_json", postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("leased_by", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime),
        sa.Column("heartbeat_at", sa.DateTime),
        sa.Column("lease", sa.Integer),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime),
        sa.CheckConstraint(
            "status IN ('queued','leased','done','failed')",
            name="ck_vid_job_status",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_vid_jobs_idempotency_key"),
    )
    op.create_table(
        "vid_jobs_failed",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("error", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", name="uq_vid_jobs_failed_job"),
    )


def downgrade() -> None:
    op.drop_table("vid_jobs_failed")
    op.drop_table("vid_jobs")
