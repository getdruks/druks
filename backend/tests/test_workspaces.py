import asyncio
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock

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
from druks.sandbox.exceptions import ExecFailed
from druks.sandbox.layout import get_repo_root
from druks.secrets.models import VaultSecret
from druks.workspaces import (
    START_FAILED_OUTPUT_CHARS,
    START_OUTPUT_CHARS,
    START_OUTPUT_LINES,
    START_SCRIPT_FAILED,
    START_SCRIPT_TIMED_OUT,
    RepoWorkspace,
    Workspace,
    _run_after_start,
)

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


async def test_a_workspace_declares_no_checks_until_an_app_says_so():
    assert await Workspace.get_checks(None) == ()
    assert await RepoWorkspace.get_checks(SimpleNamespace(repo="acme/widgets")) == ()


def test_a_repo_workspace_clones_from_the_repo_of_its_subject():
    workspace = RepoWorkspace(SimpleNamespace(repo="acme/widgets", ref=None))

    assert workspace.repo_url == "https://github.com/acme/widgets"


async def test_repo_workspace_base_commit_is_where_the_checkout_left_the_default_branch(
    tmp_path: Path,
):
    def git(*args: str, cwd: Path = tmp_path) -> str:
        identity = ("-c", "user.name=dev", "-c", "user.email=dev@example.com")
        return subprocess.run(
            ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main", "origin")
    (tmp_path / "origin" / "kept.txt").write_text("kept\n")
    git("add", "kept.txt", cwd=tmp_path / "origin")
    git("commit", "-q", "-m", "start", cwd=tmp_path / "origin")
    git("clone", "-q", "origin", "checkout")
    checkout = tmp_path / "checkout"
    start = git("rev-parse", "HEAD", cwd=checkout)
    git("checkout", "-q", "-b", "feature", cwd=checkout)
    (checkout / "committed.txt").write_text("committed\n")
    git("add", "committed.txt", cwd=checkout)
    git("commit", "-q", "-m", "change", cwd=checkout)

    class _CheckoutHost:
        ssh_username = "exedev"

        async def exec(
            self, command: list[str], *, cwd: str = "", timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(command, cwd=checkout, capture_output=True, text=True)

    workspace = RepoWorkspace(None)
    workspace.host = _CheckoutHost()  # type: ignore[assignment]

    assert await workspace.get_base_commit() == start
    assert git("rev-parse", "HEAD", cwd=checkout) != start


async def test_repo_workspace_base_commit_fails_loudly_outside_a_checkout(tmp_path: Path):
    class _NoCheckout:
        ssh_username = "exedev"

        async def exec(
            self, command: list[str], *, cwd: str = "", timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)

    workspace = RepoWorkspace(None)
    workspace.host = _NoCheckout()  # type: ignore[assignment]

    with pytest.raises(ExecFailed, match="failed to read the workspace base commit"):
        await workspace.get_base_commit()


async def test_a_check_in_the_agents_checkout_keeps_its_uncommitted_and_unpushed_work(
    tmp_path: Path, monkeypatch
):
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
    git("commit", "-q", "-m", "unpushed change", cwd=checkout)
    (checkout / "kept.txt").write_text("edited\n")
    (checkout / "untracked.txt").write_text("untracked\n")

    class _CheckoutHost:
        ssh_username = "exedev"

        async def exec(
            self, command: list[str], *, cwd: str = "", timeout: float = 30.0
        ) -> subprocess.CompletedProcess[str]:
            assert cwd == get_repo_root(self.ssh_username)
            return subprocess.run(command, cwd=checkout, capture_output=True, text=True)

    # A clone of a checkout that exists resets its branch to the remote's, which would drop
    # the work above: the check must not clone.
    monkeypatch.setattr(
        RepoWorkspace, "clone", AsyncMock(side_effect=AssertionError("a check must not clone"))
    )
    workspace = RepoWorkspace(SimpleNamespace(repo="acme/app", ref="feature"))
    workspace.host = _CheckoutHost()  # type: ignore[assignment]

    result = await workspace._run_check_here("git status --short; git log -1 --format=%s", 60)

    assert result.exit_code == 0
    assert "?? untracked.txt" in result.output
    assert " M kept.txt" in result.output
    assert "unpushed change" in result.output
    assert (checkout / "untracked.txt").read_text() == "untracked\n"


def _shell(command: str) -> subprocess.CompletedProcess[str]:
    # What the check box does with the string: its shell runs it.
    return subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL
    )


def test_a_check_runs_after_the_start_script_and_shows_its_output():
    done = _shell(_run_after_start("echo CHECK-RAN", "echo START-RAN\n", 30))

    assert done.returncode == 0
    assert done.stdout.splitlines() == ["START-RAN", "CHECK-RAN"]


def test_a_start_script_that_fails_ends_the_check_with_its_own_code_and_says_so():
    done = _shell(_run_after_start("echo CHECK-RAN", "echo boom\nexit 3\n", 30))

    assert done.returncode == 3
    assert "boom" in done.stdout
    assert f"{START_SCRIPT_FAILED} 3" in done.stderr
    assert "CHECK-RAN" not in done.stdout


def test_a_service_that_the_start_script_leaves_running_does_not_hold_the_check():
    started = time.monotonic()
    # The service keeps the output that it inherited. Were that the channel of the check,
    # the check would wait for the service to exit.
    done = _shell(_run_after_start("echo CHECK-RAN", "sleep 6 &\n", 30))

    assert done.stdout.strip() == "CHECK-RAN"
    assert time.monotonic() - started < 5


def test_a_check_with_quotes_and_several_lines_runs_as_written_after_the_start_script():
    command = "cd /tmp\necho 'a  b' \"c\"\npwd"

    done = _shell(_run_after_start(command, "true\n", 30))

    assert done.stdout.splitlines() == ["a  b c", "/tmp"] or done.stdout.splitlines() == [
        "a  b c",
        subprocess.run(["pwd", "-P"], cwd="/tmp", capture_output=True, text=True).stdout.strip(),
    ]


def test_a_start_script_that_succeeds_shows_only_the_end_of_its_output():
    done = _shell(_run_after_start("echo CHECK-RAN", "seq 1 100\n", 30))

    lines = done.stdout.splitlines()
    assert lines == [str(n) for n in range(101 - START_OUTPUT_LINES, 101)] + ["CHECK-RAN"]


def test_a_start_script_that_fails_shows_all_of_its_output():
    done = _shell(_run_after_start("echo CHECK-RAN", "seq 1 100\nexit 2\n", 30))

    assert done.returncode == 2
    assert done.stdout.splitlines() == [str(n) for n in range(1, 101)]


def test_a_start_script_that_never_ends_is_stopped_on_its_own_time_and_says_so():
    started = time.monotonic()

    done = _shell(_run_after_start("echo CHECK-RAN", "echo waiting\nsleep 30\n", 1))

    assert done.returncode == 124
    assert "waiting" in done.stdout
    assert f"{START_SCRIPT_TIMED_OUT} 1 seconds" in done.stderr
    assert "CHECK-RAN" not in done.stdout
    assert time.monotonic() - started < 15


def test_a_start_script_that_prints_one_very_long_line_cannot_crowd_out_the_check():
    done = _shell(_run_after_start("echo CHECK-RAN", "head -c 50000 /dev/zero | tr '\\0' x\n", 30))

    assert done.stdout.splitlines()[-1] == "CHECK-RAN"
    assert len(done.stdout) <= START_OUTPUT_CHARS + len("CHECK-RAN\n") + 1


def test_a_start_script_that_fails_with_a_very_long_line_shows_only_its_end():
    done = _shell(
        _run_after_start("echo CHECK-RAN", "head -c 50000 /dev/zero | tr '\\0' x\nexit 2\n", 30)
    )

    assert done.returncode == 2
    # The end of the output, and the newline that keeps the line of the failure apart.
    assert len(done.stdout) <= START_FAILED_OUTPUT_CHARS + 1


def test_a_start_script_that_exits_124_by_itself_is_a_failure_and_not_a_timeout():
    done = _shell(_run_after_start("echo CHECK-RAN", "exit 124\n", 30))

    assert done.returncode == 124
    assert f"{START_SCRIPT_FAILED} 124" in done.stderr
    assert START_SCRIPT_TIMED_OUT not in done.stderr
