from collections.abc import Collection
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import CroniterBadDateError, croniter
from sqlalchemy import ForeignKey, Index, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from druks.chat.models import Conversation
from druks.core.models import Uuid7Pk
from druks.exceptions import ValidationError
from druks.models import Base
from druks.settings import get_settings

from .constants import SCHEDULE_NAME_PREFIX


class Schedule(Base, Uuid7Pk):
    """A tool call that a person asked an agent to make on a cron. DBOS fires each due
    time, and Druks makes the call as the person, from the conversation it was set in."""

    __tablename__ = "schedules"
    __table_args__ = (Index("schedules_account_idx", "account_id", "created_at"),)

    # The account that asked for the schedule. Each call runs as this account. Only it
    # changes the schedule, and only it or an admin stops it.
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE")
    )
    # The tool's name and arguments, as the agent calls it.
    tool: Mapped[str]
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # A cron in the installation timezone.
    cron: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=Base.utc_now)

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        conversation_id: str,
        tool: str,
        arguments: dict[str, Any],
        cron: str,
        tool_names: Collection[str],
    ) -> "Schedule":
        cls._check_tool(tool, tool_names)
        cls._check_cron(cron)
        schedule = cls(
            account_id=account_id,
            conversation_id=conversation_id,
            tool=tool,
            arguments=arguments,
            cron=cron,
        )
        session.add(schedule)
        await session.flush()
        return schedule

    def change(
        self, *, tool: str, arguments: dict[str, Any], cron: str, tool_names: Collection[str]
    ) -> None:
        """Set the tool, its arguments and the cron."""
        self._check_tool(tool, tool_names)
        self._check_cron(cron)
        self.tool = tool
        self.arguments = arguments
        self.cron = cron

    @staticmethod
    def _check_tool(tool: str, tool_names: Collection[str]) -> None:
        """The tool must be one of ``tool_names``."""
        if tool not in tool_names:
            raise ValidationError(f"No tool is named '{tool}'. Choose one of your tools.")

    @staticmethod
    def _check_cron(cron: str) -> None:
        """The cron must have five fields and a due time."""
        # DBOS reads a sixth field as seconds at the start, croniter as seconds at the end.
        if len(cron.split()) != 5 or not croniter.is_valid(cron):
            raise ValidationError(
                f"'{cron}' is not a cron. Write five fields, such as '0 9 * * 1'."
            )
        try:
            croniter(cron).get_next(datetime)
        except CroniterBadDateError as error:
            raise ValidationError(
                f"'{cron}' never comes due. Write a date that exists, such as '0 9 28 2 *'."
            ) from error

    @classmethod
    async def list_for_account(cls, session: AsyncSession, account_id: str) -> list["Schedule"]:
        return list(
            await session.scalars(
                select(cls)
                .where(cls.account_id == account_id)
                .order_by(cls.created_at.desc(), cls.id.desc())
            )
        )

    @classmethod
    async def get_for_account(
        cls, session: AsyncSession, schedule_id: str, account_id: str
    ) -> "Schedule | None":
        return await session.scalar(
            select(cls).where(cls.id == schedule_id, cls.account_id == account_id)
        )

    @classmethod
    async def list_for_agent(cls, session: AsyncSession, agent_id: str) -> list["Schedule"]:
        """The schedules set in the agent's conversations, newest first."""
        return list(
            await session.scalars(
                select(cls)
                .join(Conversation, Conversation.id == cls.conversation_id)
                .where(Conversation.agent_id == agent_id)
                .order_by(cls.created_at.desc(), cls.id.desc())
            )
        )

    @property
    def schedule_name(self) -> str:
        """The name of the DBOS schedule that fires this schedule's due times."""
        return f"{SCHEDULE_NAME_PREFIX}{self.id}"

    @property
    def next_run_at(self) -> datetime:
        """When the schedule calls its tool next, in the installation timezone."""
        return croniter(
            self.cron, Base.utc_now().astimezone(ZoneInfo(get_settings().timezone))
        ).get_next(datetime)
