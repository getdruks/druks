import hashlib
import hmac
import json
import time
from functools import partial
from urllib.parse import parse_qs, quote, urlsplit

from dbos import DBOS
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from druks.accounts.models import Account
from druks.api.client import call_route
from druks.api.exceptions import get_refusal
from druks.chat.channels.slack.constants import INPUT_ID_PREFIX, LINK_BUTTON_ACTION_ID
from druks.chat.channels.slack.delivery import deliver
from druks.chat.forms import get_answer
from druks.core.services import Slack
from druks.db import db_session
from druks.durable.engine import step_session
from druks.exceptions import LockHeldError, ObjectNotFound
from druks.locks import lock
from druks.notifications.buttons import decode_action, decode_button, is_action_button
from druks.notifications.constants import (
    ACTION_RUNNING_MESSAGE,
    CLOSED_MESSAGE,
    NOTIFICATION_LOCK,
)
from druks.notifications.exceptions import (
    ActionRefusedError,
    AlreadyAcknowledgedError,
    AnswerNotAllowedError,
    InvalidChoiceError,
    MalformedButtonError,
    NotificationError,
    StaleRoundError,
    UnknownTokenError,
)
from druks.notifications.models import Notification
from druks.notifications.services import get_action_request, respond_to_notification
from druks.secrets.models import VaultSecret
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import get_settings
from druks.signals import publish
from druks.webhooks import Webhook

# Slack signs v0:{ts}:{body}. A timestamp outside the window is a replayed capture.
_TIMESTAMP_TOLERANCE_SECONDS = 300

# What a clicked message says in place of its buttons.
CHOICE_MESSAGE = "<@{user}> chose {choice}."

# What a submitted form's message says in place of its inputs.
ANSWER_MESSAGE = "<@{user}> answered:\n{answers}"

# What a person with no linked account reads by DM after a click on an action.
CONNECT_MESSAGE = (
    "Your Slack account is not connected to Druks. Druks thus cannot run this action as you. "
    "After you connect it, click the button again. Connect it here: {url}"
)
# The same message, for an installation with no address for the link.
CONNECT_IN_DRUKS_MESSAGE = (
    "Your Slack account is not connected to Druks. Druks thus cannot run this action as you. "
    "After you connect it, click the button again. To connect it, select Connect Slack "
    "under Chat → Channels in Druks."
)


def verify_slack_signature(
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
) -> None:
    if not secret:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Druks has no signing secret for Slack. Paste it on the Slack card under Settings → "
            "Connections → Services.",
        )
    if not signature or not timestamp:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The request has no webhook signature. Sign the request as Slack does, with the "
            "signing secret of the Slack app.",
        )
    try:
        age_seconds = abs(time.time() - int(timestamp))
    except ValueError as error:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The webhook signature is not correct. Sign the request as Slack does, with the "
            "signing secret of the Slack app.",
        ) from error
    if age_seconds > _TIMESTAMP_TOLERANCE_SECONDS:
        # Such a request is a replayed capture. Its signature can be real. The request is still old.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The webhook timestamp is too old. Send a new request, or make sure that the clock "
            "of the Druks host is correct.",
        )
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "The webhook signature is not correct. Make sure that the Slack service in Druks "
            "has the signing secret of the Slack app.",
        )


class SlackWebhook(Webhook):
    abstract = True
    provider = "slack"

    async def request_is_authentic(self) -> bool:
        try:
            signing_secret = (await Slack.get()).secrets["signing_secret"]
        except ServiceNotConnectedError as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(error)) from error
        verify_slack_signature(
            self.raw_body,
            self.request.headers.get("x-slack-signature"),
            self.request.headers.get("x-slack-request-timestamp"),
            signing_secret,
        )
        return True


class SlackInteractivity(SlackWebhook):
    category = "interactivity"

    def get_delivery_key(self) -> str:
        # Slack gives an interaction no ID. A retry sends the same bytes.
        return "interaction:" + hashlib.sha256(self.raw_body).hexdigest()

    def get_data(self) -> dict:
        # Interactivity arrives form-encoded as payload=<json>.
        try:
            return json.loads(parse_qs(self.raw_body.decode())["payload"][0])
        except (UnicodeDecodeError, KeyError, json.JSONDecodeError) as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Druks cannot read the Slack payload. Send a form body with a JSON payload "
                "field, as Slack does.",
            ) from error

    def get_action(self) -> str:
        try:
            return self.data["type"]
        except (TypeError, KeyError) as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Druks cannot read the Slack payload. Send a payload with a type, as Slack does.",
            ) from error

    def get_values(self) -> dict:
        """The answers in the inputs of the clicked message, keyed by the ids of their blocks, in
        the shapes that a form's fields read. The id of an input of an approval or of an
        agent's form is the name of its field. A message without inputs has no state."""
        values = {}
        for block_id, inputs in self.data.get("state", {}).get("values", {}).items():
            for element in inputs.values():
                match element["type"]:
                    case "plain_text_input":
                        values[block_id] = element.get("value") or ""
                    case "static_select" | "radio_buttons":
                        values[block_id] = (element.get("selected_option") or {}).get("value", "")
                    case "multi_static_select":
                        selected = element.get("selected_options") or []
                        values[block_id] = [option["value"] for option in selected]
                    case "checkboxes":
                        values[block_id] = bool(element.get("selected_options"))
        return values

    async def on_block_actions(self) -> Response:
        try:
            action_id = self.data["actions"][0]["action_id"]
            channel = self.data["channel"]["id"]
            message = self.data["message"]
            message_ts = message["ts"]
            user = self.data["user"]["id"]
            values = self.get_values()
        except (TypeError, KeyError, IndexError, AttributeError) as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Druks cannot read the Slack payload. Send a block_actions payload with its "
                "actions, channel, message and user, as Slack does.",
            ) from error
        if action_id == LINK_BUTTON_ACTION_ID:
            # Slack opened the link of the button. It still sends the click to Druks. A link
            # answers nothing.
            return JSONResponse({"accepted": True})
        # Druks finds the clicker's account in the workspace that it answers in, as chat does.
        account_id = await VaultSecret.get_owner_id(
            db_session(),
            Slack.Authentication.get_authority((await Slack.get()).identity["team_id"]),
            user,
        )
        # A click that cannot answer still gets a 200. A retry can fix nothing. Druks thus
        # does not make Slack try again. Corruption still propagates, as a 500 that Druks
        # logs.
        #
        # What Slack shows after a click starts immediately. The click does not wait for it.
        # A slow or refused post then cannot make the click fail. A queue adds a delay of at
        # most a second before the person sees it.
        try:
            if is_action_button(action_id):
                token, position = decode_action(action_id)
                return await self.start_action(
                    token,
                    position,
                    values=values,
                    account_id=account_id,
                    user=user,
                    channel=channel,
                    message=message,
                )
            token, choice_id = decode_button(action_id)
            notification = await respond_to_notification(
                db_session(),
                token,
                {"control": choice_id},
                user_id=user,
                account_id=account_id,
                values=values,
                has_inputs=True,
            )
        except MalformedButtonError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Druks cannot read this button. Click a button of a message that Druks posted.",
            ) from error
        except StaleRoundError as error:
            # The round ended. The clicked message thus loses its buttons.
            self.log_ignored(event="block_actions", reason=type(error).__name__)
            notification = await Notification.get_for_token(db_session(), token)
            await DBOS.start_workflow_async(
                replace_buttons,
                notification.id,
                channel=channel,
                message_ts=message_ts,
                outcome=CLOSED_MESSAGE,
            )
            return JSONResponse({"accepted": False})
        except (
            UnknownTokenError,
            AlreadyAcknowledgedError,
            InvalidChoiceError,
            AnswerNotAllowedError,
            ObjectNotFound,
        ) as error:
            # The question can still be open. The message thus stays. Only the clicker reads
            # why.
            self.log_ignored(event="block_actions", reason=type(error).__name__)
            await DBOS.start_workflow_async(
                explain_reason,
                channel,
                user=user,
                reason=str(error),
                thread_ts=message.get("thread_ts", ""),
            )
            return JSONResponse({"accepted": False})
        # Druks escapes the person's words. They thus cannot mention anyone or link anywhere
        # in the message.
        answers = get_answer(notification.fields or [], values)
        answers = answers.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if notification.fields and not notification.run_id:
            outcome = ANSWER_MESSAGE.format(user=user, answers=answers)
        else:
            outcome = CHOICE_MESSAGE.format(
                user=user, choice=notification.get_action(choice_id)["label"]
            )
            if answers:
                outcome += f"\n{answers}"
        await DBOS.start_workflow_async(
            replace_buttons,
            notification.id,
            channel=channel,
            message_ts=message_ts,
            outcome=outcome,
        )
        return JSONResponse({"accepted": True})

    async def start_action(
        self,
        token: str,
        position: list[int],
        *,
        values: dict,
        account_id: str | None,
        user: str,
        channel: str,
        message: dict,
    ) -> Response:
        """Start the action at ``position`` in the blocks of the notification of ``token``, as
        the clicker's account. ``values`` holds the answers in every input of the message.
        The action uses those of its own block. A person with no linked account gets a DM
        that says how to connect one. Nothing then runs."""
        prefix = INPUT_ID_PREFIX.format(position=position[0])
        values = {
            block_id.removeprefix(prefix): value
            for block_id, value in values.items()
            if block_id.startswith(prefix)
        }
        request = await get_action_request(
            db_session(), token, position, user_id=user, values=values
        )
        notification = request["notification"]
        if account_id:
            await DBOS.start_workflow_async(
                run_action,
                notification.id,
                position,
                values=values,
                account_id=account_id,
                user=user,
                channel=channel,
                message_ts=message["ts"],
                thread_ts=message.get("thread_ts", ""),
            )
            return JSONResponse({"accepted": True})
        reason = CONNECT_IN_DRUKS_MESSAGE
        # The link connects the Slack account, then opens the page of the notification.
        if notification.deep_link:
            page = quote(urlsplit(notification.deep_link).path, safe="")
            endpoint = get_settings().urls.endpoint.rstrip("/")
            reason = CONNECT_MESSAGE.format(url=f"{endpoint}/api/oauth/slack/connect?next={page}")
        self.log_ignored(event="block_actions", reason="NoLinkedAccount")
        # The person's DM keeps the link. Slack can lose a message that only one person sees.
        await DBOS.start_workflow_async(
            explain_reason, channel=user, user="", reason=reason, thread_ts=""
        )
        return JSONResponse({"accepted": False})


@DBOS.workflow(name="slack.run_action")
async def run_action(
    notification_id: str,
    position: list[int],
    *,
    values: dict,
    account_id: str,
    user: str,
    channel: str,
    message_ts: str,
    thread_ts: str,
) -> None:
    """Run the action that a person clicked, with ``values``, the answers in the inputs of
    its block. Then draw the message again, or show only that person why it did not run.
    DBOS tries only the edit and the reply again."""
    try:
        await call_action(
            notification_id, position, values=values, account_id=account_id, user=user
        )
    except ActionRefusedError as error:
        await explain_reason(channel, user=user, reason=str(error), thread_ts=thread_ts)
    else:
        await replace_buttons(notification_id, channel=channel, message_ts=message_ts, outcome="")


@DBOS.step(name="slack.call_action")
async def call_action(
    notification_id: str, position: list[int], *, values: dict, account_id: str, user: str
) -> None:
    """Call the route of the action at ``position`` in the notification's blocks as the
    account, with ``values``. Then store the line that replaces the action, or the form of
    the action. The call holds the notification's lock. Under it, the call reads the action
    and checks the values again. Raises ActionRefusedError with what the person who clicked
    reads."""
    async with step_session() as session:
        notification = await session.get(Notification, notification_id)
    try:
        async with lock(
            NOTIFICATION_LOCK.format(run=notification.run_id, notification=notification.id),
            blocking=False,
        ):
            async with step_session() as session:
                request = await get_action_request(
                    session, notification.correlation_token, position, user_id=user, values=values
                )
            response = await call_route(
                request["method"], request["path"], json=request["body"], account_id=account_id
            )
            if response.is_success:
                async with step_session() as session:
                    account = await session.get(Account, account_id)
                    notification = await session.get(Notification, notification_id)
                    await notification.mark_action_run(position, username=account.username)
                return
    except LockHeldError as error:
        raise ActionRefusedError(ACTION_RUNNING_MESSAGE) from error
    except (NotificationError, ObjectNotFound) as error:
        raise ActionRefusedError(str(error)) from error
    raise ActionRefusedError(get_refusal(response)["message"])


# DBOS runs a lone step as a workflow of its own. The click handler thus starts these two
# and does not wait for them.
@DBOS.step(name="slack.replace_buttons", retries_allowed=True, max_attempts=5)
async def replace_buttons(
    notification_id: str, *, channel: str, message_ts: str, outcome: str
) -> None:
    """Edit the notification's message in Slack. ``outcome`` replaces its buttons and
    inputs. With no outcome, an open question keeps them. A closed question keeps its
    message. The edit holds the lock of the message. It thus draws what an action of the
    message stored."""
    async with step_session() as session:
        notification = await session.get(Notification, notification_id)
    async with (
        lock(NOTIFICATION_LOCK.format(run=notification.run_id, notification=notification.id)),
        step_session() as session,
    ):
        notification = await session.get(Notification, notification_id)
        client = await Slack.get_client()
        edit = partial(
            deliver,
            client,
            channel,
            notification.body,
            session=session,
            account=await notification.get_account(session),
            blocks=notification.blocks,
            message_ts=message_ts,
            deep_link=notification.deep_link,
            question_offset=notification.question_offset,
        )
        if outcome:
            await edit(outcome=outcome)
        elif not notification.is_acknowledged:
            await edit(
                actions=notification.actions,
                fields=notification.fields,
                token=notification.correlation_token,
            )


@DBOS.step(name="slack.explain_reason", retries_allowed=True, max_attempts=5)
async def explain_reason(channel: str, *, user: str, reason: str, thread_ts: str) -> None:
    """Show ``reason`` to the person who clicked, and to nobody else."""
    async with step_session() as session:
        client = await Slack.get_client()
        await deliver(
            client, channel, reason, session=session, account=None, user=user, thread_ts=thread_ts
        )


class SlackEvents(SlackWebhook):
    """Publishes each person's message, another bot's tag in a room, and the bot's join
    to a room, as facts. It does not handle conversations."""

    category = "events"

    def get_delivery_key(self) -> str | None:
        if self.data["type"] == "event_callback":
            return self.data["event_id"]
        return

    def get_action(self) -> str:
        return self.data["type"]

    async def on_url_verification(self) -> Response:
        return JSONResponse({"challenge": self.data["challenge"]})

    async def on_event_callback(self) -> Response:
        """Publish a person's message, another bot's tag in a room, and the bot's own
        join to a room. Edits, deletes, other joins, hidden events, and the bot's own
        messages are not facts for anyone. Nor are the untagged or direct messages of
        other bots, or the messages of people from other workspaces."""
        event = self.data["event"]
        bot_user_id = (await Slack.get()).identity["bot_user_id"]
        # Another bot has no Slack account to link and no direct message to answer in. It
        # thus comes to the bot of Druks only through a tag in a room.
        is_tag = f"<@{bot_user_id}>" in event.get("text", "")
        is_tag_in_room = is_tag and event.get("channel_type") != "im"
        is_fact = (
            event["type"] == "message"
            and (
                not event.get("subtype")
                or event["subtype"] in ("file_share", "thread_broadcast", "bot_message")
            )
            and not event.get("hidden")
            and (not event.get("bot_id") or is_tag_in_room)
            and event.get("user") != bot_user_id
            and event.get("user_team", self.data["team_id"]) == self.data["team_id"]
        )
        if is_fact:
            await publish("slack.message", message=event)
        elif event["type"] == "member_joined_channel" and event["user"] == bot_user_id:
            await publish("slack.bot_joined", room=event["channel"])
        return JSONResponse({"accepted": True})
