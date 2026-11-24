import asyncio
import contextvars

import httpx

from druks.accounts.constants import ACCOUNT_SCOPE_KEY


async def call_route(method: str, path: str, *, json: dict, account_id: str) -> httpx.Response:
    """Call a route of the API in this process as the account, and return its answer. The
    call carries no key: the request names the account in its ASGI scope, which only this
    process can write."""
    # The API loads the modules that call this, so its app comes in at the call.
    from druks.api.server import app  # noqa: PLC0415

    async def call_as_account(scope, receive, send):
        await app({**scope, ACCOUNT_SCOPE_KEY: account_id}, receive, send)

    # An error in the route comes back as the route's own answer.
    transport = httpx.ASGITransport(app=call_as_account, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://druks") as client:
        # The route runs in a context of its own, as a request from outside does.
        return await asyncio.create_task(
            client.request(method, path, json=json), context=contextvars.Context()
        )
