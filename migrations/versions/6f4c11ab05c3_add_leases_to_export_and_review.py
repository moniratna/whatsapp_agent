"""add leases to export and review workers

Revision ID: 6f4c11ab05c3
Revises: 3a8bd527e4c1
Create Date: 2026-11-24 20:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f4c11ab05c3"
down_revision: str | Sequence[str] | None = "3a8bd527e4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("transcript_exports", "skill_review_runs"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("lease_owner", sa.String(length=128), nullable=True))
            batch.add_column(
                sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    for table in ("skill_review_runs", "transcript_exports"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("lease_expires_at")
            batch.drop_column("lease_owner")
