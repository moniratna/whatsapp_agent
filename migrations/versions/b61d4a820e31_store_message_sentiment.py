"""store sentiment labels on messages

Revision ID: b61d4a820e31
Revises: af4927cb7d21
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b61d4a820e31"
down_revision: str | Sequence[str] | None = "af4927cb7d21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("sentiment", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("sentiment_confidence", sa.Numeric(4, 3), nullable=True))
        batch.create_check_constraint(
            "ck_messages_sentiment",
            "sentiment IS NULL OR sentiment IN ('positive','neutral','negative','frustrated')",
        )
        batch.create_check_constraint(
            "ck_messages_sentiment_confidence",
            "sentiment_confidence IS NULL OR (sentiment_confidence >= 0 AND sentiment_confidence <= 1)",
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("ck_messages_sentiment_confidence", type_="check")
        batch.drop_constraint("ck_messages_sentiment", type_="check")
        batch.drop_column("sentiment_confidence")
        batch.drop_column("sentiment")
