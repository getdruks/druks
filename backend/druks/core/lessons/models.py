from datetime import datetime

from sqlalchemy import ForeignKey, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from druks.accounts.models import Account
from druks.core.lessons.constants import LESSON_INDEX_MAX_LINES
from druks.core.lessons.enums import LessonStatus
from druks.core.lessons.exceptions import LessonIndexFull
from druks.core.models import Uuid7Pk
from druks.durable.models import Run
from druks.events.models import Event
from druks.exceptions import ValidationError
from druks.models import Base, Model


class Lesson(Model, Uuid7Pk, ordering=("-accepted_at",)):
    """A judgment that a person accepted. Every later agent call reads it. Its source is
    the gate correction, an event, that the person turned into a lesson."""

    __tablename__ = "lessons"

    # The first line is the lesson's line in the index.
    text: Mapped[str]
    status: Mapped[LessonStatus] = mapped_column(default=LessonStatus.ACCEPTED)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), default=None
    )
    source_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("durable_runs.id", ondelete="SET NULL"), default=None
    )
    accepted_by_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    accepted_by: Mapped[Account] = relationship(lazy="joined", foreign_keys=[accepted_by_id])
    accepted_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def accept(
        cls,
        session: AsyncSession,
        *,
        text: str,
        account: Account,
        source_event_id: int | None = None,
        source_run_id: str | None = None,
    ) -> "Lesson":
        text = text.strip()
        if not text:
            raise ValidationError("A lesson needs text.")
        if source_event_id and not await session.get(Event, source_event_id):
            raise ValidationError(f"No event has the id {source_event_id}.")
        if source_run_id and not await session.get(Run, source_run_id):
            raise ValidationError(
                f"No run has the id {source_run_id}. Check the id, or leave it out."
            )
        accepted_count = await session.scalar(
            select(func.count()).select_from(cls).where(cls.status == LessonStatus.ACCEPTED)
        )
        if accepted_count >= LESSON_INDEX_MAX_LINES:
            raise LessonIndexFull(LESSON_INDEX_MAX_LINES)
        lesson = cls(
            text=text,
            accepted_by_id=account.id,
            source_event_id=source_event_id,
            source_run_id=source_run_id,
        )
        session.add(lesson)
        await session.flush()
        return lesson

    @classmethod
    async def list_accepted(cls, session: AsyncSession) -> list["Lesson"]:
        """The lessons a call reads, oldest first, so the index keeps its order."""
        return list(
            await session.scalars(
                select(cls)
                .where(cls.status == LessonStatus.ACCEPTED)
                .order_by(cls.accepted_at, cls.id)
            )
        )

    async def retire(self, session: AsyncSession) -> None:
        self.status = LessonStatus.RETIRED
        await session.flush()


class LessonRepeat(Model):
    """A person's mark that a gate correction repeats an accepted lesson. The event is
    append-only, so the mark lives beside it. A correction repeats one lesson."""

    __tablename__ = "lesson_repeats"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), index=True)
    marked_by_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    marked_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def mark(
        cls, session: AsyncSession, *, event_id: int, lesson: Lesson, account: Account
    ) -> None:
        """Say that the correction repeats the lesson. A second mark on the same correction
        replaces the first."""
        if lesson.status != LessonStatus.ACCEPTED:
            raise ValidationError("A correction can repeat only an accepted lesson.")
        if not await session.get(Event, event_id):
            raise ValidationError(f"No event has the id {event_id}.")
        await session.merge(
            cls(
                event_id=event_id,
                lesson_id=lesson.id,
                marked_by_id=account.id,
                marked_at=Base.utc_now(),
            )
        )
        await session.flush()
