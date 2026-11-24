"""add Zoho portfolio record mapping

Revision ID: 3a8bd527e4c1
Revises: 62587594b504
Create Date: 2026-11-24 19:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3a8bd527e4c1"
down_revision: str | Sequence[str] | None = "62587594b504"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("crm_links") as batch:
        batch.add_column(sa.Column("portfolio_asset_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_crm_links_portfolio_asset_id_portfolio_assets",
            "portfolio_assets",
            ["portfolio_asset_id"],
            ["id"],
        )
    op.create_index(
        "uq_crm_portfolio_mapping",
        "crm_links",
        ["portfolio_asset_id", "crm_system"],
        unique=True,
        sqlite_where=sa.text("portfolio_asset_id IS NOT NULL"),
        postgresql_where=sa.text("portfolio_asset_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_crm_portfolio_mapping", table_name="crm_links")
    with op.batch_alter_table("crm_links") as batch:
        batch.drop_constraint(
            "fk_crm_links_portfolio_asset_id_portfolio_assets", type_="foreignkey"
        )
        batch.drop_column("portfolio_asset_id")
