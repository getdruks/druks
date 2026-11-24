from druks.agents.constants import PRIVATE_AGENT_MESSAGE
from druks.db import db_session
from druks.exceptions import PermissionDenied
from druks.notifications.exceptions import DeliveryError
from druks.registry import channel_registry
from druks.schedules.models import Schedule
from druks.services.exceptions import ServiceNotConnectedError
from druks.workflows import FatalError, Workflow

from .app import Chat
from .models import Conversation


class RunInstruction(Workflow):
    """Does a task that a person gave in a chat, in a fresh box, as the chat's agent. The
    run reports the task and its answer to the chat. A run that a schedule starts also
    posts the answer in the schedule's channel. The report names the channel that took the
    post. When the channel does not take the post, the report says why."""

    async def run(self, instruction: str, schedule_id: str = "") -> dict[str, str]:
        conversation = await db_session().get(Conversation, self.conversation_id)
        if not conversation:
            raise FatalError("The conversation was deleted with its agent.")
        # The agent can change owner or turn private after the conversation began.
        if conversation.agent and not conversation.account.can_talk_to(conversation.agent):
            raise PermissionDenied(PRIVATE_AGENT_MESSAGE)
        answer = await Chat.run_instruction(agent_id=conversation.agent_id, instruction=instruction)
        # A schedule starts the run outside any turn, so the chat holds no call that names
        # this run. The task in the result tells the agent which ask the answer is for.
        result = {"task": instruction, "answer": answer}
        # The owner can change or stop the schedule while the task runs. An empty answer
        # posts nothing. A refused post leaves the run as it is: the result tells the
        # chat why.
        schedule = await Schedule.get_for_account(db_session(), schedule_id, self.account_id)
        if schedule and schedule.channel_id and answer:
            # The channel that posts outside a conversation, as for an account's notifications.
            (channel,) = [
                channel for channel in channel_registry.all() if channel.has_account_notifications
            ]
            try:
                await channel.send_in_room(
                    db_session(), schedule.channel_id, answer, agent=conversation.agent
                )
            except (DeliveryError, ServiceNotConnectedError) as error:
                result["post_failure"] = str(error)
            else:
                result["posted_in"] = schedule.channel_id
        return result
