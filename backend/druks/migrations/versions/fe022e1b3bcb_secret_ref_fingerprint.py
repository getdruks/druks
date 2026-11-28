"""Record the vault row's fingerprint that each box last got, and index the refs by
their vault row."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fe022e1b3bcb"
down_revision: str | Sequence[str] | None = "c7a1e4b93d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sandbox_secret_refs", sa.Column("fingerprint", sa.String(), nullable=True))
    op.create_index(op.f("ix_sandbox_secret_refs_secret_id"), "sandbox_secret_refs", ["secret_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_sandbox_secret_refs_secret_id"), table_name="sandbox_secret_refs")
    op.drop_column("sandbox_secret_refs", "fingerprint")
