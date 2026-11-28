from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from druks.chat.exceptions import ChannelHasNoThreadsError, ChannelStartsNoConversationsError
from druks.chat.models import Conversation, Message
from druks.notifications.models import Notification
from druks.registry import channel_registry
from druks.services import Service


class Channel:
    """A way people reach a Bot. It is live while its service is connected. Its name
    is the source of the conversations that arrive through it."""

    name: ClassVar[str]
    service: ClassVar[type[Service]]
    # Whether chat runs agent turns on the channel's conversations. False when something
    # outside chat answers the people on the channel.
    has_turns: ClassVar[bool] = True
    # Whether chat_read_thread reads the channel's threads, where other people write too.
    has_readable_threads: ClassVar[bool] = False
    # Whether the channel shows a parked run's controls and the agent's choices as buttons.
    has_buttons: ClassVar[bool] = False
    # Whether the channel shows the agent's form and a parked run's questions and note as
    # inputs. Only without them does a click on a control that needs more reach the agent.
    has_inputs: ClassVar[bool] = False
    # Whether the channel draws the blocks that the agent shows.
    has_blocks: ClassVar[bool] = False
    # Whether the channel posts the notifications of an account that have no conversation.
    has_account_notifications: ClassVar[bool] = False
    # Whether the channel shows a run's status in one message, which it edits as the run moves.
    has_editable_status: ClassVar[bool] = False
    # Whether the channel sends messages at the top of a room, in a conversation there.
    has_room_messaging: ClassVar[bool] = False
    # The reaction that acknowledges a person's message, as the channel names it.
    reaction_ack: ClassVar[str]

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        channel_registry.register(cls)

    @classmethod
    async def get_prompt_context(cls, session: AsyncSession, conversation: Conversation) -> dict:
        """Facts about where the conversation lives, for the Bot's prompt."""
        return {}

    @classmethod
    async def send_reply(
        cls, session: AsyncSession, conversation: Conversation, reply: Message
    ) -> None:
        """Send the agent's reply to where the conversation lives."""
        raise NotImplementedError

    @classmethod
    def validate_fields(cls, fields: list[dict]) -> None:
        """Refuse the fields of the agent's form that the channel cannot show, with a
        FormError that says why. A channel takes every form unless it says otherwise."""

    @classmethod
    async def post_buttons(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
    ) -> str | None:
        """Post the notification where the conversation lives, with its fields as inputs and
        its actions as buttons. Fields without actions get a Submit button. A notification
        that an answer closed before the post says that its question is closed instead.
        Without a conversation, post the notification where its account reads the channel.
        Returns the posted message's id, as the channel names it, or None when the account
        reads the channel nowhere."""
        raise NotImplementedError

    @classmethod
    async def remove_buttons(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
    ) -> None:
        """Take the buttons off the notification's posted message, and say that its
        question is closed."""
        raise NotImplementedError

    @classmethod
    async def show_status(
        cls,
        session: AsyncSession,
        conversation: Conversation | None,
        notification: Notification,
        body: str,
        blocks: list[dict],
    ) -> str | None:
        """Show ``body`` and ``blocks``, a run's status as its message draws it, where the
        conversation lives: post the message, or edit the message that the status's
        ``message_id`` names. Without a conversation, post it where the status's account
        reads the channel. Returns the message's id, as the channel names it, or None when
        the account reads the channel nowhere."""
        raise NotImplementedError

    @classmethod
    async def acknowledge(
        cls, session: AsyncSession, conversation: Conversation, message: Message
    ) -> None:
        """Show the person that the agent has their message."""
        raise NotImplementedError

    @classmethod
    async def read_thread(cls, session: AsyncSession, conversation: Conversation) -> list[dict]:
        """The conversation's thread at its source, oldest first."""
        raise ChannelHasNoThreadsError(f"A {cls.name} conversation has no thread to read.")

    @classmethod
    async def download_file(
        cls, session: AsyncSession, conversation: Conversation, file_id: str
    ) -> tuple[bytes, str, str]:
        """A file on the conversation's thread, by the id its source gave it, as its
        bytes, name, and content type."""
        raise ChannelHasNoThreadsError(f"A {cls.name} conversation has no thread files to read.")

    @classmethod
    async def start_conversation(
        cls, session: AsyncSession, conversation: Conversation, *, phone: str, name: str
    ) -> Conversation:
        """A direct chat with a person on the conversation's connection, under the
        conversation's account. Nothing is sent."""
        raise ChannelStartsNoConversationsError(
            f"A {cls.name} conversation cannot start a chat with a person."
        )

    @classmethod
    async def create_room_conversation(
        cls, session: AsyncSession, conversation: Conversation, *, room: str
    ) -> Conversation:
        """A conversation at the top of a room on the channel, under the conversation's
        account and with its agent. Each reply there is a new message in the room."""
        raise NotImplementedError
