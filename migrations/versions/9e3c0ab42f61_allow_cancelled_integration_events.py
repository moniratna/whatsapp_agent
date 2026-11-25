"""allow cancelled integration events

Revision ID: 9e3c0ab42f61
Revises: c267fb2a2ac9
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e3c0ab42f61"
down_revision: str | Sequence[str] | None = "c267fb2a2ac9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("integration_events") as batch:
        batch.drop_constraint("ck_integration_status", type_="check")
        batch.create_check_constraint(
            "ck_integration_status",
            "status IN ('pending','processing','succeeded','retry_wait','dead_letter','ambiguous','cancelled')",
        )


def downgrade() -> None:
    with op.batch_alter_table("integration_events") as batch:
        batch.drop_constraint("ck_integration_status", type_="check")
        batch.create_check_constraint(
            "ck_integration_status",
            "status IN ('pending','processing','succeeded','retry_wait','dead_letter','ambiguous')",
        )
