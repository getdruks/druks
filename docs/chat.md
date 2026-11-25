---
title: "Chat"
description: "Talk to an agent, queue messages, and inspect its Druks tool calls."
icon: "messages-square"
---

Chat lets you talk to an agent that acts on Druks as your account.
A conversation is a live agent session, not a workflow. It has no gate, and it
does not park. Chat does not add entries to Activity.

Only the creator can read a conversation or receive its live events. Operators
do not see the chats of an app's WhatsApp numbers. The people who hold the
number's phone read them there. There is one exception. An operator who can use
an app can read the calls of its phone numbers. See [Read a call](#read-a-call).

Chat runs on Claude, Codex, or OpenCode. The Bot's harness, model, billing, and
effort come from Chat's row in **Settings → Bots**. A field that you
leave unset uses the
[installation settings](configuration.md#personal-and-installation-settings).
A conversation with an agent runs on the agent's harness and model: see
[Talk to an agent](#talk-to-an-agent).
OpenCode ignores the effort. Its effort values are model variants. Druks's
levels are not among them. OpenCode uses only the provider of Chat's model, also
when it names a conversation. If OpenCode's model list does not have the model,
OpenCode answers with another model of that provider.

## Start a conversation

1. Open **Chat** in the dashboard sidebar.
2. Select **New conversation**.
3. Write your message.
4. Select **Send**, or press Enter.

The first message creates the conversation. It shows as **New conversation**
until the harness names it from that message. The harness names it in a short
separate call, at the same time as the first reply.
New lines use Shift + Enter.

The starter prompts fill the message field. They do not send a message.
The new conversation page also lists the installed apps.

## Talk to an agent

An agent can answer a conversation instead of the Bot. Make an agent on the
**Agents** page. Give it a name, a description, instructions, skills, a harness
and a model. Make it private or public. Its owner or an admin edits and
deletes it. A delete also deletes every conversation with the agent and stops
their schedules.

In a new web conversation, choose the agent in **Talk to**. The list shows only the
agents that you can talk to. The conversation list and the conversation header show the
agent's name.

Everyone talks to a public agent. Only the owner of a private agent talks to
it. The conversation keeps its agent. Once a private agent has another owner,
or a public agent turns private, the next turn of everyone but its current owner
fails.
Each turn adds the agent's instructions to the Chat prompt. The turn runs on
the agent's harness and model, with the tool sign-ins of the person who asks.
Druks saves an agent only on a harness that Chat runs on: Claude, Codex, or
OpenCode. A turn reads the agent when it starts. An edit to the agent
reaches the next turn.

A private agent runs on its owner's subscription. With
[Include apps](configuration.md#personal-and-installation-settings) on, it
loads the apps of that subscription.

A public agent runs on the installation's API key for the model's provider or, without
one, on the default account's subscription. It never runs on the subscription of the
person who asks. A public agent with neither fails its turn and says what to connect. A
public agent never loads the apps of the subscription that it runs on. Each person who
talks to an agent gets a sandbox of their own for it.

A workflow that an agent starts runs as the person who asks. That person must be
able to use the workflow's app.

## Find and pin conversations

Search filters conversation titles. It ignores letter case and does not search
message text. Search does not close the current conversation or clear its draft.

Select the pin beside a conversation or in its header to keep it in **Pinned**.
Druks saves pins for your account. They stay after a page reload. Select the pin
again to remove it. A pin does not change a conversation's message times.

The remaining conversations appear under **Today**, **Yesterday**, or **Earlier**,
in the timezone of your preferences. Each group shows the most recent message
first. The header shows the last saved agent reply time. Times include a date
for messages from previous days.

On a narrow screen, Chat shows either the list or the conversation. Use
**Back to conversations** to return to the list. Your draft stays in the message
field when you switch conversations.

## Wait for a reply

Replies use the agent's name, or **Agent** in a conversation with the Bot.
Messages that Druks writes for the agent use **Druks**. Your messages use **You**.

**Connecting…** appears at the next reply position until the agent can reply.
When the agent has your message, the line says that the agent is replying,
until its first text or tool row. The first message can take longer than later
messages. The composer stays available while you wait.

On Slack, GitHub, and WhatsApp, Druks reacts with 👀 to your message when
delivery starts, before the sandbox is ready. A paused conversation gets none.

## Send, stop, and send again

You can send a message while the agent replies. Druks saves the message and
shows **Queued** until its turn. A conversation processes one turn at a time.
In the web chat, a turn consists of one message and its reply. In Slack, GitHub,
and WhatsApp, a turn takes every pending message of the conversation.

**Stop** appears beside **Send** during Connecting and while a turn runs.
It cancels only that message, not the conversation. Other queued messages
go next. Sandbox startup continues. The next message reuses the sandbox, or
the sandbox expires with its lease.

A cancelled turn shows **Cancelled**. Druks keeps the text that the agent
already sent. **Send again** saves a new message with the same text and file.

If a turn dies, Druks shows **Interrupted**. Druks never sends that turn again
by itself. **Send again** saves a new message with the same text and file. Such a
message can cause the agent to do an external action again.

Druks tries a delivery up to five times. It stops at the first attempt when a
person must change the Chat agent's settings or provider credentials. When the
delivery fails, the pending messages show **Failed**. Druks never sends them
again by itself. **Send again** saves a new message with the same text and file.
A turn that the agent already runs keeps running. The next delivery saves
its reply. In a Slack, GitHub, or WhatsApp conversation, Druks also says in the
channel that the message failed. It does not say so in a paused conversation, or
after someone removes its connection. When a person must change a setting or
credential, the reply names it, except in a Bot's conversation. When the provider
is not connected, the reply also gives the link to **Settings → Providers**
when `urls.endpoint` is set. Connect the provider first. For any other failure,
send the message again in the channel to retry.

A browser connection loss does not send the message again. The page connects
again and reads the saved messages and available live events.

## Read replies and tool calls

Agent text streams into the conversation. A tool row shows its ACP title and status.
Select the row to read its input and output. Tool rows have no timestamps. Saved replies
keep their tool details and their position between text segments.

Slack, GitHub, and WhatsApp get the text after the agent's last tool call. The text
before it holds the agent's notes. Only the Chat page shows them.

When the agent writes nothing after its last tool call, Druks asks it once for the
answer in an internal message. When that turn ends without text too, the channel gets
"The reply is unavailable. Send the message again to retry." A turn that asks a
[question](#questions) or shows [blocks](#blocks) has an answer, even with no text.

An agent plan shows the newest plan in full. A plan update replaces the
previous list. Tool updates instead change only the fields that the adapter
sends. Completed replies retain text, tool calls, and blocks, not the live plan.

A run that the conversation started can report its status: see
[Report status](writing-an-app.md#report-status). The page shows the status as a
message of its own and draws its blocks as an app page draws them. Druks does not
record which message started the run. The page places the status by time: after
each turn that started at or before the run. Each report replaces the status. The
page adds no message. While the run waits, and after it ends, the status also shows
where the run stands, for example "Failed.". The status stays after the run ends. A
retry of the run keeps the status and its place. The page reads the statuses again
every two seconds. The status follows the run with no reload. A link to an app page
in the blocks opens that page of the run's app. When no installed app declares the
run's workflow, such a link shows as text.

While a run that the conversation started is active and shows no status, the page
shows one line for it below the messages. The line names the workflow and its
subject. It says what the run does now. The run is queued, waits for an answer, or
runs a step, such as an agent call or a wait for CI. The page reads the runs again
every two seconds. The line changes with the run. It goes away when the run
ends. Only the Chat page shows it.

The page stays at the bottom while text arrives. Scroll up to read earlier
messages. **Latest message** returns to the bottom.

## Questions

The agent can ask you a question with two to four short choices, or with a
[form](#forms) of fields. It calls `chat_ask` during its turn. The call
returns at once. The agent does not wait for your answer. `chat_ask` is a tool of
the Druks toolkit. The agent of an operator has it, on the web and on each
channel. The agents of a public WhatsApp number have only the tools that their Bot
names. They do not have it.

When the turn ends, Druks sends the question after the agent's reply:

- In the web chat, the question shows under the reply, with a button for each
  choice. A form shows an input for each field and one **Submit** button. The question
  stays out of the reply's text.
- In Slack, the reply, the question, and a button for each choice come in one
  message. A form comes the same way, with an input for each field and a
  **Submit** button. Druks cuts the end of a reply that is too long for one
  message. The question, the buttons, and the inputs stay whole.
- In WhatsApp and GitHub, the reply ends with the question. Its choices or fields
  follow as a numbered list, with the options of a field below the field. Answer
  with a number or in your own words. The agent reads your message as your answer.

In the web chat, when you select a button, its choice is your next message, as if
you wrote it. You can also write your answer in the message field. The agent reads one
message in each turn. Your answer is your next message after the one that
started the turn. You can send it while the agent replies. It then waits as
**Queued**. The reply then shows the question with your answer in place of
the buttons. A message that Druks writes for the agent answers nothing. The page
finds the answer in the saved messages. A reload shows the same answer.

In Slack, when you select a button, its choice reaches the agent as your next
message. The message then shows your choice in place of the buttons. Only the
person of the conversation can select one. When you write an answer instead, it
reaches the agent as any message does. When your message reaches the agent, Druks
takes the buttons off each open question of the conversation. The message of each
question then says that the question is closed. When you write while the agent
asks, your message can close the question before Druks posts it. Druks then posts
the reply and the question with no buttons. The message says that the question is
closed.

A turn that you stop asks nothing. Neither does a turn of a paused conversation.

### Forms

The agent asks with a form when it needs several answers, or an answer in your own
words. It writes the fields as one Python list of `druks.ui` field calls, as app
pages do:

```python
[TextField(name="title", label="Title"), SelectField(name="where", label="Where", options=[Option("Staging", value="staging")])]
```

A form takes `TextField`, `TextAreaField`, `NumberField`, `SelectField`,
`MultiSelectField`, `RadioField`, and `CheckboxField`, by their bare names or with
the `ui.` prefix. Each argument has the name that `druks.ui` gives it, such as
`help_text`. Only the label of an `Option` goes without its name. Druks reads the
list with Python's `ast` module and runs no code. It takes only literals, lists,
and these calls. Pydantic checks each field. A form takes no `SecretField`,
because a secret that you type in Slack stays in Slack. It takes no `UploadField`
or `MultiUploadField`, because Slack shows a file input only in a modal. In a
Slack conversation, Druks also refuses a form that Slack cannot show in one
message, such as a form of more than 48 fields. The agent gets the reason in the
same turn.

In Slack, fill in the inputs and select **Submit**. Druks checks your answers. A
required field needs an answer. A number field takes a number within its minimum,
its maximum, and its step. A choice takes one of its options. When an answer
does not fit, only you see why. The form stays open for you to correct it.
Your answers reach the agent as your next message, with one line for each field:
its label, then your answer. A checkbox reads Yes or No. A field that you
leave empty reads (no answer). The message then shows your answers in place of
the inputs. Only the person of the conversation can submit the form. When you
write an answer instead, the form closes as a question with buttons does.

In the web chat, fill in the inputs and select **Submit**. A required field needs
an answer. A required checkbox needs a check. The browser checks the inputs
first. It shows its own message at an empty required input, and at a required
checkbox that is not checked. It also shows one at a number outside its minimum,
its maximum, or its step. The page then checks the answers that the browser lets
through, such as text of only spaces or a multi-select with no option checked. It
says what is wrong under the field. In both cases, the page sends nothing. Druks
does not check the answers again. They go as an ordinary message, with the same
lines as a Slack Submit. The reply then shows your answers in place of the inputs.
When the page cannot send the message, the inputs keep your answers. You can
select **Submit** again.

## Blocks

The agent can show blocks in its reply, such as a chart, an image, a table, or a grid
of facts. It calls `chat_show` during its turn. It gives the blocks as one Python
list of `druks.ui` block calls, as app pages write them:

```python
[Chart(kind="bar", title="Builds this week", categories=["Mon", "Tue"], series=[ChartSeries(label="Builds", points=[4, 7])])]
```

The agent writes each block and each part of a block with its bare name or with the
`ui.` prefix. The parts are classes such as `Fact`, `Metric`, `ChartSeries`,
`TableRow`, and `StatusValue`. Some classes take their first arguments without a name,
in order, as in `Fact("Owner", "Ana")`. The agent gives every other argument by its
`druks.ui` name. A fact, a metric, and a table cell take a string or a number as their
value.

Druks reads the list with Python's `ast` module and runs no code. It takes only
literals, lists, and these calls. It checks each block as it checks the blocks of an
app page. A reply runs no operation and does not change after Druks sends it.
Druks refuses a `Form`, an `Action`, and `GateControls` at every depth, such as an
action in a `ControlsValue`.
Druks also refuses `SubjectStatus`, because a chat has no status to show. It refuses
the `subject=`, `page=`, and `follows=` arguments too. The agent gets the reason in the
same turn and can call `chat_show` again. A second call that Druks accepts replaces the
blocks of the first. Like `chat_ask`, `chat_show` is a tool of the Druks toolkit.

When the turn ends, Druks sends the blocks after the agent's text:

- In Slack, the text and the blocks come in one message. Slack draws them as it draws
  the blocks of an approval: see [Approvals in Slack](druks-ui.md#approvals-in-slack).
  Facts and metrics show as Slack's own fields. A table shows as a data table. A
  divider shows as a divider. A link shows as a button. A chart shows as a Slack
  chart. Progress steps and a timeline show as a plan of tasks. An image with a
  public address shows as an image. So does each such image of a gallery. A card
  shows as a card, and a list of cards as a row of cards. A section with a title
  shows as a group under its title. Columns show side by side. Any other block,
  and a block past a limit of Slack, shows as its
  [text form](druks-ui.md#text-forms). Druks cuts the end of a reply that is too long
  for one message. When Slack refuses the blocks, Druks sends their text form in
  their place.
- In the web chat, the Chat page draws the blocks after the reply's text when the
  turn ends. It draws them as an app page does. While the reply streams, the page
  shows no blocks. When the page cannot draw a block, it shows the block's
  [text form](druks-ui.md#text-forms) instead. It still shows the text and draws the
  other blocks. The Chat page also draws the blocks of a Slack reply.
- In WhatsApp and GitHub, the reply ends with the text form of the blocks.

In Slack, Druks uploads the image of a Druks file when the account of the conversation
can use the app of the file. Each person who sees the message then sees that image.

A turn can show blocks and ask a question. In Slack, you then get one message: the
text, the blocks, the question, and its buttons or inputs. When Druks cuts the rest to
fit, the question, the buttons, and the inputs stay whole. They also stay whole when
Slack refuses the blocks, when the question closes, and when you answer it. In the web
chat, the question follows the blocks. In WhatsApp and GitHub, the numbered question
follows the text form of the blocks. A turn that you stop shows no blocks.

## Agent access

The agent uses the installation's `/mcp` endpoint as the conversation's owner.
An operator's Chat key permits every tool of the Druks toolkit: the routes tagged
`agent`. Builds keep a separate key with their restricted tool list. A key with a
tool list lists and calls only those tools. The agents of a public WhatsApp number
have such keys: see [WhatsApp](#whatsapp).

An operator's agent also reaches every MCP server that is enabled in **Settings → MCP
servers**, with the same credentials as a workflow agent. Chat leaves out a server that
cannot authenticate for you, such as an OAuth server that you did not connect. The
agent of a public number reaches only the Druks server. When you enable, disable, or
connect a server, the next turn replaces the sandbox.

An operator's sandbox also holds their own sign-in at each service that names a
host. The command line acts as that person. With a GitHub sign-in, git and
`gh` act as you. Your name is on the issues and comments. The token permits
only what both you and the App can do. Druks refreshes the token before it
expires. Before each turn, Druks makes the App's bot user the author of the
sandbox's commits. Druks asks GitHub for the bot's user id once and keeps it. If
GitHub does not answer, the turn runs without that author or the co-author line.
The next turn asks again. A commit in a repo that the agent clones or
creates afterwards gets a `Co-Authored-By` line for your GitHub user. A repo with
its own hooks directory, as husky sets, gets no such line. Without a sign-in the
sandbox has no access to that service: see
[GitHub](#github). The agent's prompt names each MCP server and sign-in it holds,
and tells it where you connect a missing one. A Bot's sandbox never holds a sign-in.
A build still acts as the App: see [GitHub](configuration.md#github).

The agent can change Druks through those tools. Chat has no permission dialog
or proposal mode. Claude runs in bypass mode and cannot call `AskUserQuestion`.
Codex runs in full-access mode for an operator. For a Bot it runs in read-only
mode without shell, web, or image tools. OpenCode runs its default agent with
every permission allowed for an operator. For a Bot it runs an agent that
denies the file, shell, and web tools.

A run that the agent starts with a tool records its conversation. When the run
parks with a question, Druks adds an internal message with the request to that
conversation. On a WhatsApp number with an admin, the admin answers instead: see
[Confirmation](#confirmation). A question that names an
[approver](writing-an-app.md#wait-for-input) asks the approver instead, on any
source. The conversation gets no buttons, because only the approver can answer.
In an operator's conversation, the message holds the request and the name of the
approver. The agent tells you. A conversation that a bot or bot admin account
owns reads only that the run waits for a review. A chat on a public WhatsApp
number and a call are such conversations. A conversation of the approver's own
account is the exception. Druks asks there, as it does for a question with no
approver. The request is the ask as JSON, as
`get_gate` gives it. A review's context is text, or the JSON of its
[blocks](writing-an-app.md#wait-for-input). Druks reads each subject status in the
blocks when it writes the message. In a Slack conversation, you also get the
question of a run that waits for an answer in the dashboard. You do not get a question
that names another account as its approver. The question that you get comes in the
thread or direct message, with a button for each choice. Only the person who started the
run can click them. The question with its buttons is the only message that you get
about it. Druks keeps the internal message for the agent's next turn, such as the
turn of your next message there, and starts no turn for it. The agent then knows
that the run waits and what it waits for. The Slack message also has an input for
each of the question's own questions. The input is a menu of its options or a box
for your own words. A last box takes your note. A click sends them with your choice and answers
the run. The agent does not ask for them. When the run waits for the provider's
usage limit to reset, Druks itself posts a line. The line gives the time the run
continues, in the installation's timezone. The agent shares the run's login. Its own turn fails
on the same limit. When the run continues, Druks adds an internal message that
says so. When the run fails, Druks adds an internal message with the failure to that
conversation. When someone cancels the run, Druks adds an internal message with the
reason. A cancel that the agent of the same conversation made adds none, because
the agent already told you.
When the run ends, Druks adds an internal message with its result, whether or
not the run waited for an answer. When a run returns nothing, Druks adds a message
that says the run ended. A review that posts itself is such a run. The agent then
tells you in its own words.
When the agent no longer answers you, Druks posts a line itself in place of an
internal message that starts a turn, and no agent turn runs. The line says what
happened to the run and why the agent does not tell you. It links to the run
when `urls.endpoint` is set.
In a Slack conversation, a workflow that reports its status keeps that status in
one message of the thread or direct message. Druks edits it as the work goes
on. See [Report status](writing-an-app.md#report-status). The Chat page shows the
status in the conversation, whatever its channel: see
[Read replies and tool calls](#read-replies-and-tool-calls).

The sandbox receives credential placeholders. The Drukbox proxy exchanges them for real
credentials. See
[public URLs and access control](configuration.md#public-urls-and-access-control) for
the MCP address and edge requirements.

## Execution and recovery

Each account has one leased sandbox that serves its conversations with the Bot, and one
more for each agent it talks to. Neither replaces the other. When a sandbox's
credentials change, the next turn replaces that sandbox. A turn
still running in the old sandbox ends as `interrupted`. A detached
bridge runs in the sandbox and starts one ACP adapter per conversation. Druks
opens an SSH channel only when it communicates with the bridge.

Postgres stores conversations and messages. A reply links to the message it
answers and keeps the tool calls it made. A web reply also keeps its question.
A web or Slack reply keeps its blocks. Your messages have seven durable
states: `pending`, `deferred`, `delivered`, `replied`, `interrupted`,
`cancelled`, and `failed`. A `delivered` message is the one the agent is
answering. A `deferred` message is an internal message that waits for the
agent's next turn and starts none. Redis stores the delivery locks, the read
positions, the live events, and the question and the blocks of the running turn.
The bridge determines whether a turn runs.

Delivery and Stop each use a conditional Postgres update from `pending`.
Delivery changes the state to `delivered` just before the prompt request. Stop
changes the state to `cancelled`. The first update wins. Delivery never sends
a message that Stop cancelled while pending. Delivery changes a `deferred`
message to `delivered` only after it won the update of a `pending` message in
the same turn. When Stop cancels every pending message first, Druks sends no
prompt. The `deferred` message waits for the next turn.

After delivery wins, Stop sends ACP `session/cancel` through the bridge.
The bridge matches the message ID. A delayed Stop cannot cancel the next
turn. Druks saves the partial reply and marks the message `cancelled`.

The bridge records the message ID when it accepts a prompt. When a delivery
fails, the next delivery asks the bridge for that ID. If the bridge holds the
message, Druks follows the turn. If the bridge is idle with another ID, the
prompt never arrived. Druks sends the message again. The bridge refuses a
prompt for an ID it already accepted. A turn never runs twice.

The bridge numbers the current turn's events and writes them to disk. Druks reads the
events after its last position and streams them only to the owner's open pages. After a
completed turn, Druks saves the adapter's session files as a Druks file. The next saved
archive replaces the previous archive. A conversation that moves to another harness
starts a fresh agent session and keeps its messages.

Before each turn, the bridge compares the conversation's instructions and MCP
servers with the ones its adapter opened with. When they differ, it starts a
new adapter and loads the same agent session. A deployed prompt change or a
newly connected MCP server thus reaches the next message. The history
stays. A model or effort change applies to the live adapter. Each Druks release
carries its own bridge program. A bridge of an older release exits once no turn
runs in it. The next turn starts the current one.

### Druks restarts

The agent keeps its process in the sandbox. Each delivery is a DBOS workflow.
When Druks starts again, DBOS runs an interrupted delivery again. It follows
the turn from the last read position and saves the reply.

### The bridge or sandbox dies mid-turn

When Druks next reaches the bridge, it finds the turn gone and marks it
`interrupted`.
Druks does not resend it. The latest saved session archive contains only
the session files from the last completed turn.

### The sandbox is idle

Nothing renews an idle sandbox. It expires with its lease. The next message
gets a new sandbox and reloads the saved session files. Each turn renews the
sandbox lease and its identity expiry. The lease is 270 minutes.

## Reminders

A reminder sends a message in a conversation once, or again on a cron. Ask the
agent for one in plain words. It calls `chat_create_reminder` with exactly
one of these:

| Field | Sends | Example |
| --- | --- | --- |
| `delay_seconds` | Once, that many seconds after Druks saves the reminder | `300` for "in 5 minutes" |
| `send_at` | Once, at that time. A time with no offset is local. | `2026-10-20T09:00` |
| `cron` | Again and again, in local time | `0 9 * * 1` for Mondays at 9 |

Local time is the installation `timezone`. Each turn opens with the current time
and that timezone, so that the agent never guesses them. Without
`conversation_id`, the reminder sends in the chat that asked for it. The answer
gives `nextSendAt` in local time. The agent tells the person that time.

Druks checks for due reminders every minute. A reminder sends up to a minute
after its time. It sends the words as written, as the agent's message, and runs no
turn. The message goes through the conversation's channel. It reaches WhatsApp,
Slack, or the web page. A reminder sends once for each due time. When Druks was
down at that time, it sends once on the next check and then waits for the next
due time.

| Caller | Where their reminders go |
| --- | --- |
| An operator | The operator's own conversations |
| A public number's admin | The direct chats of everyone who wrote to the number, and the groups that the admin turned on |
| Anyone else who writes to a public number | Nowhere. They get no reminder tools. |

On a public number, a person gets a reminder in their own chat only after they
wrote to the number. Druks never starts a chat there. On a private number,
the operator's agent can start one with `chat_start_conversation`. It takes the
person's phone number and name. The chat is the operator's own. Their
reminders reach it. WAHA confirms that the number is on WhatsApp. Druks sends
nothing until a reminder does. The person's replies reach no agent.

`chat_list_conversations` lists where the caller can send. `chat_list_reminders`
lists the caller's reminders with each one's cron and next time.
`chat_cancel_reminder` stops a reminder. What it already sent stays in the
conversation.

## Schedules

A schedule makes one of the agent's tool calls again and again on a cron. Say
"review pull request 7 in acme/api every morning". The agent then calls
`create_schedule` with the tool, its arguments, and the cron. The schedule
belongs to the person who asked and to the conversation where they asked. An
agent has no schedule of its own.

| Field | What it holds | Example |
| --- | --- | --- |
| `tool` | The name of one of the agent's tools | `software_factory_review` |
| `arguments` | The tool's arguments, the same at every call | `{"repo": "acme/api", "prNumber": 7}` |
| `cron` | When to call, in local time | `0 9 * * *` |

Druks checks that the tool exists and that the cron has five fields when the
agent saves the schedule. Local time is the installation `timezone`, as for
reminders.

Each schedule has its own DBOS schedule. At each due time, Druks makes the tool
call through its own MCP server. It calls as the owner, from the
conversation, with the tools of the conversation's agent. The call goes
through the same checks as in the chat: the owner's app check, the tool's input
check, and the route's own checks. A run that the call starts reports to the
conversation, the same as a run that the chat started. When the tool refuses the
call, or Druks cannot reach its own MCP server, Druks skips the call. The agent
tells the person why in the conversation. When the agent no longer answers the
schedule's owner, Druks skips the call and tells the person itself. No agent
turn runs. DBOS records that due time as failed. The schedule goes on.

A due time makes one call, also when two Druks servers run. An interrupted call
can run again. Druks does not call a due time that passes while it is down.

`list_schedules` lists the caller's schedules with each one's tool, arguments,
cron, and next time. `change_schedule` replaces the tool, the arguments, and the
cron of one of the caller's schedules. `stop_schedule` stops it. The runs its
calls already started go on. An admin can also stop another person's schedule.

An agent's page lists the schedules that people set in the agent's conversations, each
with its owner, its last run, and how that run ended. A person sees the schedules whose
tool belongs to an app that they can use.

## Tasks

Some asks fit no tool, such as "get the price from this site every 2 hours".
For these, the agent calls `chat_run_instruction` with the task in words. The
tool starts a `chat.run_instruction` run in a fresh sandbox. Like every run, it shows
on the run page with its cost.

In the sandbox, the conversation's agent does the
task with its instructions and its model, on its billing, and with only its
picked skills. In a chat without an agent, the call uses the model that Settings gives the
`chat.run_instruction` instruction, and every enabled skill. The run
belongs to the person in the chat and reports the task with its answer, or its
failure, to the conversation.

To repeat a task, the agent schedules the tool:

```json
{"tool": "chat_run_instruction", "arguments": {"instruction": "Visit acme.test/kettle and get the price."}, "cron": "0 */2 * * *"}
```

## WhatsApp

WhatsApp is a source for Chat. A person writes to a linked number. Druks
saves the message in that person's conversation. The agent answers through its
MCP tools. Druks sends the reply back to WhatsApp. A workflow never sees a
message. Druks reaches WhatsApp through [WAHA](https://github.com/devlikeapro/waha),
an open source WhatsApp HTTP API. Connect WAHA first: see
[WhatsApp](configuration.md#whatsapp).

A number is public or private:

- **A public number**, such as an app's, answers anyone who writes. One person
  is the number's [admin](#admins).
- **A private number**, such as Chat's, answers only operators who paired their phone
  with it. Each operator talks to the Bot in a conversation of their own. It has no
  admin.

### Link a number

A linked number is a connection. Its owner account holds the WAHA session, the
session's key, and the webhook secret.

- **A public number.** In the app's settings, open **Channels** and select
  **Add number**. The tab shows only when the app declares a
  [Bot](writing-an-app.md#answer-whatsapp-with-a-bot). Druks creates a bot
  account and a bot admin account for the number. These accounts never sign in.
- **A private number.** An admin opens **Chat → Channels**, selects **Add
  number** and scans the QR code. To pair your phone, open **Settings → Phones**
  and select **Pair my phone** beside the number. The page lists the private
  Bots of the apps that you can use. Send the one-time code to that number from
  your own WhatsApp, or from the phone of the number to itself. You then talk to
  your agent at that number, under your account and with the whole Druks
  toolkit. If you use a second number, replies arrive as normal incoming
  messages. WAHA never sees your own chats. If you use the phone of the
  number itself, replies arrive in your chat with yourself. WhatsApp gives no
  sound for them. WAHA receives the chats of that phone. Several operators
  can share the number. Each operator pairs their own phone. Druks ignores every
  sender that has no paired phone. **Unpair my phone** removes your phones from
  the number. This number has no take-over.

Druks creates the WAHA session and its key, saves the connection, and then
writes the session's config. Scan the QR code from **Linked devices** in
WhatsApp on the phone, which shows the device as Druks. The number is live when
WAHA reports that the session works. When WhatsApp unlinks the device, the
number shows **Disconnected**. **Link again** takes a new scan on the same
connection. The connection keeps its chats, its admin, and its paired phones. Druks
refuses a number that another live connection holds. **Remove** deletes the WAHA
session and its key, and ends the pauses of the number's chats.
The connection and its chats stay as history. Each new link is a new
connection.

### Who writes

Each person who writes to a number has one conversation. Druks sends each
message to a conversation by its sender:

| Sender | Owner | Prompt | Tools |
| --- | --- | --- | --- |
| An operator's paired phone, on a private number | The operator's account | The Bot's `prompt` | The whole Druks toolkit |
| The number's admin, on a public number | The number's bot admin account | Druks's admin prompt | The Bot's `admin_tools`, `answer_gate`, `chat_resume_conversation`, the [reminder](#reminders) tools |
| Anyone else, on a public number | The number's bot account | The Bot's `prompt` | The Bot's `user_tools` |

The agents of a public number have no shell, file, or web tools. Their harness,
model, billing, effort, and timeout come from the Bot's row in the app's
**Settings → Bots**. A turn that runs past the timeout stops. Druks marks
it `interrupted`. A bot or bot admin account holds no credential of its own. Its
agents and runs bill the default account.

All pending messages of a WhatsApp conversation go into one turn. Druks saves
media as a Druks file on its message. An image reaches the agent with its
message. Any other file except audio reaches it as a link to a copy in the
sandbox. An operator's agent opens the copy with its tools. A number's bot
agents have no file tools. They see the link and the file's name. A shared
contact card reaches the agent as one line of text with the person's name and
phone numbers. An operator can share a card to name a person for
`chat_start_conversation` or a reminder, and type no number. Druks
drops a repeated message by its WhatsApp ID. Druks writes to a person to answer
them, one reply for each turn, or to send a reminder. It never sends in bulk.
Before it sends a reply, Druks takes a new message ID from WAHA and
records it. WAHA's copy of the sent message then carries a known ID, even when
the copy arrives before the send returns.

A voice note becomes text before its turn. Druks sends the audio to the
[Speech To Text card](configuration.md#speech-to-text) and saves the words on
the message. The agent reads them under what the person typed, marked as a
voice note, and answers in text. Nothing goes to the person before the reply.
When Druks cannot transcribe a note, when no card is connected, or when the
note is over 25 MiB, the agent gets an internal message. It then tells
the person in one line to write instead. The web page shows the typed text, the
words, and a link to the audio.

Druks also adds **internal messages** to a conversation. Each one comes from a
fixed template. An internal message starts a turn like every other message, unless it
waits for the agent's next turn: see [Agent access](#agent-access). It never
goes to WhatsApp.

### Groups

Add a linked number to a WhatsApp group as you add any contact. On a private
number, such as Chat's, only a paired phone reaches an agent in a group. Its tag
or reply opens that operator's own conversation for the group. The reply goes to
the group. Druks drops every other group message, the tags of people who are
not paired included.

On a public number, the group reaches no agent until the number's admin
tags the number there. That tag turns the group on and opens the admin's
conversation for it. After that, a message reaches the Bot when it tags the
number or replies to one of the number's messages. Druks drops every other
group message, saves nothing of it, and starts no turn. As in a
[Slack room](#rooms), each person who writes to the Bot gets their own
conversation, with the group as its thread. The admin writes
under the bot admin account. Everyone else writes under the bot account. The reply
goes to the group. Everyone in the group reads it. Druks removes the
number's tag from the text before the agent reads it. It gives each
conversation in the group the group's name as its title.

A tag decides when the agent answers, not what it sees. In a group, every agent
gets `chat_read_thread`. Its prompt tells the agent to call this tool first.
The tool returns the group's newest 200 messages from WAHA's store, oldest first.
Each has its author's id and name. It also says whether this agent wrote it, and
whether the person that the agent answers wrote it. The prompt also says that
only the message that reached the agent is an instruction.

WhatsApp can name a group member by a LID instead of a phone number. Druks asks
WAHA for the member's phone number, so that it knows a paired phone or the admin by
either name. A message that the number's phone sends in a group pauses nothing.
Druks drops it.

Druks turns on the session's message store when it links a number, so that WAHA
keeps the group history. A number linked before Druks read groups ignores them
until you remove it and add it again.

### Admins

Each public number has an admin from the moment it links: the person who holds
its phone. They talk to the admin's agent in the phone's chat with itself, **Message
yourself** in WhatsApp. WhatsApp does not ring for a message that an account sends
to itself. The phone shows Druks's questions in that chat without a sound. Druks
puts `[Druks] ` before each reply that it sends into a number's chat with itself.
Then you can tell the agent's replies from your own messages.

To get the questions on another phone, which rings, select **Add admin** on the
number. The button opens a one-time code that expires after 10 minutes. Druks then
sends the number's questions to the person who sends exactly that code from
their own WhatsApp. The admin's agent greets them. A number has one such
person. Both chats belong to the number's bot admin account, which never signs
in. The only text that Druks reads in a message is an open one-time code.

### Confirmation

A run records the conversation whose tool call started it. When such a run
parks with an in-app question, Druks adds an internal message to the admin's
conversation. The message holds the request, the person that it is about, the run ID,
and when the run started to wait. The admin's agent asks the admin, and answers
with `answer_gate`. When two questions are open, the run ID tells them apart.

Only the number's admin can answer a question that came from the number's
chats. The dashboard, `answer_gate`, and notification buttons all refuse
everyone else, operators included. A question that names an approver goes to the
approver instead of the admin. See [Agent access](#agent-access).

When a chat on the number starts a run, Druks reports the end of the run to that
chat. It does so for every [run that a chat starts](#agent-access). The Bot then
tells the person about a failure or a cancel in one short line. A failure gets no
error details and no retry.

### Taking over

A message that someone types on the number's phone pauses that chat. Druks
saves the typed text as an internal message. Messages that arrive during a pause
start no turn. They go into the chat's next turn. Druks tells the admin about the
pause. The pause ends when the phone stays quiet in that chat for 4 hours.
Each typed message starts the 4 hours again. To end it sooner, the admin tells
their agent, which calls `chat_resume_conversation`. The pause is a DBOS
workflow that the typed message names. A chat has at most one open pause.

## Slack

Slack is a source for Chat. You write to the Druks bot in a direct message, or
tag it in a room. The Bot answers there, under your account and with your whole
toolkit.
One Slack app serves every app in Druks. Connect the Slack card first: see
[Slack](configuration.md#slack). **Chat → Channels** shows the workspace, the
bot, and your Slack account while the card is connected.

### Link your account

Druks knows you by your Slack account. Select **Connect Slack** on the Slack
pane. Slack asks you to sign in. Druks saves your Slack token under your
account, like a Gmail connection. Other apps can use that grant. **Disconnect**
revokes it. Druks refuses a Slack account from another workspace.

When you write to the bot before you connect, the bot answers with a private
link. Open it, sign in to Druks, and connect Slack. Druks then answers the
message you wrote. The link works for 10 minutes. A forwarded link is harmless.
Slack's sign-in links whoever opens it. The held message waits for the
person who wrote it.

The first time the bot sends you that link, it sends a welcome before it. The
welcome says what the bot can do in this installation, what it reads, what you
connect first, and what it keeps. If you do not connect, you get the welcome
again after 30 days. The bot also posts the welcome in a room when it joins that
room.

### Direct messages

You have one conversation with the Bot in your direct message, and one with
each agent that you tag by name there. Druks drops a
repeated event by its event ID. The agent writes Markdown. Druks
posts each reply in the direct message, in pieces of at most 12,000 characters. A reply
that shows [blocks](#blocks) or asks a [question](#questions) goes as one message.
Druks ignores the bot's own messages, other bots' direct messages, edits,
deletes, joins, hidden events, and people from other workspaces.

### Files

In a direct message or a thread that you joined, a file that you send to the bot
becomes a Druks file on its message. A message with only a file starts a turn
like every other message. An image reaches the agent with its message. Every other file,
except audio, reaches the agent as a link to a copy in the sandbox. The agent
opens the copy with its tools. An image over 25 MiB goes as a link too.

An audio clip becomes text the way a WhatsApp voice note does: see
[WhatsApp](#whatsapp). Each further file in one Slack message gets a message of
its own. The agent attaches no file to a reply. When the agent shows the image of
a Druks file with `chat_show`, Slack shows the image: see [Blocks](#blocks).

### Rooms

Invite the bot to a channel or a group. Tag it in a message. The Bot
answers in that message's thread. A tag inside a thread answers in that thread.
Each person who tags the bot gets their own conversation for the thread. For 24
hours after the last message in your conversation there, your untagged replies
in the thread reach that conversation too. Other people must tag it. A
top-level message with no tag reaches nobody. When you tag the bot before you
connect Slack, it says in the thread that your account is not connected. It sends
you the link in a direct message.

To ask an agent, write its name right after the tag:
`@Druks Security engineer check this PR`. Case does not matter. When two names
match, Druks takes the longer one: "Security engineer", not "Security". A name
before the tag does
not count. A comma or a colon after the tag is fine. The agent answers in your
own conversation with it for that thread. One thread can hold your
conversations with several agents. A tag that names no agent reaches the
conversation you last wrote in there, or the Bot if you have none. Your untagged
replies reach the conversation you last wrote in, under the 24-hour rule above
and the tag rule below. When you name an agent you cannot talk to, the bot says why
in the thread and drops the message. The same tag works in a direct message,
where a name without the tag reaches the Bot.

An admin can give a public agent one Slack channel, under **Slack channel**
when they edit the agent. The bot must be in the channel. In that channel, a tag
that names no agent reaches that agent. This does not apply when you already
have a conversation in the thread, with an agent or with the Bot. A tag that
names another agent still reaches that agent. A top-level message without
the tag still reaches nobody. A channel has one agent at most. Choose
**No channel** to return the channel to normal chat.

An agent's replies in Slack show the agent's name. To give an agent a picture,
choose a png, jpg or gif file under **Picture** when you edit the agent. Slack loads
the image from Druks at `/media/<file-id>` on the webhook host, with no sign-in.
**Remove picture** returns the replies to the app's picture. The Bot's replies,
questions with buttons, and the bot's own notes, such as the link to connect Slack,
keep the app's name and picture. The app needs the `chat:write.customize` scope: see
[Slack](configuration.md#slack).

Another app's bot or a Slack workflow can tag the bot in a room too, after an
admin accepts it. Druks calls each of them an automation. The first time an
automation tags the bot, the bot says in the thread that it does not answer this
automation yet. It adds a link to the Slack pane of **Chat → Channels**. Under
**Automations**, the pane lists each automation that tagged the bot, by its name
in Slack. An admin selects **Accept**. The agent then answers the tag that waited
and each later tag of that automation, in the thread. **Remove** makes the
automation wait for an admin again. Until an admin accepts an automation, its
later tags reach nobody.

The agent of an accepted automation runs on the
[default account](configuration.md#personal-and-installation-settings), as
unattended runs do. That account's subscription pays. The agent uses its
connections. Everything that an accepted automation posts with a tag reaches
that agent, such as the text that a person types into a workflow's form. Accept
only an automation whose input you trust. An automation's untagged
message reaches nobody. Nobody can click the buttons of a question from such a
run. A person answers it in the dashboard.

Tell the agent to answer only when you tag it. Your untagged replies in that
thread then stop reaching it. The agent records your choice with `chat_require_tag`.
Tell it to answer your replies again. They then reach it again for the rest of the
24 hours. Your choice holds for that thread only. The Slack pane of
**Chat → Channels** sets the default for the threads that you tag the bot in
after that.

The agent reads the thread with `chat_read_thread`. It returns the newest 200
messages, oldest first. Each has its author's id and name, and the files on it.
It also says whether this agent wrote it, and whether the person that the agent
answers wrote it. A file stays in Slack. The agent reads one with
`chat_read_file`. Druks checks with Slack that the file is shared in the thread's
channel. Then it fetches the file into the agent's workspace. Every agent in a
thread posts as the one bot. Druks tells an agent's own replies by the Slack
ids it recorded. In a direct message the tool answers that there is no thread.

## GitHub

GitHub is a source for Chat. You tag the operator App in an issue or pull request
comment. The Bot answers there, under your account and with your whole
toolkit. The App's slug is the tag, for example `@druks-acme`. The channel is live
while the GitHub card is connected: see [GitHub](configuration.md#github).
**Chat → Channels** shows the tag and your GitHub account.

### Link your account

Druks knows you by your GitHub account. Select **Connect GitHub** on the GitHub
pane. GitHub asks you to authorize the App. Druks saves your GitHub sign-in
under your account, like a Gmail connection. Other apps can use that grant.
Your Chat sandbox acts on GitHub with it. **Disconnect** revokes it.

When you tag the App before you connect, the App answers once in the thread:
connect GitHub in Druks, then tag it again. It holds nothing.

### Who the App answers

Only a comment that tags the App reaches an agent, and only when its author can
write to the repository. A comment with no tag does nothing, also when it
answers the agent. A tag on a quoted line does not count. Each person who tags
the App gets their own conversation for the issue or pull request. Two people
can each talk to the Bot in one thread. Druks saves a comment once, whatever
GitHub delivers.

The agent decides what to do: answer, open a ticket, start a build, or start a
review. To review, it calls `software_factory_review` and passes your comment as
the note. A tag starts no review by itself. When a run the agent started fails, the
agent says so in one short line, without error details, and does not retry. Tag
it again to try again.

### Where the reply goes

The reply is a comment where the tag was. When you tagged the App in the inline
thread of a pull request, the reply goes in that thread. Otherwise, it goes at
the top of the issue or pull request. Every write goes through the operator App.
Every reply posts as the App. On a public repository, anyone can read it.

The agent reads the thread with `chat_read_thread`. It returns the issue or pull
request itself, then its newest 200 comments, oldest first. Each has its author's
id and login. It also says whether this agent wrote it, and whether the person
that the agent answers wrote it. An inline comment also names its file and line.
Every agent posts as the one App. Druks tells an agent's own replies by the
GitHub ids it recorded. The prompt treats only the comment of the person the
agent answers as instructions. Everything else in the thread is context.

## Calls

People can phone an app's Bot. Each call is its own conversation. Only an app
with a public [Bot](writing-an-app.md#answer-whatsapp-with-a-bot) takes calls.
Connect the Twilio and Voice cards first: see [Calls](configuration.md#calls).

### Link a number

1. Open the app's settings and select **Channels**. The phone numbers show once
   the Voice card is connected.
2. Pick one of your Twilio numbers and select **Add number**.

Druks creates a bot account for the number and sets the number's voice URL in
Twilio to Druks. The bot account never signs in. The picker lists only numbers
that are not linked yet. It also skips a number that a TwiML App or a SIP trunk
handles, because Twilio ignores the voice URL of such a number.

A phone number has no admin. No agent turn runs on a call. When a call
starts a run that asks a question, an account that can use the app can answer
it. The Dashboard does not show these runs, because they belong to the number's
bot account.

**Remove** clears the number's voice URL in Twilio. Druks stops answering
its calls. The number and its calls stay as history. If you link the number
again, it gets a new connection. If you disconnect the Twilio card, Druks
removes every linked number first.

### What happens on a call

1. Twilio sends the call to Druks. Druks checks Twilio's signature and starts a
   new conversation for the caller.
2. Druks tells Twilio to stream the call's audio to the calls server.
3. The voice model answers. It first says that it is an automated assistant
   and that the call is transcribed.

Druks rejects a call to a number that is not linked, and a call while the Voice
card is disconnected.

The voice model works from the Bot's prompt. It can call the tools in the Bot's
`user_tools`, as often as the call needs. It offers only what those tools
can do. It also knows:

- The caller's number and the number they called.
- The local time.
- The last 20 lines of the caller's earlier calls to that number.
- What Druks reported after those calls ended.

A caller who hides their number is a new person on each call. The voice model
gets no number and no history for them.

Druks saves each line of the call in the conversation as it is said. If a tool
starts a run, the run reports back to the conversation. Nothing reads the
report during the call. The caller hears the outcome on their next call.

A call ends when the caller or the voice model ends it, after 15 seconds of
silence, or after 5 minutes.

### Read a call

An operator who can use the number's app can read its calls:

1. Open the app's settings and select **Channels**.
2. Find the number. Its calls are below it, newest first. Each one shows the
   caller's number, the start time, and the length.
3. Select a call to read its transcript.

A call belongs to the number's bot account, and nobody signs in as that
account. As a result, every operator who can use the app can read calls. Other
conversations are for their creator only. A removed number keeps its calls. An
access token cannot read calls, and the Chat page does not show them.
