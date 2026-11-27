"""Compare and key project repo names without regard to case."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5f97d76a7605"
down_revision: str | Sequence[str] | None = "b0c440209b5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("project_repos", "full_name", type_=postgresql.CITEXT())


def downgrade() -> None:
    op.alter_column("project_repos", "full_name", type_=sa.String())
