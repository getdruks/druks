"""An agent keeps its picture as a Druks file. The pasted picture address goes: no release
has it."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3f9c2e481"
down_revision: str | Sequence[str] | None = "d5a152d54a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("picture", sa.String(), nullable=True))
    op.create_foreign_key(
        "agents_picture_fkey", "agents", "files", ["picture"], ["id"], ondelete="RESTRICT"
    )
    op.drop_column("agents", "picture_url")


def downgrade() -> None:
    op.add_column("agents", sa.Column("picture_url", sa.String(), nullable=True))
    op.drop_constraint("agents_picture_fkey", "agents", type_="foreignkey")
    op.drop_column("agents", "picture")
