"""track one-time inactivity reminder per contact

Revision ID: 7c10d4e2a911
Revises: 61c0f77a94b3
Create Date: 2026-11-26 00:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7c10d4e2a911"
down_revision: str | Sequence[str] | None = "61c0f77a94b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("inactivity_reminder_sent_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("contacts", "inactivity_reminder_sent_at")
