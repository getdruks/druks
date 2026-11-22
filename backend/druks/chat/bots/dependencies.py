from typing import Annotated

from fastapi import Depends, HTTPException

from druks.chat import datastructures
from druks.chat.dependencies import current_conversation
from druks.chat.models import Conversation


async def get_bot_user(
    conversation: Conversation | None = Depends(current_conversation),
) -> datastructures.BotUser:
    """The person who writes in the conversation that the tool call came from."""
    if conversation and conversation.user_id:
        return datastructures.BotUser(
            id=conversation.user_id,
            name=conversation.user_name,
            phone=conversation.user_phone,
            source=conversation.source,
        )
    raise HTTPException(
        status_code=409,
        detail="This tool serves the person in a chat. Call it from a chat conversation.",
    )


# Resolved from the request, never from a tool input, so it never enters a tool's schema.
BotUser = Annotated[datastructures.BotUser, Depends(get_bot_user)]
