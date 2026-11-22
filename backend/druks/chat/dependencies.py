from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request

from druks.accounts.context import current_conversation_id
from druks.accounts.dependencies import current_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep

from .constants import CONVERSATION_HEADER
from .models import Conversation


async def current_conversation(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> AsyncIterator[Conversation | None]:
    """The conversation that the request header names. It must be the account's own."""
    conversation = None
    conversation_id = request.headers.get(CONVERSATION_HEADER)
    if conversation_id:
        conversation = await Conversation.get_for_account(session, conversation_id, account)
        if not conversation:
            raise HTTPException(
                status_code=403, detail=f"Conversation {conversation_id} is not this account's."
            )
    token = current_conversation_id.set(conversation_id)
    try:
        yield conversation
    finally:
        current_conversation_id.reset(token)
