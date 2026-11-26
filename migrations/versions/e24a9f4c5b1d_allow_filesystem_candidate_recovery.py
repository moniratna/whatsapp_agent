"""allow filesystem candidate recovery without conversation rows

Revision ID: e24a9f4c5b1d
Revises: 8a6d3a09f1c2
Create Date: 2026-11-26 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e24a9f4c5b1d"
down_revision: str | Sequence[str] | None = "8a6d3a09f1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("skill_candidate_files") as batch_op:
        batch_op.drop_constraint("ck_skill_candidate_type_fields", type_="check")
        batch_op.create_check_constraint(
            "ck_skill_candidate_type_fields",
            "(candidate_type = 'customer_skill' AND source_conversation_type IS NOT NULL "
            "AND (target_skill_key IS NULL OR target_skill_key IN "
            "('buyer','seller','portfolio','general','unknown')) OR "
            "candidate_type = 'reviewer_skill' AND source_conversation_type IS NULL "
            "AND conversation_id IS NULL AND reviewer_release IS NOT NULL "
            "AND target_skill_key = 'skill_reviewer')",
        )


def downgrade() -> None:
    with op.batch_alter_table("skill_candidate_files") as batch_op:
        batch_op.drop_constraint("ck_skill_candidate_type_fields", type_="check")
        batch_op.create_check_constraint(
            "ck_skill_candidate_type_fields",
            "(candidate_type = 'customer_skill' AND source_conversation_type IS NOT NULL "
            "AND conversation_id IS NOT NULL AND (target_skill_key IS NULL OR "
            "target_skill_key IN ('buyer','seller','portfolio','general','unknown')) OR "
            "candidate_type = 'reviewer_skill' AND source_conversation_type IS NULL "
            "AND conversation_id IS NULL AND reviewer_release IS NOT NULL "
            "AND target_skill_key = 'skill_reviewer')",
        )
