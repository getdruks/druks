"""A notification keeps the Druks blocks whose text form is its body, so Slack draws them
with its own blocks when it posts the message and when it edits it. A notification
from before has no blocks and keeps its text."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c4d474a8d72f"
down_revision = "915608b8d048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("blocks", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "blocks")
