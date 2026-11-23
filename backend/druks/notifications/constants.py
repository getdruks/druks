# What a notification's message says in place of its buttons once its question is over.
CLOSED_MESSAGE = "This question is closed."

# What the run's account reads when the run fails and no chat reports it.
RUN_FAILED_MESSAGE = "Your run {run} failed: {failure}"

# The names of a run's inputs: a question's id after the prefix, and the note's own name.
QUESTION_FIELD_PREFIX = "question:"
NOTE_FIELD = "note"

# What a person reads who clicks a control that needs a note with an empty note input.
NOTE_REQUIRED_MESSAGE = "This button needs a note. Write the note, then click the button again."

# The workflow that closes the messages of a run's question, by the name a run queues it.
CLOSE_WORKFLOW = "notifications.close"

# The workflow that closes the message of a chat agent's question, by the name a chat queues it.
CLOSE_QUESTION_WORKFLOW = "notifications.close_question"

# The workflow that sends one notification, by the name a producer queues it.
SEND_WORKFLOW = "notifications.send"

# The workflow that shows the status of a run again, by the name a run queues it.
REFRESH_STATUS_WORKFLOW = "notifications.refresh_status"

# The reason of the one notification that shows the status of a run.
STATUS_REASON = "run.status"

# What the callout at the end of a run's status says while the run waits and after it
# ends, and its tone. A run that works adds no callout.
RUN_STATE_CALLOUTS = {
    "parked": ("Waiting for an answer.", "warning"),
    "finished": ("Finished.", "success"),
    "failed": ("Failed.", "danger"),
    "cancelled": ("Cancelled.", "warning"),
}

# What the callout of a parked run says when its ask has a label.
ASK_CALLOUT = "Waiting for an answer: {label}"

# The status and tone that each step in progress takes in those states of the run.
RUN_STATE_STEPS = {
    "parked": ("waiting", "active"),
    "finished": ("finished", "success"),
    "failed": ("failed", "danger"),
    "cancelled": ("cancelled", "neutral"),
}

# The ends that a run announces in its own last step. DBOS records them after that step.
RUN_ENDS = ("finished", "failed")

# Druks sends a status this long after its last change. Changes that come close together
# then go out in one edit.
STATUS_DELAY_SECONDS = 1

# A status that keeps changing goes out this long after its first unsent change.
STATUS_DELAY_LIMIT_SECONDS = 5

# The lock that keeps one send of a status at a time. The id of the status names it,
# because a retry gives the status to another run.
STATUS_LOCK = "notifications:status:{status}"

# What a person reads when they cannot answer a run of an app that a team lists.
TEAM_ONLY_MESSAGE = "Only an admin or the team that uses {app} can answer this run."

# The line that takes the place of an action in a notification once its route answers.
ACTION_RUN_MESSAGE = "{username} ran {label}."

# What a person reads when the position of their click holds no action any more.
ACTION_GONE_MESSAGE = "This action already ran."

# What a person reads when another click on an action of the message still runs.
ACTION_RUNNING_MESSAGE = "An action of this message runs now. Click again when it ends."

# One action or one edit of a notification's message at a time, keyed by run.
NOTIFICATION_LOCK = "notifications:{run}:{notification}"

# What a person reads when a review names another account as its approver.
APPROVER_ONLY_MESSAGE = "Only {approver} can answer this run."
