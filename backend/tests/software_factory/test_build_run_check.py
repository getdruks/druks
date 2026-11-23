import subprocess
from types import SimpleNamespace

import pytest
from druks.contrib.software_factory.app import SoftwareFactory
from druks.contrib.software_factory.constants import (
    CHECK_LINE_CHARS,
    CHECK_LOG_PATH,
    CHECK_TAIL_LINES,
    CHECK_TIMEOUT_SECONDS,
    MAX_CHECK_RUNS,
)
from druks.contrib.software_factory.workflows import Build
from druks.workflows import FatalError

TESTS = "uv run pytest backend/"


def _build() -> Build:
    flow = Build()
    flow._profile = {"verification": {"test_commands": [{"command": TESTS, "ci_check": None}]}}
    return flow


class _Host:
    """A box that records each command and answers the check with ``returncode``."""

    def __init__(self, *, returncode=0, output="3 passed\n"):
        self.commands = []
        self._returncode = returncode
        self._output = output

    async def exec(self, command, *, cwd, timeout):
        self.commands.append((command, cwd, timeout))
        if command[0] == "sh" and "tail" in command[2]:
            return subprocess.CompletedProcess(command, 0, self._output, "")
        return subprocess.CompletedProcess(command, self._returncode, "", "")


def _workspace(host) -> SimpleNamespace:
    return SimpleNamespace(host=host, repo_path="/home/agent/work/repo")


async def test_the_declared_command_runs_in_the_checkout_and_its_result_goes_back():
    host = _Host(returncode=1, output="1 failed\n")

    reply = await _build()._run_check(TESTS, workspace=_workspace(host))

    (run, cwd, timeout), (tail, tail_cwd, _) = host.commands
    assert run == [
        "timeout",
        "-k",
        "30",
        str(CHECK_TIMEOUT_SECONDS),
        "sh",
        "-c",
        f"({TESTS}\n) > {CHECK_LOG_PATH} 2>&1",
    ]
    assert (cwd, tail_cwd) == ("/home/agent/work/repo",) * 2
    assert timeout > CHECK_TIMEOUT_SECONDS
    assert tail == [
        "sh",
        "-c",
        f"tail -n {CHECK_TAIL_LINES} {CHECK_LOG_PATH} | cut -c1-{CHECK_LINE_CHARS}",
    ]
    assert f"Druks ran `{TESTS}` on your box: exit code 1." in reply
    assert "1 failed" in reply
    assert f"The whole output is in {CHECK_LOG_PATH}." in reply


async def test_a_check_that_outlives_its_limit_is_reported_as_killed():
    reply = await _build()._run_check(TESTS, workspace=_workspace(_Host(returncode=124)))

    assert f"killed after {CHECK_TIMEOUT_SECONDS // 60} minutes" in reply


async def test_a_command_that_is_not_declared_is_never_run():
    host = _Host()

    reply = await _build()._run_check("rm -rf /", workspace=_workspace(host))

    assert host.commands == []
    assert "`rm -rf /` is not one of this repo's declared test commands" in reply
    assert f"- `{TESTS}`" in reply


async def test_a_build_without_a_profile_declares_no_command():
    flow = Build()
    flow._profile = {}
    host = _Host()

    reply = await flow._run_check(TESTS, workspace=_workspace(host))

    assert host.commands == []
    assert "is not one of this repo's declared test commands" in reply


def _asked_for_check() -> SimpleNamespace:
    return SimpleNamespace(status="needs_check", check_command=TESTS)


async def test_an_implementer_that_asks_for_a_check_is_resumed_with_its_result(monkeypatch):
    flow = _build()
    delivered = SimpleNamespace(status="success", check_command=None)
    answers = iter([_asked_for_check(), delivered])
    resumes = []

    async def implement(*, effort=None, resume=None):
        resumes.append(resume)
        return next(answers)

    async def run_check(command, *, workspace):
        return f"ran {command} in {workspace}"

    monkeypatch.setattr(SoftwareFactory, "implement", implement)
    monkeypatch.setattr(flow, "_run_check", run_check)

    assert await flow.implement() is delivered

    first, second = resumes
    assert first is None
    assert await second(workspace="the box") == f"ran {TESTS} in the box"


async def test_an_implementer_that_keeps_asking_for_checks_fails_the_run(monkeypatch):
    flow = _build()
    asked = []

    async def implement(*, effort=None, resume=None):
        asked.append(resume)
        return _asked_for_check()

    monkeypatch.setattr(SoftwareFactory, "implement", implement)

    with pytest.raises(FatalError, match=f"asked for {MAX_CHECK_RUNS + 1} test runs"):
        await flow.implement()

    assert len(asked) == MAX_CHECK_RUNS + 1


@pytest.mark.parametrize(
    ("size", "effort"), [("small", "high"), ("medium", "high"), ("large", None), (None, None)]
)
async def test_the_implement_effort_follows_the_size_of_the_change(monkeypatch, size, effort):
    flow = _build()
    flow.journal = SimpleNamespace(plan_size=size, red_ci_report=None)
    seen = []

    async def implement(*, effort=None, resume=None):
        seen.append(effort)
        return SimpleNamespace(status="success", check_command=None)

    monkeypatch.setattr(SoftwareFactory, "implement", implement)

    await flow.implement()

    assert seen == [effort]
