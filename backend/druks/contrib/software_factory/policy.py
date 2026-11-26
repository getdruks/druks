import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

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
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class VerificationSandbox(BaseModel):
    """The computer that a repo's checks run on."""

    model_config = {"frozen": True, "extra": "forbid"}

    # The script that builds the repo's template, a path in the repo's default branch. It
    # sits in the config folder, so a push to it re-profiles the repo. Without one, the
    # checks run on the sandbox's default image.
    setup: str | None = None
    # Set on the box at creation, never in the template. The values are read from the repo,
    # so they are not secrets: a secret reaches a box through the vault.
    env: dict[str, str] = Field(default_factory=dict)
    # The script that runs on the check box before each check, a path in the default branch
    # like ``setup``. A template is built once and keeps its disk, so what a boot needs, such
    # as a service or a dependency sync, lives here.
    start: str | None = None

    @field_validator("setup", "start")
    @classmethod
    def script_is_in_the_config_folder(cls, path: str | None, info: ValidationInfo) -> str | None:
        if path is None:  # ast-grep-ignore: rule-2
            return path
        in_folder = path.startswith(SETUP_FOLDER) and not path.endswith("/")
        if not in_folder or ".." in path.split("/"):
            raise ValueError(f"{info.field_name} must be a path in {SETUP_FOLDER}, not {path!r}")
        return path

    @field_validator("env")
    @classmethod
    def env_names_are_variable_names(cls, env: dict[str, str]) -> dict[str, str]:
        if bad := sorted(name for name in env if not _ENV_NAME.fullmatch(name)):
            raise ValueError(f"env names must be valid variable names: {', '.join(bad)}")
        return env

    @model_validator(mode="after")
    def env_and_start_need_a_setup_script(self) -> "VerificationSandbox":
        # Without a template the check runs on the agent's own box, which neither reaches.
        if self.env and not self.setup:
            raise ValueError(
                "env needs a setup script: only a check box made from a template gets it"
            )
        if self.start and not self.setup:
            raise ValueError(
                "start needs a setup script: only a check box made from a template runs it"
            )
        return self


class VerificationProfile(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}

    # A list that the operator writes replaces the profiler's list, even an empty one. A
    # list that the operator leaves out stays what the profiler detected.
    test_commands: tuple[str, ...] | None = None
    lint_commands: tuple[str, ...] | None = None
    typecheck_commands: tuple[str, ...] | None = None
    sandbox: VerificationSandbox | None = None

    def get_commands(self, *, detected: dict) -> dict:
        """The detected commands, with each list that the operator wrote in their place. A
        written command keeps the CI check that the profiler detected for it."""
        checks = {
            entry["command"]: entry["ci_check"]
            for entries in detected.values()
            for entry in entries
        }
        commands = {}
        for key, written in (
            ("test_commands", self.test_commands),
            ("lint_commands", self.lint_commands),
            ("typecheck_commands", self.typecheck_commands),
        ):
            # An empty list is an operator's answer, so only a missing list is None.
            if written is None:  # ast-grep-ignore: rule-2
                commands[key] = detected.get(key, [])
            else:
                commands[key] = [
                    {"command": command, "ci_check": checks.get(command)} for command in written
                ]
        return commands


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

    async def verification_block(self, *, profile: dict, repo: str | None) -> str:
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
