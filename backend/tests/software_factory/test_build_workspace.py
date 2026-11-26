import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import connect_service
from druks.accounts.context import current_account_id
from druks.accounts.models import Account
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.constants import (
    GITHUB_MCP_NAME,
    GITHUB_MCP_URL,
    TICKET_TOOLS,
)
from druks.contrib.software_factory.exceptions import SandboxSetupMissing
from druks.contrib.software_factory.models import Project, ProjectRepo
from druks.contrib.software_factory.policy import (
    RepoPolicy,
    VerificationProfile,
    VerificationSandbox,
)
from druks.contrib.software_factory.services import GithubReviewer
from druks.contrib.software_factory.workflows import Build, BuildWorkspace, ReviewWorkspace
from druks.core.services import Github, GithubApp
from druks.mcp.exceptions import MissingEndpointError
from druks.mcp.helpers import get_bearer_token_env_var
from druks.sandbox import SandboxSecret
from druks.sandbox.host import Host
from druks.sandbox.layout import get_related_root, get_repo_root
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.settings import Urls
from druks.skills.models import Skill
from druks.workspaces import RepoWorkspace


class _FakeSandbox:
    ssh_username = "exedev"


def test_build_workspace_grants_related_root_add_dir():
    workspace = BuildWorkspace(SimpleNamespace(repo="o/main"))
    workspace.host = _FakeSandbox()  # type: ignore[assignment]
    kwargs = workspace.get_agent_run_kwargs(model="m")

    assert kwargs["model"] == "m"
    assert kwargs["add_dirs"] == (get_related_root("exedev"),)
    assert "skills" not in kwargs
    assert "mcp_servers" not in kwargs
    assert "extra_env" not in kwargs


async def test_build_workspace_makes_the_related_root_before_the_clone(
    monkeypatch: pytest.MonkeyPatch,
):
    execs: list[list[str]] = []

    async def fake_exec(self: Any, argv: list[str], **_: Any) -> None:
        execs.append(argv)

    async def base_prepare(self: Any) -> None:
        execs.append(["prepare"])

    workspace = BuildWorkspace(SimpleNamespace(repo="o/main"))
    workspace.host = Host(record=SimpleNamespace(id="h1", ssh_username="exedev"))  # type: ignore[arg-type]
    monkeypatch.setattr(Host, "exec", fake_exec)
    monkeypatch.setattr(RepoWorkspace, "prepare", base_prepare)

    await workspace.prepare()
    assert execs == [["mkdir", "-p", get_related_root("exedev")], ["prepare"]]


async def test_build_workspace_declares_its_github_mcp_as_the_operator(druks_db):
    # The review runs as the reviewer in its own workflow, so a build never acts as it.
    operator = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "reviewer-pem"},
    )
    subject = SimpleNamespace(repo="o/main")

    [github], [ref] = await BuildWorkspace.get_all_mcp_servers(druks_db, subject, None)

    assert github.url == GITHUB_MCP_URL
    assert github.bearer_token_env_var == get_bearer_token_env_var(GITHUB_MCP_NAME)
    assert ref.key == ("mcp_github_token", operator.id, ("o/main",), "api.githubcopilot.com")
    [clone] = await BuildWorkspace.get_secrets(subject)
    assert clone == SandboxSecret(
        name="github", secret_id=operator.id, resources=("o/main",), host="github.com"
    )


async def test_a_build_gets_the_skills_its_repo_profile_recommends(monkeypatch: pytest.MonkeyPatch):
    async def get_for_repo(repo: str, *, raise_on_missing: bool = False) -> SimpleNamespace:
        return SimpleNamespace(effective_profile={"recommended_skills": ["python-house-rules"]})

    monkeypatch.setattr(
        "druks.contrib.software_factory.workflows.ProjectRepo.get_for_repo", get_for_repo
    )

    skills = await BuildWorkspace.get_skills(SimpleNamespace(repo="o/app"))

    assert skills == ("python-house-rules",)


def _set_urls(monkeypatch: pytest.MonkeyPatch, urls: Urls) -> None:
    """The address a box reaches, as the workflow and the MCP server both read it."""
    for module in ("druks.mcp.inbound", "druks.contrib.software_factory.workflows"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: SimpleNamespace(urls=urls))


async def _required_servers(monkeypatch: pytest.MonkeyPatch, tracker: str):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    _set_urls(monkeypatch, Urls(endpoint="https://druks.test", webhook_host=""))
    settings = SoftwareFactory.Settings(tracker=tracker)
    monkeypatch.setattr(SoftwareFactory, "settings", AsyncMock(return_value=settings))
    return await BuildWorkspace.get_mcp_servers(SimpleNamespace(repo="o/main"))


async def test_a_board_build_requires_the_appliance_mcp(druks_db, monkeypatch):
    servers = await _required_servers(monkeypatch, "druks")

    assert [server.name for server in servers] == [GITHUB_MCP_NAME, "druks"]
    board = servers[-1]
    assert (board.url, board.secret_id, board.allowed_tools) == (
        "https://druks.test/mcp",
        "",
        # The name of the route's operation id, namespaced by core: a literal, so that a
        # renamed tool fails here and not on a box.
        (*TICKET_TOOLS, "core_run_check"),
    )


async def test_a_linear_build_holds_the_check_tool_beside_github(druks_db, monkeypatch):
    servers = await _required_servers(monkeypatch, "linear")

    assert [server.name for server in servers] == [GITHUB_MCP_NAME, "druks"]
    assert servers[-1].allowed_tools == ("core_run_check",)


async def test_a_linear_build_on_an_install_with_no_address_still_builds_with_github_alone(
    druks_db, monkeypatch
):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    _set_urls(monkeypatch, Urls(endpoint="", webhook_host=""))
    monkeypatch.setattr(
        SoftwareFactory,
        "settings",
        AsyncMock(return_value=SoftwareFactory.Settings(tracker="linear")),
    )

    servers = await BuildWorkspace.get_mcp_servers(SimpleNamespace(repo="o/main"))

    assert [server.name for server in servers] == [GITHUB_MCP_NAME]


async def test_a_board_build_on_an_install_with_no_address_still_fails_loudly(
    druks_db, monkeypatch
):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    _set_urls(monkeypatch, Urls(endpoint="", webhook_host=""))
    monkeypatch.setattr(
        SoftwareFactory,
        "settings",
        AsyncMock(return_value=SoftwareFactory.Settings(tracker="druks")),
    )

    with pytest.raises(MissingEndpointError):
        await BuildWorkspace.get_mcp_servers(SimpleNamespace(repo="o/main"))


async def _repo_with_commands(
    commands: list[str], template: str | None = "tpl-1"
) -> SimpleNamespace:
    project = await Project.create(name="Checks")
    repo = await ProjectRepo.create(project_id=project.id, full_name="o/main")
    await repo.set_profile(
        baseline={},
        effective={
            "verification": {
                "test_commands": [{"command": command, "ci_check": None} for command in commands],
                "lint_commands": [{"command": "ruff check .", "ci_check": None}],
            },
            **({"sandbox_template": template} if template else {}),
        },
    )
    return SimpleNamespace(repo="o/main")


def _sandbox_config(monkeypatch, **fields):
    async def resolve(repo):
        return RepoPolicy(verification=VerificationProfile(sandbox=VerificationSandbox(**fields)))

    monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(resolve))


async def test_a_build_checks_on_the_template_and_env_of_its_repo(druks_db, monkeypatch):
    project = await Project.create(name="Box")
    repo = await ProjectRepo.create(project_id=project.id, full_name="o/main")
    await repo.set_profile(baseline={}, effective={"sandbox_template": "template-1"})
    _sandbox_config(monkeypatch, setup=".druks/software_factory/setup.sh", env={"A": "1"})

    box = await BuildWorkspace.get_check_box(SimpleNamespace(repo="o/main"))

    assert box == {"template": "template-1", "env": {"A": "1"}}


async def test_a_build_of_a_repo_with_no_template_and_no_env_describes_no_check_box(
    druks_db, monkeypatch
):
    project = await Project.create(name="Plain")
    await ProjectRepo.create(project_id=project.id, full_name="o/main")

    async def resolve(repo):
        return RepoPolicy()

    monkeypatch.setattr(RepoPolicy, "resolve", staticmethod(resolve))

    assert await BuildWorkspace.get_check_box(SimpleNamespace(repo="o/main")) == {}


async def test_a_build_declares_the_test_commands_of_its_repo_and_no_others(druks_db, monkeypatch):
    await _required_servers(monkeypatch, "linear")
    subject = await _repo_with_commands(["uv run pytest backend/", "npm test"])

    assert await BuildWorkspace.get_checks(subject) == ("uv run pytest backend/", "npm test")


async def test_a_build_of_a_repo_with_no_template_still_declares_its_checks(druks_db, monkeypatch):
    await _required_servers(monkeypatch, "linear")
    subject = await _repo_with_commands(["uv run pytest backend/"], template=None)

    assert await BuildWorkspace.get_checks(subject) == ("uv run pytest backend/",)


async def test_a_build_of_a_repo_the_profiler_has_not_run_on_declares_no_checks(
    druks_db, monkeypatch
):
    await _required_servers(monkeypatch, "linear")
    project = await Project.create(name="Fresh")
    await ProjectRepo.create(project_id=project.id, full_name="o/main")

    assert await BuildWorkspace.get_checks(SimpleNamespace(repo="o/main")) == ()


async def test_a_build_whose_box_cannot_reach_druks_declares_no_checks(druks_db, monkeypatch):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    _set_urls(monkeypatch, Urls(endpoint="", webhook_host=""))
    monkeypatch.setattr(
        SoftwareFactory,
        "settings",
        AsyncMock(return_value=SoftwareFactory.Settings(tracker="linear")),
    )
    subject = await _repo_with_commands(["make test"])

    assert await BuildWorkspace.get_checks(subject) == ()


def test_the_build_clones_as_the_operator():
    assert BuildWorkspace.github is Github


async def test_review_mcp_and_gh_use_the_review_actor(druks_db):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    reviewer = await connect_service(
        "github_reviewer",
        identity={"app_id": "2", "slug": "druks-reviewer"},
        secrets={"private_key": "reviewer-pem"},
    )
    subject = SimpleNamespace(repo="o/app")

    [github], [ref] = await ReviewWorkspace.get_all_mcp_servers(druks_db, subject, None)

    assert github.url == GITHUB_MCP_URL
    assert github.bearer_token_env_var == get_bearer_token_env_var(GITHUB_MCP_NAME)
    assert ref.key == ("mcp_github_token", reviewer.id, ("o/app",), "api.githubcopilot.com")
    [clone] = await ReviewWorkspace.get_secrets(subject)
    assert clone == SandboxSecret(
        name="github_reviewer", secret_id=reviewer.id, resources=("o/app",), host="github.com"
    )


@pytest.mark.parametrize("workspace_class", [BuildWorkspace, ReviewWorkspace])
async def test_the_github_token_reaches_the_project_siblings(druks_db, workspace_class):
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )
    project = await Project.create(name="Acme")
    await ProjectRepo.create(project_id=project.id, full_name="acme/api")
    await ProjectRepo.create(project_id=project.id, full_name="acme/web")
    await druks_db.flush()

    [clone] = await workspace_class.get_secrets(SimpleNamespace(repo="acme/api"))

    assert clone.resources == ("acme/api", "acme/web")


class _IdentitySandbox:
    ssh_username = "exedev"

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path

    async def exec(self, command: list[str], *, timeout: float = 30.0) -> Any:
        script = command[2].replace(
            get_repo_root(self.ssh_username), shlex.quote(str(self.repo_path))
        )
        result = subprocess.run(["sh", "-c", script], check=False, capture_output=True, text=True)
        return SimpleNamespace(returncode=result.returncode, stderr="")


def _bot_author(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bot_git_author() -> tuple[str, str]:
        return "app[bot]", "1+app[bot]@users.noreply.github.com"

    monkeypatch.setattr(GithubApp, "get_bot_git_author", _bot_git_author)


async def _github_sign_in(druks_db, username: str, *, user_id: str, login: str) -> str:
    account = await Account.get_or_create(druks_db, username)
    await VaultSecret.connect(
        druks_db,
        Audience.service(Github.slug),
        account_id=account.id,
        refresh_token="rt",
        scopes=[],
        identity={"authority": "https://github.com", "subject": user_id, "login": login},
    )
    return account.id


async def test_set_git_identity_stamps_the_workspace_repo(
    druks_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    _bot_author(monkeypatch)
    first = await _github_sign_in(druks_db, "dev@example.com", user_id="42", login="octocat")
    second = await _github_sign_in(druks_db, "auth0|7", user_id="7", login="hubot")
    unlinked = await Account.get_or_create(druks_db, "nolink@example.com")
    workspace = RepoWorkspace(SimpleNamespace(repo="o/app"))
    workspace.host = _IdentitySandbox(repo_path)  # type: ignore[assignment]
    dispatch = current_account_id.set(first)
    hook = repo_path / ".git" / "hooks" / "prepare-commit-msg"
    message = repo_path / "COMMIT_EDITMSG"

    await workspace.set_git_identity()
    # A co-author line already in the message does not take the dispatcher's place.
    message.write_text("Change\n\nCo-Authored-By: Dana <dana@example.com>\n")
    subprocess.run([str(hook), str(message), "squash"], check=True)
    subprocess.run([str(hook), str(message)], check=True)
    email = subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.email"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert email.stdout.strip() == "1+app[bot]@users.noreply.github.com"
    trailer = "Co-Authored-By: octocat <42+octocat@users.noreply.github.com>"
    assert message.read_text().count(trailer) == 1
    # An empty message stays empty, so git still aborts the commit.
    message.write_text("\n# Please enter the commit message.\n")
    subprocess.run([str(hook), str(message)], check=True)
    assert "Co-Authored-By" not in message.read_text()

    # A reused warm host follows the next run's dispatcher.
    current_account_id.set(second)
    await workspace.set_git_identity()
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message)], check=True)
    assert "octocat" not in message.read_text()
    assert "Co-Authored-By: hubot <7+hubot@users.noreply.github.com>" in message.read_text()

    # A workspace that acts as another App still credits the dispatcher's GitHub sign-in.
    workspace.github = GithubReviewer  # type: ignore[assignment]
    await workspace.set_git_identity()
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message)], check=True)
    assert "Co-Authored-By: hubot <7+hubot@users.noreply.github.com>" in message.read_text()

    # A dispatcher with no GitHub sign-in credits nobody.
    current_account_id.set(unlinked.id)
    await workspace.set_git_identity()
    assert not hook.exists()

    # A system dispatch keeps the author but credits nobody.
    current_account_id.set(second)
    await workspace.set_git_identity()
    current_account_id.set(None)
    await workspace.set_git_identity()
    assert not hook.exists()
    current_account_id.reset(dispatch)


async def test_a_builds_prompt_carries_its_run_id_and_the_checks_of_its_repo(druks_db, monkeypatch):
    flow = Build()
    flow._workflow_id = "run-77"
    flow._policy = RepoPolicy()
    flow._profile = {}
    flow._blockers = []
    flow.input = SimpleNamespace(assignee_name=None, assignee_email=None)
    flow.subject = SimpleNamespace(
        repo="o/main", branch="agent/x", ticket_key="X-1", source="github", id="1"
    )
    project = await Project.create(name="Prompt")
    await ProjectRepo.create(project_id=project.id, full_name="o/main")
    monkeypatch.setattr(ProjectRepo, "get_siblings", AsyncMock(return_value=[]))
    monkeypatch.setattr(Skill, "list_delivered", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        BuildWorkspace, "get_checks", AsyncMock(return_value=("make test", "make lint"))
    )

    context = await flow.get_prompt_context()

    assert context["build"].run_id == "run-77"
    assert context["build"].checks == ("make test", "make lint")


START = ".druks/software_factory/start.sh"


async def _repo_with_template(monkeypatch, start_body):
    project = await Project.create(name="Start")
    repo = await ProjectRepo.create(project_id=project.id, full_name="o/main")
    await repo.set_profile(baseline={}, effective={"sandbox_template": "template-1"})
    _sandbox_config(monkeypatch, setup=".druks/software_factory/setup.sh", start=START)
    fetched = []

    async def fetch_file(*, repo, path):
        fetched.append((repo, path))
        return start_body

    monkeypatch.setattr("druks.contrib.software_factory.workflows.fetch_file", fetch_file)
    return fetched


async def test_a_build_starts_its_check_box_with_the_script_of_the_default_branch(
    druks_db, monkeypatch
):
    fetched = await _repo_with_template(monkeypatch, "redis-server --daemonize yes\n")

    box = await BuildWorkspace.get_check_box(SimpleNamespace(repo="o/main"))

    assert box == {"template": "template-1", "start": "redis-server --daemonize yes\n"}
    assert fetched == [("o/main", START)]


async def test_a_start_script_that_the_default_branch_lost_fails_loudly(druks_db, monkeypatch):
    await _repo_with_template(monkeypatch, None)

    with pytest.raises(SandboxSetupMissing, match=f"o/main has no {START}"):
        await BuildWorkspace.get_check_box(SimpleNamespace(repo="o/main"))


async def test_a_build_with_no_start_script_reads_none(druks_db, monkeypatch):
    project = await Project.create(name="Plain")
    repo = await ProjectRepo.create(project_id=project.id, full_name="o/main")
    await repo.set_profile(baseline={}, effective={"sandbox_template": "template-1"})
    _sandbox_config(monkeypatch, setup=".druks/software_factory/setup.sh")

    async def fetch_file(*, repo, path):
        raise AssertionError("no start script is configured, so none is read")

    monkeypatch.setattr("druks.contrib.software_factory.workflows.fetch_file", fetch_file)

    assert await BuildWorkspace.get_check_box(SimpleNamespace(repo="o/main")) == {
        "template": "template-1"
    }
