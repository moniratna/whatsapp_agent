"""add normalized customer candidate fingerprints

Revision ID: ab47cc319f52
Revises: 2d68a4c7f19b
Create Date: 2026-11-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab47cc319f52"
down_revision: str | Sequence[str] | None = "2d68a4c7f19b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("skill_candidate_files", sa.Column("content_fingerprint", sa.String(64)))
    op.create_index(
        "uq_skill_candidate_customer_fingerprint",
        "skill_candidate_files",
        ["content_fingerprint"],
        unique=True,
        postgresql_where=sa.text(
            "candidate_type = 'customer_skill' AND content_fingerprint IS NOT NULL "
            "AND status != 'rejected'"
        ),
        sqlite_where=sa.text(
            "candidate_type = 'customer_skill' AND content_fingerprint IS NOT NULL "
            "AND status != 'rejected'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_skill_candidate_customer_fingerprint", table_name="skill_candidate_files"
    )
    op.drop_column("skill_candidate_files", "content_fingerprint")
