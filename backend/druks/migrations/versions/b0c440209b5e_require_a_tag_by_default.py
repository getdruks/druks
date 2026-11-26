"""Require a tag in a thread by default, and drop the account's tag preference."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0c440209b5e"
down_revision: str | Sequence[str] | None = "a7d3f9c2e481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("accounts", "is_tag_required")
    # The old false was the default, and it let untagged replies through for 24 hours
    # only. A row that holds it does not say that its person turned the tag off.
    op.execute("UPDATE chat_conversations SET is_tag_required = true")
    op.alter_column("chat_conversations", "is_tag_required", server_default=sa.true())


def downgrade() -> None:
    op.alter_column("chat_conversations", "is_tag_required", server_default=sa.false())
    op.execute("UPDATE chat_conversations SET is_tag_required = false")
    op.add_column(
        "accounts",
        sa.Column("is_tag_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
