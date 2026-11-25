"""add idempotent reviewer update lane runs

Revision ID: 34ef51bce890
Revises: cd25f0e914a1
Create Date: 2026-11-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "34ef51bce890"
down_revision: str | Sequence[str] | None = "cd25f0e914a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reviewer_update_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_batch_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("suite_version", sa.String(length=128), nullable=False),
        sa.Column("active_reviewer_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_reviewer_sha256", sa.String(length=64), nullable=True),
        sa.Column("evaluation_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("base_skill_hash", sa.String(length=64), nullable=True),
        sa.Column("backup_relative_path", sa.String(length=256), nullable=True),
        sa.Column("backup_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued','no_change','candidate_pending','evaluating','applying',"
            "'cleanup_pending','completed','failed')",
            name="ck_reviewer_update_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["customer_batch_id"], ["skill_consolidation_batches.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["skill_candidate_files.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_batch_id", name="uq_reviewer_update_customer_batch"),
        sa.UniqueConstraint("candidate_id", name="uq_reviewer_update_candidate"),
    )
    op.create_index("ix_reviewer_update_queue", "reviewer_update_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_reviewer_update_queue", table_name="reviewer_update_runs")
    op.drop_table("reviewer_update_runs")
