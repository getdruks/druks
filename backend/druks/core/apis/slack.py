from collections import deque

import aiohttp
from slack_sdk.http_retry import HttpResponse
from slack_sdk.http_retry.async_handler import AsyncRetryHandler
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from druks.redis import get_client

# Slack's app creation page. With new_app=1 and manifest_json, it shows the app of that
# manifest, ready to create.
SLACK_CREATE_APP_URL = "https://api.slack.com/apps"
# The authority of a Slack grant: the workspace that a Slack user id belongs to.
SLACK_AUTHORITY = "https://slack.com/{team_id}"
# With this scope, a reply carries its agent's own name and picture.
SLACK_CUSTOMIZE_SCOPE = "chat:write.customize"
# What the bot does in the workspace. The manifest asks for these.
SLACK_BOT_SCOPES = (
    "chat:write",
    SLACK_CUSTOMIZE_SCOPE,
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "mpim:history",
    "users:read",
    "files:read",
    "files:write",
    "reactions:write",
)
USER_NAME_TTL_SECONDS = 24 * 60 * 60


class NewFileRetryHandler(AsyncRetryHandler):
    """Sends a message again when Slack refuses a file that the message names. Slack scans a
    file after its upload ends. Until the scan ends, Slack answers invalid_blocks with
    "invalid slack file" for an image block that names the file, and with "invalid file type"
    for the image of a card. The handler keeps the defaults of the SDK: one more post, 0.5 to
    1.5 seconds after the refusal."""

    async def _can_retry_async(self, *, response: HttpResponse | None = None, **_: object) -> bool:
        # The SDK gives no response for a failed connection, and no body for one that is not JSON.
        body = (response and response.body) or {}
        return body.get("error") == "invalid_blocks" and any(
            refusal.startswith(("invalid slack file", "invalid file type"))
            for refusal in body.get("errors", [])
        )


class SlackClient(AsyncWebClient):
    """Slack's Web API under one token, the bot's or a person's. Druks adds its markdown
    message, a thread's tail, the channels of the token's user, the names it keeps, and a
    file's bytes. The client also sends a message again when Slack still scans a file that
    the message names."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.retry_handlers.append(NewFileRetryHandler())

    async def post_markdown(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str = "",
        username: str | None = None,
        icon_url: str | None = None,
    ) -> AsyncSlackResponse:
        """Post ``text`` as one markdown block, with the same text as the fallback, in a
        thread when ``thread_ts`` names one. A user id as the channel opens the bot's DM
        with that person. ``username`` and ``icon_url`` replace the app's name and
        picture. Slack takes them only with the ``chat:write.customize`` scope."""
        return await self.chat_postMessage(
            channel=channel,
            text=text,
            blocks=[{"type": "markdown", "text": text}],
            thread_ts=thread_ts or None,
            username=username,
            icon_url=icon_url,
        )

    async def list_replies(self, channel: str, thread_ts: str, *, limit: int) -> list[dict]:
        """The thread's newest ``limit`` messages, oldest first. Slack pages a thread from
        its oldest message, so the method reads every page and keeps the tail."""
        messages: deque[dict] = deque(maxlen=limit)
        async for page in await self.conversations_replies(
            channel=channel, ts=thread_ts, limit=limit
        ):
            messages.extend(page["messages"])
        return list(messages)

    async def list_member_channels(self, *, user: str = "") -> list[dict]:
        """The public and private channels that this token's user is a member of. With
        ``user``, only the ones that this Slack user is a member of too."""
        channels = []
        async for page in await self.users_conversations(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=1000,
            user=user or None,
        ):
            channels.extend(page["channels"])
        return channels

    async def get_user_name(self, user_id: str) -> str:
        """A user's name, kept in Redis for a day."""
        key = f"slack:user:{user_id}:name"
        if name := await get_client().get(key):
            return name.decode()
        user = (await self.users_info(user=user_id))["user"]
        name = user["profile"]["display_name"] or user["real_name"]
        await get_client().set(key, name, ex=USER_NAME_TTL_SECONDS)
        return name

    async def download(self, url: str) -> bytes:
        """A file's bytes from its private Slack URL."""
        async with (
            aiohttp.ClientSession() as http,
            http.get(url, headers={"Authorization": f"Bearer {self.token}"}) as response,
        ):
            response.raise_for_status()
            return await response.read()
