from contextlib import asynccontextmanager
from types import SimpleNamespace

from dbos import DBOS
from druks import workflows
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import (
    CiReport,
    FindingOutput,
    ImplementationOutput,
    ReviewReport,
    TriageOutput,
)
from druks.contrib.software_factory.enums import HumanFeedbackAction
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.workflows import Build
from druks.harnesses.exceptions import HarnessSandboxProvisioningError
from druks.workflows import WaitTimeout


def _flow(*, red: bool, asked: bool = False, feedback: str | None = None) -> Build:
    flow = Build()
    flow.journal = SimpleNamespace(
        plan_size=None,
        red_ci_report=CiReport(head_sha="5e6f7a8b", conclusion="red") if red else None,
        requested_changes=object() if asked else None,
        revision_request=feedback,
    )
    return flow


async def test_a_red_ci_round_resumes_the_implementers_session(monkeypatch):
    flow = _flow(red=True)
    seen = []

    async def implement(*, effort=None, resume=None):
        seen.append(resume)
        return SimpleNamespace(status="success", check_command=None)

    monkeypatch.setattr(SoftwareFactory, "implement", implement)

    await flow.implement()

    assert seen == [flow._report_red_ci]


async def test_a_round_with_no_red_ci_starts_a_fresh_call(monkeypatch):
    flow = _flow(red=False)
    seen = []

    async def implement(*, effort=None, resume=None):
        seen.append(resume)
        return SimpleNamespace(status="success", check_command=None)

    monkeypatch.setattr(SoftwareFactory, "implement", implement)

    await flow.implement()

    assert seen == [None]


async def test_the_resume_message_carries_the_red_ci_rules_of_the_prompt():
    flow = Build()
    flow.subject = SimpleNamespace(repo="acme/widget")
    flow.journal = BuildJournal()
    flow.journal.add(CiReport(head_sha="5e6f7a8b", conclusion="red"))

    message = await flow._report_red_ci(workspace=None)

    assert "CI on `5e6f7a8b` is red" in message
    assert "push nothing" in message


async def test_the_wait_for_ci_keeps_the_box_and_reads_again_after_each_timeout(monkeypatch):
    flow = Build()
    flow.journal = SimpleNamespace(add=lambda report: None)
    verdicts = iter([None, None, CiReport(head_sha="5e6f7a8b", conclusion="green")])
    waits = []

    async def now():
        return 0.0

    async def read_ci(head_sha, started_at):
        return next(verdicts)

    async def wait(topic, *, label, ttl_seconds, hold_box):
        assert hold_box
        waits.append((topic, label))
        raise WaitTimeout(topic)

    async def sleep(*args, **kwargs):
        raise AssertionError("a sleep releases the box, and the resume needs it")

    monkeypatch.setattr(flow, "_now", now)
    monkeypatch.setattr(flow, "_read_ci", read_ci)
    monkeypatch.setattr(flow, "wait", wait)
    monkeypatch.setattr(flow, "sleep", sleep)

    report = await flow._wait_for_ci("5e6f7a8b")

    assert report.conclusion == "green"
    assert waits == [("check.completed", "Waiting for CI")] * 2


def _wait_flow(monkeypatch, *, host, lease, events):
    flow = Build()
    flow._host = host

    async def set_event(key, value):
        events.append(("event", key, value))

    async def recv(topic, timeout_seconds):
        events.append(("recv", topic))
        return {"conclusion": "success"}

    async def set_run_phase(label):
        events.append(("phase", label))

    async def release(host_id):
        events.append(("release", host_id))

    monkeypatch.setattr(DBOS, "set_event_async", set_event)
    monkeypatch.setattr(DBOS, "recv_async", recv)
    monkeypatch.setattr(workflows, "set_run_phase", set_run_phase)
    monkeypatch.setattr(workflows.sandbox_client, "lease", lease)
    monkeypatch.setattr(workflows.sandbox_client, "release", release)
    return flow


async def test_a_wait_renews_the_lease_of_the_box_it_keeps(monkeypatch):
    events = []

    @asynccontextmanager
    async def lease(*, host_id):
        events.append(("lease", host_id))
        yield
        events.append(("lease released", host_id))

    flow = _wait_flow(monkeypatch, host=SimpleNamespace(id="host-1"), lease=lease, events=events)

    await flow.wait("check.completed", label="Waiting for CI", ttl_seconds=60, hold_box=True)

    kinds = [event[0] for event in events]
    assert kinds.index("lease") < kinds.index("recv") < kinds.index("lease released")
    assert ("release", "host-1") not in events


async def test_a_wait_with_no_box_takes_no_lease(monkeypatch):
    events = []

    @asynccontextmanager
    async def lease(*, host_id):
        raise AssertionError("there is no box to renew")
        yield

    flow = _wait_flow(monkeypatch, host=None, lease=lease, events=events)

    await flow.wait("check.completed", label="Waiting for CI", ttl_seconds=60, hold_box=True)

    assert ("recv", "check.completed") in events


async def test_a_wait_releases_a_box_that_is_already_gone_and_still_waits(monkeypatch):
    events = []

    @asynccontextmanager
    async def lease(*, host_id):
        raise HarnessSandboxProvisioningError("gone")
        yield

    flow = _wait_flow(monkeypatch, host=SimpleNamespace(id="host-1"), lease=lease, events=events)

    facts = await flow.wait(
        "check.completed", label="Waiting for CI", ttl_seconds=60, hold_box=True
    )

    assert facts == {"conclusion": "success"}
    assert ("release", "host-1") in events
    assert flow._host is None


async def test_a_wait_that_does_not_ask_for_the_box_takes_no_lease(monkeypatch):
    events = []

    @asynccontextmanager
    async def lease(*, host_id):
        raise AssertionError("only a wait that holds the box renews its lease")
        yield

    flow = _wait_flow(monkeypatch, host=SimpleNamespace(id="host-1"), lease=lease, events=events)

    await flow.wait("review.completed", label="Waiting for the review", ttl_seconds=60)

    assert ("recv", "review.completed") in events
    assert ("release", "host-1") not in events


async def test_a_wait_that_does_not_hold_the_box_takes_no_lease(monkeypatch):
    events = []

    @asynccontextmanager
    async def lease(*, host_id):
        raise AssertionError("only a wait that needs the box renews its lease")
        yield

    flow = _wait_flow(monkeypatch, host=SimpleNamespace(id="host-1"), lease=lease, events=events)

    await flow.wait("check.completed", label="Waiting for CI", ttl_seconds=60)

    assert ("recv", "check.completed") in events


async def test_a_round_that_a_review_asked_for_resumes_the_implementers_session(monkeypatch):
    flow = _flow(red=False, asked=True)
    seen = []

    async def implement(*, effort=None, resume=None):
        seen.append(resume)
        return SimpleNamespace(status="success", check_command=None)

    monkeypatch.setattr(SoftwareFactory, "implement", implement)

    await flow.implement()

    assert seen == [flow._report_requested_changes]


async def test_the_resume_message_carries_the_findings_of_the_review():
    flow = Build()
    flow.subject = SimpleNamespace(repo="acme/widget")
    flow.journal = BuildJournal()
    flow.journal.add(
        ImplementationOutput.model_validate(
            {
                "type": "result",
                "status": "success",
                "base_sha": "a",
                "head_sha": "5e6f7a8b",
                "commit_sha": "5e6f7a8b",
                "pr_number": 1,
                "files_changed": [],
                "acceptance_results": [],
                "checks": [],
                "known_risks": [],
                "summary": "did it",
                "workspace_path": "/repo",
                "workspace_retention": None,
            }
        )
    )
    flow.journal.add(
        ReviewReport(
            decision="request_changes",
            summary="The counter is wrong.",
            findings=[
                FindingOutput(
                    severity="high",
                    summary="Off by one",
                    evidence="range stops early",
                    path="app/notes.py",
                    line=12,
                    start_line=None,
                )
            ],
            context_repos=[],
        )
    )

    message = await flow._report_requested_changes(workspace=None)

    assert "`5e6f7a8b`" in message
    assert "The counter is wrong." in message
    assert "**high** Off by one (`app/notes.py:12`): range stops early" in message
    # The rules of a revision that the first prompt gave stay in force.
    assert "Dismiss only the reviews" in message
    assert "republish the plan block" in message


async def _resume_chosen(monkeypatch, flow):
    seen = []

    async def implement(*, effort=None, resume=None):
        seen.append(resume)
        return SimpleNamespace(status="success", check_command=None)

    monkeypatch.setattr(SoftwareFactory, "implement", implement)
    await flow.implement()
    return seen


async def test_a_round_that_a_person_asked_for_resumes_the_implementers_session(monkeypatch):
    flow = _flow(red=False, feedback="rename X to Y")

    assert await _resume_chosen(monkeypatch, flow) == [flow._report_revision_request]


async def test_red_ci_and_then_a_review_come_before_a_persons_request(monkeypatch):
    red = _flow(red=True, feedback="rename X to Y")
    asked = _flow(red=False, asked=True, feedback="rename X to Y")

    assert await _resume_chosen(monkeypatch, red) == [red._report_red_ci]
    assert await _resume_chosen(monkeypatch, asked) == [asked._report_requested_changes]


async def test_the_resume_message_carries_the_triaged_instructions_of_the_person():
    flow = Build()
    flow.subject = SimpleNamespace(repo="acme/widget")
    flow.journal = BuildJournal()
    flow.journal.add(
        TriageOutput(
            action=HumanFeedbackAction.CHANGE_REQUIRED,
            body="rename the flag",
            question="",
            implementation_instructions="rename X to Y",
        )
    )

    message = await flow._report_revision_request(workspace=None)

    assert "rename X to Y" in message
    assert "dismiss only the reviews" in message
    assert "Republish the plan block" in message
