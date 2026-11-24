from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from druks.apps.config import resolve_app_config
from druks.prompts import render_prompt

GateValue = Literal["human", "none"]
PlanGate = Literal["human", "machine", "machine_then_human", "adaptive"]


class Gates(BaseModel):
    # Each approval gate's routing, grouped so config.yml reads them apart from
    # on_approval. None defers to the defaults resolved by RepoPolicy's gate methods.
    model_config = {"frozen": True, "extra": "forbid"}

    plan_approval: PlanGate | None = None
    implementation_approval: GateValue | None = None


SETUP_FOLDER = ".druks/software_factory/"


def _in_setup_folder(path: str, key: str) -> str:
    if not path.startswith(SETUP_FOLDER) or ".." in path.split("/"):
        raise ValueError(f"{key} must be a path in {SETUP_FOLDER}, not {path!r}")
    return path


class SandboxSize(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    cpu: int = Field(ge=1)
    memory_gb: int = Field(ge=1)


class VerificationSandbox(BaseModel):
    """The computer that a repo's checks run on."""

    model_config = {"frozen": True, "extra": "forbid"}

    # The script that builds the repo's template, a path in the repo's default branch. It
    # sits in the config folder, so a push to it re-profiles the repo.
    setup: str
    # The script that runs on every boot, for services and a dependency sync.
    start: str | None = None
    size: SandboxSize | None = None
    # Values that are set on the box when it is leased, never in the template. They sit in
    # the profile and the config, so they are not secrets.
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("setup")
    @classmethod
    def setup_is_in_the_config_folder(cls, setup: str) -> str:
        return _in_setup_folder(setup, "setup")

    @field_validator("start")
    @classmethod
    def start_is_in_the_config_folder(cls, start: str | None) -> str | None:
        return start if start is None else _in_setup_folder(start, "start")


class VerificationProfile(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    # A list that the operator writes replaces the profiler's list, even an empty one. A
    # list that the operator leaves out stays what the profiler detected.
    test_commands: tuple[str, ...] | None = None
    lint_commands: tuple[str, ...] | None = None
    typecheck_commands: tuple[str, ...] | None = None
    sandbox: VerificationSandbox | None = None

    def get_commands(self, *, detected: dict[str, Any]) -> dict[str, Any]:
        """The detected commands, with each list that the operator wrote in their place. A
        written command keeps the CI check that the profiler detected for it."""
        checks = {
            entry["command"]: entry["ci_check"]
            for entries in detected.values()
            for entry in entries
        }
        return {
            key: [{"command": command, "ci_check": checks.get(command)} for command in commands]
            # An empty list is an operator's answer, so only a missing list is None.
            # ast-grep-ignore: rule-2
            if commands is not None
            else detected.get(key, [])
            for key, commands in (
                ("test_commands", self.test_commands),
                ("lint_commands", self.lint_commands),
                ("typecheck_commands", self.typecheck_commands),
            )
        }


class RepoPolicy(BaseModel):
    """The operator's ``.druks/software_factory/config.yml``, validated whole so a typo'd
    key fails loud at resolution."""

    model_config = {"frozen": True, "extra": "forbid"}

    gates: Gates = Field(default_factory=Gates)
    on_approval: Literal["merge", "none"] = "none"
    delete_branch: bool = True
    # Operator-pinned verification commands. None → the repo profiler's
    # detected commands stand; each list written here replaces its detected list.
    verification: VerificationProfile | None = None

    @classmethod
    async def resolve(cls, repo: str | None) -> "RepoPolicy":
        return await resolve_app_config("software_factory", repo=repo, model=cls)

    def plan_approval_gate(self, workflow_setting: PlanGate) -> PlanGate:
        return self.gates.plan_approval or workflow_setting

    def implementation_approval_gate(self) -> GateValue:
        return self.gates.implementation_approval or "human"

    async def verification_block(self, *, profile: dict[str, Any], repo: str | None) -> str:
        # The agent-facing verification guidance: the profile's effective
        # commands. ``profile`` is {} until the repo profiler has run — the
        # no-commands branch then carries the "don't invent verification commands"
        # guardrail.
        verification = profile.get("verification") or {}
        sections = [
            {"label": "Lint", "command_entries": verification.get("lint_commands", [])},
            {
                "label": "Typecheck",
                "command_entries": verification.get("typecheck_commands", []),
            },
            {"label": "Tests", "command_entries": verification.get("test_commands", [])},
        ]
        body = await render_prompt(
            "software_factory/verification_block.md",
            repo=repo,
            sections=sections,
            has_commands=any(section["command_entries"] for section in sections),
        )
        return body.rstrip() + "\n"
