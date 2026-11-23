from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import connect_service
from druks.contrib.software_factory import subscribers  # noqa: F401 (the import registers them)
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import ReviewWork
from druks.contrib.software_factory.enums import Resolution, Status
from druks.contrib.software_factory.exceptions import (
    TicketAlreadyMerged,
    TicketBlocked,
    TicketNotRoutable,
)
from druks.contrib.software_factory.models import Project, ProjectRepo, Ticket, WorkItem
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.workflows import Build
from druks.core.apis.exceptions import GitHubMergeConflictError, GitHubStackError
from druks.events.models import Event
from druks.services.exceptions import ServiceNotConnectedError
from druks.signals import publish
from druks.testing import seed_run
from sqlalchemy import select

from software_factory.factories import make_test_work_item, seed_build_run


async def _connect_github() -> None:
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem", "webhook_secret": "hook-secret"},
    )


def _ticket(item, **overrides) -> dict:
    ticket = {
        "source": item.source,
        "identifier": item.ticket_key,
        "status": "Ready",
        "title": item.title,
        "url": f"https://tracker.test/{item.ticket_key}",
        "project_name": "r",
        "labels": [],
        "actor_id": None,
        "assignee_id": None,
        "assignee_email": None,
        "assignee_name": None,
    }
    ticket.update(overrides)
    return ticket


async def test_dispatch_leaves_the_item_alone(druks_db, monkeypatch) -> None:
    """Dispatch starts the build and changes nothing else. The scheduled reaction
    clears the previous attempt (test_lane_reactions)."""
    await _connect_github()
    await seed_run(druks_db, kind=Build.kind, run_id="run-old")
    await seed_run(druks_db, kind=Build.kind, run_id="run-new")
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-2")
    await item.update(pr_number=7, branch="agent/old")
    await item.resolve(Resolution.CLOSED, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        return "run-new"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))
    await Build.dispatch(ticket=_ticket(item))

    assert item.pr_number == 7
    assert item.branch == "agent/old"
    assert item.resolution == "closed"


async def test_dispatch_announces_and_refuses_without_github(druks_db, monkeypatch) -> None:
    """Without the GitHub identity, dispatch announces the refusal and starts nothing."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-8")
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with pytest.raises(ServiceNotConnectedError):
        await Build.dispatch(ticket=_ticket(item))

    assert not started
    events = list(await druks_db.scalars(select(Event)))
    assert len(events) == 1
    assert events[0].type == "build.rejected"
    assert events[0].app == "software_factory"
    assert events[0].subject_id == str(item.id)
    assert "not connected" in events[0].payload["reason"]
    assert "run" not in events[0].payload


async def test_the_tracker_funnel_swallows_the_missing_identity(druks_db, monkeypatch) -> None:
    """End to end through publish: a ticket transition on an unconnected appliance
    raises nothing, so the webhook never goes into redelivery."""
    settings = await SoftwareFactory.settings()
    item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-9", source=settings.tracker
    )
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await publish(
        "ticket.transitioned",
        payload=_ticket(item, source=settings.tracker, status=settings.trigger_status),
    )

    assert not started


async def test_dispatch_refuses_a_merged_ticket_before_the_identity_check(
    druks_db, monkeypatch
) -> None:
    """A merged item refuses a new build before the identity check runs."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-10")
    await item.update(pr_number=7, branch="agent/old")
    await item.resolve(Resolution.MERGED, at=datetime.now(UTC))

    async def fake_start(cls, **kwargs):
        raise AssertionError("a merged item never starts")

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with pytest.raises(TicketAlreadyMerged):
        await Build.dispatch(ticket=_ticket(item))


async def test_dispatch_syncs_a_parked_build_instead_of_restarting(druks_db, monkeypatch) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-12")
    run = await seed_build_run(
        druks_db, work_item_id=item.id, state="parked", input_gate=ReviewWork.name
    )
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "should-not-run"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    assert await Build.dispatch(ticket=_ticket(item)) == run.id
    assert not started


async def test_dispatch_refuses_an_unroutable_ticket_before_the_identity_check(
    druks_db, monkeypatch
) -> None:
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    with pytest.raises(TicketNotRoutable):
        await Build.dispatch(
            ticket={
                "source": "linear",
                "identifier": "ACME-11",
                "status": "Ready",
                "title": "t",
                "url": "https://tracker.test/ACME-11",
                "project_name": "no-such-project",
                "labels": [],
                "actor_id": None,
                "assignee_id": None,
                "assignee_email": None,
                "assignee_name": None,
            }
        )

    assert not started


async def test_update_clears_nullable_with_none_and_skips_omitted(druks_db) -> None:
    """update(pr_number=None) clears the column. An omitted branch keeps its value."""
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-4")
    await item.update(pr_number=9, branch="agent/keep")

    await item.update(pr_number=None)

    assert not item.pr_number
    assert item.branch == "agent/keep"


@pytest.mark.parametrize(
    ("source", "ticket_key", "held", "branch"),
    [
        ("linear", "ACME-5", {"agent/ACME-5", "agent/ACME-5-2"}, "agent/ACME-5-3"),
        ("github", "o/r#42", set(), "agent/issue-42"),
    ],
)
async def test_create_branch_takes_a_name_github_does_not_hold(
    druks_db, monkeypatch, source, ticket_key, held, branch
) -> None:
    item = await make_test_work_item(repo="o/r", title="t", source=source, ticket_key=ticket_key)
    created = []

    async def has_branch(repo, name):
        return name in held

    async def create_branch(repo, name, base=None):
        created.append((repo, name))

    async def get_client(repo):
        return SimpleNamespace(has_branch=has_branch, create_branch=create_branch)

    monkeypatch.setattr("druks.contrib.software_factory.models.Github.get_client", get_client)

    await item.create_branch()

    assert item.branch == branch
    assert created == [("o/r", branch)]


class _FakeTracker(Tracker):
    """Tickets that block one another, with no HTTP. ``blocking`` holds the tickets the
    Done ticket blocks, and each of them is blocked by ``blocked_by``."""

    def __init__(self, *, blocked_by: dict[str, bool], blocking: dict[str, dict] | None = None):
        super().__init__(status_names={})
        self.blocked_by = blocked_by
        self.blocking = blocking or {}

    async def get_ticket(self, key):
        # The Done subscriber takes the blocked tickets from get_blocking, whole.
        raise AssertionError(f"read {key} again")

    async def get_blocked_by(self, key):
        return self.blocked_by

    async def get_blocking(self, key):
        return list(self.blocking.values())

    async def get_status_name(self, key):
        return self.blocking.get(key, {}).get("status", "")

    async def set_status(self, key, status):
        return

    async def aclose(self):
        return


@pytest.fixture
def started(monkeypatch) -> list[str]:
    keys = []

    async def start(cls, *, subject, **kwargs):
        keys.append(subject.ticket_key)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(start))
    return keys


def _use_tracker(monkeypatch, tracker: Tracker) -> None:
    async def get_tracker(cls, source=None):
        return tracker

    monkeypatch.setattr(SoftwareFactory, "get_tracker", classmethod(get_tracker))


def _use_pull_request(monkeypatch, *, draft: bool = False, state: str = "open") -> AsyncMock:
    """GitHub's answer about the pull request of a blocker."""
    get_pull_request = AsyncMock(return_value={"state": state, "draft": draft})

    async def get_client(repo):
        return SimpleNamespace(get_pull_request=get_pull_request)

    monkeypatch.setattr("druks.contrib.software_factory.workflows.Github.get_client", get_client)
    return get_pull_request


async def test_dispatch_waits_on_an_open_blocker(druks_db, monkeypatch, started) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-20")
    tracker = _FakeTracker(blocked_by={"ACME-18": True, "ACME-19": False})
    _use_tracker(monkeypatch, tracker)

    with pytest.raises(TicketBlocked, match="ACME-20 is blocked by ACME-18, so"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []
    [event] = await druks_db.scalars(select(Event))
    assert event.type == "build.waiting"
    assert event.payload["blockers"] == ["ACME-18"]


async def test_dispatch_starts_when_the_open_blocker_has_an_open_pr(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    blocker = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-40")
    await blocker.update(pr_number=41)
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-42")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-40": True}))
    _use_pull_request(monkeypatch)

    await Build.dispatch(ticket=_ticket(item))

    assert started == ["ACME-42"]


async def test_dispatch_waits_on_a_blocker_whose_pr_is_still_a_draft(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    blocker = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-36")
    await blocker.update(pr_number=37)
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-38")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-36": True}))
    _use_pull_request(monkeypatch, draft=True)

    with pytest.raises(TicketBlocked):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def test_dispatch_waits_on_a_blocker_whose_pr_closed_without_a_merge(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    blocker = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-43")
    await blocker.update(pr_number=44)
    await blocker.resolve(Resolution.CLOSED, at=datetime.now(UTC))
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-45")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-43": True}))

    with pytest.raises(TicketBlocked):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def test_dispatch_names_every_open_blocker_while_one_of_them_holds_the_ticket(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    blocker_with_pr = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-46")
    await blocker_with_pr.update(pr_number=47)
    await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-48")
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-49")
    _use_tracker(
        monkeypatch, _FakeTracker(blocked_by={"ACME-46": True, "ACME-48": True, "ACME-50": True})
    )

    with pytest.raises(TicketBlocked):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []
    [event] = await druks_db.scalars(select(Event))
    assert event.payload["blockers"] == ["ACME-46", "ACME-48", "ACME-50"]


async def _a_blocker_with_a_dependent(monkeypatch):
    settings = await SoftwareFactory.settings()
    blocker = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-51", source=settings.tracker
    )
    await blocker.update(branch="agent/ACME-51", pr_number=53)
    dependent = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-52", source=settings.tracker
    )
    _use_tracker(
        monkeypatch,
        _FakeTracker(
            blocked_by={"ACME-51": True},
            blocking={"ACME-52": _ticket(dependent, status=settings.trigger_status)},
        ),
    )
    _use_pull_request(monkeypatch)
    return blocker


async def test_a_blockers_pr_leaving_draft_starts_each_ticket_it_blocks(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    await _a_blocker_with_a_dependent(monkeypatch)

    await publish("pr.ready_for_review", repo="o/r", pr_number=53, payload={"is_fork": False})

    assert started == ["ACME-52"]


async def test_a_blockers_pr_opening_starts_no_ticket(druks_db, monkeypatch, started) -> None:
    await _connect_github()
    blocker = await _a_blocker_with_a_dependent(monkeypatch)
    # The PR number is not stored yet, so the opening fills it and announces it.
    await blocker.update(pr_number=None)

    await publish(
        "pr.opened",
        repo="o/r",
        pr_number=53,
        payload={"branch": "agent/ACME-51", "base_branch": "main", "is_fork": False},
    )

    assert started == []


async def test_a_done_ticket_starts_each_ticket_it_blocks_at_the_trigger_status(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    settings = await SoftwareFactory.settings()
    ready_item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-23", source=settings.tracker
    )
    resting_item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-24", source=settings.tracker
    )
    blocked_tickets = {
        "ACME-23": _ticket(ready_item, status=settings.trigger_status),
        "ACME-24": _ticket(resting_item, status=settings.resting_status),
    }
    tracker = _FakeTracker(blocked_by={"ACME-22": False}, blocking=blocked_tickets)
    _use_tracker(monkeypatch, tracker)

    await publish(
        "ticket.transitioned",
        payload=_ticket(ready_item, identifier="ACME-22", status=settings.done_status),
    )

    assert started == ["ACME-23"]


async def test_a_done_ticket_leaves_a_ticket_with_another_open_blocker(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    settings = await SoftwareFactory.settings()
    item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-26", source=settings.tracker
    )
    tracker = _FakeTracker(
        blocked_by={"ACME-25": False, "ACME-27": True},
        blocking={"ACME-26": _ticket(item, status=settings.trigger_status)},
    )
    _use_tracker(monkeypatch, tracker)

    await publish(
        "ticket.transitioned",
        payload=_ticket(item, identifier="ACME-25", status=settings.done_status),
    )

    assert started == []


async def test_a_blocker_at_another_status_starts_nothing(druks_db, monkeypatch, started) -> None:
    await _connect_github()
    settings = await SoftwareFactory.settings()
    item = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-29", source=settings.tracker
    )
    tracker = _FakeTracker(
        blocked_by={},
        blocking={"ACME-29": _ticket(item, status=settings.trigger_status)},
    )
    _use_tracker(monkeypatch, tracker)

    await publish(
        "ticket.transitioned",
        payload=_ticket(item, identifier="ACME-28", status=settings.in_review_status),
    )

    assert started == []


async def test_a_board_ticket_waits_for_its_blocker_and_starts_when_it_is_done(
    druks_db, started
) -> None:
    await _connect_github()
    await SoftwareFactory.override_setting("tracker", "druks")
    # A value kept from another tracker. The board's done status is fixed.
    await SoftwareFactory.override_setting("done_status", "Completed")
    project = await Project.create(name="Acme")
    project_repo = await ProjectRepo.create(project_id=project.id, full_name="acme/api")
    blocker = await Ticket.create(project_repo=project_repo, title="Define the contract")
    ticket = await Ticket.create(project_repo=project_repo, title="Use the contract")
    ticket.blocked_by = [blocker]

    await ticket.transition(Status.READY_FOR_AGENT)

    assert started == []

    await blocker.transition(Status.DONE)

    assert started == [ticket.identifier]


async def test_a_waiting_board_ticket_starts_when_an_edit_removes_its_open_blocker(
    druks_db, started
) -> None:
    await _connect_github()
    await SoftwareFactory.override_setting("tracker", "druks")
    project = await Project.create(name="Acme")
    project_repo = await ProjectRepo.create(project_id=project.id, full_name="acme/api")
    blocker = await Ticket.create(project_repo=project_repo, title="Define the contract")
    ticket = await Ticket.create(project_repo=project_repo, title="Use the contract")
    ticket.blocked_by = [blocker]
    await ticket.transition(Status.READY_FOR_AGENT)

    await ticket.replace_blockers([])

    assert started == [ticket.identifier]


async def test_dispatch_passes_the_done_blockers_to_the_build(druks_db, monkeypatch) -> None:
    await _connect_github()
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-32")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-30": False, "ACME-31": False}))
    started = []

    async def fake_start(cls, **kwargs):
        started.append(kwargs)
        return "run-x"

    monkeypatch.setattr(Build, "start", classmethod(fake_start))

    await Build.dispatch(ticket=_ticket(item))

    assert started[0]["blockers"] == ["ACME-30", "ACME-31"]


async def test_each_blocker_gives_its_merged_pr_or_its_key(druks_db) -> None:
    blocker_item = await make_test_work_item(
        repo="o/api", title="Add the endpoint", ticket_key="ACME-33"
    )
    await blocker_item.update(pr_number=12)
    await blocker_item.resolve(Resolution.MERGED, at=datetime.now(UTC))
    build = Build()
    build.input = Build._run_input_model(blockers=("ACME-33", "ACME-34"))
    build.subject = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-35")

    blockers = await Build._load_blockers.__wrapped__(build)

    assert blockers == [
        {"repo": "o/api", "pr_number": 12, "title": "Add the endpoint"},
        {"ticket_key": "ACME-34", "pr_number": None},
    ]


async def test_an_open_blocker_pr_in_this_repo_carries_its_branch(druks_db) -> None:
    same_repo = await make_test_work_item(repo="o/r", title="Same", ticket_key="ACME-60")
    await same_repo.update(branch="agent/ACME-60", pr_number=61)
    other_repo = await make_test_work_item(repo="o/api", title="Other", ticket_key="ACME-62")
    await other_repo.update(branch="agent/ACME-62", pr_number=63)
    build = Build()
    build.input = Build._run_input_model(blockers=("ACME-60", "ACME-62"))
    build.subject = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-64")

    build._blockers = await Build._load_blockers.__wrapped__(build)

    assert build._blockers == [
        {"repo": "o/r", "pr_number": 61, "title": "Same", "branch": "agent/ACME-60"},
        {"repo": "o/api", "pr_number": 63, "title": "Other"},
    ]
    assert build._base_branch == "agent/ACME-60"


async def test_create_branch_starts_at_the_given_base(druks_db, monkeypatch) -> None:
    item = await make_test_work_item(repo="o/r", title="t", source="linear", ticket_key="ACME-65")
    bases = []

    async def has_branch(repo, name):
        return False

    async def create_branch(repo, name, base=None):
        bases.append(base)

    async def get_client(repo):
        return SimpleNamespace(has_branch=has_branch, create_branch=create_branch)

    monkeypatch.setattr("druks.contrib.software_factory.models.Github.get_client", get_client)

    await item.create_branch(base="agent/ACME-60")

    assert bases == ["agent/ACME-60"]


async def test_dispatch_waits_while_several_open_prs_in_its_repo_could_be_its_base(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    for key, number in (("ACME-66", 67), ("ACME-68", 69)):
        blocker = await make_test_work_item(repo="o/r", title="t", ticket_key=key)
        await blocker.update(pr_number=number)
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-70")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-66": True, "ACME-68": True}))

    with pytest.raises(TicketBlocked, match="ACME-66, ACME-68"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def test_dispatch_waits_on_an_open_pr_in_another_repo(druks_db, monkeypatch, started) -> None:
    await _connect_github()
    other_repo_blocker = await make_test_work_item(repo="o/api", title="t", ticket_key="ACME-73")
    await other_repo_blocker.update(pr_number=74)
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-75")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-73": True}))

    with pytest.raises(TicketBlocked, match="ACME-73"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def _a_blocker_with_two_dependents(monkeypatch, *, other: dict) -> WorkItem:
    """A blocker with an open PR, a ticket that it blocks, and the ``other`` ticket that it
    blocks too."""
    await _connect_github()
    blocker = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-85")
    await blocker.update(pr_number=86)
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-87")
    _use_tracker(
        monkeypatch,
        _FakeTracker(
            blocked_by={"ACME-85": True},
            blocking={"ACME-87": _ticket(item), other["identifier"]: other},
        ),
    )
    _use_pull_request(monkeypatch)
    return item


async def test_dispatch_waits_when_its_blocker_blocks_another_ticket_in_its_repo(
    druks_db, monkeypatch, started
) -> None:
    other = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-88")
    item = await _a_blocker_with_two_dependents(monkeypatch, other=_ticket(other))

    with pytest.raises(TicketBlocked, match="ACME-85"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def test_dispatch_waits_when_its_blocker_blocks_a_ticket_that_routes_to_its_repo(
    druks_db, monkeypatch, started
) -> None:
    await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-80")
    unrouted = {
        **_ticket(SimpleNamespace(source="linear", ticket_key="ACME-89", title="t")),
        "project_name": "o/r",
    }
    item = await _a_blocker_with_two_dependents(monkeypatch, other=unrouted)

    with pytest.raises(TicketBlocked, match="ACME-85"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def test_dispatch_starts_when_the_other_ticket_that_its_blocker_blocks_is_in_another_repo(
    druks_db, monkeypatch, started
) -> None:
    other = await make_test_work_item(repo="o/api", title="t", ticket_key="ACME-88")
    item = await _a_blocker_with_two_dependents(monkeypatch, other=_ticket(other))

    await Build.dispatch(ticket=_ticket(item))

    assert started == ["ACME-87"]


async def test_dispatch_waits_for_the_merges_when_two_blockers_have_work_in_its_repo(
    druks_db, monkeypatch, started
) -> None:
    await _connect_github()
    merged_blocker = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-76")
    await merged_blocker.update(pr_number=77)
    await merged_blocker.resolve(Resolution.MERGED, at=datetime.now(UTC))
    open_blocker = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-78")
    await open_blocker.update(pr_number=79)
    item = await make_test_work_item(repo="o/r", title="t", ticket_key="ACME-84")
    _use_tracker(monkeypatch, _FakeTracker(blocked_by={"ACME-78": True, "ACME-76": False}))
    _use_pull_request(monkeypatch)

    with pytest.raises(TicketBlocked, match="ACME-78"):
        await Build.dispatch(ticket=_ticket(item))

    assert started == []


async def _set_up_a_stack(monkeypatch, *, base: str):
    """A blocker and a dependent, both with open PRs, and a GitHub client whose PR of the
    dependent targets ``base``. The blocker's PR targets main."""
    settings = await SoftwareFactory.settings()
    blocker = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-80", source=settings.tracker
    )
    await blocker.update(branch="agent/ACME-80", pr_number=81)
    dependent = await make_test_work_item(
        repo="o/r", title="t", ticket_key="ACME-82", source=settings.tracker
    )
    await dependent.update(branch="agent/ACME-82", pr_number=83)
    _use_tracker(
        monkeypatch,
        _FakeTracker(
            blocked_by={},
            blocking={"ACME-82": _ticket(dependent, status=settings.in_review_status)},
        ),
    )
    github = SimpleNamespace(
        get_pull_request=AsyncMock(
            side_effect=lambda repo, number: {"base": {"ref": "main" if number == 81 else base}}
        ),
        merge_branch=AsyncMock(),
        stack_on=AsyncMock(),
    )

    async def get_client(repo):
        return github

    monkeypatch.setattr("druks.contrib.software_factory.subscribers.Github.get_client", get_client)
    return github


async def _push_to_the_blocker() -> None:
    await publish(
        "pr.synchronized",
        repo="o/r",
        pr_number=81,
        payload={"branch": "agent/ACME-80", "is_fork": False},
    )


async def test_a_push_to_a_blocker_updates_the_pr_that_targets_its_branch(
    druks_db, monkeypatch
) -> None:
    github = await _set_up_a_stack(monkeypatch, base="agent/ACME-80")

    await _push_to_the_blocker()

    github.merge_branch.assert_awaited_once_with("o/r", base="agent/ACME-82", head="agent/ACME-80")
    [event] = await druks_db.scalars(select(Event).where(Event.type == "build.base_updated"))
    assert event.payload == {"blocker": "ACME-80"}


async def test_a_push_to_a_blocker_leaves_a_pr_that_targets_another_branch(
    druks_db, monkeypatch
) -> None:
    github = await _set_up_a_stack(monkeypatch, base="main")

    await _push_to_the_blocker()

    github.merge_branch.assert_not_awaited()


async def test_a_conflicting_update_stops_the_dependent_build_with_a_reason(
    druks_db, monkeypatch
) -> None:
    github = await _set_up_a_stack(monkeypatch, base="agent/ACME-80")
    github.merge_branch.side_effect = GitHubMergeConflictError("o/r: agent/ACME-80")
    dependent = await WorkItem.get_for_ticket_key(source="linear", ticket_key="ACME-82")
    run = await seed_build_run(druks_db, work_item_id=dependent.id, state="running")

    await _push_to_the_blocker()

    await druks_db.refresh(run)
    assert run.failure == (
        "merge conflict with ACME-80 (#81), whose branch agent/ACME-80 has new commits"
    )


async def _open_the_dependents_pr(*, base_branch: str, branch: str = "agent/ACME-82") -> None:
    await publish(
        "pr.opened",
        repo="o/r",
        pr_number=83,
        payload={"branch": branch, "base_branch": base_branch, "is_fork": False},
    )


async def test_a_pr_opened_on_a_blockers_branch_joins_its_stack(druks_db, monkeypatch) -> None:
    github = await _set_up_a_stack(monkeypatch, base="agent/ACME-80")

    await _open_the_dependents_pr(base_branch="agent/ACME-80")

    github.stack_on.assert_awaited_once_with("o/r", 83, below=81)


async def test_a_pr_opened_on_the_default_branch_joins_no_stack(druks_db, monkeypatch) -> None:
    github = await _set_up_a_stack(monkeypatch, base="main")

    await _open_the_dependents_pr(base_branch="main")

    github.stack_on.assert_not_awaited()


async def test_a_pr_no_build_opened_joins_no_stack(druks_db, monkeypatch) -> None:
    github = await _set_up_a_stack(monkeypatch, base="agent/ACME-80")

    await _open_the_dependents_pr(base_branch="agent/ACME-80", branch="feature/other")

    github.stack_on.assert_not_awaited()


async def test_a_pr_that_github_will_not_stack_is_left_as_it_is(
    druks_db, monkeypatch, caplog
) -> None:
    github = await _set_up_a_stack(monkeypatch, base="agent/ACME-80")
    github.stack_on.side_effect = GitHubStackError("o/r#83 on #81: already part of a stack")

    await _open_the_dependents_pr(base_branch="agent/ACME-80")

    assert "already part of a stack" in caplog.text


async def test_an_edited_pr_wakes_the_build_that_waits_on_its_base(druks_db, monkeypatch) -> None:
    await _set_up_a_stack(monkeypatch, base="agent/ACME-80")
    answer = AsyncMock()
    monkeypatch.setattr(Build, "answer", answer)

    await publish("pr.edited", repo="o/r", pr_number=83, payload={"is_fork": False})

    [(item, topic)] = [call.args for call in answer.await_args_list]
    assert (item.ticket_key, topic) == ("ACME-82", "pr.changed")


async def test_a_review_wakes_the_build_that_waits_for_its_blocker(druks_db, monkeypatch) -> None:
    await _set_up_a_stack(monkeypatch, base="agent/ACME-80")
    answer = AsyncMock()
    monkeypatch.setattr(Build, "answer", answer)

    await publish(
        "pr.review_submitted",
        repo="o/r",
        pr_number=83,
        payload={
            "branch": "agent/ACME-82",
            "action": "request_changes",
            "reviewer": "paulo",
            "author_can_write": True,
            "body": "Rename it.",
            "is_latest_commit": True,
        },
    )

    [call] = answer.await_args_list
    item, topic = call.args
    assert (item.ticket_key, topic, call.kwargs) == (
        "ACME-82",
        "pr.changed",
        {"action": "request_changes", "reviewer": "paulo", "body": "Rename it."},
    )


async def _blocker_pr_closes_without_a_merge() -> None:
    await publish(
        "pr.closed",
        repo="o/r",
        pr_number=81,
        payload={"branch": "agent/ACME-80", "merged": False, "resolved_at": datetime.now(UTC)},
    )


async def test_a_blocker_closed_without_a_merge_stops_the_builds_of_the_tickets_it_blocks(
    druks_db, monkeypatch
) -> None:
    await _set_up_a_stack(monkeypatch, base="agent/ACME-80")
    dependent = await WorkItem.get_for_ticket_key(source="linear", ticket_key="ACME-82")
    run = await seed_build_run(druks_db, work_item_id=dependent.id, state="running")

    await _blocker_pr_closes_without_a_merge()

    await druks_db.refresh(run)
    assert run.failure == "ACME-80 (#81) closed without a merge"
