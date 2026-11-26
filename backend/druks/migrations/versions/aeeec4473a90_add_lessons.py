"""Add lessons and the repeat marks of gate corrections.

Revision ID: aeeec4473a90
Revises: b0c440209b5e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aeeec4473a90"
down_revision: str | Sequence[str] | None = "b0c440209b5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lessons",
        sa.Column("text", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "accepted",
                "retired",
                name="lessonstatus",
                native_enum=False,
                create_constraint=True,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.Integer(), nullable=True),
        sa.Column("source_run_id", sa.String(), nullable=True),
        sa.Column("accepted_by_id", sa.String(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["accepted_by_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_event_id"], ["events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_run_id"], ["durable_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "lesson_repeats",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.String(), nullable=False),
        sa.Column("marked_by_id", sa.String(), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["marked_by_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        op.f("ix_lesson_repeats_lesson_id"), "lesson_repeats", ["lesson_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_lesson_repeats_lesson_id"), table_name="lesson_repeats")
    op.drop_table("lesson_repeats")
    op.drop_table("lessons")
