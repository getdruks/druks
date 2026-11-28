import functools
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from druks.accounts.context import current_account_id
from druks.accounts.models import Account
from druks.core.services import Github
from druks.durable.engine import step_session
from druks.durable.exceptions import WorkflowError
from druks.mcp import models as mcp_models
from druks.mcp.constants import TOKEN_ENV_PREFIX
from druks.mcp.enums import Credential, IdentityMode, Toolkit
from druks.mcp.exceptions import MissingGrantError, MissingTokenError
from druks.mcp.helpers import get_bearer_token_env_var
from druks.mcp.inbound import get_druks_account_token
from druks.registry import service_registry
from druks.sandbox import repo as checkout
from druks.sandbox.client import provisioning_key, sandbox_client
from druks.sandbox.constants import MAX_AGENT_TIMEOUT_SECONDS, WORKFLOW_HOST_LEASE_SECONDS
from druks.sandbox.datastructures import (
    AgentResult,
    McpServer,
    SandboxMcpServer,
    SandboxSecret,
)
from druks.sandbox.exceptions import ExecFailed
from druks.sandbox.layout import get_repo_root, get_work_root
from druks.sandbox.models import SecretRef

if TYPE_CHECKING:
    from druks.harnesses.base import Harness
    from druks.harnesses.datastructures import Manifest
    from druks.sandbox.host import Host

logger = logging.getLogger(__name__)

# Each script runs in the checkout at $0. A snapshot is a commit of the working copy: what is
# committed, and every file that git does not ignore. A scratch index makes it, so the
# agent's own index stays as it is. The bundle at $1 holds what the copy, which has the
# snapshot $2, does not have yet.
_BUNDLE_SNAPSHOT = """
set -e
cd "$0"
index=$(mktemp)
cp "$(git rev-parse --git-path index)" "$index"
GIT_INDEX_FILE="$index" git add --all
tree=$(GIT_INDEX_FILE="$index" git write-tree)
rm -f "$index"
held=""
if [ -n "$2" ] && git cat-file -e "$2^{commit}" 2>/dev/null; then held=$2; fi
snapshot=$(git -c user.name=druks -c user.email=druks@localhost \\
    commit-tree "$tree" ${held:+-p "$held"} -m snapshot)
git update-ref refs/druks/snapshot "$snapshot"
git bundle create -q "$1" refs/druks/snapshot ${held:+"^$held"}
"""
# The snapshot that the copy holds, or nothing for a copy that holds none.
_HELD_SNAPSHOT = 'git init -q "$0" && git -C "$0" rev-parse -q --verify refs/druks/synced || true'
# The copy becomes the snapshot of the bundle at $1. Files that git ignores, such as
# installed dependencies, stay.
_CHECK_OUT_SNAPSHOT = """
set -e
cd "$0"
git fetch -q "$1" refs/druks/snapshot
git read-tree -u --reset FETCH_HEAD
git update-ref refs/druks/synced FETCH_HEAD
"""
# What the command changed in the copy, as a patch at $1. Prints the size of the patch.
_WRITE_CHANGES = """
set -e
cd "$0"
git add --all
git diff --cached --binary refs/druks/synced > "$1"
wc -c < "$1"
"""
_APPLY_CHANGES = 'git -C "$0" apply --whitespace=nowarn "$1"'


async def _run_script(host: "Host", script: str, *arguments: str) -> str:
    """The output of a shell script on the host. A script that fails raises."""
    result = await host.exec(["sh", "-c", script, *arguments], timeout=600.0)
    if result.returncode:
        raise ExecFailed(
            f"exit={result.returncode} stderr={result.stderr.strip()}",
            exit_code=result.returncode,
        )
    return result.stdout


class Workspace:
    """The place an agent works: a box, what must be in it before the agent
    starts, and how the agent is run there. The classmethods answer what the box
    is from the subject alone, before it exists; ``prepare()`` readies it."""

    def __init__(self, subject: Any) -> None:
        # What the run is about; None for a workflow about nothing.
        self.subject = subject
        self._host: Host | None = None

    @classmethod
    async def get_skills(cls, subject: Any) -> tuple[str, ...]:
        # The skills the box gets beside the enabled catalog; the manifest records them.
        return ()

    async def prepare(self) -> None:
        """Put on the box what the agent needs: an upload, a clone, a wrapper.
        Reads and calls work here, each a unit of work of its own."""

    @property
    def host(self) -> "Host":
        # Core sets it once the box is up; author code never does.
        if not self._host:
            raise WorkflowError("the box is not up yet: act on it in prepare()")
        return self._host

    @host.setter
    def host(self, host: "Host") -> None:
        self._host = host

    @property
    def host_id(self) -> str:
        return self.host.id

    def get_agent_run_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        # Override to add what the run needs on this workspace, such as add_dirs.
        return kwargs

    def get_env(self) -> dict[str, str]:
        """Environment variables every agent call on this workspace gets."""
        return {}

    @classmethod
    async def get_mcp_servers(cls, subject: Any) -> tuple[SandboxMcpServer, ...]:
        # Override to declare this workspace's servers and the vault row each
        # one issues through. Read before the box exists. Base: none.
        return ()

    @classmethod
    async def get_secrets(cls, subject: Any) -> list[SandboxSecret]:
        # The secrets a box of this workspace fetches, beyond its profile's.
        # Read before the box exists, so from the subject alone. Base: none.
        return []

    @classmethod
    async def get_secret_refs(cls, subject: Any) -> list[SecretRef]:
        return [
            SecretRef(
                name=secret.name,
                secret_id=secret.secret_id,
                resources=list(secret.resources),
                host=secret.host,
            )
            for secret in await cls.get_secrets(subject)
        ]

    async def exec(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        """Run the command in the workspace's working directory."""
        return await self.host.exec(
            command, cwd=get_work_root(self.host.ssh_username), timeout=timeout
        )

    async def run_agent(
        self,
        *,
        harness: "Harness",
        manifest: "Manifest",
        timeout: int,
        identity: dict | None,
        include_apps: bool,
        **kwargs: Any,
    ) -> AgentResult:
        run_kwargs = self.get_agent_run_kwargs(extra_env=self.get_env(), **kwargs)
        return await self.host.run_agent(
            harness,
            manifest,
            timeout=timeout,
            identity=identity,
            include_apps=include_apps,
            **run_kwargs,
        )

    @classmethod
    async def get_all_mcp_servers(
        cls,
        session: AsyncSession,
        subject: Any,
        account_id: str | None,
        *,
        skip_unauthenticated: bool = False,
    ) -> tuple[tuple[McpServer, ...], list[SecretRef]]:
        """The MCP servers a box of this workspace reaches, as the harness names
        them, and the secret refs for the box's entries, one per bearer and per
        secret header. The workspace's servers come first and own their names:
        a same-named registry entry is neither resolved nor delivered. A registry
        server that cannot authenticate fails here, before the box, unless each
        account keys or connects it for itself or ``skip_unauthenticated`` leaves
        it out."""
        workspace_servers = await cls.get_mcp_servers(subject)
        workspace_names = {server.name for server in workspace_servers}
        if len(workspace_names) != len(workspace_servers):
            # One config key per name in the emitted harness config — a dupe
            # would break the VM's config parse mid-run.
            raise ValueError(f"duplicate workspace MCP server names: {sorted(workspace_names)}")
        servers = []
        refs = []
        for server in workspace_servers:
            variable = get_bearer_token_env_var(server.name)
            servers.append(
                McpServer(name=server.name, url=server.url, bearer_token_env_var=variable)
            )
            if server.secret_id:
                secret_id = server.secret_id
            else:
                account = await Account.get_for_run(session, account_id)
                row = await get_druks_account_token(
                    session, account.id, server.allowed_tools or Toolkit.ALL
                )
                secret_id = row.id
            refs.append(
                SecretRef(
                    name=variable.lower(),
                    secret_id=secret_id,
                    resources=list(server.resources),
                    host=urlsplit(server.url).hostname,
                )
            )
        owner = await Account.get_secrets_owner(session, account_id)
        run_account = owner.id if owner else None
        accesses = await mcp_models.McpServer.list_access(session, run_account)
        accesses = [access for access in accesses if access.is_enabled]
        if skip_unauthenticated:
            accesses = [access for access in accesses if access.has_token]
        for access in accesses:
            name = access.name
            if name in workspace_names:
                continue
            host = urlsplit(access.url).hostname
            # A pasted service login rides as a header row and a grant as a bearer, each
            # loud when the account has none. The installed app's login rides as a bearer
            # that the service issues for the server. A server without OAuth rides its
            # header rows, loud when it has none.
            bearer_token_env_var = ""
            secret_headers = access.secret_headers
            pasted_login = access.credential == Credential.SERVICE_LOGIN and not (
                await service_registry.get(access.service).get_grant()
            )
            if pasted_login:
                login = await mcp_models.McpServer.store_login(
                    session, name, access.secret, run_account
                )
                secret_headers = {login.header: login}
            elif access.secret:
                bearer_token_env_var = get_bearer_token_env_var(name)
                refs.append(
                    SecretRef(
                        name=bearer_token_env_var.lower(),
                        secret_id=access.secret.id,
                        host=host,
                    )
                )
            elif access.credential != Credential.HEADERS:
                if access.identity_mode == IdentityMode.PER_USER:
                    # The account has connected no grant of its own; its box goes
                    # without, and the call's manifest records the server as
                    # declared but not delivered.
                    continue
                raise MissingGrantError(name, run_account)
            elif not secret_headers:
                if access.identity_mode == IdentityMode.PER_USER:
                    # The account has set no key of its own; its box goes without.
                    continue
                raise MissingTokenError(name)
            env_headers = {}
            for index, (header, secret) in enumerate(secret_headers.items()):
                variable = f"{TOKEN_ENV_PREFIX}{name.upper()}_HEADER_{index}"
                env_headers[header] = variable
                refs.append(SecretRef(name=variable.lower(), secret_id=secret.id, host=host))
            servers.append(
                McpServer(
                    name=name,
                    url=access.url,
                    bearer_token_env_var=bearer_token_env_var,
                    headers=dict(access.headers),
                    env_headers=env_headers,
                )
            )
        return tuple(servers), refs


class RepoWorkspace(Workspace):
    """A VM with the subject's ``repo`` cloned at its ``ref``, the default branch
    when None, re-cloned before every agent call. The box holds a placeholder in
    ``GH_TOKEN``, and Drukbox points git and ``gh`` at it; the Druks issuer
    answers the token of the GitHub identity this workspace names."""

    # The GitHub identity the box's git and gh act as: a connected service.
    github: ClassVar[type[Github]] = Github

    @classmethod
    async def get_secrets(cls, subject: Any) -> list[SandboxSecret]:
        # The identity's vault row and the repo: the whole selection the
        # issuer reads. A service that is not connected fails here, before the box.
        return [
            SandboxSecret(
                name=cls.github.slug,
                secret_id=(await cls.github.get()).id,
                resources=(subject.repo,),
                host=cls.github.host,
            )
        ]

    @property
    def repo_path(self) -> str:
        return get_repo_root(self.host.ssh_username)

    async def clone(self) -> None:
        repo, ref = self.subject.repo, self.subject.ref
        logger.info("cloning %s at %s into %s", repo, ref or "the default branch", self.repo_path)
        ensure = functools.partial(
            checkout.ensure,
            self.host,
            repo_url=f"https://github.com/{repo}",
            ref=ref,
            target_path=self.repo_path,
        )
        try:
            await ensure()
        except ExecFailed as error:
            # GitHub's answer to a token that no longer reaches the repo. The vault and
            # each box's exchange keep that token until it expires, so drop both and
            # try once with a new one.
            if "Repository not found" not in str(error):
                raise
            # The token swap is one unit of work.
            async with step_session() as session:
                await self.github.revoke_tokens(repo)
                secret = await self.github.get()
                await sandbox_client.request_refreshes(session, secret.id)
            try:
                await ensure()
            except ExecFailed as retry_error:
                logger.warning(
                    "checkout of %s on box %s failed again with a new GitHub token: %s",
                    repo,
                    self.host_id,
                    retry_error,
                )
                raise error from retry_error
            logger.warning(
                "checkout of %s on box %s succeeded with a new GitHub token", repo, self.host_id
            )

    async def prepare(self) -> None:
        await self.clone()
        await self.set_git_identity()

    async def exec(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        await self.clone()
        return await self.host.exec(command, cwd=self.repo_path, timeout=timeout)

    async def run_on_copy(self, command: str, *, template: str) -> subprocess.CompletedProcess[str]:
        """Run ``command`` at the root of a copy of this checkout, and bring the files that
        it changed back into this checkout. The copy is on a sandbox from ``template``
        that this box keeps for its commands. That sandbox holds no secret. The copy holds
        what git does not ignore here, and keeps what git ignores there from command to
        command. The process has the output and the error output of the command as one."""
        sandbox = await sandbox_client.provision(
            idempotency_key=provisioning_key(self.host.id, template),
            template=template,
            lease_seconds=WORKFLOW_HOST_LEASE_SECONDS,
        )
        copy = get_repo_root(sandbox.ssh_username)
        bundle = f"{get_work_root(self.host.ssh_username)}/snapshot.bundle"
        copy_bundle = f"{get_work_root(sandbox.ssh_username)}/snapshot.bundle"
        copy_patch = f"{get_work_root(sandbox.ssh_username)}/changes.patch"
        patch = f"{get_work_root(self.host.ssh_username)}/changes.patch"
        async with sandbox, sandbox_client.lease(host_id=sandbox.id):
            with tempfile.TemporaryDirectory() as folder:
                held = await _run_script(sandbox, _HELD_SNAPSHOT, copy)
                await _run_script(self.host, _BUNDLE_SNAPSHOT, self.repo_path, bundle, held.strip())
                await self.host.download(remote=bundle, local=Path(folder) / "bundle")
                await sandbox.upload_file(local=Path(folder) / "bundle", remote=copy_bundle)
                await _run_script(sandbox, _CHECK_OUT_SNAPSHOT, copy, copy_bundle)
                completed = await sandbox.exec(
                    ["sh", "-c", f"exec 2>&1; {command}"],
                    cwd=copy,
                    timeout=MAX_AGENT_TIMEOUT_SECONDS,
                )
                if int(await _run_script(sandbox, _WRITE_CHANGES, copy, copy_patch)):
                    await sandbox.download(remote=copy_patch, local=Path(folder) / "patch")
                    await self.host.upload_file(local=Path(folder) / "patch", remote=patch)
                    await _run_script(self.host, _APPLY_CHANGES, self.repo_path, patch)
        return completed

    async def get_diff(self, *, binary: bool = False) -> str:
        """The checkout's changes since it diverged from the default branch,
        including untracked files.

        `binary` includes the contents of changed binary files so that
        `git apply` can apply the diff to another checkout. By default,
        binary content is omitted to keep the prompt small.
        """
        flags = "--binary " if binary else ""
        # A scratch index, so the agent's own index stays as it is.
        script = (
            'index=$(mktemp) && cp "$(git rev-parse --git-path index)" "$index" && '
            'GIT_INDEX_FILE="$index" git add --intent-to-add --all && '
            f'GIT_INDEX_FILE="$index" git diff {flags}"$(git merge-base HEAD origin/HEAD)"; '
            'status=$?; rm -f "$index"; exit $status'
        )
        result = await self.host.exec(["sh", "-c", script], cwd=self.repo_path, timeout=60.0)
        if result.returncode:
            raise ExecFailed(
                f"failed to read the workspace diff: "
                f"exit={result.returncode} stderr={result.stderr.strip()}",
                exit_code=result.returncode,
            )
        return result.stdout

    async def set_git_identity(self) -> None:
        """Commits in the repo are authored as the operator's bot user, with a
        prepare-commit-msg hook crediting the GitHub user of the dispatcher's
        first sign-in. Druks rewrites both before every agent call, so a reused
        warm host follows the current run's dispatcher. A system dispatch, or a
        dispatcher with no GitHub sign-in, carries no hook and credits nobody."""
        author = await self.github.get_bot_git_author()
        dispatcher = None
        if account_id := current_account_id.get():
            sign_ins = await Github.list_for_account(account_id)
            if sign_ins:
                dispatcher = Github.get_user_git_author(sign_ins[0])
        await checkout.set_git_identity(self.host, author, dispatcher, repo_path=self.repo_path)
