---
title: "Writing an app"
description: "Build a separately packaged Druks app with workflows, instructions, gates, routes, models, and migrations."
sidebarTitle: "Author guide"
icon: "puzzle"
---

An app is a Python distribution installed in Druks. It owns domain behavior.
Druks supplies durable execution and the shared services that apps run on. Read
[the app boundary](concepts.md#the-app-boundary)
before you assign ownership of a capability.

## Publish useful activity

An instruction output declares its saved result and its Activity topic:

```python
from druks.workflows import InstructionOutput


class GistOutput(InstructionOutput):
    gist: str

    def to_artifact(self) -> dict[str, str]:
        return {"kind": "markdown", "title": "Gist", "content": self.gist}

    def to_event(self) -> dict[str, str]:
        return {"topic": "gist.prepared", "summary": self.gist}
```

A workflow asks the operator to review the result. It announces the outcome that
it owns:

```python
from druks.workflows import Workflow, step

from druks_field_notes.app import FieldNotes
from druks_field_notes.models import Note


class Summarize(Workflow):
    subject = Note

    async def run_multistep(self) -> None:
        note = await self.subject
        operator_note = ""
        while True:
            result = await FieldNotes.summarize(note_body=note.body, operator_note=operator_note)
            reply = await self.review()
            if reply.action == "approve":
                break
            operator_note = reply.note
        await self.save_gist(result.gist)
        await self.announce("note.gist_approved")

    @step
    async def save_gist(self, gist: str) -> None:
        note = await self.subject
        await note.save_gist(gist)
```

The independently installed Field Notes proof app uses these classes. The
operator answers the review from the dashboard. Druks records the accepted work,
each saved result, each request, and each validated reply. It also records a
terminal failure. Authors supply no routing IDs, timestamps, sessions, or event
rows. See [Activity facts](#activity-facts-and-signals) for the ownership rules.

## Scaffold and prove the package

```bash
uvx --from druks druks create app night-watch
cd druks-night-watch
uv sync
uv run pytest
```

From a Druks checkout, `uv run druks create app night-watch` scaffolds with
that checkout's CLI instead.

The command writes a standalone `druks-night-watch` project in the current
directory. The folder and the distribution use hyphens. The app name and its
package use underscores. Its `pyproject.toml` contains:

```toml
[project.entry-points."druks.apps"]
night_watch = "druks_night_watch.app:NightWatch"
```

The app name must match `[a-z][a-z0-9_]*`. The command also accepts the
hyphenated spelling and converts it. The name becomes the API namespace, the table
prefix, the suffix of the migration version table, and the settings namespace. The install of
the distribution is the registration:

```bash
uv pip install -e /path/to/druks-night-watch
```

At boot Druks imports installed entry points. It fails loudly on duplicate
names, a mismatched entry-point key, a malformed target, a failed import, or a
table with no prefix.

The project root also has an `AGENTS.md`. It holds the contracts that a
coding agent cannot infer from the stubs, and a link back to this guide.

The scaffold depends on the published `druks`. To develop an app against a
local checkout instead, pin it:

```toml
[tool.uv.sources]
druks = { path = "../druks", editable = true }
```

## Package layout

The scaffold separates the capability modules, which register themselves, from
ordinary package modules:

| Path | Contract |
| --- | --- |
| `app.py` | `App` subclass, instructions, app settings |
| `workflows.py` | durable `Workflow` and `Gate` subclasses |
| `tasks.py` | Optional `@task` background functions |
| `models.py` | SQLAlchemy models with `<name>_` table names, `StoredSubject` among them |
| `contracts.py` | `InstructionOutput` contracts |
| `schemas.py` | HTTP responses and subject summaries |
| `routes.py` | FastAPI routers |
| `pages.py` | `@page` declarations that return `Page` objects |
| `subscribers.py` | signal reactions |
| `hooks.py` | Optional hooks on instructions, this app's or another app's |
| `webhooks.py` | Optional authenticated provider deliveries |
| `services.py` | Optional `Service` declarations for appliance credentials at external providers |
| `migrations/versions/` | this distribution's Alembic history |
| `dist/` | optional built frontend module, mounted inside the shell (served under `/app/<name>`) |

Druks recursively discovers leaf modules named `workflows`, `tasks`, `routes`,
`pages`, `subscribers`, `hooks`, `webhooks`, `services`, and `channel`. Druks
does not discover a capability hidden in `workflow.py`. Ordinary names such as
`policy.py` and `workspace.py` have no import side effect unless a discovered
module imports them.

## Declare the app

```python
from druks.apps import App


class NightWatch(App):
    name = "night_watch"
    icon = "telescope"
    description = "Checks repositories after hours."
```

The class is a stateless installation singleton. Do not instantiate it. Druks
mounts every router found in its `routes` modules under `/api/night_watch`. It
supplies transcript routes. It serves `druks_night_watch/dist/` under
`/app/night_watch` when that folder contains `entry.js`.

An app writes files outside the database only under `App.get_data_dir()`,
`<DRUKS_DATA_DIR>/apps/night_watch`, which Druks creates on first use. A
downloaded tool binary or a tool's state directory goes there, never under
`~`. The process user has no writable home. Only the data directory survives a
release.

## Choose the right workflow shape

The parameters of `run()` or `run_multistep()` are the workflow input. Druks
builds a Pydantic model from their annotations and validates the call to
`start()`.

If the whole body is one durable operation, use `run()`:

```python
from druks.workflows import Workflow


class RecordHeartbeat(Workflow):
    async def run(self, source: str) -> None:
        Heartbeat.record(source)
```

If completed operations require independent recovery, use `run_multistep()`.
Also use it for a workflow that waits on a gate:

```python
from druks.workflows import Workflow, step


class Sweep(Workflow):
    async def run_multistep(self, repo: str) -> None:
        findings = await self.scan(repo)
        await NightWatch.write_report(repo=repo, findings=findings)

    @step
    async def scan(self, repo: str) -> list[str]:
        return await scanner.scan(repo)
```

Druks treats `run()` as one step. `run()` must not carry `@step`. DBOS replays the
orchestration of `run_multistep()`. `run_multistep()` must not carry `@step`
either. Decorate its operations that have side effects instead.

An instruction that
the orchestration body calls directly gets its own step. An instruction that a
`@step` or `run()` calls shares the checkpoint of that caller.

### Declare the sandbox environment

A workflow can ship the environment that its agents need as a plain shell file:

```python
from druks.sandbox import Sandbox
from druks.workflows import Workflow


class BuildSite(Workflow):
    sandbox = Sandbox(setup="sandboxes/build.sh")
```

Place the file at `site_builder/sandboxes/build.sh`, beside
`site_builder/workflows.py`. The path is relative to the folder of the module
that declares the workflow.

Druks reads the raw bytes. It does not render the file or run it during
import. Drukbox builds a reusable template from the platform base and the
script. A run waits in a visible phase while Drukbox builds that
template. A workflow with no declaration uses the platform base unchanged.

The content hash of the base and the script identifies the template. App authors do
not name provider images.

Use provider idempotency keys for writes. An interrupted operation can run again.
DBOS reuses completed checkpoints on recovery. Keep decisions in replayable
control flow. Keep I/O inside steps. See
[durability and recovery](concepts.md#durability-and-recovery).

Start a workflow with an explicit subject, an instance of the class that it
declares:

```python
run_id = await Sweep.start(
    subject=repository,
    repo=full_name,
)
```

A workflow without a subject declaration passes `subject=None`. A subject has
at most one active run for each workflow kind. A duplicate start returns the
active run ID. Attribution does not change this rule. Two accounts that start
the same subject share one run.

Outside a workflow, `start()` puts a new run in the queue when the caller's
transaction commits. If the transaction rolls back, the run does not start.
Before the commit, a duplicate start in the same transaction returns the same
run ID.

When a new start must replace the active run, call `replace()` with the same
arguments. It cancels the subject's active run with the failure "A newer run
replaced this run." Then it starts a new run. `replace()` runs only in a
workflow body.

If the app requires prelaunch policy, wrap `start()` in a domain `dispatch()`
method. This method can own lookup, snapshot, or routing policy. It binds its
own database session. A workflow body can then call it.

Druks attributes a browser start with no code from the app. The request identity gate records the
resolved account. `start()` inherits it. A route does not require more
attribution code.

If the dispatcher has a better account, pass `account_id`.
For example, a webhook can resolve the ticket assignee. During a webhook
delivery that names a person, the run starts as that person's account. This
is true even when the dispatcher passes `account_id`. See
[Receive webhooks](#receive-webhooks).

The run carries its account. A body reads `self.account`, the `Account` row,
and `self.account_id`. A route takes the caller's row with
`account: Account = Depends(current_account)` from `druks.accounts`.

Every run needs a `Run.account_id`. A browser start records the authenticated
account. An unattended start records the default account. Druks refuses to
start a run before an account is available. A parked run keeps its account
after resume.

A run starts only as an account that can use the app. Druks refuses the start
for an account that a [team](concepts.md) excludes. A route answers 403. A
webhook acknowledges the delivery. The delivery changes nothing. An app makes no
check and does not catch the refusal.

Each agent call uses the installation execution defaults. Instruction overrides
replace them. Subscription billing uses the run account's subscription. The
call records exactly one billing reference: `subscription_id` or
`api_key_provider`. Druks uses that selected credential for execution.

Druks refuses a call that has no credentials. A workflow can use
different providers across its agent calls. A disconnect clears the credential
secret and retains its billing identity for call history. See
[personal and installation settings](configuration.md#personal-and-installation-settings)
for execution defaults, personal preferences, and timezone rules.

### The journal

Druks keeps a journal of the typed values for each run. Each body-level agent
call and gate reply enters it in call order. Add your values with
`self.journal.add()`. Read them by contract type:

```python
self.journal.filter(PlanData)
self.journal.latest(PlanData)
self.journal.filter(ImplementationOutput, status="success")
self.journal.filter(ReviewWork)
```

- `filter()` returns every entry of the type, oldest first.
- `latest()` returns the newest entry, or `None`.
- A keyword filter tests equality. An entry must match every keyword filter.
- `filter()` with a `Gate` class returns the replies of that gate.

Subclass `Journal` to name your projections. Declare the subclass on the
workflow:

```python
class SweepJournal(Journal):
    @property
    def findings(self) -> list[FindingData]:
        return self.filter(FindingData)


class Sweep(Workflow):
    journal_class = SweepJournal
```

The journal survives crashes without separate storage. Recovery runs the body
again with every durable call memoized. The journal then gets the same entries
in the same order.

Two rules apply:

- Druks journals only body-level calls. An agent call inside a `@step` never
  enters the journal. Neither does a call in a `run()` body, which is one big
  step. Keep that state in local variables.
- Never mutate body-held state inside a `@step`. DBOS skips a completed step
  on replay. The write then disappears.

### Announce domain events

Announce a domain fact from the workflow body:

```python
await self.announce("sweep.finished", findings=len(self.journal.findings))
```

Druks records the event in one checkpoint. It notifies subscribers in a second
checkpoint. A subscriber retry cannot add the completed event again.

Recovery
reuses completed checkpoints. An interrupted operation can run again.
Subscribers must stay idempotent. Call this method outside a `@step`.

A domain method announces through its subject:

```python
from druks.db import StoredSubject
from sqlalchemy.orm import Mapped


class Report(StoredSubject):
    published_url: Mapped[str | None]

    async def publish(self, url: str) -> None:
        if self.published_url != url:
            self.published_url = url
            await self.announce("report.published", url=url)
```

`Subject` and `StoredSubject` both supply `announce()`. Druks gets the owner from
the registered app package. The call records the subject identity, its current
key, its optional summary title, and the supplied facts. It then notifies
subscribers.

The event and every subscriber's writes commit together. In a step, put the
domain change and its announcement in one `atomic()` block. If a subscriber
fails, Druks rolls back the change with its event.

The app must prevent duplicate domain changes on webhook redelivery.

Authors supply no app ID, run ID, timestamp, or session. Frontend code owns the
wording.

### Read what the run has cost

Read the run's cost so far from the workflow body, for example for a summary:

```python
cost = await self.get_cost()
```

It is the sum, in US dollars, of the costs that the run's agent calls recorded. A
call that recorded no cost counts as zero. The read is a checkpoint. A replay
returns the cost that the run read the first time. Inside a `@step` or `run()`,
the read shares that checkpoint.

### Read how long the run's steps took

Read how much time the run's own steps and agent calls used so far. Use it,
for example, to report the time that the work took:

```python
duration = await self.get_duration()
```

It is a `timedelta`: the sum of the time that each step took, as DBOS timed it.
A wait at a gate counts nothing, whether the gate got an answer or timed out. A
`sleep` and a restart count nothing too. A retried run counts the steps that it
reused from the run that it retries. A step that still runs counts nothing.

Read the duration from `run_multistep()`, not from inside a `@step` or `run()`.
Like `get_cost()`, the read is a checkpoint.

### Schedules and settings

Set `every` to declare a cron:

```python
class Sweep(Workflow):
    every = "0 6 * * *"
```

The tick fires the workflow's body with no subject and no input. Every body
parameter then needs a default. A workflow whose runs are *about* something (it
declares a `subject`) must not start that way. Give it a `dispatch()`
classmethod. The schedule then fires `dispatch()` instead. `dispatch()` resolves
the subject and starts the real run:

```python
class Engage(Workflow):
    subject = Account
    every = "0 */4 * * *"

    @classmethod
    async def dispatch(cls) -> str:
        return await cls.start(subject=Account.get())

    async def run(self) -> None:
        ...
```

A scheduled `dispatch()` fires with no arguments. Each of its parameters needs
a default. Druks evaluates cron expressions in the installation timezone. An
operator can change the cadence or pause a declared schedule on the Schedules
page or in the app settings. Druks cannot add a schedule to a workflow that
declares none.

### Background tasks

Use a `Workflow` for work that you want on the timeline of a subject. Such work
has agent calls, gates, and operator-tunable settings. Some plumbing needs none
of that, such as periodic maintenance or a side effect that nothing waits for.
Use a `task` for it:

```python
from druks.workflows import task


@task(every="*/15 * * * *")
async def refresh_tokens() -> None:
    ...


@task(retries=4)
async def sync_labels(pull_request_id: int) -> None:
    ...
```

Call `await sync_labels.enqueue(pull_request_id=7)` from a route, a webhook's
subscriber, or a workflow body. Never call it inside a `@step`, or in a subscriber
of a `WorkflowEvent` or of an announced event. Druks notifies those subscribers
inside a step of the run.

As with a workflow, the task's signature is the wire contract. Each parameter
carries a type annotation. `enqueue()` validates the parameters and stores JSON. A
task keeps no run row and never shows on the timeline.

Outside a workflow, `enqueue()` puts the task in the queue when the caller's
transaction commits. If the transaction rolls back, the task does not run.

A task has no subject, gate, or operator settings. It cannot make agent calls.
`every=` on a task is a fixed UTC cadence that the code owns. An operator can
change only a workflow schedule, on the Schedules page or in the app settings.
`retries=` sets retries after the first attempt, both here and on `@step`.

A workflow can declare its own operator settings:

```python
from pydantic import BaseModel, Field


class Sweep(Workflow):
    class Settings(BaseModel):
        batch_size: int = Field(default=20, ge=1, le=100)

    @step
    async def load_settings(self) -> "Sweep.Settings":
        return await self.settings()
```

A read of the settings inside a step checkpoints them for replay. A direct read
from replayed orchestration lets later edits change a run that is in progress.

## Add an instruction

An instruction belongs to the app class. The operator chooses the CLI that runs
it, the model, the login that it bills, and the effort. The defaults are in
**Settings → Instructions**. The app's own page overrides them for one
instruction. `timeout` is the one setting that the app declares, because how long
a step can run is a fact about the task.

```python
from druks.workflows import Instruction, InstructionOutput


class ReportOutput(InstructionOutput):
    title: str
    body: str


class NightWatch(App):
    name = "night_watch"

    write_report = Instruction(
        prompt="night_watch/write_report.md",
        contract=ReportOutput,
        description="Turns findings into an operator report.",
    )
```

The app name and the attribute name form the instruction's id:
`night_watch.write_report`. Settings overrides, the timeline, and the step name
use that id.

An instruction that reads untrusted content, such as email or a web page, gets no
plugin state and no MCP server:

```python
    triage = Instruction(
        prompt="triage.md",
        contract=TriageOutput,
        include_plugins=False,
        include_mcp=False,
    )
```

A workflow that sets `steps_reuse_sandbox = True` keeps one sandbox for all its
instructions. The sandbox takes its entries from the agent call that creates it.
Give such a workflow instructions that all include MCP servers, or none that do.

Call an instruction only inside a workflow:

```python
result = await NightWatch.write_report(repo=repo, findings=findings)
```

Druks renders the prompt with the current workflow, workspace, and supplied
context. It resolves the instruction's harness, model, and login in one place.
It refuses a call that has no login, before any sandbox work. Then it
provisions or attaches a sandbox, runs the CLI, validates the structured
output, and records the call.

Override `InstructionOutput.to_result()` to map
the strict output contract to a domain value. Override `to_artifact()` to
publish a reviewable artifact. Override `to_event()` to record that saved
result in Activity. Return a `topic` and an optional `summary` from it. An
output that returns an event must also return an artifact. Otherwise the agent
call fails with `WorkflowError`.

Druks records one Activity row with the artifact and the agent call that made it.
The row and the artifact share a transaction. Recovery reuses a completed call
and does not create a second row. Two new calls can produce two rows with the
same topic. The saved result has its own identity in each row.

When the required output fields of an agent call depend on the input, pass
`contract=OutputType` on the call. Druks uses that type for the harness schema,
validation, artifact, and result conversion. The instruction's declared contract
stays unchanged. `contract` is not prompt context. Build dynamic types with
Pydantic `create_model`. Use `to_result()` to return a stable type for durable
storage.

If an agent produces a file, use [`File` and `FileField`](files.md). The
contract declares the file. Druks transports and serves it. The app can keep its
stable reference on an app row.

An app that runs a CLI of its own inside the sandbox reads how a declared
instruction runs. It gives that configuration to the CLI. Declare the
instruction. Never call it. The operator configures it in the app's
**Settings → Instructions**. The shared defaults are in
**Settings → Instructions**. The app reads the configuration with
`get_config()`:

```python
config = await NightWatch.auditor.get_config()
config.harness
config.model_id
config.model
config.effort
config.billing
config.secrets
```

- `harness` is `"claude"`, `"codex"`, `"opencode"`, or `"pi"`.
- `model_id` is the model as that CLI names it, without the provider prefix.
- `model` is `"provider/model"`.
- `billing` is `"subscription"` or `"api_key"`.
- `secrets` holds the Drukbox entries that put the key in the VM as a
  placeholder.

`get_config()` returns an `AgentConfig` inside a workflow. This temporary value
contains shared execution settings and the run account's selected credential.
Druks resolves it at call time, as it does for an agent call. It stores no
personal preferences and has no database table. `get_config()` raises before
any sandbox work when there is no login or no key.

Under subscription billing there is no key. The VM home holds only the login of
the subscription of the agent in it. A nested CLI on another provider then needs
`api_key` billing. Under `api_key` billing, the VM holds the key as a
placeholder in the variable that the entry names: `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, or `CODEX_API_KEY` for Codex. A nested CLI reads it from the
environment. The [configuration guide](configuration.md#harnesses) lists the
variable, host, and header for each harness.

### Give an instruction a service secret

An instruction that calls a provider from its sandbox lists the secret that it
needs. The [service](#declare-a-service) declares the one host that its secrets
can go to:

```python
class Acme(Service):
    host = "api.acme.example"

    class Authentication(auth.Authentication):
        api_key = auth.ApiKey()

    class Settings(BaseModel):
        webhook_secret: SecretStr = Field(title="Webhook secret")


class NightWatch(App):
    name = "night_watch"

    write_report = Instruction(
        prompt="night_watch/write_report.md",
        contract=ReportOutput,
        secrets=(Acme.fields.api_key,),
    )
```

`Acme.fields` holds every field of the service's card by name. The card holds
the fields of `Settings` and the credential fields of each way, such as
`api_key`. The sandbox of each `write_report` call holds a placeholder in
`ACME_API_KEY`: the service slug and the field name, in upper case. The secrets
proxy puts the key in an `Authorization: Bearer` header only on requests to
`host`. The sandbox never holds the key. Nor does it hold `webhook_secret`,
which no instruction lists.

Druks refuses to load an instruction that lists a field that is not a secret.
It also refuses a field of a service with no `host`, or of a service that
declares `jwt`. If a service with `required = False` is not connected, the
sandbox gets no variable for it. The prompt must handle that case.

A workflow that sets `steps_reuse_sandbox = True` binds the secrets of the call
that creates the sandbox. Give the instructions of such a workflow the same
`secrets`.

For a credential that depends on the subject, such as one account's connection,
override [`get_secrets(subject)`](#customize-the-workspace) on the workspace.

A person's own sign-in needs no list. If a service has OAuth endpoints and a
`host`, each operator's Chat sandbox holds that operator's sign-in under the
service slug. It holds the sign-in the way it holds a listed field. An operator
with no sign-in gets no variable.

Do not ask the framework to infer domain side effects from agent prose.
The prompt or a subsequent explicit step owns those actions.

### Give an agent one more turn

An app can hook an instruction in `hooks.py`. Decorate a function with the
instruction's `after_agent`:

```python
# hooks.py
from druks.workspaces import RepoWorkspace

from .app import NightWatch


@NightWatch.write_report.after_agent
async def review(*, prompt: str, workspace: RepoWorkspace) -> str | None:
    finding = await NightWatch.critic(diff=await workspace.get_diff(), task=prompt)
    return finding.instruction
```

To hook another app's instruction, name it with `Instruction.get`, so that
neither app imports the other:

```python
from druks.workflows import Instruction

implementer = Instruction.get("software_factory.implement")


@implementer.after_agent
async def review(*, prompt: str, workspace: RepoWorkspace) -> str | None:
    ...
```

Druks calls the hook when the agent ends its turn, before Druks takes the
agent's output. The hook gets the rendered prompt and the workspace that the
agent runs in. If the hook returns text, the agent gets the text as one more
turn in the same session. The agent's next result is then the result of the
call. If the hook returns `None` or an empty string, the turn ends.

A call gets one round. Druks asks once. The next end of turn is final. The hooks
of one instruction run in registration order. Druks joins their texts.

`Instruction.get` first loads the class of the app that owns the id. `hooks.py` can then
name an instruction of an app that Druks did not load yet. An unknown id
raises `ObjectNotFound`.

A hook runs inside the step of the hooked call. An instruction that a hook
calls records its own call and artifact. It gets its own sandbox, because the
hooked agent still holds its sandbox.

Recovery reuses a completed hooked call.
An interrupted one runs again with its hooks. A hook that raises makes the
hooked call fail. The run time of a hook counts against the timeout of the
hooked call.

`RepoWorkspace.get_diff()` returns the change of the checkout since it left the
default branch: commits, uncommitted edits, and untracked files. It reads the
checkout as it is, without a clone. A changed binary file reads "Binary files
differ". `get_diff(binary=True)` includes its content, so that `git apply` can
apply the diff to another checkout.

Each harness gives the agent the text in its own way:

| Harness | The agent ends its turn when | The agent gets the text as |
| --- | --- | --- |
| Claude Code | it records its result | the reason that Druks refuses that result |
| Codex | it stops | a message from a Stop hook |
| pi | it calls `submit_result` | the answer of `submit_result` |
| OpenCode | its message completes | the next message in the session |

## Answer WhatsApp with a Bot

A Bot answers the connections that an operator links to your app, such as a
WhatsApp number. It is not an instruction. It has no contract. No workflow
awaits it.

Each person who writes gets their own [chat conversation](chat.md#whatsapp). The
Bot acts only through the app routes that you tag `bot`.

An app's number is public. It answers anyone who writes.
One person is the number's admin. Only Chat's number is private. See
[WhatsApp](chat.md#whatsapp).

Declare the Bot as the `bot` attribute of the app class. An app has one Bot:

```python
from druks.chat import Bot
from druks.apps import App


class Helpdesk(App):
    name = "helpdesk"

    bot = Bot(
        prompt="helpdesk/bot.md",
        user_tools=("get_ticket", "request_access"),
        admin_tools=("list_requests",),
    )
```

- `prompt` names a template, like an instruction's prompt. Druks renders it with
  `source` and `thread_id`. Druks gives the result to the agent as its system
  prompt.
- `user_tools` are the tools of each person who writes to the connection.
- `admin_tools` are the tools of the number's admin. The admin also gets
  Druks's admin prompt, `get_gate`, `answer_gate`, `chat_resume_conversation`,
  and the [reminder](chat.md#reminders) tools.

`source` is the conversation's source, `web` or `whatsapp`. `thread_id` names the
WhatsApp group of a group conversation. It is empty in a direct chat.

At the end of the system prompt, Druks adds one paragraph. That paragraph explains
the `[Internal: …]` messages that Druks writes to the agent. In a group, Druks
first adds a paragraph that tells the agent to read the group with
`chat_read_thread`. Druks also gives the agent that tool and `chat_read_file`.

Each name is the `operation_id` of one of the app's routes tagged `bot`. Druks
refuses to start when an app declares a Bot under another attribute name, or
when a name matches no such route. The Bot's agent has no shell, file, or web
tools.

Tag a route with `bot` to make it a tool for the Bot. The rules for `agent`
routes apply: an explicit `operation_id`, a docstring, and the app name as the
tool prefix. A `bot` route is never in the Druks toolkit of an operator's
agent. Tag a route with both `agent` and `bot` to put it in both.

A bot tool takes the person who writes as `user: BotUser`:

```python
from typing import Annotated

from fastapi import APIRouter, Body

from druks.chat import BotUser

from .workflows import GrantAccess

router = APIRouter(prefix="/access")


@router.post("", tags=["bot"], operation_id="request_access")
async def request_access(system: Annotated[str, Body(embed=True)], user: BotUser) -> str:
    """Ask an admin to approve access to a system for the person writing."""
    return await GrantAccess.dispatch(system=system, user_id=user.id)
```

| Field | Value |
| --- | --- |
| `id` | The person's id on the channel, such as a WhatsApp id or a caller's number. It is always set. |
| `name` | The person's name, or empty when the channel does not have it. |
| `phone` | The person's phone number, or empty when the channel does not have it. |
| `source` | The channel of the conversation, such as `whatsapp` or `call`. |

Druks fills `user` from the conversation that the tool call came from. `user`
then never appears in the tool's input schema. A route that takes `BotUser`
refuses a call from outside a conversation.

A run that a bot tool starts remembers its conversation. Ask for approval with
an in-app question, `self.review()`. Druks then asks the number's admin in the
admin's own chat. Only that admin can answer.

When the run ends, Druks tells the
conversation its result or its failure, whether or not the run waited. The Bot
then tells the person. A run that returns nothing, such as a review that posts
itself, tells the conversation that it ended. A cancelled run tells nothing.

To withdraw a request that waits, cancel it in a bot tool:
`await GrantAccess.cancel(request)`. A subject has one active run for each
workflow. Cancel a request before you start a changed one.

The Bot has a row in the app's **Settings → Bots** under the id `<app>.bot`.
Its harness, model, billing, effort, and timeout resolve like an instruction's.
The timeout is the longest that one turn can run. `timeout=` on the Bot declares
its default. The harness must have a chat adapter, as for Chat: Claude Code,
Codex, or OpenCode.

## Customize the workspace

Every agent call uses a `Workspace` around a Drukbox sandbox.
`Workflow.workspace_class` names the kind. A workflow about a GitHub repository
declares `RepoWorkspace` and nothing else:

```python
from druks.workspaces import RepoWorkspace


class Sweep(Workflow):
    subject = Repository
    workspace_class = RepoWorkspace
```

The subject names what to clone. `repo` is the repository as `owner/name`.
`ref` is the commit or branch to clone, or `None` for the default branch. A
column, a property, or a class attribute can supply each value:

```python
class Repository(StoredSubject):
    repo: Mapped[str]
    ref = None


class Check(StoredSubject):
    repo: Mapped[str]
    head_sha: Mapped[str]

    @property
    def ref(self) -> str:
        return self.head_sha
```

Before every agent call, Druks clones that ref into `workspace.repo_path`.
Prompts read `{{ workspace.repo_path }}`. The sandbox holds a placeholder for
its GitHub token in `GH_TOKEN`.

Drukbox configures git and `gh` to use it. The Drukbox secrets proxy replaces the
placeholder with a token that Druks mints on demand. A long run then never
lasts longer than its token. The clone is idempotent. A warm host keeps its
working tree. A bare host that replaces it gets the working tree again.

Every workspace holds the run's `subject`. The sandbox's GitHub token covers only
its repository. To let the agent clone other repositories, override
`get_secrets(subject)`. Name the other repositories after the subject's
repository in the secret's `resources`. The token also gives access to each one
that the same App installation covers.

The sandbox's GitHub token comes from a connected service of the appliance.
`RepoWorkspace` names the operator App, `Github`. An app that acts as another
identity declares its own service in its `services` module. That service is a
subclass with its own connect card. The app names it on its workspace:

```python
from pydantic import BaseModel, Field, SecretStr

from druks.core.services import Github
from druks.workspaces import RepoWorkspace


class GithubReviewer(Github):
    required = False

    class Settings(BaseModel):
        app_id: str = Field(title="App ID")
        private_key: SecretStr = Field(title="Private key (PEM)")


class ReviewWorkspace(RepoWorkspace):
    github = GithubReviewer
```

The operator connects the service in **Settings → Connections → Services**. The
sandbox's identity stores the service and the repos. The issuer reads only
those. A request then cannot select another repo or identity.

A workspace defines the box from the subject alone, before the box
exists. It does so through `get_secrets(subject)`, `get_mcp_servers(subject)`,
and `get_skills(subject)`. `get_skills(subject)` names the skills that the box
gets beside the enabled catalog.

`prepare()` readies the box when the box runs:
read rows, call a service, upload a file, write a wrapper, run a command on
`self.host`. Each read and write there is a unit of work of its own. The box
then holds no database connection through the run. Override
`get_agent_run_kwargs()` to grant directories. Override `get_env()` to set the
environment variables that every agent call gets:

```python
class DeployWorkspace(RepoWorkspace):
    async def prepare(self) -> None:
        await super().prepare()
        deployment = await Deployment.get(id=self.subject.deployment_id)
        await self.host.write_secret(remote="deploy-token", secret=deployment.token)
        await self.host.upload_file(local=DEPLOY_SCRIPT, remote="deploy.sh")
```

Override `get_secrets(subject)` to give the sandbox a secret of its own:

```python
from druks.sandbox import SandboxSecret
from druks.workspaces import Workspace

from .services import BillingApi


class InvoiceWorkspace(Workspace):
    @classmethod
    async def get_secrets(cls, subject) -> list[SandboxSecret]:
        return [
            SandboxSecret(
                name="billing_token",
                secret_id=(await BillingApi.get()).id,
                host="api.billing.example",
            )
        ]
```

The secret names the vault row that the issuer answers from. With a `host`, it
is a custom entry. The sandbox holds a placeholder in `BILLING_TOKEN`, the name
in upper case. The secrets proxy puts the value in a request header only for
that host. A header row supplies its own header. The proxy sends any other row as
`Authorization: Bearer <token>`.

An entry for `github.com` is Drukbox's GitHub
service under any name: `GH_TOKEN`, with git and `gh` configured. Without a `host`,
the name is a Drukbox catalog entry. Drukbox sets its variable and hosts.
`resources` tells the issuer what the one token covers, such as repos. Druks
reads the secrets before the sandbox exists. Read them from the subject alone.

A vault row with one secret issues it under any name. For the secret of a
service that does not depend on the subject,
[list it on the instruction](#give-an-instruction-a-service-secret) instead.

Override `get_mcp_servers(subject)` to give the sandbox an MCP server with its
own vault row:

```python
from druks.sandbox import SandboxMcpServer


class BuildWorkspace(RepoWorkspace):
    @classmethod
    async def get_mcp_servers(cls, subject) -> tuple[SandboxMcpServer, ...]:
        actor = await get_review_actor()
        return (
            SandboxMcpServer(
                name="github",
                url="https://api.githubcopilot.com/mcp/",
                secret_id=(await actor.service.get()).id,
                resources=(subject.repo,),
            ),
        )
```

The server names the vault row that the issuer answers from and what the token
covers: here a connected GitHub service and its repo. Druks binds the server's
host and the variable `MCP_GITHUB_TOKEN` to the entry when it creates the
sandbox. The harness configuration names the variable. The sandbox never holds
the token.

A workspace server owns its name. Druks does not deliver a registry
server with the same name. `Workspace.get_all_mcp_servers(subject, account_id)`
returns the harness shapes and the secret refs for every MCP server of a
sandbox: the workspace's servers and the enabled registry servers. An instruction
that declares `include_mcp=False` gets none of them.

A workflow runs one command in a VM of its workspace with `exec`. The VM comes
from the workflow's sandbox template and holds the workspace's secrets. The
command runs in the workspace's working directory, the clone for a
`RepoWorkspace`. Druks releases the VM after the command. Call `exec` from
`run()` or inside a `@step`:

```python
class Lint(Workflow):
    subject = Repository
    sandbox = Sandbox(setup="sandboxes/lint.sh")
    workspace_class = RepoWorkspace

    async def run(self) -> None:
        result = await self.exec(["ruff", "check", "."], timeout=300)
        if result.returncode:
            raise FatalError(result.stdout)
```

The result is a `subprocess.CompletedProcess`: `returncode`, `stdout`, and `stderr`.

Keep durable state outside the VM. A workflow can set
`steps_reuse_sandbox = True` to retain one host across a segment. Druks releases
the host at a gate, during a `sleep`, and when the workflow ends. It rotates the
host near the end of its lease, and when the next agent call needs other secret
entries.

### Borrow a browser session

Declare required logins on the app class. The attribute name and the app name form
the session identity. The sessions pane asks the operator to sign in:

```python
from druks.apps import App
from druks.browser import BrowserSession


class FieldNotes(App):
    name = "field_notes"
    atlas = BrowserSession(site="atlas.example", persist=True)
```

A workflow borrows the signed-in browser as a Playwright handle. The app
declares Playwright as its dependency. Druks owns the browser container and its
lifecycle. The browser starts in a container on the Druks host. It stops with
the block. Druks exports and stores a ``persist`` session before the stop:

```python
async with FieldNotes.atlas.playwright() as browser:
    page = await browser.new_page()
    await page.goto("https://atlas.example/species")
```

``playwright()`` yields the signed-in browser context. Pages that you open in
this context use the session. ``FieldNotes.atlas.cdp()`` borrows the same browser
and yields the raw CDP URL. Use this URL with a test suite, a raw CDP client, or
a custom wrapper.

``pinchtab()`` borrows the same browser through a PinchTab bridge. The app needs
no other dependency:

```python
async with FieldNotes.atlas.pinchtab() as browser:
    await browser.navigate("https://atlas.example/species")
    title = await browser.evaluate("document.title")
```

The client has five methods: ``navigate``, ``evaluate``, ``snapshot``, ``text``,
and ``action``. They work in one tab, which the first ``navigate`` opens.
``evaluate`` returns the JSON value of the expression and waits for a promise.
``action`` takes a PinchTab action, for example ``action("click", ref="e5")``.
``snapshot`` supplies the refs.

If the browser refuses a command, the method
raises ``BrowserCommandError``. Druks starts the bridge beside Chrome and stops
it with the block. The bridge opens only the session's site and its subdomains.
It disables its cookie commands.

``read_only=True`` makes every borrow refuse each request that is not a GET, in
the browser. The rule covers every tab, popup, worker, and service worker. The
rule does not cover a shared worker. A read-only browser then has no shared
worker. The rule does not affect the login window. The operator can still sign
in.

``persist=True`` writes rotated state after each borrow. Use it for sites that
expire an unused login. ``headless=True`` is an optional optimization for sites
that do not fingerprint headless browsers.

``anonymous=True`` declares a session that needs no login. A borrow opens a
browser with an empty profile. The operator does not sign in.

Use this option
for a public target or app-owned credentials. These credentials can be an
identity header or a token in the URL. An anonymous session stores no state.
Thus, ``persist=True`` with it fails during class definition.

If a borrowed browser returns to the login page, raise
``BrowserSessionSignedOutError`` from ``druks.browser``. Druks marks the session
as stale. The sessions pane shows this state and stops new borrows. The run
fails with the same reason. After the operator signs in again, the next
scheduled run can proceed.

Provider selection is an operator concern. App workspace code targets the
Druks sandbox contract, not `exe`, AWS, or Docker directly.

### Give an instruction a browser session

An instruction can drive a browser session. Name the session and the client:

```python
from druks.workflows import Instruction, InstructionOutput
from druks.apps import App
from druks.browser import BrowserClient, BrowserSession


class Balance(InstructionOutput):
    amount_due: str
    due_date: str


class Payables(App):
    name = "payables"
    supplier = BrowserSession(site="supplier.example", read_only=True)

    read_balance = Instruction(
        prompt="payables/read_balance.md",
        contract=Balance,
        browser=supplier,
        browser_client=BrowserClient.PINCHTAB,
        include_plugins=False,
        include_mcp=False,
    )
```

The prompt names the page and the tools:

```markdown
Open https://supplier.example/account with browser_navigate. Read what the
account owes and when it is due. browser_snapshot reads the page.
browser_evaluate runs JavaScript in it. Only read. Never submit a form.
```

A workflow calls it like any other instruction: `await Payables.read_balance()`.

Druks borrows the session inside each call and starts PinchTab beside Chrome.
The call's sandbox gets one MCP server, `browser`, with five tools:
`browser_navigate`, `browser_evaluate`, `browser_snapshot`, `browser_text`, and
`browser_action`. They work in one tab, which the first `browser_navigate`
opens. `browser_action` takes a PinchTab action, for example `click` with a
`ref` from `browser_snapshot`.

Each tool runs in Druks against the call's browser. The sandbox has no access to
the browser, its debugger port, its profile, or the bridge's network log.

The key of the server allows the five tools and nothing else. Druks mints
it for the call. It revokes the key when the call ends, fails, or times out, and
when a person cancels the call. It then releases the browser.

The `browser` server comes beside the plugins and MCP servers that the
instruction declares. A site that the operator does not run can try to control the
agent through its pages. Declare `include_plugins=False` and `include_mcp=False`
for such a site.

Each call of an instruction with `browser=` gets its own sandbox, even in a
workflow that sets `steps_reuse_sandbox = True`. A replay of a completed call
starts no browser. An interrupted call borrows the browser again. The call gets
prompt context only. Druks opens no page for it. `BrowserClient.PINCHTAB` is the
only client that an instruction can use.

## Wait for input

The gate fields form the reply schema. `name` fixes the durable gate identity.
This identity selects the receive channel and the `gate` value of the parked
run. You must declare it because the identity must survive a class rename:

```python
from typing import Literal

from druks.workflows import Gate, Workflow


class ApproveReport(Gate):
    name = "approve_report"
    action: Literal["approve", "revise", "cancel"]
    note: str = ""

    @classmethod
    async def on_wait(cls, workflow: Workflow) -> None:
        await notifier.report_ready(workflow.workflow_id)
```

Wait from `run_multistep()`:

```python
reply = await ApproveReport.wait(
    input_request={
        "presentation": "external",
        "label": "Review the night-watch report",
        "url": review_url,
    }
)
```

`on_wait()` is a checkpointed notification step. The workflow then parks
durably and releases its warm sandbox. The external system that owns the answer
resumes the workflow through the gate and its subject:

```python
await ApproveReport.answer(
    repository,
    workflow=Sweep,
    action="approve",
    note="Ship it.",
)
```

`answer()` resolves the workflow's subject run that waits on the gate. It raises
if no such run exists. It also raises when the gate already has an answer or
timed out. A subject can have runs from several workflows. The workflow and
the gate identify the applicable run.

For a subject-backed decision inside the Druks dashboard, use:

```python
reply = await self.review(questions=report.questions, context=review_context)
```

It offers `approve` and `request_changes`. Druks shows the optional `context`
next to the review. The context is text, or a list of `druks.ui` blocks:

```python
reply = await self.review(
    context=[
        ui.Facts([ui.Fact("Peers", len(report.peers)), ui.Fact("Stale", report.stale)]),
        ui.Table(
            columns=[ui.TableColumn("Peer"), ui.TableColumn("Answers", align="end")],
            rows=[ui.TableRow([peer.name, peer.answers]) for peer in report.peers],
        ),
    ]
)
```

The dashboard draws the blocks as a page draws them. Slack draws the facts,
metrics, dividers, links, tables, charts, progress steps, timelines, images,
image galleries, cards, sections, columns, and stacks of an approval with its
own blocks. It shows the other blocks as their
[text forms](druks-ui.md#text-forms). See
[Approvals in Slack](druks-ui.md#approvals-in-slack).

An `Action` or a `Form` in the blocks names the app's own writes. At the top of
the blocks, Slack draws an input for each of its fields and a button for each of
its actions. A form's `extra_actions` are more buttons that send the same
fields. An action with no fields alone in a cell of a table is a button too. The
table must be at the top of the blocks and select no rows.

A click runs the operation through its route, as the person who clicks. The
click sends the answers in the inputs of its block. Slack uses the `label` and
the `confirm` of the action. `tone`, `refresh`, and `link` have no effect there.

Any other action, and a form with `submit="change"`, shows in Slack as its
label, linked to the run's page. The person runs it there as on a page. See
[Actions in Slack](druks-ui.md#actions-in-slack).

`review` refuses a `SecretField`, an `UploadField`, or a `MultiUploadField`
anywhere in the blocks, because a chat message holds no secret input and no file
input. The operator also acts through a `GateControls`. It has no text form.
`review` thus refuses it anywhere in the blocks. An agent's tools send no action.

The `context` of a gate's in-app ask obeys the same rules. A `SubjectStatus` in
the blocks shows where the work stands each time Druks reads the ask. The ask
that Druks stores holds no status.

Text that is not blank, or a list of one or more blocks, guides the next pass.
With this context, `request_changes` does not require answers or a note. Authors
must treat that response as another pass. They must include the context.

A subjectless workflow cannot use in-app review. A subjectless custom gate must
override `on_wait()` to show the wait. Without this override, nobody sees the
wait. Druks then raises an error and does not park the run.

Every gate's reply, a review's and `YesNo`'s included, carries `answered_by`: the
username of the account that answered in Druks. The admin of a
[public number](chat.md#link-a-number) answers as the number's bot admin account,
whose username names no person. `answered_by` is empty when no account answered:

- an answer from outside Druks
- a link in a notification
- a click in Slack by a person whose Slack account is not linked

Only Druks sets it. `answer()` refuses `answered_by`.

To make one person decide, name the username of their account as `approver`. An
app usually reads it from its [settings](#app-settings-and-checks), in a step:

```python
@step
async def get_approver(self) -> str:
    return (await Deploys.settings()).approver
```

```python
reply = await self.review(context=plan, approver=await self.get_approver())
```

Only that account can answer. Druks sends it the
[Run waits for an answer](configuration.md#notifications) notification, and
refuses an answer from every other account, admins included. A chat of another
account that started the workflow gets no buttons. See
[Agent access](chat.md#agent-access). The workflow fails before it waits when no
account has the username, or when the account cannot use the app.

For a yes or no decision, wait on the built-in `YesNo` gate:

```python
from druks.workflows import YesNo

reply = await YesNo.wait(input_request={"presentation": "in_app", "label": "Keep this quote?"})
if reply.action == "yes":
    ...
```

The in-app ask of a gate names an approver with its `approver` key.

Raise `FatalError` for a deliberate domain stop. If readers need a stable
machine failure code, subclass it. Set `code` on the subclass. Unexpected
exceptions make the run fail. Druks raises them again to DBOS.

Stop this workflow's active execution for a subject through the workflow class:

```python
await Sweep.cancel(repository)
```

The workflow class supplies its kind. The caller does not find or handle the
internal timeline row. A cancel request with no active run has no effect. Thus,
a redelivered webhook stays idempotent.

## Wait on an event

A gate waits for a person. To wait for a machine, for example a review from a
bot or the checks on a commit, wait on a topic:

```python
review = await self.wait("review.completed", label="Waiting for the review")
```

The run stays running. The run page shows the label. Druks sends no
notification and keeps the warm sandbox. When the wait is longer than
`ttl_seconds`, the workflow fails with `WaitTimeout`. Without `ttl_seconds`,
the limit is 14 days, as for a gate.

A subscriber answers the wait through the workflow class and the subject. The
facts that it sends are the result of `wait()`:

```python
@subscribe(WorkflowEvent.FINISHED, workflow=PullRequestReview)
async def finished_review_answers_the_build(*, subject: PullRequest, result: dict, **_: object) -> None:
    if item := await WorkItem.get_for_pr(repo=subject.repo, pr_number=subject.number):
        await Build.answer(item, "review.completed", **result)
```

`answer()` goes only to a run that waits on that topic. Other runs get nothing.

A wait can also fail. If the work that the run waits for fails, answer with
`failure`. The wait raises `WaitFailed` with the topic and the reason. The run
fails with it:

```python
@subscribe(WorkflowEvent.FAILED, workflow=PullRequestReview)
async def failed_review_fails_the_build(*, subject: PullRequest, failure: str, **_: object) -> None:
    if item := await WorkItem.get_for_pr(repo=subject.repo, pr_number=subject.number):
        await Build.answer(item, "review.completed", failure=failure)
```

To look again later rather than wait for an answer, sleep:

```python
await self.sleep(60, label="Waiting to look again", until="source.changed")
```

The run stays running and shows the label. The sleep releases the warm sandbox,
as a gate park does. The next agent call boots a new one. The sleep ends after
the given seconds, or earlier when `answer()` arrives on `until`. It returns
whether an answer ended it. It never fails.

## Report status

A workflow can show where it stands while it runs. Call `report` with a list of
`druks.ui` blocks each time the status changes:

```python
from druks import ui

await self.report(
    [
        ui.Progress(
            "Build",
            steps=[
                ui.ProgressStep(label="Plan", status=ui.StatusValue("done", tone="success")),
                ui.ProgressStep(label="Code", status=ui.StatusValue("writing", tone="active")),
            ],
        )
    ]
)
```

Each call replaces the status. The status is one notification with the reason
`run.status`, which keeps the blocks and their
[text form](druks-ui.md#text-forms). Every block must have a text form.
`report` thus raises `NoTextFormError` for a `Form`, an `Action`, or a
`GateControls` anywhere in the blocks, before it saves anything.

`report` is a checkpoint. A replay reports nothing again and sends nothing
again. Druks does not send a message exactly once. When a worker stops after
Slack took a post and before Druks recorded it, the send runs again. It can then
post the message a second time.

### Where the status goes

- A workflow that a Slack chat started shows its status in the thread or the
  direct message of that chat.
- A workflow that a web chat started shows its status in that conversation on the
  Chat page, at the place where the workflow started. Each report replaces the
  status there, and the status stays after the workflow ends or fails. The page
  reads the status again every two seconds. A workflow that does not report shows
  only a line below the messages while it is active. See
  [Read replies and tool calls](chat.md#read-replies-and-tool-calls).
- A workflow that no chat started shows its status where its account's
  notifications go: the Slack channel that its account chose, else the
  account's direct message. See [Notifications](configuration.md#notifications).
- A web chat keeps the status in Druks. So does a chat on a channel that cannot
  edit a message, such as WhatsApp. So does an account with no Slack channel and
  no linked Slack account. Druks sends no message. It does not send the status of
  a chat to the account instead.

The first report posts the message. Each later report edits it. The bell in the
dashboard does not show a status, because the workflow changes it in place.

### How Druks sends the edits

Druks sends the status a few seconds after its last change. Druks thus sends
changes that come close together in one edit. It always sends the last change.
While a status changes continuously, Druks sends it every few seconds.

When a post or an edit fails, Druks tries it again. Its failure never makes the
workflow fail. When no tries are left, the status records the failure. The next
report sends the newest status. It posts the message when no earlier post
arrived at Slack. It edits the message when one did.

### Waits and the end

The status keeps your report as you wrote it. While the workflow runs, the
message shows that report and nothing more. When the workflow waits or ends,
Druks draws the message again and adds where the workflow stands:

- When the workflow parks on a gate, a callout at the end of the message says
  what it waits for. An example is "Waiting for an answer: Review". A gate that
  waits with no ask gives "Waiting for an answer.". Each `ProgressStep` whose
  status has the tone `active`, at any depth, shows "waiting" and keeps its tone.
- When the workflow continues, the message shows your report again, with your
  own words for each step and no callout.
- When the workflow finishes or fails, or a person cancels it, Druks edits the
  message one last time. The callout says "Finished.", "Failed.", or
  "Cancelled.". Each step that is still in progress shows that end, with a tone
  that is not `active`. A step that settled keeps its status.

When a person retries a workflow that failed, it keeps the message. The message
shows the last report again, as you wrote it, until the workflow reports, waits,
or ends.

An approval stays a message of its own, with its buttons. An answer to the
approval, or its close, edits only the message of the approval.

The final status is in addition to what Druks already sends when a workflow
ends. Druks still tells the chat that started the workflow its result, its
failure, or its cancel. It still tells the account when a workflow that no chat
started fails.

## Give runs a subject read-side

A subject is what your runs are about, such as a repository, a work item, or a
pull request. It is always a class. The workflow names it:

```python
class Sweep(Workflow):
    subject = Repository
```

This declaration lets Druks show subject history and available actions before a
run exists. `start()`, `cancel()`, and `Gate.answer()` enforce the declaration.
A workflow with a subject starts with an instance of that class. A workflow
without a subject passes `subject=None`.

When the subject is a row that you keep, list, edit, and show fields from,
subclass `StoredSubject` instead of `Model`. The class name is the subject type:
`Repository` becomes `repository`.

```python
from druks.db import StoredSubject
from sqlalchemy.orm import Mapped


class Repository(StoredSubject):
    full_name: Mapped[str]

    def __str__(self) -> str:
        return self.full_name
```

A subject's `__str__` is its name on runs, the Activity feed, and its board. The
default is its type and id, such as `repository 7`. Its type and id identify
it. The name then can be the same as the name of another subject.

Druks supplies the rest: the
table `night_watch_repository`, an `id`, `created_at` and `updated_at`,
`create()`, `save()`, and `delete()`. It also supplies a board of the newest
hundred rows by `updated_at`, or in the class's declared ordering. To scope the
board by caller, or to select other rows, override `list_summaries()`.

If you keep no row for a subject, subclass `Subject`. The platform requires only
an identity. The ID is the full record and its name:

```python
from druks.workflows import Subject, SubjectSummary


class PullRequest(Subject):
    @classmethod
    async def list_summaries(cls, account_id: str | None) -> list[SubjectSummary]:
        return [pull_request.get_summary() for pull_request in await cls.list_open()]
```

Each ID names one of these subjects. A detail read then always answers.

Override `get_or_none(id)` to reject an invalid shape. For example,
`owner/repo#7` is a pull request. `nonsense` returns a 404.
`await PullRequest.get(id=subject_id)` raises `ObjectNotFound` for it, the same
as `Model.get`. Code that already has the parts builds the subject directly:
`PullRequest(id=f"{repo}#{number}")`.

An identity-only `Subject` that a workflow declares must implement
`list_summaries()`. A `StoredSubject` has a board by default. The board reads
the method and passes the caller. `account_id` is the signed-in account, or None
outside a request. If each operator has a separate board, use it to scope the
rows. If all operators share one board, ignore it.

A model method never reads request context. Druks validates the method at load.
If a `Subject` does not have it, the app does not load. The error names the app, the
subject, and the method.

Druks serves the same `/api/night_watch/repository` surface for both subject
types. This surface contains a board, detail pages, and a live stream. Druks
mounts it for each declared subject. Each response contains your summary, the run
status, the timeline, the agent calls, the artifacts, and the active question.
While a run starts or waits, the status also holds its phase.

A workflow can declare a subject from another app, such as Software Factory's
`PullRequest`. Your board then lists the subjects that your workflows keep open.
Your board, your home page, and the `ui.SubjectStatus` and `ui.GateControls` on
your pages read only your workflows' runs. A subject's detail page shows the runs
of every app. See [Where the work stands](druks-ui.md#where-the-work-stands).

Pass the subject instance to each component that requires one. This includes a
workflow start, a gate answer, or an event:

```python
await Sweep.start(subject=repository, repo=repository.full_name)
```

Inside the workflow, `self.subject` resolves through the declared class. It is
live, not a snapshot from the dispatch. A run can park on a gate for three days.
After resume, it reads the current row. If the row no longer exists, the subject
does not resolve.

Your app names domain outcomes. For example, a work item ships or an operator
cancels it. Druks owns the active run state. Read this state from the status:

```python
status = await repository.get_status()
if status.is_parked:
    ...
```

`status.is_parked` is true when a run stopped to ask a person something.
`status.kind` names the workflow of the current run. `status.gate`
names the question that the run stopped on. `status.phase` names the step of a
running run, such as `queued_for_vm`.

A subject that Druks did not run has no state. `status.state` and `status.run`
are both None. `await repository.get_status(workflow=Sweep)` narrows the read to
one workflow's runs. It answers the same way when the subject has no run of
that kind.

A page that lists subjects uses `get_statuses()` instead. It takes the ids and
returns one status for each id, in a single read:

```python
summaries = await Repository.list_summaries(account_id)
statuses = await Repository.get_statuses([summary.id for summary in summaries])
```

The platform's own board uses this read. A declared page that lists fifty rows
then costs one query rather than fifty.

`get_status()`, `get_statuses()`, and `list_open()` count the runs
of the workflows of the app that defines the subject. A run of another app on the
same subject does not show. On a subject from another app, pass your workflow:
`get_status(workflow=Sweep)` or `list_open(workflows=[Sweep])`.

A page needs neither read to show where the work stands.
`ui.SubjectStatus(repository)` takes the subject. Druks reads every status on
the page when it serves the page. See
[Where the work stands](druks-ui.md#where-the-work-stands).

## Activity facts and signals

Use [announcements](#announce-domain-events) for facts that the app owns.
Use an instruction output's `to_artifact()` and `to_event()` for a saved result.
Do not announce that same result again from a completion subscriber. Field Notes
records `gist.prepared` for each gist that its agent prepares. It announces
`note.gist_approved` when the operator approves a gist.

Druks records these workflow facts without app calls:

- Accepted work: a new subject run entered the queue. A deduplicated start adds
  no second row.
- Input requested: the workflow stopped at a gate. The record holds the gate and
  the request time for that round.
- Response received: the concrete gate validated a reply. This does not mean
  that the domain accepted the reply's proposal. The workflow decides that.
- Run finished: the run ended.
- Run failed: the run ended with a terminal failure. Routine running signals
  remain available to subscribers but do not enter Activity.
- Run cancelled: an operator cancelled the run through the cancel route. Domain
  cleanup that cancels a run records no row and announces no owner outcome.

An external owner can announce an outcome after the run stops. When the owner
reports that outcome, record it. Do not infer it from the run state.

Each Activity row keeps the subject's name as Druks recorded it. `start()` stores
`str(subject)` in the workflow attributes. Admission, transitions, workflow
announcements, output artifacts, and operator cancellation use that run's
recorded name. A rename during the run applies to the next run. A subject
announcement records the subject's name when it records the event.

Druks records `payload.run` and `payload.kind`. An announcement that names one
of them raises `WorkflowError`. A later rename or deletion does not change
history. Search matches a literal, case-insensitive part of the recorded key or
title. It does not search current subjects, failure text, or artifacts.

The feed exposes one `payload` dictionary with the stored fact names, including
app-owned facts. The envelope supplies `id`, `seq`, `at`, `topic`, `app`,
`subjectType`, `subjectId`, and `subjectKey`. The dashboard supplies readable
activity labels. For example, it shows `gist.prepared` as "Gist prepared". Keep
UI wording out of the event identity.

A gate request and its reply retain the same run, gate, and request-time
identity. A result retains its artifact identity. These references describe the
recorded occurrence even when the current subject, run, or file is unavailable.

React with filters rather than body guards:

```python
from druks.signals import subscribe
from druks.workflows import WorkflowEvent


@subscribe(WorkflowEvent.FINISHED, subject=Repository)
async def on_sweep_finished(*, subject: Repository, **_: object) -> None:
    await notify(subject.full_name)
```

`subject=Repository` selects each workflow for a repository. `workflow=Sweep`
selects one workflow and its declared subject. Do not use both filters together.
The subscriber body receives its subject with either filter.

Signals deliver at least one time. A subscriber exception propagates. Then the
webhook provider or DBOS retries the publication. Make each reaction idempotent.

## Receive webhooks

A webhook authenticates and normalizes provider input. It must publish a
domain-neutral signal rather than contain workflow policy:

```python
from fastapi.responses import JSONResponse

from druks.signals import publish
from druks.webhooks import Webhook, verify_hmac_sha256


class NightWatchWebhook(Webhook):
    provider = "night_watch"
    category = "events"

    async def request_is_authentic(self) -> bool:
        verify_hmac_sha256(
            self.raw_body,
            self.request.headers.get("x-signature"),
            secret,
        )
        return True

    def get_action(self) -> str:
        return self.data["type"].replace(".", "_")

    async def on_report_approved(self) -> JSONResponse:
        await publish("report.approved", payload=self.data)
        return JSONResponse({"accepted": True})
```

A delivery that is a person's request, such as a ticket's status change, names
that person. Override `get_actor_id()` to return the provider's id for the person.
The default returns `None`. That means that the delivery is no person's request.
Druks finds the account under the authority that
`Authentication.get_authority()` of the webhook's service returns. The
authority is where that service's people sign in.

While the handler runs, a run
that a subscriber starts runs as that person's account. A person with no Druks
account counts as a member of no team. A run of an app that a team lists then
does not start. A run of an open app starts as `account_id`, or else as the
default account. The app makes no lookup of the person and no access check.

The public path is `/_external/night_watch/events/`. Druks deduplicates a
delivery when the class supplies a delivery key. A handler that fails releases
the claim, so that the provider can retry.

### GitHub facts

The operator App's webhook publishes `pr.opened`, `pr.synchronized`,
`pr.edited`, and `pr.ready_for_review`, the App's own pull requests included.
Each carries `repo`, `pr_number`, and a `payload` with `branch`, `is_fork`,
`base_branch`, `head_sha`, `title`, `body`, `draft`, `author`, and
`author_can_write`. A fork can use any branch name. A subscriber that matches
on `branch` then filters with `payload__is_fork=False`.

A comment on a pull request or on a line of its diff publishes `pr.commented`
with `repo`, `pr_number`, and a `payload`. The payload holds `comment_id`,
`body`, `author`, `author_is_bot`, `author_can_write`, `actor_id`, and
`review_thread_id`. `review_thread_id` is the review comment that GitHub puts
replies under. It is `null` for a comment outside a review thread.

A comment on an issue publishes `ticket.commented` instead, one of the
[tracker events](#tracker-events). Both come from any sender, apps included.
Filter with `payload__author_is_bot=False` to get people only. The GitHub chat
channel answers both.

A completed check suite publishes `commit.check_suite_completed`. A commit
status publishes `commit.status_updated`. Each carries `repo` and a `payload`
with `head_sha` and `branches`, the branches that commit heads. The status fact
also carries `state` and `is_pending`, which is true while that state is
`pending`. A commit on a fork or a tag heads no branch.

`post_check_run` creates the named check run on the head commit, or updates the
one that the App already posted there. An annotation is GitHub's shape. The App
needs **Checks: Read and write**. See [Configuration](configuration.md#github).

```python
from druks.core.services import Github
from druks.signals import subscribe


@subscribe("pr.synchronized")
async def check_the_head(*, repo: str, payload: dict, **_: object) -> None:
    findings = await lint(repo, payload["head_sha"])
    github = await Github.get_client(repo)
    await github.post_check_run(
        repo,
        head_sha=payload["head_sha"],
        name="night-watch",
        status="completed",
        conclusion="failure" if findings else "success",
        title=f"{len(findings)} findings",
        summary="One annotation per finding.",
        annotations=[
            {
                "path": finding.path,
                "start_line": finding.line,
                "end_line": finding.line,
                "annotation_level": "failure",
                "message": finding.message,
            }
            for finding in findings
        ],
    )
```

`dispatch_workflow` starts an Actions workflow, named by its file, on a ref with
its inputs. It returns the id of the run it started. `get_workflow_run` reads
that run: its `status`, its `conclusion` when `status` is `completed`, and its
`html_url`. The App needs **Actions: Read and write**. An app thus dispatches
as the App and holds no token of its own.

```python
from druks.core.services import Github


async def deploy(environment: str) -> dict:
    github = await Github.get_client("acme/portal")
    run_id = await github.dispatch_workflow(
        "acme/portal", "deploy.yml", ref="main", inputs={"environment": environment}
    )
    return await github.get_workflow_run("acme/portal", run_id)
```

### Tracker events

Linear and GitHub tickets publish the same events. Each `payload` names its
tracker in `source`. Filter on it:

```python
from druks.signals import subscribe


@subscribe("ticket.created", payload__source="linear")
async def on_new_issue(*, payload: dict, **_: object) -> None:
    ...
```

| Event | Linear | GitHub |
| --- | --- | --- |
| `ticket.created` | someone creates an issue | someone opens an issue |
| `ticket.updated` | an issue's title, description, labels, or project changes | someone edits or reopens an issue |
| `ticket.commented` | someone comments on an issue | a comment on an issue, from any sender |
| `ticket.transitioned` | an issue's status changes | someone adds a label, through Software Factory's GitHub tracker |

Every `ticket.*` event carries the same ticket keys, whatever the tracker:
`source`, `ticket_id`, `identifier`, `title`, `url`, `project_name`, `labels`,
and `actor_id`. A key that the tracker sends no value for is `null`.
`identifier` is the ticket's name: `DRU-123` in Linear, `owner/repo#number` on
GitHub. It changes when a Linear issue moves to another team, or when someone
renames a GitHub repository. Store state under `ticket_id`, the tracker's own
id.

`actor_id` is whoever made the change. A payload carries the ticket as it
is after the change, not what changed. Read the ticket for its description or
body. A Linear update that also changes the status publishes only
`ticket.transitioned`.

| Event | Adds |
| --- | --- |
| `ticket.created`, `ticket.updated` | nothing |
| `ticket.commented` | `comment_id`, `body` |
| `ticket.transitioned` | `status`, `assignee_id`, `assignee_email`, `assignee_name` |

A key that only one tracker has is only in that tracker's events. Filter on
`source` before you read it. Every Linear event adds `team`, the team's id.

A Linear `ticket.commented` also adds `parent_id`. A Linear comment's payload has
no project or labels. `ticket.commented` from Linear then carries `null` for
both. A Linear comment on a project or a document publishes nothing.

On GitHub, `project_name` is the repository. A GitHub `ticket.commented` adds
`author`, `author_is_bot`, and `author_can_write`, as `pr.commented` has them.
Jira and Software Factory's own board publish `ticket.transitioned` too.

## Models and migrations

Models subclass `druks.db.Model`. Druks names the table for the app and the
class: `Report` in `night_watch` is the table `night_watch_report`. A class that
sets `__tablename__` keeps it. Every app table starts with `<name>_`:

```python
from sqlalchemy.orm import Mapped, mapped_column

from druks.db import Model


class Report(Model):
    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str]
```

A model reads and writes by field:

```python
report = await Report.create(repo="acme/widgets", status="open")
report = await Report.get(id=report_id)
report = await Report.get_or_none(repo="acme/widgets")
reports = await Report.all()
open_reports = await Report.filter(status="open")
report.status = "closed"
await report.save()
await report.delete()
```

`get` raises `ObjectNotFound` on a miss. The API answers it with 404. A page
answers it with an empty state. A route or page that names a row by id then
never writes either answer.

`get_or_none` returns None instead. Both expect one row. Two rows raise
SQLAlchemy's `MultipleResultsFound`. Put a unique constraint on the fields that
they read.

`all` returns every row. `filter` returns
the rows that match every field that it gets. It needs at least one field. Both
return the rows in primary key order, or in the order that the class declares
on its class line, in Django's form:

```python
class Report(Model, ordering=("-created_at",)):
    ...
```

Druks reads a text value as its column's type. An id from a URL then
finds its row. For a read that needs a limit, a different order, or a filter
other than equality, write `select()`:

```python
reports = await db_session().scalars(
    select(Report).where(Report.status != "closed").order_by(Report.created_at).limit(20)
)
```

When a page names by id a row that does not exist, it shows an empty state.
The empty state links to its parent page, or to the app's landing page.

A `Mapped[datetime]` column stores UTC. A `Mapped[SomeStrEnum]` or
`Mapped[SomeLiteral]` column stores its value as text under a CHECK constraint of
the allowed values. A `Mapped[list]` or `Mapped[dict]` column is JSONB. A typed
one such as `Mapped[list[dict]]` names the type,
`mapped_column(JSONB)`. Encrypted columns come from
`druks.db.fields`: `EncryptedTextField` and `EncryptedJsonField`, with their
value types `Secret` and `SecretsMapping`.

Generate the app's revision after the model is importable:

```bash
uv run druks makemigrations night_watch -m "add reports"
uv run druks init-db
```

Druks scopes autogeneration to the table prefix, names the revisions
`night_watch_0001`, `night_watch_0002`, and so on, and writes the version to
`alembic_version_night_watch`. Never write a revision manually. The one change
that Alembic does not detect is a new member of an enum. That change needs one
`drop_constraint` and one `create_check_constraint`.

Druks supports Postgres 16 and newer. A model or revision uses only what 16
has. For example, do not use the SQL function `uuidv7()`, virtual generated
columns, or `RETURNING OLD/NEW`. Make a UUIDv7 in Python, as Druks does.

Query through `druks.db.db_session()` inside an HTTP request, a durable step, or
a workspace's `prepare()`. Outside those, `db_session()` raises. A workflow
body holds no session. Read inside a `@step`.

`await self.subject`, `start()`,
`dispatch()`, `cancel()`, and `answer()` open their own session. A row that a step
returns is detached. Load what the body reads from it inside that step.

An HTTP request is one transaction, which commits before Druks sends the
response. A step is not one transaction. Each read and each write in a step commits
on its own. A step then holds no database connection while it calls another
service or waits.

Writes that must commit together go in one `druks.db.atomic()`
block. They all commit at its end, or none does. The block holds its connection
until then. Keep calls to other services outside it:

```python
from druks.db import atomic


class Sweep(Workflow):
    @step
    async def scan(self, report_id: int) -> None:
        report = await Report.get(id=report_id)
        findings = await scanner.scan(report.repo)
        async with atomic():
            for text in findings:
                await Finding.create(report_id=report.id, text=text)
            report.status = "scanned"
            await report.save()
```

HTTP response models subclass `druks.schemas.Schema`, whose snake_case fields
serialize as camelCase. Request models are ordinary Pydantic models.
Druks mounts each router from a discovered `routes.py` under the app namespace.
It tags the router with the app name. A router declares only the prefix of its
resource:

```python
router = APIRouter(prefix="/reviews")
```

Your routes require authentication. The loader puts each router behind the
platform identity gate. The gate accepts a Bearer PAT or the signed-in session.
The gate blocks anonymous requests before your code. You do not implement
authentication. If a route uses account scope, read the caller:

```python
from druks.accounts import current_account_id

@router.get("/reviews")
def list_reviews() -> list[ReviewResponse]:
    return Review.list_for_account(current_account_id.get())
```

`Account` is a `Model`. Your rows relate to it with `relationship(Account)`.
`Account.get_or_none(id=...)` and `Account.filter(kind=AccountKind.OPERATOR)`
read other accounts, such as an assignee that a form submitted.
`Account.lookup(authority, subject)` returns the account that signed in as a
provider user, such as an assignee that an event names. Druks itself finds the
account of the person who asks.

Druks prefixes every operation id of an app with the app name. Write the bare
verb. For example, `operation_id="write_note"` in `field_notes` becomes
`field_notes_write_note` in the OpenAPI document. Startup refuses an id that
already starts with the app name. An `Action` names the bare id.

Tag a route with `agent` to create an MCP tool from it. Give the route an
explicit `operation_id`. The tool takes the prefixed name. The docstring
supplies the description.

A `GET` route is read-only. If a write is non-destructive, declare
`x-destructive: false`. If a write is idempotent, declare `x-idempotent: true`.
The safe defaults are destructive and non-idempotent.

A tool that is available only in some configurations guards its router with a
dependency. The dependency is an `async def` with no parameters. It reads the
setting or the connection. It raises `ToolNotAvailable` when the configuration
is not one of them.

Druks runs it on each list of the tools and on each call. Druks lists the tool only while the
dependency allows it. A call to the route gets the error. Startup refuses a
route with no `operation_id` or no docstring.

Druks uses two spellings. The spelling of a segment shows who owns it:

| | |
| --- | --- |
| `snake_case` | an identity that the platform serves, such as your app name or a subject type |
| `kebab-case` | a resource that you named, such as your route prefixes or your frontend paths |

Thus, `/api/software_factory/pull_request` is the subject board for pull request
review runs. `/api/software_factory/reviews` is the resource that your POST
creates. The platform matches `<subject_type>`, `transcripts`, `pages`, and
`uploads` before your routers.

A custom router cannot take a platform read,
including through a catch-all. Druks reserves `transcripts`, `pages`, and
`uploads`. A subject type or a router prefix that takes one makes the load fail.
Name the router for its resource to prevent a conflict.

## Declare a service

A service identity is the appliance registration at an external provider. A
deployment has one identity for each service string. The platform GitHub App is
the first service identity. OAuth grants are not service identities. The
platform stores them after an operator connection.

See [Connect provider accounts](#connect-provider-accounts-oauth).

An app serves a service's endpoints under `/api/<app>/services/<slug>/...`, where
the app mounts those routers like every other route.

Put a credential that only your app uses in its app settings.

Declare one class in `services.py`. The platform creates the connection card in
Settings. It validates and stores the submitted values. It encrypts
`SecretStr` fields and stores plain fields as identity facts. A field with a
default can stay empty on the card.

The platform also reports the state through `druks doctor`. The class name is
the identity. Druks derives the slug from it (`Gmail` → `gmail`,
`GoogleCalendar` → `google_calendar`) and derives the card heading from the
slug:

```python
from pydantic import BaseModel, Field, SecretStr

from druks.services import Service, ServiceConnectError, auth


class Gmail(Service):
    description = "The appliance's own OAuth client. Every mailbox authenticates against it."

    class Authentication(auth.Authentication):
        sign_in = auth.SignIn(
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
        )
```

A service declares how it authenticates in a nested class named
`Authentication`. Druks finds that class by name, as it finds `Settings`. The
class names its ways as instances of the kinds in `druks.services.auth`, under
fixed names.

`api_key = auth.ApiKey()` is a pasted key. `jwt = auth.Jwt()` is an app's
private key. `oauth = auth.Oauth(...)` is the service's own OAuth app. The
operator installs it. Druks acts with the grant. `sign_in = auth.SignIn(...)` is
an OAuth client that people only sign in through.

A service declares `oauth` or `sign_in`, not both. Each kind owns its
credential: `api_key`, `private_key`, or `client_id` and `client_secret`. These
fields join the card after `Settings`, which holds the rest, such as a webhook
secret or the identifier of an account.

Druks acts as the service through `api_key`, `jwt`, or `oauth`. It holds one of
them at a time. A pasted key ends the app's grant. An install ends the key.
`Authentication` also carries what the provider does differently, as
classmethods. See [Connect provider accounts](#connect-provider-accounts-oauth).

The slug names the service's vault row and the connect wire. A class rename
changes the slug and the key of the card. The connected identity then has no
card. Set `slug = "gmail"` on the class to keep the old key.

Read it through the same class:

```python
(await Gmail.get()).secrets["client_secret"]
await Gmail.is_connected()
```

`Gmail.get()` raises `ServiceNotConnectedError` when nobody connected the
service.

An optional `verify` classmethod checks the pasted values with the live provider
before anything replaces the current identity. It returns extra identity facts to
store. It raises `ServiceConnectError` with a message that is safe to show. The
platform never echoes what the operator pasted:

```python
@classmethod
async def verify(cls, settings: Settings) -> dict:
    if await probe(settings):
        return {}
    raise ServiceConnectError(
        "The provider did not accept these credentials. Check them and paste them again."
    )
```

If the appliance is healthy without the service, set `required = False` on the
class. Doctor then reports a note, not an error that asks for setup.

The card renders `description` as Markdown. The description can then link to a
setup guide.

Name the service for the integration that your app uses (`Gmail`), not for the
provider (`Google`). A second integration on the same provider declares its own
service. The operator decides whether each card uses a shared or narrow
registration. This choice controls the scope and the effect of a credential problem.

If the provider runs an MCP server that the service's identity also signs in to,
set `mcp_host` to the host of that server. An OAuth MCP server at that host then
uses the service's credential.

If the service has OAuth endpoints, each account
uses its own sign-in at the service, with no second consent. If not, the
default account sends the service's pasted login. Every other account connects
its own grant. Override `Authentication.get_authorization` to make the
`Authorization` value from the connected row:

```python
class Acme(Service):
    mcp_host = "mcp.acme.example"

    class Authentication(auth.Authentication):
        api_key = auth.ApiKey()

        @classmethod
        def get_authorization(cls, login) -> str:
            return f"Bearer {login.secrets['api_key']}"
```

### Resources at a provider

A resource is an item that an app keeps at a provider, for example a Cloudflare
Worker. Each resource has its own token.

A resource class does only what the token of the resource cannot do. It has four
steps: `create`, `update`, `delete`, and `get_token`. Druks refuses a resource
class with other public methods. An app makes all other calls to the provider with
`get_token()` and the SDK of the provider.

Declare each resource class inside its service class, as you declare `Settings`.
Write the steps that Druks uses with pasted credentials:

```python
from hashlib import sha256

from druks.services import Resource, Service


class Cloudflare(Service):
    host = "api.cloudflare.com"

    class Authentication(auth.Authentication):
        api_key = auth.ApiKey()

    class Workers(Resource):
        def __init__(self, key: str) -> None:
            super().__init__(sha256(key.encode()).hexdigest()[:12])

        async def create(self) -> None: ...
        async def update(self, *, domain: str | None) -> dict | None: ...
        async def delete(self) -> None: ...
```

The app uses the resource class in the same way in each installation:

```python
worker = Cloudflare.Workers(site.id)
await worker.create()
state = await worker.update(domain="www.example.com")
value, expires_at = await worker.get_token()
```

These rules apply to each resource:

- A resource keeps the key that the app gives to it.
- The `__init__` method can make the name from the key, as in the example. If
  the class has no `__init__`, the name is the key.
- The name starts with the `resource_prefix` of the service in druks.toml. The
  provider must get `worker.name`, not the key.
- The `update` method takes one attribute and returns the answer for that
  attribute. The value `None` removes the attribute.
- If the resource does not exist, the `delete` method must not raise an error.
- If the class does not declare `get_token`, the token is the one secret of the
  card. A class declares `get_token` only to give a token with fewer permissions.
- Druks uses a token again until a short time before the token expires.
- The slug of the class is the class name in snake case. `Workers` gives
  `workers`. A class can set `slug` to keep its slug after a rename.
- A step can also raise `ResourceError`. Catch it with the errors of the SDK of
  the provider. Its `status_code` and `reason` are the manager's answer. The
  app can then tell its user why the manager refused a step. A step that got no
  answer has no status code.

To give the token to a sandbox, add the secrets of the resource to the secrets of
the workspace:

```python
class SiteWorkspace(RepoWorkspace):
    @classmethod
    async def get_secrets(cls, site):
        return [*await super().get_secrets(site), *await Cloudflare.Workers(site.id).get_secrets()]
```

The sandbox holds a placeholder in `CLOUDFLARE_API_TOKEN`. The name of this
variable is the service slug and `API_TOKEN`, in upper case. On requests to
`host`, the secrets proxy replaces the placeholder with the token. A sandbox can
hold only one resource of each service.

Druks asks the service's `Authentication` for each token that a sandbox fetches
from the row of the service:

```python
@classmethod
async def issue_token(
    cls, resources: tuple[str, ...] = (), *, name: str = ""
) -> tuple[str, datetime | None]:
```

A resource recipe answers when `resources` names one. Otherwise the default
answers in this order. When `name` is the service slug and the field name, it
answers that pasted field. Under any other name, it answers the installed app's
access token. Without an installed app, it answers the one secret of the row.

When the row holds a credential that no sandbox must get, override
`issue_token`. Return the token and its expiry. When the row cannot give a token until a
person acts, raise `MissingSecretError`.

Before each chat turn, Druks asks the service to prepare an operator's sandbox,
once for each sign-in that the sandbox holds:

```python
@classmethod
async def prepare_sandbox(cls, host: Host, connection: Connection) -> None:
```

The default does nothing. `Github` overrides it to set the git author of the
sandbox's commits. Catch each failure that the sandbox can survive. Log
it. Anything that `prepare_sandbox` raises makes the turn fail.

## Connect provider accounts (OAuth)

Declare the client that people sign in through as `sign_in`, or the service's own
OAuth app as `oauth`. Both have `client_id` and `client_secret`, which the
operator pastes:

```python
class Acme(Service):
    class Authentication(auth.Authentication):
        sign_in = auth.SignIn(
            authorization_endpoint="https://acme.example/oauth/authorize",
            token_endpoint="https://acme.example/oauth/token",
            basic_auth=True,
            identity_endpoint="https://acme.example/oauth/userinfo",
            identity_scopes=("openid", "email"),
            identity_key="sub",
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )
```

`basic_auth=True` sends the client secret with HTTP Basic on the token
endpoint. `basic_auth=False` sends the secret in the body.

An `oauth` way is the service's own app. The operator installs it from the
card. Druks acts with the grant that the install returns. `get_grant()` and
`get_access_token()` on the service read that grant.

`install_params` join the
install request. `Authentication.get_install_endpoint()` names where the
install starts. `Authentication.complete_install(tokens)` records what the
install proves.

`extra_authorize_params` declares special consent-query values for the
provider. The platform adds them to every sign-in that it starts for the service.
The example shows Google's values. Google grants a refresh token only when the
consent asks for `access_type=offline` with `prompt=consent`.

`identity_endpoint` names the provider endpoint that returns the signed-in
account's facts (email, username, name). Druks calls it once at consent and
shows the facts as the connection label in Settings. `identity_scopes` are the
scopes that this call requires. Druks adds them to the consent request.

People sign in through the client when it declares `identity_key`, the
identity fact that names the provider account: `"sub"` for Google, `"subject"`
for GitHub. A fresh sign-in that matches a connection for the same
owner updates that row and does not create a second one. A revoked row that
matches becomes live again and keeps its id.

Without `identity_key`, an `oauth`
app only installs. The card then offers no sign-in. `identity_key` needs
`identity_endpoint` or a `get_identity` override. `identity_endpoint` needs
`identity_key`. Druks refuses a service that declares one without the other.

Some providers have no such endpoint, or return the facts in a different
shape. Override `Authentication.get_identity` for them:

```python
    class Authentication(auth.Authentication):
        sign_in = auth.SignIn(...)

        @classmethod
        async def get_identity(cls, access_token: str) -> dict:
            payload = await fetch_profile(access_token)
            return payload["data"]
```

`Authentication` also has `get_consent_query(scopes)`, `read_grant(tokens)`,
and `is_grant_revoked(response)`, for a provider that shapes the consent
query, the token answer, or a refused refresh differently.

One provider can serve several services. For example, Google serves both Gmail
and Google Calendar. Each service keeps its own card and its own key.

Share the
provider's declarations through an abstract base. Set `abstract = True`. The
base never registers. Each subclass inherits everything that the base
declares, `Authentication` included. A subclass needs nothing more than its class
name:

```python
class GoogleOauth(Service):
    abstract = True

    class Authentication(auth.Authentication):
        sign_in = auth.SignIn(
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            identity_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
            identity_scopes=("openid", "email"),
            identity_key="sub",
        )


class Gmail(GoogleOauth):
    pass


class GoogleCalendar(GoogleOauth):
    pass
```

Declare your app's use of the service, with the scopes that your calls need:

```python
class NightWatch(App):
    name = "night_watch"
    acme = Acme.with_scopes("profile.read", "posts.write")
```

A *connection* is one signed-in provider account. A user can have several
connections for each provider. Each mailbox, handle, or workspace can have one.
The platform stores the refresh token, granted scopes, and owner for each
connection. Workflow code reads them through the declaration. It gets one token
for each connection:

```python
for connection in await NightWatch.acme.list_for_account(account_id):
    token = await connection.get_access_token()
```

When your app declares no scopes, read the sign-in facts directly from
the service:

```python
from druks.core.services import Github

sign_ins = await Github.list_for_account(self.account_id)
```

Each connection's `identity["login"]` names the GitHub user. The direct read
does not add the app to the service's scope declarations. An empty list means
that the account has no live sign-in. Both reads exclude revoked connections.

`account_id` is the caller: `self.account_id` in a run body,
`current_account_id.get()` in a route, the handler's argument in a subscriber,
the platform's argument in `list_summaries`.
`await NightWatch.acme.get(connection_id)` returns one connection when your own
row stored its id.

Each connection carries `id`, `scopes`, `identity`,
`account_id`, and `connected_at`. `identity` holds the provider's facts for the
sign-in. `account_id` is the Druks account that signed it in. The `scopes`
value is a list, or `None` when the provider did not report scopes.

The handle serves live connections only. `get` and `list_for_account` return no
revoked connection. Its platform row survives with its owner and identity.
Your rows never need tombstone copies of either.

To start a sign-in, your UI opens `/api/oauth/acme/connect`. The platform
requests the combined scopes from each installed app. It stores the connection
for the signed-in user. A fresh sign-in creates a new connection, unless the
service's `identity_key` matches it to a connection for the same
owner.

To widen the scopes of a connection, open
`/api/oauth/acme/connect?connection=<id>`. Reconsent replaces its tokens. Only
the user who owns the connection can reconsent or revoke it. For another user's
connection, the platform answers 404.

Reconsent names the row. It then also
makes a revoked connection live again under its old id. A fresh sign-in that
matches the `identity_key` does the same.

Add `?next=/night_watch/accounts` to send the user to your page after
consent instead of the generic "connected" page. `next` must be a bare path
that starts with `/`. Druks rejects a URL with a scheme or host. Thus, the
connection flow cannot redirect to another host. Register
`https://<host>/api/oauth/callback` as the provider redirect URI. It serves
each service.

React to sign-ins with signals. The platform publishes
`oauth.connected` when a consent completes. `reconsent` is true when the
consent replaced the tokens of a connection. This happens on reconsent by
id and on an `identity_key` match, for a live or a revoked row.

The platform publishes `oauth.disconnected` after a user revokes a connection.
A replacement of the service's client credentials also publishes this signal.
So does a token refresh that the provider answers with `invalid_grant`. Druks
then revokes that connection, because the provider no longer accepts the grant.

When
the operator disconnects the service in Settings, Druks publishes the signal
for each of its connections. Revocation is a state, not a deletion. Your
subscriber can still read the connection that the signal names. Subscribe in
`subscribers.py`:

```python
from druks.signals import subscribe


@subscribe("oauth.connected", provider="acme")
async def adopt_sign_in(
    provider: str, connection_id: str, account_id: str, reconsent: bool
) -> None:
    if not reconsent:
        WatchedAccount.adopt(connection_id, account_id)


@subscribe("oauth.disconnected", provider="acme")
async def drop_sign_in(provider: str, connection_id: str, account_id: str) -> None:
    WatchedAccount.drop(connection_id)
```

The user sees and revokes everything in Settings: every connection that they
hold, across services. Settings shows revoked connections as history. A
replacement of a service's client credentials revokes its connections. A new
client can never refresh the old client's tokens. The consents stay on record.

`get_access_token` serves the access token that the vault keeps, until five
minutes before the token expires. It lets only one refresher run for each
connection and scope set. This limit is necessary. Two refreshes at the same
time can make the provider revoke the whole connection.

It raises
`OauthRefreshError` when the refresh fails. Then ask the user to reconnect. The
call holds no database connection while it waits or refreshes. Call it from a
step or from `prepare()`, never inside an `atomic()` block.

`get_access_token(scopes=("profile.read",))` asks the provider for a token with
fewer scopes than the grant. If the token goes to a machine that you do not trust, use it.
Pass a subset of the connection scopes.

`get_access_token(min_ttl_seconds=30 * 60)` asks for a token that stays valid
at least that long, such as one that a box holds through an agent run. The
vault serves its kept token while it has that long left. Otherwise one caller
refreshes. The others take its token. A provider whose tokens last less gives a
fresh one. The call then logs a warning.

## App settings and checks

An inner `AppSettings` class defines the settings that the dashboard edits, and
owns their cross-field coherence:

```python
from typing import Literal

from pydantic import Field

from druks.apps import App, AppSettings, Secret


class NightWatch(App):
    name = "night_watch"

    class Settings(AppSettings):
        provider: Literal["none", "acme"] = "none"
        service_token: Secret = Field(
            json_schema_extra={"section": "Acme", "visible_when": {"provider": ["acme"]}},
        )
        webhook_secret: Secret = Field(
            title="Webhook secret",
            json_schema_extra={"section": "Acme", "visible_when": {"provider": ["acme"]}},
        )

        def clean(self) -> dict[str, str]:
            if self.service_token and not self.webhook_secret:
                return {"webhook_secret": "The service token needs a webhook secret. Enter one."}
            return {}
```

Supported display shapes are scalar values, `Literal` choices, and
`SecretStr`, including optional forms. Druks rejects nested Pydantic models.
It redacts secret values and submitted validation errors. Declare a secret
field as `Secret`.

A deployment can also set a `Secret` field from a file in `DRUKS_SECRETS_DIR`
named `apps.<app>.<field>`. The file's value replaces the stored one. The form
shows the field as **Managed by configuration**. Your app needs no change to
support it. `await NightWatch.settings()` returns the file's value. See
[Secret files](configuration.md#secret-files).

A `Literal` field can give each value a label and description through
`json_schema_extra`. `choice_details` maps each value to its `label` and `help`.
Druks rejects a key outside the declared choices when it loads the declaration.
The form shows only the selected value's help and saves the original value:

```python
review: Literal["human", "automatic"] = Field(
    default="human",
    title="Review policy",
    json_schema_extra={
        "choice_details": {
            "human": {"label": "Human review", "help": "Wait for your approval."},
            "automatic": {"label": "Automatic review", "help": "Continue after the automated checks pass."},
        },
    },
)
```

A `str` field can take its choices from a live source, such as a connected
service. Declare it as `Annotated[str, Choices(source)]`, with `Choices` from
`druks.apps`. The async source returns dicts with `value` and `label` strings.
An optional `group` string puts choices under a group heading.

Druks calls each source once for each choices request and shows these fields as
searchable selects.
The select starts with an empty choice. It marks a stored or edited value that
the source no longer lists under **Unavailable choices**. When the source returns
no choices, the field stays a text box.

```python
async def list_board_choices() -> list[dict[str, str]]:
    return [
        {"value": "ops", "label": "Operations board", "group": "Boards"},
        {"value": "dev", "label": "Development board", "group": "Boards"},
    ]


class Settings(AppSettings):
    board: Annotated[str, Choices(list_board_choices)] = Field(default="ops", title="Board")
```

An unset field is an empty, false `SecretStr`. Thus,
`if self.service_token:` reads its state without a guard for
`.get_secret_value()`. A multiline secret, such as a PEM private key, can use
`json_schema_extra={"multiline": True}`. The settings form shows a textarea and
keeps newlines. Storage, redaction, and write-only behavior do not change.

`section` is a plain heading that Druks renders in first-declaration order, with
unsectioned fields first. `visible_when` takes one same-model `{field: [values]}`
condition. The field shows when its controller holds one of the values. The
controller must be non-secret and unconditional. A `Literal` controller
requires declared members.

Hidden fields keep their stored values. Read the resolved model with
`await NightWatch.settings()`. The settings form runs `clean()` against the
resolved settings after the proposed edits and rejects an incoherent save.
`druks doctor` runs the same method over stored settings, so that rows from
older releases or manual database edits remain visible. Workflow settings stay
plain Pydantic `BaseModel` declarations.

An app can add precondition checks through `checks`. These checks supplement
settings validation. Return `druks.doctor.CheckResult`. Druks namespaces the
result. It converts an exception or malformed result into an error. The later
checks still run.

## Cache a provider read

A page reads again on every load and every followed event. A page that shows
what a provider holds, such as a repository's commits, thus asks the provider
each time. Cache the answer instead:

```python
history = await NightWatch.get_cached(f"commits:{repo}")
if history is None:
    history = await list_commits(repo)
    await NightWatch.set_cached(f"commits:{repo}", history, ttl=3600)
```

The cache belongs to the app. Another app cannot read or clear its values. A
value is JSON. `None` means a miss: the app never set the value, its TTL
ended, or the app cleared it. Every value expires. Pass `ttl` in seconds.

When the app changes what the provider holds, such as after a push, call
`await NightWatch.clear_cached(f"commits:{repo}")`. Let the TTL cover a change
that happens elsewhere.

Cache only what the provider can supply again. The cache is Redis. A value can
disappear before its TTL. Keep a record that the app owns in the database.

## Test an app

The Druks installation registers its pytest plugin. An app can request the
fixtures directly without a `conftest.py` or `pytest_plugins` declaration:

| Fixture | Contract |
| --- | --- |
| `druks_db` | A SQLAlchemy `AsyncSession` bound to a per-test transaction. Commits become savepoints. Teardown rolls the outer transaction back. |
| `druks_client` | An authenticated `httpx.AsyncClient` with installed apps mounted. It shares the connection of `druks_db`. A request starts with no session bound, as in production. |
| `druks_redis` | The test Redis database, flushed before the test. |
| `druks_without_dispatch` | Workflow starts and run-phase writes become no-ops. Use it in tests that start no durable engine. |
| `druks_without_remote_config` | Every `.druks` namespace lookup misses. Prompts then resolve to bundled templates. Config resolves to its declared defaults. |

The fixtures are not autouse. A test that requests `druks_client` also gets
`druks_db`. A test accesses Redis only if it requests `druks_redis`.

`druks check-app` loads one installed app and checks its subjects, pages,
operations, and routers. It needs no database and no configured install. An
app's CI can run it. It exits non-zero on the first contract that the app
does not obey:

```bash
druks check-app field_notes
```

Run a workflow's body against a subject with no durable engine. The run has no
checkpoints, no lifecycle events, and no retries:

```python
from druks.testing import run_workflow

summary = await run_workflow(RecordHeartbeat, subject=None, source="night_watch")
```

`@step` calls inside a `run_multistep` body still need the real engine.

Seed platform-owned run and agent-call rows with plain functions:

```python
from druks.testing import seed_call, seed_run

run = await seed_run(
    druks_db,
    kind=Sweep.kind,
    subject=repository,
    state="running",
)
call = await seed_call(
    druks_db,
    run,
    NightWatch.write_report.id,
    status="running",
)
```

`seed_run` writes both the run row and its DBOS workflow status. That status
determines `Run.state`. `seed_run` requires `kind`. If you seed
`state="parked"`, pass `input_gate`. `seed_call` accepts an instruction's string
ID.

`make_settings(tmp_path, **overrides)` builds isolated Druks settings.
`configure_app_for_test(settings=..., authenticated=False)` returns the mounted
app if a test needs its own client or an unauthenticated request path.
`druks_client` covers the normal authenticated case.

The fixtures never read the runtime's settings. They read
`DRUKS_TEST_DATABASE_URL` and `DRUKS_TEST_REDIS_URL`. The defaults are a local
`druks_test` database and Redis index 15. The fixtures point the code under test
to the same pair. Thus, a run cannot use the values in `DRUKS_DATABASE_URL` or
`DRUKS_REDIS_URL`.

Create the database one time with `createdb druks_test`. The
development Compose project already creates it.

On that database, the plugin creates `citext` and imports installed app models.
It runs SQLAlchemy `create_all`. It builds the DBOS system tables through DBOS
database migrations. It does not reset or drop a schema. It rolls back each test
write through `druks_db`. `druks_redis` runs `FLUSHDB` on the test index.

## Declare pages

An app declares its screens in Python and ships no JavaScript. Pages are in
`pages.py`:

```python
from druks import ui


@ui.page("/")
async def reports():
    return ui.Page("Reports", description="Every sweep this install ran.")


@ui.page("/peers/{peer_id}")
async def peer(peer_id: int):
    return ui.Page(title=f"Peer {peer_id}")


@peer.child("/history")
async def peer_history(peer_id: int):
    return ui.Page(title=f"Peer {peer_id} history")
```

`@page` declares a top-level page. `@parent.child` declares a page under it,
one level deep. A page function takes one parameter for each parameter of its
route. A child then inherits its parent's parameters. It needs no return
annotation.

At most one page declares `/`. That page is the one that the app opens on. A
static child renders as a tab on its parent. A parameterized child is a detail
page that a `Link` opens.

The page name is the function name. The page label is that name with its
underscores as spaces. `label=` overrides it.

`App.pages()` enumerates the pages in route-match order: literal segments
before parameters, at every depth. Declaration order never decides a match.

`navigation` names the pages that the appbar shows as subnav tabs, in order.
Each one must be a static top-level page. The tab shows the page's label. An
app then never writes a label twice:

```python
class NightWatch(App):
    name = "night_watch"
    navigation = ["reports"]
```

Druks checks the whole table at boot. Each of these makes the load fail, with
the app name and the exact cause:

- Two landing pages
- Two pages with the same name
- A nested child
- Two routes that match the same request
- A signature that does not match its route
- A navigation entry that is not a static top-level page.

### What pages are for

Pages show data and act on the app's own records. A page or an app setting
does not rebuild the screens or the settings of a service that the app
connects to. The service's own interface does this better.

To change the setup of a connected service, a person asks Druks in chat. The
app's [Bot](#answer-whatsapp-with-a-bot) makes the change through the
service's API. Its tools are the app's routes tagged `bot`.

These belong in Druks:

- The service's card, with its URL and credentials.
- An action on the app's own record, for example a retry of one delivery.

This does not belong in Druks:

- A page or an app setting that edits the service's configuration, for
  example its taxes, users, or templates.

### A page function is a pure read

Druks reruns a page function on the first load, on an event, on a reconnect, on a
manual refresh, and on a retry. Druks guarantees neither the call count nor the
call order. These are the rules.

A page function can:

- Read Druks state
- Read the app's own data
- Read a projection
- Read a read-only external source.

A page function must not:

- Write data
- Start or enqueue work
- Publish an event
- Answer a gate
- Cause an external effect
- Depend on mutable process state.

Write the function so that a second call costs nothing. Do every write in an
operation that the operator triggers.

### Watch a subject

A page, or a named region inside it, declares the subject that it `follows`. Druks
streams that subject through the read side that every app already gets. The
shell rereads the page on each snapshot:

```python
@ui.page("/notes/{note_id}")
async def note(note_id: int):
    found = await Note.get(id=note_id)
    return ui.Page(
        title=f"Note {note_id}",
        blocks=[
            ui.Section(
                title="Your decision",
                name="decision",
                follows=found,
                blocks=[ui.GateControls(found)],
            )
        ],
    )
```

The shell replaces the named region and leaves the rest of the page alone.
Scroll position, focus, and half-filled inputs outside the region then survive.
A region that follows a subject must have a name. That is how the shell finds
it.

`GateControls` takes the subject. The shell shows nothing while the subject
waits on nothing. Otherwise it shows the ask, its options, its context, and its
artifact. It sends the operator's answer. A `GateControls` block must be
inside something that follows a subject. Otherwise an answered gate stays on
screen.

### Let an operator act

A page acts through the app's own routes. Give the route an `operation_id`.
Name that id from an `Action`:

```python
# routes.py
@router.post("/notes", status_code=201, operation_id="write_note")
async def write_note(body: Annotated[str, Body(embed=True)]) -> dict[str, int]:
    note = await Note.create(body=body)
    await Summarize.dispatch(note=note)
    return {"id": note.id}
```

```python
# pages.py
@ui.page("/notes/new")
async def new_note():
    return ui.Page(
        "Write a note",
        blocks=[
            ui.Form(
                title="New note",
                fields=[ui.TextAreaField(name="body", label="Note", is_required=True, rows=3)],
                action=ui.Action(
                    label="Save",
                    operation="write_note",
                    tone="primary",
                    link=ui.Link("Notes", page="notes"),
                ),
            )
        ],
    )
```

The shell resolves the operation to its method and URL. The author then writes
no URL. It sends the action's `arguments` and the field values as one object.
It fills the route's path parameters from that object and posts the rest as the
body. Your route keeps the identity gate. The shell shows the Pydantic and FastAPI
errors of the route on the fields that they belong to.

`confirm` asks the operator before the action runs. When the operation answers,
the action does what it declared. `refresh` reads the page or the region again.
`link` navigates. A `tone` of `danger` marks the action as dangerous on screen.

A form that needs a token uses a `ui.SecretField`:

```python
ui.SecretField(name="token", label="Access token", help_text="From your account settings.")
```

Druks masks it. The password managers of the browser do not get it. After a
successful submit, the field is empty. A refusal shows a fixed line, never the
server's words, because a validation message can quote the token.

The masking protects the screen. Your operation receives the token in plain
text and owns it from there. Keep one secret for each record in an
`EncryptedJsonField` column from `druks.db.fields`. A token that the whole app
shares belongs in `AppSettings` as a `Secret`, where Druks encrypts it and
never returns it to the browser.

Two routes of one app cannot share an `operation_id`. An action that names an
operation that the app does not declare, or a GET route, makes the page read fail. A
GET is a read. An action fills only path parameters and a JSON body. A route
that needs a query parameter answers 422 when the action runs.

### Not in V1

V1 has no `Tabs` block, no accordion, no general modal block, no inline reveal
form, and no general client-state API. Static child pages give tabs. The URL
holds the current one. Use `TableRow.detail` for expandable row text.

`MoneyValue`, `PercentValue`, `DurationValue`, and date and time input fields
have agreed names. Druks adds each one when an app needs it. Ask for one. Do not
build a replacement.

The [Druks UI contract](druks-ui.md) holds the block, value, and field catalog,
actions, and liveness.

## Frontends

An installed app is visible in the dashboard without a custom UI. The shell
reads the installed roster from `/api/apps`. It gives each app an entry in the
Apps sidebar and generic pages. Each subject type gets a board. Each subject
gets a page with its timeline, transcripts, and gate controls. The subject
summary fields form the board row.

No additional declaration is necessary. The shell derives the sidebar label
from `name` (underscores become spaces).

An app that needs full control of its interface ships a frontend instead. A
frontend is the exception, not the ordinary path. Its pages are its own
JavaScript. It declares its own tabs there and leaves `App.navigation` empty.

The scaffold writes no JavaScript and no `dist/`. If the block catalog cannot show
what your app must show, add one. Otherwise add none.

The frontend is an ES module that the shell mounts inside its own document,
below the chrome. The scaffold writes none. An app that chooses this approach creates
`druks_night_watch/dist/` itself. It sends the output of its frontend build there.
The contract uses `shellApi: 1`:

- **Entry module:** `entry.js` exports `shellApi = 1` and `mount(el, ctx)`. The
  function renders into `el` and returns a dispose function. An entry with no
  `mount`, or with a version mismatch, renders a visible error panel in the shell.
- **Context:** `ctx` carries `apiBase` (`/api/<name>`), `navigate(path)` for
  shell-side navigation, `theme.accent`, and `markdown(source)`.
  `markdown(source)` is the shell's own markdown renderer. An app then does not
  bundle one. The app renders in the shell's document. The shell's CSS
  variables then cascade into it. The shell re-broadcasts every location change
  as a `popstate` event while the app stays mounted.
- When `dist/style.css` is present, the shell loads it while it mounts the app.

Build the bundle in Vite library mode. Keep `react`, `react-dom`,
`react-dom/client`, and `react/jsx-runtime` external. The shell import map
resolves them to its copy. Thus, one React instance serves the document.
Bundle other dependencies as usual. To route, read `location.pathname` under
`/<name>/`.

The bundled Druks SPA also has a shared React app registry. To join this shell,
compile the app UI module into the dashboard image. An installed Python wheel
cannot change a built JavaScript bundle. See the
[frontend guide](https://github.com/getdruks/druks/blob/main/frontend/README.md)
for that in-repository path.

## Stable author imports

Import from concern namespaces, not from `druks.durable` or internal modules:

| Namespace | Public names |
| --- | --- |
| `druks.accounts` | `Account`, `AccountKind`, `current_account`, `current_account_id` |
| `druks.apps` | `App`, `AppSettings`, `Choices`, `Secret` |
| `druks.services` | `Service`, `Connection`, `auth`, `Resource`, `ResourceError`, `ServiceConnectError`, `ServiceNotConnectedError`, `OauthClient`, `OauthExchangeError`, `OauthRefreshError` |
| `druks.browser` | `BrowserClient`, `BrowserCommandError`, `BrowserSession`, `BrowserSessionSignedOutError`, `BrowserSessionStatus` |
| `druks.chat` | `Bot`, `BotUser` |
| `druks.workflows` | `Workflow`, `Instruction`, `InstructionOutput`, `Gate`, `YesNo`, `GateTimeout`, `WaitTimeout`, `WaitFailed`, `step`, `task`, `Journal`, `Subject`, `SubjectSummary`, `SubjectStatus`, `OperatorReply`, `RunResponse`, `AgentCall`, `AgentCallResponse`, `AgentCallStatus`, `WorkflowEvent`, `WorkflowError`, `FatalError` |
| `druks.sandbox` | `Sandbox`, `SandboxMcpServer`, `SandboxSecret` |
| `druks.workspaces` | `Workspace`, `RepoWorkspace` |
| `druks.db` | `Model`, `StoredSubject`, `atomic`, `db_session` |
| `druks.db.fields` | `EncryptedJsonField`, `EncryptedTextField`, `Secret`, `SecretsMapping` |
| `druks.exceptions` | `DruksError`, `ObjectNotFound` |
| `druks.schemas` | `Schema` |
| `druks.ui` | `Action`, `Block`, `Callout`, `Card`, `Cards`, `Chart`, `ChartSeries`, `CheckboxField`, `Columns`, `ControlsValue`, `Divider`, `EmptyState`, `Fact`, `Facts`, `Field`, `FileSummary`, `Files`, `Follows`, `Form`, `GateControls`, `Image`, `ImageGallery`, `Link`, `List`, `Markdown`, `Metric`, `Metrics`, `MultiSelectField`, `MultiUploadField`, `NumberField`, `NumberValue`, `Option`, `Page`, `Progress`, `ProgressStep`, `Quote`, `RadioField`, `Section`, `SecretField`, `SelectField`, `Stack`, `StatusValue`, `SubjectStatus`, `Table`, `TableColumn`, `TableRow`, `Text`, `TextAreaField`, `TextField`, `TextValue`, `TimeValue`, `Timeline`, `TimelineItem`, `UploadField`, `Value`, `page` |
| `druks.signals` | `subscribe` |
| `druks.events` | `Event` |
| `druks.files` | `File`, `FileField` |
| `druks.prompts` | `render_prompt` |
| `druks.webhooks` | `Webhook`, `InvalidWebhookError`, `verify_hmac_sha256` |
| `druks.testing` | `run_workflow`, `seed_run`, `seed_call`, `init_db`, `asgi_client`, and the fixtures that the plugin registers |

The root `druks` package deliberately exports only its version.
