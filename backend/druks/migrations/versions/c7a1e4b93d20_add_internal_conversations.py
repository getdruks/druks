"""Mark a conversation that Druks made for reports as internal."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1e4b93d20"
down_revision: str | Sequence[str] | None = "5f97d76a7605"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("chat_conversations", "is_internal")
