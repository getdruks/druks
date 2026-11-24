import json

import redis.asyncio as aioredis

from druks.settings import get_settings

_client: aioredis.Redis | None = None


def get_client() -> aioredis.Redis:
    global _client
    if not _client:
        _client = aioredis.from_url(get_settings().redis_url.get_secret_value())
    return _client


async def close_client() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


# A run's live check state. Both keys are transient: a run that ends or a process that dies
# leaves a key that expires, and nothing reads it as a record of what happened.
_RESUMABLE_KEY = "druks:run:{run_id}:resumable"
_CHECK_REQUEST_KEY = "druks:run:{run_id}:check-request"
CHECK_REQUEST_TTL_SECONDS = 6 * 3600


async def set_resumable(run_id: str, *, ttl_seconds: int) -> None:
    """Mark the run's agent call as one that can end its turn for a check and be resumed.
    A request that a crashed attempt left behind is not this call's, so it goes."""
    await get_client().delete(_CHECK_REQUEST_KEY.format(run_id=run_id))
    await get_client().set(_RESUMABLE_KEY.format(run_id=run_id), "1", ex=ttl_seconds)


async def clear_resumable(run_id: str) -> None:
    await get_client().delete(
        _RESUMABLE_KEY.format(run_id=run_id), _CHECK_REQUEST_KEY.format(run_id=run_id)
    )


async def is_resumable(run_id: str) -> bool:
    return bool(await get_client().exists(_RESUMABLE_KEY.format(run_id=run_id)))


async def request_check(run_id: str, *, command: str, timeout_seconds: int) -> bool:
    """Record the check the agent asked for. False when one is already waiting."""
    return bool(
        await get_client().set(
            _CHECK_REQUEST_KEY.format(run_id=run_id),
            json.dumps({"command": command, "timeout_seconds": timeout_seconds}),
            nx=True,
            ex=CHECK_REQUEST_TTL_SECONDS,
        )
    )


async def take_check_request(run_id: str) -> dict | None:
    """The check the agent asked for, removed so that it runs once."""
    raw = await get_client().getdel(_CHECK_REQUEST_KEY.format(run_id=run_id))
    return json.loads(raw) if raw else None
