import asyncio
import dataclasses
import hashlib
import inspect
import json
import random
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial, wraps
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Self,
    TypeVar,
    get_args,
    get_type_hints,
    overload,
)

import pydantic
from croniter import croniter
from dbos import (
    DBOS,
    EnqueueOptions,
    SetEnqueueOptions,
    SetWorkflowAttributes,
    SetWorkflowID,
    StepOptions,
)
from dbos._dbos import _get_or_create_dbos_registry
from dbos._error import (
    DBOSAwaitedWorkflowCancelledError,
    DBOSWorkflowCancelledError,
)
from drukbox_sdk import Secret
from opentelemetry import trace
from pydantic import BaseModel, Field, create_model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from druks.accounts.context import current_account_id, current_actor, current_conversation_id
from druks.accounts.models import Account
from druks.agents.models import Agent
from druks.apps.loader import resolve_app
from druks.apps.settings import (
    coerce_setting_value,
    field_live_choices,
    validate_setting_override,
    validate_settings_declaration,
)
from druks.browser.agent_browsers import AgentBrowser
from druks.browser.enums import BrowserClient
from druks.chat.bots.service import ask_for_answer, request_approval
from druks.chat.models import Conversation
from druks.chat.service import (
    deliver,
    report_failure,
    report_quota_resume,
    report_quota_wait,
    report_result,
)
from druks.db import db_session
from druks.durable.activity import set_run_phase
from druks.durable.constants import (
    NOTIFICATIONS_QUEUE,
    REPLACED_FAILURE,
    RUN_QUEUE,
    TASK_QUEUE,
)
from druks.durable.datastructures import Subject
from druks.durable.engine import (
    _step_engine,
    autocommit_session,
    bound_session,
    enqueue_in_transaction,
    get_dbos_client,
    register_schedule,
    register_task_schedule,
    step_session,
)
from druks.durable.enums import AgentCallStatus, RunState, WorkflowEvent
from druks.durable.exceptions import (
    FatalError,
    GateTimeout,
    SubjectlessGate,
    WaitFailed,
    WaitTimeout,
    WorkflowError,
)
from druks.durable.models import AgentCall, Artifact, Run
from druks.durable.schemas import (
    AgentCallResponse,
    RunResponse,
    SubjectStatus,
    SubjectSummary,
)
from druks.events.models import Event
from druks.exceptions import ObjectNotFound, PermissionDenied
from druks.files.datastructures import File
from druks.files.exceptions import FileUnavailableError
from druks.files.models import FileRecord
from druks.harnesses.artifacts import normalize_token_usage
from druks.harnesses.config import AgentConfig, get_config
from druks.harnesses.exceptions import (
    HarnessError,
    HarnessInvalidOutputError,
    HarnessSandboxProvisioningError,
    Retry,
)
from druks.models import StoredSubject, snake_name
from druks.notifications.constants import RUN_FAILED_MESSAGE
from druks.notifications.models import Notification
from druks.notifications.outbox import send_notification
from druks.notifications.services import create_park_notification
from druks.prompts.resolver import get_template
from druks.registry import get_app, get_app_by_module, get_app_name, get_instruction
from druks.sandbox import gate as sandbox_gate
from druks.sandbox.client import provisioning_key, sandbox_client
from druks.sandbox.constants import WORKFLOW_HOST_LEASE_SECONDS
from druks.sandbox.datastructures import Sandbox
from druks.sandbox.exceptions import HostGone
from druks.sandbox.models import SandboxIdentity, SecretRef
from druks.sandbox.templates import get_template_id
from druks.secrets.enums import SecretKind
from druks.secrets.models import VaultSecret
from druks.settings import get_settings
from druks.signals import publish
from druks.skills.models import Skill
from druks.teams.models import Team
from druks.tracing import record_gate_wait
from druks.ui.blocks import Block, Stack
from druks.usage.models import UsageScrape
from druks.user_settings.models import SettingsOverride
from druks.workspaces import Workspace

# druks.workflows is the author door for workflow authoring: Workflow, Gate,
# step, task, and the author-facing read contracts. The engine
# itself (druks.durable) stays internal.
__all__ = [
    "AgentCall",
    "AgentCallResponse",
    "AgentCallStatus",
    "FatalError",
    "Gate",
    "GateTimeout",
    "Instruction",
    "InstructionOutput",
    "Journal",
    "OperatorReply",
    "RunResponse",
    "Subject",
    "SubjectStatus",
    "SubjectSummary",
    "WaitFailed",
    "WaitTimeout",
    "Workflow",
    "WorkflowError",
    "WorkflowEvent",
    "YesNo",
    "step",
    "task",
]

if TYPE_CHECKING:
    from druks.browser.sessions import BrowserSession
    from druks.harnesses.base import Harness
    from druks.sandbox.datastructures import AgentResult
    from druks.sandbox.host import Host
    from druks.services.base import ServiceField

# A human gate can park for days; a long recv TTL still caps zombie parks.
GATE_TTL_SECONDS = 14 * 24 * 60 * 60

# The decision verbs an in-app review offers. Framework-owned: an app's
# agent supplies the plan and questions (content), but the verbs are ours — so a
# resume can only carry an action we defined, the line the resume endpoint checks.
_ReviewAction = Literal["approve", "request_changes"]

T = TypeVar("T")
GateReply = TypeVar("GateReply", bound="Gate")

# The running workflow instance, so a Gate's on_wait() can reach its app's
# side-effects (set draft, request review, …) when the gate parks. No default:
# Gate.wait() only runs inside a workflow, so it's always set there.
current_workflow: ContextVar["Workflow"] = ContextVar("current_workflow")
# True while a @step body runs. An agent run inside one is already memoized by that
# step, so it skips wrapping itself; outside, it wraps itself in its own step.
_in_step: ContextVar[bool] = ContextVar("_in_step", default=False)

_QUOTA_FALLBACK_WAIT_SECONDS = 30 * 60
_QUOTA_MAX_WAIT_SECONDS = 6 * 60 * 60
_REAP_BEFORE_WAIT_SECONDS = 120
# The topic a run waiting out a quota window listens on for a changed provider or account.
_QUOTA_TOPIC = "agents.quota"

_in_after_agent: ContextVar[bool] = ContextVar("_in_after_agent", default=False)

# Reserved so _entry's arity and old checkpoints stay untouched; a body
# parameter may not claim either.
_ACCOUNT_INPUT_KEY = "__account_id__"
_CONVERSATION_INPUT_KEY = "__conversation_id__"


def _resolve_body_method(cls: type["Workflow"]) -> str:
    # Stamps run() @step here, before _wrap_steps runs, so it picks it up.
    has_run = "run" in cls.__dict__
    has_multistep = "run_multistep" in cls.__dict__
    if has_run and has_multistep:
        raise WorkflowError(
            f"{cls.__name__} defines both run() and run_multistep() — exactly "
            "one is allowed: run() for a single durable operation, "
            "run_multistep() for orchestration across steps/gates."
        )
    if has_run:
        method = cls.__dict__["run"]
        if getattr(method, "_durable_step", False):
            raise WorkflowError(
                f"{cls.__name__}.run() doesn't take @step — the whole call is "
                "already the durable step. Remove the decorator."
            )
        method._durable_step = True
        method._step_name = None
        method._step_retries = 0
        return "run"
    if has_multistep:
        method = cls.__dict__["run_multistep"]
        if getattr(method, "_durable_step", False):
            raise WorkflowError(
                f"{cls.__name__}.run_multistep() must not be @step — step the "
                "individual operations it calls instead, or use run() if the "
                "whole workflow is one operation."
            )
        return "run_multistep"
    raise WorkflowError(
        f"{cls.__name__} must define run() (a single durable operation) or "
        "run_multistep() (orchestration across multiple steps/gates)."
    )


class _DeclaredSubject:
    """``subject = WorkItem`` on a workflow: the kind of thing its runs are about, read
    off the workflow, and the particular one a run is about, read off that run."""

    def __init__(self, subject_class: "type[Subject] | type[StoredSubject] | None") -> None:
        self.subject_class = subject_class

    def __get__(self, run: "Workflow | None", owner: type) -> Any:
        if run:
            # Live, not a snapshot taken at dispatch: a long-parked run resumes against
            # whatever the declared class says then, and finds nothing if it went away.
            # Awaitable either way, so ``await self.subject`` is the one shape.
            async def resolve() -> Any:
                if "subject" in run.__dict__:
                    return run.__dict__["subject"]
                if run._subject:
                    async with bound_session():
                        return await self.subject_class.get_or_none(id=run._subject["id"])

            return resolve()
        return self.subject_class

    def __set__(self, run: "Workflow", value: Any) -> None:
        # A test hands the run its subject directly; ``await self.subject``
        # then resolves to it without a read.
        run.__dict__["subject"] = value


def _declare_subject(cls: type["Workflow"]) -> None:
    # The author writes a plain class attribute; it becomes the descriptor above.
    # Presence in ``cls.__dict__``, not the value: an explicit ``subject = None`` is a
    # declaration of nothing, which the error below names rather than passing over.
    if "subject" not in cls.__dict__:
        return
    declared = cls.__dict__["subject"]
    if not (isinstance(declared, type) and issubclass(declared, Subject | StoredSubject)):
        raise WorkflowError(
            f"{cls.__name__}.subject must be a Subject or StoredSubject subclass — a run "
            f"is about a kind of thing, not {declared!r}. A workflow about nothing "
            "declares no subject at all."
        )
    cls.subject = _DeclaredSubject(declared)


def _input_model_from_signature(
    function: Callable, *, owner: str, model_name: str
) -> type[BaseModel] | None:
    # A body's input IS its signature: plain annotated parameters, Python's
    # native way to declare inputs. The synthesized model is the wire contract —
    # the caller validates kwargs against it and dumps to JSON (input crosses a
    # JSONB row and a DBOS checkpoint; never a live or pickled object), and the
    # entry re-validates. A parameter without a default is required; a
    # cron-scheduled body must default every parameter.
    parameters = [p for name, p in inspect.signature(function).parameters.items() if name != "self"]
    if not parameters:
        return
    hints = get_type_hints(function)
    fields: dict[str, Any] = {}
    for p in parameters:
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise WorkflowError(f"{owner} cannot take *args/**kwargs")
        if p.name not in hints:
            raise WorkflowError(f"{owner} parameter {p.name!r} needs a type")
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[p.name] = (hints[p.name], default)
    return create_model(model_name, **fields)


class Journal:
    """The run's working memory. Druks adds each body-level agent output and
    gate reply; a body adds its own derived values; subclass it for named
    projections and declare yours via ``journal_class``."""

    def __init__(self) -> None:
        self._entries: list[Any] = []

    def add(self, entry: Any) -> None:
        self._entries.append(entry)

    def filter(self, contract: type[T], *, after: Any = None, **filters: Any) -> list[T]:
        # ``after`` anchors by identity: only entries recorded after that exact entry.
        entries = self._entries
        if after:
            if positions := [i for i, entry in enumerate(entries) if entry is after]:
                entries = entries[positions[0] + 1 :]
            else:
                raise WorkflowError("after= must be an entry of this journal")
        return [
            entry
            for entry in entries
            if isinstance(entry, contract)
            and all(getattr(entry, name) == expected for name, expected in filters.items())
        ]

    def latest(self, contract: type[T], **filters: Any) -> T | None:
        with suppress(IndexError):
            return self.filter(contract, **filters)[-1]
        return


class Gate(BaseModel):
    """A typed human-in-the-loop gate. Subclass per park point; ``name`` pins the
    gate's durable identity — the recv channel and the parked run's ``gate`` on
    the read side — and the fields are the reply's schema. `wait()` parks the
    running workflow until `answer()` resolves it (or the TTL lapses) and
    returns the validated reply — both ends of the channel hang off the class."""

    name: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("name"):
            raise WorkflowError(
                f"{cls.__name__} must pin ``name`` — it is the gate's durable identity "
                "(the recv channel and the parked run's gate), so it never rides the "
                "class name."
            )

    @classmethod
    async def on_wait(cls, workflow: "Workflow") -> None:
        # Runs when the gate parks, before the run suspends — override to make a
        # human aware there's something to answer (set PR draft, request review,
        # notify Slack, …). Default: nothing.
        return

    @classmethod
    async def answer(
        cls,
        subject: Subject | StoredSubject,
        *,
        workflow: type["Workflow"],
        **reply: Any,
    ) -> None:
        workflow._validate_subject(subject)
        if parked := await Run.get_latest_for_subject(
            db_session(),
            subject.subject_type,
            str(subject.id),
            kind=workflow.kind,
            gate=cls.name,
        ):
            await parked.resume(**reply)
            return
        raise WorkflowError(
            f"{workflow.__name__} has no run for {subject.subject_type}:{subject.id} "
            f"parked on gate {cls.name!r}"
        )

    @classmethod
    async def wait(
        cls, *, input_request: dict[str, Any] | None = None, ttl_seconds: float = GATE_TTL_SECONDS
    ) -> Self:
        # Suspend the running workflow until its gate is answered. A gate is a
        # run-level state — the read surfaces "needs you" straight off the parked run.
        # ``input_request`` is the plain-dict ask (at least a ``label`` and
        # ``presentation``), stored on the run beside ``input_gate`` and cleared on
        # resume — so an app declares the ask here, beside on_wait, not at read time.
        workflow = current_workflow.get()
        if not workflow._subject and cls.on_wait.__func__ is Gate.on_wait.__func__:
            # No subject means no feed surface; if on_wait wasn't overridden
            # either, nobody would ever see this park — fail loudly instead
            # of parking invisibly for the whole TTL.
            raise SubjectlessGate(cls.name)

        async def _on_wait() -> None:
            # on_wait is real IO (it tells a human), so it gets its own checkpointed
            # step with a DB session.
            async with step_session():
                await cls.on_wait(workflow)

        await DBOS.run_step_async(StepOptions(name=f"{cls.name}._on_wait"), _on_wait)
        reply = await _park(workflow, cls, input_request, ttl_seconds)
        workflow.journal.add(reply)
        return reply


class OperatorReply(Gate):
    """The operator's in-app review decision. Answers and note are content for
    the next agent prompt, never control flow."""

    # One gate serves every in-app review — a run parks on one at a time.
    name = "review"
    action: _ReviewAction
    answers: dict[str, str] = Field(default_factory=dict)
    note: str = ""


class YesNo(Gate):
    """A built-in yes or no decision, with an optional note."""

    name = "yes_no"
    action: Literal["yes", "no"]
    note: str = ""


async def _park(
    workflow: "Workflow",
    gate: type[GateReply],
    input_request: dict[str, Any] | None,
    ttl_seconds: float,
) -> GateReply:
    # Shared park core: a park lasts days, so reap the warm VM, then suspend on the
    # gate's channel until Run.resume answers it.
    await workflow._reap_run()
    if input_request and input_request.get("presentation") == "in_app":
        # The dashboard shows a note box only for a gate whose reply fields
        # include note.
        input_request = {"questions": [], **input_request, "reply_fields": list(gate.model_fields)}
        context = input_request.get("context", "")
        if stack := Stack.from_context(context):
            # An approval writes the blocks as text. A part with no text form fails here,
            # before the run parks.
            stack.to_markdown()
            input_request["context"] = [
                block.model_dump(mode="json", by_alias=True) for block in stack.blocks
            ]
        if action := gate.model_fields.get("action"):
            # A request for changes needs a note to guide the next pass, unless the ask
            # carries its own context. Every place that takes an answer reads this.
            is_guided = bool(stack or context and context.strip())
            input_request["controls"] = [
                {"id": control, "note_required": control == "request_changes" and not is_guided}
                for control in input_request.get("controls", get_args(action.annotation))
            ]
    requested_at = datetime.now(UTC)
    await _emit_run_event(
        workflow.workflow_id,
        RunState.PARKED,
        subject=workflow._subject,
        facts={
            "input_gate": gate.name,
            "input_request": input_request,
            "input_requested_at": requested_at,
            "input_expires_at": requested_at + timedelta(seconds=ttl_seconds),
        },
    )
    if workflow._subject:
        # Every subjected park runs this step, with no author opt-in.
        await _notify_account(workflow.workflow_id, workflow._subject)
    if workflow.conversation_id:
        if workflow._subject and await _notify_conversation(
            workflow.workflow_id, workflow._subject
        ):
            await _post_to_chat(workflow.workflow_id, "chat.request_approval", request_approval)
        else:
            # The step keeps its recorded name, so a parked run replays it.
            await _post_to_chat(workflow.workflow_id, "chat.ask_admin", ask_for_answer)
    payload = await DBOS.recv_async(gate.name, timeout_seconds=ttl_seconds)
    if payload is None:
        raise GateTimeout(gate.name)
    reply = gate.model_validate(payload)
    await _emit_run_event(
        workflow.workflow_id,
        RunState.RUNNING,
        subject=workflow._subject,
        facts={**_GATE_CLEARED, "answer_parked_at": Run.input_requested_at},
        result=reply,
    )
    return reply


async def _notify_account(workflow_id: str, subject: dict[str, Any]) -> None:
    # A run that a chat started asks in that chat, so the person does not get the same
    # buttons twice. The step still runs for every subjected park, so a parked run replays
    # its recorded steps in their order.
    async def _create() -> str | None:
        async with step_session() as session:
            run = await session.get(Run, workflow_id)
            if run.input_request and not run.conversation_id:
                return await create_park_notification(
                    session,
                    run,
                    subject,
                    account_id=run.account_id,
                    deep_link=_get_run_link(run.id),
                )

    notification_id = await DBOS.run_step_async(
        StepOptions(name="notifications.gate_park", **_IO_RETRIES), _create
    )
    if notification_id:
        # The step memoized the row (one per parked round); this body-level enqueue
        # is DBOS's deterministic child-start, so a replayed park never double-sends.
        await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)


async def _notify_conversation(workflow_id: str, subject: dict[str, Any]) -> bool:
    # A chat whose channel shows buttons gets an in-app ask's buttons in its thread.
    async def _create() -> str | None:
        async with step_session() as session:
            run = await session.get(Run, workflow_id)
            if run.conversation_id:
                conversation = await session.get(Conversation, run.conversation_id)
                channel = conversation.channel
                ask = run.input_request or {}
                if channel and channel.has_buttons and ask.get("presentation") == "in_app":
                    return await create_park_notification(
                        session,
                        run,
                        subject,
                        conversation_id=conversation.id,
                        deep_link=_get_run_link(run.id),
                    )

    notification_id = await DBOS.run_step_async(
        StepOptions(name="notifications.chat_park", **_IO_RETRIES), _create
    )
    if notification_id:
        await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)
    return bool(notification_id)


async def _notify_failure(workflow_id: str, subject: dict[str, Any] | None, failure: str) -> None:
    notification_id = await DBOS.run_step_async(
        StepOptions(name="notifications.run_failed", **_IO_RETRIES),
        _create_failure_notification,
        workflow_id,
        subject,
        failure,
    )
    await DBOS.enqueue_workflow_async(NOTIFICATIONS_QUEUE, send_notification, notification_id)


async def _create_failure_notification(
    workflow_id: str, subject: dict[str, Any] | None, failure: str
) -> str:
    async with step_session() as session:
        run = await session.get(Run, workflow_id)
        notification = await Notification.create(
            session,
            account_id=run.account_id,
            reason="run.failed",
            body=RUN_FAILED_MESSAGE.format(run=run.subject_key or run.kind, failure=failure),
            subject=subject,
            run_id=run.id,
            deep_link=_get_run_link(run.id),
        )
        return notification.id


def _get_run_link(run_id: str) -> str | None:
    # An installation with no endpoint has no address for the link.
    endpoint = get_settings().urls.endpoint.rstrip("/")
    return f"{endpoint}/runs/{run_id}" if endpoint else None


async def _post_to_chat(workflow_id: str, name: str, post: Callable) -> None:
    # A run that a chat's tool call started talks back through chat. When post() returns
    # the conversation, the conversation's agent takes its message from there.
    async def _post() -> str | None:
        async with step_session() as session:
            run = await session.get(Run, workflow_id)
            # Deleting an agent deletes its conversations.
            if run.conversation_id:
                return await post(session, run)

    conversation_id = await DBOS.run_step_async(StepOptions(name=name, **_IO_RETRIES), _post)
    if conversation_id:
        # Body level, like the notification enqueue: a replay never starts it twice.
        await DBOS.start_workflow_async(deliver, conversation_id)


def step(method: Callable | None = None, *, name: str | None = None, retries: int = 0) -> Callable:
    """Mark a ``run_multistep()`` helper method as a durable, replay-safe step —
    its body runs once and its return is memoized for recovery. `name=` pins the
    durable step name independent of the method name. `retries=` sets retries
    after the first attempt. A leading `_` is only Python privacy."""

    def stamp(m: Callable) -> Callable:
        m._durable_step = True  # type: ignore[attr-defined]
        m._step_name = name  # type: ignore[attr-defined]
        m._step_retries = retries  # type: ignore[attr-defined]
        return m

    return stamp(method) if method else stamp


class _Task:
    def __init__(
        self,
        function: Callable[..., Awaitable[Any]],
        *,
        every: str | None,
        retries: int,
    ) -> None:
        self._function = function
        self.module = function.__module__
        try:
            app = get_app_name(function.__module__)
        except LookupError:
            raise WorkflowError(
                f"{function.__module__} declares task {function.__name__} outside every "
                "app package. Declare it in a module of the app's package, and "
                "import that module after the App class."
            ) from None
        self.name = f"{app}.{function.__name__}"
        self._retries = retries
        self._input_model = _input_model_from_signature(
            function, owner=f"task {self.name}", model_name=f"{function.__name__}_input"
        )
        self._scheduled_entry: Callable[..., Any] | None = None

        # DBOS only warns on a duplicate durable name and lets the last
        # registration win — enqueues would silently run the other body.
        if self.name in _get_or_create_dbos_registry().workflow_info_map:
            raise WorkflowError(
                f"task {self.name} shares its durable name with a registered workflow "
                "or task — two capabilities can't share a durable identity; rename one"
            )
        if every and self._input_model:
            required = [
                name
                for name, field in self._input_model.model_fields.items()
                if field.is_required()
            ]
            if required:
                raise WorkflowError(
                    f"task {self.name} requires {required}, but a scheduled task fires "
                    "with no arguments — a scheduled task must be nullary"
                )

        @DBOS.workflow(name=self.name)
        async def _entry(input: dict[str, Any]) -> None:
            await self._run(input)

        self._entry = _entry

        if every:

            @DBOS.workflow(name=f"{self.name}.scheduled")
            async def _scheduled_entry(
                _scheduled_at: datetime, _context: dict[str, Any] | None = None
            ) -> None:
                await self._run({})

            self._scheduled_entry = _scheduled_entry
            register_task_schedule(f"{self.name}.scheduled", every, _scheduled_entry)

    async def enqueue(self, **input: Any) -> None:
        if _in_step.get():
            raise WorkflowError(
                "enqueue() cannot run inside a @step — a retried step would enqueue the task again"
            )
        wire: dict[str, Any] = {}
        if self._input_model:
            wire = self._input_model.model_validate(input).model_dump(mode="json")
        elif input:
            raise WorkflowError(f"task {self.name} takes no input")
        if DBOS.workflow_id:
            # Inside a workflow, DBOS records the enqueue as the workflow's child, so a
            # replay does not enqueue the task again.
            await DBOS.enqueue_workflow_async(TASK_QUEUE, self._entry, wire)
        else:
            # Elsewhere the task enters the queue in the caller's transaction, so a
            # rollback enqueues nothing.
            options: EnqueueOptions = {
                "workflow_name": self.name,
                "queue_name": TASK_QUEUE,
                "workflow_id": str(uuid7()),
            }
            async with bound_session() as session:
                await enqueue_in_transaction(session, options, wire)

    async def _run(self, input: dict[str, Any]) -> None:
        kwargs: dict[str, Any] = {}
        if self._input_model:
            validated = self._input_model.model_validate(input)
            kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}

        async def _do() -> None:
            async with step_session():
                await self._function(**kwargs)

        await DBOS.run_step_async(
            StepOptions(
                name=self.name,
                retries_allowed=self._retries > 0,
                max_attempts=self._retries + 1,
            ),
            _do,
        )


@overload
def task(
    function: Callable[..., Awaitable[Any]],
    *,
    every: str | None = None,
    retries: int = 0,
) -> _Task: ...


@overload
def task(
    function: None = None,
    *,
    every: str | None = None,
    retries: int = 0,
) -> Callable[[Callable[..., Awaitable[Any]]], _Task]: ...


def task(
    function: Callable[..., Awaitable[Any]] | None = None,
    *,
    every: str | None = None,
    retries: int = 0,
) -> _Task | Callable[[Callable[..., Awaitable[Any]]], _Task]:
    """Make an async function a durable background task. ``every=`` adds a UTC
    cron, and ``retries=`` sets retries after the first attempt."""

    def decorate(declared: Callable[..., Awaitable[Any]]) -> _Task:
        return _Task(declared, every=every, retries=retries)

    return decorate(function) if function else decorate


# Lifecycle steps do real IO (DB, Redis, event subscribers), so a
# transient failure must not become a failed run — a lost reaction has no
# redelivery, unlike a webhook.
_IO_RETRIES: StepOptions = {"retries_allowed": True, "max_attempts": 5}


async def _emit_run_event(
    workflow_id: str,
    state: RunState,
    *,
    subject: dict[str, Any] | None,
    facts: dict[str, Any] | None = None,
    result: Any = None,
) -> None:
    # Facts (gate, ask, failure — DBOS owns state) and the event commit in one
    # memoized step; the signal publishes in a second, so a raising subscriber
    # can't roll back the record. Publish is at-least-once and can land before
    # DBOS commits the terminal status — subscribers stay idempotent and read
    # the payload, never derived Run.state. subject comes from the workflow's
    # own arguments, so a replay stamps the same routing every time.
    left_gate: tuple[str, datetime] | None = None

    async def _transition() -> dict[str, Any] | None:
        nonlocal left_gate
        async with step_session() as session:
            run = await session.get(Run, workflow_id)
            # Read before the flush: flushing the update unloads the row's
            # computed columns, and reading one back would be implicit IO.
            key = run.subject_key
            gate = run.input_gate if state == RunState.RUNNING and result else None
            # A run that leaves its question closes the question's messages. A park whose
            # step runs again already sees its own gate, so a park never closes one.
            if run.input_gate and state != RunState.PARKED:
                await run.close_park_notifications()
                left_gate = (run.input_gate, run.input_requested_at)
            if facts:
                for field, value in facts.items():
                    setattr(run, field, value)
                await session.flush()
            # Subjectless framework crons are plumbing: no feed entry. A routine
            # start records none either; its signal still publishes.
            if subject:
                payload = {"run": run.id, "kind": run.kind}
                if result or state != RunState.RUNNING:
                    payload = await _log_run_event(session, run, state, subject, key, result, gate)
                return {"kind": run.kind, "subject": subject, "payload": payload}

    transition = await DBOS.run_step_async(
        StepOptions(name=f"run.{state.value}", **_IO_RETRIES), _transition
    )
    # Only the execution that records the step traces the wait, so a replay adds none.
    if left_gate:
        record_gate_wait(workflow_id, *left_gate, is_answered=state == RunState.RUNNING)
    if not transition:
        return

    async def _propagate() -> None:
        async with step_session():
            await publish(
                WorkflowEvent.for_state(state),
                subject=transition["subject"],
                kind=transition["kind"],
                **{k: v for k, v in transition["payload"].items() if k != "kind"},
            )

    await DBOS.run_step_async(
        StepOptions(name=f"run.{state.value}:propagate", **_IO_RETRIES), _propagate
    )


async def _log_run_event(
    session: AsyncSession,
    run: Run,
    state: RunState,
    subject: dict[str, Any],
    key: str | None,
    result: Any = None,
    gate: str | None = None,
) -> dict[str, Any]:
    # One event per recorded transition — the feed's run-level granularity, read off
    # the just-written row so gate and failure ride the transition that set them.
    # The result rides the finished event so reactions read the outcome off the
    # payload instead of artifacts.
    facts: dict[str, Any] = {}
    if gate or run.input_gate:
        facts["gate"] = gate or run.input_gate
    if gate or state == RunState.PARKED:
        facts["input_requested_at"] = run.input_requested_at.isoformat()
    if state == RunState.PARKED:
        facts["input_request"] = run.input_request
    if run.failure:
        facts["failure"] = run.failure
    if run.failure_code:
        facts["failure_code"] = run.failure_code
    if isinstance(result, BaseModel):
        facts["result"] = result.model_dump(mode="json")
    elif isinstance(result, dict):
        facts["result"] = result
    await Event.emit(
        session,
        type=WorkflowEvent.for_state(state),
        subject=subject,
        key=key,
        run=run.id,
        kind=run.kind,
        facts=facts,
        app=run.app,
    )
    return {"run": run.id, "kind": run.kind, **facts}


async def _broadcast_fatal(exc: FatalError) -> None:
    # A fatal that names a topic announces itself once the run is recorded
    # failed, so a subscriber can settle state the rolled-back body couldn't.
    # Its own retrying checkpoint, like the run-lifecycle publishes.
    if not exc.broadcast_topic:
        return

    async def _fan_out() -> None:
        async with step_session():
            await publish(exc.broadcast_topic, **exc.broadcast_facts)

    await DBOS.run_step_async(StepOptions(name=exc.broadcast_topic, **_IO_RETRIES), _fan_out)


# Park sets the gate pair together; resume and a failure clear it together, so
# a terminal or resumed run never keeps a stale ask.
_GATE_CLEARED: dict[str, Any] = {"input_gate": None, "input_request": None}


async def _execute_run(
    workflow_id: str,
    kind: str,
    subject: dict[str, Any] | None,
    account_id: str,
    body: Callable,
    *,
    conversation_id: str | None = None,
) -> Any:
    # Create the row (idempotent, so a scheduled run with no start() makes it
    # here), then run the body between its running and finished/failed events.
    # Every failure re-raises so DBOS records the terminal ERROR derived state
    # reads; an operator cancel already carries its own reason and terminal
    # status, so it passes through untouched.
    async with step_session() as session:
        await Run.create_row(
            session,
            workflow_id=workflow_id,
            kind=kind,
            account_id=account_id,
            conversation_id=conversation_id,
        )
    await _emit_run_event(workflow_id, RunState.RUNNING, subject=subject)

    async def record_failed(exc: BaseException, code: str) -> None:
        # DBOS records no exception on the workflow's span.
        span = trace.get_current_span()
        span.record_exception(exc)
        if code:
            span.set_attribute("druks.failure_code", code)
        facts = {**_GATE_CLEARED, "failure": str(exc), "failure_code": code}
        await _emit_run_event(workflow_id, RunState.FAILED, subject=subject, facts=facts)
        if conversation_id:
            post = partial(report_failure, failure=str(exc))
            await _post_to_chat(workflow_id, "chat.report_failure", post)
        else:
            await _notify_failure(workflow_id, subject, str(exc))

    try:
        result = await body()
    except (DBOSAwaitedWorkflowCancelledError, DBOSWorkflowCancelledError):
        raise
    except FatalError as exc:
        await record_failed(exc, exc.code)
        await _broadcast_fatal(exc)
        raise
    except HarnessError as exc:
        await record_failed(exc, exc.code)
        raise
    except Exception as exc:
        await record_failed(exc, "")
        raise
    await _emit_run_event(workflow_id, RunState.FINISHED, subject=subject, result=result)
    if conversation_id:
        post = partial(report_result, result=result)
        await _post_to_chat(workflow_id, "chat.report_result", post)
    return result


class Workflow:
    kind: ClassVar[str] = ""
    # The app that declares this workflow — class identity, resolved from
    # the loader's package registrations at definition time and namespacing
    # ``kind``. Never supplied or stored per run.
    app: ClassVar[str] = ""
    # What this workflow's runs are about, written ``subject = WorkItem`` on the
    # subclass. None for a workflow about nothing, which says so by silence.
    subject = _DeclaredSubject(None)
    # When set to a cron string, the workflow also registers a schedule. It fires
    # dispatch() on that cadence if the class declares one (the policy starts the
    # real subject-backed run), else run() with no subject (a framework cron).
    every: ClassVar[str | None] = None
    # True holds one warm VM across the run's agent calls (released at gate parks and sleeps);
    # False gives each call a throwaway VM.
    steps_reuse_sandbox: ClassVar[bool] = False
    # The environment this workflow's agents need. None uses the platform base.
    sandbox: ClassVar[Sandbox | None] = None
    # The Workspace subclass agents run in; an app sets it (default: the bare VM).
    workspace_class: ClassVar[type[Workspace]] = Workspace
    # The Journal subclass the run keeps; an app sets it for named projections.
    journal_class: ClassVar[type[Journal]] = Journal
    # Exactly one: run() is a single operation, auto-stepped, no ceremony.
    # run_multistep() orchestrates explicit @step calls and/or gates.
    run: ClassVar[Callable]
    run_multistep: ClassVar[Callable]
    _entry: ClassVar[Callable]
    # Which of run()/run_multistep() this subclass defines, resolved once at
    # class-definition time; _run_instance() calls it by name.
    _body_method: ClassVar[str] = "run"
    # The resolved body's declared input model (None for an input-less run),
    # re-validated from the wire per instance.
    _run_input_model: ClassVar[type[BaseModel] | None] = None

    class Settings(BaseModel):
        # Operator-tunable settings; a workflow overrides this inner class to
        # declare fields, none by default.
        pass

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        try:
            app = get_app_by_module(cls.__module__)
        except LookupError:
            raise WorkflowError(
                f"{cls.__module__} declares workflow {cls.__name__} outside every "
                "app package. Declare it in a module of the app's package, and "
                "import that module after the App class."
            ) from None
        cls.app = app.name
        local_kind = cls.__dict__.get("kind") or snake_name(cls.__name__)
        if "." in local_kind:
            raise WorkflowError(
                f"{cls.__name__}.kind {local_kind!r} must be a local name — "
                "the declaring app supplies the namespace"
            )
        cls.kind = f"{cls.app}.{local_kind}"
        _declare_subject(cls)
        validate_settings_declaration(cls.Settings)
        if any(field_live_choices(field) for field in cls.Settings.model_fields.values()):
            raise WorkflowError(
                f"{cls.__name__}.Settings declares Choices, which applies only to app settings. "
                "Declare the field on the app Settings."
            )
        cls._body_method = _resolve_body_method(cls)
        # Before _wrap_steps: run()'s wrapper signature is (*args, **kwargs).
        cls._run_input_model = _input_model_from_signature(
            getattr(cls, cls._body_method),
            owner=f"{cls.__name__}.{cls._body_method}()",
            model_name=f"{cls.__name__}Input",
        )
        if cls._run_input_model:
            claimed = {"account_id"} & set(cls._run_input_model.model_fields)
            if claimed:
                raise WorkflowError(
                    f"{cls.__name__}.{cls._body_method}() declares reserved start() "
                    f"parameter(s) {sorted(claimed)} — attribution is platform "
                    "routing, not workflow input; name the parameter differently"
                )
        if cls.every and getattr(cls, "dispatch", None):
            required = [
                name
                for name, p in inspect.signature(cls.dispatch).parameters.items()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            ]
            if required:
                raise WorkflowError(
                    f"{cls.__name__}.dispatch() requires {required}, but a scheduled "
                    "dispatch fires with no arguments — a schedule's dispatch must be "
                    "nullary"
                )
        if "dispatch" in vars(cls):
            cls.dispatch = _bind_dispatch(vars(cls)["dispatch"])
        _wrap_steps(cls)
        _register_entry(cls)
        app.workflows.register(cls)

    def __init__(self) -> None:
        self._workflow_id: str = ""
        # What the run is about ({"type", "id"}), set from the dispatch arguments
        # before run(); None for a subjectless framework run.
        self._subject: dict[str, Any] | None = None
        # run()'s validated input bundle (the model synthesized from its signature),
        # set before run() — for templates and derived properties. None = no input.
        self.input: BaseModel | None = None
        # The dispatcher binds the authenticated or default account before execution.
        self.account: Account
        self.account_id: str
        # The chat conversation whose tool call started the run, if one did.
        self.conversation_id: str | None = None
        self.journal = self.journal_class()
        self._host: Host | None = None
        self._host_secrets_hash = ""

    async def announce(self, topic: str, **facts: Any) -> None:
        """Record a domain fact, then notify subscribers in a separate checkpoint."""
        if _in_step.get():
            raise WorkflowError("announce() runs in the workflow body, not inside a @step")

        async def record() -> None:
            async with step_session() as session:
                run = await session.get(Run, self.workflow_id)
                await Event.emit(
                    session,
                    type=topic,
                    subject=self._subject,
                    key=run.subject_key,
                    run=self.workflow_id,
                    kind=self.kind,
                    facts=facts,
                    app=self.app,
                )

        await DBOS.run_step_async(StepOptions(name=topic, **_IO_RETRIES), record)

        async def notify() -> None:
            async with step_session():
                await publish(topic, subject=self._subject, kind=self.kind, **facts)

        await DBOS.run_step_async(StepOptions(name=f"{topic}:propagate", **_IO_RETRIES), notify)

    async def get_cost(self) -> float:
        """What this run's agent calls have cost so far, in US dollars, as recorded. A
        checkpoint: a replay reads the cost the run read the first time."""

        async def read() -> float:
            async with step_session() as session:
                return await AgentCall.get_cost_for_run(session, self.workflow_id)

        return await DBOS.run_step_async(StepOptions(name="get_cost", **_IO_RETRIES), read)

    async def get_duration(self) -> timedelta:
        """How long this run's steps and agent calls have taken so far. A checkpoint: a
        replay reads what the run read the first time."""
        return await DBOS.run_step_async(
            StepOptions(name="get_duration", **_IO_RETRIES),
            Run.get_duration,
            self.workflow_id,
            self.kind,
        )

    async def review(
        self, *, questions: list[BaseModel] | None = None, context: str | list[Block] = ""
    ) -> OperatorReply:
        # Park for an in-app decision: the ask carries the controls, any
        # questions, and optional context (rendered beside the reviewed
        # document); the OperatorReply carries the operator's answer (the resume
        # endpoint holds the action to the offered controls). External gates use
        # a Gate. The artifact the reviewer judges isn't named here — a parked
        # run can't produce new ones, so the read side resolves the latest on
        # demand.
        if not self._subject:
            # An in-app review is answered from the subject's surfaces; a
            # subjectless run has none, so nobody would ever see the ask.
            raise SubjectlessGate(OperatorReply.name)
        request = {
            "presentation": "in_app",
            "questions": [q.model_dump(mode="json") for q in questions or ()],
        }
        if context:
            request["context"] = context
        reply = await _park(self, OperatorReply, request, GATE_TTL_SECONDS)
        self.journal.add(reply)
        return reply

    async def wait(self, topic: str, *, label: str, ttl_seconds: float) -> dict[str, Any]:
        """Wait for the facts that ``answer`` delivers on ``topic``, and return them. The run
        stays running, shows ``label``, and keeps its warm sandbox. A failure answered
        instead raises ``WaitFailed``."""
        # The wait takes answers before its label shows, so an answer sent on seeing it arrives.
        await DBOS.set_event_async("waiting_on", topic)
        await set_run_phase(label)
        facts = await DBOS.recv_async(topic, timeout_seconds=ttl_seconds)
        await DBOS.set_event_async("waiting_on", "")
        if facts is None:
            raise WaitTimeout(topic)
        if failure := facts.get("failure"):
            raise WaitFailed(topic, failure)
        return facts

    async def sleep(self, seconds: float, *, label: str, until: str = "") -> bool:
        """Sleep for ``seconds``, or until ``answer`` arrives on ``until``, and return whether
        it arrived. The run stays running, shows ``label``, and releases its warm sandbox."""
        # ``answer`` drops what arrives before ``waiting_on`` is set, and the release
        # takes time.
        await DBOS.set_event_async("waiting_on", until)
        await self._reap_run()
        await set_run_phase(label)
        if until:
            answer = await DBOS.recv_async(until, timeout_seconds=seconds)
            await DBOS.set_event_async("waiting_on", "")
            # An answer with no facts is an empty dict.
            return answer is not None
        await DBOS.sleep_async(seconds)
        return False

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        # Everything an agent's template renders with, beyond the workflow and
        # workspace it always gets. Extend via super() to add an app's standing
        # values (expensive derived prose); the call's own kwargs win on collision.
        return dict(context)

    @asynccontextmanager
    async def _workspace(
        self,
        host_id: str | None,
        step: str,
        refs: list[SecretRef],
        *,
        workspace: Workspace,
        secrets: dict[str, Secret],
        secrets_hash: str,
    ) -> AsyncIterator[Workspace]:
        # A warm run attaches the run's held VM; the rest get a fresh ephemeral VM.
        # Either way the workspace, read before the box, gets the VM for this call
        # only, so nothing (connection or credential) is held across steps. The
        # identity row stays in the session that read it, which binds it to the
        # box; that session's unit of work closes before the boot.
        async with step_session() as session:
            identity = None
            if refs and not host_id:
                identity = await SandboxIdentity.lookup(
                    session,
                    account_id=self.account_id,
                    run_id=self._workflow_id,
                    scoped_to=step,
                    secret_refs=refs,
                )
            if host_id:
                vm = sandbox_client.attach(host_id=host_id)
            elif identity:
                # A crashed attempt left its box behind. Its identity finds it again.
                vm = sandbox_client.resume(host_id=identity.host_id)
            else:
                template = None
                if self.sandbox:
                    template = await get_template_id(self.sandbox)
                    await set_run_phase("provisioning_vm")
                # A box that fetches gets its own identity, and the key names it. A
                # replay finds the box through the identity, above.
                identity, entries, key = None, {}, secrets_hash
                if refs:
                    identity, entries = await SandboxIdentity.create(
                        session,
                        account_id=self.account_id,
                        run_id=self._workflow_id,
                        scoped_to=step,
                        secret_refs=refs,
                        secrets_hash=secrets_hash,
                    )
                    key = identity.id
                vm = sandbox_client.ephemeral(
                    idempotency_key=provisioning_key(self._workflow_id, step, key),
                    secrets={**secrets, **entries},
                    template=template,
                    identity=identity,
                    lease_seconds=WORKFLOW_HOST_LEASE_SECONDS,
                )
            await session.commit()
            try:
                # The box work commits each operation on its own, so a read in
                # prepare() or in a hook holds no connection through the run.
                async with vm as box, sandbox_client.lease(host_id=box.id), autocommit_session():
                    workspace.host = box
                    yield workspace
            except HostGone as error:
                await self._reap_run()
                raise HarnessSandboxProvisioningError(str(error)) from error
            except HarnessSandboxProvisioningError:
                if host_id:
                    await self._reap_run()
                raise

    async def exec(
        self, command: list[str], *, timeout: float = 600.0
    ) -> subprocess.CompletedProcess[str]:
        """Run one command in a VM of this workflow's workspace, then release the VM.
        The VM comes from the workflow's sandbox template and holds the workspace's
        secrets. Call it from ``run()`` or inside a ``@step``."""
        async with step_session():
            subject = await self.subject
            refs = await self.workspace_class.get_secret_refs(subject)
            workspace = self.workspace_class(subject)
        digest = hashlib.sha256(json.dumps([ref.secret_id for ref in refs]).encode()).hexdigest()
        async with self._workspace(
            None, "exec", refs, workspace=workspace, secrets={}, secrets_hash=digest
        ) as workspace:
            return await workspace.exec(command, timeout=timeout)

    async def _lease_host(self, config: AgentConfig, refs: list[SecretRef]) -> str | None:
        # The warm VM, provisioned once per segment; state is carried in git, so
        # only the host-id matters across steps — held-across-steps never fights replay.
        if not self.steps_reuse_sandbox:
            return
        # The identity row stays in the session that read it, which binds it to
        # the box; that session's unit of work closes before the boot.
        async with step_session() as session:
            # A crashed process left its box behind. Its identity finds it again.
            if not self._host and refs:
                identity = await SandboxIdentity.lookup(
                    session,
                    account_id=self.account_id,
                    run_id=self._workflow_id,
                    scoped_to="workflow",
                    secret_refs=refs,
                )
                if identity:
                    self._host = await sandbox_client.reattach(host_id=identity.host_id)
                    self._host_secrets_hash = config.secrets_hash
            if self._host and self._host_secrets_hash != config.secrets_hash:
                # Drukbox binds entries at creation.
                await self._reap_run()
            if not self._host:
                template = None
                if self.sandbox:
                    template = await get_template_id(self.sandbox)
                    await set_run_phase("provisioning_vm")
                # The key names the pasted key the VM holds, so a replay finds its VM.
                # A box that fetches gets its own identity, and the key names that
                # instead. A replay finds the box through the identity, above.
                identity, entries, key = None, {}, config.secrets_hash
                if refs:
                    identity, entries = await SandboxIdentity.create(
                        session,
                        account_id=self.account_id,
                        run_id=self._workflow_id,
                        scoped_to="workflow",
                        secret_refs=refs,
                        secrets_hash=config.secrets_hash,
                    )
                    key = identity.id
                await session.commit()
                self._host = await sandbox_client.provision(
                    idempotency_key=provisioning_key(self._workflow_id, "workflow", key),
                    secrets={**config.secrets, **entries},
                    template=template,
                    identity=identity,
                    lease_seconds=WORKFLOW_HOST_LEASE_SECONDS,
                )
                self._host_secrets_hash = config.secrets_hash
        return self._host.id

    async def _reap_run(self) -> None:
        if self._host:
            host, self._host = self._host, None
            await sandbox_client.release(host_id=host.id)

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    # The effective schedule, resolved like an agent's model/effort/timeout:
    # operator override → the declared default. The reconciler and the settings
    # read both go through these, so the workflow owns its own knobs.
    @classmethod
    async def get_schedule(
        cls, session: AsyncSession, *, overrides: dict[str, SettingsOverride] | None = None
    ) -> str | None:
        return await SettingsOverride.workflow_setting(
            session, cls.kind, "schedule", cls.every, overrides=overrides
        )

    @classmethod
    async def has_enabled_schedule(
        cls, session: AsyncSession, *, overrides: dict[str, SettingsOverride] | None = None
    ) -> bool:
        # There is a schedule and it's on — False for unscheduled workflows too.
        if not cls.every:
            return False
        return await SettingsOverride.workflow_setting(
            session, cls.kind, "schedule_enabled", True, overrides=overrides
        )

    @classmethod
    async def settings(cls) -> BaseModel:
        """The workflow's ``Settings``, resolved through the override store — the read
        twin of ``override_setting``, like ``App.settings()`` for an app."""
        values = {
            name: await SettingsOverride.workflow_setting(
                db_session(), cls.kind, name, field.default
            )
            for name, field in cls.Settings.model_fields.items()
        }
        return cls.Settings.model_validate(values)

    @classmethod
    async def override_setting(cls, field: str, value: Any) -> None:
        # An operator's override for one knob; None clears it back to the declared
        # default. Raises ValueError so the API layer can 422 it. The schedule pair
        # is validated here, not against Settings — those knobs live beside every=.
        if cls.every and field in ("schedule", "schedule_enabled"):
            if field == "schedule" and value is not None and not croniter.is_valid(str(value)):
                raise ValueError(f"Invalid cron expression {value!r}")
            if field == "schedule_enabled" and value is not None and not isinstance(value, bool):
                raise ValueError(f"schedule_enabled must be a bool, got {value!r}")
        elif field not in cls.Settings.model_fields:
            raise ValueError(f"Unknown {cls.kind} setting {field!r}")
        elif value is not None:
            value = coerce_setting_value(cls.Settings, field, value)
            validate_setting_override(
                cls.Settings, (await cls.settings()).model_dump(), field, value
            )
        await SettingsOverride.set_workflow_setting(db_session(), cls.kind, field, value)

    @classmethod
    def _validate_subject(cls, subject: "Subject | StoredSubject | None") -> None:
        # The declaration is the contract — subscribers derive their subject class
        # from ``workflow=``, so it has to hold for every run.
        if cls.subject:
            if isinstance(subject, cls.subject):
                return
            given = type(subject).__name__ if subject else "nothing"
            raise WorkflowError(f"{cls.__name__} is about {cls.subject.__name__}, not {given}")
        if subject:
            raise WorkflowError(
                f"{cls.__name__} declares no subject — declare "
                f"``subject = {type(subject).__name__}`` on it, or pass subject=None"
            )

    @classmethod
    async def cancel(cls, subject: Subject | StoredSubject, *, failure: str | None = None) -> None:
        cls._validate_subject(subject)
        async with bound_session() as session:
            run = await Run.get_latest_for_subject(
                session, subject.subject_type, str(subject.id), kind=cls.kind
            )
            if run and run.is_active:
                await run.cancel(failure=failure)

    @classmethod
    async def answer(
        cls,
        subject: Subject | StoredSubject,
        topic: str,
        *,
        failure: str | None = None,
        **facts: Any,
    ) -> None:
        """Deliver ``facts`` to the subject's run of this workflow, when it waits on ``topic``.
        ``failure`` instead fails that wait with the reason."""
        cls._validate_subject(subject)
        if failure and facts:
            raise WorkflowError(f"answer() on {topic!r} takes failure= or facts, not both")
        async with bound_session() as session:
            run = await Run.get_latest_for_subject(
                session, subject.subject_type, str(subject.id), kind=cls.kind
            )
        if run and await DBOS.get_event_async(run.id, "waiting_on", timeout_seconds=0) == topic:
            await DBOS.send_async(run.id, {"failure": failure} if failure else facts, topic=topic)

    @classmethod
    async def _get_run_account(cls, session: AsyncSession, account_id: str | None) -> Account:
        """The account that a new run starts as. It must be able to use the workflow's app.
        During a webhook delivery that names a person, the run starts as that person's account,
        also when the caller passes another ``account_id``."""
        app = get_app(cls.app)
        if actor := current_actor.get():
            if owner_id := await VaultSecret.get_owner_id(session, actor.authority, actor.user_id):
                account_id = owner_id
            elif await Team.exists_for_app(session, app):
                # A person with no account is on no team.
                raise PermissionDenied(f"A person with no account cannot use the app {app.name}.")
        account = await Account.get_for_run(session, account_id)
        if DBOS.workflow_id:
            # A replay runs the caller's body again. The step keeps the first answer, so a
            # later change to a team cannot refuse a run that started.
            can_use_app = await DBOS.run_step_async(
                StepOptions(name=f"{cls.kind}.can_use_app"), account.can_use_app, app
            )
        else:
            can_use_app = await account.can_use_app(app)
        if can_use_app:
            return account
        raise PermissionDenied(f"{account.username} cannot use the app {app.name}.")

    @classmethod
    async def start(
        cls,
        *,
        subject: Subject | StoredSubject | None,
        account_id: str | None = None,
        **input: Any,
    ) -> str:
        # Mint the id, enqueue the body, write the projection row. Returns the
        # run to follow — a freshly enqueued run, or the subject's already-live
        # one handed back. Enqueuing (not start_workflow) routes execution
        # onto the shared queue, so the process that kicks a run off — often the
        # web process — doesn't have to be the one that runs it; any launched
        # executor picks it up. The input kwargs mirror the body's own signature
        # and validate against the model synthesized from it, so a bad shape
        # fails at start, not inside the run.
        # subject is required (no default) so a run can't silently lose its
        # timeline by omission — pass subject=None explicitly for a background run.
        cls._validate_subject(subject)
        async with bound_session() as session:
            account = await cls._get_run_account(session, account_id or current_account_id.get())
            account_id = account.id
            wire: dict[str, Any] = {}
            if cls._run_input_model:
                wire = cls._run_input_model.model_validate(input).model_dump(mode="json")
            elif input:
                raise WorkflowError(f"{cls.__name__}.{cls._body_method}() takes no input")
            wire[_ACCOUNT_INPUT_KEY] = account_id
            conversation_id = current_conversation_id.get()
            if conversation_id:
                wire[_CONVERSATION_INPUT_KEY] = conversation_id
            workflow_id = str(uuid7())
            options: EnqueueOptions = {
                "workflow_name": cls.kind,
                "queue_name": RUN_QUEUE,
                "workflow_id": workflow_id,
            }
            if subject:
                # A subject has at most one active run per workflow kind, enforced by
                # DBOS queue deduplication: the slot is claimed atomically at enqueue,
                # held while the workflow is enqueued or pending (a parked run keeps
                # it), and freed by DBOS itself at the terminal outcome — including
                # when DBOS gives up on a dead workflow. Subjectless runs are unbounded.
                options["deduplication_id"] = f"{cls.kind}:{subject.subject_type}:{subject.id}"
                # The routing metadata, stamped as DBOS custom attributes so "runs for
                # this subject" is answered by workflow_status itself. The subject id is
                # a string — the one shape every reader compares.
                options["attributes"] = {
                    "subject_type": subject.subject_type,
                    "subject_id": str(subject.id),
                    "subject_key": subject.key,
                }
            subject_record = subject.identity if subject else None
            if DBOS.workflow_id:
                # Inside a workflow, DBOS records the start as the workflow's child, so a
                # replay finds the run instead of starting it again.
                deduplication = (
                    SetEnqueueOptions(
                        deduplication_id=options["deduplication_id"],
                        duplication_policy="return-existing",
                    )
                    if subject
                    else nullcontext()
                )
                with (
                    SetWorkflowID(workflow_id),
                    SetWorkflowAttributes(options.get("attributes")),
                    deduplication,
                ):
                    handle = await DBOS.enqueue_workflow_async(
                        RUN_QUEUE, cls._entry, subject_record, wire
                    )
                run_id = handle.workflow_id
            else:
                # Elsewhere the run enters the queue in the caller's transaction, so a
                # rollback starts nothing.
                run_id = await enqueue_in_transaction(session, options, subject_record, wire)
            if run_id != workflow_id:
                # The slot was held — the run is the subject's live one.
                return run_id
            # The body also creates its row (idempotently); this one commits with the start.
            await Run.create_row(
                session,
                workflow_id=workflow_id,
                kind=cls.kind,
                account_id=account_id,
                conversation_id=conversation_id,
            )
            if subject:
                await Event.emit(
                    session,
                    type=WorkflowEvent.SCHEDULED,
                    subject=subject.identity,
                    key=subject.key,
                    run=workflow_id,
                    kind=cls.kind,
                    app=cls.app,
                )
                await publish(WorkflowEvent.SCHEDULED, subject=subject.identity, kind=cls.kind)
            return workflow_id

    @classmethod
    async def replace(cls, *, subject: Subject | StoredSubject, **arguments: Any) -> str:
        """Start a new run of this workflow in place of the subject's active run."""
        if _in_step.get() or not DBOS.workflow_id:
            raise WorkflowError("replace() runs in the workflow body, not inside a @step")
        # DBOS has no start that replaces the active run, and its cancel joins no
        # transaction. As one recorded step, a replay cannot cancel the new run.
        await DBOS.run_step_async(
            StepOptions(name=f"{cls.kind}.replace"),
            partial(cls.cancel, subject, failure=REPLACED_FAILURE),
        )
        return await cls.start(subject=subject, **arguments)


def _bind_dispatch(method: classmethod) -> classmethod:
    # dispatch() is launch policy that reads. A route, a subscriber, and the cron
    # tick call it with a session bound; a workflow body calls it holding none.
    policy = method.__func__

    @wraps(policy)
    async def dispatch(cls: type[Workflow], *args: Any, **kwargs: Any) -> Any:
        async with bound_session():
            return await policy(cls, *args, **kwargs)

    return classmethod(dispatch)


def _wrap_steps(cls: type[Workflow]) -> None:
    for method_name, method in list(vars(cls).items()):
        if getattr(method, "_durable_step", False):
            name = getattr(method, "_step_name", None) or method_name
            retries = getattr(method, "_step_retries", 0)
            setattr(cls, method_name, _make_step(cls.kind, name, method, retries))


def _make_step(kind: str, name: str, method: Callable, retries: int) -> Callable:
    # Run the method inside its own session via a zero-arg closure, so `self` is
    # never serialized into the DBOS checkpoint. Each operation commits on its own,
    # so the step holds no connection through a call that leaves the database.
    async def _step(self: Workflow, *args: Any, **kwargs: Any) -> Any:
        async def _do() -> Any:
            token = _in_step.set(True)
            try:
                async with autocommit_session():
                    return await method(self, *args, **kwargs)
            finally:
                _in_step.reset(token)

        options = StepOptions(name=f"{kind}.{name}")
        if retries > 0:
            options["retries_allowed"] = True
            options["max_attempts"] = retries + 1
        return await DBOS.run_step_async(options, _do)

    _step.__name__ = name
    _step.__wrapped__ = method  # lets a test call run() without DBOS
    return _step


def _bind_instance(
    cls: type[Workflow],
    subject: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
    *,
    account: Account,
) -> tuple[Workflow, dict[str, Any]]:
    """A workflow instance carrying its subject and validated input, with the
    keyword arguments its body takes."""
    instance = cls()
    instance._subject = subject
    input = dict(input or {})
    input.pop(_ACCOUNT_INPUT_KEY, None)
    instance.account = account
    instance.account_id = account.id
    instance.conversation_id = input.pop(_CONVERSATION_INPUT_KEY, None)
    # The body's input re-validates from its wire dict; a cron fires with no
    # input, so a scheduled workflow must default every parameter. The validated
    # bundle also lands on the instance for templates / derived properties.
    run_kwargs: dict[str, Any] = {}
    if cls._run_input_model:
        validated = cls._run_input_model.model_validate(input)
        instance.input = validated
        run_kwargs = {name: getattr(validated, name) for name in type(validated).model_fields}
    return instance, run_kwargs


async def _run_instance(
    cls: type[Workflow],
    subject: dict[str, Any] | None = None,
    input: dict[str, Any] | None = None,
) -> Any:
    async with step_session() as session:
        account = await Account.get_for_run(session, (input or {}).get(_ACCOUNT_INPUT_KEY))
    instance, run_kwargs = _bind_instance(cls, subject, input, account=account)
    instance._workflow_id = DBOS.workflow_id  # type: ignore[assignment]
    # The workflow's span names what the run works on, so a trace store adds up the
    # runs of a subject.
    attributes = {"druks.app": cls.app, "druks.run_id": instance._workflow_id}
    if subject:
        attributes |= {
            "druks.subject_type": subject["type"],
            "druks.subject_id": str(subject["id"]),
        }
    trace.get_current_span().set_attributes(attributes)
    token = current_workflow.set(instance)
    # The run's account is ambient, as a request's is: a workspace reads the
    # dispatcher from it, and a child run started from the body attributes to it.
    account_token = current_account_id.set(instance.account_id)
    try:
        return await _execute_run(
            instance._workflow_id,
            cls.kind,
            subject,
            instance.account_id,
            lambda: getattr(instance, cls._body_method)(**run_kwargs),
            conversation_id=instance.conversation_id,
        )
    finally:
        current_account_id.reset(account_token)
        current_workflow.reset(token)
        await instance._reap_run()


async def _dispatch_instance(cls: type[Workflow], _context: dict[str, Any] | None = None) -> Any:
    # The cron tick runs no workflow of its own kind — no Run row — it just calls
    # dispatch(), which start()s the real subject-backed run. Body level, not a
    # step: DBOS only allows the child-start inside start() from a workflow body.
    return await cls.dispatch()


async def _run_scheduled_instance(
    cls: type[Workflow], context: dict[str, Any] | None = None
) -> Any:
    # The cron tick of a workflow with no dispatch() is the run itself, as the default
    # account.
    async with step_session() as session:
        account = await cls._get_run_account(session, None)
    return await _run_instance(cls, context, {_ACCOUNT_INPUT_KEY: account.id})


def _register_entry(cls: type[Workflow]) -> None:
    # The closure binds cls outside the durable arguments: the DBOS workflow
    # NAME (the kind) is what says which class this is, so recovery rebinds by
    # name and no class object ever rides a checkpoint.
    @DBOS.workflow(name=cls.kind)
    async def _entry(subject: dict[str, Any] | None, input: dict[str, Any]) -> None:
        await _run_instance(cls, subject, input)

    cls._entry = staticmethod(_entry)  # type: ignore[assignment]

    if cls.every:
        fire = _dispatch_instance if getattr(cls, "dispatch", None) else _run_scheduled_instance
        register_schedule(cls, partial(fire, cls))


class InstructionOutput(BaseModel):
    """Base for an instruction's structured output contract — the model an ``Instruction``
    declares as its ``contract``. The harness sends the model's schema to OpenAI's
    strict structured-output validator on a Codex model, which requires every
    object node to set ``additionalProperties: false`` and list every property in
    ``required``: ``extra="forbid"`` gives the former, and declaring every field
    without a default (optionals as required-but-nullable ``X | None``) gives the
    latter — a field with a default 400s at runtime."""

    model_config = pydantic.ConfigDict(extra="forbid")

    def to_result(self) -> Any:
        # What the caller gets back from an agent call: the validated output itself
        # by default. Override to map the strict output onto a looser domain
        # type — the seam that keeps the output contract out of durable records,
        # applied by the call so no caller ever invokes it.
        return self

    def to_artifact(self) -> dict[str, str]:
        # The call's renderable output as {kind, title, content} — the platform persists
        # it after the call. Empty unless the contract produces a reviewable document.
        return {}

    def to_event(self) -> dict[str, str]:
        """Return a topic and optional summary to record with the saved artifact. Empty
        opts out."""
        return {}


@dataclass(frozen=True)
class Instruction:
    contract: type[InstructionOutput]
    # Display label for the settings UI; the attribute name shows when it's None.
    name: str | None = None
    # Short human-friendly blurb of what the instruction does, shown in the settings UI.
    description: str = ""
    # The prompt template ``run`` renders. None for instructions that build their
    # prompt inline and drive the harness themselves (planning) instead of
    # going through ``run``.
    prompt: str | None = None
    # Declared run timeout in seconds, overridable per instruction; None inherits the
    # harness default. How long a step may take is a fact about the task; the
    # model and the effort are the operator's, set in Settings.
    timeout: int | None = None
    # ``include_plugins=False`` skips the operator's plugin state for prompts
    # that hit no MCP server.
    include_plugins: bool = True
    # ``include_mcp=False`` gives the call no MCP server and its sandbox no
    # server entry, for an instruction that reads untrusted content.
    include_mcp: bool = True
    # True lets a call continue this instruction's last call in the run, with ``resume=``.
    # The first call then writes the model's prompt cache for an hour, not five minutes, so
    # the resume reads the cache after a pause. The templates read ``can_resume``, true
    # when the call's harness can resume too.
    resumable: bool = False
    # The service secrets the call's sandbox holds, ``(Acme.fields.api_key,)``.
    # Each is a placeholder that the secrets proxy swaps at the service's host.
    secrets: "tuple[ServiceField, ...]" = ()
    # The saved browser session the call drives, and the client Druks starts
    # beside Chrome for it. The call gets the ``browser`` MCP server beside the
    # servers it has anyway.
    # A session is mutable, so it stays out of the instruction's hash.
    browser: "BrowserSession | None" = field(default=None, compare=False)
    browser_client: BrowserClient | None = None
    # ``id`` is the instruction's durable key (settings, timeline, registry, step name):
    # ``<app>.<attribute>``, from the App it is declared on. ``app`` is that App's
    # name. Both are read from the class in __set_name__.
    id: str = field(default="", init=False, compare=False)
    app: str = field(init=False, compare=False, default="")
    _after_agent: list[Callable[..., Awaitable[str | None]]] = field(
        default_factory=list, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.browser and self.browser_client != BrowserClient.PINCHTAB:
            raise TypeError(
                "An instruction with browser= needs browser_client=BrowserClient.PINCHTAB. "
                "Druks starts no other client for an agent call."
            )
        if self.browser_client and not self.browser:
            raise TypeError(
                "Instruction(browser_client=...) needs browser=, the session it drives."
            )
        for secret in self.secrets:
            declared = f"{secret.service.__name__}.fields.{secret.name}"
            if not secret.is_secret:
                raise TypeError(f"{declared} is not a secret field, so no sandbox holds it")
            if not secret.service.host:
                raise TypeError(
                    f"{declared} goes to a sandbox, so {secret.service.__name__} must declare "
                    "`host`: the one host the secret may be sent to"
                )
            if secret.service.secret_kind != SecretKind.STATIC:
                raise TypeError(
                    f"{declared} belongs to an App key, which issues tokens; no sandbox holds it"
                )

    def __set_name__(self, owner: type, attr: str) -> None:
        if self.id:  # a second name for an instruction that an App declares already
            return
        object.__setattr__(self, "id", f"{owner.name}.{attr}")
        object.__setattr__(self, "app", owner.name)
        owner.instructions.register(self)

    @classmethod
    def get(cls, id: str) -> "Instruction":
        """The instruction registered as ``id``, ``<app>.<attribute>``, loading its app first."""
        if not get_instruction(id):
            resolve_app(id.partition(".")[0])
        instruction = get_instruction(id)
        if not instruction:
            raise ObjectNotFound("instruction", {"id": id})
        return instruction

    def after_agent(
        self, hook: Callable[..., Awaitable[str | None]]
    ) -> Callable[..., Awaitable[str | None]]:
        """Call ``hook(prompt=..., workspace=...)`` when this agent ends its turn. Text
        that it returns goes back to the agent as one more turn."""
        self._after_agent.append(hook)
        return hook

    async def get_config(self) -> AgentConfig:
        """How this instruction runs for the current run's actor, read from settings now."""
        workflow = current_workflow.get(None)
        if not workflow:
            raise WorkflowError(f"instruction {self.id!r} reads its config only inside a workflow")
        return await get_config(db_session(), self.id, workflow.account_id)

    async def __call__(
        self,
        *,
        contract: type[InstructionOutput] | None = None,
        agent_id: str | None = None,
        resume: Callable[..., Awaitable[str]] | None = None,
        **context: object,
    ) -> Any:
        """Run the instruction — ``await SoftwareFactory.implement(...)`` — as a durable step in the
        current workflow and return its parsed output. An instruction run is always memoized —
        this picks which step does it: its own, or the @step it's already inside.
        workflow_id comes from the workflow context, not the caller; everything
        else (repo, …) is prompt context. ``contract`` overrides the output
        contract for this call, including schema generation and validation. ``agent_id``
        runs the call as that saved agent: with its instructions, its model, its billing,
        and only its skills. ``resume`` continues the instruction's last call in this run, on
        its box, as the same conversation: Druks calls ``resume(workspace=...)`` with the
        checkout as the agent left it, and sends the text it returns as the agent's next
        message. The instruction must be ``resumable``, and its harness must be able to resume."""
        if not self.id:
            # Built loose — never assigned to an App — so its settings, its
            # registry entry and the call it would record all key on nothing.
            raise WorkflowError("an instruction runs under its own id — declare it on an App")
        workflow = current_workflow.get(None)
        if not workflow:
            raise WorkflowError(
                f"instruction {self.id!r} can only run inside a workflow; standalone instruction "
                "runs aren't supported yet"
            )

        async def _invoke() -> Any:
            with trace.get_tracer(__name__).start_as_current_span(
                f"agent_call {self.id}",
                # A call inside a @step names its run as well.
                attributes={"druks.agent": self.id, "druks.run_id": workflow.workflow_id},
            ):
                return await self._run(
                    workflow_id=workflow.workflow_id,
                    contract=contract,
                    agent_id=agent_id,
                    resume=resume,
                    **context,
                )

        if _in_step.get():
            transient_retry_index = 0
            while True:
                try:
                    # The enclosing @step memoizes the call.
                    return await _invoke()
                except HarnessError as error:
                    # Transient only, on the error's own schedule. Quota waits
                    # are hours; nothing that long belongs inside a held step,
                    # so those fail here instead of sleeping.
                    if error.retry is not Retry.TRANSIENT or transient_retry_index >= len(
                        error.retry_delays
                    ):
                        raise
                    delay = error.retry_delays[transient_retry_index]
                    transient_retry_index += 1
                    # Plain sleep: nothing checkpoints inside a step — a crash
                    # re-runs the whole enclosing step from attempt one anyway.
                    await asyncio.sleep(delay * random.uniform(0.9, 1.1))

        transient_retry_index = 0
        quota_retried = False
        while True:
            try:
                result = await DBOS.run_step_async(
                    StepOptions(name=f"{workflow.kind}.agent.{self.id}"), _invoke
                )
            except HarnessError as error:
                if error.retry is Retry.TRANSIENT:
                    # One retry per delay on the error's own schedule; past it,
                    # the failure stands.
                    if transient_retry_index >= len(error.retry_delays):
                        raise
                    delay = error.retry_delays[transient_retry_index]
                    transient_retry_index += 1
                    # Jitter, so a provider outage doesn't wake every run at once.
                    wait_seconds = delay * random.uniform(0.9, 1.1)
                elif error.retry is Retry.QUOTA:
                    if quota_retried:
                        raise
                    # Wait until the provider's window turns, read off the latest
                    # usage scrape.
                    quota = await DBOS.run_step_async(
                        StepOptions(name=f"{workflow.kind}.agent.{self.id}.retry_wait"),
                        _quota_retry_wait,
                        self,
                        workflow.account_id,
                        agent_id,
                    )
                    # A far-off reset (an exhausted weekly window) isn't worth
                    # sleeping into — fail now.
                    if quota["wait_seconds"] > _QUOTA_MAX_WAIT_SECONDS:
                        raise
                    await set_run_phase("waiting_for_quota", ends_at=quota["resumes_at"])
                    if workflow.conversation_id:
                        await _post_to_chat(
                            workflow.workflow_id,
                            "chat.report_quota_wait",
                            partial(report_quota_wait, resumes_at=quota["resumes_at"]),
                        )
                    # A wait that a switch ends does not use the retry.
                    quota_retried = await self._wait_out_quota(workflow, quota, agent_id)
                    if workflow.conversation_id:
                        await _post_to_chat(
                            workflow.workflow_id, "chat.report_quota_resume", report_quota_resume
                        )
                    continue
                else:
                    # Permanent or unclassified — never retried.
                    raise
                # A long sleep shouldn't hold a VM; the next attempt rebuilds
                # its workspace from git, like a gate park.
                if wait_seconds >= _REAP_BEFORE_WAIT_SECONDS:
                    await workflow._reap_run()
                # Durable: the end time checkpoints now, so a restart resumes
                # with the remaining wait, and replay skips it entirely.
                await DBOS.sleep_async(wait_seconds)
            else:
                # Body-level only, on both passes: in-step calls hit the early return
                # live and are skipped with their step on replay.
                workflow.journal.add(result)
                return result

    async def _wait_out_quota(self, workflow, quota: dict, agent_id: str | None) -> bool:
        """Sleep until the quota resets. Return False early when the agent switches to another
        account, model, or billing."""
        wait_seconds = quota["wait_seconds"]
        while wait_seconds > 0:
            if not await workflow.sleep(
                wait_seconds, label="waiting_for_quota", until=_QUOTA_TOPIC
            ):
                return True
            # Every waiting run gets the wake. The step says how long this one still waits,
            # so replay takes the same turns.
            wait_seconds = await DBOS.run_step_async(
                StepOptions(name=f"{workflow.kind}.agent.{self.id}.wait_remaining"),
                _quota_wait_remaining,
                self,
                workflow.account_id,
                agent_id,
                quota,
            )
        return False

    async def _get_config(
        self, session: AsyncSession, account_id: str | None, agent_id: str | None
    ) -> AgentConfig:
        """How one call runs: as the saved agent when it has one, else by this
        instruction's settings for the account."""
        if agent_id:
            return await (await Agent.get(session, id=agent_id)).get_config(session)
        return await get_config(session, self.id, account_id)

    @classmethod
    async def wake_runs_waiting_for_quota(cls, session: AsyncSession) -> None:
        """Wake every run waiting out a quota window, so it reads its agent's provider and
        account again. The wake commits with the session."""
        running = await session.scalars(select(Run.id).where(Run.state == RunState.RUNNING.value))
        for run_id in running:
            if await DBOS.get_event_async(run_id, "waiting_on", timeout_seconds=0) == _QUOTA_TOPIC:
                await session.run_sync(
                    get_dbos_client().send_in_transaction, run_id, {}, _QUOTA_TOPIC
                )

    async def _run(
        self,
        *,
        workflow_id: str,
        contract: type[InstructionOutput] | None = None,
        agent_id: str | None = None,
        resume: Callable[..., Awaitable[str]] | None = None,
        **context: Any,
    ) -> Any:
        """The raw execution: read what the call needs, provision or attach a host,
        run the harness, record the call. ``__call__`` handles the durable
        wrapping + nesting. Each read is a unit of work of its own, so no
        connection sits idle through the boot, the preparation, or the run."""
        if not self.prompt:
            raise WorkflowError(f"instruction {self.id!r} has no prompt template to render")
        contract = contract or self.contract
        workflow = current_workflow.get()
        # The span that __call__ opened for this call.
        span = trace.get_current_span()
        if db_session.registry.has():
            # An agent call is a durability boundary — its effects don't roll
            # back — so the enclosing step's work commits now instead of waiting
            # in an open transaction through the run.
            await db_session().commit()
        artifact_dir = get_settings().artifacts_dir / f"run-{workflow_id}"
        engine = _step_engine()

        async with step_session() as session:
            # Refusing an unservable call here beats provisioning a VM and
            # 401ing mid-run.
            config = await self._get_config(session, workflow.account_id, agent_id)
            call_id = config.harness_class.mint_run_id(None)
            running = await AgentCall.lookup_running(session, workflow_id, self.id)
            previous = None
            if resume:
                if not (self.resumable and config.harness_class.can_resume):
                    raise WorkflowError(
                        f"instruction {self.id!r} cannot resume: declare it resumable, and run "
                        f"it on a harness that resumes, which {config.harness_class.name!r} is not"
                    )
                previous = await AgentCall.lookup_last_succeeded(session, workflow_id, self.id)
                if not previous:
                    raise WorkflowError(f"instruction {self.id!r} has no finished call to resume")
        model = config.model
        span.set_attributes({"druks.harness": config.harness, "druks.model": model})
        subscription_id = config.subscription.id if config.subscription else None
        api_key_id = config.api_key.id if config.api_key else None

        # Registered for provisioning through execution — the subscription's
        # rotation defers around it. A key never rotates.
        gate = nullcontext()
        if subscription_id:
            gate = sandbox_gate.use(subscription_id, call_id)
        borrow = nullcontext()
        if self.browser:
            borrow = AgentBrowser.lend(
                self.browser,
                self.browser_client,
                account_id=workflow.account_id,
                call_id=call_id,
            )
        async with gate, borrow as lent_browser:
            await set_run_phase("provisioning_vm")
            async with step_session() as session:
                # Everything the box and the prompt need, from rows, before the
                # box exists. The workspace carries it across.
                subject = await workflow.subject
                workspace_class = workflow.workspace_class
                workspace = workspace_class(subject)
                mcp_servers, mcp_secret_refs = (), []
                if self.include_mcp:
                    mcp_servers, mcp_secret_refs = await workspace_class.get_all_mcp_servers(
                        session, subject, workflow.account_id
                    )
                if lent_browser:
                    server, secret = lent_browser
                    mcp_servers = (*mcp_servers, server)
                    mcp_secret_refs = [*mcp_secret_refs, secret]
                service_refs = []
                for secret in self.secrets:
                    # An optional service that is not connected leaves its secrets out.
                    if secret.service.required or await secret.service.is_connected():
                        service_refs.append(
                            SecretRef(
                                name=secret.secret_name,
                                secret_id=(await secret.service.get()).id,
                                host=secret.service.host,
                            )
                        )
                refs = [
                    *config.secret_refs,
                    *service_refs,
                    *(await workspace_class.get_secret_refs(subject)),
                    *mcp_secret_refs,
                ]
                # A deleted or missing File argument stops the call before the box
                # exists; the workspace ships the rest once it is up, and the
                # template reads each as its in-VM path.
                for value in context.values():
                    for file in value if type(value) is list else [value]:
                        if type(file) is File:
                            record = await session.get(FileRecord, file.id)
                            if not record or record.deleted_at:
                                raise FileUnavailableError(f"file {file.id} is deleted or missing")
                prompt_context = await workflow.get_prompt_context(**context)
                # The override lookup reads the GitHub credential from a row.
                template = await get_template(self.prompt, repo=prompt_context.get("repo"))
                harness = config.harness_class(
                    model=config.model, fast_mode=config.fast_mode, effort=config.effort
                )
                agent_instructions = ""
                if agent_id:
                    agent = await Agent.get(session, id=agent_id)
                    agent_instructions = agent.instructions
                    # An agent with no skills gets none, not the whole catalog.
                    skills = await agent.list_enabled_skills(session)
                else:
                    skills = [
                        skill.name
                        for skill in await Skill.list_delivered(
                            session, await workspace_class.get_skills(subject)
                        )
                    ]
                manifest = await harness.get_manifest(
                    session, mcp_servers=mcp_servers, skills=skills
                )
            # An agent browser's box holds the call's key, so the box belongs to
            # the call: it is never the warm box, and no other attempt resumes it.
            # The same goes for a call from an after-agent hook: the hooked agent holds it.
            scope = self.id
            host_id = None
            if self.browser or _in_after_agent.get():
                scope = call_id
            else:
                host_id = await workflow._lease_host(config, refs)

            # Record the call RUNNING once it has a host to run on (its id names
            # the on-disk transcript dir) so the live step shows while the agent
            # works, then finish it — or fail it if the run raised after
            # starting. A provisioning failure happens before this and records
            # no call.
            async with workflow._workspace(
                host_id,
                scope,
                refs,
                workspace=workspace,
                secrets=config.secrets,
                secrets_hash=config.secrets_hash,
            ) as runner:
                # The running call's box is this box when its identity found it again:
                # this attempt takes that call over, under its id, instead of starting
                # a second agent beside it. A box that is gone leaves the call to
                # start over on a new one.
                if running and running.sandbox_host_id == runner.host_id:
                    call_id = running.id
                    resume_from = running
                else:
                    resume_from = None
                # The session is a file on the box that ran it, and that box holds the checkout
                # as the agent left it. A long wait releases the box, so a retry can land on
                # another one. The session and the checkout went with the old box, and the
                # call starts over with its full prompt.
                resuming = bool(previous) and previous.sandbox_host_id == runner.host_id

                context_on_box = await self._upload_files(prompt_context, runner.host, call_id)
                context_on_box.setdefault("workflow", workflow)
                context_on_box["workspace"] = runner
                context_on_box["can_resume"] = self.resumable and harness.can_resume
                prompt = ""
                if not resuming:
                    prompt = await template.render_async(**context_on_box)
                    if agent_instructions:
                        prompt = f"{agent_instructions}\n\n{prompt}"
                hooks = {}
                if self._after_agent:
                    hooks["after_agent"] = partial(
                        self._run_after_agent,
                        harness=config.harness_class,
                        prompt=prompt,
                        workspace=runner,
                    )
                if resume_from:
                    await set_run_phase("agent_running")
                    attempt = runner.host.resume_agent(
                        harness,
                        agent=self.id,
                        artifact_dir=artifact_dir,
                        call_id=call_id,
                        started_at=resume_from.started_at,
                        timeout=config.timeout,
                        hooks=hooks,
                    )
                else:
                    session_options = {}
                    if self.resumable and harness.can_resume:
                        session_options["resumable"] = True
                    if resuming:
                        # Preparing the workspace would discard the agent's uncommitted work.
                        await set_run_phase("running_check")
                        prompt = await resume(workspace=runner)
                        session_options["resume_session"] = previous.id
                    else:
                        await runner.prepare()
                    await set_run_phase("agent_running")
                    await AgentCall.start(
                        engine,
                        call_id=call_id,
                        run_id=workflow_id,
                        model=model,
                        agent=self.id,
                        host_id=runner.host_id,
                        subscription_id=subscription_id,
                        api_key_id=api_key_id,
                    )
                    attempt = runner.run_agent(
                        harness=harness,
                        manifest=manifest,
                        timeout=config.timeout,
                        identity=config.identity,
                        include_apps=config.include_apps,
                        agent=self.id,
                        prompt=prompt,
                        schema=contract.model_json_schema(),
                        artifact_dir=artifact_dir,
                        call_id=call_id,
                        include_plugins=self.include_plugins,
                        mcp_servers=mcp_servers,
                        hooks=hooks,
                        **session_options,
                    )
                try:
                    result = await attempt
                except BaseException as error:
                    await AgentCall.fail(engine, call_id=call_id, error=error)
                    raise
                if resuming:
                    cost_usd, cost_metadata = harness.get_own_cost(
                        result.cost_usd, result.cost_metadata, previous.cost_metadata
                    )
                    result = dataclasses.replace(
                        result, cost_usd=cost_usd, cost_metadata=cost_metadata
                    )
                if usage := normalize_token_usage(result.cost_metadata):
                    span.set_attributes(
                        {f"druks.usage.{name}": count for name, count in usage.items()}
                    )
                # A call can cost nothing, and it says so with 0.0.
                # ast-grep-ignore: rule-2
                if result.cost_usd is not None:
                    span.set_attribute("druks.cost_usd", result.cost_usd)
                if result.error:
                    await AgentCall.finish(engine, call_id=call_id, result=result)
                    raise result.error
                try:
                    output, workspace_files = self._parse_output(contract, result)
                    if workspace_files:
                        # The files come off the box, so they are recorded while it is up.
                        async with step_session() as session:
                            await runner.host.save_files(
                                session,
                                workspace_files,
                                app=workflow.app,
                                agent_call_id=call_id,
                            )
                    event, artifact = self._get_event_and_artifact(contract, output)
                    await AgentCall.finish(engine, call_id=call_id, result=result)
                except BaseException as error:
                    await AgentCall.fail(engine, call_id=call_id, error=error)
                    raise

        if artifact:
            async with step_session() as session:
                await Artifact.record(
                    session,
                    call_dir=artifact_dir / call_id,
                    call_id=call_id,
                    event=event,
                    **artifact,
                )
        return output.to_result()

    async def _upload_files(
        self, context: dict[str, Any], host: "Host", call_id: str
    ) -> dict[str, Any]:
        # Each File argument lands in the call's directory on the box and reads
        # as its path there, at the top level or inside a list.
        uploaded = {}
        for key, value in context.items():
            if type(value) is File:
                uploaded[key] = await value.upload(host, call_id)
            elif type(value) is list:
                uploaded[key] = [
                    await item.upload(host, call_id) if type(item) is File else item
                    for item in value
                ]
            else:
                uploaded[key] = value
        return uploaded

    def _parse_output(
        self, contract: type[InstructionOutput], result: "AgentResult"
    ) -> tuple[InstructionOutput, list[File]]:
        workspace_files: list[File] = []
        try:
            output = contract.model_validate(
                result.output, context={"workspace_files": workspace_files}
            )
        except pydantic.ValidationError as error:
            raise HarnessInvalidOutputError(
                f"instruction {self.id!r} returned a payload that fails "
                f"{contract.__name__}: {error}"
            ) from error
        return output, workspace_files

    def _get_event_and_artifact(
        self, contract: type[InstructionOutput], output: InstructionOutput
    ) -> tuple[dict[str, str], dict[str, str]]:
        event = output.to_event()
        artifact = output.to_artifact()
        if event and not artifact:
            raise WorkflowError(
                f"{contract.__name__}.to_event() returned an event without an "
                "artifact. Implement to_artifact() for the saved result."
            )
        return event, artifact

    async def _run_after_agent(
        self, *, harness: type["Harness"], prompt: str, workspace: Workspace
    ) -> str:
        # The hooks run inside the hooked call's step, so an agent call in a hook
        # replays with that step instead of taking a checkpoint of its own.
        in_step = _in_step.set(True)
        in_after_agent = _in_after_agent.set(True)
        try:
            # The hooks run in the box work, through its session.
            texts = [await hook(prompt=prompt, workspace=workspace) for hook in self._after_agent]
        finally:
            _in_step.reset(in_step)
            _in_after_agent.reset(in_after_agent)
        return harness.get_after_agent_reply("\n\n".join(text for text in texts if text))


async def _quota_retry_wait(
    instruction: Instruction, account_id: str | None, agent_id: str | None
) -> dict:
    # Runs as its own step: replay reuses the recorded wait instead of
    # re-reading the scrape.
    async with step_session() as session:
        config = await instruction._get_config(session, account_id, agent_id)
        provider_id = config.model.partition("/")[0]
        scrape = await UsageScrape.latest_for(session, provider_id, config.charged_account_id)
    now = datetime.now(UTC)
    # No scrape yet, or nothing ahead of now: a blind half hour.
    wait_seconds = _QUOTA_FALLBACK_WAIT_SECONDS
    if scrape:
        reset = scrape.soonest_reset_after(now)
        if reset:
            wait_seconds = (reset - now).total_seconds()
    # Jitter, so one window's reset doesn't wake every run at once.
    wait_seconds += random.uniform(60, 120)
    return {
        "model": config.model,
        "billing": config.billing,
        "identity": config.identity,
        "wait_seconds": wait_seconds,
        "resumes_at": now + timedelta(seconds=wait_seconds),
    }


async def _quota_wait_remaining(
    instruction: Instruction, account_id: str | None, agent_id: str | None, quota: dict
) -> float:
    async with step_session() as session:
        config = await instruction._get_config(session, account_id, agent_id)
    # Another account, model, or billing retries now.
    if (
        config.model != quota["model"]
        or config.billing != quota["billing"]
        or config.identity != quota["identity"]
    ):
        return 0
    return (quota["resumes_at"] - datetime.now(UTC)).total_seconds()
