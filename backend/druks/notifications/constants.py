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

# What a person reads when they cannot answer a run of an app that a team lists.
TEAM_ONLY_MESSAGE = "Only an admin or the team that uses {app} can answer this run."
