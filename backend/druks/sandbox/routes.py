import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from druks.api.dependencies import SessionDep
from druks.secrets.exceptions import MissingSecretError

from .exceptions import IdentityDenied
from .models import SandboxIdentity, SecretIssue

logger = logging.getLogger(__name__)

# The identity bearer is the one credential here. Dashboard cookies, personal
# access tokens, and the edge identity do not authorize a fetch.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="sandboxIdentity")

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


async def _record_failure(
    session: AsyncSession, identity_id: str, name: str, error: Exception
) -> None:
    # The request rolls back with its error, so the reason commits before the error rises.
    await session.rollback()
    await SecretIssue.record(session, identity_id, name, error=str(error))
    await session.commit()


@router.get("/{identity_id}/{name}", include_in_schema=False)
async def secret(
    session: SessionDep,
    identity_id: str,
    name: str,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> JSONResponse:
    """The value behind a box's placeholder. A 403 carries no reason. A 422 says
    the source has no secret until a person acts, and names the fix. A 503 says the source
    holds nothing valid now, and the exchange retries. An answer to a box that holds
    the secret is recorded, with the reason when the answer is a 422 or a 503."""
    credential = bearer.credentials if bearer else ""
    identity = None
    try:
        identity = await SandboxIdentity.authenticate(session, identity_id, credential, name)
        # The ref is the whole selection: the vault row and the resources.
        # Nothing in the request can pick another.
        ref = identity.get_secret_ref(name)
        value, expires_at = await ref.secret.issue_token(
            tuple(ref.resources), name=ref.name, host_id=identity.host_id or ""
        )
        # The identity can die during the source I/O.
        await SandboxIdentity.authenticate(session, identity_id, credential, name)
    except IdentityDenied:
        raise HTTPException(status_code=403) from None
    except MissingSecretError as exc:
        logger.warning("fetch of %s for identity %s failed: %s", name, identity_id, exc)
        await _record_failure(session, identity_id, name, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — the source failed; the box gets a 503 and the exchange retries
        logger.warning("fetch of %s for identity %s failed: %s", name, identity_id, exc)
        if identity:
            await _record_failure(session, identity_id, name, exc)
        raise HTTPException(status_code=503) from None
    await SecretIssue.record(session, identity_id, name, value=value, expires_at=expires_at)
    return JSONResponse(
        {"value": value, "expires_at": expires_at.isoformat() if expires_at else None},
        headers={"Cache-Control": "no-store"},
    )
