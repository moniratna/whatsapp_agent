"""allow rental property requirements

Revision ID: 8a6d3a09f1c2
Revises: 7c10d4e2a911
Create Date: 2026-11-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8a6d3a09f1c2"
down_revision: str | Sequence[str] | None = "7c10d4e2a911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("property_requirements") as batch_op:
        batch_op.drop_constraint("ck_requirement_transaction", type_="check")
        batch_op.create_check_constraint(
            "ck_requirement_transaction", "transaction_type IN ('buy','rent','sell')"
        )


def downgrade() -> None:
    with op.batch_alter_table("property_requirements") as batch_op:
        batch_op.drop_constraint("ck_requirement_transaction", type_="check")
        batch_op.create_check_constraint(
            "ck_requirement_transaction", "transaction_type IN ('buy','sell')"
        )
