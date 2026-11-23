import logging
from datetime import datetime
from typing import TYPE_CHECKING

from dbos import DBOS, DebouncerClient, EnqueueOptions, StepOptions
from dbos import error as dbos_error
from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.models import Conversation
from druks.durable.constants import NOTIFICATIONS_QUEUE
from druks.durable.engine import enqueue_in_transaction, get_dbos_client, step_session
from druks.durable.models import Run
from druks.locks import lock
from druks.notifications import constants, exceptions
from druks.notifications.models import Notification
from druks.registry import channel_registry
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import get_settings

if TYPE_CHECKING:
    from druks.chat.channels.base import Channel

logger = logging.getLogger(__name__)

# Delivery retries on its own schedule, fully decoupled from run lifecycles: a
# flaky provider endpoint must never retry (or wedge) a run's steps, so the
# outbox gets its own queue + workflow instead of riding RUN_QUEUE. The
# workflow must exist before DBOS.launch(); the queue is registered after.

_SEND_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


@DBOS.workflow(name=constants.SEND_WORKFLOW)
async def send_notification(notification_id: str) -> None:
    async def _send() -> None:
        async with step_session() as session:
            notification = await session.get(Notification, notification_id)
            is_status = notification.reason == constants.STATUS_REASON
            # A duplicate enqueue or the replay of a completed send finds the
            # row delivered and stops. At-least-once: a crash after the send but
            # before this step checkpoints re-posts once on recovery — the click
            # side is idempotent, so a duplicate message is the accepted cost. A
            # status sends each time, to show what changed.
            if notification.delivered_at and not is_status:
                return
            notification.attempts += 1
        if is_status:
            await send_status(notification_id)
            return
        # Delivery runs after that session commits, so a refusal still counts the
        # attempt. The detached rows stay readable: sessions don't expire on commit.
        # The channel posts where the conversation lives, or where the account reads it, and
        # reads its grant here.
        async with step_session() as session:
            channel, conversation = await get_channel(session, notification)
            # An account that reads the channel nowhere gets no message: the bell has the
            # notification.
            message_id = await channel.post_buttons(
                session, conversation=conversation, notification=notification
            )
        # Kept so that Druks can close the message when its question ends without a click,
        # and so that a chat's agent knows its question in the thread as its own.
        async with step_session() as session:
            await (await session.get(Notification, notification_id)).mark_delivered(message_id)

    try:
        await DBOS.run_step_async(
            StepOptions(name="notifications.send.deliver", **_SEND_RETRIES), _send
        )
    except Exception as error:
        if isinstance(error, dbos_error.DBOSMaxStepRetriesExceeded):
            error = error.errors[-1]
        # An unexpected error's text can quote the request; the class name alone is
        # always safe.
        reason = (
            str(error)
            if isinstance(error, (exceptions.NotificationError, ServiceNotConnectedError))
            else type(error).__name__
        )

        async def _mark_failed() -> None:
            async with step_session() as session:
                await (await session.get(Notification, notification_id)).mark_failed(reason)

        # Terminal: record the failure and return normally — re-raising would
        # put the workflow into perpetual DBOS recovery for a dead endpoint.
        await DBOS.run_step_async(StepOptions(name="notifications.send.mark_failed"), _mark_failed)


async def send_status(notification_id: str) -> None:
    """Show the status with where its run stands: post its message, or edit the message
    that it has. One send of a status runs at a time. A status with no channel that can
    edit a message stays in Druks."""
    async with (
        lock(constants.STATUS_LOCK.format(status=notification_id)),
        step_session() as session,
    ):
        status = await session.get(Notification, notification_id)
        channel, conversation = await get_channel(session, status)
        if channel and channel.has_editable_status:
            stack = await status.draw(await session.get(Run, status.run_id))
            if message_id := await channel.show_status(
                session,
                conversation=conversation,
                notification=status,
                body=stack.to_markdown(),
                blocks=[block.model_dump(mode="json", by_alias=True) for block in stack.blocks],
            ):
                await status.mark_delivered(message_id)


async def queue_status_send(status: Notification) -> None:
    """Queue a send of the status. DBOS joins the sends that come close together into one
    send, which shows the newest status."""
    options: EnqueueOptions = {
        "workflow_name": constants.SEND_WORKFLOW,
        "queue_name": NOTIFICATIONS_QUEUE,
    }
    sends = DebouncerClient(
        get_dbos_client(), options, debounce_timeout_sec=constants.STATUS_DELAY_LIMIT_SECONDS
    )
    await sends.debounce_async(status.id, constants.STATUS_DELAY_SECONDS, status.id)


@DBOS.workflow(name=constants.REFRESH_STATUS_WORKFLOW)
async def refresh_status(run_id: str) -> None:
    """Show the status of the run again, after a cancel or a retry changed where the run
    stands. A run that retries another takes the status of that run."""
    # A rerun shows the status once more, so the body needs no step.
    async with step_session() as session:
        if status := await Notification.get_status(session, run_id):
            run = await session.get(Run, run_id)
            await status.follow(run, state=run.state)
            await queue_status_send(status)


async def notify_account(
    session: AsyncSession, account_id: str, *, reason: str, body: str, path: str
) -> None:
    """Make a notification for the account and queue its send, in the session's
    transaction. The notification links to the dashboard page at ``path``."""
    endpoint = get_settings().urls.endpoint.rstrip("/")
    notification = await Notification.create(
        session,
        account_id=account_id,
        reason=reason,
        body=body,
        deep_link=f"{endpoint}{path}" if endpoint else None,
    )
    await queue_send(session, notification)


async def queue_send(session: AsyncSession, notification: Notification) -> None:
    """Queue the send of the notification, in the session's transaction."""
    options: EnqueueOptions = {
        "workflow_name": constants.SEND_WORKFLOW,
        "queue_name": NOTIFICATIONS_QUEUE,
        "workflow_id": f"{constants.SEND_WORKFLOW}:{notification.id}",
    }
    await enqueue_in_transaction(session, options, notification.id)


async def get_channel(
    session: AsyncSession, notification: Notification
) -> "tuple[type[Channel] | None, Conversation | None]":
    """The channel that posts the notification, and the conversation that it posts in. A
    notification with no conversation goes to its account, through the channel that posts
    the notifications of accounts."""
    if notification.conversation_id:
        conversation = await session.get(Conversation, notification.conversation_id)
        return conversation.channel, conversation
    (channel,) = [
        channel for channel in channel_registry.all() if channel.has_account_notifications
    ]
    return channel, None


@DBOS.workflow(name=constants.CLOSE_WORKFLOW)
async def close_notifications(run_id: str, parked_at: str) -> None:
    # The claims make a rerun redo only the messages still open, so the body needs no step.
    async with step_session() as session:
        notification_ids = [
            notification.id
            for notification in await Notification.list_open(
                session, run_id, datetime.fromisoformat(parked_at)
            )
        ]
    for notification_id in notification_ids:
        async with (
            lock(constants.NOTIFICATION_LOCK.format(run=run_id, notification=notification_id)),
            step_session() as session,
        ):
            notification = await session.get(Notification, notification_id)
            if await notification.mark_acknowledged():
                # The other messages still close when one edit fails.
                try:
                    await remove_buttons(session, notification)
                except (exceptions.DeliveryError, ServiceNotConnectedError) as error:
                    logger.warning("notification %s kept its buttons: %s", notification.id, error)


async def close_questions(session: AsyncSession, conversation_id: str) -> None:
    """Claim each open question of the conversation's agent, and queue the removal of
    its buttons, in the session's transaction."""
    for question in await Notification.list_open_questions(session, conversation_id):
        if await question.mark_acknowledged():
            options: EnqueueOptions = {
                "workflow_name": constants.CLOSE_QUESTION_WORKFLOW,
                "queue_name": NOTIFICATIONS_QUEUE,
                "workflow_id": f"{constants.CLOSE_QUESTION_WORKFLOW}:{question.id}",
            }
            await enqueue_in_transaction(session, options, question.id)


@DBOS.workflow(name=constants.CLOSE_QUESTION_WORKFLOW)
async def close_question(notification_id: str) -> None:
    """Take the buttons off the question's message once its send has ended."""
    await DBOS.get_result_async(f"{constants.SEND_WORKFLOW}:{notification_id}")
    async with step_session() as session:
        notification = await session.get(Notification, notification_id)
        if notification.message_id:
            try:
                await remove_buttons(session, notification)
            except (exceptions.DeliveryError, ServiceNotConnectedError) as error:
                logger.warning("notification %s kept its buttons: %s", notification.id, error)


async def remove_buttons(session: AsyncSession, notification: Notification) -> None:
    """Take the buttons off the notification's posted message, and say that its question
    is closed. The channel that posted the message edits it."""
    channel, conversation = await get_channel(session, notification)
    await channel.remove_buttons(session, conversation=conversation, notification=notification)
