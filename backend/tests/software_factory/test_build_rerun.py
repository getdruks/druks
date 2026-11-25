import asyncio
from types import SimpleNamespace

from druks.contrib.software_factory.constants import RERUN_POLL_SECONDS, RERUN_START_POLLS
from druks.contrib.software_factory.contracts import CiReport
from druks.contrib.software_factory.journal import BuildJournal
from druks.contrib.software_factory.workflows import Build
from druks.core.apis.exceptions import GitHubRerunRefusedError
from druks.core.services import Github

HEAD = "0f3a2c1d"


def _run(run_id, *, conclusion, attempt=1, status="completed"):
    return {"id": run_id, "conclusion": conclusion, "run_attempt": attempt, "status": status}


class _FakeGithub:
    """A GitHub client that answers list_workflow_runs from a script of pages and
    records each rerun."""

    def __init__(self, pages, *, forbidden=()):
        self._pages = list(pages)
        self.listed = 0
        self.reruns = []
        self._forbidden = set(forbidden)

    async def list_workflow_runs(self, repo, head_sha):
        page = self._pages[min(self.listed, len(self._pages) - 1)]
        self.listed += 1
        return page

    async def rerun_failed_jobs(self, repo, run_id):
        self.reruns.append(run_id)
        if run_id in self._forbidden:
            raise GitHubRerunRefusedError(f"{repo} run {run_id}")


def _rerun_flow(monkeypatch, github):
    flow = Build()
    flow.subject = SimpleNamespace(repo="org/repo")

    async def get_client(repo):
        return github

    monkeypatch.setattr(Github, "get_client", get_client)
    return flow


# _wrap_steps replaces the @step method with a wrapper that needs a running workflow;
# __wrapped__ is the body, which a test drives on its own.
async def _rerun(flow, head_sha):
    return await Build._rerun_failed_jobs.__wrapped__(flow, head_sha)


async def test_a_non_actions_red_has_no_run_to_rerun(monkeypatch):
    # druks-house-rules posts the only red; its check has no Actions run, so the runs
    # on HEAD are green and nothing is rerun.
    github = _FakeGithub([[_run(1, conclusion="success")]])
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is False
    assert github.reruns == []


async def test_a_403_sends_the_red_to_implement(monkeypatch):
    github = _FakeGithub([[_run(1, conclusion="failure")]], forbidden={1})
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is False
    assert github.reruns == [1]


async def test_a_403_after_a_rerun_still_waits_for_the_run_that_was_rerun(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    github = _FakeGithub(
        [
            [_run(1, conclusion="failure"), _run(2, conclusion="failure")],
            [_run(1, conclusion=None, attempt=2, status="queued"), _run(2, conclusion="failure")],
        ],
        forbidden={2},
    )
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.reruns == [1, 2]


async def test_a_run_that_cannot_be_rerun_does_not_stop_the_others(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    github = _FakeGithub(
        [
            [_run(2, conclusion="failure"), _run(1, conclusion="failure")],
            [_run(2, conclusion="failure"), _run(1, conclusion=None, attempt=2, status="queued")],
        ],
        forbidden={2},
    )
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.reruns == [2, 1]


async def test_a_retry_finds_the_run_it_already_reran_and_does_not_rerun_it(monkeypatch):
    github = _FakeGithub([[_run(1, conclusion="success", attempt=2)]])
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.reruns == []


async def test_a_retry_that_finds_the_rerun_active_before_its_attempt_rises_reruns_nothing(
    monkeypatch,
):
    github = _FakeGithub([[_run(1, conclusion=None, attempt=1, status="in_progress")]])
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.reruns == []


async def test_a_run_that_already_has_a_second_attempt_is_not_rerun_again(monkeypatch):
    github = _FakeGithub([[_run(1, conclusion="failure", attempt=2)]])
    flow = _rerun_flow(monkeypatch, github)

    await _rerun(flow, HEAD)

    assert github.reruns == []


async def test_the_rerun_stops_waiting_for_the_new_attempt_after_the_poll_limit(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    github = _FakeGithub([[_run(1, conclusion="failure")]])
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.listed == 1 + RERUN_START_POLLS


async def test_the_rerun_waits_for_the_new_attempt_before_it_returns(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    github = _FakeGithub(
        [
            [_run(1, conclusion="failure", attempt=1)],
            [_run(1, conclusion="failure", attempt=1)],
            [_run(1, conclusion=None, attempt=2, status="queued")],
        ]
    )
    flow = _rerun_flow(monkeypatch, github)

    assert await _rerun(flow, HEAD) is True
    assert github.reruns == [1]
    # It read the runs until the rerun's attempt went up, so the next CI read sees the
    # rerun, not the old red.
    assert github.listed == 3
    assert slept == [RERUN_POLL_SECONDS]


def _phase_flow(monkeypatch, *, ci, rerun, fingerprints=(), decisions=()):
    flow = Build()
    flow.subject = SimpleNamespace(repo="org/repo")
    flow.journal = BuildJournal()
    flow._settings = Build.Settings(plan_gate="human")
    counts = SimpleNamespace(implement=0, rerun=0, wait=0)
    heads = iter(["aaa", "bbb", "ccc", "ddd"])
    verdicts = iter(ci)
    seen_fingerprints = iter(fingerprints)
    review_decisions = iter(decisions)

    async def create_branch():
        pass

    async def implement():
        counts.implement += 1
        return SimpleNamespace(head_sha=next(heads))

    async def wait_for_ci(head_sha):
        counts.wait += 1
        return CiReport(head_sha=head_sha, conclusion=next(verdicts))

    async def rerun_failed_jobs(head_sha):
        counts.rerun += 1
        return rerun

    async def get_failure_fingerprint(head_sha):
        return next(seen_fingerprints, "")

    async def review_work():
        return SimpleNamespace(decision=next(review_decisions, "approve"))

    async def work_gate(**_):
        return True

    async def report_step(step, **status):
        pass

    monkeypatch.setattr(flow, "_create_branch", create_branch)
    monkeypatch.setattr(flow, "implement", implement)
    monkeypatch.setattr(flow, "_report_step", report_step)
    monkeypatch.setattr(flow, "_wait_for_ci", wait_for_ci)
    monkeypatch.setattr(flow, "_rerun_failed_jobs", rerun_failed_jobs)
    monkeypatch.setattr(flow, "_get_failure_fingerprint", get_failure_fingerprint)
    monkeypatch.setattr(flow, "_review_work", review_work)
    monkeypatch.setattr(flow, "_work_gate", work_gate)
    return flow, counts


async def test_a_flaky_red_is_cleared_by_the_rerun_without_a_revision(monkeypatch):
    # Red, the rerun clears it to green, so the round reviews and ends — one implement.
    flow, counts = _phase_flow(monkeypatch, ci=["red", "green"], rerun=True)

    await flow._implement_phase()

    assert counts.implement == 1
    assert counts.rerun == 1


async def test_a_red_that_survives_the_rerun_goes_back_to_implement(monkeypatch):
    # Red, rerun, still red → a second implement call; its head is green and reviews.
    flow, counts = _phase_flow(monkeypatch, ci=["red", "red", "green"], rerun=True)

    await flow._implement_phase()

    assert counts.implement == 2
    assert counts.rerun == 1


async def test_a_new_head_that_fails_as_the_last_did_stops_the_rounds(monkeypatch):
    # Two red heads with one fingerprint: the third implement call never runs.
    flow, counts = _phase_flow(
        monkeypatch, ci=["red", "red", "green"], rerun=False, fingerprints=["same", "same"]
    )

    await flow._implement_phase()

    assert counts.implement == 2


async def test_a_new_head_that_fails_differently_gets_another_round(monkeypatch):
    flow, counts = _phase_flow(
        monkeypatch, ci=["red", "red", "green"], rerun=False, fingerprints=["one", "two"]
    )

    await flow._implement_phase()

    assert counts.implement == 3


async def test_an_empty_fingerprint_never_counts_as_a_repeat(monkeypatch):
    flow, counts = _phase_flow(
        monkeypatch, ci=["red", "red", "green"], rerun=False, fingerprints=["", ""]
    )

    await flow._implement_phase()

    assert counts.implement == 3


async def test_a_repeat_also_ends_the_revisions_that_the_review_asks_for(monkeypatch):
    flow, counts = _phase_flow(
        monkeypatch,
        ci=["red", "red", "green"],
        rerun=False,
        fingerprints=["same", "same"],
        decisions=["request_changes"],
    )

    await flow._implement_phase()

    assert counts.implement == 2


class _LogGithub:
    def __init__(self, logs):
        self.logs = logs
        self.asked_for = None

    async def get_failed_job_logs(self, repo, head_sha, *, lines):
        self.asked_for = lines
        return self.logs


async def _fingerprint(flow, head_sha="aaa"):
    return await Build._get_failure_fingerprint.__wrapped__(flow, head_sha)


async def test_the_fingerprint_ignores_timestamps_and_durations_and_not_the_failure(monkeypatch):
    first = {
        "backend": "2026-11-25T12:51:45.5Z FAILED test_a\n2026-11-25T12:51:46.0Z 1 failed in 3.2s"
    }
    again = {
        "backend": "2026-11-25T13:20:01.9Z FAILED test_a\n2026-11-25T13:20:03.1Z 1 failed in 7.9s"
    }
    other = {
        "backend": "2026-11-25T13:20:01.9Z FAILED test_b\n2026-11-25T13:20:03.1Z 1 failed in 7.9s"
    }

    prints = []
    for logs in (first, again, other):
        flow = _rerun_flow(monkeypatch, _LogGithub(logs))
        prints.append(await _fingerprint(flow))

    assert prints[0] == prints[1]
    assert prints[0] != prints[2]


async def test_the_fingerprint_ignores_the_duration_in_parentheses_of_a_test_line(monkeypatch):
    def log(name, took):
        return {"go": f"2026-11-25T12:51:45.5Z --- FAIL: {name} ({took})\nfailure lines: 1"}

    prints = []
    for logs in (
        log("TestA", "0.00s"),
        log("TestA", "0.04s"),
        log("TestA", "12 ms"),
        log("TestB", "0.00s"),
    ):
        prints.append(await _fingerprint(_rerun_flow(monkeypatch, _LogGithub(logs))))

    assert prints[0] == prints[1] == prints[2]
    assert prints[0] != prints[3]


async def test_the_fingerprint_changes_when_the_list_of_failures_gets_shorter(monkeypatch):
    tail = "2026-11-25T12:51:45.5Z backend/app.py:300:1: F401 unused import 300"
    longer = {"lint": f"failure lines: 300\n{tail}"}
    shorter = {"lint": f"failure lines: 250\n{tail}"}

    first = await _fingerprint(_rerun_flow(monkeypatch, _LogGithub(longer)))
    second = await _fingerprint(_rerun_flow(monkeypatch, _LogGithub(shorter)))

    assert first != second


async def test_the_fingerprint_reads_the_last_200_failure_lines_of_a_job(monkeypatch):
    github = _LogGithub({"lint": "failure lines: 1\n2026-11-25T12:51:45.5Z error TS2322"})

    await _fingerprint(_rerun_flow(monkeypatch, github))

    assert github.asked_for == 200


async def test_no_failed_job_log_gives_an_empty_fingerprint(monkeypatch):
    flow = _rerun_flow(monkeypatch, _LogGithub({}))

    assert await _fingerprint(flow) == ""
