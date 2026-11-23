import base64
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Literal

import httpx
from githubkit import AppAuthStrategy, GitHub, TokenAuthStrategy
from githubkit.exception import GraphQLFailed, RequestFailed
from githubkit.retry import RETRY_SERVER_ERROR
from githubkit.utils import UNSET

from druks.core.apis import exceptions
from druks.core.utils.time import ensure_utc

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
# The authority of a GitHub sign-in: one GitHub, so a user id names one person.
GITHUB_AUTHORITY = "https://github.com"

# githubkit waits with no limit unless it gets a timeout. The read limit applies to
# each read, not to the whole response, so a large tarball still downloads.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0, write=10.0, pool=5.0)


@dataclass(frozen=True)
class ReviewComment:
    path: str
    line: int
    body: str
    start_line: int | None = None


_ANNOTATIONS_PER_REQUEST = 50


# Where-druks-may-act is derived from the App's installations. Module-level
# so it survives the per-call-site client construction; keyed by app id;
# serves the last-known set when GitHub hiccups.
_INSTALLATION_ACCOUNTS_TTL_SECONDS = 600.0
_INSTALLATION_ACCOUNTS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}


class GitHubAppClient:
    """The App's key signs its calls at ``app_url``: the App itself, its installations,
    and a token for one of them."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        app_url: str = GITHUB_API_URL,
        slug: str = "",
    ) -> None:
        self._app_id = app_id
        self._slug = slug
        self._app = GitHub(
            AppAuthStrategy(app_id, private_key),
            base_url=app_url,
            timeout=DEFAULT_TIMEOUT,
            auto_retry=RETRY_SERVER_ERROR,
        )

    async def get_installation(self, owner_or_repo: str):
        """The installation that covers a repository ``owner/name``, or an account."""
        owner, _, name = owner_or_repo.partition("/")
        apps = self._app.rest.apps
        if name:
            lookups = [partial(apps.async_get_repo_installation, owner, name)]
        else:
            # An organization and a user each have their own endpoint.
            lookups = [
                partial(apps.async_get_org_installation, owner),
                partial(apps.async_get_user_installation, owner),
            ]
        for lookup in lookups:
            try:
                return (await lookup()).parsed_data
            except RequestFailed as error:
                if error.response.status_code != 404:
                    raise
        raise exceptions.GitHubAppNotInstalledError(owner_or_repo, app=self._slug)

    async def create_installation_token(
        self, installation_id: int, repositories: Sequence[str] = ()
    ) -> tuple[str, datetime]:
        """A token for the installation, and the expiry GitHub gave it. Named
        ``repositories`` limit it to them; without them it covers the whole installation."""
        response = await self._app.rest.apps.async_create_installation_access_token(
            installation_id, repositories=list(repositories) or UNSET
        )
        minted = response.parsed_data
        return str(minted.token), ensure_utc(datetime.fromisoformat(str(minted.expires_at)))

    async def get_bot_user_id(self) -> int:
        """The user id of the App's bot user, ``<slug>[bot]``. The users endpoint
        answers a token of the first installation that is not suspended: GitHub
        limits anonymous requests to 60 an hour per IP."""
        response = await self._app.rest.apps.async_list_installations(per_page=100)
        installation_ids = [item.id for item in response.parsed_data if not item.suspended_at]
        if not installation_ids:
            raise exceptions.GitHubAppNotActiveError(self._slug)
        token, _ = await self.create_installation_token(installation_ids[0])
        async with GitHub(
            TokenAuthStrategy(token),
            base_url=GITHUB_API_URL,
            timeout=DEFAULT_TIMEOUT,
            auto_retry=RETRY_SERVER_ERROR,
        ) as client:
            response = await client.rest.users.async_get_by_username(f"{self._slug}[bot]")
        return response.parsed_data.id

    async def list_installation_accounts(self, cached: bool = True) -> tuple[str, ...]:
        """Account logins (orgs/users) this App is installed on — the
        authoritative "where druks may act". Install the App somewhere and
        druks works there; uninstall and it stops. Cached ~10 min per app
        id, serving the last-known set when GitHub hiccups. ``cached=False``
        asks now, after an install."""
        known = _INSTALLATION_ACCOUNTS_CACHE.get(self._app_id)
        now = time.monotonic()
        if cached and known and now - known[0] < _INSTALLATION_ACCOUNTS_TTL_SECONDS:
            return known[1]
        try:
            accounts: list[str] = []
            page = 1
            while True:
                response = await self._app.rest.apps.async_list_installations(
                    per_page=100, page=page
                )
                batch = response.parsed_data
                accounts.extend(
                    str(login)
                    for installation in batch
                    if (login := getattr(installation.account, "login", None))
                )
                if len(batch) < 100:
                    break
                page += 1
        except Exception:
            if known:
                logger.warning(
                    "Could not refresh App installations; serving the last-known set.",
                    exc_info=True,
                )
                return known[1]
            raise
        result = tuple(dict.fromkeys(accounts))
        _INSTALLATION_ACCOUNTS_CACHE[self._app_id] = (now, result)
        return result

    async def get_app_permissions(self) -> dict[str, str]:
        """What GitHub grants the App today, by permission name."""
        response = await self._app.rest.apps.async_get_authenticated()
        return response.json()["permissions"]

    async def get_authenticated_app_slug(self) -> str:
        """The slug GitHub reports for these credentials — a live App-JWT call,
        so it proves the App ID matches the PEM. The connect flow's validation;
        mention resolution reads the stored slug instead."""
        response = await self._app.rest.apps.async_get_authenticated()
        return str(getattr(response.parsed_data, "slug", None) or "")


class GitHubClient:
    """The App acting in one installation, with the token minted for it."""

    def __init__(self, token: str, *, app_id: str, slug: str) -> None:
        self._app_id = app_id
        self._slug = slug
        self._github = GitHub(
            TokenAuthStrategy(token),
            base_url=GITHUB_API_URL,
            timeout=DEFAULT_TIMEOUT,
            auto_retry=RETRY_SERVER_ERROR,
        )

    async def list_repos(self) -> list[dict[str, Any]]:
        """The repositories the installation can see, as
        ``[{"full_name": "<owner>/<name>", "description": ...}, ...]``."""
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._github.rest.apps.async_list_repos_accessible_to_installation(
                per_page=100,
                page=page,
            )
            items = response.parsed_data.repositories
            repos.extend(
                {"full_name": repository.full_name, "description": repository.description}
                for repository in items
            )
            if len(items) < 100:
                break
            page += 1
        return repos

    async def create_repo_from_template(self, owner: str, template_repo: str, name: str) -> str:
        """A new private repo ``name`` under ``owner``, generated from the
        ``owner/name`` template ``template_repo``; returns its ``full_name``.
        Safe to retry: a name already taken reads as already created."""
        template_owner, template_name = template_repo.split("/", 1)
        full_name = f"{owner}/{name}"
        try:
            await self._github.rest.repos.async_create_using_template(
                template_owner,
                template_name,
                name=name,
                owner=owner,
                private=True,
            )
        except RequestFailed as error:
            # On a valid name GitHub's 422 is "already taken" — a retried create.
            if error.response.status_code == 422:
                return full_name
            raise
        return full_name

    async def get_repository(self, repo: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.repos.async_get(owner, name)
        return response.parsed_data.model_dump()

    async def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.issues.async_get(owner, name, issue_number)
        return response.parsed_data.model_dump()

    async def list_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        """Every top-level comment on the issue or pull request, oldest first."""
        owner, name = repo.split("/", 1)
        comments = self._github.rest.paginate(
            self._github.rest.issues.async_list_comments,
            owner=owner,
            repo=name,
            issue_number=issue_number,
            per_page=100,
        )
        return [comment.model_dump() async for comment in comments]

    async def list_review_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Every inline comment on the pull request, oldest first."""
        owner, name = repo.split("/", 1)
        comments = self._github.rest.paginate(
            self._github.rest.pulls.async_list_review_comments,
            owner=owner,
            repo=name,
            pull_number=pr_number,
            per_page=100,
        )
        return [comment.model_dump() async for comment in comments]

    async def create_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.issues.async_create_comment(
            owner, name, issue_number, body=body
        )
        return response.parsed_data.model_dump()

    async def create_reaction(self, repo: str, comment_id: int, content: str) -> None:
        """React to a top-level comment. GitHub keeps one reaction per App and content,
        so reacting again changes nothing."""
        owner, name = repo.split("/", 1)
        await self._github.rest.reactions.async_create_for_issue_comment(
            owner, name, comment_id, content=content
        )

    async def create_review_comment_reaction(
        self, repo: str, comment_id: int, content: str
    ) -> None:
        """React to a comment in a pull request review."""
        owner, name = repo.split("/", 1)
        await self._github.rest.reactions.async_create_for_pull_request_review_comment(
            owner, name, comment_id, content=content
        )

    async def reply_to_review_comment(
        self, repo: str, pr_number: int, comment_id: int, body: str
    ) -> dict[str, Any]:
        """A reply in the inline thread that ``comment_id`` starts. GitHub takes the
        thread's first comment only, never a reply in it."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_create_reply_for_review_comment(
            owner, name, pr_number, comment_id, body=body
        )
        return response.parsed_data.model_dump()

    async def replace_issue_labels(
        self, repo: str, issue_number: int, *, add: str, remove: set[str]
    ) -> None:
        """Put ``add`` on the issue and take ``remove`` off. GitHub answers 404 for a
        label the issue does not carry, and that label is already off."""
        owner, name = repo.split("/", 1)
        await self._github.rest.issues.async_add_labels(owner, name, issue_number, data=[add])
        for label in remove:
            try:
                await self._github.rest.issues.async_remove_label(owner, name, issue_number, label)
            except RequestFailed as error:
                if error.response.status_code != 404:
                    raise

    async def list_blocked_by(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        owner, name = repo.split("/", 1)
        issues = self._github.rest.paginate(
            self._github.rest.issues.async_list_dependencies_blocked_by,
            owner=owner,
            repo=name,
            issue_number=issue_number,
            per_page=100,
        )
        return [issue.model_dump() async for issue in issues]

    async def list_blocking(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        owner, name = repo.split("/", 1)
        issues = self._github.rest.paginate(
            self._github.rest.issues.async_list_dependencies_blocking,
            owner=owner,
            repo=name,
            issue_number=issue_number,
            per_page=100,
        )
        return [issue.model_dump() async for issue in issues]

    async def close_issue(self, repo: str, issue_number: int) -> None:
        owner, name = repo.split("/", 1)
        await self._github.rest.issues.async_update(
            owner, name, issue_number, state="closed", state_reason="completed"
        )

    async def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_get(owner, name, pr_number)
        return response.parsed_data.model_dump()

    async def squash_merge_pull_request(self, repo: str, pr_number: int) -> bool:
        # No commit_title: GitHub then titles the squash commit from the PR title.
        owner, name = repo.split("/", 1)
        try:
            response = await self._github.rest.pulls.async_merge(
                owner,
                name,
                pr_number,
                data={"merge_method": "squash"},
            )
        except RequestFailed as error:
            if error.response.status_code in {405, 409}:
                return False
            raise
        return response.parsed_data.merged

    async def enable_auto_merge(self, repo: str, node_id: str) -> bool:
        try:
            await self._github.async_graphql(
                """
                mutation EnablePullRequestAutoMerge($id: ID!) {
                  enablePullRequestAutoMerge(
                    input: {pullRequestId: $id, mergeMethod: SQUASH}
                  ) {
                    pullRequest { id }
                  }
                }
                """,
                {"id": node_id},
            )
        except GraphQLFailed:
            return False
        return True

    async def update_pull_request_branch(self, repo: str, pr_number: int) -> None:
        owner, name = repo.split("/", 1)
        try:
            await self._github.rest.pulls.async_update_branch(owner, name, pr_number)
        except RequestFailed as error:
            if error.response.status_code == 422:
                return
            raise

    async def rebase_branch(self, repo: str, branch: str, onto: str, *, since: str) -> None:
        """Move the one commit of ``branch`` from ``since``, the tip it was built on, onto the tip
        of ``onto``, so that a stack keeps the linear history that GitHub requires to merge it.
        GitHub refuses to update the branch of a stacked PR, and has no call that rebases a
        stack. A plain merge of the new tip into the branch starts from the branch's own old
        copy of the lower commits, and reports a conflict when that commit was amended or
        squashed. So the merge starts from ``since``: a scratch branch holds a commit on
        ``since`` with the tree of ``onto``, and the branch merges into it. The result goes on
        ``onto`` as one commit with the branch's message. GitHub answers a real conflict
        with 409."""
        owner, name = repo.split("/", 1)
        git = self._github.rest.git
        # Behind by nothing: the branch holds the new tip already, as it does when GitHub
        # rebased the chain itself.
        comparison = await self._github.rest.repos.async_compare_commits(
            owner, name, basehead=f"{onto}...{branch}"
        )
        if not comparison.parsed_data.behind_by:
            return
        tip = (await git.async_get_ref(owner, name, f"heads/{branch}")).parsed_data.object_.sha
        onto_tip = (await git.async_get_ref(owner, name, f"heads/{onto}")).parsed_data.object_.sha
        onto_tree = (await git.async_get_commit(owner, name, onto_tip)).parsed_data.tree.sha
        original = (await git.async_get_commit(owner, name, tip)).parsed_data
        anchor = await git.async_create_commit(
            owner, name, message=f"Base of {branch}", tree=onto_tree, parents=[since]
        )
        # A new name each time: a scratch branch that a crash left behind refuses the next one.
        scratch = f"{branch}-rebase-{uuid.uuid4().hex[:8]}"
        await git.async_create_ref(
            owner, name, ref=f"refs/heads/{scratch}", sha=anchor.parsed_data.sha
        )
        try:
            try:
                merge = await self._github.rest.repos.async_merge(
                    owner, name, base=scratch, head=tip
                )
            except RequestFailed as error:
                if error.response.status_code == 409:
                    raise exceptions.GitHubMergeConflictError(
                        f"{repo}: {branch} onto {onto}"
                    ) from error
                raise
            # The commit keeps its message and its author. Its committer is whoever rebased it.
            commit = await git.async_create_commit(
                owner,
                name,
                message=original.message,
                tree=merge.parsed_data.commit.tree.sha,
                parents=[onto_tip],
                author={
                    "name": original.author.name,
                    "email": original.author.email,
                    "date": original.author.date,
                },
            )
            await git.async_update_ref(
                owner, name, f"heads/{branch}", sha=commit.parsed_data.sha, force=True
            )
        finally:
            await git.async_delete_ref(owner, name, f"heads/{scratch}")

    async def merge_when_ready(self, repo: str, pr_number: int) -> bool:
        """Whether GitHub accepted ownership of the merge."""
        pull_request = await self.get_pull_request(repo, pr_number)
        if pull_request["state"] == "closed":
            return True

        # A clean pull request merges directly: GitHub refuses to arm auto-merge
        # on one, and updating its branch first moves the head asynchronously,
        # racing an immediate merge into a 409.
        if await self.squash_merge_pull_request(repo, pr_number):
            return True
        if await self.enable_auto_merge(repo, pull_request["node_id"]):
            await self.update_pull_request_branch(repo, pr_number)
            return True
        return False

    async def get_stack(self, repo: str, pr_number: int) -> dict[str, object] | None:
        """The stack that holds the pull request, as GitHub gives it on the pull request.
        githubkit's model of a pull request drops this field, so the request is raw."""
        owner, name = repo.split("/", 1)
        response = await self._github.arequest("GET", f"/repos/{owner}/{name}/pulls/{pr_number}")
        return response.json().get("stack")

    async def has_stacks(self, repo: str) -> bool:
        """Whether the repository has stacked pull requests. GitHub answers 404 when it has
        not."""
        owner, name = repo.split("/", 1)
        try:
            await self._github.arequest("GET", f"/repos/{owner}/{name}/stacks")
        except RequestFailed as error:
            if error.response.status_code == 404:
                return False
            raise
        return True

    async def stack_on(self, repo: str, pr_number: int, *, below: int) -> None:
        """Put the pull request in the stack of ``below``, the pull request whose branch it
        targets. ``below`` starts a stack when it has none. GitHub accepts only a pull
        request that targets the top of a stack, and a closed pull request on top blocks the
        stack. Such a stack is dissolved, and a new one holds its open pull requests, in
        order, and then this one."""
        owner, name = repo.split("/", 1)
        try:
            stack = await self.get_stack(repo, below)
            pull_requests = [below, pr_number]
            if stack:
                number = stack["number"]
                response = await self._github.arequest(
                    "GET", f"/repos/{owner}/{name}/stacks/{number}"
                )
                members = response.json()["pull_requests"]
                top = members[-1]
                if top["state"] == "closed" and not top["merged_at"]:
                    pull_requests = [
                        member["number"] for member in members if member["state"] == "open"
                    ] + [pr_number]
                    await self._github.arequest(
                        "POST", f"/repos/{owner}/{name}/stacks/{number}/unstack"
                    )
                    stack = None
            if stack:
                await self._github.arequest(
                    "POST",
                    f"/repos/{owner}/{name}/stacks/{stack['number']}/add",
                    json={"pull_requests": [pr_number]},
                )
            else:
                await self._github.arequest(
                    "POST", f"/repos/{owner}/{name}/stacks", json={"pull_requests": pull_requests}
                )
        except RequestFailed as error:
            raise exceptions.GitHubStackError(
                f"{repo}#{pr_number} on #{below}: {error.response.status_code} "
                f"{error.response.text}",
                status_code=error.response.status_code,
            ) from error

    async def list_open_pull_requests(self, repo: str, *, base: str) -> list[int]:
        """The numbers of the open pull requests that target the branch ``base``."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_list(owner, name, base=base, state="open")
        return [pull_request.number for pull_request in response.parsed_data]

    async def request_stacked_merge(self, repo: str, pr_number: int):
        """Ask GitHub to merge a stacked pull request. GitHub merges it only through the
        asynchronous endpoint, and that merge takes every open pull request below it too.
        The answer says whether the merge is still running."""
        owner, name = repo.split("/", 1)
        try:
            response = await self._github.arequest(
                "PUT",
                f"/repos/{owner}/{name}/pulls/{pr_number}/merge-async",
                json={"merge_method": "squash"},
            )
        except RequestFailed as error:
            # 409: a merge of this pull request is already running, and the answer is its.
            # 400: the pull request is closed or still a draft. Both answer as a result.
            if error.response.status_code not in {400, 409}:
                raise
            return error.response.json()
        return response.json()

    async def read_stacked_merge(self, repo: str, pr_number: int, uuid: str):
        """Where the merge that ``request_stacked_merge`` started has got to."""
        owner, name = repo.split("/", 1)
        try:
            response = await self._github.arequest(
                "GET", f"/repos/{owner}/{name}/pulls/{pr_number}/merge-async/{uuid}"
            )
        except RequestFailed as error:
            # GitHub keeps the result for 24 hours and then answers 404. A merge that was
            # still pending then is lost, and the work goes back to review.
            if error.response.status_code != 404:
                raise
            return {
                "status": "failed",
                "details": {"message": "GitHub no longer keeps the result of this merge"},
            }
        return response.json()

    async def update_pull_request_body(
        self,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        response = await self._github.rest.pulls.async_update(owner, name, pr_number, body=body)
        return response.parsed_data.model_dump()

    async def set_pull_request_draft_state(
        self,
        repo: str,
        pr_number: int,
        *,
        draft: bool,
    ) -> None:
        pull_request = await self.get_pull_request(repo, pr_number)
        if bool(pull_request.get("draft")) == draft:
            return

        node_id = str(pull_request["node_id"])
        if draft:
            await self._github.async_graphql(
                """
                mutation ConvertPullRequestToDraft($id: ID!) {
                  convertPullRequestToDraft(input: {pullRequestId: $id}) {
                    pullRequest { id }
                  }
                }
                """,
                {"id": node_id},
            )
            return

        await self._github.async_graphql(
            """
            mutation MarkPullRequestReadyForReview($id: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                pullRequest { id }
              }
            }
            """,
            {"id": node_id},
        )

    async def has_branch(self, repo: str, branch: str) -> bool:
        owner, name = repo.split("/", 1)
        try:
            await self._github.rest.git.async_get_ref(owner, name, f"heads/{branch}")
        except RequestFailed as error:
            if error.response.status_code == 404:
                return False
            raise

        return True

    async def get_default_branch(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        repository = await self._github.rest.repos.async_get(owner, name)
        return repository.parsed_data.default_branch

    async def update_pull_request_base(self, repo: str, pr_number: int, *, base: str) -> None:
        owner, name = repo.split("/", 1)
        await self._github.rest.pulls.async_update(owner, name, pr_number, base=base)

    async def create_branch(self, repo: str, branch: str, *, base: str | None = None) -> None:
        """Create ``branch`` at the head of ``base``, or of the default branch."""
        owner, name = repo.split("/", 1)
        if not base:
            base = await self.get_default_branch(repo)
        head = await self._github.rest.git.async_get_ref(owner, name, f"heads/{base}")
        await self._github.rest.git.async_create_ref(
            owner, name, ref=f"refs/heads/{branch}", sha=head.parsed_data.object_.sha
        )

    async def delete_branch(self, repo: str, branch: str) -> bool:
        owner, name = repo.split("/", 1)
        try:
            await self._github.rest.git.async_delete_ref(owner, name, f"heads/{branch}")
        except RequestFailed as error:
            if error.response.status_code == 404:
                return False
            raise

        return True

    async def request_pull_request_reviewers(
        self,
        repo: str,
        pr_number: int,
        reviewers: list[str],
    ) -> None:
        owner, name = repo.split("/", 1)
        await self._github.rest.pulls.async_request_reviewers(
            owner,
            name,
            pr_number,
            reviewers=reviewers,
        )

    async def create_review(
        self,
        repo: str,
        pr_number: int,
        *,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        body: str,
        comments: list[ReviewComment] | None = None,
    ) -> dict[str, Any]:
        owner, name = repo.split("/", 1)
        raw_comments = _build_review_comments(comments) if comments else None
        try:
            kwargs: dict[str, Any] = {
                "event": event,
                "body": body,
            }
            if raw_comments:
                kwargs["comments"] = raw_comments
            response = await self._github.rest.pulls.async_create_review(
                owner,
                name,
                pr_number,
                **kwargs,
            )
            return response.parsed_data.model_dump()
        except RequestFailed as error:
            if error.response.status_code != 422 or not raw_comments:
                raise
            logger.warning(
                "Review with inline comments rejected (422) for %s#%d; retrying body-only.",
                repo,
                pr_number,
            )
            fallback_body = _fold_comments_into_body(body, comments or [])
            response = await self._github.rest.pulls.async_create_review(
                owner,
                name,
                pr_number,
                event=event,
                body=fallback_body,  # type: ignore[arg-type]
            )
            return response.parsed_data.model_dump()

    async def post_check_run(
        self,
        repo: str,
        *,
        head_sha: str,
        name: str,
        status: str,
        conclusion: str = "",
        title: str,
        summary: str,
        annotations: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Create the check run ``name`` on ``head_sha``, or update the one this App
        already posted there. An annotation is GitHub's shape: ``path``, ``start_line``,
        ``end_line``, ``annotation_level``, ``message``."""
        owner, repo_name = repo.split("/", 1)
        checks = self._github.rest.checks
        posted = await checks.async_list_for_ref(
            owner, repo_name, head_sha, check_name=name, app_id=int(self._app_id)
        )
        check_run_id = next((run.id for run in posted.parsed_data.check_runs), None)
        limit = _ANNOTATIONS_PER_REQUEST
        output = {"title": title, "summary": summary}
        body: dict[str, Any] = {
            "name": name,
            "status": status,
            "output": {**output, "annotations": list(annotations[:limit])},
        }
        if conclusion:
            body["conclusion"] = conclusion
        if check_run_id:
            await checks.async_update(owner, repo_name, check_run_id, data=body)
        else:
            response = await checks.async_create(
                owner, repo_name, data={**body, "head_sha": head_sha}
            )
            check_run_id = response.parsed_data.id
        for start in range(limit, len(annotations), limit):
            body["output"] = {**output, "annotations": list(annotations[start : start + limit])}
            await checks.async_update(owner, repo_name, check_run_id, data=body)

    async def dispatch_workflow(
        self, repo: str, workflow: str, *, ref: str, inputs: dict[str, Any] | None = None
    ) -> int:
        """Start ``workflow``, a file name such as ``deploy.yml``, on ``ref`` with
        ``inputs``, and return the id of the run it started."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.actions.async_create_workflow_dispatch(
            owner, name, workflow, ref=ref, inputs=inputs or UNSET
        )
        return response.parsed_data.workflow_run_id

    async def get_workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
        """The run ``run_id``: its ``status``, its ``conclusion`` once ``status`` is
        ``completed``, and its ``html_url``."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.actions.async_get_workflow_run(owner, name, run_id)
        return response.parsed_data.model_dump()

    async def can_write(self, repo: str, username: str) -> bool:
        """Whether ``username`` can write to ``repo``. GitHub reports maintain as write."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.repos.async_get_collaborator_permission_level(
            owner, name, username
        )
        return response.parsed_data.permission in ("admin", "write")

    async def list_check_suites(self, repo: str, ref: str) -> list[dict[str, Any]]:
        """The check suites on ``ref``: one per Actions workflow run, or per other app."""
        owner, name = repo.split("/", 1)
        suites = self._github.rest.paginate(
            self._github.rest.checks.async_list_suites_for_ref,
            map_func=lambda response: response.parsed_data.check_suites,
            owner=owner,
            repo=name,
            ref=ref,
            per_page=100,
        )
        return [suite.model_dump() async for suite in suites]

    async def list_commit_statuses(self, repo: str, ref: str) -> list[dict[str, Any]]:
        """The latest status of each context on ``ref``."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.repos.async_get_combined_status_for_ref(
            owner, name, ref, per_page=100
        )
        return [status.model_dump() for status in response.parsed_data.statuses]

    async def get_file_content(
        self,
        repo: str,
        path: str,
        *,
        ref: str | None = None,
    ) -> str | None:
        owner, name = repo.split("/", 1)
        try:
            response = await self._github.rest.repos.async_get_content(
                owner,
                name,
                path,
                ref=ref or "",
            )
        except RequestFailed as error:
            if error.response.status_code == 404:
                return
            raise
        data = response.parsed_data
        content = getattr(data, "content", None) or ""
        return base64.b64decode(content).decode()

    async def download_tarball(self, repo: str) -> bytes:
        """Gzipped tarball of ``repo``'s default branch."""
        owner, name = repo.split("/", 1)
        response = await self._github.rest.repos.async_download_tarball_archive(owner, name, "")
        return response.content


async def download_public_tarball(repo: str) -> bytes:
    """Gzipped tarball of a *public* repo's default branch.
    Fetched anonymously — no App installation, unlike ``GitHubClient``, which acts
    with an installation's token and so reaches only the repos the App is on."""
    owner, name = repo.split("/", 1)
    async with GitHub(timeout=DEFAULT_TIMEOUT, auto_retry=RETRY_SERVER_ERROR) as github:
        response = await github.rest.repos.async_download_tarball_archive(owner, name, "")
        return response.content


def _build_review_comments(comments: list[ReviewComment]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for comment in comments:
        entry: dict[str, Any] = {"path": comment.path, "line": comment.line, "body": comment.body}
        if comment.start_line:
            entry["start_line"] = comment.start_line
        result.append(entry)
    return result


def _fold_comments_into_body(body: str, comments: list[ReviewComment]) -> str:
    lines = [body, "", "---", ""]
    for comment in comments:
        location = f"`{comment.path}:{comment.line}`"
        if comment.start_line:
            location = f"`{comment.path}:{comment.start_line}-{comment.line}`"
        lines.append(f"**{location}**\n{comment.body}\n")
    return "\n".join(lines)
