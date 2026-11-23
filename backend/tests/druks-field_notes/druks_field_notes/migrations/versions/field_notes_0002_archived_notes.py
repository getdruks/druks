"""archived notes

Revision ID: field_notes_0002
Revises: field_notes_0001
Create Date: 2026-11-22 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "field_notes_0002"
down_revision: str | Sequence[str] | None = "field_notes_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("field_notes_note", sa.Column("archived_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("field_notes_note", "archived_by")
