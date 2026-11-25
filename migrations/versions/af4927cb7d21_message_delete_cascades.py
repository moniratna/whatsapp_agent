"""cascade message deletion through dependent records

Revision ID: af4927cb7d21
Revises: 9e3c0ab42f61
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "af4927cb7d21"
down_revision: str | Sequence[str] | None = "9e3c0ab42f61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _replace_foreign_keys(
    table_name: str,
    foreign_keys: list[tuple[str, str, str]],
) -> None:
    """Set on-delete behavior while handling named PostgreSQL and SQLite FKs."""
    inspector = sa.inspect(op.get_bind())
    reflected = inspector.get_foreign_keys(table_name)
    replacements: list[tuple[str, str, str, str]] = []
    for column, referred_table, ondelete in foreign_keys:
        match = next(
            (
                item
                for item in reflected
                if item["constrained_columns"] == [column]
                and item["referred_table"] == referred_table
            ),
            None,
        )
        constraint_name = (
            match["name"]
            if match and match.get("name")
            else f"fk_{table_name}_{column}_{referred_table}"
        )
        replacements.append((constraint_name, column, referred_table, ondelete))

    with op.batch_alter_table(table_name, naming_convention=_FK_NAMING) as batch:
        for constraint_name, _, _, _ in replacements:
            batch.drop_constraint(constraint_name, type_="foreignkey")
        for constraint_name, column, referred_table, ondelete in replacements:
            batch.create_foreign_key(
                constraint_name,
                referred_table,
                [column],
                ["id"],
                ondelete=ondelete,
            )


def upgrade() -> None:
    _replace_foreign_keys(
        "messages",
        [
            ("reply_to_message_id", "messages", "SET NULL"),
            ("agent_run_id", "agent_runs", "CASCADE"),
        ],
    )
    _replace_foreign_keys(
        "agent_runs",
        [
            ("trigger_message_id", "messages", "CASCADE"),
            ("response_message_id", "messages", "SET NULL"),
        ],
    )
    _replace_foreign_keys(
        "agent_run_drafts",
        [("agent_run_id", "agent_runs", "CASCADE")],
    )
    _replace_foreign_keys(
        "message_attachments",
        [("message_id", "messages", "CASCADE")],
    )
    _replace_foreign_keys(
        "message_delivery_events",
        [("message_id", "messages", "CASCADE")],
    )
    _replace_foreign_keys(
        "message_send_attempts",
        [("message_id", "messages", "CASCADE")],
    )
    _replace_foreign_keys(
        "context_facts",
        [("source_message_id", "messages", "CASCADE")],
    )
    _replace_foreign_keys(
        "handover_cases",
        [("trigger_message_id", "messages", "SET NULL")],
    )


def downgrade() -> None:
    _replace_foreign_keys(
        "handover_cases",
        [("trigger_message_id", "messages", "NO ACTION")],
    )
    _replace_foreign_keys(
        "context_facts",
        [("source_message_id", "messages", "NO ACTION")],
    )
    _replace_foreign_keys(
        "message_send_attempts",
        [("message_id", "messages", "NO ACTION")],
    )
    _replace_foreign_keys(
        "message_delivery_events",
        [("message_id", "messages", "NO ACTION")],
    )
    _replace_foreign_keys(
        "message_attachments",
        [("message_id", "messages", "NO ACTION")],
    )
    _replace_foreign_keys(
        "agent_run_drafts",
        [("agent_run_id", "agent_runs", "NO ACTION")],
    )
    _replace_foreign_keys(
        "agent_runs",
        [
            ("trigger_message_id", "messages", "NO ACTION"),
            ("response_message_id", "messages", "NO ACTION"),
        ],
    )
    _replace_foreign_keys(
        "messages",
        [
            ("reply_to_message_id", "messages", "NO ACTION"),
            ("agent_run_id", "agent_runs", "NO ACTION"),
        ],
    )
