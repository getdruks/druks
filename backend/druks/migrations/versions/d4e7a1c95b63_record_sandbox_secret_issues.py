"""Record what the issuer answers a box for each of its secrets.

Revision ID: d4e7a1c95b63
Revises: b0c440209b5e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7a1c95b63"
down_revision: str | Sequence[str] | None = "b0c440209b5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_secret_issues",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("identity_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("minted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "name"],
            ["sandbox_secret_refs.identity_id", "sandbox_secret_refs.name"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_sandbox_secret_issues_ref", "sandbox_secret_issues", ["identity_id", "name"]
    )


def downgrade() -> None:
    op.drop_table("sandbox_secret_issues")
