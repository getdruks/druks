"""Add agents, a conversation's agent, and schedules.

Revision ID: 5e2a9c7d4b18
Revises: c4d474a8d72f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5e2a9c7d4b18"
down_revision: str | Sequence[str] | None = "c4d474a8d72f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("instructions", sa.String(), nullable=False),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("harness", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("is_private", sa.Boolean(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("picture_url", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", name="agents_channel_id_key"),
    )
    op.create_index("agents_name_lower_key", "agents", [sa.text("lower(name)")], unique=True)
    op.add_column("chat_conversations", sa.Column("agent_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "chat_conversations_agent_id_fkey",
        "chat_conversations",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "chat_conversations_agent_idx",
        "chat_conversations",
        ["agent_id"],
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id", "thread_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cron", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["conversation_id"], ["chat_conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("schedules_account_idx", "schedules", ["account_id", "created_at"])


def downgrade() -> None:
    op.drop_index("schedules_account_idx", table_name="schedules")
    op.drop_table("schedules")
    op.drop_index("chat_conversations_user_idx", table_name="chat_conversations")
    op.create_index(
        "chat_conversations_user_idx",
        "chat_conversations",
        ["connection_id", "account_id", "user_id", "thread_id"],
        unique=True,
        postgresql_where=sa.text("connection_id IS NOT NULL"),
    )
    op.drop_index("chat_conversations_agent_idx", table_name="chat_conversations")
    op.drop_constraint("chat_conversations_agent_id_fkey", "chat_conversations", type_="foreignkey")
    op.drop_column("chat_conversations", "agent_id")
    op.drop_table("agents")
