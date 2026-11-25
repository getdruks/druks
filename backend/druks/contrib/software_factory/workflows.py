import asyncio
import hashlib
import json
import logging
import time
from contextlib import suppress
from typing import Any

from pydantic import BaseModel, Field

from druks.accounts import Account
from druks.apps.fetcher import fetch_file
from druks.contrib.software_factory.contracts import (
    CiReport,
    ImplementationOutput,
    ReviewReport,
    ReviewWork,
)
from druks.contrib.software_factory.enums import HumanFeedbackAction, Resolution, ReviewDecision
from druks.contrib.software_factory.models import ProjectRepo, WorkItem
from druks.contrib.software_factory.ticketing.enums import TicketStatus
from druks.core.apis.exceptions import GitHubRerunRefusedError
from druks.core.services import Github
from druks.db import db_session
from druks.mcp.inbound import get_druks_mcp_server
from druks.prompts import render_prompt
from druks.sandbox import Sandbox, SandboxMcpServer, SandboxSecret, TemplateUnavailable
from druks.sandbox.layout import get_related_root, get_work_root
from druks.services.exceptions import ServiceNotConnectedError
from druks.settings import get_settings
from druks.skills.models import Skill
from druks.workflows import FatalError, WaitTimeout, Workflow, step
from druks.workspaces import RepoWorkspace

from .app import SoftwareFactory
from .constants import (
    CHECK_TOOLS,
    CI_POLL_SECONDS,
    CI_TTL_SECONDS,
    GITHUB_MCP_NAME,
    GITHUB_MCP_URL,
    IMPLEMENT_EFFORT_BY_SIZE,
    LOCKFILES,
    RERUN_CONCLUSIONS,
    RERUN_POLL_SECONDS,
    RERUN_START_POLLS,
    RUN_ACTIVE_STATUSES,
    TICKET_TOOLS,
)
from .datastructures import PullRequest
from .exceptions import (
    SandboxSetupMissing,
    TicketAlreadyMerged,
    TicketBlocked,
    TicketNotRoutable,
)
from .github import get_review_actor
from .journal import BuildJournal
from .policy import PlanGate, RepoPolicy
from .prompt_context import BuildPromptContext

logger = logging.getLogger(__name__)


async def get_project_repos(repo: str) -> tuple[str, ...]:
    # The repo, then the project's other repos, which agents clone on demand.
    project_repo = await ProjectRepo.get_for_repo(repo)
    siblings = await project_repo.get_siblings() if project_repo else []
    return (repo, *(sibling.full_name for sibling in siblings))


class BuildWorkspace(RepoWorkspace):
    @property
    def workspace_root(self) -> str:
        return get_work_root(self.host.ssh_username)

    @classmethod
    async def get_skills(cls, subject: Any) -> tuple[str, ...]:
        project_repo = await ProjectRepo.get_for_repo(subject.repo, raise_on_missing=True)
        return tuple(project_repo.effective_profile.get("recommended_skills", []))

    @classmethod
    async def get_secrets(cls, subject: Any) -> list[SandboxSecret]:
        return [
            SandboxSecret(
                name=cls.github.slug,
                secret_id=(await cls.github.get()).id,
                resources=await get_project_repos(subject.repo),
                host=cls.github.host,
            )
        ]

    @classmethod
    async def get_checks(cls, subject: Any) -> tuple[str, ...]:
        # The repo's test commands, which the agent may run on another box. A box that
        # cannot reach Druks' server has no tool to run them with, so it declares none.
        if not await cls._get_druks_server():
            return ()
        project_repo = await ProjectRepo.get_for_repo(subject.repo, raise_on_missing=True)
        verification = project_repo.effective_profile.get("verification") or {}
        return tuple(entry["command"] for entry in verification.get("test_commands", []))

    @classmethod
    async def get_check_box(cls, subject: Any) -> dict[str, Any]:
        # The repo's template, when its setup script has built one, and the env that its
        # config sets. A repo with neither checks on the sandbox's default image.
        project_repo = await ProjectRepo.get_for_repo(subject.repo, raise_on_missing=True)
        policy = await RepoPolicy.resolve(subject.repo)
        sandbox = policy.verification and policy.verification.sandbox
        box = {}
        if template := project_repo.effective_profile.get("sandbox_template"):
            box["template"] = template
        if sandbox and sandbox.env:
            box["env"] = dict(sandbox.env)
        return box

    @classmethod
    async def _get_druks_server(cls) -> SandboxMcpServer | None:
        tracker = (await SoftwareFactory.settings()).tracker
        tools = (*TICKET_TOOLS, *CHECK_TOOLS) if tracker == "druks" else CHECK_TOOLS
        urls = get_settings().urls
        if tracker != "druks" and not (urls.mcp_url or urls.webhook_base):
            # Any other tracker built without this server before it carried the check tool.
            return None
        # A board build with no address raises here, as it did before.
        return get_druks_mcp_server(allowed_tools=tools)

    @classmethod
    async def get_mcp_servers(cls, subject: Any) -> tuple[SandboxMcpServer, ...]:
        github = SandboxMcpServer(
            name=GITHUB_MCP_NAME,
            url=GITHUB_MCP_URL,
            secret_id=(await cls.github.get()).id,
            resources=await get_project_repos(subject.repo),
        )
        if druks := await cls._get_druks_server():
            return (github, druks)
        return (github,)

    async def prepare(self) -> None:
        # Agents clone related repos on demand. The --add-dir target must exist first.
        related_root = get_related_root(self.host.ssh_username)
        await self.host.exec(["mkdir", "-p", related_root], timeout=10.0)
        await super().prepare()

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Never the repo cwd. Claude hangs with no output on ``--add-dir <cwd>``.
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (get_related_root(self.host.ssh_username),)
        return kwargs


class Build(Workflow):
    subject = WorkItem
    steps_reuse_sandbox = True
    workspace_class = BuildWorkspace
    journal_class = BuildJournal
    journal: BuildJournal

    class Settings(BaseModel):
        plan_gate: PlanGate = Field(
            default="human",
            title="Plan gate",
            description="Choose who approves the plan before implementation.",
            json_schema_extra={
                "choice_details": {
                    "human": {
                        "label": "Human review",
                        "help": "You approve every plan. The machine reviewer does not run.",
                    },
                    "machine": {
                        "label": "Machine review",
                        "help": (
                            "The machine reviewer checks once. "
                            "Implementation starts without your approval."
                        ),
                    },
                    "machine_then_human": {
                        "label": "Machine then human",
                        "help": "The machine reviewer checks once. You then approve the plan.",
                    },
                    "adaptive": {
                        "label": "Adaptive review",
                        "help": (
                            "An approved high-confidence plan starts directly. "
                            "All other plans need your approval."
                        ),
                    },
                },
            },
        )
        max_implementation_revisions: int = Field(
            default=5,
            ge=1,
            le=20,
            title="Max implementation revisions",
            description="Implement/review round-trips before parking for a human.",
        )

    @classmethod
    async def dispatch(cls, *, ticket: dict) -> str:
        # A ticket at the trigger status, or an agent's start, opens a build. An active
        # build announces nothing again, so the ticket takes its status here.
        item = await WorkItem.get_for_ticket_key(
            source=ticket["source"], ticket_key=ticket["identifier"]
        )
        if item:
            if item.resolution == Resolution.MERGED:
                raise TicketAlreadyMerged(ticket["identifier"])
            await item.update(title=ticket["title"], ticket_url=ticket["url"])
            status = await item.get_status(workflow=cls)
            if status.is_active:
                reviewing = status.gate == ReviewWork.name
                await item.set_ticket_status(
                    TicketStatus.IN_REVIEW if reviewing else TicketStatus.IN_PROGRESS
                )
                return status.run
        else:
            project_repo = await ProjectRepo.lookup(
                project_name=ticket["project_name"], labels=ticket["labels"]
            )
            if project_repo:
                item = await WorkItem.create(
                    project=project_repo.project,
                    source=ticket["source"],
                    title=ticket["title"] or ticket["identifier"],
                    ticket_key=ticket["identifier"],
                    ticket_url=ticket["url"],
                    repo=project_repo.full_name,
                )
            else:
                raise TicketNotRoutable(ticket["identifier"])
        try:
            await Github.get()
        except ServiceNotConnectedError as error:
            await item.announce("build.rejected", reason=str(error))
            raise
        account_id = None
        blockers = {}
        if tracker := await SoftwareFactory.get_tracker():
            async with tracker:
                blockers = await tracker.get_blocked_by(ticket["identifier"])
                if open_blockers := [key for key, is_open in blockers.items() if is_open]:
                    await item.announce("build.waiting", blockers=open_blockers)
                    raise TicketBlocked(ticket["identifier"], open_blockers)
                account_id = await tracker.get_account_id(ticket["assignee_id"])
        return await cls.start(
            subject=item,
            account_id=account_id,
            assignee_email=ticket["assignee_email"],
            assignee_name=ticket["assignee_name"],
            blockers=list(blockers),
        )

    async def run_multistep(
        self,
        assignee_email: str | None = None,
        assignee_name: str | None = None,
        blockers: tuple[str, ...] = (),
    ) -> None:
        # Steps read the policy, the profile, the settings, and the blockers, so replay
        # reuses them.
        resolved = await self._load_policy_and_profile()
        # The stored policy passed the file's check when the build started. The copy of an
        # older build can hold a key that the config no longer has.
        self._policy = RepoPolicy.model_validate(resolved["policy"], extra="ignore")
        self._profile = resolved["profile"]
        self._settings = await self._load_settings()
        self._blockers = await self._load_blockers()

        if await self._plan_phase():
            if self.journal.plan.tickets:
                await self._split()
            else:
                await self._implement_phase()

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        work_item = await self.subject
        project_repo = await ProjectRepo.get_for_repo(work_item.repo, raise_on_missing=True)
        endpoint = get_settings().urls.endpoint.rstrip("/")
        work_item_url = f"{endpoint}/software_factory/work-items/{work_item.id}" if endpoint else ""
        prompt_context = BuildPromptContext(
            repo=work_item.repo,
            work_item_url=work_item_url,
            branch=work_item.branch,
            pr_number=self.pr_number,
            ticket_ref=work_item.ticket_key,
            source=work_item.source,
            assignee_name=self.input.assignee_name,
            assignee_email=self.input.assignee_email,
            related_repos=await project_repo.get_siblings(),
            skills=await Skill.list_delivered(
                db_session(), self._profile.get("recommended_skills", [])
            ),
            blockers=self._blockers,
            run_id=self.workflow_id,
            checks=await self.workspace_class.get_checks(work_item),
            journal=self.journal,
        )
        return {
            "verification": await self._policy.verification_block(
                profile=self._profile, repo=work_item.repo
            ),
            "build": prompt_context,
            **await super().get_prompt_context(**context),
        }

    @step
    async def _load_policy_and_profile(self) -> dict[str, Any]:
        repo = (await self.subject).repo
        policy = await RepoPolicy.resolve(repo)
        project_repo = await ProjectRepo.get_for_repo(repo, raise_on_missing=True)
        return {
            "policy": policy.model_dump(mode="json"),
            "profile": project_repo.effective_profile,
        }

    @step
    async def _load_settings(self) -> "Build.Settings":
        # Replay reuses the settings that the run started with, not later edits.
        return await self.settings()

    @step
    async def _load_blockers(self) -> list[dict]:
        """Each blocker's ticket key, or its merged PR when Druks built it."""
        source = (await self.subject).source
        blockers = []
        for key in self.input.blockers:
            item = await WorkItem.get_for_ticket_key(source=source, ticket_key=key)
            if item and item.resolution == Resolution.MERGED:
                blockers.append(
                    {"repo": item.repo, "pr_number": item.pr_number, "title": item.title}
                )
            else:
                blockers.append({"ticket_key": key, "pr_number": None})
        return blockers

    async def _plan_phase(self) -> bool:
        """Return True when the plan is approved for implementation."""
        plan_gate = self._policy.plan_approval_gate(self._settings.plan_gate)
        answered_questions: list[dict[str, str]] = []
        operator_note = ""
        critique = ""
        reviewed = False
        while True:
            plan = await SoftwareFactory.generate_plan(
                answered_questions=answered_questions,
                operator_note=operator_note,
                reviewer_notes=critique,
            )
            # The planner could not read the ticket or the repo. No review can fix missing
            # access, so the run fails here with the planner's reason.
            if plan.status == "blocked":
                raise FatalError(plan.plan_markdown)
            # Only the Linear planner is asked for a split, and only Linear can write one.
            if plan.tickets and (await self.subject).source != "linear":
                raise FatalError(
                    "The planner proposed a split, but only a Linear ticket can split. Move the "
                    "ticket to the trigger status again for a new plan."
                )
            critique = ""
            # A split writes tickets to the tracker, so only a person approves it.
            if not plan.questions and not plan.tickets:
                if plan_gate != "human" and not reviewed:
                    # The machine reviewer gets one pass per run. A critique goes into
                    # one redraft, which continues without a second review.
                    reviewed = True
                    machine_review = await SoftwareFactory.review_plan()
                    if machine_review.decision == ReviewDecision.REQUEST_CHANGES:
                        critique = machine_review.body
                        continue
                    if plan_gate == "machine":
                        return True
                    if (
                        plan_gate == "adaptive"
                        and plan.confidence == "high"
                        and plan.acceptance_criteria
                    ):
                        return True
                elif plan_gate == "machine":
                    return True
            operator_reply = await self.review(questions=plan.questions)
            if plan.is_confirmed_by(operator_reply):
                return True
            answered_questions = plan.get_answered_questions(operator_reply.answers)
            operator_note = operator_reply.note

    @step
    async def _split(self) -> None:
        """Create the approved tickets under the spike with their "blocked by" links, then
        put the spike at rest: the tickets carry the work now."""
        spike = await self.subject
        tickets = self.journal.plan.tickets
        async with await SoftwareFactory.get_tracker() as tracker:
            parent = await tracker.get_ticket(spike.ticket_key)
            keys = {}
            for ticket in tickets:
                # A repo's name routes a ticket to it, so the label is the name after the owner.
                keys[ticket.repo] = await tracker.create_ticket(
                    f"{spike.source}:{spike.ticket_key}:{ticket.repo}",
                    team=parent["team"],
                    parent=parent["ticket_id"],
                    label=ticket.repo.rsplit("/", 1)[-1],
                    title=ticket.title,
                    description=ticket.description,
                )
            for ticket in tickets:
                await tracker.add_blockers(
                    keys[ticket.repo], [keys[blocker] for blocker in ticket.blocked_by]
                )
        await spike.release_ticket()

    async def _implement_phase(self) -> None:
        await self._create_branch()
        while True:
            previous_delivery = self.journal.last_implementation
            delivery = await self.implement()
            can_revise = (
                self.journal.implementation_revision < self._settings.max_implementation_revisions
            )
            # The implementer pushes nothing when no red check is its diff's, and CI on
            # that head has already been read.
            if not previous_delivery or delivery.head_sha != previous_delivery.head_sha:
                ci_report = await self._wait_for_ci(delivery.head_sha)
                if ci_report.conclusion == "red" and await self._rerun_failed_jobs(
                    delivery.head_sha
                ):
                    ci_report = await self._wait_for_ci(delivery.head_sha)
                if ci_report.conclusion == "red" and can_revise:
                    continue
            review = await self._review_work()
            if review.decision == "request_changes" and can_revise:
                continue
            if await self._work_gate():
                return

    async def _wait_for_ci(self, head_sha: str) -> CiReport:
        """Wait until CI has finished on ``head_sha``, and return its verdict. The wait keeps
        the implementer's box, because a red CI resumes the implementer's session, a file on
        that box."""
        started_at = await self._now()
        while not (ci_report := await self._read_ci(head_sha, started_at)):
            with suppress(WaitTimeout):
                await self.wait(
                    "check.completed",
                    label="Waiting for CI",
                    ttl_seconds=CI_POLL_SECONDS,
                    hold_box=True,
                )
        self.journal.add(ci_report)
        return ci_report

    @step(retries=3)
    async def _rerun_failed_jobs(self, head_sha: str) -> bool:
        """Rerun every failed or timed-out Actions run on ``head_sha`` once, and wait for
        the rerun to start, so the next CI read sees it and not the old red. Return whether
        a rerun is running. A red with no Actions run to rerun, such as
        ``druks-house-rules``, returns False. A run that already has a second attempt is not
        rerun again, so a retry of this step reruns no run twice."""
        repo = (await self.subject).repo
        github = await Github.get_client(repo)
        runs = await github.list_workflow_runs(repo, head_sha)
        # CI had finished when the build read it red, so a run that is active now is a rerun,
        # also when GitHub has not raised its attempt number yet.
        has_rerun = any(
            run["run_attempt"] > 1 or run["status"] in RUN_ACTIVE_STATUSES for run in runs
        )
        attempts = {}
        for run in runs:
            if run["conclusion"] not in RERUN_CONCLUSIONS or run["run_attempt"] > 1:
                continue
            try:
                await github.rerun_failed_jobs(repo, run["id"])
            except GitHubRerunRefusedError as error:
                logger.warning("Druks could not rerun %s", error)
                continue
            attempts[run["id"]] = run["run_attempt"]
        if not attempts and not has_rerun:
            return False
        for _ in range(RERUN_START_POLLS):
            runs = {run["id"]: run for run in await github.list_workflow_runs(repo, head_sha)}
            if all(
                run_id in runs
                and (
                    runs[run_id]["run_attempt"] > attempt
                    or runs[run_id]["status"] in RUN_ACTIVE_STATUSES
                )
                for run_id, attempt in attempts.items()
            ):
                break
            await asyncio.sleep(RERUN_POLL_SECONDS)
        else:
            logger.warning("The rerun on %s did not start in time", head_sha)
        return True

    @step
    async def _now(self) -> float:
        return time.time()

    # The read runs about once a minute for up to an hour, so it rides out a short
    # network error.
    @step(retries=3)
    async def _read_ci(self, head_sha: str, started_at: float) -> CiReport | None:
        """CI's verdict on ``head_sha``, or None while it can still change."""
        repo = (await self.subject).repo
        github = await Github.get_client(repo)
        return CiReport.judge(
            head_sha,
            await github.list_check_suites(repo, head_sha),
            await github.list_commit_statuses(repo, head_sha),
            waited=time.time() - started_at,
        )

    async def _review_work(self) -> ReviewReport:
        """Hand the pull request to the standalone review and wait for its decision."""
        # The body starts the child run, so a replay gets back the review it started.
        await PullRequestReview.dispatch(
            repo=(await self.subject).repo, pr_number=self.pr_number, account=self.account
        )
        facts = await self.wait("review.completed", label="Waiting for the review")
        review = ReviewReport.model_validate(facts)
        self.journal.add(review)
        return review

    async def _work_gate(self) -> bool:
        """Park for work approval. Return True when the work is done, or False when it
        goes back to implementation."""
        if self._policy.implementation_approval_gate() == "none":
            return await self._approved_work()
        label = f"Review implementation on GitHub: PR #{self.pr_number}"
        ci_report = self.journal.latest(CiReport)
        if ci_report.conclusion == "red":
            label = f"{label}. CI on {ci_report.head_sha[:7]} is red"
        if ci_report.conclusion == "unfinished":
            label = (
                f"{label}. CI on {ci_report.head_sha[:7]} did not finish within "
                f"{CI_TTL_SECONDS // 60} minutes"
            )
        decision = await ReviewWork.wait(
            input_request={
                "presentation": "external",
                "label": label,
                "url": f"https://github.com/{(await self.subject).repo}/pull/{self.pr_number}",
            }
        )
        if decision.action == "merged":
            return True
        if decision.action == "approve":
            # A person's approval stands over CI, and GitHub merges no draft.
            if self._policy.on_approval == "merge":
                await self._clear_draft()
            return await self._approved_work()
        if decision.action == "request_changes":
            return await self._triage()
        if decision.action == "revise_contract":
            await SoftwareFactory.revise_contract()
        return False

    async def _approved_work(self) -> bool:
        # GitHub announces the merge. The pr.closed reaction stores the result.
        if self._policy.on_approval == "merge":
            if await self.declare_merge_intent():
                return True
            logger.warning(
                "GitHub did not accept the merge of %s#%s. The work parks for review again.",
                (await self.subject).repo,
                self.pr_number,
            )
            return await self._work_gate()
        await self._clear_draft()
        return True

    async def _triage(self) -> bool:
        feedback = await SoftwareFactory.triage_human_feedback()
        if feedback.action == HumanFeedbackAction.CHANGE_REQUIRED:
            return False
        if feedback.action == HumanFeedbackAction.CONTRACT_CHANGE_REQUIRED:
            await SoftwareFactory.revise_contract()
            return False
        if feedback.action == HumanFeedbackAction.CLOSE:
            raise FatalError("closed at human triage")
        # NO_CHANGE and QUESTION park the work again.
        return await self._work_gate()

    # Body code, never @step. The agent calls inside memoize themselves and write the
    # journal. On replay a @step would skip them, and the journal would stay empty.
    async def implement(self) -> ImplementationOutput:
        effort = IMPLEMENT_EFFORT_BY_SIZE.get(self.journal.plan_size)
        # A red CI continues the implementer's session with the red checks, and the call
        # starts over with its full prompt when it cannot.
        resume = self._report_red_ci if self.journal.red_ci_report else None
        delivery = await SoftwareFactory.implement(effort=effort, resume=resume)
        # The implementer found a contradiction in the binding requirements and stopped.
        # The run fails with that reason, so the dashboard shows it.
        if delivery.status == "needs_clarification":
            raise FatalError(f"implementation needs clarification: {delivery.summary}")
        return delivery

    async def _report_red_ci(self, *, workspace: BuildWorkspace) -> str:
        """The message that continues the implementer's session when CI on its head is red:
        the red-CI rules of the implement prompt."""
        return await render_prompt(
            "software_factory/build/_red_ci.md", repo=(await self.subject).repo, build=self
        )

    @step
    async def _create_branch(self) -> None:
        # Replay keeps the branch: GitHub holds it, and a second pass would create the
        # next number.
        await (await self.subject).create_branch()

    @step
    async def declare_merge_intent(self) -> bool:
        """Whether GitHub accepted ownership of the merge."""
        repo = (await self.subject).repo
        github = await Github.get_client(repo)
        return await github.merge_when_ready(repo, self.pr_number)

    # The PR comes from the first delivery. Before it, this is None. Rework deliveries
    # reuse the PR, so a later delivery with another value must not change this read.
    @property
    def pr_number(self) -> int | None:
        implementations = self.journal.implementations
        return implementations[0].pr_number if implementations else None

    @step
    async def _clear_draft(self) -> None:
        await self.set_pr_draft(draft=False)

    async def request_assignee_review(self) -> None:
        login = self.journal.assignee_github_login
        repo = (await self.subject).repo
        if login:
            try:
                github = await Github.get_client(repo)
                await github.request_pull_request_reviewers(repo, self.pr_number, [login])
            except Exception:  # noqa: BLE001 — a missed ping must not fail the park
                logger.warning(
                    "could not request review from %s on %s#%s",
                    login,
                    repo,
                    self.pr_number,
                )

    async def set_pr_draft(self, *, draft: bool) -> None:
        repo = (await self.subject).repo
        try:
            github = await Github.get_client(repo)
            await github.set_pull_request_draft_state(repo, self.pr_number, draft=draft)
        except Exception:  # noqa: BLE001 — a draft merge fails loudly anyway
            logger.warning("Could not set draft=%s on %s#%s.", draft, repo, self.pr_number)


class Profile(Workflow):
    """Profile a repo once, when it joins a project. ``refresh_only`` skips the agent
    and applies the pinned verification to the stored baseline again."""

    subject = ProjectRepo
    workspace_class = RepoWorkspace

    @classmethod
    async def dispatch(cls, project_repo: ProjectRepo, *, refresh_only: bool = False) -> str:
        # The profiler clones with the operator App token. The lookup raises a clear
        # error before the run starts a VM.
        await Github.get()
        return await cls.start(
            subject=project_repo,
            repo_id=project_repo.id,
            refresh_only=refresh_only,
        )

    async def run(self, repo_id: int, refresh_only: bool = False) -> None:
        project_repo = await ProjectRepo.get_or_none(id=repo_id)

        if refresh_only:
            baseline = project_repo.profile.get("baseline") or {}
        else:
            profile = await SoftwareFactory.repo_profiler(repo=project_repo.full_name)
            baseline = profile["baseline"]
            # A purpose someone typed stays. Someone can save one while the profiler runs,
            # so read it again.
            await db_session().refresh(project_repo, ["purpose"])
            if not project_repo.purpose:
                project_repo.purpose = profile["purpose"]
            # A skill can be disabled after the prompt renders, so read the enabled
            # skills again.
            enabled = {skill.name for skill in await Skill.list_enabled(db_session())}
            baseline["recommended_skills"] = [
                name for name in baseline["recommended_skills"] if name in enabled
            ]

        policy = await RepoPolicy.resolve(project_repo.full_name)
        effective = dict(baseline)
        if policy.verification:
            effective["verification"] = policy.verification.get_commands(
                detected=baseline.get("verification") or {}
            )
        sandbox = policy.verification and policy.verification.sandbox
        if sandbox and sandbox.setup:
            repo = project_repo.full_name
            setup_script = await fetch_file(repo=repo, path=sandbox.setup)
            if not setup_script:
                raise SandboxSetupMissing(repo, sandbox.setup)
            # Drukbox keys a template by its script. The lockfiles go into the script, so a
            # changed dependency builds a new template.
            github = await Github.get_client(repo)
            lockfiles = {
                name: content
                for name in LOCKFILES
                if (content := await github.get_file_content(repo, name))
            }
            if lockfiles:
                digest = hashlib.sha256(json.dumps(lockfiles, sort_keys=True).encode())
                setup_script = f"{setup_script}\n# lockfiles {digest.hexdigest()}\n"
            try:
                template = await Sandbox(setup=sandbox.setup, script=setup_script).build(
                    label=f"{repo}:{sandbox.setup}"
                )
            except TemplateUnavailable as error:
                # A script that does not build leaves the last good template in place.
                logger.warning("The sandbox template of %s did not build: %s", repo, error)
                if previous := (project_repo.profile.get("effective") or {}).get(
                    "sandbox_template"
                ):
                    effective["sandbox_template"] = previous
            else:
                effective["sandbox_template"] = template.id
        await project_repo.set_profile(baseline=baseline, effective=effective)

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        return {
            "repo": (await ProjectRepo.get_or_none(id=self.input.repo_id)).full_name,
            "skills_catalog": [
                {"name": skill.name, "description": skill.description}
                for skill in await Skill.list_enabled(db_session())
            ],
            **await super().get_prompt_context(**context),
        }


class ReviewWorkspace(RepoWorkspace):
    # A checkout of the default branch, with room beside it for siblings. The reviewer
    # checks out the PR itself. The add_dirs grant needs the directory to exist.
    @classmethod
    async def get_secrets(cls, subject: Any) -> list[SandboxSecret]:
        # The review is authored under the review actor's identity.
        actor = await get_review_actor()
        return [
            SandboxSecret(
                name=actor.service.slug,
                secret_id=(await actor.service.get()).id,
                resources=await get_project_repos(subject.repo),
                host=actor.service.host,
            )
        ]

    @classmethod
    async def get_mcp_servers(cls, subject: Any) -> tuple[SandboxMcpServer, ...]:
        actor = await get_review_actor()
        return (
            SandboxMcpServer(
                name=GITHUB_MCP_NAME,
                url=GITHUB_MCP_URL,
                secret_id=(await actor.service.get()).id,
                resources=await get_project_repos(subject.repo),
            ),
        )

    @property
    def related_root(self) -> str:
        return get_related_root(self.host.ssh_username)

    async def prepare(self) -> None:
        await self.host.exec(["mkdir", "-p", self.related_root], timeout=10.0)
        await super().prepare()

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        kwargs = super().get_agent_run_kwargs(**kwargs)
        kwargs["add_dirs"] = (self.related_root,)
        return kwargs


class PullRequestReview(Workflow):
    """Review one pull request against a checkout of its repo and the repos around it.
    The reviewer reads, judges, and posts the review itself."""

    subject = PullRequest
    workspace_class = ReviewWorkspace

    @classmethod
    async def dispatch(cls, *, repo: str, pr_number: int, account: Account, note: str = "") -> str:
        """Start a review under the account that asks for it."""
        # The review workspace sets its git author from the operator App, even when a
        # reviewer is connected. The lookup raises a clear error before the run starts a VM.
        await Github.get()
        return await cls.start(
            subject=PullRequest(id=f"{repo}#{pr_number}"), account_id=account.id, note=note
        )

    async def run_multistep(self, note: str = "") -> ReviewReport:
        # The finished event carries this, so a build waiting on the review reads it there.
        return await SoftwareFactory.review_pull_request()

    async def get_prompt_context(self, **context: Any) -> dict[str, Any]:
        project_repo = await ProjectRepo.get_for_repo(
            (await self.subject).repo, raise_on_missing=True
        )
        # The reviewer writes on GitHub, so the requester goes by their GitHub login.
        sign_ins = await Github.list_for_account(self.account.id)
        requested_by = sign_ins[0].identity["login"] if sign_ins else self.account.username
        return {
            "siblings": await project_repo.get_siblings(),
            "review_mode": (await get_review_actor()).mode,
            "requested_by": requested_by,
            **await super().get_prompt_context(**context),
        }
