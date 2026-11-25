"""add skill update job state and reviewer evaluations

Revision ID: 2d68a4c7f19b
Revises: b61d4a820e31
Create Date: 2026-11-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d68a4c7f19b"
down_revision: str | Sequence[str] | None = "b61d4a820e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_update_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("application_release", sa.String(length=128), nullable=False),
        sa.Column("reviewer_release", sa.String(length=64), nullable=False),
        sa.Column("conversation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("candidate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','partial','failed')",
            name="ck_skill_update_run_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_skill_update_run_attempt_count"),
        sa.CheckConstraint(
            "conversation_count >= 0", name="ck_skill_update_run_conversation_count"
        ),
        sa.CheckConstraint("candidate_count >= 0", name="ck_skill_update_run_candidate_count"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_update_run_queue", "skill_update_runs", ["status", "created_at"])

    op.create_table(
        "skill_candidate_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_type", sa.String(length=24), nullable=False),
        sa.Column("source_conversation_type", sa.String(length=16), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_release", sa.String(length=64), nullable=True),
        sa.Column("target_skill_key", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="writing", nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("regression_cases_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("source_skill_version", sa.String(length=128), nullable=True),
        sa.Column("source_snapshot_ids_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "candidate_type IN ('customer_skill','reviewer_skill')", name="ck_skill_candidate_type"
        ),
        sa.CheckConstraint(
            "source_conversation_type IS NULL OR source_conversation_type IN "
            "('buyer','seller','portfolio','general','unknown')",
            name="ck_skill_candidate_source_type",
        ),
        sa.CheckConstraint(
            "target_skill_key IS NULL OR target_skill_key IN "
            "('buyer','seller','portfolio','general','unknown','skill_reviewer')",
            name="ck_skill_candidate_target_key",
        ),
        sa.CheckConstraint(
            "(candidate_type = 'customer_skill' AND source_conversation_type IS NOT NULL "
            "AND conversation_id IS NOT NULL AND (target_skill_key IS NULL OR "
            "target_skill_key IN ('buyer','seller','portfolio','general','unknown')) OR "
            "candidate_type = 'reviewer_skill' AND source_conversation_type IS NULL "
            "AND conversation_id IS NULL AND reviewer_release IS NOT NULL "
            "AND target_skill_key = 'skill_reviewer')",
            name="ck_skill_candidate_type_fields",
        ),
        sa.CheckConstraint(
            "status IN ('writing','pending','claimed','merged','rejected',"
            "'cleanup_pending','cleaned')",
            name="ck_skill_candidate_status",
        ),
        sa.CheckConstraint("sequence > 0", name="ck_skill_candidate_sequence"),
        sa.ForeignKeyConstraint(["run_id"], ["skill_update_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relative_path", name="uq_skill_candidate_relative_path"),
        sa.UniqueConstraint(
            "source_conversation_type", "sequence", name="uq_skill_candidate_source_sequence"
        ),
    )
    op.create_index(
        "uq_skill_candidate_reviewer_sequence",
        "skill_candidate_files",
        ["sequence"],
        unique=True,
        postgresql_where=sa.text("candidate_type = 'reviewer_skill'"),
        sqlite_where=sa.text("candidate_type = 'reviewer_skill'"),
    )
    op.create_index(
        "uq_skill_candidate_conversation",
        "skill_candidate_files",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("candidate_type = 'customer_skill'"),
        sqlite_where=sa.text("candidate_type = 'customer_skill'"),
    )
    op.create_index(
        "uq_skill_candidate_reviewer_run",
        "skill_candidate_files",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("candidate_type = 'reviewer_skill' AND run_id IS NOT NULL"),
        sqlite_where=sa.text("candidate_type = 'reviewer_skill' AND run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_skill_candidate_pending",
        "skill_candidate_files",
        ["candidate_type", "source_conversation_type", "status"],
    )

    op.create_table(
        "skill_candidate_sequences",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("last_allocated_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "scope IN ('buyer','seller','portfolio','general','unknown','reviewer')",
            name="ck_skill_candidate_sequence_scope",
        ),
        sa.CheckConstraint(
            "last_allocated_sequence >= 0", name="ck_skill_candidate_sequence_value"
        ),
        sa.PrimaryKeyConstraint("scope"),
    )

    op.create_table(
        "skill_consolidation_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_conversation_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="claimed", nullable=False),
        sa.Column("candidate_ids_json", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("base_skill_hashes_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("target_skill_keys_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("artifact_paths_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("validation_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("active_skill_update_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_conversation_type IN ('buyer','seller','portfolio','general','unknown')",
            name="ck_skill_batch_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('claimed','consolidating','validating','updating_skills',"
            "'completed','cleanup_pending','cleaned','failed')",
            name="ck_skill_batch_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_skill_batch_attempt_count"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_skill_batch_queue",
        "skill_consolidation_batches",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_skill_batch_source_status",
        "skill_consolidation_batches",
        ["source_conversation_type", "status"],
    )

    op.create_table(
        "reviewer_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("suite_version", sa.String(length=128), nullable=False),
        sa.Column("evaluator_release", sa.String(length=128), nullable=False),
        sa.Column("active_reviewer_sha256", sa.String(length=64), nullable=False),
        sa.Column("candidate_reviewer_sha256", sa.String(length=64), nullable=False),
        sa.Column("case_results_json", sa.JSON(), nullable=False),
        sa.Column("aggregate_metrics_json", sa.JSON(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('passed','failed')", name="ck_reviewer_evaluation_decision"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["skill_candidate_files.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", name="uq_reviewer_evaluation_candidate"),
    )
    op.create_index(
        "ix_reviewer_evaluation_decision", "reviewer_evaluations", ["decision", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_reviewer_evaluation_decision", table_name="reviewer_evaluations")
    op.drop_table("reviewer_evaluations")
    op.drop_index("ix_skill_batch_source_status", table_name="skill_consolidation_batches")
    op.drop_index("ix_skill_batch_queue", table_name="skill_consolidation_batches")
    op.drop_table("skill_consolidation_batches")
    op.drop_table("skill_candidate_sequences")
    op.drop_index("ix_skill_candidate_pending", table_name="skill_candidate_files")
    op.drop_index("uq_skill_candidate_reviewer_run", table_name="skill_candidate_files")
    op.drop_index("uq_skill_candidate_conversation", table_name="skill_candidate_files")
    op.drop_index("uq_skill_candidate_reviewer_sequence", table_name="skill_candidate_files")
    op.drop_table("skill_candidate_files")
    op.drop_index("ix_skill_update_run_queue", table_name="skill_update_runs")
    op.drop_table("skill_update_runs")
