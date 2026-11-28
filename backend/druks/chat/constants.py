import hashlib
from pathlib import Path

from druks.mcp.constants import DRUKS_SERVER_NAME

# The dot keeps the name outside NAME_PATTERN, so no registry server can share its vault row.
CHAT_KEY_NAME = f"{DRUKS_SERVER_NAME}.chat"
CHAT_BRIDGE_PORT = 43123
# A running bridge echoes the hash it was launched with, so a deployment finds the bridges
# of an older Druks.
BRIDGE_VERSION = hashlib.sha256(Path(__file__).with_name("bridge.mjs").read_bytes()).hexdigest()
# The bridge statuses with a live session and no running turn.
BRIDGE_SETTLED_STATUSES = ("idle", "replied", "cancelled")
TRANSCRIPTION_TIMEOUT_SECONDS = 120.0
# The header an agent's MCP calls carry to name their conversation.
CONVERSATION_HEADER = "X-Druks-Conversation"
# Every agent's prompt ends with this, so the agent knows an internal message when it
# reads one.
INTERNAL_MESSAGES_PROMPT = (
    "Druks, the platform that runs you, also writes to you. Its messages have the form "
    "[Internal: ...]. The person never sees them. Say what the person must know in your "
    "own words."
)
# Each turn opens with the time, so the agent never guesses it.
TIME_MESSAGE = "[Internal: The time is {now} ({timezone}).]"
# A new web conversation is named from its first message, so the name can arrive while the
# first reply runs.
NAMING_PROMPT = (
    "Name this conversation in at most six words. Reply with the name only.\n\nMessage: {message}"
)
# Druks tells a run's conversation how the run ended with one of these templates.
RESULT_MESSAGE = "[Internal: Run {run} ended. Its result:\n{result}]"
NO_RESULT_MESSAGE = "[Internal: Run {run} ended with no result. Tell the person it is done.]"
# No tool of the agent reads a finished run's subject, so the agent names it and never
# guesses what the run did.
NO_RESULT_ON_SUBJECT_MESSAGE = (
    "[Internal: Run {run} finished. It returned nothing, because its output is on its "
    "subject, the {type} {id} ({key}). Tell the person that the run finished and that its "
    "output is on {key}. Say what the run did only when one of your tools shows it. Do not "
    "guess.]"
)
PARKED_MESSAGE = (
    "[Internal: Run {run} waits for an answer. The request:\n{request}\nTell the person "
    "what it waits for and where to answer it.]"
)
PARKED_FOR_APPROVER_MESSAGE = (
    "[Internal: Run {run} waits for an answer from {approver}. Druks asked {approver}, and "
    "only they can answer it. The request:\n{request}\nTell the person what the run waits "
    "for, and that only {approver} can answer it.]"
)
PARKED_FOR_REVIEW_MESSAGE = (
    "[Internal: Run {run} waits for a review by someone Druks asked. Tell the person that "
    "the run waits for a review.]"
)
# The thread's buttons answer the run. A click that cannot carry the whole answer reaches
# the agent as the person's message, so the agent asks for the rest.
PARKED_BUTTONS_MESSAGE = (
    "[Internal: Run {run} waits for an answer. Its buttons are in this thread, with its "
    "ask. The person already knows what the run waits for, so do not repeat the ask "
    "unless they ask about it. The request:\n{request}\nA click on a button answers the "
    "run, and you then get the answer. A click that cannot carry a note or answers that "
    "its button needs reaches you instead as the person's message with the button's name. "
    "Then ask them for what is missing, and answer the run with answer_gate.]"
)
CLICK_ANSWER_MESSAGE = (
    "[Internal: The person answered run {run} with a button in this thread. Their answer:\n"
    "{answer}]"
)
QUOTA_RESUMED_MESSAGE = (
    "[Internal: Run {run} continues after its usage-limit wait. Tell the person in one short line.]"
)
FAILURE_MESSAGE = (
    "[Internal: Run {run} failed: {failure}. Tell the person in one short line, with no "
    "error details, and offer what they can do next. Do not retry it.]"
)
CANCELLED_MESSAGE = (
    "[Internal: Run {run} was cancelled. The reason: {reason}. Tell the person in one short "
    "line. Do not start it again.]"
)
SKIPPED_MESSAGE = (
    "[Internal: Druks skipped the scheduled call of {tool}: {reason} Tell the person in one "
    "short line, and offer what they can do next. The schedule goes on.]"
)
# Druks sends this line itself: the agent no longer answers the schedule's owner, so its
# turn would only refuse.
REFUSED_MESSAGE = (
    "Druks skipped the scheduled call of {tool}: {reason} The schedule goes on. "
    "To stop it, ask Druks in another chat."
)
# Druks sends this line itself for a run's report: the agent no longer answers the
# conversation's person, so its turn would only refuse.
REFUSED_RUN_MESSAGE = "Your run {run} {outcome}. The agent does not tell you itself: {reason}"
# The workflow that posts a cancelled run's report to its chat, by the name a run queues it.
POST_CANCEL_WORKFLOW = "chat.post_cancel"
# A channel turn that ends with no answer gets this once; the next empty turn fails.
NO_ANSWER_MESSAGE = (
    "[Internal: Your turn ended with no answer for the person. Write the answer now from "
    "what you already did. Do not repeat a tool call you already made.]"
)
# The line between what a person typed and what they said in the same message.
VOICE_NOTE_MARKER = "[Voice note]"
# The person reads this, so it holds no error details.
DELIVERY_FAILED_MESSAGE = "The reply is unavailable. Send the message again to retry."
# Druks sends this itself when a turn stops before it finishes: the backend posts no reply
# otherwise, so the person would wait for one that never comes. It holds no error details.
INTERRUPTED_MESSAGE = "This turn stopped before it finished. Send the message again to retry."
# Shown to an operator when the stop was a usage or rate limit, with the Providers link
# appended, as the disconnected-provider reply does. A non-operator gets INTERRUPTED_MESSAGE.
USAGE_LIMIT_INTERRUPTED_MESSAGE = (
    "This turn stopped: the agent's provider hit its usage or rate limit."
)
# Druks sends this line itself: the chat's agent shares the run's login, so its turn
# would fail on the same limit.
QUOTA_WAIT_MESSAGE = "The run hit the provider's usage limit. It continues at about {resumes_at}."
TRANSCRIPTION_FAILED_MESSAGE = (
    "[Internal: Druks could not turn the person's voice note into text. Tell the person "
    "in one short line to write it instead.]"
)
FIELDS_TEXT_LIMIT = 20_000
# The list, a field, its options, an option, and its literals.
FIELDS_DEPTH_LIMIT = 5
REFUSED_FIELDS = {
    "SecretField": (
        "A form takes no SecretField. A secret that a person types in Slack stays in Slack. "
        "Do not ask for a secret in a chat."
    ),
    "UploadField": (
        "A form takes no UploadField. Slack shows a file input only in a modal, and a form "
        "is a message. Ask the person to send the file in the chat."
    ),
    "MultiUploadField": (
        "A form takes no MultiUploadField. Slack shows a file input only in a modal, and a "
        "form is a message. Ask the person to send the files in the chat."
    ),
}
CHECKED_ANSWER = "Yes"
UNCHECKED_ANSWER = "No"
EMPTY_ANSWER = "(no answer)"
# A reply's blocks can hold a long table, so they take more text than a form.
BLOCKS_TEXT_LIMIT = 50_000
BLOCKS_DEPTH_LIMIT = 20
REFUSED_BLOCKS = {
    "Form": (
        "A reply takes no Form. A form runs an operation of an app. To ask for answers, "
        "call chat_ask with fields."
    ),
    "Action": (
        "A reply takes no Action. An action runs an operation of an app, and a reply runs "
        "none. Give a Link, or tell the person what to do."
    ),
    "GateControls": (
        "A reply takes no GateControls. A run's question comes with its own buttons. Tell "
        "the person where to answer it."
    ),
    "SubjectStatus": (
        "A reply takes no SubjectStatus. Druks reads no status for a reply, so the value "
        "shows nothing. Write the status as a StatusValue."
    ),
    "subject": (
        "A reply takes no subject=. A reply opens no page of a subject. Give the link its "
        "url= instead."
    ),
    "page": (
        "A reply takes no page=. A reply opens no page of an app. Give the link its url= instead."
    ),
    "follows": (
        "A reply takes no follows=. A reply does not change after Druks sends it. Leave "
        "follows= out."
    ),
}
