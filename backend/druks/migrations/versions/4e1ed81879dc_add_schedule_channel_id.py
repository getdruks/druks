"""A schedule can name the Slack channel where each of its runs posts its answer."""

import sqlalchemy as sa
from alembic import op

revision = "4e1ed81879dc"
down_revision = "a7d3f9c2e481"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedules", sa.Column("channel_id", sa.String()))


def downgrade() -> None:
    op.drop_column("schedules", "channel_id")
