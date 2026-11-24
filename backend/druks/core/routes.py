import json
import logging
from urllib.parse import quote, urlencode

import httpx
from drukbox_sdk.exceptions import SandboxAPIError, SandboxUnavailableError, SandboxValidationError
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from druks.accounts.dependencies import current_account, current_admin_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.api.exceptions import agent_error_responses
from druks.core.apis.github import GITHUB_API_URL
from druks.core.apis.slack import SLACK_CREATE_APP_URL
from druks.core.services import GithubApp, Slack
from druks.core.templates import render_page
from druks.durable.models import AgentCall, Run
from druks.redis import is_resumable, request_check
from druks.registry import get_workflow, service_registry
from druks.sandbox.client import sandbox_client
from druks.sandbox.exceptions import HostGone
from druks.settings import get_settings

from .exceptions import (
    BoxGone,
    CheckAlreadyStarted,
    CheckBoxRefused,
    CheckBoxUnavailable,
    CheckNotDeclared,
    CheckRejected,
    RunHasNoRepo,
    RunNotFound,
    RunNotRunning,
)

# Mounted by the loader under /api/core, like any app's routes.
router = APIRouter(prefix="/services", tags=["services"])


# Declared before the GitHub routes, whose {slug} segment also matches "slack".
@router.get("/slack/manifest")
async def create_slack_app() -> RedirectResponse:
    query = urlencode({"new_app": 1, "manifest_json": json.dumps(Slack.get_manifest())})
    return RedirectResponse(f"{SLACK_CREATE_APP_URL}?{query}")


def get_manifest_service(slug: str) -> type[GithubApp]:
    service = service_registry.get(slug)
    if service and service.manifest:
        return service
    raise HTTPException(status_code=404, detail=f"No service {slug!r} with a manifest.")


@router.get("/{slug}/manifest", response_class=HTMLResponse)
async def create_github_app(slug: str) -> HTMLResponse:
    # GitHub only accepts a manifest via form POST, so this page carries it across.
    service = get_manifest_service(slug)
    settings = get_settings()
    endpoint = settings.urls.endpoint.rstrip("/")
    if not endpoint:
        raise HTTPException(
            status_code=409,
            detail="Set urls.endpoint to the base URL the operator's browser reaches druks "
            "at, to create the GitHub App.",
        )
    manifest = service.get_manifest(endpoint=endpoint, webhook_base=settings.urls.webhook_base)
    return render_page("github_manifest.html", manifest_json=json.dumps(manifest))


@router.get(
    "/{slug}/manifest/callback",
    response_class=HTMLResponse,
    dependencies=[Depends(current_admin_account)],
)
async def github_manifest_callback(slug: str, code: str = "") -> HTMLResponse:
    service = get_manifest_service(slug)
    if not code:
        raise HTTPException(status_code=400, detail="Missing code in the GitHub redirect.")
    async with httpx.AsyncClient() as client:
        converted = await client.post(
            f"{GITHUB_API_URL}/app-manifests/{quote(code, safe='')}/conversions",
            headers={"Accept": "application/vnd.github+json"},
        )
    if converted.is_error:
        # Codes are single-use and expire within the hour; the only fix is a
        # fresh pass through the create page.
        raise HTTPException(
            status_code=400,
            detail="GitHub rejected the creation code — restart from Create App.",
        )
    app = converted.json()
    row = await service.connect(
        {
            "app_id": str(app["id"]),
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "private_key": app["pem"],
            "webhook_secret": app["webhook_secret"],
        }
    )
    return render_page(
        "github_manifest_callback.html",
        slug=row.identity["slug"],
        # Druks' own install door carries the state the install's sign-in returns with.
        install_url=f"/api/oauth/{slug}/install",
        service=slug,
    )


logger = logging.getLogger(__name__)

checks_router = APIRouter(prefix="/checks", tags=["checks"])


class CheckStarted(BaseModel):
    started: bool = Field(description="True: the check runs while you are not working.")
    message: str


class CheckResult(BaseModel):
    exit_code: int | None = Field(description="Null when the command timed out.")
    timed_out: bool
    output: str = Field(description="The end of stdout and stderr, in the order they came.")
    duration_seconds: float


@checks_router.post(
    "/run",
    operation_id="run_check",
    tags=["agent"],
    responses=agent_error_responses(
        RunNotFound("01JEXAMPLE"),
        RunNotRunning("01JEXAMPLE"),
        RunHasNoRepo("01JEXAMPLE"),
        BoxGone("01JEXAMPLE"),
        CheckNotDeclared("make test", ("make check",)),
        CheckRejected("the provider stops its VMs after 0:45:00"),
        CheckBoxRefused("the clone failed"),
        CheckBoxUnavailable("Sandbox service transport failed"),
    ),
)
async def run_check(
    session: SessionDep,
    account: Account = Depends(current_account),
    run_id: str = Body(..., embed=True, description="The id of the run you work in."),
    command: str = Body(
        ..., embed=True, description="One of the run's declared checks, exactly as declared."
    ),
    timeout_seconds: int = Body(
        600, embed=True, ge=1, le=3600, description="The command is stopped after this long."
    ),
) -> CheckResult | CheckStarted:
    """Run one of this run's declared checks on a separate box. The box has the repo at the
    commit your work started from, with your uncommitted and untracked changes on top, and
    is deleted when the check ends. A failing check still answers with its exit code and the
    end of its output. Your own box is not touched. Either the answer is the result, or the
    answer says the check started: then end your turn, and the result is your next message."""
    run = await session.get(Run, run_id)
    if not run or run.account_id != account.id:
        raise RunNotFound(run_id)
    call = await AgentCall.lookup_running(session, run.id)
    workflow = get_workflow(run.kind)
    if not call or not workflow:
        raise RunNotRunning(run_id)
    subject = None
    if workflow.subject and run.subject_id:
        subject = await workflow.subject.get_or_none(id=run.subject_id)
    if not subject:
        # The subject is read live, and a deleted one resolves to nothing. It comes before
        # the declared checks, which are read from the subject.
        raise RunHasNoRepo(run_id)
    declared = await workflow.workspace_class.get_checks(subject)
    if command not in declared:
        raise CheckNotDeclared(command, declared)
    if await is_resumable(run.id):
        if not await request_check(run.id, command=command, timeout_seconds=timeout_seconds):
            raise CheckAlreadyStarted(command)
        return CheckStarted(
            started=True,
            message=(
                "The check starts when your turn ends. End your turn now, with the shortest "
                "answer that fills the required fields. The answer to this turn is not used. "
                "The result of the check is your next message."
            ),
        )
    # A workspace that declares checks is a repo workspace: its subject names the repo.
    workspace = workflow.workspace_class(subject)
    host_id = call.sandbox_host_id
    # The check can run for an hour: the session holds no transaction across it.
    await session.commit()
    try:
        async with sandbox_client.attach(host_id=host_id) as host:
            workspace.host = host
            result = await workspace.run_check(
                session,
                run_id=run.id,
                account_id=run.account_id,
                command=command,
                timeout_seconds=timeout_seconds,
            )
    except HostGone as error:
        raise BoxGone(run_id) from error
    except SandboxValidationError as error:
        raise CheckRejected(str(error)) from error
    except SandboxUnavailableError as error:
        # Drukbox is down or at its host limit. The SDK's capacity error is one of these.
        raise CheckBoxUnavailable(str(error)) from error
    except SandboxAPIError as error:
        raise CheckBoxRefused(str(error)) from error
    return CheckResult(
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        output=result.output,
        duration_seconds=result.duration_seconds,
    )
