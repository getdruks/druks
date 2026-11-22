"""A chat reply keeps the druks.ui blocks that its agent showed, so the web chat and a channel
that draws blocks show them after the reply's text. A reply from before has no blocks and
keeps its text."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "74e46e4b59dd"
down_revision = "5e2a9c7d4b18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("blocks", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "blocks")
