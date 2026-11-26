from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select

from druks import ui
from druks.accounts import Account, AccountKind, current_account_id
from druks.contrib.software_factory.constants import (
    CORRECTION_REPEAT_SHARE_BASELINE,
    CORRECTIONS_PER_BUILD_BASELINE,
    CORRECTIONS_SHOWN,
    CORRECTIONS_WINDOW_DAYS,
    JUDGEMENT_BUILDS,
)
from druks.contrib.software_factory.enums import Priority, Status
from druks.contrib.software_factory.models import Project, ProjectRepo, Ticket, WorkItem
from druks.core.lessons.constants import LESSON_INDEX_MAX_LINES
from druks.core.lessons.models import Lesson, LessonRepeat
from druks.db import db_session
from druks.events import Event
from druks.models import Base
from druks.workflows import WorkflowEvent

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.NONE: "No priority",
    Priority.URGENT: "Urgent",
    Priority.HIGH: "High",
    Priority.MEDIUM: "Medium",
    Priority.LOW: "Low",
}

UNASSIGNED = "Unassigned"
# An account that is gone, or Druks itself. The row still reads.
UNATTRIBUTED = "Unattributed"
FILTER_ANY = "Any"
# The assignee filter's value for unassigned. An empty value means any.
UNASSIGNED_FILTER = "none"


def _repo_options(repos: list[ProjectRepo]) -> list[ui.Option]:
    return [
        ui.Option(repo.full_name, value=str(repo.id), group=repo.project.name) for repo in repos
    ]


def _assignee_options(accounts: list[Account]) -> list[ui.Option]:
    return [ui.Option(UNASSIGNED, value="")] + [
        ui.Option(account.username, value=account.id) for account in accounts
    ]


def _priority_options() -> list[ui.Option]:
    return [ui.Option(label, value=priority.value) for priority, label in PRIORITY_LABELS.items()]


def _status_options() -> list[ui.Option]:
    return [ui.Option(status.label, value=status.value) for status in Status]


def _filter_select(name: str, label: str, options: list[ui.Option], value: str) -> ui.SelectField:
    return ui.SelectField(
        name=name,
        label=label,
        options=[ui.Option(FILTER_ANY, value=""), *options],
        value=value,
    )


def _live_form(ticket: Ticket, field: ui.Field, *, operation: str) -> ui.Form:
    return ui.Form(
        fields=[field],
        action=ui.Action(
            label=f"Save {field.label.lower()}",
            operation=operation,
            arguments={"identifier": ticket.identifier},
            refresh="page",
        ),
        submit="change",
        layout="row",
    )


@ui.page("/board")
async def board(
    status: Status | None = None,
    priority: Priority | None = None,
    updated: Literal["today", "week", "month"] | None = None,
    assignee: str = "",
    creator: str = "",
    project: int | None = None,
    repo: int | None = None,
):
    updated_since = None
    if updated == "today":
        updated_since = Base.utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    elif updated == "week":
        updated_since = Base.utc_now() - timedelta(days=7)
    elif updated == "month":
        updated_since = Base.utc_now() - timedelta(days=30)
    projects = await Project.list_all()
    repos = await ProjectRepo.list_all()
    accounts = await Account.filter(kind=AccountKind.OPERATOR)
    account_names = {account.id: account.username for account in accounts}
    tickets = await Ticket.list_matching(
        status=status,
        priority=priority,
        assignee=assignee,
        creator=creator,
        project_id=project,
        repo_id=repo,
        updated_since=updated_since,
    )
    repo_choices = [item for item in repos if not project or item.project_id == project]
    cards: dict[Status, list[ui.Card]] = {item: [] for item in Status}
    for ticket in tickets:
        description = [ticket.identifier]
        if ticket.priority != Priority.NONE:
            description.append(PRIORITY_LABELS[Priority(ticket.priority)])
        if ticket.assignee_id:
            description.append(account_names.get(ticket.assignee_id, UNATTRIBUTED))
        cards[Status(ticket.status)].append(
            ui.Card(
                title=ticket.title,
                description=" · ".join(description),
                link=ui.Link(
                    ticket.title, page="ticket", arguments={"identifier": ticket.identifier}
                ),
                drag={"identifier": ticket.identifier},
            )
        )
    return ui.Page(
        "Board",
        controls=[
            ui.Action(
                label="New ticket",
                operation="create_ticket",
                tone="primary",
                fields=[
                    ui.TextField(name="title", label="Title", is_required=True),
                    ui.SelectField(
                        name="repo_id",
                        label="Repo",
                        options=_repo_options(repos),
                        is_required=True,
                        help_text="The GitHub repo that this ticket's pull request will target.",
                    ),
                    ui.TextAreaField(
                        name="description", label="Description", is_markdown=True, rows=3
                    ),
                    ui.SelectField(
                        name="status",
                        label="Status",
                        options=_status_options(),
                        value=Status.BACKLOG.value,
                    ),
                    ui.SelectField(
                        name="priority",
                        label="Priority",
                        options=_priority_options(),
                        value=Priority.NONE.value,
                    ),
                    ui.SelectField(
                        name="assignee_id",
                        label="Assignee",
                        options=_assignee_options(accounts),
                        value=current_account_id.get() or "",
                    ),
                ],
            ),
        ],
        filters=[
            _filter_select("status", "Status", _status_options(), status or ""),
            _filter_select("priority", "Priority", _priority_options(), priority or ""),
            _filter_select(
                "updated",
                "Updated",
                [
                    ui.Option("Today", value="today"),
                    ui.Option("Past week", value="week"),
                    ui.Option("Past month", value="month"),
                ],
                updated or "",
            ),
            ui.SelectField(
                name="assignee",
                label="Assignee",
                options=[
                    ui.Option(FILTER_ANY, value=""),
                    ui.Option(UNASSIGNED, value=UNASSIGNED_FILTER),
                    *[ui.Option(account.username, value=account.id) for account in accounts],
                ],
                value=assignee,
            ),
            _filter_select(
                "creator",
                "Creator",
                [ui.Option(account.username, value=account.id) for account in accounts],
                creator,
            ),
            _filter_select(
                "project",
                "Project",
                [ui.Option(item.name, value=str(item.id)) for item in projects],
                str(project or ""),
            ),
            _filter_select("repo", "Repo", _repo_options(repo_choices), str(repo or "")),
        ],
        blocks=[
            ui.Columns(
                [
                    ui.Section(
                        title=item.label,
                        blocks=[
                            ui.Cards(
                                layout="stack",
                                drop=ui.Action(
                                    label=f"Move to {item.label}",
                                    operation="set_status",
                                    arguments={"status": item.value},
                                    refresh="page",
                                ),
                                cards=cards[item],
                                empty=ui.EmptyState(
                                    "Nothing here",
                                    description=f"No ticket is in {item.label}.",
                                ),
                            )
                        ],
                    )
                    for item in Status
                ]
            )
        ],
    )


@ui.page("/tickets/{identifier}")
async def ticket(identifier: str):
    found = await Ticket.get_for_identifier(identifier)
    if not found:
        return ui.Page(
            identifier,
            blocks=[
                ui.EmptyState(
                    "No such ticket",
                    description=f"No ticket on this board has the identifier {identifier}.",
                    controls=[ui.Link("Board", page="board")],
                )
            ],
        )

    repos = await ProjectRepo.list_all()
    accounts = await Account.filter(kind=AccountKind.OPERATOR)
    account_names = {account.id: account.username for account in accounts}
    tickets = await Ticket.list_matching()
    thread = [
        ui.Card(
            title=comment.author.username,
            description=comment.created_at.isoformat(sep=" ", timespec="minutes"),
            blocks=[ui.Markdown(comment.body)],
        )
        for comment in found.comments
    ] or [ui.EmptyState("No comments yet", description="Say something about this ticket.")]
    build = await WorkItem.get_for_ticket_key(source="druks", ticket_key=found.identifier)
    controls = [ui.Link("Open build", subject=build)] if build else []

    return ui.Page(
        found.identifier,
        controls=controls,
        blocks=[
            ui.Columns(
                [
                    ui.Stack(
                        [
                            ui.Form(
                                fields=[
                                    ui.TextField(
                                        name="title",
                                        label="Title",
                                        value=found.title,
                                        is_required=True,
                                        placeholder="Title",
                                    ),
                                    ui.TextAreaField(
                                        name="description",
                                        label="Description",
                                        value=found.description,
                                        placeholder="Add a description…",
                                        rows=12,
                                        is_markdown=True,
                                    ),
                                ],
                                action=ui.Action(
                                    label="Save",
                                    operation="update_ticket",
                                    arguments={"identifier": found.identifier},
                                    refresh="none",
                                ),
                                submit="change",
                                layout="prose",
                            ),
                            ui.Section(
                                title="Comments",
                                # The name lets a new comment replace this section alone.
                                name="comments",
                                blocks=[
                                    *thread,
                                    ui.Form(
                                        title="Add a comment",
                                        fields=[
                                            ui.TextAreaField(
                                                name="body",
                                                label="Comment",
                                                is_required=True,
                                                is_markdown=True,
                                                rows=3,
                                            )
                                        ],
                                        action=ui.Action(
                                            label="Comment",
                                            operation="add_comment",
                                            arguments={"identifier": found.identifier},
                                            tone="primary",
                                            refresh="region",
                                        ),
                                    ),
                                ],
                            ),
                        ],
                        gap="large",
                    ),
                    ui.Stack(
                        [
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="status",
                                    label="Status",
                                    options=_status_options(),
                                    value=found.status,
                                    is_required=True,
                                ),
                                operation="set_status",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="priority",
                                    label="Priority",
                                    options=_priority_options(),
                                    value=found.priority,
                                ),
                                operation="update_ticket",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="assignee_id",
                                    label="Assignee",
                                    options=_assignee_options(accounts),
                                    value=found.assignee_id or "",
                                ),
                                operation="update_ticket",
                            ),
                            _live_form(
                                found,
                                ui.SelectField(
                                    name="repo_id",
                                    label="Repo",
                                    options=_repo_options(repos),
                                    value=str(found.repo_id),
                                ),
                                operation="update_ticket",
                            ),
                            _live_form(
                                found,
                                ui.MultiSelectField(
                                    name="blocked_by",
                                    label="Blocked by",
                                    options=[
                                        ui.Option(
                                            f"{ticket.identifier} {ticket.title}",
                                            value=ticket.identifier,
                                        )
                                        for ticket in tickets
                                        if ticket.id != found.id
                                    ],
                                    value=[blocker.identifier for blocker in found.blocked_by],
                                    is_searchable=True,
                                ),
                                operation="update_ticket",
                            ),
                            ui.Facts(
                                [
                                    ui.Fact("Identifier", value=ui.TextValue(found.identifier)),
                                    ui.Fact(
                                        "Created by",
                                        value=ui.TextValue(
                                            account_names.get(found.creator_id, UNATTRIBUTED)
                                        ),
                                    ),
                                    ui.Fact("Created", value=ui.TimeValue(found.created_at)),
                                    ui.Fact("Updated", value=ui.TimeValue(found.updated_at)),
                                ]
                            ),
                        ],
                        gap="small",
                    ),
                ],
                layout="sidebar",
            )
        ],
    )


@ui.page("/lessons")
async def lessons():
    session = db_session()
    since = datetime.now(UTC) - timedelta(days=CORRECTIONS_WINDOW_DAYS)
    # A correction is a gate reply that says something: the note of a plan review, or the
    # body of a request for changes on the work.
    correction_text = func.coalesce(
        func.nullif(Event.payload["result"]["note"].astext, ""),
        func.nullif(Event.payload["result"]["body"].astext, ""),
    )
    is_correction = (
        Event.app == "software_factory",
        Event.type == WorkflowEvent.RUNNING,
        correction_text.is_not(None),
    )
    corrections = (
        await session.execute(
            select(Event, correction_text)
            .where(*is_correction)
            .order_by(Event.id.desc())
            .limit(CORRECTIONS_SHOWN)
        )
    ).all()
    accepted_lessons = await Lesson.list_accepted(session)
    repeats = {
        repeat.event_id: repeat.lesson_id
        for repeat in await session.scalars(
            select(LessonRepeat).where(
                LessonRepeat.event_id.in_([event.id for event, _ in corrections])
            )
        )
    }
    recent_corrections = await session.scalar(
        select(func.count()).select_from(Event).where(*is_correction, Event.created_at >= since)
    )
    builds = await session.scalar(
        select(func.count(func.distinct(Event.payload["run"].astext))).where(
            Event.app == "software_factory",
            Event.payload["kind"].astext == "software_factory.build",
            Event.created_at >= since,
        )
    )
    marked_repeats = await session.scalar(
        select(func.count())
        .select_from(LessonRepeat)
        .join(Event, Event.id == LessonRepeat.event_id)
        .where(Event.created_at >= since)
    )
    lesson_options = [
        ui.Option(lesson.text.splitlines()[0], value=lesson.id) for lesson in accepted_lessons
    ]
    lesson_titles = {lesson.id: lesson.text.splitlines()[0] for lesson in accepted_lessons}
    per_build = recent_corrections / builds if builds else 0
    return ui.Page(
        "Lessons",
        blocks=[
            ui.Metrics(
                metrics=[
                    ui.Metric(
                        "Corrections",
                        ui.NumberValue(recent_corrections),
                        description=f"Notes at a gate, last {CORRECTIONS_WINDOW_DAYS} days.",
                    ),
                    ui.Metric(
                        "Per build",
                        ui.NumberValue(round(per_build, 2)),
                        description=f"Before the lessons: {CORRECTIONS_PER_BUILD_BASELINE}.",
                    ),
                    ui.Metric(
                        "Marked as repeats",
                        ui.NumberValue(marked_repeats),
                        description=(
                            "Corrections that repeat an accepted lesson. Before the lessons, "
                            f"{CORRECTION_REPEAT_SHARE_BASELINE:.0%} repeated an earlier one."
                        ),
                    ),
                    ui.Metric(
                        "Accepted lessons",
                        ui.NumberValue(len(accepted_lessons)),
                        description=f"The index holds {LESSON_INDEX_MAX_LINES} at most.",
                    ),
                ]
            ),
            ui.Callout(
                title="When to judge",
                text=(
                    f"Judge the lessons after {JUDGEMENT_BUILDS} builds or a month, whichever "
                    "comes first. If the corrections that repeat an accepted lesson are no "
                    "fewer than before the lessons, stop and rethink the feature. "
                    f"Builds in the last {CORRECTIONS_WINDOW_DAYS} days: {builds}."
                ),
            ),
            ui.Section(
                title="Corrections",
                blocks=[
                    ui.Cards(
                        layout="stack",
                        cards=[
                            ui.Card(
                                title=event.subject_key or event.payload["run"],
                                description=(
                                    f"{event.payload['gate']} · {event.created_at:%-d %b %H:%M} UTC"
                                ),
                                blocks=[
                                    ui.Quote(text),
                                    *(
                                        [
                                            ui.Text(
                                                "Repeats the lesson: "
                                                + lesson_titles.get(repeats[event.id], "(retired)")
                                            )
                                        ]
                                        if event.id in repeats
                                        else []
                                    ),
                                ],
                                controls=[
                                    ui.Action(
                                        label="Make a lesson",
                                        operation="accept_lesson",
                                        tone="primary",
                                        arguments={"event_id": event.id},
                                        fields=[
                                            ui.TextAreaField(
                                                name="text",
                                                label="Lesson",
                                                value=text,
                                                is_required=True,
                                            )
                                        ],
                                    ),
                                    *(
                                        [
                                            ui.Action(
                                                label="Repeats a lesson",
                                                operation="mark_repeat",
                                                arguments={"event_id": event.id},
                                                fields=[
                                                    ui.SelectField(
                                                        name="lesson_id",
                                                        label="Lesson",
                                                        options=lesson_options,
                                                        is_required=True,
                                                    )
                                                ],
                                            )
                                        ]
                                        if accepted_lessons
                                        else []
                                    ),
                                ],
                            )
                            for event, text in corrections
                        ],
                        empty=ui.EmptyState(
                            "No corrections yet",
                            description="A person's note at a gate shows here.",
                        ),
                    )
                ],
            ),
            ui.Section(
                title="Lessons",
                blocks=[
                    ui.Cards(
                        layout="stack",
                        cards=[
                            ui.Card(
                                title=lesson.text.splitlines()[0],
                                description=(
                                    f"Accepted by {lesson.accepted_by.username} · "
                                    f"{lesson.accepted_at:%-d %b} UTC"
                                ),
                                blocks=[ui.Markdown(lesson.text)],
                                controls=[
                                    ui.Action(
                                        label="Retire",
                                        operation="retire_lesson",
                                        tone="danger",
                                        confirm="Retire this lesson?",
                                        arguments={"lesson_id": lesson.id},
                                    )
                                ],
                            )
                            for lesson in accepted_lessons
                        ],
                        empty=ui.EmptyState(
                            "No lessons yet",
                            description="Make a lesson from a correction, or add one below.",
                        ),
                    )
                ],
            ),
            ui.Form(
                title="Add a lesson",
                description="A judgment that holds for every build, not a fact about the code.",
                fields=[
                    ui.TextAreaField(name="text", label="Lesson", is_required=True),
                    ui.TextField(
                        name="run_id", label="Run id", help_text="The run it comes from. Optional."
                    ),
                ],
                action=ui.Action(label="Keep lesson", operation="accept_lesson", tone="primary"),
            ),
        ],
    )
