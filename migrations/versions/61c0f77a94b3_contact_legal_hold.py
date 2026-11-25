"""exclude legal hold contacts from automated skill updates

Revision ID: 61c0f77a94b3
Revises: 34ef51bce890
Create Date: 2026-11-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "61c0f77a94b3"
down_revision: str | Sequence[str] | None = "34ef51bce890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("legal_hold", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("contacts", "legal_hold")
