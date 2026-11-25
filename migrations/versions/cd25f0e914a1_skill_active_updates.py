"""persist recoverable local skill update transactions

Revision ID: cd25f0e914a1
Revises: ab47cc319f52
Create Date: 2026-11-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd25f0e914a1"
down_revision: str | Sequence[str] | None = "ab47cc319f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_active_updates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("target_skill_keys_json", sa.JSON(), nullable=False),
        sa.Column("base_skill_hashes_json", sa.JSON(), nullable=False),
        sa.Column("proposed_skill_hashes_json", sa.JSON(), nullable=False),
        sa.Column("applied_skill_hashes_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("backup_artifacts_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("attempts_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('prepared','applying','applied','rolled_back',"
            "'rollback_required','failed')",
            name="ck_skill_active_update_status",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["skill_consolidation_batches.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", name="uq_skill_active_update_batch"),
    )
    op.create_index(
        "ix_skill_active_update_status",
        "skill_active_updates",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_active_update_status", table_name="skill_active_updates")
    op.drop_table("skill_active_updates")
