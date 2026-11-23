import json
import os
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import druks.core.apis.github as github_api
import pytest
from conftest import connect_service
from druks.core.apis.exceptions import (
    GitHubAppNotActiveError,
    GitHubAppNotInstalledError,
    GitHubMergeConflictError,
    GitHubStackError,
)
from druks.core.apis.github import GitHubAppClient, GitHubClient
from druks.core.services import Github
from druks.services import Connection, ServiceNotConnectedError
from githubkit.exception import GraphQLFailed, RequestFailed, RequestTimeout
from githubkit.retry import RETRY_SERVER_ERROR


class _Parsed:
    def model_dump(self) -> dict[str, str]:
        return {"id": "review-1"}


class _Response:
    parsed_data = _Parsed()


class _Pulls:
    def __init__(self) -> None:
        self.create_review_kwargs: dict[str, Any] | None = None

    async def async_create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        **kwargs: Any,
    ) -> _Response:
        self.create_review_kwargs = {
            "owner": owner,
            "repo": repo,
            "pr_number": pr_number,
            **kwargs,
        }
        return _Response()


class _Rest:
    def __init__(self) -> None:
        self.pulls = _Pulls()


class _GitHub:
    def __init__(self) -> None:
        self.rest = _Rest()


class _TestGitHubClient(GitHubClient):
    def __init__(self, github: _GitHub) -> None:
        self._github = github


async def test_create_review_omits_empty_comments() -> None:
    github = _GitHub()
    client = _TestGitHubClient(github)

    await client.create_review(
        "ClawHaven/example",
        7,
        event="APPROVE",
        body="Approved.",
    )

    assert github.rest.pulls.create_review_kwargs == {
        "owner": "ClawHaven",
        "repo": "example",
        "pr_number": 7,
        "event": "APPROVE",
        "body": "Approved.",
    }


async def test_enable_auto_merge_uses_squash_mutation() -> None:
    github = SimpleNamespace(async_graphql=AsyncMock())
    client = _TestGitHubClient(github)

    assert await client.enable_auto_merge("ClawHaven/example", "PR_node")

    query, variables = github.async_graphql.await_args.args
    assert "enablePullRequestAutoMerge" in query
    assert "mergeMethod: SQUASH" in query
    assert variables == {"id": "PR_node"}


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, object]:
        return self._body


def _make_request_failed(status_code: int, body: dict[str, object] | None = None) -> RequestFailed:
    exc = RequestFailed.__new__(RequestFailed)
    exc.response = _FakeResponse(status_code, body)  # type: ignore[assignment]
    return exc


async def test_enable_auto_merge_absorbs_graphql_rejection() -> None:
    error = GraphQLFailed(SimpleNamespace(errors=["rejected"]))  # type: ignore[arg-type]
    client = _TestGitHubClient(SimpleNamespace(async_graphql=AsyncMock(side_effect=error)))

    assert not await client.enable_auto_merge("ClawHaven/example", "PR_node")


async def test_enable_auto_merge_raises_transport_failure() -> None:
    error = _make_request_failed(500)
    client = _TestGitHubClient(SimpleNamespace(async_graphql=AsyncMock(side_effect=error)))

    with pytest.raises(RequestFailed) as raised:
        await client.enable_auto_merge("ClawHaven/example", "PR_node")

    assert raised.value is error


@pytest.mark.parametrize("status_code", [405, 409])
async def test_squash_merge_pull_request_absorbs_not_mergeable_status(
    status_code: int,
) -> None:
    pulls = SimpleNamespace(async_merge=AsyncMock(side_effect=_make_request_failed(status_code)))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    assert not await client.squash_merge_pull_request("ClawHaven/example", 7)


async def test_squash_merge_pull_request_raises_other_failures() -> None:
    error = _make_request_failed(422)
    pulls = SimpleNamespace(async_merge=AsyncMock(side_effect=error))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    with pytest.raises(RequestFailed) as raised:
        await client.squash_merge_pull_request("ClawHaven/example", 7)

    assert raised.value is error


async def test_update_pull_request_branch_accepts_already_current_branch() -> None:
    pulls = SimpleNamespace(
        async_update_branch=AsyncMock(side_effect=_make_request_failed(422)),
    )
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    await client.update_pull_request_branch("ClawHaven/example", 7)

    pulls.async_update_branch.assert_awaited_once_with("ClawHaven", "example", 7)


async def test_update_pull_request_branch_raises_other_failures() -> None:
    error = _make_request_failed(409)
    pulls = SimpleNamespace(async_update_branch=AsyncMock(side_effect=error))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    with pytest.raises(RequestFailed) as raised:
        await client.update_pull_request_branch("ClawHaven/example", 7)

    assert raised.value is error


async def test_create_branch_starts_at_the_head_of_the_default_branch() -> None:
    repository = SimpleNamespace(parsed_data=SimpleNamespace(default_branch="trunk"))
    head = SimpleNamespace(parsed_data=SimpleNamespace(object_=SimpleNamespace(sha="abc123")))
    git = SimpleNamespace(async_get_ref=AsyncMock(return_value=head), async_create_ref=AsyncMock())
    repos = SimpleNamespace(async_get=AsyncMock(return_value=repository))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(repos=repos, git=git)))

    await client.create_branch("ClawHaven/example", "agent/ACME-5")

    git.async_get_ref.assert_awaited_once_with("ClawHaven", "example", "heads/trunk")
    git.async_create_ref.assert_awaited_once_with(
        "ClawHaven", "example", ref="refs/heads/agent/ACME-5", sha="abc123"
    )


async def test_create_branch_starts_at_the_head_of_a_given_base() -> None:
    head = SimpleNamespace(parsed_data=SimpleNamespace(object_=SimpleNamespace(sha="def456")))
    git = SimpleNamespace(async_get_ref=AsyncMock(return_value=head), async_create_ref=AsyncMock())
    repos = SimpleNamespace(async_get=AsyncMock())
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(repos=repos, git=git)))

    await client.create_branch("ClawHaven/example", "agent/ACME-6", base="agent/ACME-5")

    repos.async_get.assert_not_awaited()
    git.async_get_ref.assert_awaited_once_with("ClawHaven", "example", "heads/agent/ACME-5")
    git.async_create_ref.assert_awaited_once_with(
        "ClawHaven", "example", ref="refs/heads/agent/ACME-6", sha="def456"
    )


def _merge_when_ready_client(
    *,
    state: str = "open",
    auto_merge_accepted: bool = True,
    squash_merge_accepted: bool = True,
) -> tuple[_TestGitHubClient, Mock]:
    client = _TestGitHubClient(SimpleNamespace())
    operations = Mock()
    get_pull_request = AsyncMock(return_value={"state": state, "node_id": "PR_node"})
    update_pull_request_branch = AsyncMock()
    enable_auto_merge = AsyncMock(return_value=auto_merge_accepted)
    squash_merge_pull_request = AsyncMock(return_value=squash_merge_accepted)
    operations.attach_mock(get_pull_request, "get_pull_request")
    operations.attach_mock(update_pull_request_branch, "update_pull_request_branch")
    operations.attach_mock(enable_auto_merge, "enable_auto_merge")
    operations.attach_mock(squash_merge_pull_request, "squash_merge_pull_request")
    client.get_pull_request = get_pull_request
    client.update_pull_request_branch = update_pull_request_branch
    client.enable_auto_merge = enable_auto_merge
    client.squash_merge_pull_request = squash_merge_pull_request
    return client, operations


async def test_merge_when_ready_short_circuits_closed_pull_request() -> None:
    client, operations = _merge_when_ready_client(state="closed")

    assert await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [call.get_pull_request("ClawHaven/example", 7)]


async def test_merge_when_ready_merges_clean_pull_request_directly() -> None:
    client, operations = _merge_when_ready_client()

    assert await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [
        call.get_pull_request("ClawHaven/example", 7),
        call.squash_merge_pull_request("ClawHaven/example", 7),
    ]


async def test_merge_when_ready_arms_auto_merge_before_updating_branch() -> None:
    client, operations = _merge_when_ready_client(squash_merge_accepted=False)

    assert await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [
        call.get_pull_request("ClawHaven/example", 7),
        call.squash_merge_pull_request("ClawHaven/example", 7),
        call.enable_auto_merge("ClawHaven/example", "PR_node"),
        call.update_pull_request_branch("ClawHaven/example", 7),
    ]


def _json(body: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(json=lambda: body)


def _pending(uuid: str) -> dict[str, object]:
    return {"status": "pending", "details": {"uuid": uuid, "message": "Merge request enqueued."}}


_MERGE_ASYNC = "/repos/ClawHaven/example/pulls/7/merge-async"


async def test_a_stacked_merge_is_one_request_and_gives_github_answer() -> None:
    arequest = AsyncMock(return_value=_json(_pending("u1")))
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    merge = await client.request_stacked_merge("ClawHaven/example", 7)

    assert merge == _pending("u1")
    assert arequest.await_args_list == [call("PUT", _MERGE_ASYNC, json={"merge_method": "squash"})]


async def test_a_stacked_merge_that_github_refused_is_a_failed_result() -> None:
    refusal = {"status": "failed", "details": {"message": "The pull request is a draft"}}
    arequest = AsyncMock(side_effect=_make_request_failed(400, refusal))
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    assert await client.request_stacked_merge("ClawHaven/example", 7) == refusal


async def test_a_stacked_merge_that_is_already_running_answers_with_that_merge() -> None:
    arequest = AsyncMock(side_effect=_make_request_failed(409, _pending("u1")))
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    assert await client.request_stacked_merge("ClawHaven/example", 7) == _pending("u1")


async def test_a_stacked_merge_raises_other_failures() -> None:
    error = _make_request_failed(403)
    client = _TestGitHubClient(SimpleNamespace(arequest=AsyncMock(side_effect=error)))

    with pytest.raises(RequestFailed) as raised:
        await client.request_stacked_merge("ClawHaven/example", 7)

    assert raised.value is error


async def test_reading_a_stacked_merge_asks_once_for_the_merge_by_its_id() -> None:
    merged = {"status": "merged", "details": {"message": "Merged", "sha": "abc"}}
    arequest = AsyncMock(return_value=_json(merged))
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    assert await client.read_stacked_merge("ClawHaven/example", 7, "u1") == merged
    assert arequest.await_args_list == [call("GET", f"{_MERGE_ASYNC}/u1")]


async def test_a_stacked_merge_that_github_no_longer_keeps_reads_as_failed() -> None:
    client = _TestGitHubClient(
        SimpleNamespace(arequest=AsyncMock(side_effect=_make_request_failed(404)))
    )

    merge = await client.read_stacked_merge("ClawHaven/example", 7, "u1")

    assert merge["status"] == "failed"


async def test_reading_a_stacked_merge_raises_other_failures() -> None:
    error = _make_request_failed(500)
    client = _TestGitHubClient(SimpleNamespace(arequest=AsyncMock(side_effect=error)))

    with pytest.raises(RequestFailed) as raised:
        await client.read_stacked_merge("ClawHaven/example", 7, "u1")

    assert raised.value is error


async def test_stack_on_starts_a_stack_of_the_two_pull_requests() -> None:
    arequest = AsyncMock(side_effect=[_json({"number": 5}), _json({})])
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    await client.stack_on("ClawHaven/example", 9, below=5)

    assert arequest.await_args_list == [
        call("GET", "/repos/ClawHaven/example/pulls/5"),
        call("POST", "/repos/ClawHaven/example/stacks", json={"pull_requests": [5, 9]}),
    ]


def _stack(*pull_requests: tuple[int, str, str | None]) -> SimpleNamespace:
    return _json(
        {
            "pull_requests": [
                {"number": number, "state": state, "merged_at": merged_at}
                for number, state, merged_at in pull_requests
            ]
        }
    )


_BELOW = {"number": 5, "stack": {"number": 16, "position": 2, "size": 3}}


async def test_stack_on_adds_the_pull_request_to_the_stack_that_holds_the_one_below() -> None:
    arequest = AsyncMock(
        side_effect=[_json(_BELOW), _stack((3, "open", None), (5, "open", None)), _json({})]
    )
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    await client.stack_on("ClawHaven/example", 9, below=5)

    assert arequest.await_args_list[2] == call(
        "POST", "/repos/ClawHaven/example/stacks/16/add", json={"pull_requests": [9]}
    )


async def test_stack_on_rebuilds_a_stack_that_a_closed_pull_request_tops() -> None:
    """A closed PR in a stack blocks every PR above it, and a second try of a build opens its
    PR on the same branch. The unstack removes every open PR, so the new stack holds all of
    them, in order, and then the new one: GitHub moves the lower ones when the bottom merges."""
    arequest = AsyncMock(
        side_effect=[
            _json(_BELOW),
            _stack((3, "open", None), (5, "open", None), (8, "closed", None)),
            _json({}),
            _json({}),
        ]
    )
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    await client.stack_on("ClawHaven/example", 9, below=5)

    assert arequest.await_args_list[2:] == [
        call("POST", "/repos/ClawHaven/example/stacks/16/unstack"),
        call("POST", "/repos/ClawHaven/example/stacks", json={"pull_requests": [3, 5, 9]}),
    ]


async def test_stack_on_keeps_a_stack_whose_top_merged() -> None:
    arequest = AsyncMock(
        side_effect=[
            _json(_BELOW),
            _stack((5, "open", None), (8, "closed", "2026-11-24T00:00:00Z")),
            _json({}),
        ]
    )
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    await client.stack_on("ClawHaven/example", 9, below=5)

    assert arequest.await_args_list[2] == call(
        "POST", "/repos/ClawHaven/example/stacks/16/add", json={"pull_requests": [9]}
    )


async def test_stack_on_names_why_github_refused() -> None:
    refusal = {"message": "Pull request #5 is already part of a stack"}
    arequest = AsyncMock(side_effect=[_json({}), _make_request_failed(422, refusal)])
    client = _TestGitHubClient(SimpleNamespace(arequest=arequest))

    with pytest.raises(GitHubStackError, match="already part of a stack") as raised:
        await client.stack_on("ClawHaven/example", 9, below=5)

    assert raised.value.status_code == 422


async def test_stack_on_names_a_failed_read_of_the_stack() -> None:
    """The read is part of the call: a failure of it is the same typed error."""
    error = _make_request_failed(409, {"message": "stack is being modified"})
    client = _TestGitHubClient(SimpleNamespace(arequest=AsyncMock(side_effect=error)))

    with pytest.raises(GitHubStackError, match="being modified") as raised:
        await client.stack_on("ClawHaven/example", 9, below=5)

    assert raised.value.status_code == 409


async def test_a_repository_has_stacks_when_github_lists_them() -> None:
    client = _TestGitHubClient(SimpleNamespace(arequest=AsyncMock(return_value=_json([]))))

    assert await client.has_stacks("ClawHaven/example") is True


async def test_a_repository_without_stacked_pull_requests_has_none() -> None:
    error = _make_request_failed(404)
    client = _TestGitHubClient(SimpleNamespace(arequest=AsyncMock(side_effect=error)))

    assert await client.has_stacks("ClawHaven/example") is False


async def test_a_failed_check_for_stacks_raises() -> None:
    error = _make_request_failed(500)
    client = _TestGitHubClient(SimpleNamespace(arequest=AsyncMock(side_effect=error)))

    with pytest.raises(RequestFailed) as raised:
        await client.has_stacks("ClawHaven/example")

    assert raised.value is error


async def test_the_default_branch_is_the_repositorys() -> None:
    repository = SimpleNamespace(parsed_data=SimpleNamespace(default_branch="trunk"))
    repos = SimpleNamespace(async_get=AsyncMock(return_value=repository))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(repos=repos)))

    assert await client.get_default_branch("ClawHaven/example") == "trunk"


async def test_a_pull_request_takes_another_base() -> None:
    pulls = SimpleNamespace(async_update=AsyncMock())
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    await client.update_pull_request_base("ClawHaven/example", 9, base="trunk")

    pulls.async_update.assert_awaited_once_with("ClawHaven", "example", 9, base="trunk")


async def test_the_open_pull_requests_on_a_branch_come_from_the_library() -> None:
    listed = SimpleNamespace(parsed_data=[SimpleNamespace(number=7), SimpleNamespace(number=9)])
    pulls = SimpleNamespace(async_list=AsyncMock(return_value=listed))
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(pulls=pulls)))

    assert await client.list_open_pull_requests("ClawHaven/example", base="agent/ACME-1") == [7, 9]

    pulls.async_list.assert_awaited_once_with(
        "ClawHaven", "example", base="agent/ACME-1", state="open"
    )


def _rebasing_client(*, behind_by: int = 2, merge_error: Exception | None = None):
    def ref(sha: str):
        return SimpleNamespace(parsed_data=SimpleNamespace(object_=SimpleNamespace(sha=sha)))

    author = SimpleNamespace(
        name="Druks Agent", email="agent@druks.ai", date="2026-11-24T10:00:00Z"
    )

    def commit(message: str = "", tree: str = "", sha: str = ""):
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                message=message, tree=SimpleNamespace(sha=tree), sha=sha, author=author
            )
        )

    git = SimpleNamespace(
        async_get_ref=AsyncMock(side_effect=lambda owner, name, path: ref(path + "-sha")),
        async_get_commit=AsyncMock(
            side_effect=lambda owner, name, sha: commit(message="Build it", tree=f"tree-of-{sha}")
        ),
        async_create_ref=AsyncMock(),
        async_create_commit=AsyncMock(
            side_effect=lambda owner, name, **kwargs: commit(
                sha=f"commit-{len(kwargs['parents'])}-{kwargs['parents'][0]}"
            )
        ),
        async_update_ref=AsyncMock(),
        async_delete_ref=AsyncMock(),
    )
    merge = SimpleNamespace(
        parsed_data=SimpleNamespace(commit=SimpleNamespace(tree=SimpleNamespace(sha="merged-tree")))
    )
    repos = SimpleNamespace(
        async_merge=AsyncMock(return_value=merge, side_effect=merge_error),
        async_compare_commits=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(behind_by=behind_by))
        ),
    )
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(git=git, repos=repos)))
    return client, git, repos


async def test_a_branch_moves_onto_the_new_tip_with_the_merge_starting_from_its_old_base() -> None:
    """A plain merge of the new tip starts from the branch's own copy of the lower commits,
    and conflicts when that commit was amended or squashed. The merge starts from the old
    base instead: a commit there with the new tip's tree, and the branch merged into it."""
    client, git, repos = _rebasing_client()

    await client.rebase_branch(
        "ClawHaven/example", "agent/ACME-2", onto="agent/ACME-1", since="old-base"
    )

    git.async_create_commit.assert_any_await(
        "ClawHaven",
        "example",
        message="Base of agent/ACME-2",
        tree="tree-of-heads/agent/ACME-1-sha",
        parents=["old-base"],
    )
    [scratch_call] = git.async_create_ref.await_args_list
    scratch = scratch_call.kwargs["ref"].removeprefix("refs/heads/")
    assert scratch.startswith("agent/ACME-2-rebase-")
    assert scratch_call.kwargs["sha"] == "commit-1-old-base"
    repos.async_merge.assert_awaited_once_with(
        "ClawHaven", "example", base=scratch, head="heads/agent/ACME-2-sha"
    )
    git.async_create_commit.assert_any_await(
        "ClawHaven",
        "example",
        message="Build it",
        tree="merged-tree",
        parents=["heads/agent/ACME-1-sha"],
        author={"name": "Druks Agent", "email": "agent@druks.ai", "date": "2026-11-24T10:00:00Z"},
    )
    git.async_update_ref.assert_awaited_once_with(
        "ClawHaven",
        "example",
        "heads/agent/ACME-2",
        sha="commit-1-heads/agent/ACME-1-sha",
        force=True,
    )
    git.async_delete_ref.assert_awaited_once_with("ClawHaven", "example", f"heads/{scratch}")


async def test_a_branch_that_holds_the_new_tip_already_is_left_alone() -> None:
    """GitHub rebases a chain of three itself, and its push to B makes the handler rebase C
    onto a tip that C holds."""
    client, git, repos = _rebasing_client(behind_by=0)

    await client.rebase_branch(
        "ClawHaven/example", "agent/ACME-2", onto="agent/ACME-1", since="old-base"
    )

    repos.async_merge.assert_not_awaited()
    git.async_create_ref.assert_not_awaited()
    git.async_update_ref.assert_not_awaited()


async def test_a_real_conflict_raises_moves_nothing_and_removes_the_scratch_branch() -> None:
    client, git, _ = _rebasing_client(merge_error=_make_request_failed(409))

    with pytest.raises(GitHubMergeConflictError):
        await client.rebase_branch(
            "ClawHaven/example", "agent/ACME-2", onto="agent/ACME-1", since="old-base"
        )

    git.async_update_ref.assert_not_awaited()
    git.async_delete_ref.assert_awaited_once()


async def test_a_rebase_raises_other_failures() -> None:
    error = _make_request_failed(404)
    client, _, _ = _rebasing_client(merge_error=error)

    with pytest.raises(RequestFailed) as raised:
        await client.rebase_branch(
            "ClawHaven/example", "agent/ACME-2", onto="agent/ACME-1", since="old-base"
        )

    assert raised.value is error


async def test_merge_when_ready_reports_unsettled_merge() -> None:
    client, operations = _merge_when_ready_client(
        auto_merge_accepted=False,
        squash_merge_accepted=False,
    )

    assert not await client.merge_when_ready("ClawHaven/example", 7)
    assert operations.mock_calls == [
        call.get_pull_request("ClawHaven/example", 7),
        call.squash_merge_pull_request("ClawHaven/example", 7),
        call.enable_auto_merge("ClawHaven/example", "PR_node"),
    ]


class _TestGitHubAppClient(GitHubAppClient):
    def __init__(self, apps: Any) -> None:
        self._slug = "example-app"
        self._app = SimpleNamespace(rest=SimpleNamespace(apps=apps))


async def test_an_account_installation_falls_back_to_the_user_endpoint() -> None:
    apps = SimpleNamespace(
        async_get_org_installation=AsyncMock(side_effect=_make_request_failed(404)),
        async_get_user_installation=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=42))
        ),
    )

    assert (await _TestGitHubAppClient(apps).get_installation("czpython")).id == 42
    apps.async_get_org_installation.assert_awaited_once_with("czpython")
    apps.async_get_user_installation.assert_awaited_once_with("czpython")


async def test_an_org_installation_does_not_consult_the_user_endpoint() -> None:
    apps = SimpleNamespace(
        async_get_org_installation=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=41))
        ),
        async_get_user_installation=AsyncMock(),
    )

    assert (await _TestGitHubAppClient(apps).get_installation("clawhaven")).id == 41
    apps.async_get_user_installation.assert_not_awaited()


async def test_installation_lookup_404_names_the_repo() -> None:
    """An App without access to the repo fails at the installation lookup. The raw
    githubkit repr (``Response(404 Not Found, …)``) is what a run's failure column
    would show the operator — translate it into an error that names the repo and
    the fix."""
    apps = SimpleNamespace(
        async_get_repo_installation=AsyncMock(side_effect=_make_request_failed(404))
    )

    with pytest.raises(GitHubAppNotInstalledError) as raised:
        await _TestGitHubAppClient(apps).get_installation("ClawHaven/acme-app")

    assert "The GitHub App example-app has no access to ClawHaven/acme-app" in str(raised.value)
    assert "install the app" in str(raised.value)


async def test_list_repos_paginates() -> None:
    first_page = [
        SimpleNamespace(full_name=f"czpython/repo-{number}", description=None)
        for number in range(100)
    ]
    second_page = [
        SimpleNamespace(full_name="czpython/repo-100", description="first"),
        SimpleNamespace(full_name="czpython/repo-101", description="second"),
    ]
    apps = SimpleNamespace(
        async_list_repos_accessible_to_installation=AsyncMock(
            side_effect=[
                SimpleNamespace(parsed_data=SimpleNamespace(repositories=first_page)),
                SimpleNamespace(parsed_data=SimpleNamespace(repositories=second_page)),
            ]
        )
    )
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(apps=apps)))

    repos = await client.list_repos()

    assert len(repos) == 102
    assert repos[0] == {"full_name": "czpython/repo-0", "description": None}
    assert repos[-1] == {"full_name": "czpython/repo-101", "description": "second"}
    apps.async_list_repos_accessible_to_installation.assert_has_awaits(
        [call(per_page=100, page=1), call(per_page=100, page=2)]
    )


async def test_get_bot_user_id_asks_with_an_active_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = SimpleNamespace(
        async_get_by_username=AsyncMock(
            return_value=SimpleNamespace(parsed_data=SimpleNamespace(id=123456789))
        )
    )
    tokens = []

    @asynccontextmanager
    async def github(auth, *, base_url, **_):
        tokens.append(auth.token)
        yield SimpleNamespace(rest=SimpleNamespace(users=users))

    monkeypatch.setattr(github_api, "GitHub", github)
    installations = [
        SimpleNamespace(id=1, suspended_at="2026-11-01T00:00:00Z"),
        SimpleNamespace(id=2, suspended_at=None),
    ]
    apps = SimpleNamespace(
        async_list_installations=AsyncMock(return_value=SimpleNamespace(parsed_data=installations))
    )
    client = _TestGitHubAppClient(apps)
    client.create_installation_token = AsyncMock(return_value=("ghs_two", None))

    assert await client.get_bot_user_id() == 123456789
    # A suspended installation mints no token.
    client.create_installation_token.assert_awaited_once_with(2)
    assert tokens == ["ghs_two"]
    users.async_get_by_username.assert_awaited_once_with("example-app[bot]")


async def test_get_bot_user_id_without_an_active_installation_says_so() -> None:
    installations = [SimpleNamespace(id=1, suspended_at="2026-11-01T00:00:00Z")]
    apps = SimpleNamespace(
        async_list_installations=AsyncMock(return_value=SimpleNamespace(parsed_data=installations))
    )

    with pytest.raises(GitHubAppNotActiveError, match="no active installation"):
        await _TestGitHubAppClient(apps).get_bot_user_id()


async def test_the_app_row_keeps_the_bot_user_id(druks_db, monkeypatch: pytest.MonkeyPatch):
    row = await connect_service(
        "github", identity={"app_id": "1", "slug": "druks-acme"}, secrets={"private_key": "pem"}
    )
    app = SimpleNamespace(get_bot_user_id=AsyncMock(return_value=7))
    monkeypatch.setattr(Github, "_app_client", staticmethod(lambda row: app))

    author = await Github.get_bot_git_author()

    assert author == ("druks-acme[bot]", "7+druks-acme[bot]@users.noreply.github.com")
    assert row.identity["bot_user_id"] == 7
    assert await Github.get_bot_git_author() == author
    app.get_bot_user_id.assert_awaited_once()


async def test_github_prepares_a_box_with_the_bot_and_the_github_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": str(tmp_path / ".gitconfig")}

    async def exec(command, *, timeout):
        result = subprocess.run(command, env=env, check=False, capture_output=True, text=True)
        return SimpleNamespace(returncode=result.returncode, stderr=result.stderr)

    monkeypatch.setattr(
        Github,
        "get_bot_git_author",
        AsyncMock(return_value=("app[bot]", "1+app[bot]@users.noreply.github.com")),
    )
    sign_in = SimpleNamespace(
        identity={"authority": "https://github.com", "subject": "42", "login": "octocat"}
    )

    await Github.prepare_sandbox(
        SimpleNamespace(id="sandbox-1", exec=exec),  # type: ignore[arg-type]
        Connection(Github, sign_in),  # type: ignore[arg-type]
    )
    # The agent clones its repos after Druks writes the identity.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("Change\n")
    subprocess.run([str(hook), str(message)], cwd=repo, env=env, check=True)
    email = subprocess.run(
        ["git", "-C", str(repo), "config", "user.email"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert email.stdout.strip() == "1+app[bot]@users.noreply.github.com"
    assert "Co-Authored-By: octocat <42+octocat@users.noreply.github.com>" in message.read_text()
    assert (repo / ".git" / "info" / "exclude").exists()


@pytest.mark.parametrize(
    "error",
    [
        ServiceNotConnectedError("github"),
        GitHubAppNotActiveError("druks-acme"),
        TimeoutError(),
        RequestTimeout(Mock()),
    ],
)
async def test_a_failed_bot_lookup_leaves_the_box_without_an_author(
    monkeypatch: pytest.MonkeyPatch, caplog, error
):
    monkeypatch.setattr(Github, "get_bot_git_author", AsyncMock(side_effect=error))
    host = SimpleNamespace(id="sandbox-1", exec=AsyncMock())
    sign_in = SimpleNamespace(identity={"subject": "42", "login": "octocat"})

    await Github.prepare_sandbox(host, Connection(Github, sign_in))  # type: ignore[arg-type]

    host.exec.assert_not_awaited()
    assert "Sandbox sandbox-1 has no bot author" in caplog.text


def _template_generating_client(repo_apis: Any) -> _TestGitHubClient:
    return _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(repos=repo_apis)))


async def test_create_repo_from_template_generates_a_private_repo() -> None:
    repo_apis = SimpleNamespace(async_create_using_template=AsyncMock())
    client = _template_generating_client(repo_apis)

    full_name = await client.create_repo_from_template(
        "clawhaven", "clawhaven/shop-template", "acme-coffee"
    )

    assert full_name == "clawhaven/acme-coffee"
    repo_apis.async_create_using_template.assert_awaited_once_with(
        "clawhaven",
        "shop-template",
        name="acme-coffee",
        owner="clawhaven",
        private=True,
    )


async def test_create_repo_from_template_reads_a_taken_name_as_already_created() -> None:
    repo_apis = SimpleNamespace(
        async_create_using_template=AsyncMock(side_effect=_make_request_failed(422))
    )
    client = _template_generating_client(repo_apis)

    full_name = await client.create_repo_from_template(
        "clawhaven", "clawhaven/shop-template", "acme-coffee"
    )

    assert full_name == "clawhaven/acme-coffee"


class _Checks:
    def __init__(self, posted_ids: tuple[int, ...] = ()) -> None:
        self.posted_ids = posted_ids
        self.calls: list[tuple[str, Any]] = []

    async def async_list_for_ref(self, owner: str, repo: str, ref: str, **kwargs: Any) -> Any:
        runs = [SimpleNamespace(id=run_id) for run_id in self.posted_ids]
        return SimpleNamespace(parsed_data=SimpleNamespace(check_runs=runs))

    async def async_create(self, owner: str, repo: str, *, data: dict[str, Any]) -> Any:
        self.calls.append(("create", data))
        return SimpleNamespace(parsed_data=SimpleNamespace(id=900))

    async def async_update(
        self, owner: str, repo: str, check_run_id: int, *, data: dict[str, Any]
    ) -> Any:
        self.calls.append(("update", {"check_run_id": check_run_id, **data}))


def _checks_client(checks: _Checks) -> _TestGitHubClient:
    client = _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(checks=checks)))
    client._app_id = "41"
    return client


async def test_post_check_run_creates_the_run_and_sends_annotations_in_batches() -> None:
    checks = _Checks()

    await _checks_client(checks).post_check_run(
        "acme/widget",
        head_sha="0f3a2c1d",
        name="druks-house-rules",
        status="completed",
        conclusion="failure",
        title="120 findings",
        summary="One annotation per finding.",
        annotations=[{"path": "app.py", "start_line": line} for line in range(120)],
    )

    assert [name for name, _ in checks.calls] == ["create", "update", "update"]
    assert checks.calls[0][1]["head_sha"] == "0f3a2c1d"
    assert checks.calls[0][1]["conclusion"] == "failure"
    assert [len(data["output"]["annotations"]) for _, data in checks.calls] == [50, 50, 20]
    assert {data["check_run_id"] for _, data in checks.calls[1:]} == {900}


async def test_post_check_run_updates_the_run_this_app_already_posted() -> None:
    checks = _Checks(posted_ids=(77,))

    await _checks_client(checks).post_check_run(
        "acme/widget",
        head_sha="0f3a2c1d",
        name="druks-house-rules",
        status="in_progress",
        title="Checking",
        summary="",
    )

    assert checks.calls == [
        (
            "update",
            {
                "check_run_id": 77,
                "name": "druks-house-rules",
                "status": "in_progress",
                "output": {"title": "Checking", "summary": "", "annotations": []},
            },
        )
    ]


def _actions_client(actions: Any) -> _TestGitHubClient:
    return _TestGitHubClient(SimpleNamespace(rest=SimpleNamespace(actions=actions)))


async def test_dispatch_workflow_returns_the_run_it_started() -> None:
    dispatched = SimpleNamespace(parsed_data=SimpleNamespace(workflow_run_id=4512))
    actions = SimpleNamespace(async_create_workflow_dispatch=AsyncMock(return_value=dispatched))

    run_id = await _actions_client(actions).dispatch_workflow(
        "acme/portal", "deploy.yml", ref="main", inputs={"environment": "dev"}
    )

    assert run_id == 4512
    actions.async_create_workflow_dispatch.assert_awaited_once_with(
        "acme", "portal", "deploy.yml", ref="main", inputs={"environment": "dev"}
    )


async def test_dispatch_workflow_without_inputs_omits_them() -> None:
    dispatched = SimpleNamespace(parsed_data=SimpleNamespace(workflow_run_id=4512))
    actions = SimpleNamespace(async_create_workflow_dispatch=AsyncMock(return_value=dispatched))

    await _actions_client(actions).dispatch_workflow("acme/portal", "deploy.yml", ref="main")

    actions.async_create_workflow_dispatch.assert_awaited_once_with(
        "acme", "portal", "deploy.yml", ref="main", inputs=github_api.UNSET
    )


async def test_get_workflow_run_reads_its_status_and_conclusion() -> None:
    run = {"status": "completed", "conclusion": "success", "html_url": "https://x/runs/4512"}
    fetched = SimpleNamespace(parsed_data=SimpleNamespace(model_dump=lambda: run))
    actions = SimpleNamespace(async_get_workflow_run=AsyncMock(return_value=fetched))

    assert await _actions_client(actions).get_workflow_run("acme/portal", 4512) == run
    actions.async_get_workflow_run.assert_awaited_once_with("acme", "portal", 4512)


def test_github_clients_wait_with_a_limit() -> None:
    clients = [
        GitHubClient("token", app_id="1", slug="druks")._github,
        GitHubAppClient(app_id="1", private_key="key")._app,
    ]

    assert [client.config.timeout for client in clients] == [github_api.DEFAULT_TIMEOUT] * 2
    assert [client.config.auto_retry for client in clients] == [RETRY_SERVER_ERROR] * 2
