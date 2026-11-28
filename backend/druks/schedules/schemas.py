from datetime import datetime

from pydantic import ConfigDict

from druks.api.schemas import ScheduledRun
from druks.schemas import Schema

from .models import Schedule


class ScheduleResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    tool: str
    arguments: dict
    cron: str
    next_run_at: datetime


class AgentScheduleResponse(ScheduleResponse):
    """A schedule on its agent's page: who owns it, and its newest due time."""

    account_id: str
    # The title of the group or the room that the schedule reports in. None for a direct
    # chat, and for a thread, which has no title.
    room: str | None
    last_run: ScheduledRun | None

    @classmethod
    def from_schedule(
        cls, schedule: Schedule, *, last_run: ScheduledRun | None
    ) -> "AgentScheduleResponse":
        return cls(
            id=schedule.id,
            account_id=schedule.account_id,
            conversation_id=schedule.conversation_id,
            tool=schedule.tool,
            arguments=schedule.arguments,
            cron=schedule.cron,
            next_run_at=schedule.next_run_at,
            room=schedule.conversation.title if schedule.conversation.thread_id else None,
            last_run=last_run,
        )
