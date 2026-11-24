from datetime import datetime

from pydantic import AliasPath, ConfigDict, Field, field_validator

from druks.schemas import Schema
from druks.ui.blocks import Block, FileSummary, Stack
from druks.ui.fields import Field as FormField

from .enums import ConversationSource, MessageRole, MessageState


class AutomationResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    is_accepted: bool


class ConversationResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    is_pinned: bool
    source: ConversationSource
    user_id: str | None
    user_name: str
    # The group or room of the conversation. Empty for a direct chat.
    thread_id: str
    created_at: datetime
    last_message_at: datetime
    message_count: int
    active_message_id: str | None
    agent_id: str | None
    agent_name: str | None = Field(default=None, validation_alias=AliasPath("agent", "name"))


class QuestionResponse(Schema):
    question: str
    choices: list[str]
    fields: list[FormField]


class ReplyBlockResponse(Schema):
    block: Block
    text_form: str


class MessageResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: MessageRole
    body: str
    blocks: list[ReplyBlockResponse]
    reply_to: str | None
    state: MessageState | None
    tool_calls: list[dict]
    question: QuestionResponse | None
    is_internal: bool
    file: FileSummary | None
    transcript: str
    created_at: datetime
    delivered_at: datetime | None

    @field_validator("blocks", mode="before")
    @classmethod
    def add_text_forms(cls, blocks: list[dict] | None) -> list[dict]:
        """Each block that the reply shows, with its text form."""
        return [
            {"block": block, "text_form": block.to_markdown()}
            for block in Stack(blocks or []).blocks
        ]


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse]


class RunStatusResponse(Schema):
    # The status that a run reported, as its message shows it.
    id: str
    run: str
    # The app that declares the run's workflow. Empty when no loaded app declares it.
    app: str
    blocks: list[Block]
    # When the first run of the retry chain started. The status shows at that place.
    started_at: datetime


class ReminderResponse(Schema):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    body: str
    cron: str
    next_send_at: datetime = Field(validation_alias="local_next_send_at")
