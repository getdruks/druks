import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlparse

import druks.chat.channels.slack.subscribers  # noqa: F401 — the webhook publishes to it
import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import Answer, bind_ambient_session, install_service
from druks import ui
from druks.accounts.models import Account, PersonalAccessToken
from druks.agents.constants import PRIVATE_AGENT_MESSAGE
from druks.agents.models import Agent
from druks.api.dependencies import request_session
from druks.chat import service
from druks.chat.bridge import Bridge
from druks.chat.channels.slack.channel import SlackChannel
from druks.chat.channels.slack.constants import LINK_KEY
from druks.chat.constants import CONVERSATION_HEADER
from druks.chat.dependencies import current_conversation
from druks.chat.enums import ConversationSource, MessageRole
from druks.chat.exceptions import ChannelHasNoThreadsError
from druks.chat.models import Automation, Conversation, Message
from druks.chat.routes import read_file, read_thread, require_tag
from druks.core.apis.slack import SlackClient
from druks.core.services import Slack
from druks.core.webhooks.slack import SlackEvents, SlackInteractivity
from druks.files import File
from druks.mcp.inbound import get_druks_mcp_server
from druks.models import Base
from druks.notifications.models import Notification
from druks.notifications.services import respond_to_notification
from druks.redis import close_client, get_client
from druks.sandbox.exceptions import SandboxUploadError
from druks.sandbox.layout import get_work_root
from druks.secrets.datastructures import Audience
from druks.secrets.models import VaultSecret
from druks.settings import configure_settings, get_settings
from druks.testing import asgi_client, configure_app_for_test, make_settings
from druks.webhooks.router import match_webhook
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry import BackoffRetryIntervalCalculator

SLACK_AUDIENCE = Audience.service("slack")
AUTHORITY = "https://slack.com/T1"
ANA = "U100"
BOT = "U900"


async def install_slack(druks_db, **grant) -> VaultSecret:
    """The Slack install of the Acme workspace, with ``grant`` such as its scopes."""
    bind_ambient_session(druks_db)
    row = await install_service(
        "slack",
        access_token="xoxb-1",
        identity={
            "client_id": "c1",
            "team": "Acme",
            "team_id": "T1",
            "bot_name": "druks",
            "bot_user_id": BOT,
        },
        secrets={"client_secret": "cs", "signing_secret": "ss"},
        **grant,
    )
    await druks_db.commit()
    return row


@pytest.fixture
async def card(druks_db):
    return await install_slack(druks_db)


@pytest.fixture
def slack(monkeypatch):
    """Answer Slack's Web API and its upload host locally. Record each call's method and
    arguments, and record an upload of bytes as POST, with its address and bytes. Slack
    names each uploaded file F1."""
    calls = []

    async def api_call(client, api_method, *, params=None, json=None, **request):
        calls.append((api_method, json or params or {}))
        return Answer(
            ok=True,
            channel="D1",
            ts=f"{len(calls)}.0",
            bot={"id": "B500", "name": "Support form"},
            file_id="F1",
            upload_url="https://files.slack.example/upload/F1",
            files=[{"id": "F1"}],
        )

    async def upload_file(client, *, url, data, **request):
        calls.append(("POST", {"url": url, "data": data}))
        return SimpleNamespace(status=200, body="")

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    # The SDK sends the bytes of an upload to the address that Slack gives, not by api_call.
    monkeypatch.setattr(SlackClient, "_upload_file", upload_file)
    return calls


@pytest.fixture
def delivery(monkeypatch):
    started = AsyncMock()
    monkeypatch.setattr("druks.chat.channels.slack.channel.DBOS.start_workflow_async", started)
    return started


@pytest.fixture
def endpoint(monkeypatch):
    urls = SimpleNamespace(endpoint="https://druks.example", webhook_base="https://druks.example")
    settings = SimpleNamespace(urls=urls)
    monkeypatch.setattr("druks.chat.channels.slack.channel.get_settings", lambda: settings)


async def link_person(session, account, user_id=ANA):
    grant = await VaultSecret.connect(
        session,
        SLACK_AUDIENCE,
        account_id=account.id,
        refresh_token="xoxe-1",
        scopes=["users:read"],
        identity={"authority": AUTHORITY, "subject": user_id, "name": "ana"},
    )
    await session.commit()
    return grant


def message_event(
    user=ANA, text="Read my runs", ts="1.0", channel="D1", channel_type="im", **event
):
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel": channel,
            "channel_type": channel_type,
            "user": user,
            "text": text,
            "ts": ts,
            **event,
        },
    }


def join_event(user):
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event": {
            "type": "member_joined_channel",
            "user": user,
            "channel": "C1",
            "channel_type": "C",
            "team": "T1",
        },
    }


async def receive(data):
    webhook = SlackEvents(None, {}, None)
    webhook.raw_body = json.dumps(data).encode()
    await webhook.on_event_callback()


async def test_a_linked_persons_message_reaches_their_own_conversation(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(message_event())

    [conversation] = await Conversation.list_for_account(druks_db, ana)
    assert (conversation.source, conversation.connection_id, conversation.user_id) == (
        ConversationSource.SLACK,
        card.id,
        ANA,
    )
    await druks_db.refresh(conversation, ["messages"])
    assert [(message.body, message.source_id) for message in conversation.messages] == [
        ("Read my runs", "D1:1.0")
    ]
    delivery.assert_awaited_once()
    assert not slack


@pytest.mark.parametrize(
    "event", [{"user": BOT}, {"subtype": "message_changed"}, {"user_team": "T2"}]
)
async def test_the_webhook_drops_what_is_not_for_the_bot(card, druks_db, slack, delivery, event):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(message_event(**event))

    assert not await Conversation.list_for_account(druks_db, ana)
    assert not slack
    delivery.assert_not_awaited()


async def test_an_unlinked_person_gets_a_link_that_answers_their_message_once_they_connect(
    card, druks_db, druks_client, slack, delivery, endpoint
):
    await receive(message_event())

    [_, (method, sent)] = slack
    assert (method, sent["channel"]) == ("chat.postMessage", ANA)
    link = sent["text"].partition("https://druks.example")[2]
    assert not await Conversation.list_for_connection(druks_db, card.id)

    not_linked = await druks_client.get(link, follow_redirects=False)
    assert not_linked.status_code == 307
    assert not_linked.headers["location"].startswith("/api/oauth/slack/connect?next=")

    operator = await Account.get_or_create(druks_db, "op@example.com")
    await link_person(druks_db, operator)
    linked = await druks_client.get(link, follow_redirects=False)
    [conversation] = await Conversation.list_for_account(druks_db, operator)
    assert linked.headers["location"] == f"/chat/{conversation.id}"
    await druks_db.refresh(conversation, ["messages"])
    assert [message.source_id for message in conversation.messages] == ["D1:1.0"]
    delivery.assert_awaited_once()
    assert (await druks_client.get(link, follow_redirects=False)).status_code == 410


async def test_a_forwarded_link_links_only_the_person_who_opens_it(
    card, druks_db, druks_client, slack, delivery, endpoint
):
    await receive(message_event())
    [_, (_, sent)] = slack
    link = sent["text"].partition("https://druks.example")[2]
    operator = await Account.get_or_create(druks_db, "op@example.com")
    await link_person(druks_db, operator, user_id="U200")

    response = await druks_client.get(f"{link}?has_connected=true", follow_redirects=False)

    assert response.headers["location"] == "/chat"
    assert not await Conversation.list_for_connection(druks_db, card.id)
    delivery.assert_not_awaited()
    assert await get_client().get(LINK_KEY.format(token=link.rpartition("/")[2]))


async def test_a_reply_posts_markdown_in_pieces_and_records_its_slack_id(card, druks_db, slack):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await thread_conversation(druks_db, card, ana, thread_id="")
    reply = await conversation.create_message(druks_db, "a" * 12_001, role=MessageRole.ASSISTANT)

    await SlackChannel.send_reply(druks_db, conversation, reply)

    assert [
        (method, sent["channel"], len(sent["text"]), sent["blocks"][0]["type"])
        for method, sent in slack
    ] == [("chat.postMessage", ANA, 12_000, "markdown"), ("chat.postMessage", ANA, 1, "markdown")]
    assert reply.source_id == "D1:1.0"


async def reply_in_slack(session, card, account, agent_id, *, blocks=None):
    conversation = await thread_conversation(
        session, card, account, thread_id="C1:1.0", agent_id=agent_id
    )
    reply = await conversation.create_message(
        session, "Done", role=MessageRole.ASSISTANT, blocks=blocks
    )
    await SlackChannel.send_reply(session, conversation, reply)


FACTS = [
    ui.Facts([ui.Fact("Owner", "Ana"), ui.Fact("Builds", 12)], title="Project").model_dump(
        mode="json", by_alias=True
    )
]


@pytest.mark.usefixtures("tmp_data_dir")
@pytest.mark.parametrize("blocks", [None, FACTS], ids=["text", "blocks"])
async def test_an_agents_reply_shows_its_name_and_picture_and_the_bots_shows_neither(
    druks_db, slack, blocks
):
    # The workspace approved the app's custom names.
    card = await install_slack(druks_db, scopes=("chat:write", "chat:write.customize"))
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    agent = await create_agent(druks_db, ana, "Security engineer")
    picture = await File.create(
        name="face.png", content_type="image/png", content=b"png", app="core", uploaded_by=ana.id
    )
    await agent.replace(
        druks_db,
        editor=ana,
        owner_id=ana.id,
        name=agent.name,
        description=agent.description,
        instructions=agent.instructions,
        skills=agent.skills,
        harness=agent.harness,
        model=agent.model,
        is_private=agent.is_private,
        channel_id=None,
        picture=picture.id,
    )

    await reply_in_slack(druks_db, card, ana, agent.id, blocks=blocks)
    await reply_in_slack(druks_db, card, ana, None, blocks=blocks)

    assert [(sent.get("username"), sent.get("icon_url")) for _, sent in slack] == [
        ("Security engineer", f"{get_settings().urls.webhook_base}/media/{picture.id}"),
        (None, None),
    ]


@pytest.mark.parametrize(
    ("body", "text"),
    [("Let me look.Here are the facts.", ["Here are the facts."]), ("Let me look.", [])],
    ids=["text-then-blocks", "blocks-only"],
)
async def test_a_reply_with_blocks_goes_out_as_one_message_of_its_text_and_slack_s_blocks(
    card, druks_db, slack, monkeypatch, body, text
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await thread_conversation(druks_db, card, ana, thread_id="C1:1.0")
    message = await conversation.create_message(druks_db, "Show me the project")
    assert await message.mark_delivered(druks_db)
    await druks_db.commit()
    await get_client().set(service.blocks_key(conversation.id, message.id), json.dumps(FACTS))
    tool_calls = [{"toolCallId": "show-1", "textOffset": 12}]
    monkeypatch.setattr(Bridge, "reply", AsyncMock(return_value=(body, tool_calls)))
    monkeypatch.setattr(Conversation, "is_held", AsyncMock(return_value=False))

    await service.finish_turn(
        druks_db, conversation, message, Bridge(None), {"status": "replied", "archivePath": ""}
    )

    [(method, sent)] = slack
    assert (method, sent["channel"], sent["thread_ts"]) == ("chat.postMessage", "C1", "1.0")
    assert [block["type"] for block in sent["blocks"]] == [*("markdown" for _ in text), "section"]
    assert sent["text"] == "\n\n".join([*text, "### Project\n\n- **Owner**: Ana\n- **Builds**: 12"])
    reply = await Message.get_for_source_id(druks_db, "D1:1.0")
    assert (reply.reply_to, reply.blocks) == (message.id, FACTS)
    # The blocks answer the person, so Druks does not ask the agent again.
    assert not await conversation.get_unanswered_message(druks_db)


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_reply_shows_a_druks_image_of_its_blocks_as_the_slack_file_it_uploads(
    card, druks_db, slack
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    rack = await File.create(
        name="rack.png",
        content_type="image/png",
        content=b"png bytes",
        app="chat",
        uploaded_by=None,
    )
    image = ui.Image(url=rack.url, alternative_text="Rack 3.")

    await reply_in_slack(
        druks_db, card, ana, None, blocks=[image.model_dump(mode="json", by_alias=True)]
    )

    *_, (method, post) = slack
    assert method == "chat.postMessage"
    assert post["blocks"] == [
        {"type": "markdown", "text": "Done"},
        {"type": "image", "slack_file": {"id": "F1"}, "alt_text": "Rack 3."},
    ]


async def test_an_agents_reply_comes_from_the_app_until_the_workspace_approves_names(
    card, druks_db, slack
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    agent = await create_agent(druks_db, ana, "Security engineer")

    await reply_in_slack(druks_db, card, ana, agent.id)

    assert [("username" in sent, "icon_url" in sent) for _, sent in slack] == [(False, False)]


async def test_delivery_reacts_with_eyes_to_each_message_from_slack(
    card, druks_db, monkeypatch, caplog
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await thread_conversation(druks_db, card, ana, thread_id="")
    # A Slack message with two files is two Druks messages.
    messages = [
        await conversation.create_message(druks_db, "Hi", source_id=source_id)
        for source_id in ("D1:1.0", "D1:2.0", "D1:2.0:F2")
    ]
    await conversation.create_message(druks_db, "[Internal: ...]", is_internal=True)
    reactions = []

    async def api_call(client, api_method, *, params=None, **request):
        reaction = (api_method, params["channel"], params["timestamp"], params["name"])
        if reaction in reactions:
            raise SlackApiError("", {"ok": False, "error": "already_reacted"})
        reactions.append(reaction)
        if params["timestamp"] == "1.0":
            raise SlackApiError("", {"ok": False, "error": "ratelimited"})
        return {"ok": True}

    monkeypatch.setattr(SlackClient, "api_call", api_call)

    with caplog.at_level(logging.WARNING):
        await service.acknowledge_pending(druks_db, conversation)

    assert reactions == [
        ("reactions.add", "D1", "1.0", "eyes"),
        ("reactions.add", "D1", "2.0", "eyes"),
    ]
    failures = [record.args[0] for record in caplog.records if record.name == service.__name__]
    assert failures == [messages[0].id]


async def test_connect_slack_asks_for_user_scopes_and_keeps_the_token_from_authed_user(
    card, druks_db, tmp_path, monkeypatch
):
    def token_endpoint(request: httpx.Request) -> httpx.Response:
        exchange = dict(parse_qsl(request.content.decode()))
        assert exchange["client_id"] == "c1"
        authed_user = {"id": ANA, "scope": "users:read", "access_token": "xoxp-1"}
        return httpx.Response(
            200, json={"ok": True, "access_token": "xoxb-1", "authed_user": authed_user}
        )

    async def auth_test(client):
        assert client.token == "xoxp-1"
        return {"ok": True, "team_id": "T1", "user_id": ANA, "user": "ana", "team": "Acme"}

    monkeypatch.setattr(
        "druks.services.oauth._http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint)),
    )
    monkeypatch.setattr(SlackClient, "auth_test", auth_test)
    await close_client()
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        consent = client.get("/api/oauth/slack/connect", follow_redirects=False).headers["location"]
        query = dict(parse_qsl(urlparse(consent).query))
        assert query["user_scope"] == "users:read"
        assert "scope" not in query
        page = client.get("/api/oauth/callback", params={"state": query["state"], "code": "c-1"})
        assert page.status_code == 200

    operator = await Account.get_or_create(druks_db, "op@example.com")
    assert await VaultSecret.get_owner_id(druks_db, AUTHORITY, ANA) == operator.id
    [grant] = await VaultSecret.list_account_connections(druks_db, SLACK_AUDIENCE, operator.id)
    assert (grant.identity, grant.scopes) == (
        {"authority": AUTHORITY, "subject": ANA, "name": "ana"},
        ["users:read"],
    )
    client = await Slack.get_oauth_client()
    assert await client.get_access_token(druks_db, connection=grant) == ("xoxp-1", None)


def test_create_slack_app_opens_slack_with_the_whole_manifest(tmp_path):
    settings = make_settings(tmp_path, urls={"endpoint": "https://druks.example"})
    with TestClient(configure_app_for_test(settings=settings)) as client:
        response = client.get("/api/core/services/slack/manifest", follow_redirects=False)
        manifest = Slack.get_manifest()

    location = urlparse(response.headers["location"])
    query = dict(parse_qsl(location.query))
    assert (location.netloc, location.path, query["new_app"]) == ("api.slack.com", "/apps", "1")
    assert json.loads(query["manifest_json"]) == manifest


def test_the_manifest_keeps_the_webhook_host_when_a_box_reaches_druks_elsewhere(tmp_path):
    settings = make_settings(
        tmp_path,
        urls={"webhook_host": "hooks.druks.example", "mcp_url": "http://100.64.0.5/mcp"},
    )
    configure_settings(settings)

    manifest = Slack.get_manifest()

    assert get_druks_mcp_server(allowed_tools=()).url == "http://100.64.0.5/mcp"
    assert manifest["settings"]["event_subscriptions"]["request_url"] == (
        "https://hooks.druks.example/_external/slack/events/"
    )
    assert manifest["settings"]["interactivity"]["request_url"] == (
        "https://hooks.druks.example/_external/slack/interactivity/"
    )


def test_the_manifest_sends_button_clicks_to_the_interactivity_webhook():
    interactivity = Slack.get_manifest()["settings"]["interactivity"]

    path = urlparse(interactivity["request_url"]).path.removeprefix("/_external/")
    assert interactivity["is_enabled"]
    assert match_webhook(path)[0] is SlackInteractivity


def room_event(user=ANA, text=f"<@{BOT}> read my runs", ts="10.0", channel="C1", **event):
    return message_event(user, text, ts, channel=channel, channel_type="channel", **event)


def thread_reads(monkeypatch, messages):
    """Answer ``conversations.replies`` and ``users.info`` locally, and record each read."""
    names = {ANA: "Ana", BOT: "Druks", "U200": "Ben"}
    reads = []

    async def api_call(client, api_method, *, params=None, json=None, **request):
        reads.append(api_method)
        if api_method == "conversations.replies":
            return Answer(ok=True, messages=messages)
        user = params["user"]
        profile = {"display_name": ""}
        return Answer(ok=True, user={"id": user, "real_name": names[user], "profile": profile})

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return reads


async def thread_conversation(session, card, account, *, thread_id="C1:10.0", agent_id=None):
    return await Conversation.get_or_create_for_user(
        session,
        card,
        account.id,
        source=ConversationSource.SLACK,
        user_id=ANA,
        user_name="",
        user_phone="",
        thread_id=thread_id,
        agent_id=agent_id,
    )


def conversation_api(tmp_path, path: str, route, **options) -> FastAPI:
    """An API that serves ``route`` at ``path`` to a caller that names its conversation."""
    configure_app_for_test(settings=make_settings(tmp_path), authenticated=False)
    api = FastAPI(dependencies=[Depends(request_session)])
    api.add_api_route(path, route, dependencies=[Depends(current_conversation)], **options)
    return api


async def test_each_person_who_tags_the_bot_gets_their_own_conversation_for_the_thread(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")

    await receive(room_event())
    await receive(room_event(user="U200", ts="11.0", thread_ts="10.0"))

    conversations = await Conversation.list_for_connection(druks_db, card.id)
    assert sorted((chat.account_id, chat.user_id, chat.thread_id) for chat in conversations) == (
        sorted([(ana.id, ANA, "C1:10.0"), (ben.id, "U200", "C1:10.0")])
    )
    assert delivery.await_count == 2
    reply = await conversations[0].create_message(druks_db, "Done.", role=MessageRole.ASSISTANT)
    await SlackChannel.send_reply(druks_db, conversations[0], reply)
    [(method, sent)] = slack
    assert (method, sent["channel"], sent["thread_ts"]) == ("chat.postMessage", "C1", "10.0")


async def test_a_message_with_no_tag_in_a_room_reaches_no_agent(card, druks_db, slack, delivery):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    await receive(room_event())

    await receive(room_event(text="more", ts="12.0", thread_ts="10.0"))
    await receive(room_event(user="U200", text="me too", ts="13.0", thread_ts="10.0"))
    await receive(room_event(text="a new topic", ts="14.0"))

    [conversation] = await Conversation.list_for_connection(druks_db, card.id)
    await druks_db.refresh(conversation, ["messages"])
    assert [message.body for message in conversation.messages] == ["read my runs"]
    delivery.assert_awaited_once()
    assert not slack


async def create_agent(session, owner, name, *, is_private=False):
    return await Agent.create(
        session,
        owner=owner,
        name=name,
        description="",
        instructions="",
        skills=[],
        harness="claude",
        model="anthropic/claude-opus-4-7",
        is_private=is_private,
    )


async def bind_channel(session, agent, channel_id):
    """Give the agent the channel as its owner, an admin, does on the agent's page."""
    await agent.replace(
        session,
        editor=await Account.get(session, id=agent.owner_id),
        owner_id=agent.owner_id,
        name=agent.name,
        description=agent.description,
        instructions=agent.instructions,
        skills=agent.skills,
        harness=agent.harness,
        model=agent.model,
        is_private=agent.is_private,
        channel_id=channel_id,
        picture=None,
    )
    await session.commit()


async def test_a_tag_reaches_the_agent_whose_longest_name_follows_it(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    await create_agent(druks_db, ana, "Security")
    engineer = await create_agent(druks_db, ana, "Security engineer")
    inci = await create_agent(druks_db, ana, "İnci")
    await druks_db.commit()

    await receive(room_event(text=f"hey <@{BOT}> security engineer check this PR"))
    await receive(room_event(text=f"Security engineer <@{BOT}> check this PR", ts="20.0"))
    await receive(room_event(text=f"<@{BOT}> securityscan please", ts="30.0"))
    await receive(room_event(text=f"<@{BOT}> İnci check this PR", ts="40.0"))
    await receive(room_event(text=f"<@{BOT}>, Security engineer check this PR", ts="50.0"))
    await receive(room_event(text=f"<@{BOT}>: Security engineer check this PR", ts="60.0"))

    conversations = await Conversation.list_for_connection(druks_db, card.id)
    assert sorted((chat.thread_id, chat.agent_id or "") for chat in conversations) == [
        ("C1:10.0", engineer.id),
        ("C1:20.0", ""),
        ("C1:30.0", ""),
        ("C1:40.0", inci.id),
        ("C1:50.0", engineer.id),
        ("C1:60.0", engineer.id),
    ]


async def test_a_direct_message_reaches_an_agent_only_when_it_tags_the_bot(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    engineer = await create_agent(druks_db, ana, "Security engineer")
    await druks_db.commit()

    await receive(message_event(text="Security engineer check this PR"))
    await receive(message_event(text=f"<@{BOT}> Security engineer check this PR", ts="2.0"))

    conversations = await Conversation.list_for_connection(druks_db, card.id)
    assert sorted(chat.agent_id or "" for chat in conversations) == ["", engineer.id]


async def list_bodies(session, card):
    """The bodies of each conversation on the connection, by its person and its agent."""
    bodies = {}
    for chat in await Conversation.list_for_connection(session, card.id):
        await session.refresh(chat, ["messages"])
        bodies[(chat.user_id, chat.agent_id)] = [message.body for message in chat.messages]
    return bodies


async def test_the_last_agent_named_in_a_thread_owns_it_for_everyone(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    writer = await create_agent(druks_db, ana, "Writer")
    designer = await create_agent(druks_db, ana, "Designer")
    await druks_db.commit()

    await receive(room_event(text=f"<@{BOT}> Writer draft the notes"))
    await receive(room_event(text=f"<@{BOT}> make it shorter", ts="11.0", thread_ts="10.0"))
    await receive(room_event(text=f"<@{BOT}> Designer make a logo", ts="12.0", thread_ts="10.0"))
    await receive(
        room_event(user="U200", text=f"<@{BOT}> make it blue", ts="13.0", thread_ts="10.0")
    )
    await receive(room_event(text=f"<@{BOT}> Writer add the logo", ts="14.0", thread_ts="10.0"))
    await receive(room_event(user="U200", text=f"<@{BOT}> and a date", ts="15.0", thread_ts="10.0"))

    assert await list_bodies(druks_db, card) == {
        (ANA, writer.id): ["Writer draft the notes", "make it shorter", "Writer add the logo"],
        (ANA, designer.id): ["Designer make a logo"],
        ("U200", designer.id): ["make it blue"],
        ("U200", writer.id): ["and a date"],
    }


async def test_an_agents_late_reply_does_not_take_the_thread_back(card, druks_db, slack, delivery):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    engineer = await create_agent(druks_db, ana, "Security engineer")
    manager = await create_agent(druks_db, ana, "Release manager")
    await druks_db.commit()
    await receive(room_event(text=f"<@{BOT}> Security engineer check this PR"))
    await receive(
        room_event(text=f"<@{BOT}> Release manager can it ship?", ts="11.0", thread_ts="10.0")
    )
    [checking] = [
        chat
        for chat in await Conversation.list_for_connection(druks_db, card.id)
        if chat.agent_id == engineer.id
    ]
    late_reply = await checking.create_message(
        druks_db, "Looks safe.", role=MessageRole.ASSISTANT, source_id="C1:99.0"
    )
    late_reply.created_at = Base.utc_now() + timedelta(minutes=5)
    report = await checking.create_message(druks_db, "Run r1 finished.", is_internal=True)
    report.created_at = Base.utc_now() + timedelta(minutes=5)
    await druks_db.commit()

    await receive(room_event(text=f"<@{BOT}> and today?", ts="12.0", thread_ts="10.0"))

    [shipping] = [
        chat
        for chat in await Conversation.list_for_connection(druks_db, card.id)
        if chat.agent_id == manager.id
    ]
    await druks_db.refresh(shipping, ["messages"])
    assert [message.body for message in shipping.messages] == [
        "Release manager can it ship?",
        "and today?",
    ]


async def test_a_click_on_an_agents_question_does_not_take_the_thread_back(
    card, druks_db, slack, delivery, monkeypatch
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    writer = await create_agent(druks_db, ana, "Writer")
    designer = await create_agent(druks_db, ana, "Designer")
    await druks_db.commit()
    await receive(room_event(text=f"<@{BOT}> Writer draft the notes"))
    [drafting] = await Conversation.list_for_connection(druks_db, card.id)
    question = await Notification.create(
        druks_db,
        conversation_id=drafting.id,
        reason="question.asked",
        body="Which tone?",
        question_offset=0,
        actions=[{"id": "1", "label": "Formal"}],
    )
    await receive(
        room_event(user="U200", text=f"<@{BOT}> Designer make a logo", ts="11.0", thread_ts="10.0")
    )
    monkeypatch.setattr(
        "druks.notifications.services.DBOS", SimpleNamespace(start_workflow_async=AsyncMock())
    )

    await respond_to_notification(
        druks_db, question.correlation_token, {"control": "1"}, user_id=ANA, account_id=None
    )
    await receive(
        room_event(user="U200", text=f"<@{BOT}> make it blue", ts="12.0", thread_ts="10.0")
    )

    assert await list_bodies(druks_db, card) == {
        (ANA, writer.id): ["Writer draft the notes", "Formal"],
        ("U200", designer.id): ["Designer make a logo", "make it blue"],
    }


async def test_a_person_who_turns_the_tag_off_reaches_the_threads_agent_with_no_tag(
    card, druks_db, slack, delivery, tmp_path
):
    api = conversation_api(tmp_path, "/thread/tag", require_tag, methods=["PUT"], status_code=204)
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    writer = await create_agent(druks_db, ana, "Writer")
    designer = await create_agent(druks_db, ana, "Designer")
    await receive(room_event(text=f"<@{BOT}> Writer draft the notes"))
    [drafting] = await Conversation.list_for_connection(druks_db, card.id)
    direct = await thread_conversation(druks_db, card, ana, thread_id="")
    _, token = await PersonalAccessToken.create(druks_db, account_id=ana.id, name="chat")
    await druks_db.commit()
    headers = {"Authorization": f"Bearer {token}", CONVERSATION_HEADER: drafting.id}

    async with asgi_client(api) as client:
        await receive(room_event(text="too early", ts="11.0", thread_ts="10.0"))
        optional = await client.put("/thread/tag", json={"is_required": False}, headers=headers)
        await receive(room_event(text="make it shorter", ts="12.0", thread_ts="10.0"))
        await receive(
            room_event(
                user="U200", text=f"<@{BOT}> Designer make a logo", ts="13.0", thread_ts="10.0"
            )
        )
        # The thread moved to Designer. Ana's choice stays, and it is hers alone.
        await receive(room_event(text="Writer make it blue", ts="14.0", thread_ts="10.0"))
        await receive(room_event(user="U200", text="and bigger", ts="15.0", thread_ts="10.0"))
        [drawing] = [
            chat
            for chat in await Conversation.list_for_account(druks_db, ana)
            if chat.agent_id == designer.id
        ]
        required = await client.put(
            "/thread/tag",
            json={"is_required": True},
            headers={**headers, CONVERSATION_HEADER: drawing.id},
        )
        await receive(room_event(text="and round", ts="16.0", thread_ts="10.0"))
        no_thread = await client.put(
            "/thread/tag",
            json={"is_required": False},
            headers={**headers, CONVERSATION_HEADER: direct.id},
        )

    assert (optional.status_code, required.status_code, no_thread.status_code) == (204, 204, 409)
    assert await list_bodies(druks_db, card) == {
        (ANA, writer.id): ["Writer draft the notes", "make it shorter"],
        (ANA, designer.id): ["Writer make it blue"],
        ("U200", designer.id): ["Designer make a logo"],
        (ANA, None): [],
    }


async def test_an_automations_tag_does_not_take_the_thread_from_its_agent(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    writer = await create_agent(druks_db, ana, "Writer")
    druks_db.add(
        Automation(
            connection_id=card.id,
            source_id="B500",
            name="Support form",
            is_accepted=True,
            first_message=bot_event()["event"],
        )
    )
    await druks_db.commit()
    await receive(room_event(text=f"<@{BOT}> Writer draft the notes"))
    await receive(bot_event(ts="11.0", thread_ts="10.0"))

    await receive(room_event(text=f"<@{BOT}> make it shorter", ts="12.0", thread_ts="10.0"))

    [drafting] = [
        chat
        for chat in await Conversation.list_for_connection(druks_db, card.id)
        if chat.agent_id == writer.id
    ]
    await druks_db.refresh(drafting, ["messages"])
    assert [message.body for message in drafting.messages] == [
        "Writer draft the notes",
        "make it shorter",
    ]


async def test_a_tag_naming_no_agent_in_a_channel_reaches_the_channels_agent(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    engineer = await create_agent(druks_db, ana, "Security engineer")
    writer = await create_agent(druks_db, ana, "Writer")
    await bind_channel(druks_db, engineer, "C1")

    await receive(room_event(text=f"<@{BOT}> check this PR"))
    await receive(room_event(text=f"<@{BOT}> Writer draft the notes", ts="20.0"))
    await receive(room_event(text=f"<@{BOT}> make it shorter", ts="21.0", thread_ts="20.0"))
    await receive(room_event(text=f"<@{BOT}> check this PR", ts="30.0", channel="C2"))

    conversations = await Conversation.list_for_connection(druks_db, card.id)
    assert sorted((chat.thread_id, chat.agent_id or "") for chat in conversations) == [
        ("C1:10.0", engineer.id),
        ("C1:20.0", writer.id),
        ("C2:30.0", ""),
    ]


async def test_a_channel_without_its_agent_returns_to_normal_chat(card, druks_db, slack, delivery):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    engineer = await create_agent(druks_db, ana, "Security engineer")
    await bind_channel(druks_db, engineer, "C1")
    await bind_channel(druks_db, engineer, None)

    await receive(room_event(text=f"<@{BOT}> check this PR"))

    [conversation] = await Conversation.list_for_connection(druks_db, card.id)
    assert not conversation.agent_id


async def test_a_tag_naming_someone_elses_private_agent_is_refused_in_the_thread(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ben, user_id="U200")
    await create_agent(druks_db, ana, "Security engineer", is_private=True)
    await druks_db.commit()

    await receive(room_event(user="U200", text=f"<@{BOT}> Security engineer check this PR"))

    assert not await Conversation.list_for_connection(druks_db, card.id)
    delivery.assert_not_awaited()
    [(method, sent)] = slack
    assert (method, sent["channel"], sent["thread_ts"]) == ("chat.postMessage", "C1", "10.0")
    assert PRIVATE_AGENT_MESSAGE in json.dumps(sent)


async def test_a_tag_naming_no_agent_is_refused_when_the_threads_agent_is_private(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    await create_agent(druks_db, ana, "Security engineer", is_private=True)
    await druks_db.commit()
    await receive(room_event(text=f"<@{BOT}> Security engineer check this PR"))

    await receive(room_event(user="U200", text=f"<@{BOT}> me too", ts="11.0", thread_ts="10.0"))

    assert not await Conversation.list_for_account(druks_db, ben)
    delivery.assert_awaited_once()
    [(method, sent)] = slack
    assert (method, sent["channel"], sent["thread_ts"]) == ("chat.postMessage", "C1", "10.0")
    assert PRIVATE_AGENT_MESSAGE in json.dumps(sent)


async def test_an_untagged_reply_for_a_private_agent_is_dropped_with_no_refusal(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ana)
    await link_person(druks_db, ben, user_id="U200")
    writer = await create_agent(druks_db, ana, "Writer")
    helper = await create_agent(druks_db, ben, "Helper", is_private=True)
    await druks_db.commit()
    await receive(room_event(text=f"<@{BOT}> Writer draft the notes"))
    [drafting] = await Conversation.list_for_connection(druks_db, card.id)
    await drafting.set_tag_required(druks_db, False)
    await druks_db.commit()
    await receive(
        room_event(user="U200", text=f"<@{BOT}> Helper check this", ts="11.0", thread_ts="10.0")
    )

    await receive(room_event(text="thanks Ben", ts="12.0", thread_ts="10.0"))

    assert not slack
    assert await list_bodies(druks_db, card) == {
        (ANA, writer.id): ["Writer draft the notes"],
        ("U200", helper.id): ["Helper check this"],
    }


async def test_the_agent_asks_for_a_tag_only_from_a_person_who_must_tag_it(card, druks_db):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await thread_conversation(druks_db, card, ana)
    await druks_db.refresh(conversation, ["account"])

    _, required_prompt, _ = await service.get_agent(druks_db, conversation)
    await conversation.set_tag_required(druks_db, False)
    _, optional_prompt, _ = await service.get_agent(druks_db, conversation)

    assert "ask them to tag you in the answer" in required_prompt
    assert "ask them to tag you in the answer" not in optional_prompt
    assert "do not ask them for one" in optional_prompt


async def test_a_held_message_reaches_the_threads_agent_once_the_person_connects(
    card, druks_db, druks_client, slack, delivery, endpoint
):
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await link_person(druks_db, ben, user_id="U200")
    writer = await create_agent(druks_db, ben, "Writer")
    await druks_db.commit()
    await receive(room_event(user="U200", text=f"<@{BOT}> Writer draft the notes"))
    await receive(room_event(text=f"<@{BOT}> make it shorter", ts="11.0", thread_ts="10.0"))
    [_, (_, direct_message), _] = slack
    link = direct_message["text"].partition("https://druks.example")[2]
    operator = await Account.get_or_create(druks_db, "op@example.com")
    await link_person(druks_db, operator)

    linked = await druks_client.get(link, follow_redirects=False)

    [conversation] = await Conversation.list_for_account(druks_db, operator)
    assert linked.headers["location"] == f"/chat/{conversation.id}"
    await druks_db.refresh(conversation, ["messages"])
    assert conversation.agent_id == writer.id
    assert [message.body for message in conversation.messages] == ["make it shorter"]


async def test_a_held_message_for_a_private_agent_is_refused_once_the_person_connects(
    card, druks_db, druks_client, slack, delivery, endpoint
):
    ben = await Account.get_or_create(druks_db, "ben@example.com")
    await create_agent(druks_db, ben, "Writer", is_private=True)
    await druks_db.commit()
    await receive(room_event(text=f"<@{BOT}> Writer draft the notes"))
    [_, (_, direct_message), _] = slack
    link = direct_message["text"].partition("https://druks.example")[2]
    operator = await Account.get_or_create(druks_db, "op@example.com")
    await link_person(druks_db, operator)

    linked = await druks_client.get(link, follow_redirects=False)

    assert linked.headers["location"] == "/chat"
    assert not await Conversation.list_for_connection(druks_db, card.id)
    delivery.assert_not_awaited()
    (method, refusal) = slack[-1]
    assert (method, refusal["channel"], refusal["thread_ts"]) == ("chat.postMessage", "C1", "10.0")
    assert PRIVATE_AGENT_MESSAGE in json.dumps(refusal)
    assert (await druks_client.get(link, follow_redirects=False)).status_code == 410


async def test_an_unlinked_person_who_tags_the_bot_gets_the_link_by_direct_message(
    card, druks_db, slack, delivery, endpoint
):
    await receive(room_event(ts="10.0"))
    await receive(room_event(text="hello?", ts="11.0", thread_ts="10.0"))

    [(_, welcome), (_, direct_message), (_, reply)] = slack
    assert welcome["channel"] == direct_message["channel"] == ANA
    assert "/api/chat/services/slack/link/" in direct_message["text"]
    assert (reply["channel"], reply["thread_ts"]) == ("C1", "10.0")
    assert f"<@{ANA}>" in reply["text"] and "/link/" not in reply["text"]
    delivery.assert_not_awaited()


def bot_event(user="U500", **event):
    """A message from another app's bot, or with no user, from a workflow."""
    data = room_event(user, bot_id="B500")
    data["event"].update(event)
    if not user:
        del data["event"]["user"]
        data["event"]["subtype"] = "bot_message"
    return data


async def test_an_automations_first_tag_waits_for_an_admin_and_the_thread_hears_where(
    card, druks_db, slack, delivery, endpoint
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")

    await receive(bot_event())
    await receive(bot_event(ts="11.0"))

    [automation] = await Automation.list_for_connection(druks_db, card.id)
    assert (automation.source_id, automation.name, automation.is_accepted) == (
        "B500",
        "Support form",
        False,
    )
    assert automation.first_message["ts"] == "10.0"
    assert not await Conversation.list_for_account(druks_db, ana)
    delivery.assert_not_awaited()
    [(_, lookup), (_, reply)] = slack
    assert lookup["bot"] == "B500"
    assert (reply["channel"], reply["thread_ts"]) == ("C1", "10.0")
    assert "https://druks.example/apps/chat/settings/channels" in reply["text"]


@pytest.mark.parametrize(("user", "conversation_user"), [("U500", "U500"), (None, "B500")])
async def test_an_admin_accepts_an_automation_and_its_tags_reach_the_default_account(
    card, druks_db, druks_client, slack, delivery, endpoint, user, conversation_user
):
    admin = await Account.get_or_create(druks_db, "op@example.com")
    await receive(bot_event(user))
    [automation] = await Automation.list_for_connection(druks_db, card.id)

    accepted = await druks_client.post(
        f"/api/chat/services/slack/automations/{automation.id}/accept"
    )
    await receive(bot_event(user, ts="11.0"))

    assert accepted.json() == {"id": automation.id, "name": "Support form", "isAccepted": True}
    conversations = await Conversation.list_for_account(druks_db, admin)
    assert {(conversation.user_id, conversation.thread_id) for conversation in conversations} == {
        (conversation_user, "C1:10.0"),
        (conversation_user, "C1:11.0"),
    }
    assert delivery.await_count == 2


async def test_an_accepted_automation_reaches_the_bot_only_by_a_tag_in_a_room(
    card, druks_db, slack, delivery
):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    druks_db.add(
        Automation(
            connection_id=card.id,
            source_id="B500",
            name="Support form",
            is_accepted=True,
            first_message=bot_event()["event"],
        )
    )
    await druks_db.flush()

    await receive(bot_event(text="read my runs", ts="11.0"))
    await receive(bot_event(channel="D1", channel_type="im", ts="12.0"))
    await receive(bot_event(ts="13.0"))

    [conversation] = await Conversation.list_for_account(druks_db, ana)
    assert conversation.thread_id == "C1:13.0"
    delivery.assert_awaited_once()
    assert not slack


async def test_a_person_hears_the_welcome_once_before_their_first_link(
    card, slack, endpoint, monkeypatch
):
    monkeypatch.setattr(
        "druks.chat.channels.slack.channel.iter_apps",
        lambda: [
            SimpleNamespace(description="Builds features.", builtin=False),
            SimpleNamespace(description="The platform.", builtin=True),
        ],
    )

    await receive(message_event())
    await receive(message_event(text="hello?", ts="2.0"))

    [(_, welcome), (_, first_link), (_, second_link)] = slack
    assert welcome["channel"] == ANA
    assert "- Builds features." in welcome["text"]
    assert "The platform." not in welcome["text"]
    assert "https://druks.example/settings/providers" in welcome["text"]
    assert "/link/" in first_link["text"] and "/link/" in second_link["text"]


async def test_the_bot_welcomes_a_room_when_it_joins_and_ignores_other_joins(card, slack, endpoint):
    await receive(join_event(ANA))
    assert not slack

    await receive(join_event(BOT))

    [(method, sent)] = slack
    assert (method, sent["channel"]) == ("chat.postMessage", "C1")
    assert "https://druks.example/chat" in sent["text"]


async def test_the_agent_reads_its_thread_and_knows_who_wrote_what(card, druks_db, monkeypatch):
    """Ben's agent answers in the same thread as the same bot: only the replies that this
    conversation recorded are the agent's own. A reply that asked posted as its
    question's message. The tool lists each file on a message by its id."""
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    reads = thread_reads(
        monkeypatch,
        [
            {"ts": "10.0", "user": ANA, "text": f"<@{BOT}> read my runs"},
            {"ts": "10.1", "user": BOT, "text": "Sure."},
            {"ts": "10.2", "user": "U200", "text": "", "files": [shared_file()]},
            {"ts": "10.3", "user": BOT, "text": "Hi Ben."},
            {"ts": "10.4", "user": BOT, "text": "Which database?"},
        ],
    )
    conversation = await thread_conversation(druks_db, card, ana)
    await conversation.create_message(
        druks_db, "Sure.", role=MessageRole.ASSISTANT, source_id="C1:10.1"
    )
    question = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body="Which database?",
        actions=[{"id": "1", "label": "Postgres"}, {"id": "2", "label": "MySQL"}],
    )
    await question.mark_delivered("C1:10.4")

    thread = await SlackChannel.read_thread(druks_db, conversation)
    await SlackChannel.read_thread(druks_db, conversation)

    assert [
        (message["ts"], message["user_name"], message["is_from_you"], message["is_from_user"])
        for message in thread
    ] == [
        ("10.0", "Ana", False, True),
        ("10.1", "Druks", True, False),
        ("10.2", "Ben", False, False),
        ("10.3", "Druks", False, False),
        ("10.4", "Druks", True, False),
    ]
    assert [message["files"] for message in thread] == [
        [],
        [],
        [{"id": "F1", "name": "plan.pdf", "content_type": "application/pdf"}],
        [],
        [],
    ]
    assert reads.count("users.info") == 3
    direct = await thread_conversation(druks_db, card, ana, thread_id="")
    with pytest.raises(ChannelHasNoThreadsError):
        await SlackChannel.read_thread(druks_db, direct)


async def test_the_agent_reads_an_alerts_attachments_and_blocks_as_text(
    card, druks_db, monkeypatch
):
    """An alert bot posts no text, only an attachment and blocks: the agent reads
    their words, with each link's URL. A person's rich text and Druks's translated
    markdown repeat their text, so the agent reads it once."""
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    link = {"type": "link", "url": "https://sentry.io/1", "text": "Open"}
    thread_reads(
        monkeypatch,
        [
            {
                "ts": "10.0",
                "user": "U200",
                "text": "",
                "attachments": [
                    {
                        "pretext": "New alert",
                        "title": "KeyError: 'user'",
                        "title_link": "https://sentry.io/1",
                        "text": "in read_thread",
                        "fields": [{"title": "Level", "value": "error"}],
                        "blocks": [
                            {"type": "header", "text": {"type": "plain_text", "text": "Prod"}}
                        ],
                        "footer": "Sentry",
                    }
                ],
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "*Seen* 3 times"},
                        "fields": [{"type": "mrkdwn", "text": "Release 1.2"}],
                    },
                    {"type": "divider"},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": "today"}]},
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "See "}, link],
                            }
                        ],
                    },
                ],
            },
            {
                "ts": "10.1",
                "user": ANA,
                "text": "what is this error?",
                "blocks": [
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "what is this error?"}],
                            }
                        ],
                    }
                ],
            },
            {
                "ts": "10.2",
                "user": BOT,
                "text": "A KeyError.",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "A KeyError."}}],
            },
        ],
    )
    conversation = await thread_conversation(druks_db, card, ana)

    thread = await SlackChannel.read_thread(druks_db, conversation)

    assert [message["text"] for message in thread] == [
        "New alert\nKeyError: 'user' (https://sentry.io/1)\nin read_thread\nLevel: error\n"
        "Prod\nSentry\n*Seen* 3 times\nRelease 1.2\ntoday\nSee Open (https://sentry.io/1)",
        "what is this error?",
        "A KeyError.",
    ]


async def test_chat_read_thread_reads_the_callers_own_conversation(
    card, druks_db, tmp_path, monkeypatch
):
    api = conversation_api(tmp_path, "/thread", read_thread, methods=["GET"])
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    thread_reads(monkeypatch, [{"ts": "10.0", "user": ANA, "text": "hi"}])
    conversation = await thread_conversation(druks_db, card, ana)
    direct = await thread_conversation(druks_db, card, ana, thread_id="")
    room = await thread_conversation(druks_db, card, ana, thread_id="C1")
    web = await Conversation.create(druks_db, account_id=ana.id, body="hi")
    _, token = await PersonalAccessToken.create(druks_db, account_id=ana.id, name="chat")
    await druks_db.commit()
    headers = {"Authorization": f"Bearer {token}"}

    async with asgi_client(api) as client:
        outside = await client.get("/thread", headers=headers)
        thread = await client.get(
            "/thread", headers={**headers, CONVERSATION_HEADER: conversation.id}
        )
        no_thread = await client.get("/thread", headers={**headers, CONVERSATION_HEADER: direct.id})
        room_top = await client.get("/thread", headers={**headers, CONVERSATION_HEADER: room.id})
        no_channel = await client.get("/thread", headers={**headers, CONVERSATION_HEADER: web.id})

    statuses = (
        outside.status_code,
        no_thread.status_code,
        room_top.status_code,
        no_channel.status_code,
    )
    assert statuses == (409, 409, 409, 409)
    assert [(message["ts"], message["is_from_user"]) for message in thread.json()] == [
        ("10.0", True)
    ]


async def test_chat_read_file_puts_a_thread_file_in_the_agents_workspace(
    card, druks_db, tmp_path, monkeypatch
):
    """The tool fetches the file from Slack once Slack confirms that it is shared where the
    thread lives. The tool then uploads it to the conversation's folder in the caller's
    sandbox."""
    api = conversation_api(tmp_path, "/thread/files/{file_id}", read_file, methods=["GET"])
    rooms = {"F1": ["C1"], "F2": ["C2"]}

    async def api_call(client, api_method, *, params=None, json=None, **request):
        shared = {**shared_file(params["file"]), "channels": rooms[params["file"]], "groups": []}
        return Answer(ok=True, file=shared)

    uploads = []

    async def upload_file(*, local, remote, mode=0o600):
        uploads.append((local.read_bytes(), remote))

    host = SimpleNamespace(ssh_username="agent", upload_file=upload_file, aclose=AsyncMock())
    monkeypatch.setattr(SlackClient, "api_call", api_call)
    monkeypatch.setattr(SlackClient, "download", AsyncMock(return_value=b"%PDF"))
    monkeypatch.setattr("druks.chat.service.get_running_sandbox", AsyncMock(return_value=host))
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await thread_conversation(druks_db, card, ana)
    _, token = await PersonalAccessToken.create(druks_db, account_id=ana.id, name="chat")
    await druks_db.commit()
    headers = {"Authorization": f"Bearer {token}", CONVERSATION_HEADER: conversation.id}

    async with asgi_client(api) as client:
        served = await client.get("/thread/files/F1", headers=headers)
        elsewhere = await client.get("/thread/files/F2", headers=headers)
        outside = await client.get("/thread/files/F1", headers={"Authorization": f"Bearer {token}"})

    path = f"{get_work_root('agent')}/chat/{conversation.id}/files/F1/plan.pdf"
    assert served.json() == {"path": path, "name": "plan.pdf", "content_type": "application/pdf"}
    assert uploads == [(b"%PDF", path)]
    assert (elsewhere.status_code, outside.status_code) == (404, 409)


async def test_chat_read_file_answers_502_when_the_upload_to_the_sandbox_fails(
    card, druks_db, tmp_path, monkeypatch
):
    """A failed upload answers which file failed and why, not a bare 500."""
    api = conversation_api(tmp_path, "/thread/files/{file_id}", read_file, methods=["GET"])

    async def api_call(client, api_method, *, params=None, json=None, **request):
        shared = {**shared_file(params["file"]), "channels": ["C1"], "groups": []}
        return Answer(ok=True, file=shared)

    error = SandboxUploadError("failed to upload plan.pdf: SFTPConnectionLost: Connection lost")
    upload_file = AsyncMock(side_effect=error)
    host = SimpleNamespace(ssh_username="agent", upload_file=upload_file, aclose=AsyncMock())
    monkeypatch.setattr(SlackClient, "api_call", api_call)
    monkeypatch.setattr(SlackClient, "download", AsyncMock(return_value=b"%PDF"))
    monkeypatch.setattr("druks.chat.service.get_running_sandbox", AsyncMock(return_value=host))
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    conversation = await thread_conversation(druks_db, card, ana)
    _, token = await PersonalAccessToken.create(druks_db, account_id=ana.id, name="chat")
    await druks_db.commit()
    headers = {"Authorization": f"Bearer {token}", CONVERSATION_HEADER: conversation.id}

    async with asgi_client(api) as client:
        response = await client.get("/thread/files/F1", headers=headers)

    assert response.status_code == 502
    assert response.json()["detail"] == str(error)


def shared_file(file_id="F1", name="plan.pdf"):
    return {
        "id": file_id,
        "name": name,
        "mimetype": "application/pdf",
        "url_private_download": f"https://files.slack.com/{file_id}/{name}",
    }


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_file_a_linked_person_sends_is_a_druks_file_on_their_message(
    card, druks_db, slack, delivery, monkeypatch
):
    downloads = []

    async def download(client, url):
        downloads.append((client.token, url))
        return b"%PDF"

    monkeypatch.setattr(SlackClient, "download", download)
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)

    await receive(message_event(text="", subtype="file_share", files=[shared_file()]))
    await receive(
        message_event(
            text="both of these",
            ts="2.0",
            subtype="file_share",
            files=[shared_file("F2", "a.pdf"), shared_file("F3", "b.pdf")],
        ),
    )
    await receive(message_event(text="", ts="3.0"))

    [conversation] = await Conversation.list_for_account(druks_db, ana)
    await druks_db.refresh(conversation, ["messages"])
    assert [
        (message.body, message.source_id, message.file.name) for message in conversation.messages
    ] == [
        ("", "D1:1.0", "plan.pdf"),
        ("both of these", "D1:2.0", "a.pdf"),
        ("", "D1:2.0:F3", "b.pdf"),
    ]
    assert [url for _, url in downloads] == [
        "https://files.slack.com/F1/plan.pdf",
        "https://files.slack.com/F2/a.pdf",
        "https://files.slack.com/F3/b.pdf",
    ]
    assert {token for token, _ in downloads} == {"xoxb-1"}
    assert delivery.await_count == 2


@pytest.mark.parametrize(
    "refusal",
    [
        "invalid slack file [json-pointer:/blocks/0/slack_file.id/slack_file]",
        "invalid file type [json-pointer:/blocks/0/hero_image/slack_file.id/slack_file]",
    ],
    ids=["image", "card"],
)
async def test_the_client_posts_a_message_again_when_slack_still_scans_its_file(
    monkeypatch, refusal
):
    # The test waits no time before the second post.
    monkeypatch.setattr(BackoffRetryIntervalCalculator, "calculate_sleep_duration", lambda *_: 0)
    # The SDK sends a message again inside its HTTP call. A local server thus stands in for Slack.
    answers = iter(
        [
            {
                "ok": False,
                "error": "invalid_blocks",
                "errors": [refusal],
            },
            {"ok": True, "channel": "C1", "ts": "1.0"},
        ]
    )

    async def post_message(request):
        return web.json_response(next(answers))

    api = web.Application()
    api.router.add_post("/chat.postMessage", post_message)
    async with TestServer(api) as server:
        client = SlackClient(token="xoxb-1")
        client.base_url = str(server.make_url("/"))
        posted = await client.chat_postMessage(channel="C1", text="Rack 3.")

    assert posted["ts"] == "1.0"


ENG = {"id": "C1", "name": "eng"}
LEADERSHIP = {"id": "C2", "name": "leadership"}


def member_rooms(monkeypatch, rooms: dict) -> None:
    """Answer the channels that the bot is in under "", and those of a Slack user under
    their id."""

    async def list_member_channels(client, *, user=""):
        return rooms[user]

    monkeypatch.setattr(SlackClient, "list_member_channels", list_member_channels)


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_schedule_with_a_room_reports_in_the_rooms_internal_conversation(
    druks_db, tmp_path, slack, monkeypatch
):
    monkeypatch.setattr("druks.schedules.routes.apply_schedule", AsyncMock())
    member_rooms(monkeypatch, {"": [ENG], ANA: [ENG]})
    card = await install_slack(druks_db, scopes=("chat:write", "chat:write.customize"))
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    await link_person(druks_db, ana)
    agent = await create_agent(druks_db, ana, "Scout")
    chat = await thread_conversation(druks_db, card, ana, thread_id="", agent_id=agent.id)
    await druks_db.commit()
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    headers = {"X-User": "ana@example.com", CONVERSATION_HEADER: chat.id}
    brief = {"tool": "list_schedules", "arguments": {}, "cron": "0 8 * * 1"}

    async with asgi_client(api) as client:
        made = await client.post("/api/schedules", json={**brief, "room": "#eng"}, headers=headers)
        again = await client.post(
            "/api/schedules", json={**brief, "room": "<#C1|eng>"}, headers=headers
        )
        in_chat = await client.post("/api/schedules", json=brief, headers=headers)
        listed = await client.get("/api/chat/conversations", headers=headers)
        written = await client.post(
            f"/api/chat/conversations/{made.json()['conversationId']}/messages",
            json={"body": "What did you post?"},
            headers=headers,
        )
    room = await druks_db.get(Conversation, made.json()["conversationId"])
    reply = await room.create_message(druks_db, "Three merges.", role=MessageRole.ASSISTANT)
    await SlackChannel.send_reply(druks_db, room, reply)

    assert (made.status_code, again.json()["conversationId"]) == (201, room.id)
    assert in_chat.json()["conversationId"] == chat.id
    assert (room.title, room.thread_id, room.agent_id, room.is_internal) == (
        "#eng",
        "C1",
        agent.id,
        True,
    )
    assert {each["id"]: each["isInternal"] for each in listed.json()} == {
        room.id: True,
        chat.id: False,
    }
    assert written.status_code == 409
    posts = [(sent["channel"], sent.get("thread_ts"), sent.get("username")) for _, sent in slack]
    assert posts == [("C1", None, "Scout")]


async def test_a_schedule_takes_only_a_room_that_the_bot_and_its_owner_are_in(
    card, druks_db, tmp_path, monkeypatch
):
    monkeypatch.setattr("druks.schedules.routes.apply_schedule", AsyncMock())
    member_rooms(monkeypatch, {"": [ENG, LEADERSHIP], ANA: [ENG]})
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    chat = await Conversation.create(druks_db, account_id=ana.id, body="Brief us")
    await druks_db.commit()
    api = configure_app_for_test(
        settings=make_settings(tmp_path, identity={"mode": "header", "header": "X-User"}),
        authenticated=False,
    )
    headers = {"X-User": "ana@example.com", CONVERSATION_HEADER: chat.id}
    brief = {"tool": "list_schedules", "arguments": {}, "cron": "0 8 * * 1"}

    async with asgi_client(api) as client:
        unknown = await client.post(
            "/api/schedules", json={**brief, "room": "#eng"}, headers=headers
        )
        await link_person(druks_db, ana)
        without_bot = await client.post(
            "/api/schedules", json={**brief, "room": "#ops"}, headers=headers
        )
        without_owner = await client.post(
            "/api/schedules", json={**brief, "room": "#leadership"}, headers=headers
        )
        # A schedule that Druks refuses leaves no conversation in the room.
        bad_cron = await client.post(
            "/api/schedules", json={**brief, "cron": "soon", "room": "#eng"}, headers=headers
        )

    assert [unknown.json()["detail"], without_bot.json()["detail"]] == [
        "Druks does not know the person's Slack account. Ask them to connect Slack in Druks first.",
        "The bot is not in #ops. Invite it to the channel first.",
    ]
    assert without_owner.json()["detail"] == (
        "The person is not in #leadership. Ask them to join the channel first, or choose a "
        "channel that they are in."
    )
    assert bad_cron.status_code == 422
    assert await Conversation.list_for_connection(druks_db, card.id) == []


async def test_at_the_top_of_a_room_the_agent_learns_that_everyone_reads_its_reply(card, druks_db):
    ana = await Account.get_or_create(druks_db, "ana@example.com")
    room = await thread_conversation(druks_db, card, ana, thread_id="C1")
    thread = await thread_conversation(druks_db, card, ana, thread_id="C1:10.0")
    await druks_db.refresh(room, ["account"])
    await druks_db.refresh(thread, ["account"])

    _, room_prompt, _ = await service.get_agent(druks_db, room)
    _, thread_prompt, _ = await service.get_agent(druks_db, thread)

    assert "This conversation is the top of a room." in room_prompt
    assert "This is a thread in a room." not in room_prompt
    assert "This conversation is the top of a room." not in thread_prompt


async def test_the_client_lists_the_channels_of_a_slack_user(monkeypatch):
    asked = []

    async def api_call(client, api_method, *, params, **request):
        asked.append((api_method, params["user"]))
        return Answer(ok=True, channels=[ENG])

    monkeypatch.setattr(SlackClient, "api_call", api_call)

    channels = await SlackClient(token="xoxb-1").list_member_channels(user=ANA)

    assert channels == [ENG]
    assert asked == [("users.conversations", ANA)]
