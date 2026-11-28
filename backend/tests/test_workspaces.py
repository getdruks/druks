import asyncio
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY

import pytest
from conftest import connect_service
from druks import workspaces as workspace_mod
from druks.contrib.software_factory.services import GithubReviewer
from druks.core.apis.github import GitHubAppClient
from druks.core.services import Github
from druks.db import db_session
from druks.exceptions import SessionNotBoundError
from druks.harnesses.claude import ClaudeHarness
from druks.sandbox import SandboxSecret
from druks.sandbox.constants import WORKFLOW_HOST_LEASE_SECONDS
from druks.sandbox.exceptions import ExecFailed
from druks.sandbox.layout import get_repo_root
from druks.secrets.models import VaultSecret
from druks.workspaces import RepoWorkspace, Workspace

_CONFIG = SimpleNamespace(
    harness_class=ClaudeHarness,
    model="anthropic/claude-opus-4-8",
    fast_mode=False,
    effort=None,
    timeout=60,
    identity={},
    include_apps=False,
)


class _RecordingHost:
    id = "h1"
    ssh_username = "exedev"

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []


async def test_every_agent_call_gets_the_workspace_env(druks_db):
    class DeployWorkspace(Workspace):
        def get_env(self) -> dict[str, str]:
            return {"DEPLOY_TOKEN": "token"}

    calls: list[dict[str, Any]] = []

    class _Host:
        async def run_agent(self, harness, manifest, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "result"

    workspace = DeployWorkspace(None)
    workspace.host = _Host()  # type: ignore[assignment]
    await workspace.run_agent(
        harness=None,
        manifest=None,
        timeout=_CONFIG.timeout,
        identity=_CONFIG.identity,
        include_apps=_CONFIG.include_apps,
        prompt="p",
    )

    assert calls == [
        {
            "extra_env": {"DEPLOY_TOKEN": "token"},
            "prompt": "p",
            "timeout": 60,
            "identity": {},
            "include_apps": False,
        }
    ]


async def test_repo_workspace_prepares_the_clone_and_the_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    host = _RecordingHost()
    subject = SimpleNamespace(repo="acme/widgets", ref="feature")
    workspace = RepoWorkspace(subject)
    workspace.host = host  # type: ignore[assignment]

    async def _ensure(_host: Any, *, repo_url: str, ref: str | None, target_path: str) -> None:
        host.events.append(("clone", repo_url, ref, target_path))

    async def _git_identity(self: RepoWorkspace) -> None:
        host.events.append(("git_identity",))

    monkeypatch.setattr(workspace_mod.checkout, "ensure", _ensure)
    monkeypatch.setattr(RepoWorkspace, "set_git_identity", _git_identity)

    await workspace.prepare()
    assert host.events == [
        ("clone", "https://github.com/acme/widgets", "feature", get_repo_root("exedev")),
        ("git_identity",),
    ]


async def test_repo_workspace_diff_holds_commits_edits_and_untracked_files(tmp_path: Path):
    def git(*args: str, cwd: Path = tmp_path) -> str:
        identity = ("-c", "user.name=dev", "-c", "user.email=dev@example.com")
        return subprocess.run(
            ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q", "-b", "main", "origin")
    (tmp_path / "origin" / "kept.txt").write_text("kept\n")
    git("add", "kept.txt", cwd=tmp_path / "origin")
    git("commit", "-q", "-m", "start", cwd=tmp_path / "origin")
    git("clone", "-q", "origin", "checkout")
    checkout = tmp_path / "checkout"
    git("checkout", "-q", "-b", "feature", cwd=checkout)
    (checkout / "committed.txt").write_text("committed\n")
    git("add", "committed.txt", cwd=checkout)
    git("commit", "-q", "-m", "change", cwd=checkout)
    (checkout / "kept.txt").write_text("edited\n")
    (checkout / "untracked.txt").write_text("untracked\n")

    class _CheckoutHost:
        ssh_username = "exedev"

        async def exec(
            self, command: list[str], *, cwd: str = "", timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            assert cwd == get_repo_root(self.ssh_username)
            return subprocess.run(command, cwd=checkout, capture_output=True, text=True)

    workspace = RepoWorkspace(None)
    workspace.host = _CheckoutHost()  # type: ignore[assignment]
    diff = await workspace.get_diff()

    for path in ("committed.txt", "kept.txt", "untracked.txt"):
        assert f"+++ b/{path}" in diff
    assert "?? untracked.txt" in git("status", "--short", cwd=checkout)


async def test_repo_workspace_diff_carries_a_binary_file_only_when_asked(tmp_path: Path):
    def git(*args: str, cwd: Path = tmp_path, stdin: str | None = None) -> str:
        identity = ("-c", "user.name=dev", "-c", "user.email=dev@example.com")
        return subprocess.run(
            ["git", *identity, *args],
            cwd=cwd,
            input=stdin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    git("init", "-q", "-b", "main", "origin")
    (tmp_path / "origin" / "kept.txt").write_text("kept\n")
    git("add", "kept.txt", cwd=tmp_path / "origin")
    git("commit", "-q", "-m", "start", cwd=tmp_path / "origin")
    git("clone", "-q", "origin", "checkout")
    git("clone", "-q", "origin", "elsewhere")
    checkout = tmp_path / "checkout"
    (checkout / "logo.bin").write_bytes(bytes(range(256)) * 8)

    class _CheckoutHost:
        ssh_username = "exedev"

        async def exec(
            self, command: list[str], *, cwd: str = "", timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(command, cwd=checkout, capture_output=True, text=True)

    workspace = RepoWorkspace(None)
    workspace.host = _CheckoutHost()  # type: ignore[assignment]

    assert "GIT binary patch" not in await workspace.get_diff()
    git("apply", "-", cwd=tmp_path / "elsewhere", stdin=await workspace.get_diff(binary=True))
    assert (tmp_path / "elsewhere" / "logo.bin").read_bytes() == (
        checkout / "logo.bin"
    ).read_bytes()


async def _connect(slug: str = "github") -> VaultSecret:
    return await connect_service(
        slug, identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )


async def test_repo_workspace_names_its_github_secret_and_repo_before_the_box_exists(druks_db):
    row = await _connect()
    subject = SimpleNamespace(repo="acme/widgets")

    [secret] = await RepoWorkspace.get_secrets(subject)

    assert secret == SandboxSecret(
        name="github", secret_id=row.id, resources=("acme/widgets",), host="github.com"
    )
    assert await Workspace.get_secrets(subject) == []


async def test_a_workspace_selects_its_github_identity_by_service(druks_db):
    row = await _connect("github_reviewer")

    class Reviewing(RepoWorkspace):
        github = GithubReviewer

    [secret] = await Reviewing.get_secrets(SimpleNamespace(repo="o/r"))

    assert (secret.secret_id, secret.resources) == (row.id, ("o/r",))


async def test_the_github_service_issues_its_installation_token(
    druks_db, monkeypatch: pytest.MonkeyPatch
):
    expiry = datetime.now(UTC) + timedelta(hours=1)
    asked: list[tuple] = []

    async def get_installation(self, owner_or_repo: str) -> SimpleNamespace:
        return SimpleNamespace(id=7)

    async def create_installation_token(self, installation_id, repositories=()):
        asked.append((self._app_id, installation_id, *repositories))
        return "ghs_operator", expiry

    monkeypatch.setattr(GitHubAppClient, "get_installation", get_installation)
    monkeypatch.setattr(GitHubAppClient, "create_installation_token", create_installation_token)
    await connect_service(
        "github",
        identity={"app_id": "1", "slug": "druks-operator"},
        secrets={"private_key": "operator-pem"},
    )

    assert await Github.issue_token(("acme/widgets",)) == ("ghs_operator", expiry)
    assert asked == [("1", 7, "widgets")]


def _mint_tokens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    minted: list[str] = []

    async def get_installation(self, owner_or_repo: str) -> SimpleNamespace:
        return SimpleNamespace(id=7)

    async def create_installation_token(self, installation_id, repositories=()):
        minted.append(f"ghs_{len(minted)}")
        return minted[-1], datetime.now(UTC) + timedelta(hours=1)

    monkeypatch.setattr(GitHubAppClient, "get_installation", get_installation)
    monkeypatch.setattr(GitHubAppClient, "create_installation_token", create_installation_token)
    return minted


def _refuse_checkouts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """Each checkout fails with the next answer; an empty answer succeeds."""
    asked: list[str] = []

    async def _ensure(_host: Any, *, repo_url: str, ref: str | None, target_path: str) -> None:
        asked.append(repo_url)
        if answer := answers[len(asked) - 1]:
            raise ExecFailed(f"git clone {repo_url} failed: {answer}", exit_code=128)

    monkeypatch.setattr(workspace_mod.checkout, "ensure", _ensure)
    return asked


def _raise_session_not_bound() -> None:
    raise SessionNotBoundError


def _record_refreshes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    refreshed: list[str] = []

    async def request_refreshes(session, secret_id: str, *, except_host_id: str = "") -> None:
        refreshed.append(secret_id)

    monkeypatch.setattr(workspace_mod.sandbox_client, "request_refreshes", request_refreshes)
    return refreshed


async def test_a_refused_checkout_retries_once_with_a_new_github_token(
    druks_db, monkeypatch: pytest.MonkeyPatch
):
    minted = _mint_tokens(monkeypatch)
    row = await _connect()
    assert await Github.issue_token(("acme/widgets",)) == (minted[0], ANY)
    asked = _refuse_checkouts(monkeypatch, "remote: Repository not found.", "")
    refreshed = _record_refreshes(monkeypatch)
    workspace = RepoWorkspace(SimpleNamespace(repo="acme/widgets", ref=None))
    workspace.host = _RecordingHost()  # type: ignore[assignment]

    # prepare() runs on a task of its own with no session bound, so a read there raises.
    monkeypatch.setattr(db_session.registry, "createfunc", _raise_session_not_bound)
    await asyncio.create_task(workspace.clone())

    assert len(asked) == 2
    assert refreshed == [row.id]
    # The kept token is gone, so the box's next fetch mints a new one.
    assert await Github.issue_token(("acme/widgets",)) == (minted[1], ANY)


async def test_a_checkout_refused_again_raises_the_first_failure(
    druks_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _mint_tokens(monkeypatch)
    await _connect()
    asked = _refuse_checkouts(monkeypatch, "remote: Repository not found.", "second failure")
    _record_refreshes(monkeypatch)
    workspace = RepoWorkspace(SimpleNamespace(repo="acme/widgets", ref=None))
    workspace.host = _RecordingHost()  # type: ignore[assignment]

    with (
        caplog.at_level("WARNING", logger="druks.workspaces"),
        pytest.raises(ExecFailed, match="Repository not found") as raised,
    ):
        await workspace.clone()

    assert len(asked) == 2
    assert "second failure" in str(raised.value.__cause__)
    assert "second failure" in caplog.text


class _LocalBox:
    """A box that is a folder of this machine: commands run here, and a transfer is a
    copy. It stands in for SSH, so the copy of a checkout is made with real git."""

    def __init__(self, user: str) -> None:
        self.ssh_username = self.id = user

    async def exec(self, cmd, *, cwd="", timeout=30.0):
        return await asyncio.to_thread(
            subprocess.run, cmd, cwd=cwd or None, capture_output=True, text=True
        )

    async def upload_file(self, *, local, remote):
        shutil.copy(local, remote)

    async def download(self, *, remote, local):
        shutil.copy(remote, local)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *error):
        return


async def test_a_command_runs_on_a_copy_of_the_checkout_and_its_changes_come_back(
    tmp_path, monkeypatch
):
    provisioned = []

    async def provision(**kwargs):
        provisioned.append(kwargs)
        return _LocalBox("copy")

    @asynccontextmanager
    async def lease(*, host_id):
        yield

    monkeypatch.setattr(
        workspace_mod, "sandbox_client", SimpleNamespace(provision=provision, lease=lease)
    )
    monkeypatch.setattr(workspace_mod, "get_work_root", lambda user: f"{tmp_path}/{user}")
    monkeypatch.setattr(workspace_mod, "get_repo_root", lambda user: f"{tmp_path}/{user}/repo")
    repo, copy = tmp_path / "agent" / "repo", tmp_path / "copy" / "repo"
    # A clone with one uncommitted change, one new file, one staged file and one file that
    # git ignores.
    script = f"""
        set -e
        git init -q origin && cd origin
        echo node_modules/ > .gitignore && echo one > a.txt && echo keep > b.txt
        git add . && git -c user.name=t -c user.email=t@t.test commit -qm first
        git clone -q . {repo} && cd {repo}
        echo two >> a.txt && echo new > untracked.txt
        echo staged > staged.txt && git add staged.txt
        mkdir node_modules && echo here > node_modules/mine
    """
    subprocess.run(["sh", "-c", script], cwd=tmp_path, check=True)
    workspace = RepoWorkspace(subject=SimpleNamespace(repo="acme/widget", ref="main"))
    workspace.host = _LocalBox("agent")
    staged = subprocess.check_output(["git", "diff", "--cached", "--stat"], cwd=repo)

    first = await workspace.run_on_copy(
        "cat a.txt untracked.txt staged.txt; ls node_modules; echo made > generated.txt;"
        " echo three >> a.txt; mkdir -p node_modules; echo dep > node_modules/installed; exit 3",
        template="template-1",
    )

    # The command saw what is not committed, and not what git ignores.
    assert first.returncode == 3
    assert first.stdout.split()[:4] == ["one", "two", "new", "staged"]
    assert "No such file" in first.stdout
    # What it changed is in the checkout, and the agent's index is as it was.
    assert (repo / "generated.txt").read_text() == "made\n"
    assert (repo / "a.txt").read_text() == "one\ntwo\nthree\n"
    assert subprocess.check_output(["git", "diff", "--cached", "--stat"], cwd=repo) == staged
    assert not (repo / "node_modules" / "installed").exists()

    (repo / "generated.txt").unlink()
    (repo / "b.txt").unlink()
    second = await workspace.run_on_copy("ls; cat node_modules/installed", template="template-1")

    # The copy follows a deletion, and keeps what git ignores from command to command.
    assert second.stdout.split() == ["a.txt", "node_modules", "staged.txt", "untracked.txt", "dep"]
    assert (copy / "node_modules" / "installed").exists()
    # Both commands ran on the one sandbox that the agent's box keeps for the template.
    assert (
        provisioned
        == [
            {
                "idempotency_key": "agent:template-1",
                "template": "template-1",
                "lease_seconds": WORKFLOW_HOST_LEASE_SECONDS,
            }
        ]
        * 2
    )
