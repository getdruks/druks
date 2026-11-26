import asyncio
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import connect_service
from drukbox_sdk import Issuer, SandboxRun
from drukbox_sdk.exceptions import (
    SandboxCapacityError,
    SandboxProvisioningError,
    SandboxUnavailableError,
    SandboxValidationError,
)
from druks.accounts.models import Account
from druks.core.routes import run_check
from druks.db import db_session
from druks.durable.models import AgentCall
from druks.redis import (
    clear_resumable,
    is_resumable,
    request_check,
    set_resumable,
    take_check_request,
)
from druks.sandbox.exceptions import ExecFailed, HostGone
from druks.sandbox.layout import get_repo_root
from druks.sandbox.models import SandboxIdentity
from druks.testing import configure_app_for_test, make_settings, seed_call, seed_run
from druks.workspaces import (
    CHECK_KILL_GRACE_SECONDS,
    CHECK_OUTPUT_CHARS,
    COMMAND_NOT_FOUND_HINT,
    START_SCRIPT_FAILED,
    START_SCRIPT_TIMEOUT_SECONDS,
    RepoWorkspace,
    _run_after_start,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

URL = "/api/core/checks/run"
CHECK = "make test"


@pytest.fixture
def client(tmp_path: Path, druks_db):
    with TestClient(configure_app_for_test(settings=make_settings(tmp_path))) as client:
        yield client


@pytest.fixture
async def account(druks_db):
    # configure_app_for_test sends each request as this account.
    return await Account.get_or_create(druks_db, "op@example.com")


class _Repo(RepoWorkspace):
    @classmethod
    async def get_checks(cls, subject):
        # A real app reads the subject, so a deleted one cannot declare anything.
        return (CHECK, "make lint") if subject else ()

    @classmethod
    async def get_check_box(cls, subject):
        return {"template": "template-1"}

    @classmethod
    async def get_secret_refs(cls, subject):
        return []


def _workflow(workspace_class):
    return SimpleNamespace(
        subject=SimpleNamespace(
            get_or_none=AsyncMock(return_value=SimpleNamespace(repo="acme/app"))
        ),
        workspace_class=workspace_class,
    )


@pytest.fixture
def box(monkeypatch):
    """The agent's box, the sandbox client and the repo workspace, each a recorder."""
    attached = []
    host_exec = AsyncMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))

    @asynccontextmanager
    async def attach(*, host_id):
        attached.append(host_id)
        yield SimpleNamespace(id=host_id, exec=host_exec, ssh_username="exedev")

    run_command = AsyncMock(
        return_value=SandboxRun(exit_code=0, timed_out=False, output="ok\n", duration_seconds=2.5)
    )
    get_diff = AsyncMock(return_value="the patch")
    monkeypatch.setattr("druks.core.routes.sandbox_client.attach", attach)
    monkeypatch.setattr("druks.core.routes.sandbox_client.run_command", run_command)
    monkeypatch.setattr(RepoWorkspace, "get_diff", get_diff)
    monkeypatch.setattr(RepoWorkspace, "get_base_commit", AsyncMock(return_value="abc123"))
    monkeypatch.setattr("druks.core.routes.get_workflow", lambda kind: _workflow(_Repo))
    return SimpleNamespace(
        attached=attached, run_command=run_command, get_diff=get_diff, host_exec=host_exec
    )


async def _run(session, account, *, call_status="running", subject=True):
    run = await seed_run(
        session,
        kind="anything",
        state="running",
        account_id=account.id,
        subject=SimpleNamespace(identity={"type": "repo", "id": "1"}, key="acme/app")
        if subject
        else None,
    )
    await seed_call(session, run, "implement", status=call_status)
    await session.commit()
    return run


def _ask(client, run, command=CHECK, **extra):
    return client.post(URL, json={"run_id": run.id, "command": command, **extra})


async def test_a_declared_check_runs_on_a_box_with_the_agents_work(client, account, druks_db, box):
    run = await _run(druks_db, account)

    response = _ask(client, run, timeout_seconds=900)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "exit_code": 0,
        "timed_out": False,
        "output": "ok\n",
        "duration_seconds": 2.5,
    }
    assert box.attached == [f"test-host-{run.id}"]
    box.get_diff.assert_awaited_once_with(binary=True)
    box.run_command.assert_awaited_once_with(
        repo_url="https://github.com/acme/app",
        commit="abc123",
        command=CHECK,
        patch="the patch",
        timeout_seconds=900,
        secrets={},
        template="template-1",
    )


async def test_a_check_runs_on_the_box_that_the_workspace_describes(
    client, account, druks_db, box, monkeypatch
):
    async def get_check_box(cls, subject):
        return {"template": "template-1", "env": {"DATABASE_URL": "postgres://localhost/test"}}

    monkeypatch.setattr(_Repo, "get_check_box", classmethod(get_check_box))
    run = await _run(druks_db, account)

    assert _ask(client, run).status_code == 200

    kwargs = box.run_command.await_args.kwargs
    assert kwargs["template"] == "template-1"
    assert kwargs["env"] == {"DATABASE_URL": "postgres://localhost/test"}


async def _no_template(monkeypatch, *, env=None):
    async def get_check_box(cls, subject):
        return {"env": env} if env else {}

    monkeypatch.setattr(_Repo, "get_check_box", classmethod(get_check_box))


def _done(returncode, stdout=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


async def test_a_workspace_with_no_template_runs_the_check_in_the_agents_own_checkout(
    client, account, druks_db, box, monkeypatch
):
    await _no_template(monkeypatch)
    box.host_exec.return_value = _done(3, "start\nFAILED test_x\n")
    run = await _run(druks_db, account)

    body = _ask(client, run, timeout_seconds=900).json()

    box.run_command.assert_not_awaited()
    box.get_diff.assert_not_awaited()
    box.host_exec.assert_awaited_once_with(
        ["timeout", "-k", "30", "900", "sh", "-c", f"exec 2>&1; {CHECK}"],
        cwd=get_repo_root("exedev"),
        timeout=900 + 2 * CHECK_KILL_GRACE_SECONDS,
    )
    assert (body["exit_code"], body["timed_out"], body["output"]) == (
        3,
        False,
        "start\nFAILED test_x\n",
    )


async def test_a_check_that_the_remote_timeout_stopped_reports_it(
    client, account, druks_db, box, monkeypatch
):
    await _no_template(monkeypatch)
    box.host_exec.return_value = _done(124, "partial\n")
    run = await _run(druks_db, account)

    body = _ask(client, run, timeout_seconds=900).json()

    assert (body["exit_code"], body["timed_out"], body["output"]) == (None, True, "partial\n")


async def test_a_check_that_the_remote_timeout_had_to_kill_reports_it(
    client, account, druks_db, box, monkeypatch
):
    await _no_template(monkeypatch)

    async def killed_after_the_full_time(*args, **kwargs):
        await asyncio.sleep(1.1)
        return _done(137, "partial\n")

    box.host_exec.side_effect = killed_after_the_full_time
    run = await _run(druks_db, account)

    body = _ask(client, run, timeout_seconds=1).json()

    assert (body["exit_code"], body["timed_out"]) == (None, True)


async def test_a_command_killed_early_is_not_a_timeout(client, account, druks_db, box, monkeypatch):
    await _no_template(monkeypatch)
    box.host_exec.return_value = _done(137, "Killed\n")
    run = await _run(druks_db, account)

    body = _ask(client, run, timeout_seconds=900).json()

    assert (body["exit_code"], body["timed_out"]) == (137, False)


async def test_a_check_that_outlives_the_ssh_backstop_reports_a_timeout(
    client, account, druks_db, box, monkeypatch
):
    await _no_template(monkeypatch)
    box.host_exec.side_effect = TimeoutError
    run = await _run(druks_db, account)

    body = _ask(client, run, timeout_seconds=60).json()

    assert (body["exit_code"], body["timed_out"]) == (None, True)


@pytest.mark.parametrize(
    "error", [OSError("connection reset"), ExecFailed("no shell", exit_code=1)]
)
async def test_an_agent_box_that_cannot_run_the_check_is_unavailable_and_not_a_crash(
    client, account, druks_db, box, monkeypatch, error
):
    await _no_template(monkeypatch)
    box.host_exec.side_effect = error
    run = await _run(druks_db, account)

    response = _ask(client, run)

    assert response.status_code == 503
    assert response.json()["code"] == "CHECK_BOX_UNAVAILABLE"


async def test_a_check_in_the_agents_own_checkout_keeps_only_the_end_of_its_output(
    client, account, druks_db, box, monkeypatch
):
    await _no_template(monkeypatch)
    box.host_exec.return_value = _done(0, "x" * CHECK_OUTPUT_CHARS + "tail")
    run = await _run(druks_db, account)

    body = _ask(client, run).json()

    assert body["output"] == "x" * (CHECK_OUTPUT_CHARS - 4) + "tail"


async def test_the_hint_for_a_missing_tool_is_for_the_check_box_only(
    client, account, druks_db, box, monkeypatch
):
    await _no_template(monkeypatch)
    box.host_exec.return_value = _done(127, "x: not found\n")
    run = await _run(druks_db, account)

    own_checkout = _ask(client, run).json()["output"]

    assert COMMAND_NOT_FOUND_HINT not in own_checkout


async def test_an_env_that_has_no_template_to_ride_on_is_said_not_to_be_set(
    client, account, druks_db, box, monkeypatch, caplog
):
    await _no_template(monkeypatch, env={"DATABASE_URL": "postgres://x/y"})
    run = await _run(druks_db, account)

    with caplog.at_level("WARNING", logger="druks.workspaces"):
        assert _ask(client, run).status_code == 200

    assert "env and the start script of the check box of acme/app are not used" in caplog.text


async def test_a_check_on_a_template_runs_after_the_start_script_that_the_box_names(
    client, account, druks_db, box, monkeypatch
):
    async def get_check_box(cls, subject):
        return {"template": "template-1", "start": "redis-server --daemonize yes\n"}

    monkeypatch.setattr(_Repo, "get_check_box", classmethod(get_check_box))
    run = await _run(druks_db, account)

    assert _ask(client, run).status_code == 200

    kwargs = box.run_command.await_args.kwargs
    # The default time of a check is 600 seconds: half of it is the limit of a start.
    assert kwargs["command"] == _run_after_start(
        CHECK, "redis-server --daemonize yes\n", START_SCRIPT_TIMEOUT_SECONDS
    )
    assert kwargs["template"] == "template-1"
    assert "start" not in kwargs


async def test_a_start_script_gets_half_of_the_time_of_the_check_and_no_more_than_its_limit(
    client, account, druks_db, box, monkeypatch
):
    async def get_check_box(cls, subject):
        return {"template": "template-1", "start": "true\n"}

    monkeypatch.setattr(_Repo, "get_check_box", classmethod(get_check_box))
    run = await _run(druks_db, account)

    for seconds in (60, 400, 3600):
        assert _ask(client, run, timeout_seconds=seconds).status_code == 200

    commands = [call.kwargs["command"] for call in box.run_command.await_args_list]
    assert commands == [
        _run_after_start(CHECK, "true\n", 30),
        _run_after_start(CHECK, "true\n", 200),
        _run_after_start(CHECK, "true\n", START_SCRIPT_TIMEOUT_SECONDS),
    ]


async def test_the_hint_for_a_missing_tool_is_not_sent_when_the_start_script_failed(
    client, account, druks_db, box
):
    run = await _run(druks_db, account)
    box.run_command.return_value = SandboxRun(
        exit_code=127,
        timed_out=False,
        output=f"sh: 1: redis-server: not found\n{START_SCRIPT_FAILED} 127\n",
        duration_seconds=1.0,
    )

    output = _ask(client, run).json()["output"]

    assert START_SCRIPT_FAILED in output
    assert COMMAND_NOT_FOUND_HINT not in output


async def test_a_start_script_that_has_no_template_to_run_on_is_said_not_to_be_used(
    client, account, druks_db, box, monkeypatch, caplog
):
    async def get_check_box(cls, subject):
        return {"start": "redis-server --daemonize yes\n"}

    monkeypatch.setattr(_Repo, "get_check_box", classmethod(get_check_box))
    run = await _run(druks_db, account)

    with caplog.at_level("WARNING", logger="druks.workspaces"):
        assert _ask(client, run).status_code == 200

    box.run_command.assert_not_awaited()
    assert "start script of the check box of acme/app are not used" in caplog.text


async def test_a_failing_check_answers_with_its_exit_code(client, account, druks_db, box):
    box.run_command.return_value = SandboxRun(
        exit_code=None, timed_out=True, output="start\n", duration_seconds=900.0
    )
    run = await _run(druks_db, account)

    body = _ask(client, run).json()

    assert (body["exit_code"], body["timed_out"], body["output"]) == (None, True, "start\n")


async def test_a_command_the_box_does_not_have_says_the_box_is_the_cause(
    client, account, druks_db, box
):
    box.run_command.return_value = SandboxRun(
        exit_code=127, timed_out=False, output="sh: uv: not found\n", duration_seconds=0.4
    )
    run = await _run(druks_db, account)

    body = _ask(client, run).json()

    assert body["exit_code"] == 127
    assert body["output"] == f"sh: uv: not found\n\n\n{COMMAND_NOT_FOUND_HINT}"


async def test_a_command_that_is_not_declared_is_refused_with_the_declared_ones(
    client, account, druks_db, box
):
    run = await _run(druks_db, account)

    response = _ask(client, run, command="rm -rf /")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "CHECK_NOT_DECLARED"
    assert "'make test'" in body["message"] and "'make lint'" in body["message"]
    box.run_command.assert_not_awaited()


async def test_a_run_that_declares_no_checks_says_so(client, account, druks_db, box, monkeypatch):
    class _Nothing(_Repo):
        @classmethod
        async def get_checks(cls, subject):
            return ()

    monkeypatch.setattr("druks.core.routes.get_workflow", lambda kind: _workflow(_Nothing))
    run = await _run(druks_db, account)

    response = _ask(client, run)

    assert (response.status_code, response.json()["code"]) == (422, "CHECK_NOT_DECLARED")
    assert "declares no checks" in response.json()["message"]
    box.run_command.assert_not_awaited()


async def test_an_unknown_run_and_another_accounts_run_are_the_same_404(
    client, account, druks_db, box
):
    other = await Account.get_or_create(druks_db, "someone@example.com")
    theirs = await _run(druks_db, other)

    foreign = _ask(client, theirs)
    unknown = client.post(URL, json={"run_id": "nope", "command": CHECK})

    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert foreign.json()["code"] == unknown.json()["code"] == "RUN_NOT_FOUND"
    box.run_command.assert_not_awaited()


async def test_a_run_with_no_agent_running_is_refused(client, account, druks_db, box):
    run = await _run(druks_db, account, call_status="succeeded")

    response = _ask(client, run)

    assert (response.status_code, response.json()["code"]) == (409, "RUN_NOT_RUNNING")
    box.run_command.assert_not_awaited()


async def test_a_repo_run_with_no_subject_is_refused_not_crashed(client, account, druks_db, box):
    run = await _run(druks_db, account, subject=False)

    response = _ask(client, run)

    assert (response.status_code, response.json()["code"]) == (409, "RUN_HAS_NO_REPO")
    box.run_command.assert_not_awaited()


async def test_a_box_that_is_gone_is_refused(client, account, druks_db, box, monkeypatch):
    @asynccontextmanager
    async def gone(*, host_id):
        raise HostGone(host_id)
        yield

    monkeypatch.setattr("druks.core.routes.sandbox_client.attach", gone)
    run = await _run(druks_db, account)

    response = _ask(client, run)

    assert (response.status_code, response.json()["code"]) == (409, "BOX_GONE")
    box.run_command.assert_not_awaited()


async def test_a_refusal_of_the_check_box_carries_its_reason(client, account, druks_db, box):
    box.run_command.side_effect = SandboxProvisioningError("the clone failed: exit=128")
    run = await _run(druks_db, account)

    response = _ask(client, run)

    assert (response.status_code, response.json()["code"]) == (502, "CHECK_BOX_REFUSED")
    assert response.json()["retryable"] is False
    assert "the clone failed: exit=128" in response.json()["message"]


@pytest.mark.parametrize("fails", [False, True])
async def test_the_identity_of_the_check_box_is_revoked_either_way(
    client, account, druks_db, box, fails
):
    if fails:
        box.run_command.side_effect = SandboxProvisioningError("down")
    run = await _run(druks_db, account)

    _ask(client, run)

    rows = list(
        await db_session().scalars(select(SandboxIdentity).where(SandboxIdentity.run_id == run.id))
    )
    assert [row.scoped_to for row in rows] == ["check"]
    assert rows[0].revoked_at


async def test_a_run_is_checked_on_its_newest_running_call_without_naming_the_agent(
    account, druks_db
):
    run = await seed_run(druks_db, kind="anything", state="running", account_id=account.id)
    await seed_call(druks_db, run, "plan", status="succeeded")
    newest = await seed_call(druks_db, run, "implement", status="running")
    await druks_db.commit()

    found = await AgentCall.lookup_running(druks_db, run.id)

    assert found and found.id == newest.id
    assert await AgentCall.lookup_running(druks_db, run.id, "plan") is None


async def test_a_validation_error_of_the_check_box_is_not_a_retryable_refusal(
    client, account, druks_db, box
):
    box.run_command.side_effect = SandboxValidationError("the provider stops its VMs after 0:45:00")
    run = await _run(druks_db, account)

    response = _ask(client, run, timeout_seconds=3600)

    assert response.status_code == 422
    body = response.json()
    assert (body["code"], body["retryable"]) == ("CHECK_REJECTED", False)
    assert "stops its VMs" in body["message"]


async def test_the_identity_of_the_check_box_is_revoked_when_the_call_is_cancelled_twice(
    account, druks_db, box
):
    """A cancel is already delivered when the finally runs, so one cancel proves nothing. A
    second one reaches the revoke itself: only a shield, on a session of its own, finishes it."""
    started = asyncio.Event()

    async def slow(**kwargs):
        started.set()
        await asyncio.sleep(30)

    box.run_command.side_effect = slow
    run = await _run(druks_db, account)
    call = asyncio.create_task(
        run_check(
            session=druks_db, account=account, run_id=run.id, command=CHECK, timeout_seconds=600
        )
    )
    await started.wait()
    call.cancel()
    await asyncio.sleep(0)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    await asyncio.sleep(0.3)

    [row] = list(
        await db_session().scalars(
            select(SandboxIdentity)
            .where(SandboxIdentity.run_id == run.id)
            .execution_options(populate_existing=True)
        )
    )
    assert row.revoked_at


async def test_a_check_that_ran_keeps_its_result_when_the_identity_cannot_be_revoked(
    client, account, druks_db, box, monkeypatch
):
    def broken():
        raise ConnectionError("the database went away")

    monkeypatch.setattr("druks.workspaces.session_scope", broken)
    run = await _run(druks_db, account)

    response = _ask(client, run)

    assert response.status_code == 200
    assert response.json()["output"] == "ok\n"


async def test_the_check_box_gets_the_github_secret_of_the_repo_through_its_own_identity(
    client, account, druks_db, box, monkeypatch
):
    """The route's whole chain, with the workspace's real secret refs: the issuer entry that
    reaches drukbox is the one that the identity of the check issues."""
    await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-operator"}, secrets={"private_key": "pem"}
    )

    class _Github(RepoWorkspace):
        @classmethod
        async def get_checks(cls, subject):
            return (CHECK,)

        @classmethod
        async def get_check_box(cls, subject):
            return {"template": "template-1"}

    monkeypatch.setattr("druks.core.routes.get_workflow", lambda kind: _workflow(_Github))
    run = await _run(druks_db, account)

    assert _ask(client, run).status_code == 200

    [identity] = list(
        await db_session().scalars(select(SandboxIdentity).where(SandboxIdentity.run_id == run.id))
    )
    [ref] = identity.secret_refs
    assert (ref.name, tuple(ref.resources), ref.host) == ("github", ("acme/app",), "github.com")
    [entries] = [call.kwargs["secrets"] for call in box.run_command.await_args_list]
    assert list(entries) == ["github"]
    issuer = entries["github"]
    assert isinstance(issuer, Issuer)
    assert issuer.url.endswith(f"/api/secrets/{identity.id}/github")
    assert issuer.headers["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize(
    "error",
    [SandboxUnavailableError("Sandbox service transport failed"), SandboxCapacityError("full", 30)],
)
async def test_a_drukbox_that_is_down_or_full_is_a_retryable_503_not_a_crash(
    client, account, druks_db, box, error
):
    box.run_command.side_effect = error
    run = await _run(druks_db, account)

    response = _ask(client, run)

    assert response.status_code == 503
    body = response.json()
    assert (body["code"], body["retryable"]) == ("CHECK_BOX_UNAVAILABLE", True)
    assert str(error) in body["message"]


@pytest.fixture
def resumable_call(monkeypatch):
    """A run whose agent call can end its turn for a check. The route runs on the test
    client's own event loop, which the shared Redis client cannot cross, so the two Redis
    calls are in memory here and the Redis keys have a test of their own."""
    requests = {}

    async def request_check(run_id, *, command, timeout_seconds):
        return requests.setdefault(run_id, (command, timeout_seconds)) == (command, timeout_seconds)

    monkeypatch.setattr("druks.core.routes.is_resumable", AsyncMock(return_value=True))
    monkeypatch.setattr("druks.core.routes.request_check", request_check)
    return requests


async def test_a_check_asked_in_a_resumable_call_starts_when_the_turn_ends(
    client, account, druks_db, box, resumable_call
):
    run = await _run(druks_db, account)

    response = _ask(client, run, timeout_seconds=900)

    assert response.status_code == 200, response.text
    assert response.json()["started"] is True
    assert resumable_call == {run.id: (CHECK, 900)}
    box.run_command.assert_not_awaited()
    assert box.attached == []


async def test_a_second_check_before_the_turn_ends_is_refused(
    client, account, druks_db, box, resumable_call
):
    run = await _run(druks_db, account)
    assert _ask(client, run).status_code == 200

    response = _ask(client, run, command="make lint")

    assert response.status_code == 409
    assert response.json()["code"] == "CHECK_ALREADY_STARTED"
    assert resumable_call == {run.id: (CHECK, 600)}


async def test_the_check_request_of_a_run_is_taken_once(druks_redis):
    await set_resumable("run-1", ttl_seconds=60)

    assert await is_resumable("run-1")
    assert await request_check("run-1", command=CHECK, timeout_seconds=30)
    assert not await request_check("run-1", command="make lint", timeout_seconds=30)
    assert await take_check_request("run-1") == {"command": CHECK, "timeout_seconds": 30}
    assert await take_check_request("run-1") is None
    await clear_resumable("run-1")
    assert not await is_resumable("run-1")


async def test_a_request_that_a_crashed_attempt_left_is_not_the_next_calls(druks_redis):
    await set_resumable("run-1", ttl_seconds=60)
    assert await request_check("run-1", command=CHECK, timeout_seconds=30)

    # The attempt crashed before it took the request. The replay marks its call again.
    await set_resumable("run-1", ttl_seconds=60)

    assert await take_check_request("run-1") is None
    assert await request_check("run-1", command="make lint", timeout_seconds=30)
    await clear_resumable("run-1")
    assert await take_check_request("run-1") is None
