"""require agent run for automated messages

Revision ID: c267fb2a2ac9
Revises: 6f4c11ab05c3
Create Date: 2026-11-24 20:40:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c267fb2a2ac9"
down_revision: str | Sequence[str] | None = "6f4c11ab05c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.create_foreign_key(
            "fk_messages_agent_run_id_agent_runs", "agent_runs", ["agent_run_id"], ["id"]
        )
        batch.create_check_constraint(
            "ck_messages_automated_agent_run_required",
            "sender_type != 'automated_agent' OR agent_run_id IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("ck_messages_automated_agent_run_required", type_="check")
        batch.drop_constraint("fk_messages_agent_run_id_agent_runs", type_="foreignkey")
