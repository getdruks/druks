from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import Depends, Request

from druks.accounts.dependencies import current_account, current_session_account
from druks.accounts.models import Account
from druks.api.dependencies import SessionDep
from druks.chat.bots.service import get_bot_connection
from druks.durable.models import AgentCall, Artifact, Run
from druks.exceptions import ObjectNotFound, PermissionDenied
from druks.files.models import FileRecord
from druks.notifications.models import Notification
from druks.registry import app_registry, get_app, get_workflow

if TYPE_CHECKING:
    from druks.apps import App


def require_app_access(app: "type[App]") -> Callable[..., Awaitable[None]]:
    """The dependency that checks access to one app. The caller must be able to use the app."""

    async def require_access(account: Account = Depends(current_account)) -> None:
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )

    return require_access


async def require_named_app_access(app: str, account: Account = Depends(current_account)) -> None:
    """The caller must be able to use the app that the ``app`` query parameter names. Druks
    answers 404 for an app that is not installed."""
    try:
        named = get_app(app)
    except KeyError as error:
        raise ObjectNotFound("app", {"name": app}) from error
    if not await account.can_use_app(named):
        raise PermissionDenied(
            f"{account.username} cannot use the app {app}. Ask an admin to add "
            f"{account.username} to a team that lists the app."
        )


# Each dependency that follows finds the app in the request path. A path parameter in
# the signature of a dependency replaces the parameter of the route, and the description
# that a tool shows for it. Druks answers 404 for anything of an app that is not
# installed (removed, or `enabled = false`).


async def require_run_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the run's app. The route answers an unknown run."""
    run = await session.get(Run, request.path_params["run"])
    if run:
        workflow = run.workflow
        if not workflow:
            raise ObjectNotFound("run", {"id": run.id})
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )


async def require_call_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app of the call's run. The route answers an unknown
    call."""
    call = await session.get(AgentCall, request.path_params["call"])
    if call:
        workflow = call.run.workflow
        if not workflow:
            raise ObjectNotFound("agent call", {"id": call.id})
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )


async def require_artifact_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app of the artifact's call. The route answers an
    unknown artifact."""
    artifact = await session.get(Artifact, request.path_params["artifact_id"])
    if artifact:
        call = await AgentCall.get(session, artifact.agent_call_id)
        workflow = call.run.workflow
        if not workflow:
            raise ObjectNotFound("artifact", {"id": artifact.id})
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )


async def require_file_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the file's app. The route answers an unknown
    file."""
    record = await session.get(FileRecord, request.path_params["file_id"])
    if record:
        try:
            app = get_app(record.app)
        except KeyError as error:
            raise ObjectNotFound("file", {"id": record.id}) from error
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )


async def require_notification_access(
    request: Request, session: SessionDep, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app of the notification's run. The route
    answers an unknown notification. A notification with no run passes. When the app of
    the run is not installed, Druks answers as for an unknown notification."""
    notification = await session.get(Notification, request.path_params["notification_id"])
    if notification and notification.run_id:
        run = await session.get(Run, notification.run_id)
        workflow = run.workflow
        app = app_registry.get(workflow.app) if workflow else None
        if not app:
            raise ObjectNotFound("notification", {"id": notification.id})
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )


async def require_schedule_access(
    request: Request, account: Account = Depends(current_account)
) -> None:
    """The caller must be able to use the app that declares the schedule. The route answers an
    unknown kind."""
    workflow = get_workflow(request.path_params["kind"])
    if workflow:
        app = get_app(workflow.app)
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )


async def require_pairing_access(
    request: Request, session: SessionDep, account: Account = Depends(current_session_account)
) -> None:
    """The caller must be able to use the app of the number. The route answers an unknown
    number."""
    connection = await get_bot_connection(session, request.path_params["connection_id"])
    if connection:
        try:
            app = get_app(connection.identity["app"])
        except KeyError as error:
            raise ObjectNotFound("number", {"id": connection.id}) from error
        if not await account.can_use_app(app):
            raise PermissionDenied(
                f"{account.username} cannot use the app {app.name}. Ask an admin to add "
                f"{account.username} to a team that lists the app."
            )
