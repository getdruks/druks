import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from druks.contrib import software_factory
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.contracts import (
    CiReport,
    CommandCheckOutput,
    FindingOutput,
    ImplementationOutput,
    ReviewReport,
    TriageOutput,
)
from druks.contrib.software_factory.enums import HumanFeedbackAction
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.contrib.software_factory.policy import RepoPolicy
from druks.contrib.software_factory.prompt_context import BuildPromptContext
from druks.prompts import render_prompt
from druks.workflows import FatalError

_OP_TEMPLATES = [
    "generate_plan.md",
    "review_plan.md",
    "revise_contract.md",
    "implement.md",
    "triage_human_feedback.md",
]

# The kwargs the workflow passes at each template's agent call site.
_CALL_KWARGS = {
    "generate_plan.md": {"answered_questions": [], "operator_note": "", "reviewer_notes": ""},
}


def _build() -> SimpleNamespace:
    """A stand-in BuildPromptContext exposing the fields the templates read —
    identity facts faked, the journal real and empty."""
    return SimpleNamespace(
        repo="acme/widget",
        work_item_url="https://druks.test/work-items/1",
        branch="agent/eng-1",
        pr_number=7,
        ticket_ref="ACME-1",
        source="github",
        assignee_name=None,
        assignee_email=None,
        related_repos=[],
        blockers=[],
        run_id="run-1",
        checks=(),
        skills=[
            SimpleNamespace(
                name="python-house-rules",
                description="Apply the Python house rules.",
            )
        ],
        journal=BuildJournal(),
    )


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(
        repo_path="/home/agent/work/repo",
        workspace_root="/home/agent/work",
    )


@pytest.mark.parametrize("template", _OP_TEMPLATES)
async def test_build_operation_prompt_renders(template):
    await render_prompt(
        f"software_factory/build/{template}",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )


async def _generate_plan_prompt(
    *, answered_questions=None, operator_note="", reviewer_notes=""
) -> str:
    return await render_prompt(
        "software_factory/build/generate_plan.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        answered_questions=answered_questions or [],
        operator_note=operator_note,
        reviewer_notes=reviewer_notes,
    )


async def test_generate_plan_prompt_prioritizes_operator_note_over_reviewer_critique():
    prompt = await _generate_plan_prompt(
        operator_note="keep the existing endpoint",
        reviewer_notes="replace the endpoint",
    )

    assert "## Operator note" in prompt
    assert "> keep the existing endpoint" in prompt
    assert "## Plan reviewer critique" in prompt
    assert "> replace the endpoint" in prompt
    assert (
        prompt.count(
            "Where the operator's note conflicts with this critique, the operator's note wins."
        )
        == 1
    )
    assert "Before deep code reading" not in prompt


async def test_generate_plan_prompt_asks_for_a_change_map_from_files_it_opened():
    prompt = await _generate_plan_prompt()

    assert "End the plan with a `## Change map`" in prompt
    assert "never from a guess" in prompt
    assert "never by line range" in " ".join(prompt.split())


async def test_implement_prompt_starts_from_the_plans_change_map():
    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        can_resume=False,
    )

    assert "Start from the change map" in prompt
    assert "say so in `known_risks`" in prompt


async def test_generate_plan_prompt_without_reviewer_notes_omits_critique_instructions():
    prompt = await _generate_plan_prompt(operator_note="keep the existing endpoint")

    assert "## Plan reviewer critique" not in prompt
    assert "Where the operator's note conflicts with this critique" not in prompt


async def test_generate_plan_prompt_keeps_first_draft_ambiguity_instructions():
    prompt = await _generate_plan_prompt()

    assert "Before deep code reading" in prompt


async def test_verification_profile_renders_ci_provenance_per_command():
    block = await RepoPolicy().verification_block(
        profile={
            "verification": {
                "lint_commands": [{"command": "ruff check .", "ci_check": "Backend / lint"}],
                "typecheck_commands": [],
                "test_commands": [{"command": "pytest", "ci_check": None}],
            }
        },
        repo=None,
    )

    assert block.splitlines() == [
        "## Verification profile",
        "",
        "**Lint:**",
        "- `ruff check .` (CI job `Backend / lint` runs this too)",
        "**Tests:**",
        "- `pytest`",
    ]


async def _implement_prompt(**build_fields) -> str:
    build = _build()
    for name, value in build_fields.items():
        setattr(build, name, value)
    return await render_prompt(
        "software_factory/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )


async def test_the_implement_prompt_names_the_check_tool_and_the_run_when_checks_are_declared():
    prompt = await _implement_prompt(run_id="run-42", checks=("make test",))

    assert "`core_run_check`" in prompt
    assert "`run_id` set to `run-42`" in prompt
    assert "A command that you narrowed to some files is not a listed command" in prompt
    # The agent that is told `started` ends its turn, and the result is its next message.
    assert "or `started`: then end your turn at once" in prompt
    assert "Your next message holds the result" in prompt


def test_the_implementer_can_end_its_turn_for_a_check_and_be_resumed():
    assert SoftwareFactory.implement.resumable is True


async def test_the_implement_prompt_without_declared_checks_never_mentions_the_tool():
    prompt = await _implement_prompt(checks=())

    assert "core_run_check" not in prompt
    # The paragraph leaves no blank line behind where it would have been.
    assert "\n\n\n" not in prompt.split("## Delivering your work")[0].splitlines()[-1]
    assert "\n\n## Delivering your work" in prompt


async def test_implement_prompt_renders_the_prior_review():
    build = _build()
    finding = FindingOutput(
        severity="high",
        summary="The flag keeps its old name.",
        evidence="Rename it everywhere.",
        path="app/flags.py",
        line=12,
        start_line=None,
    )
    build.journal.add(
        ReviewReport(
            decision="request_changes",
            summary="Rename the flag.",
            findings=[finding],
            context_repos=[],
        )
    )

    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "## Prior implementation review" in prompt
    assert "### Round 1 — decision: request_changes" in prompt
    assert "Rename the flag." in prompt
    assert "- **high** The flag keeps its old name. (`app/flags.py:12`)" in prompt


def _delivery(**overrides) -> ImplementationOutput:
    fields = dict(
        type="result",
        status="success",
        base_sha="1a2b3c4d",
        head_sha="5e6f7a8b",
        commit_sha="5e6f7a8b",
        pr_number=7,
        files_changed=["app/flags.py"],
        acceptance_results=[],
        checks=[CommandCheckOutput(command="ruff check .", status="pass", exit_code=0, reason="")],
        known_risks=["The old flag name stays in the changelog."],
        summary="Renamed the flag.",
        workspace_path="/home/agent/work/repo",
        workspace_retention=None,
    )
    return ImplementationOutput(**{**fields, **overrides})


async def test_implement_prompt_hands_over_the_previous_round():
    build = _build()
    build.journal.add(_delivery())

    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "## Your previous round" in prompt
    assert "You delivered `5e6f7a8b` on this branch." in prompt
    assert "Renamed the flag." in prompt
    assert "- `app/flags.py`" in prompt
    assert "- `ruff check .`: pass" in prompt
    assert "- The old flag name stays in the changelog." in prompt
    assert (
        "The branch may have moved since, so run `git log -1` before you rely on this head."
        in prompt
    )


async def test_the_previous_round_says_none_for_a_delivery_with_no_files():
    build = _build()
    build.journal.add(_delivery(files_changed=[]))

    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "**Files changed:** none\n" in prompt
    assert "**Files changed:**\n" not in prompt


async def test_implement_prompt_reads_regions_not_whole_files():
    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        can_resume=False,
    )

    flat = " ".join(prompt.split())
    assert "Read a region with the Read tool and its line range, not a whole file" in flat
    assert "only when the plan depends on it" in flat


async def test_implement_prompt_has_no_previous_round_on_the_first_round():
    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=_build(),
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "## Your previous round" not in prompt


async def test_implement_prompt_puts_red_ci_ahead_of_older_instructions():
    build = _build()
    build.journal.add(
        TriageOutput(
            action=HumanFeedbackAction.CHANGE_REQUIRED,
            body="",
            question="",
            implementation_instructions="Rename the flag.",
        )
    )
    build.journal.add(CiReport(head_sha="0f3a2c1d", conclusion="red"))

    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "## CI is red" in prompt
    assert "CI on `0f3a2c1d` is red." in prompt
    assert "## Revision request" not in prompt
    assert "Implement the approved plan" not in prompt


@pytest.mark.parametrize("template", ["generate_plan.md", "implement.md"])
async def test_a_prompt_lists_each_blocker(template):
    build = _build()
    build.blockers = [
        {"repo": "acme/api", "pr_number": 12, "title": "Add the endpoint"},
        {"ticket_key": "ACME-2", "pr_number": None},
    ]

    prompt = await render_prompt(
        f"software_factory/build/{template}",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
        **_CALL_KWARGS.get(template, {}),
    )

    assert "- acme/api#12: Add the endpoint" in prompt
    assert "- ACME-2" in prompt


async def test_a_build_without_blockers_renders_no_builds_on_section():
    build = _build()
    build.pr_number = None

    prompt = await render_prompt(
        "software_factory/build/implement.md",
        build=build,
        verification="VERIFICATION-BLOCK",
        workspace=_workspace(),
    )

    assert "Builds on" not in prompt


async def test_only_a_linear_planner_proposes_a_split():
    prompts = {}
    for source in ("linear", "github"):
        build = _build()
        build.source = source
        prompts[source] = await render_prompt(
            "software_factory/build/generate_plan.md",
            build=build,
            verification="VERIFICATION-BLOCK",
            workspace=_workspace(),
            **_CALL_KWARGS["generate_plan.md"],
        )

    assert "`blocked_by`" in prompts["linear"]
    assert "`blocked_by`" not in prompts["github"]


def test_build_prompt_context_covers_template_attrs():
    # Every build prompt reads build.<attr>; assert BuildPromptContext carries them
    # all, so a template ref can never outrun the context contract.
    prompts_dir = Path(software_factory.__file__).parent / "templates/build"
    templates = sorted(prompts_dir.glob("*.md"))
    assert templates, f"no build prompts under {prompts_dir}"
    attrs: set[str] = set()
    for template in templates:
        attrs |= set(re.findall(r"\bbuild\.([a-z_]+)", template.read_text()))
    fields = set(BuildPromptContext.__dataclass_fields__)
    missing = sorted(a for a in attrs if a not in fields)
    assert not missing, f"BuildPromptContext missing template attrs: {missing}"


async def test_get_for_repo_returns_the_repo(druks_db):
    project = await Project.create(name="acme/widget")
    await ProjectRepo.create(project_id=project.id, full_name="acme/widget")
    assert (await ProjectRepo.get_for_repo("acme/widget")).full_name == "acme/widget"


async def test_get_for_repo_raises_when_the_repo_was_transferred(druks_db):
    # Registered under the new name; a run still holding the old name must fail
    # with the reason, not an opaque NoneType crash.
    project = await Project.create(name="czpython/druks")
    await ProjectRepo.create(project_id=project.id, full_name="czpython/druks")
    with pytest.raises(FatalError, match="renamed or transferred"):
        await ProjectRepo.get_for_repo("clawhaven/druks", raise_on_missing=True)
