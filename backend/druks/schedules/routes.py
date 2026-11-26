from typing import Annotated

from dbos import DBOS
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request

from druks.accounts.context import current_conversation_id
from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.exceptions import PermissionDenied
from druks.notifications.services import require_bot_in_channel, require_person_in_channel

from .models import Schedule
from .schemas import ScheduleResponse
from .service import apply_schedule

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


async def require_schedule_stopper(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be the schedule's owner or an admin. The route itself answers a
    request for an unknown schedule."""
    schedule = await session.get(Schedule, request.path_params["schedule_id"])
    if schedule and not account.can_stop(schedule):
        raise PermissionDenied("Only the schedule's owner or an admin can stop it.")


@router.post(
    "",
    operation_id="create_schedule",
    status_code=201,
    tags=["agent"],
    response_model=ScheduleResponse,
    response_model_by_alias=True,
)
async def create_schedule(
    request: Request,
    session: SessionDep,
    tool: Annotated[str, Body(description="The name of one of your tools to call.")],
    arguments: Annotated[
        dict,
        Body(description="The tool's arguments, the same at every call. Give {} for none."),
    ],
    cron: Annotated[str, Body(description="A cron for when to call, local time.")],
    channel: Annotated[
        str,
        Body(
            description="A Slack channel where each run of chat_run_instruction posts its "
            "answer. Give its id, such as C0123ABCD: a mention <#C0123ABCD|eng> holds it. "
            "With no id, give its name, such as #eng. Leave it out to post in no channel."
        ),
    ] = "",
    account: Account = Depends(current_account),
) -> Schedule:
    """Call one of your tools again and again on a cron, as the person in this chat. For
    example, schedule a tool that starts a run. When none of your tools does the task,
    schedule chat_run_instruction with the task in words. Each call goes through the same
    checks as when you call the tool. Its runs report to this chat. With channel,
    each run also posts its answer in that channel. The bot and the person must both be
    members of it. Local time is the installation timezone. The answer's nextRunAt is
    local. Tell the person that time as it is. When Druks refuses the time, ask the person
    rather than guess."""
    conversation_id = current_conversation_id.get()
    if not conversation_id:
        raise HTTPException(422, "This call comes from no chat. Make a schedule from a chat.")
    if channel:
        channel = await require_bot_in_channel(channel)
        await require_person_in_channel(account, channel)
    schedule = await Schedule.create(
        session,
        account_id=account.id,
        conversation_id=conversation_id,
        tool=tool,
        arguments=arguments,
        cron=cron,
        channel_id=channel,
        tool_names=request.app.state.agent_tools.values(),
    )
    await apply_schedule(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


@router.get(
    "",
    operation_id="list_schedules",
    tags=["agent"],
    response_model=list[ScheduleResponse],
    response_model_by_alias=True,
)
async def list_schedules(
    session: SessionDep, account: Account = Depends(current_account)
) -> list[Schedule]:
    """Your schedules, newest first: each one's conversation, tool, arguments, cron, Slack
    channel, and next time."""
    return await Schedule.list_for_account(session, account.id)


@router.put(
    "/{schedule_id}",
    operation_id="change_schedule",
    tags=["agent"],
    response_model=ScheduleResponse,
    response_model_by_alias=True,
)
async def change_schedule(
    request: Request,
    session: SessionDep,
    schedule_id: Annotated[str, Path(description="A schedule from list_schedules.")],
    tool: Annotated[str, Body(description="The name of one of your tools to call.")],
    arguments: Annotated[
        dict,
        Body(description="The tool's arguments, the same at every call. Give {} for none."),
    ],
    cron: Annotated[str, Body(description="A cron for when to call, local time.")],
    channel: Annotated[
        str,
        Body(
            description="A Slack channel where each run of chat_run_instruction posts its "
            "answer. Give its id, such as C0123ABCD: a mention <#C0123ABCD|eng> holds it. "
            'With no id, give its name, such as #eng. Give "" to post in no channel.'
        ),
    ],
    account: Account = Depends(current_account),
) -> Schedule:
    """Change one of your schedules. Give all four: the tool, its arguments, the cron, and
    the Slack channel. They replace the old ones."""
    schedule = await Schedule.get_for_account(session, schedule_id, account.id)
    if not schedule:
        raise HTTPException(404, "Schedule not found. Choose one from list_schedules.")
    # The bot or the person can leave the channel later. A change that gives the saved id
    # still saves.
    if channel and channel != schedule.channel_id:
        channel = await require_bot_in_channel(channel)
        await require_person_in_channel(account, channel)
    schedule.change(
        tool=tool,
        arguments=arguments,
        cron=cron,
        channel_id=channel,
        tool_names=request.app.state.agent_tools.values(),
    )
    await apply_schedule(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


@router.delete(
    "/{schedule_id}",
    operation_id="stop_schedule",
    status_code=204,
    tags=["agent"],
    dependencies=[Depends(require_schedule_stopper)],
)
async def stop_schedule(
    session: SessionDep,
    schedule_id: Annotated[str, Path(description="A schedule from list_schedules.")],
) -> None:
    """Stop one of your schedules. The runs its calls already started go on."""
    schedule = await session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "Schedule not found. Choose one from list_schedules.")
    await DBOS.delete_schedule_async(schedule.schedule_name)
    await session.delete(schedule)
    await session.commit()
