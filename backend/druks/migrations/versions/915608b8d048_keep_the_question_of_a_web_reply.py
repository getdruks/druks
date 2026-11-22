"""A web chat's reply keeps the question that its agent asked, so the web chat shows the
question's choices or fields after the turn's record of the question is gone."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "915608b8d048"
down_revision = "c9100c395625"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("question", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "question")
