from itertools import cycle
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.constants import (
    BLOCKER_POLL_SECONDS,
    BLOCKER_TTL_SECONDS,
    MERGE_POLL_SECONDS,
    STACK_RETRY_SECONDS,
)
from druks.contrib.software_factory.contracts import (
    AcceptanceCriterionOutput,
    CiReport,
    PlanData,
    PlanOutput,
    QuestionOptionOutput,
    QuestionOutput,
    ReviewOutput,
    ReviewReport,
    ReviewWork,
    TicketOutput,
)
from druks.contrib.software_factory.enums import ReviewDecision
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.policy import PlanGate, RepoPolicy
from druks.contrib.software_factory.ticketing.base import Tracker
from druks.contrib.software_factory.workflows import Build, PullRequestReview
from druks.core.apis.exceptions import GitHubMergeConflictError
from druks.notifications.services import get_rendered_ask
from druks.workflows import FatalError, OperatorReply, Run, WaitTimeout

from software_factory.factories import make_test_work_item


def _flow(*, plan_gate: PlanGate = "human") -> Build:
    flow = Build()
    # plan_approval is undeclared, so the workflow setting resolves the gate.
    flow._policy = RepoPolicy()
    flow._settings = Build.Settings(plan_gate=plan_gate)
    return flow


def _fake_plans(monkeypatch, *plans: PlanData) -> list[dict]:
    passes: list[dict] = []
    supply = iter(plans)

    async def fake_plan_agent(**kwargs):
        passes.append(kwargs)
        return next(supply)

    monkeypatch.setattr(SoftwareFactory, "generate_plan", fake_plan_agent)
    return passes


def _fake_grades(monkeypatch, *grades: ReviewOutput) -> None:
    supply = iter(grades)

    async def fake_review_agent():
        return next(supply)

    monkeypatch.setattr(SoftwareFactory, "review_plan", fake_review_agent)


def _no_review_agent(monkeypatch) -> None:
    async def fail_review_agent():
        raise AssertionError("review_plan must not run here")

    monkeypatch.setattr(SoftwareFactory, "review_plan", fail_review_agent)


def _acceptance_criteria() -> list[AcceptanceCriterionOutput]:
    return [
        AcceptanceCriterionOutput(
            id="AC-1",
            description="The planned change is implemented.",
            verification="Read the changed code.",
        )
    ]


def _blocked_plan(reason: str) -> PlanData:
    # The agent call returns to_result(), so the status must survive the conversion.
    return PlanOutput(
        plan_markdown=reason,
        acceptance_criteria=[],
        questions=[],
        rejected_approaches=[],
        confidence="low",
        status="blocked",
        assignee_github_login=None,
        tickets=[],
    ).to_result()


async def test_gate_mode_parks_every_plan_and_never_calls_the_reviewer(monkeypatch):
    """Gate mode: generate → park, the reviewer never runs; operator answers
    and notes thread into the next pass."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[QuestionOptionOutput(id="a", label="Redis", recommended=False)],
                )
            ],
        ),
        PlanData(plan_markdown="v2"),
        PlanData(plan_markdown="v3", acceptance_criteria=_acceptance_criteria()),
    )
    _no_review_agent(monkeypatch)

    replies = iter(
        [
            OperatorReply(
                action="request_changes", answers={"q1": "memcache — redis is banned here"}
            ),
            OperatorReply(action="request_changes", note="add a rollback section"),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert passes == [
        {"answered_questions": [], "operator_note": "", "reviewer_notes": ""},
        {
            "answered_questions": [
                {"question": "Which cache?", "answer": "memcache — redis is banned here"}
            ],
            "operator_note": "",
            "reviewer_notes": "",
        },
        {"answered_questions": [], "operator_note": "add a rollback section", "reviewer_notes": ""},
    ]


async def test_approve_confirming_recommendations_proceeds_without_redraft(monkeypatch):
    """Approving every recommended option proceeds after one plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            acceptance_criteria=_acceptance_criteria(),
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[
                        QuestionOptionOutput(id="a", label="Redis", recommended=True),
                        QuestionOptionOutput(id="b", label="Memcache", recommended=False),
                    ],
                )
            ],
        ),
    )
    _no_review_agent(monkeypatch)

    async def fake_review(*, questions=None, context=""):
        return OperatorReply(action="approve", answers={"q1": "a"})

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 1


async def test_approve_confirming_recommendations_on_empty_plan_redrafts(monkeypatch):
    """Approving recommended options on an empty-AC plan triggers a second plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="blocked",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[
                        QuestionOptionOutput(id="a", label="Redis", recommended=True),
                        QuestionOptionOutput(id="b", label="Memcache", recommended=False),
                    ],
                )
            ],
        ),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
        ),
    )
    _no_review_agent(monkeypatch)
    replies = iter(
        [
            OperatorReply(action="approve", answers={"q1": "a"}),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 2
    assert passes[1]["answered_questions"] == [{"question": "Which cache?", "answer": "Redis"}]


async def test_approve_diverging_from_recommendation_redrafts(monkeypatch):
    """Approving a different option folds that answer into a second plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[
                        QuestionOptionOutput(id="a", label="Redis", recommended=True),
                        QuestionOptionOutput(id="b", label="Memcache", recommended=False),
                    ],
                )
            ],
        ),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _no_review_agent(monkeypatch)
    replies = iter(
        [
            OperatorReply(action="approve", answers={"q1": "b"}),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 2
    assert passes[1]["answered_questions"] == [{"question": "Which cache?", "answer": "Memcache"}]


async def test_approve_with_note_redrafts(monkeypatch):
    """An approve note is change guidance and triggers a second plan."""
    flow = _flow()
    passes = _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[
                QuestionOutput(
                    id="q1",
                    prompt="Which cache?",
                    options=[QuestionOptionOutput(id="a", label="Redis", recommended=True)],
                )
            ],
        ),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _no_review_agent(monkeypatch)
    replies = iter(
        [
            OperatorReply(
                action="approve",
                answers={"q1": "a"},
                note="include the rollback command",
            ),
            OperatorReply(action="approve"),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(passes) == 2
    assert passes[1]["operator_note"] == "include the rollback command"


async def test_machine_mode_folds_the_critique_into_one_redraft(monkeypatch):
    """Machine mode, no questions: REQUEST_CHANGES routes the critique into one
    redraft (reviewer_notes) that implements without re-review, and nothing
    parks — the reviewer's single grade is all the loop may consume."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(monkeypatch, PlanData(plan_markdown="v1"), PlanData(plan_markdown="v2"))
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
    )

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a machine-mode plan must not park")

    flow.review = fail_park

    assert await flow._plan_phase() is True
    assert [p["reviewer_notes"] for p in passes] == ["", "name the wire schema"]


async def test_machine_mode_redraft_questions_park_without_the_folded_critique(monkeypatch):
    """A redraft's questions park without repeating the critique already folded
    into it; the answered redraft implements without a second review."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(
            plan_markdown="v2",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(plan_markdown="v3"),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the rollout boundary"),
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="request_changes", answers={"q1": "behind a flag"})

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert len(parks) == 1
    assert [question.id for question in parks[0]] == ["q1"]
    assert [p["reviewer_notes"] for p in passes] == [
        "",
        "name the rollout boundary",
        "",
    ]


async def test_adaptive_high_confidence_approved_plan_implements_without_parking(monkeypatch):
    """Adaptive: the critic's approval plus the planner's own high confidence
    skip the operator entirely."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a high-confidence approved plan must not park")

    flow.review = fail_park

    assert await flow._plan_phase() is True


async def test_adaptive_questions_park_before_confidence_counts(monkeypatch):
    """Adaptive: open questions always park — confidence never outranks them."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            confidence="high",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks: list[list[QuestionOutput]] = []
    replies = iter(
        [
            OperatorReply(action="request_changes", answers={"q1": "behind a flag"}),
        ]
    )

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [question.id for question in parks[0]] == ["q1"]
    assert len(parks) == 1


async def test_adaptive_confident_plan_without_acceptance_criteria_parks(monkeypatch):
    """Adaptive: skipping the operator takes the same bar the human path holds —
    no acceptance criteria, no auto-proceed."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1", confidence="high"),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks = 0

    async def fake_review(*, questions=None, context=""):
        nonlocal parks
        parks += 1
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == 2


@pytest.mark.parametrize("confidence", ["medium", "low"])
async def test_adaptive_parks_unless_the_planner_is_confident(monkeypatch, confidence):
    """Adaptive: a critic-approved plan still parks when the planner hedges."""
    flow = _flow(plan_gate="adaptive")
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            acceptance_criteria=_acceptance_criteria(),
            confidence=confidence,
        ),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == [[]]


async def test_adaptive_critic_concerns_park_even_at_high_confidence(monkeypatch):
    """Adaptive: a critique means the operator looks — the folded redraft parks
    no matter how confident it claims to be."""
    flow = _flow(plan_gate="adaptive")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1", confidence="high"),
        PlanData(
            plan_markdown="v2",
            acceptance_criteria=_acceptance_criteria(),
            confidence="high",
        ),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="wrong layer"),
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [p["reviewer_notes"] for p in passes] == ["", "wrong layer"]
    assert parks == [[]]


@pytest.mark.parametrize("plan_gate", ["machine", "machine_then_human", "adaptive"])
async def test_questions_park_before_machine_review(monkeypatch, plan_gate):
    """Open questions always park for the operator — the machine reviewer only
    ever sees a question-free plan."""
    flow = _flow(plan_gate=plan_gate)
    _fake_plans(
        monkeypatch,
        PlanData(
            plan_markdown="v1",
            questions=[QuestionOutput(id="q1", prompt="Feature flag?", options=[])],
        ),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    replies = iter(
        [
            OperatorReply(action="request_changes", answers={"q1": "yes, behind a flag"}),
            OperatorReply(action="approve"),
        ]
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [question.id for question in parks[0]] == ["q1"]
    assert len(parks) == (1 if plan_gate == "machine" else 2)


async def test_machine_then_human_approval_parks_for_operator_approval(monkeypatch):
    """A machine-approved plan still parks for operator approval."""
    flow = _flow(plan_gate="machine_then_human")
    _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(monkeypatch, ReviewOutput(decision=ReviewDecision.APPROVE, body=""))
    parks: list[tuple[list, str]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append((list(questions or []), context))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == [([], "")]


async def test_machine_then_human_critiques_force_one_redraft_before_the_park(monkeypatch):
    """The machine critique is folded into one redraft that parks for the
    operator without a re-review."""
    flow = _flow(plan_gate="machine_then_human")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
    )
    parks: list[list[QuestionOutput]] = []

    async def fake_review(*, questions=None, context=""):
        parks.append(list(questions or []))
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert [passing["reviewer_notes"] for passing in passes] == ["", "name the wire schema"]
    assert parks == [[]]


async def test_operator_rounds_after_the_critique_never_rerun_the_reviewer(monkeypatch):
    """The reviewer's pass is spent: operator request_changes rounds redraft
    and re-park on the operator alone."""
    flow = _flow(plan_gate="machine_then_human")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        PlanData(plan_markdown="v2", acceptance_criteria=_acceptance_criteria()),
        PlanData(plan_markdown="v3", acceptance_criteria=_acceptance_criteria()),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="wrong layer"),
    )
    replies = iter(
        [
            OperatorReply(action="request_changes", note="steer left"),
            OperatorReply(action="approve"),
        ]
    )
    parks = 0

    async def fake_review(*, questions=None, context=""):
        nonlocal parks
        parks += 1
        return next(replies)

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert parks == 2
    assert [passing["reviewer_notes"] for passing in passes] == ["", "wrong layer", ""]
    assert [passing["operator_note"] for passing in passes] == ["", "", "steer left"]


def _split_plan() -> PlanData:
    return PlanData(
        plan_markdown="Split into the API and the web client.",
        tickets=[
            TicketOutput(repo="acme/api", title="Add the endpoint", description="d", blocked_by=[]),
            TicketOutput(
                repo="acme/web", title="Call the endpoint", description="d", blocked_by=["acme/api"]
            ),
        ],
    )


async def test_a_split_parks_for_a_person_under_the_machine_gate(monkeypatch):
    flow = _flow(plan_gate="machine")
    flow.subject = SimpleNamespace(source="linear")
    _fake_plans(monkeypatch, _split_plan())
    _no_review_agent(monkeypatch)
    asked = []

    async def fake_review(*, questions=None, context=""):
        asked.append(questions)
        return OperatorReply(action="approve")

    flow.review = fake_review

    assert await flow._plan_phase() is True
    assert asked == [[]]


async def test_a_split_outside_linear_fails_the_run_before_any_review(monkeypatch):
    flow = _flow()
    flow.subject = SimpleNamespace(source="github")
    _fake_plans(monkeypatch, _split_plan())

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a split outside Linear must not park")

    flow.review = fail_park

    with pytest.raises(FatalError, match="only a Linear ticket can split"):
        await flow._plan_phase()


async def test_an_approved_split_creates_tickets_instead_of_implementing():
    flow = Build()
    flow.journal = BuildJournal()
    flow.journal.add(_split_plan())
    splits = []

    async def load_policy_and_profile():
        return {"policy": RepoPolicy().model_dump(mode="json"), "profile": {}}

    async def load_settings():
        return Build.Settings()

    async def load_blockers():
        return []

    async def plan_phase():
        return True

    async def split():
        splits.append(True)

    async def implement_phase():
        raise AssertionError("a split opens no pull request")

    flow._load_policy_and_profile = load_policy_and_profile
    flow._load_settings = load_settings
    flow._load_blockers = load_blockers
    flow._plan_phase = plan_phase
    flow._split = split
    flow._implement_phase = implement_phase

    await flow.run_multistep()

    assert splits == [True]


async def test_a_split_creates_the_tickets_and_links_then_releases_the_spike(druks_db, monkeypatch):
    keys = {"acme/api": "ENG-2", "acme/web": "ENG-3"}
    calls = []

    class _FakeTracker(Tracker):
        async def get_ticket(self, key):
            return {"team": "team-1", "ticket_id": "issue-1"}

        async def create_ticket(self, key, **kwargs):
            calls.append(("create", key, kwargs))
            return keys[key.rsplit(":", 1)[-1]]

        async def add_blockers(self, key, blockers):
            calls.append(("link", key, blockers))

        async def release(self, key):
            calls.append(("release", key))

        async def get_status_name(self, key):
            return

        async def set_status(self, key, status):
            return

        async def aclose(self):
            return

    async def get_tracker(cls, source=None):
        return _FakeTracker(status_names={})

    monkeypatch.setattr(SoftwareFactory, "get_tracker", classmethod(get_tracker))
    flow = Build()
    flow.journal = BuildJournal()
    flow.journal.add(_split_plan())
    spike = await make_test_work_item(repo="acme/api", title="t", ticket_key="ENG-1")
    flow.subject = spike

    await Build._split.__wrapped__(flow)

    assert calls == [
        (
            "create",
            f"{spike.source}:ENG-1:acme/api",
            {
                "team": "team-1",
                "parent": "issue-1",
                "label": "api",
                "title": "Add the endpoint",
                "description": "d",
            },
        ),
        (
            "create",
            f"{spike.source}:ENG-1:acme/web",
            {
                "team": "team-1",
                "parent": "issue-1",
                "label": "web",
                "title": "Call the endpoint",
                "description": "d",
            },
        ),
        ("link", "ENG-2", []),
        ("link", "ENG-3", ["ENG-2"]),
        ("release", "ENG-1"),
    ]


@pytest.mark.parametrize("plan_gate", ["human", "machine", "machine_then_human", "adaptive"])
async def test_blocked_plan_fails_the_run_before_any_review(monkeypatch, plan_gate):
    """A planner that cannot read the ticket or the repo fails the run with its
    own reason. Neither the machine reviewer nor the operator sees the plan."""
    flow = _flow(plan_gate=plan_gate)
    _fake_plans(monkeypatch, _blocked_plan("Blocked: the tracker tools cannot read ACME-1."))
    _no_review_agent(monkeypatch)

    async def fail_park(*, questions=None, context=""):
        raise AssertionError("a blocked plan must not park")

    flow.review = fail_park

    with pytest.raises(FatalError) as failure:
        await flow._plan_phase()
    assert str(failure.value) == "Blocked: the tracker tools cannot read ACME-1."


async def test_blocked_redraft_fails_the_run_instead_of_implementing(monkeypatch):
    """The machine gate sends a redraft to implementation without a second
    review, so a blocked redraft fails the run on its own pass."""
    flow = _flow(plan_gate="machine")
    passes = _fake_plans(
        monkeypatch,
        PlanData(plan_markdown="v1"),
        _blocked_plan("Blocked: the repo checkout is empty."),
    )
    _fake_grades(
        monkeypatch,
        ReviewOutput(decision=ReviewDecision.REQUEST_CHANGES, body="name the wire schema"),
    )

    with pytest.raises(FatalError, match="the repo checkout is empty"):
        await flow._plan_phase()
    assert [passing["reviewer_notes"] for passing in passes] == ["", "name the wire schema"]


async def test_review_work_ask_renders_a_notification_body(druks_db, monkeypatch):
    workflow = _flow()
    workflow.subject = SimpleNamespace(repo="org/repo")
    workflow.journal = BuildJournal()
    workflow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion="green"))
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    input_requests: list[dict] = []

    async def fake_wait(*, input_request):
        input_requests.append(input_request)
        return ReviewWork(action="approve")

    async def fake_approved_work():
        return True

    monkeypatch.setattr(ReviewWork, "wait", fake_wait)
    monkeypatch.setattr(workflow, "_approved_work", fake_approved_work)

    await workflow._work_gate()

    run = Run(input_request=input_requests[0])
    assert (await get_rendered_ask(druks_db, run))["body"]
    assert input_requests[0]["url"] == "https://github.com/org/repo/pull/42"
    assert input_requests[0]["label"] == "Review implementation on GitHub: PR #42"


@pytest.mark.parametrize(
    ("conclusion", "note"),
    [
        ("red", "CI on 0f3a2c1 is red"),
        ("unfinished", "CI on 0f3a2c1 did not finish within 60 minutes"),
    ],
)
async def test_a_round_without_green_ci_parks_saying_so(monkeypatch, conclusion, note):
    workflow = _flow()
    workflow.subject = SimpleNamespace(repo="org/repo")
    workflow.journal = BuildJournal()
    workflow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion=conclusion))
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    input_requests: list[dict] = []

    async def fake_wait(*, input_request):
        input_requests.append(input_request)
        return ReviewWork(action="merged")

    monkeypatch.setattr(ReviewWork, "wait", fake_wait)

    await workflow._work_gate()

    assert input_requests[0]["label"] == f"Review implementation on GitHub: PR #42. {note}"


@pytest.mark.parametrize(
    ("gate", "calls"), [("human", ["clear_draft", "merge"]), ("none", ["merge"])]
)
async def test_only_a_person_s_approval_clears_the_draft_before_a_merge(monkeypatch, gate, calls):
    flow = Build()
    flow._policy = RepoPolicy(on_approval="merge", gates={"implementation_approval": gate})
    flow._blockers = []
    flow.subject = SimpleNamespace(repo="org/repo")
    flow.journal = BuildJournal()
    flow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion="red"))
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    recorded: list[str] = []

    async def fake_wait(*, input_request):
        return ReviewWork(action="approve")

    async def clear_draft():
        recorded.append("clear_draft")

    async def merge(*, stacked):
        recorded.append("merge")
        return True

    monkeypatch.setattr(ReviewWork, "wait", fake_wait)
    monkeypatch.setattr(flow, "_clear_draft", clear_draft)
    monkeypatch.setattr(flow, "_is_stacked", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "declare_merge_intent", merge)

    assert await flow._work_gate() is True
    assert recorded == calls


async def test_needs_clarification_delivery_stops_the_run(monkeypatch):
    """The implementer bailing (needs_clarification) fails the run with its own
    reason — the stop is a workflow decision now, not a contract side effect."""
    flow = Build()

    async def bailed():
        return SimpleNamespace(
            status="needs_clarification",
            summary="AC-3 requires a pure function that performs I/O",
        )

    monkeypatch.setattr(SoftwareFactory, "implement", bailed)
    with pytest.raises(FatalError, match="pure function"):
        await flow.implement()


async def test_approval_ends_the_build_without_merging_by_default(monkeypatch):
    """A repository that has not opted in keeps its merge: approval marks the PR
    ready and the build ends."""
    flow = Build()
    flow._policy = RepoPolicy()
    cleared = []

    async def merged(*, stacked):
        raise AssertionError("merge without opt-in")

    async def fake_clear_draft():
        cleared.append(True)

    monkeypatch.setattr(flow, "declare_merge_intent", merged)
    monkeypatch.setattr(flow, "_clear_draft", fake_clear_draft)

    assert await flow._approved_work() is True
    assert cleared == [True]


async def test_a_rejected_merge_intent_reparks_the_work_gate(monkeypatch):
    """GitHub declining the merge sends the operator back to the work gate,
    rather than finishing a run whose PR never merged."""
    flow = Build()
    flow._policy = RepoPolicy(on_approval="merge")
    flow._blockers = []
    flow.subject = SimpleNamespace(repo="clawhaven/example")
    flow.journal = SimpleNamespace(implementations=[])
    reparked = []

    async def declined(*, stacked):
        return False

    async def fake_work_gate():
        reparked.append(True)
        return True

    monkeypatch.setattr(flow, "_is_stacked", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "declare_merge_intent", declined)
    monkeypatch.setattr(flow, "_work_gate", fake_work_gate)

    assert await flow._approved_work() is True
    assert reparked == [True]


def _merging_flow(monkeypatch, *, stacked: bool, requested, reads=()) -> SimpleNamespace:
    """A build whose PR is approved. GitHub answers the stacked merge with ``requested``,
    and then with each of ``reads``, one per ask."""
    flow = Build()
    flow.subject = SimpleNamespace(repo="org/repo")
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    answers = iter(reads)
    seen = SimpleNamespace(sleeps=[], reads=[], requests=0, direct=0)

    async def is_stacked():
        return stacked

    async def merge_when_ready():
        seen.direct += 1
        return True

    async def request_stacked_merge():
        seen.requests += 1
        return requested

    async def read_stacked_merge(uuid):
        seen.reads.append(uuid)
        return next(answers)

    async def sleep(seconds, *, label, until=""):
        seen.sleeps.append((seconds, label))
        return False

    monkeypatch.setattr(flow, "_is_stacked", is_stacked)
    monkeypatch.setattr(flow, "_merge_when_ready", merge_when_ready)
    monkeypatch.setattr(flow, "_request_stacked_merge", request_stacked_merge)
    monkeypatch.setattr(flow, "_read_stacked_merge", read_stacked_merge)
    monkeypatch.setattr(flow, "sleep", sleep)
    seen.flow = flow
    return seen


def _merge(status: str, uuid: str = "", message: str = "") -> dict:
    """GitHub's answer to the merge of a stacked PR."""
    return {"status": status, "details": {"uuid": uuid, "message": message}}


async def test_a_pr_outside_a_stack_merges_in_one_call_and_waits_for_nothing(monkeypatch):
    merging = _merging_flow(monkeypatch, stacked=False, requested=None)

    assert await merging.flow.declare_merge_intent(stacked=False) is True

    assert (merging.direct, merging.requests, merging.sleeps) == (1, 0, [])


async def test_a_stacked_merge_is_asked_again_with_a_durable_sleep_until_it_is_done(monkeypatch):
    """The wait is the run's, not the client's: it sleeps durably between the asks, so it
    releases the box and survives a restart."""
    merging = _merging_flow(
        monkeypatch,
        stacked=True,
        requested=_merge("pending", "u1", "enqueued"),
        reads=[_merge("pending", "u1"), _merge("merged", message="Merged")],
    )

    assert await merging.flow.declare_merge_intent(stacked=True) is True

    assert merging.requests == 1
    assert merging.reads == ["u1", "u1"]
    assert merging.sleeps == [(MERGE_POLL_SECONDS, "Waiting for GitHub to merge")] * 2


async def test_a_stacked_merge_that_github_fails_parks_the_work(monkeypatch):
    merging = _merging_flow(
        monkeypatch,
        stacked=True,
        requested=_merge("pending", "u1"),
        reads=[_merge("failed", "u1", "Required status check is failing")],
    )

    assert await merging.flow.declare_merge_intent(stacked=True) is False


async def test_a_stacked_merge_that_github_refuses_at_once_does_not_wait(monkeypatch):
    merging = _merging_flow(
        monkeypatch, stacked=True, requested=_merge("failed", message="The PR is a draft")
    )

    assert await merging.flow.declare_merge_intent(stacked=True) is False

    assert merging.sleeps == []


async def test_a_stacked_merge_is_asked_about_until_it_is_no_longer_pending(monkeypatch):
    """GitHub says a stack merge may take a few minutes, so no number of asks ends the wait
    while it is still pending. A merge that fails late is not lost."""
    pending = _merge("pending", "u1")
    merging = _merging_flow(
        monkeypatch,
        stacked=True,
        requested=pending,
        reads=[pending] * 40 + [_merge("failed", "u1", "Required status check is failing")],
    )

    assert await merging.flow.declare_merge_intent(stacked=True) is False

    assert len(merging.sleeps) == 41


def _flow_on_a_blockers_branch(monkeypatch, *bases: str, answers=()) -> SimpleNamespace:
    """A build whose PR targets each of ``bases`` as it reads it, one after another. Each
    wait for the blocker answers with the next of ``answers``: facts, or a timeout."""
    flow = Build()
    flow._policy = RepoPolicy(on_approval="merge")
    flow._base_branch = "agent/ACME-1"
    flow.subject = SimpleNamespace(repo="org/repo")
    flow.journal = BuildJournal()
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    reads = iter(bases)
    github = SimpleNamespace(
        get_pull_request=AsyncMock(side_effect=lambda repo, number: {"base": {"ref": next(reads)}})
    )
    monkeypatch.setattr(
        "druks.contrib.software_factory.workflows.Github.get_client",
        AsyncMock(return_value=github),
    )
    replies = iter(answers)
    waits: list[tuple] = []
    merges: list[int] = []
    triages: list[bool] = []

    async def wait(topic, *, label, ttl_seconds):
        waits.append((topic, label, ttl_seconds))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def merge(*, stacked):
        merges.append(len(waits))
        return True

    async def triage():
        triages.append(True)
        return False

    async def now():
        return 0.0

    async def targets_blocker_branch():
        return await Build._targets_blocker_branch.__wrapped__(flow)

    monkeypatch.setattr(flow, "wait", wait)
    monkeypatch.setattr(flow, "_is_stacked", AsyncMock(return_value=False))
    monkeypatch.setattr(flow, "declare_merge_intent", merge)
    monkeypatch.setattr(flow, "_triage", triage)
    monkeypatch.setattr(flow, "_now", now)
    monkeypatch.setattr(flow, "_targets_blocker_branch", targets_blocker_branch)
    return SimpleNamespace(flow=flow, waits=waits, merges=merges, triages=triages)


_WAIT = ("pr.changed", "Waiting for its blocker to merge", BLOCKER_POLL_SECONDS)


async def test_approval_waits_to_merge_while_the_pr_targets_a_blockers_branch(monkeypatch):
    """GitHub merges a stacked PR with every open PR below it, so a PR on a blocker's
    branch merges only after GitHub moves it to the base the blocker merged into. A wake
    from GitHub and a poll both read the base again."""
    build = _flow_on_a_blockers_branch(
        monkeypatch,
        "agent/ACME-1",
        "agent/ACME-1",
        "main",
        answers=[{"action": "moved"}, WaitTimeout("pr.changed")],
    )

    assert await build.flow._approved_work() is True

    assert build.waits == [_WAIT, _WAIT]
    assert build.merges == [2]


async def test_approval_merges_at_once_when_the_pr_no_longer_targets_a_blockers_branch(
    monkeypatch,
):
    build = _flow_on_a_blockers_branch(monkeypatch, "main")

    assert await build.flow._approved_work() is True

    assert (build.waits, build.merges) == ([], [0])


async def test_a_build_that_waits_too_long_for_its_blocker_fails(monkeypatch):
    build = _flow_on_a_blockers_branch(monkeypatch, "agent/ACME-1")
    clock = iter([0.0, BLOCKER_TTL_SECONDS + 1.0])

    async def now():
        return next(clock)

    monkeypatch.setattr(build.flow, "_now", now)

    with pytest.raises(FatalError, match="agent/ACME-1 did not merge in time"):
        await build.flow._approved_work()

    assert (build.waits, build.merges) == ([], [])


async def test_a_change_request_while_the_build_waits_goes_to_triage(monkeypatch):
    """The wait is not the review gate, so a review that asks for changes has to reach the
    build through the wait, and not merge with the blocker."""
    review = {"action": "request_changes", "reviewer": "paulo", "body": "Rename it."}
    build = _flow_on_a_blockers_branch(
        monkeypatch, "agent/ACME-1", "agent/ACME-1", answers=[review]
    )

    assert await build.flow._approved_work() is False

    assert (build.merges, build.triages) == ([], [True])
    assert [(entry["reviewer"], entry["body"]) for entry in build.flow.journal.human_feedback] == [
        ("paulo", "Rename it.")
    ]


async def test_an_approval_while_the_build_waits_changes_nothing(monkeypatch):
    approval = {"action": "approve", "reviewer": "paulo", "body": ""}
    build = _flow_on_a_blockers_branch(
        monkeypatch, "agent/ACME-1", "agent/ACME-1", "main", answers=[approval, {"action": "moved"}]
    )

    assert await build.flow._approved_work() is True

    assert (build.merges, build.triages) == ([2], [])


def _stacked_approval(monkeypatch, *, heads, conclusion: str, merged: bool):
    """An approved stacked PR whose head reads as each of ``heads`` in turn, whose checks
    conclude ``conclusion``, and whose merge GitHub accepts or refuses."""
    build = _flow_on_a_blockers_branch(monkeypatch, "main")
    reads = iter(heads)
    waited, asked, parked = [], [], []

    async def wait_for_ci(head_sha):
        waited.append(head_sha)
        return CiReport(head_sha=head_sha, conclusion=conclusion)

    async def merge(*, stacked):
        asked.append(stacked)
        return merged

    async def work_gate():
        parked.append(True)
        return True

    monkeypatch.setattr(build.flow, "_is_stacked", AsyncMock(return_value=True))
    monkeypatch.setattr(build.flow, "_read_head_sha", AsyncMock(side_effect=lambda: next(reads)))
    monkeypatch.setattr(build.flow, "_wait_for_ci", wait_for_ci)
    monkeypatch.setattr(build.flow, "declare_merge_intent", merge)
    monkeypatch.setattr(build.flow, "_work_gate", work_gate)
    return SimpleNamespace(flow=build.flow, waited=waited, asked=asked, parked=parked)


async def test_a_stacked_pr_waits_for_the_checks_of_its_head_and_then_asks_github(monkeypatch):
    """GitHub rebases a PR when the one below it merges, and its checks start again. GitHub
    decides whether to merge, and the build asks once the checks have finished."""
    approval = _stacked_approval(
        monkeypatch, heads=["9c1d", "9c1d"], conclusion="green", merged=True
    )

    assert await approval.flow._approved_work() is True

    assert (approval.waited, approval.asked) == (["9c1d"], [True])


async def test_a_stacked_pr_whose_head_moved_during_the_wait_waits_again(monkeypatch):
    """GitHub moves a stacked PR first and rebases the others after, so the wait can end on
    the old head."""
    approval = _stacked_approval(
        monkeypatch, heads=["old", "new", "new"], conclusion="green", merged=True
    )

    assert await approval.flow._approved_work() is True

    assert approval.waited == ["old", "new"]


async def test_red_checks_that_github_refuses_to_merge_go_back_to_the_implementer(monkeypatch):
    approval = _stacked_approval(
        monkeypatch, heads=["9c1d", "9c1d"], conclusion="red", merged=False
    )

    assert await approval.flow._approved_work() is False

    assert approval.parked == []


async def test_red_checks_do_not_stop_a_merge_that_github_accepts(monkeypatch):
    """The base requires only some checks, so a red check that it does not require leaves the
    person's approval standing."""
    approval = _stacked_approval(monkeypatch, heads=["9c1d", "9c1d"], conclusion="red", merged=True)

    assert await approval.flow._approved_work() is True

    assert approval.parked == []


async def test_a_refused_merge_with_green_checks_parks_the_work_for_review(monkeypatch):
    approval = _stacked_approval(
        monkeypatch, heads=["9c1d", "9c1d"], conclusion="green", merged=False
    )

    assert await approval.flow._approved_work() is True

    assert approval.parked == [True]


@pytest.mark.parametrize(
    ("pull_request", "state"),
    [
        ({"state": "open", "merged": False}, "open"),
        ({"state": "closed", "merged": True}, "merged"),
        ({"state": "closed", "merged": False}, "closed"),
    ],
)
async def test_the_build_reads_the_state_of_the_blockers_pr_that_it_started_on(
    monkeypatch, pull_request, state
):
    flow = Build()
    flow._base_pr_number = 7
    flow.subject = SimpleNamespace(repo="org/repo")
    github = SimpleNamespace(get_pull_request=AsyncMock(return_value=pull_request))
    monkeypatch.setattr(
        "druks.contrib.software_factory.workflows.Github.get_client",
        AsyncMock(return_value=github),
    )

    assert await Build._read_base_state.__wrapped__(flow) == state

    github.get_pull_request.assert_awaited_once_with("org/repo", 7)


@pytest.mark.parametrize(("state", "is_open"), [("open", True), ("merged", False)])
async def test_a_blockers_pr_is_a_base_while_it_is_open(monkeypatch, state, is_open):
    flow = Build()
    monkeypatch.setattr(flow, "_read_base_state", AsyncMock(return_value=state))

    assert await flow._is_base_open() is is_open


async def test_a_blockers_pr_that_closed_without_a_merge_fails_the_build(monkeypatch):
    """The blocker's change is on no branch then, and the default branch does not hold it."""
    flow = Build()
    flow._base_pr_number = 7
    flow.input = Build._run_input_model(base_ticket="ACME-1")
    monkeypatch.setattr(flow, "_read_base_state", AsyncMock(return_value="closed"))

    with pytest.raises(FatalError, match=r"ACME-1 \(#7\) closed without a merge"):
        await flow._is_base_open()


async def test_a_branch_starts_on_the_base_that_it_is_given_and_gives_the_commit_it_starts_on():
    async def create_branch(*, base=None):
        return f"tip of {base}"

    flow = Build()
    flow.subject = SimpleNamespace(repo="org/repo", create_branch=create_branch)

    assert await Build._create_branch.__wrapped__(flow, "agent/ACME-1") == "tip of agent/ACME-1"
    assert await Build._create_branch.__wrapped__(flow, None) == "tip of None"


async def test_a_blocker_that_merged_meanwhile_leaves_the_build_on_the_default_branch(
    monkeypatch,
):
    flow, calls = _implementing_build(
        monkeypatch, max_revisions=1, heads=["a"], conclusions=["green"], decisions=["approve"]
    )
    flow._base_branch = "agent/ACME-1"
    monkeypatch.setattr(flow, "_is_base_open", AsyncMock(return_value=False))

    await flow._implement_phase()

    assert flow._base_branch is None
    assert flow._create_branch.created == [None]
    assert "join_stack" not in calls


def _implementing_build_on_a_blocker(monkeypatch, **delivered) -> tuple[Build, list[str]]:
    """``_implementing_build``, on the branch of a blocker whose PR is open. The checks of
    the PR's head are green."""
    flow, calls = _implementing_build(monkeypatch, conclusions=[], **delivered)
    flow._base_branch = "agent/ACME-1"
    flow._base_pr_number = 7

    async def join_stack():
        calls.append("join_stack")

    async def wait_for_checks_of_the_head():
        calls.append("checks of the head")
        return CiReport(head_sha="rebased", conclusion="green")

    monkeypatch.setattr(flow, "_is_base_open", AsyncMock(return_value=True))
    monkeypatch.setattr(flow, "_join_stack", join_stack)
    monkeypatch.setattr(flow, "_wait_for_checks_of_the_head", wait_for_checks_of_the_head)
    return flow, calls


async def test_a_build_on_a_blockers_branch_joins_its_stack_after_the_first_delivery(monkeypatch):
    flow, calls = _implementing_build_on_a_blocker(
        monkeypatch, max_revisions=5, heads=["a", "b"], decisions=["request_changes", "approve"]
    )

    await flow._implement_phase()

    assert flow._create_branch.created == ["agent/ACME-1"]
    assert flow._base_tip == "tip of agent/ACME-1"
    assert calls.count("join_stack") == 1
    assert calls[:2] == ["implement", "join_stack"]


async def test_a_build_on_a_blockers_branch_waits_for_the_checks_of_its_prs_head(monkeypatch):
    """A rebase onto the blocker gives the PR a head that the implementer did not deliver."""
    flow, calls = _implementing_build_on_a_blocker(
        monkeypatch, max_revisions=1, heads=["a"], decisions=["approve"]
    )

    await flow._implement_phase()

    assert calls == ["implement", "join_stack", "checks of the head", "review", "work_gate"]


def _refusal(status_code: int, message: str = "must form a stack") -> dict:
    return {"status_code": status_code, "message": f"org/repo#9 on #7: {status_code} {message}"}


def _joining_build(monkeypatch, *answers, base_open=(True,)):
    """A build that asks GitHub to stack its PR, and gets each of ``answers`` in turn: None
    for a PR that joined, or GitHub's refusal. The blocker's PR is open, or ``base_open``
    answers in turn."""
    flow = Build()
    flow._base_branch = "agent/ACME-1"
    flow.sleeps = []

    async def sleep(seconds, *, label, until=""):
        flow.sleeps.append(seconds)
        return False

    monkeypatch.setattr(flow, "sleep", sleep)
    monkeypatch.setattr(flow, "_rebase_onto_blocker", AsyncMock(return_value="blocker tip"))
    monkeypatch.setattr(flow, "_stack_on_blocker", AsyncMock(side_effect=list(answers)))
    monkeypatch.setattr(flow, "_is_base_open", AsyncMock(side_effect=cycle(base_open)))
    monkeypatch.setattr(flow, "_move_to_default_branch", AsyncMock())
    return flow


async def test_a_pr_that_github_will_not_stack_fails_the_build_with_the_reason(monkeypatch):
    flow = _joining_build(monkeypatch, _refusal(422))

    with pytest.raises(
        FatalError, match="cannot join the stack of agent/ACME-1: .*must form a stack"
    ):
        await flow._join_stack()

    flow._move_to_default_branch.assert_not_awaited()
    assert flow.sleeps == []


async def test_a_pr_whose_blocker_merged_before_it_joined_moves_onto_the_default_branch(
    monkeypatch,
):
    """Nothing would move a PR on a merged PR's branch. The blocker's change is on the
    default branch now, so the PR goes there, and GitHub is not asked to stack it."""
    flow = _joining_build(monkeypatch, base_open=(False,))

    await flow._join_stack()

    flow._stack_on_blocker.assert_not_awaited()
    flow._move_to_default_branch.assert_awaited_once_with()
    assert flow._base_branch is None


async def test_a_pr_whose_blocker_merges_while_it_joins_moves_onto_the_default_branch(
    monkeypatch,
):
    """GitHub refuses a PR on a merged PR's branch. That refusal does not fail the build."""
    flow = _joining_build(monkeypatch, _refusal(422), base_open=(True, False, False))

    await flow._join_stack()

    flow._move_to_default_branch.assert_awaited_once_with()
    assert flow._base_branch is None


async def test_a_branch_moves_onto_the_blockers_tip_before_and_after_its_pr_joins_the_stack(
    monkeypatch,
):
    """Until the PR is in the stack, only its build moves it. The blocker can change before
    the join, and between the move and the join."""
    flow = _joining_build(monkeypatch)
    calls = []

    async def rebase_onto_blocker():
        calls.append("rebase")
        return "blocker tip"

    async def stack_on_blocker():
        calls.append("stack")

    monkeypatch.setattr(flow, "_rebase_onto_blocker", rebase_onto_blocker)
    monkeypatch.setattr(flow, "_stack_on_blocker", stack_on_blocker)

    await flow._join_stack()

    assert calls == ["rebase", "stack", "rebase"]
    assert flow._base_tip == "blocker tip"
    flow._move_to_default_branch.assert_not_awaited()
    assert flow._base_branch == "agent/ACME-1"


async def test_a_stack_that_github_is_changing_is_tried_again_after_a_wait(monkeypatch):
    """GitHub answers 409 while it rebases the stack after a merge below."""
    busy = _refusal(409, "stack is being modified")
    flow = _joining_build(monkeypatch, busy, busy, busy, busy, None)

    await flow._join_stack()

    assert flow.sleeps == [STACK_RETRY_SECONDS] * 4
    assert flow._base_branch == "agent/ACME-1"


def _moving_build(monkeypatch, *, conflict: bool):
    flow = Build()
    flow._base_branch = "agent/ACME-1"
    flow._base_tip = "cut tip"
    flow.subject = SimpleNamespace(repo="org/repo", branch="agent/ACME-2")
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    github = SimpleNamespace(
        get_repository=AsyncMock(return_value={"default_branch": "main"}),
        rebase_branch=AsyncMock(
            return_value="new tip",
            side_effect=GitHubMergeConflictError("o/r: agent/ACME-2") if conflict else None,
        ),
        update_pull_request_base=AsyncMock(),
    )
    monkeypatch.setattr(
        "druks.contrib.software_factory.workflows.Github.get_client",
        AsyncMock(return_value=github),
    )
    return flow, github


async def test_a_branch_that_fell_behind_moves_onto_the_blockers_tip_from_where_it_was_cut(
    monkeypatch,
):
    flow, github = _moving_build(monkeypatch, conflict=False)

    assert await Build._rebase_onto_blocker.__wrapped__(flow) == "new tip"

    github.rebase_branch.assert_awaited_once_with(
        "org/repo", "agent/ACME-2", onto="agent/ACME-1", since="cut tip"
    )


async def test_a_branch_that_conflicts_with_its_blocker_fails_the_build(monkeypatch):
    flow, _ = _moving_build(monkeypatch, conflict=True)

    with pytest.raises(FatalError, match="conflicts with agent/ACME-1"):
        await Build._rebase_onto_blocker.__wrapped__(flow)


async def test_moving_a_pr_to_the_default_branch_rebases_it_from_the_tip_it_sits_on(monkeypatch):
    """The blocker can change after the branch is cut, so the head of the merged PR is not
    always that tip."""
    flow, github = _moving_build(monkeypatch, conflict=False)

    await Build._move_to_default_branch.__wrapped__(flow)

    github.rebase_branch.assert_awaited_once_with(
        "org/repo", "agent/ACME-2", onto="main", since="cut tip"
    )
    github.update_pull_request_base.assert_awaited_once_with("org/repo", 42, base="main")


async def test_a_pr_that_conflicts_with_the_default_branch_fails_the_build_and_keeps_its_base(
    monkeypatch,
):
    flow, github = _moving_build(monkeypatch, conflict=True)

    with pytest.raises(FatalError, match="conflicts with main"):
        await Build._move_to_default_branch.__wrapped__(flow)

    github.update_pull_request_base.assert_not_awaited()


def _reviewing_flow(monkeypatch, facts: dict) -> tuple[Build, list[dict]]:
    flow = Build()
    flow.subject = SimpleNamespace(repo="acme/widget")
    flow.account = SimpleNamespace(id="acct-1")
    flow.journal = BuildJournal()
    monkeypatch.setattr(Build, "pr_number", property(lambda self: 42))
    dispatched: list[dict] = []

    async def dispatch(cls, **kwargs):
        dispatched.append(kwargs)

    async def wait(topic, *, label):
        assert topic == "review.completed"
        return facts

    monkeypatch.setattr(PullRequestReview, "dispatch", classmethod(dispatch))
    monkeypatch.setattr(flow, "wait", wait)
    return flow, dispatched


async def test_a_build_hands_its_pull_request_to_the_review(monkeypatch):
    flow, dispatched = _reviewing_flow(
        monkeypatch,
        {
            "decision": "request_changes",
            "summary": "Rename the flag.",
            "findings": [],
            "context_repos": [],
        },
    )

    review = await flow._review_work()

    assert dispatched == [{"repo": "acme/widget", "pr_number": 42, "account": flow.account}]
    assert review == ReviewReport(
        decision="request_changes", summary="Rename the flag.", findings=[], context_repos=[]
    )
    assert flow.journal.reviews == [review]


def _implementing_build(
    monkeypatch, *, max_revisions, heads, conclusions, decisions
) -> tuple[Build, list[str]]:
    """A build whose implementer delivers ``heads``, whose CI concludes ``conclusions``,
    and whose review decides ``decisions``, each in turn. It returns the calls made."""
    flow = Build()
    flow._settings = Build.Settings(max_implementation_revisions=max_revisions)
    flow._blockers = []
    flow.journal = SimpleNamespace(implementation_revision=0, last_implementation=None)
    heads, conclusions, decisions = iter(heads), iter(conclusions), iter(decisions)
    calls: list[str] = []

    async def create_branch(base):
        create_branch.created.append(base)
        return f"tip of {base}"

    create_branch.created = []

    async def implement():
        delivery = SimpleNamespace(head_sha=next(heads))
        flow.journal.implementation_revision += 1
        flow.journal.last_implementation = delivery
        calls.append("implement")
        return delivery

    async def wait_for_ci(head_sha):
        conclusion = next(conclusions)
        calls.append(f"ci:{conclusion}")
        return CiReport(head_sha=head_sha, conclusion=conclusion)

    async def review_work():
        calls.append("review")
        return ReviewReport(decision=next(decisions), summary="", findings=[], context_repos=[])

    async def work_gate():
        calls.append("work_gate")
        return True

    monkeypatch.setattr(flow, "_create_branch", create_branch)
    monkeypatch.setattr(flow, "implement", implement)
    monkeypatch.setattr(flow, "_wait_for_ci", wait_for_ci)
    monkeypatch.setattr(flow, "_review_work", review_work)
    monkeypatch.setattr(flow, "_work_gate", work_gate)
    return flow, calls


@pytest.mark.parametrize(
    ("max_revisions", "expected"),
    [
        (5, ["implement", "ci:green", "review", "implement", "ci:green", "review", "work_gate"]),
        (1, ["implement", "ci:green", "review", "work_gate"]),
    ],
)
async def test_requested_changes_go_back_to_implement_until_the_cap(
    monkeypatch, max_revisions, expected
):
    flow, calls = _implementing_build(
        monkeypatch,
        max_revisions=max_revisions,
        heads=["a", "b"],
        conclusions=["green", "green"],
        decisions=["request_changes", "approve"],
    )

    await flow._implement_phase()

    assert calls == expected


@pytest.mark.parametrize(
    ("max_revisions", "expected"),
    [
        (5, ["implement", "ci:red", "implement", "ci:green", "review", "work_gate"]),
        (1, ["implement", "ci:red", "review", "work_gate"]),
    ],
)
async def test_red_ci_goes_back_to_implement_before_the_review_until_the_cap(
    monkeypatch, max_revisions, expected
):
    flow, calls = _implementing_build(
        monkeypatch,
        max_revisions=max_revisions,
        heads=["a", "b"],
        conclusions=["red", "green"],
        decisions=["approve"],
    )

    await flow._implement_phase()

    assert calls == expected


async def test_a_round_that_pushes_nothing_goes_to_the_review_without_a_ci_wait(monkeypatch):
    flow, calls = _implementing_build(
        monkeypatch, max_revisions=5, heads=["a", "a"], conclusions=["red"], decisions=["approve"]
    )

    await flow._implement_phase()

    assert calls == ["implement", "ci:red", "implement", "review", "work_gate"]


@pytest.mark.parametrize(
    ("conclusion", "decision", "leaves_draft"),
    [
        ("green", "approve", True),
        ("green", "comment", True),
        ("red", "approve", False),
        ("green", "request_changes", False),
    ],
)
async def test_the_work_park_takes_the_pr_out_of_draft_only_when_ready(
    monkeypatch, conclusion, decision, leaves_draft
):
    flow = Build()
    flow.journal.add(CiReport(head_sha="0f3a2c1d", conclusion=conclusion))
    flow.journal.add(ReviewReport(decision=decision, summary="", findings=[], context_repos=[]))
    drafts: list[bool] = []

    async def set_pr_draft(*, draft):
        drafts.append(draft)

    async def request_assignee_review():
        pass

    monkeypatch.setattr(flow, "set_pr_draft", set_pr_draft)
    monkeypatch.setattr(flow, "request_assignee_review", request_assignee_review)

    await ReviewWork.on_wait(flow)

    assert drafts == ([False] if leaves_draft else [])
