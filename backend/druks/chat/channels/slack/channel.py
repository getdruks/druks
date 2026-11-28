import json
import logging
import secrets
from functools import partial

from dbos import DBOS
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession

from druks import ui
from druks.accounts.models import Account
from druks.agents.constants import PRIVATE_AGENT_MESSAGE
from druks.agents.models import Agent
from druks.chat.channels.base import Channel
from druks.chat.enums import ConversationSource
from druks.chat.exceptions import ChannelHasNoThreadsError, FileNotInThreadError, FormError
from druks.chat.models import Automation, Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.slack import SLACK_CUSTOMIZE_SCOPE, SlackClient
from druks.core.services import Slack
from druks.exceptions import ValidationError
from druks.files.datastructures import File
from druks.notifications.constants import CLOSED_MESSAGE
from druks.notifications.exceptions import ChannelUnavailableError
from druks.notifications.models import Notification, mint_correlation_token
from druks.prompts import render_prompt
from druks.redis import get_client
from druks.registry import iter_apps
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.settings import get_settings

from .constants import (
    LINK_KEY,
    LINK_MESSAGE,
    LINK_TTL_SECONDS,
    MARKDOWN_TEXT_LIMIT,
    MESSAGE_BLOCKS_LIMIT,
    THREAD_MESSAGES,
    UNACCEPTED_AUTOMATION_REPLY,
    UNLINKED_REPLY,
    WELCOME_KEY,
    WELCOME_TEMPLATE,
    WELCOME_TTL_SECONDS,
)
from .delivery import check_input, get_blocks
from .delivery import deliver as deliver_notification

logger = logging.getLogger(__name__)


def get_thread_id(message: dict) -> str:
    """Where a message lives, as the room and the thread in one id. A direct message
    has none. A message at the top of a room starts a thread at itself."""
    if message["channel_type"] == "im":
        return ""
    return f"{message['channel']}:{message.get('thread_ts') or message['ts']}"


class SlackChannel(Channel):
    name = ConversationSource.SLACK
    service = Slack
    has_readable_threads = True
    has_buttons = True
    has_inputs = True
    has_blocks = True
    has_account_notifications = True
    has_editable_status = True
    has_room_messaging = True
    reaction_ack = "eyes"

    @classmethod
    async def lookup_account(
        cls, session: AsyncSession, row: VaultSecret, message: dict
    ) -> Account | None:
        """The account with a live Slack grant for the Slack user who wrote the message."""
        authority = Slack.Authentication.get_authority(row.identity["team_id"])
        owner_id = await VaultSecret.get_owner_id(
            session, authority=authority, subject=message["user"]
        )
        return await session.get(Account, owner_id) if owner_id else None

    @classmethod
    async def route_message(
        cls, session: AsyncSession, row: VaultSecret, message: dict
    ) -> Conversation | None:
        """Route a message for the bot, and return the conversation that takes it. A
        direct message or a tag reaches the linked account's own conversation, or waits
        under a private link until the person connects their Slack account. A tag
        followed by an agent's name reaches the person's conversation with that agent. A
        tag that names none reaches the agent that owns the thread, or else the
        channel's agent, or else the Bot. So does a reply with no tag, from a person who
        turned the tag off for the thread.

        The tag of an automation, which is another app's bot or a workflow, reaches the
        default account once an admin accepted the automation. Its first tag waits for
        that. Everything else is not for the bot."""
        if bot_id := message.get("bot_id"):
            automation = await Automation.get_or_none(
                session, connection_id=row.id, source_id=bot_id
            )
            if automation and automation.is_accepted:
                account = await Account.get_default(session)
                await cls.save_message(
                    session, row, account=account, message=message, agent_id=None
                )
            elif not automation:
                await cls.hold_message(session, row, message=message)
            return
        thread_id = get_thread_id(message)
        mention = f"<@{row.identity['bot_user_id']}>"
        is_tagged = mention in message["text"]
        is_addressed = not thread_id or is_tagged
        if not is_addressed and await Conversation.is_tag_required_for_user(
            session, row, user_id=message["user"], thread_id=thread_id
        ):
            # Most of a room's traffic: a message that names nobody.
            return
        linked_account = await cls.lookup_account(session, row, message)
        if linked_account:
            agent = None
            if is_tagged:
                # "@Druks, Security engineer" and "@Druks: Security engineer" name it too.
                text = message["text"].partition(mention)[2].lstrip(",: \n")
                agent = await Agent.lookup(session, text)
            if not agent and thread_id:
                agent = await Conversation.get_thread_owner(
                    session, row, thread_id=thread_id
                ) or await Agent.get_for_channel(session, message["channel"])
            if agent and not linked_account.can_talk_to(agent):
                # A reply with no tag can be for a person in the thread, so it gets no
                # refusal.
                if is_addressed:
                    room, _, thread_ts = thread_id.partition(":")
                    client = SlackClient(token=await Slack.get_bot_token())
                    await client.post_markdown(
                        room or message["user"], PRIVATE_AGENT_MESSAGE, thread_ts=thread_ts
                    )
                return
            return await cls.save_message(
                session,
                row,
                account=linked_account,
                message=message,
                agent_id=agent.id if agent else None,
            )
        if is_addressed:
            await cls.send_link(message)

    @classmethod
    async def save_message(
        cls,
        session: AsyncSession,
        row: VaultSecret,
        account: Account,
        message: dict,
        *,
        agent_id: str | None,
    ) -> Conversation:
        """Save the message in the account's conversation with the agent, or with the
        Bot, for its place, and start its turn. Each file that the message carries is a Druks
        file: the first on the message, each further one on a message of its own. A
        message with no text and no file gives the agent nothing to read."""
        conversation = await Conversation.get_or_create_for_user(
            session,
            row,
            account_id=account.id,
            source=ConversationSource.SLACK,
            # A workflow's post has no user, so its bot ID stands for the person.
            user_id=message.get("user") or message["bot_id"],
            user_name="",
            user_phone="",
            thread_id=get_thread_id(message),
            agent_id=agent_id,
        )
        client = SlackClient(token=await Slack.get_bot_token())
        shares = message.get("files", [])
        # Every download first: a failed one then leaves no file on disk.
        contents = [await client.download(shared["url_private_download"]) for shared in shares]
        files = [
            await File.create(
                name=shared["name"],
                content_type=shared["mimetype"],
                content=content,
                app="chat",
                uploaded_by=account.id,
            )
            for shared, content in zip(shares, contents, strict=True)
        ]
        # Else the agent takes the bot's own mention for the person that it answers.
        text = message["text"].replace(f"<@{row.identity['bot_user_id']}>", "").strip()
        source_id = f"{message['channel']}:{message['ts']}"
        if files:
            await conversation.create_message(session, text, source_id=source_id, file=files[0])
            for shared, file in zip(shares[1:], files[1:], strict=True):
                await conversation.create_message(
                    session, body="", source_id=f"{source_id}:{shared['id']}", file=file
                )
        elif text:
            await conversation.create_message(session, text, source_id=source_id)
        else:
            return conversation
        await session.commit()
        await DBOS.start_workflow_async(deliver, conversation.id)
        return conversation

    @classmethod
    async def get_prompt_context(cls, session: AsyncSession, conversation: Conversation) -> dict:
        """Whether the conversation is the top of a room, and whether the person must tag
        the bot in its thread. The agent's prompt reads both."""
        room, _, thread_ts = conversation.thread_id.partition(":")
        return {
            "is_room": bool(room and not thread_ts),
            "is_tag_required": await Conversation.is_tag_required_for_user(
                session,
                conversation.connection,
                user_id=conversation.user_id,
                thread_id=conversation.thread_id,
            ),
        }

    @classmethod
    async def get_welcome(cls) -> str:
        """What the bot says when it meets a room or a person, for this installation's
        apps and address."""
        return await render_prompt(
            WELCOME_TEMPLATE,
            apps=[app for app in iter_apps() if app.description and not app.builtin],
            endpoint=get_settings().urls.endpoint.rstrip("/"),
        )

    @classmethod
    async def send_link(cls, message: dict) -> None:
        """Hold the message under a private link, and send the person the link in their
        DM, after the welcome if they have not had it. In a room, also tell them in the
        thread where the link is."""
        token = secrets.token_urlsafe(32)
        await get_client().set(
            name=LINK_KEY.format(token=token), value=json.dumps(message), ex=LINK_TTL_SECONDS
        )
        endpoint = get_settings().urls.endpoint.rstrip("/")
        client = SlackClient(token=await Slack.get_bot_token())
        is_first_contact = await get_client().set(
            name=WELCOME_KEY.format(user=message["user"]),
            value="1",
            nx=True,
            ex=WELCOME_TTL_SECONDS,
        )
        if is_first_contact:
            await client.post_markdown(message["user"], await cls.get_welcome())
        # Slack drops a message that only one person sees unless they are active in
        # that client. The link goes to their DM, which keeps it.
        await client.post_markdown(
            message["user"],
            LINK_MESSAGE.format(url=f"{endpoint}/api/chat/services/slack/link/{token}"),
        )
        if thread_id := get_thread_id(message):
            room, _, thread_ts = thread_id.partition(":")
            await client.post_markdown(
                room, UNLINKED_REPLY.format(user=message["user"]), thread_ts=thread_ts
            )

    @classmethod
    async def hold_message(cls, session: AsyncSession, row: VaultSecret, message: dict) -> None:
        """Keep the first tag of an automation until an admin accepts the automation,
        and tell the thread where an admin does that."""
        client = SlackClient(token=await Slack.get_bot_token())
        bot = (await client.bots_info(bot=message["bot_id"]))["bot"]
        session.add(
            Automation(
                connection_id=row.id, source_id=bot["id"], name=bot["name"], first_message=message
            )
        )
        # The flush comes before the post: a second first tag of one automation fails here
        # and posts nothing.
        await session.flush()
        endpoint = get_settings().urls.endpoint.rstrip("/")
        room, _, thread_ts = get_thread_id(message).partition(":")
        await client.post_markdown(
            room,
            UNACCEPTED_AUTOMATION_REPLY.format(url=f"{endpoint}/apps/chat/settings/channels"),
            thread_ts=thread_ts,
        )

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Post the reply where the conversation lives, in pieces that fit a markdown
        block. The first piece's Slack id is the reply's source id. A reply that shows blocks
        goes out as one message, as a notice does: its text, then its blocks as Slack draws
        them. An agent's reply shows the agent's name and picture."""
        client = SlackClient(token=await Slack.get_bot_token())
        room, _, thread_ts = conversation.thread_id.partition(":")
        # Slack takes a custom name only with the scope. A workspace that has not
        # approved the app again gets the reply under the app's own name.
        agent = None
        if SLACK_CUSTOMIZE_SCOPE in (await Slack.get_grant()).scopes:
            agent = conversation.agent
        username = agent.name if agent else None
        icon_url = agent.picture_url if agent else None
        if reply.blocks:
            blocks = reply.answer_blocks
            posted = await deliver_notification(
                client,
                room or conversation.user_id,
                ui.Stack(blocks).to_markdown(),
                session=session,
                account=conversation.account,
                blocks=blocks,
                thread_ts=thread_ts,
                username=username,
                icon_url=icon_url,
            )
        else:
            posted, *_ = [
                await client.post_markdown(
                    room or conversation.user_id,
                    reply.answer[start : start + MARKDOWN_TEXT_LIMIT],
                    thread_ts=thread_ts,
                    username=username,
                    icon_url=icon_url,
                )
                for start in range(0, len(reply.answer), MARKDOWN_TEXT_LIMIT)
            ]
        reply.source_id = f"{posted['channel']}:{posted['ts']}"
        await session.commit()

    @classmethod
    async def create_room_conversation(
        cls, session: AsyncSession, conversation: Conversation, *, room: str
    ) -> Conversation:
        """The conversation at the top of the Slack channel that ``room`` names by id or by
        name. The bot and the person must both be members: Druks posts in a channel for
        nobody who is not in it."""
        sign_ins = await VaultSecret.list_account_connections(
            session, audience=Audience.service(Slack.slug), account_id=conversation.account_id
        )
        if not sign_ins:
            raise ValidationError(
                "Druks does not know the person's Slack account. Ask them to connect Slack "
                "in Druks first."
            )
        user_id = sign_ins[0].identity["subject"]
        client = SlackClient(token=await Slack.get_bot_token())
        name = room.removeprefix("#")
        bot_rooms = [
            bot_room
            for bot_room in await client.list_member_channels()
            if name in (bot_room["id"], bot_room["name"])
        ]
        if not bot_rooms:
            raise ValidationError(f"The bot is not in {room}. Invite it to the channel first.")
        found_room = bot_rooms[0]
        shared_rooms = await client.list_member_channels(user=user_id)
        if found_room["id"] not in [shared_room["id"] for shared_room in shared_rooms]:
            raise ValidationError(
                f"The person is not in #{found_room['name']}. Ask them to join the channel "
                "first, or choose a channel that they are in."
            )
        created = await Conversation.get_or_create_for_user(
            session,
            await Slack.get(),
            conversation.account_id,
            source=ConversationSource.SLACK,
            user_id=user_id,
            user_name="",
            user_phone="",
            thread_id=found_room["id"],
            agent_id=conversation.agent_id,
        )
        created.title = f"#{found_room['name']}"
        return created

    @classmethod
    def validate_fields(cls, fields: list[dict]) -> None:
        """Refuse fields whose message Slack refuses: more blocks than one message holds, or
        a name, a text, or options past their Slack limits. The check reads the blocks that
        delivery posts. A field's name is the id of its block."""
        blocks = get_blocks(text="", fields=fields, token=mint_correlation_token())
        if len(blocks) > MESSAGE_BLOCKS_LIMIT:
            raise FormError(
                f"A Slack message holds {MESSAGE_BLOCKS_LIMIT} blocks: the question, an input "
                f"for each field, and the Submit button. Ask {MESSAGE_BLOCKS_LIMIT - 2} fields "
                "or fewer."
            )
        # The inputs come between the question and the Submit button.
        for block in blocks[1:-1]:
            check_input(block)

    @classmethod
    async def post_buttons(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
    ) -> str | None:
        """Post the notification in the conversation's thread, or its DM. A notification
        that an answer closed before the post says that its question is closed, in place
        of its buttons and inputs. Without a conversation, post the notification for its
        account in the channel that the person chose. When the bot cannot post there, post
        it in the bot's DM with their Slack user. An account with neither gets no
        message."""
        return await cls._post(
            session,
            conversation=conversation,
            notification=notification,
            body=notification.body,
            blocks=notification.blocks,
        )

    @classmethod
    async def _post(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
        body: str,
        blocks: list[dict] | None,
    ) -> str | None:
        """Post ``body`` and ``blocks`` as the message of the notification, where
        ``post_buttons`` says. A status gives its content as its message draws it."""
        thread_ts = ""
        actions, fields, outcome = notification.actions, notification.fields, ""
        account = await notification.get_account(session)
        if conversation:
            room, _, thread_ts = conversation.thread_id.partition(":")
            rooms = [room or conversation.user_id]
            if notification.is_acknowledged:
                actions, fields, outcome = None, None, CLOSED_MESSAGE
        else:
            sign_ins = await VaultSecret.list_account_connections(
                session,
                audience=Audience.service(Slack.slug),
                account_id=notification.account_id,
            )
            rooms = []
            if account.notification_channel_id:
                rooms.append(account.notification_channel_id)
            if sign_ins:
                rooms.append(sign_ins[0].identity["subject"])
            if not rooms:
                return
        post = partial(
            deliver_notification,
            SlackClient(token=await Slack.get_bot_token()),
            body=body,
            session=session,
            account=account,
            blocks=blocks,
            thread_ts=thread_ts,
            actions=actions,
            fields=fields,
            token=notification.correlation_token,
            deep_link=notification.deep_link,
            outcome=outcome,
            question_offset=notification.question_offset,
        )
        try:
            posted = await post(rooms[0])
        except ChannelUnavailableError:
            # The next room takes a notification that the first one refuses.
            if not rooms[1:]:
                raise
            posted = await post(rooms[1])
        return f"{posted['channel']}:{posted['ts']}"

    @classmethod
    async def remove_buttons(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
    ) -> None:
        """Edit the notification's message to say that its question is closed."""
        room, message_ts = notification.message_id.split(":")
        await deliver_notification(
            SlackClient(token=await Slack.get_bot_token()),
            channel=room,
            body=notification.body,
            session=session,
            account=await notification.get_account(session),
            blocks=notification.blocks,
            message_ts=message_ts,
            deep_link=notification.deep_link,
            outcome=CLOSED_MESSAGE,
            question_offset=notification.question_offset,
        )

    @classmethod
    async def show_status(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
        body: str,
        blocks: list[dict],
    ) -> str | None:
        """Edit the status's message in the channel that Slack gave it, such as a DM, to show
        ``body`` and ``blocks``. A status with no message yet posts where ``post_buttons``
        posts."""
        if notification.message_id:
            room, message_ts = notification.message_id.split(":")
            await deliver_notification(
                SlackClient(token=await Slack.get_bot_token()),
                channel=room,
                body=body,
                session=session,
                account=await notification.get_account(session),
                blocks=blocks,
                message_ts=message_ts,
                deep_link=notification.deep_link,
            )
            return notification.message_id
        return await cls._post(
            session, conversation=conversation, notification=notification, body=body, blocks=blocks
        )

    @classmethod
    async def acknowledge(
        cls, session: AsyncSession, conversation: Conversation, message: Message
    ) -> None:
        """React to the person's message."""
        client = SlackClient(token=await Slack.get_bot_token())
        room, ts = message.source_id.split(":")[:2]
        try:
            await client.reactions_add(channel=room, timestamp=ts, name=cls.reaction_ack)
        except SlackApiError as error:
            if error.response["error"] == "already_reacted":
                logger.info("Slack message %s already has the reaction", message.source_id)
                return
            raise

    @classmethod
    def get_block_text(cls, block: dict) -> str:
        """A link shows its URL, so that the agent can follow it."""
        match block["type"]:
            case "plain_text" | "mrkdwn" | "text":
                return block["text"]
            case "link":
                if block.get("text"):
                    return f"{block['text']} ({block['url']})"
                return block["url"]
            case "user":
                return f"<@{block['user_id']}>"
            case "channel":
                return f"<#{block['channel_id']}>"
            case "emoji":
                return f":{block['name']}:"
            case "header":
                return cls.get_block_text(block["text"])
            case "section":
                pieces = [block.get("text"), *block.get("fields", [])]
                return "\n".join(cls.get_block_text(piece) for piece in pieces if piece)
            case "context":
                return " ".join(cls.get_block_text(element) for element in block["elements"])
            case "rich_text" | "rich_text_list":
                return "\n".join(cls.get_block_text(element) for element in block["elements"])
            case "rich_text_section" | "rich_text_preformatted" | "rich_text_quote":
                return "".join(cls.get_block_text(element) for element in block["elements"])
        return ""

    @classmethod
    def get_message_text(cls, message: dict, *, bot_user_id: str) -> str:
        """A person's message repeats its text as a rich text block, so rich text counts
        only when the text is empty. Druks puts its whole message in the text, and its
        blocks repeat it."""
        text = message["text"]
        if message["user"] == bot_user_id:
            return text
        pieces = [text]
        for attachment in message.get("attachments", []):
            title = attachment.get("title", "")
            if attachment.get("title_link"):
                title = f"{title} ({attachment['title_link']})"
            pieces += [attachment.get("pretext", ""), title, attachment.get("text", "")]
            pieces += [
                ": ".join(part for part in (field.get("title"), field.get("value")) if part)
                for field in attachment.get("fields", [])
            ]
            pieces += [cls.get_block_text(block) for block in attachment.get("blocks", [])]
            pieces.append(attachment.get("footer", ""))
        pieces += [
            cls.get_block_text(block)
            for block in message.get("blocks", [])
            if not text or block["type"] != "rich_text"
        ]
        return "\n".join(piece for piece in pieces if piece)

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The thread's newest messages, oldest first, with who wrote each one and the
        files on it. Every agent in the thread posts as the one bot, so a reply is this
        conversation's by its recorded Slack id. A webhook's post has no author, so the
        list leaves it out."""
        room, _, thread_ts = conversation.thread_id.partition(":")
        if not thread_ts:
            raise ChannelHasNoThreadsError(
                "A direct message or the top of a room has no thread to read."
            )
        client = SlackClient(token=await Slack.get_bot_token())
        messages = await client.list_replies(
            channel=room, thread_ts=thread_ts, limit=THREAD_MESSAGES
        )
        own_replies = await conversation.list_reply_source_ids(session)
        bot_user_id = (await Slack.get()).identity["bot_user_id"]
        return [
            {
                "ts": message["ts"],
                "user_id": message["user"],
                "user_name": await client.get_user_name(message["user"]),
                "text": cls.get_message_text(message, bot_user_id=bot_user_id),
                "files": [
                    {
                        "id": shared["id"],
                        "name": shared["name"],
                        "content_type": shared["mimetype"],
                    }
                    for shared in message.get("files", [])
                ],
                "is_from_you": f"{room}:{message['ts']}" in own_replies,
                "is_from_user": message["user"] == conversation.user_id,
            }
            for message in messages
            if "user" in message
        ]

    @classmethod
    async def download_file(
        cls, session: AsyncSession, conversation: Conversation, file_id: str
    ) -> tuple[bytes, str, str]:
        """A file shared in the room where the conversation's thread lives, by its
        Slack id, as its bytes, name, and content type."""
        room, _, _ = conversation.thread_id.partition(":")
        if not room:
            raise ChannelHasNoThreadsError("A direct message has no thread to read.")
        client = SlackClient(token=await Slack.get_bot_token())
        shared = (await client.files_info(file=file_id))["file"]
        if room not in shared["channels"] + shared["groups"]:
            raise FileNotInThreadError(
                f"File {file_id} is not shared where this thread lives. "
                "Give the id of a file that the thread lists."
            )
        content = await client.download(shared["url_private_download"])
        return content, shared["name"], shared["mimetype"]
