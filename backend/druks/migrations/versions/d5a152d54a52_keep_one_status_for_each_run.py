"""A run keeps one status notification, and each report of the run replaces it. The status
keeps the end that its run announced, because a send can come before DBOS records that end."""

import sqlalchemy as sa
from alembic import op

revision = "d5a152d54a52"
down_revision = "74e46e4b59dd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("run_end", sa.String()))
    op.create_index(
        "notifications_run_status_idx",
        "notifications",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("reason = 'run.status'"),
    )


def downgrade() -> None:
    op.drop_index("notifications_run_status_idx", table_name="notifications")
    op.drop_column("notifications", "run_end")
