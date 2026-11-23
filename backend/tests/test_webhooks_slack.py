import hashlib
import hmac
import inspect
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from urllib.parse import urlencode

import httpx
import pytest
from conftest import FORM_TEXT, bind_ambient_session, install_service
from druks import ui
from druks.accounts.models import Account
from druks.api.client import call_route
from druks.chat.channels.slack.constants import LINK_BUTTON_ACTION_ID
from druks.chat.enums import ConversationSource
from druks.chat.forms import parse_fields
from druks.chat.models import Conversation, Message
from druks.chat.service import deliver
from druks.core.apis.slack import SLACK_AUTHORITY, SlackClient
from druks.core.webhooks.slack import (
    CONNECT_IN_DRUKS_MESSAGE,
    CONNECT_MESSAGE,
    SlackInteractivity,
    call_action,
    explain_reason,
    replace_buttons,
    run_action,
    verify_slack_signature,
)
from druks.db import db_session as ambient_db_session
from druks.durable import Run
from druks.locks import lock
from druks.models import Base
from druks.notifications.buttons import encode_action, encode_button
from druks.notifications.constants import (
    ACTION_RUNNING_MESSAGE,
    CLOSED_MESSAGE,
    NOTIFICATION_LOCK,
)
from druks.notifications.exceptions import AnswerNotAllowedError, DeliveryError
from druks.notifications.models import Notification
from druks.notifications.services import create_park_notification, get_action_request
from druks.secrets.models import Audience, VaultSecret
from druks.settings import configure_settings, get_settings
from druks.teams.models import Team
from druks.testing import (
    asgi_client,
    configure_app_for_test,
    make_settings,
    seed_dbos_status,
)
from druks.webhooks.router import match_webhook
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from sqlalchemy import select
from uuid_utils import uuid7

_SIGNING_SECRET = "slack-test-signing-secret"
_MESSAGE_TS = "1700000000.000200"
_THREAD_TS = "1700000000.000100"

_IN_APP_ASK = {
    "presentation": "in_app",
    "controls": [
        {"id": "approve", "note_required": False},
        {"id": "request_changes", "note_required": True},
    ],
    "questions": [],
}


async def _parked_notification(druks_db):
    run = Run(
        account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
        id=str(uuid7()),
        kind=Summarize.kind,
        input_gate="review",
        input_request=_IN_APP_ASK,
        input_requested_at=Base.utc_now(),
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, "parked")
    notification = await Notification.create(
        druks_db,
        account_id=run.account_id,
        reason="gate.parked",
        body="review the plan",
        subject={"type": "notification_probe", "id": 1},
        actions=[{"id": "approve", "label": "Approve"}],
        run_id=run.id,
        run_parked_at=run.input_requested_at,
    )
    return run, notification


def _signed_headers(body: bytes, *, timestamp=None, secret=_SIGNING_SECRET):
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256)
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": "v0=" + digest.hexdigest(),
        "content-type": "application/x-www-form-urlencoded",
    }


def _interactivity_payload(action_id: str, *, user: str = "U7") -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": user},
        "channel": {"id": "C1"},
        "message": {"ts": _MESSAGE_TS, "thread_ts": _THREAD_TS},
        "actions": [{"type": "button", "action_id": action_id}],
    }


def _interactivity_body(action_id: str, *, user: str = "U7") -> bytes:
    return urlencode({"payload": json.dumps(_interactivity_payload(action_id, user=user))}).encode()


def _client(tmp_path) -> TestClient:
    return TestClient(configure_app_for_test(settings=make_settings(tmp_path)))


@pytest.fixture
async def slack_card(druks_db):
    """The Slack card whose signing secret signs every delivery."""
    bind_ambient_session(druks_db)
    await install_service(
        "slack",
        access_token="xoxb",
        identity={"client_id": "c1", "team": "Acme", "team_id": "T1", "bot_user_id": "U900"},
        secrets={"client_secret": "cs", "signing_secret": _SIGNING_SECRET},
    )
    await druks_db.commit()


@pytest.fixture
def started(monkeypatch):
    """What the click handler starts and does not wait for."""
    started = AsyncMock()
    monkeypatch.setattr("druks.core.webhooks.slack.DBOS.start_workflow_async", started)
    return started


@pytest.fixture
def resume_spy(monkeypatch):
    calls = []

    async def _spy(self, **fields):
        calls.append({"id": self.id, **fields})

    monkeypatch.setattr(Run, "resume", _spy)
    return calls


def test_match_webhook_resolves_slack_interactivity():
    cls, kwargs = match_webhook("slack/interactivity/")
    assert cls is SlackInteractivity
    assert kwargs == {}


def test_signature_verifier_against_a_known_answer(monkeypatch):
    # This fixed vector comes from a computation that uses neither the code under test nor
    # _signed_headers. A mirror-image bug in the base string then cannot hide. The test makes
    # sure that the verifier uses Slack's own v0:{ts}:{body} algorithm. The clock stays at the
    # epoch of the vector.
    body = b"payload=%7B%22type%22%3A%22ping%22%7D"
    timestamp = "1234567890"
    known = "v0=b14d6fbbfab2d9b2cb99c67b41b6289778e8bf6f4b534b69451b9e7ba1f878dd"
    monkeypatch.setattr(
        "druks.core.webhooks.slack.time", SimpleNamespace(time=lambda: 1234567890.0)
    )

    verify_slack_signature(body, known, timestamp, "slack-test-signing-secret")

    bit_flipped = known[:-1] + ("0" if known[-1] != "0" else "1")
    with pytest.raises(HTTPException) as rejected:
        verify_slack_signature(body, bit_flipped, timestamp, "slack-test-signing-secret")
    assert rejected.value.status_code == 401

    with pytest.raises(HTTPException) as tampered_body:
        verify_slack_signature(b"tampered" + body, known, timestamp, "slack-test-signing-secret")
    assert tampered_body.value.status_code == 401


async def test_unsigned_or_stale_requests_401_and_never_resume(
    tmp_path, druks_db, resume_spy, slack_card
):
    _, notification = await _parked_notification(druks_db)
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        no_headers = client.post("/_external/slack/interactivity/", content=body)
        assert no_headers.status_code == 401

        wrong_secret = client.post(
            "/_external/slack/interactivity/",
            content=body,
            headers=_signed_headers(body, secret="wrong-secret"),
        )
        assert wrong_secret.status_code == 401

        replayed = client.post(
            "/_external/slack/interactivity/",
            content=body,
            headers=_signed_headers(body, timestamp=str(int(time.time()) - 3600)),
        )
        assert replayed.status_code == 401

    assert resume_spy == []
    assert (await ambient_db_session().get(Notification, notification.id)).state == "pending"
    for response in (no_headers, wrong_secret, replayed):
        assert _SIGNING_SECRET not in response.text
        assert notification.correlation_token not in response.text


async def test_signed_click_routes_through_respond(
    tmp_path, druks_db, resume_spy, slack_card, started
):
    run, notification = await _parked_notification(druks_db)
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert resume_spy == [
        {"id": run.id, "action": "approve", "answers": {}, "note": "", "answered_by_id": ""}
    ]
    started.assert_awaited_once_with(
        replace_buttons,
        notification.id,
        channel="C1",
        message_ts=_MESSAGE_TS,
        outcome="<@U7> chose Approve.",
    )
    # The request's own session committed the transition. Clear the cache of the ambient,
    # task-scoped session to read it.
    ambient_db_session().expunge_all()
    assert (await ambient_db_session().get(Notification, notification.id)).state == "acknowledged"
    assert notification.correlation_token not in response.text
    assert _SIGNING_SECRET not in response.text


async def test_dead_round_click_is_acknowledged_without_resume(
    tmp_path, druks_db, resume_spy, slack_card, started
):
    _, notification = await _parked_notification(druks_db)
    await notification.mark_acknowledged()
    with _client(tmp_path) as client:
        body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
        acknowledged = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json() == {"accepted": False}

        ghost = _interactivity_body(encode_button("ghost-token", "approve"))
        unknown = client.post(
            "/_external/slack/interactivity/", content=ghost, headers=_signed_headers(ghost)
        )
        assert unknown.status_code == 200
    assert unknown.json() == {"accepted": False}
    assert "ghost-token" not in unknown.text

    assert resume_spy == []
    assert started.await_args_list == [
        call(explain_reason, "C1", user="U7", reason=reason, thread_ts=_THREAD_TS)
        for reason in ("This notification was already acknowledged.", "Unknown respond token.")
    ]


async def test_a_click_on_a_link_button_answers_nothing(
    tmp_path, druks_db, resume_spy, slack_card, started
):
    _, notification = await _parked_notification(druks_db)
    body = _interactivity_body(LINK_BUTTON_ACTION_ID)
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert resume_spy == []
    started.assert_not_awaited()
    assert (await ambient_db_session().get(Notification, notification.id)).state == "pending"


async def test_click_on_a_finished_round_takes_the_buttons_off_the_message(
    tmp_path, druks_db, resume_spy, slack_card, started
):
    run, notification = await _parked_notification(druks_db)
    notification.run_parked_at = run.input_requested_at - timedelta(minutes=1)
    await druks_db.flush()
    body = _interactivity_body(encode_button(notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": False}
    assert resume_spy == []
    started.assert_awaited_once_with(
        replace_buttons,
        notification.id,
        channel="C1",
        message_ts=_MESSAGE_TS,
        outcome="This question is closed.",
    )


async def test_click_that_is_not_the_clickers_to_answer_explains_it_to_the_clicker(
    tmp_path, druks_db, slack_card, started, monkeypatch
):
    monkeypatch.setattr(
        "druks.core.webhooks.slack.respond_to_notification",
        AsyncMock(side_effect=AnswerNotAllowedError("Only the person who started it answers.")),
    )
    body = _interactivity_body(encode_button("token", "approve"))
    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": False}
    started.assert_awaited_once_with(
        explain_reason,
        "C1",
        user="U7",
        reason="Only the person who started it answers.",
        thread_ts=_THREAD_TS,
    )


async def _form_question(druks_db) -> Notification:
    """An open form that the agent of a Slack chat with U7 sent at the end of its turn."""
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id="U7",
        thread_id=f"C1:{_THREAD_TS}",
    )
    druks_db.add(conversation)
    await druks_db.flush()
    return await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body="Where does it ship?",
        question_offset=0,
        fields=parse_fields(FORM_TEXT),
    )


def _option(label: str, value: str) -> dict:
    return {"text": {"type": "plain_text", "text": label}, "value": value}


# Slack names each input's action itself.
_STATE = {
    "title": {"a1": {"type": "plain_text_input", "value": "Fix <!here> & go"}},
    "notes": {"a2": {"type": "plain_text_input", "value": None}},
    "budget": {"a3": {"type": "plain_text_input", "value": "15"}},
    "where": {
        "a4": {"type": "static_select", "selected_option": _option("Production", "production")}
    },
    "tags": {
        "a5": {
            "type": "multi_static_select",
            "selected_options": [_option("Shelf", "shelf"), _option("Rack", "rack")],
        }
    },
    "size": {"a6": {"type": "radio_buttons", "selected_option": _option("Large", "l")}},
    "notify": {"a7": {"type": "checkboxes", "selected_options": [_option("Yes", "yes")]}},
}


def _form_body(action: dict, state: dict) -> bytes:
    payload = {**_interactivity_payload(""), "actions": [action], "state": {"values": state}}
    return urlencode({"payload": json.dumps(payload)}).encode()


def _submit(question: Notification) -> dict:
    return {"type": "button", "action_id": encode_button(question.correlation_token, "submit")}


async def test_a_submit_reads_the_inputs_and_shows_the_answers_in_their_place(
    tmp_path, druks_db, slack_card, started
):
    question = await _form_question(druks_db)
    body = _form_body(_submit(question), _STATE)

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": True}
    answers = (
        "Title: Fix <!here> & go\nNotes: (no answer)\nBudget: 15\nWhere: Production\n"
        "Tags: Rack, Shelf\nSize: Large\nNotify the owner: Yes"
    )
    messages = await ambient_db_session().execute(
        select(Message.body, Message.is_internal, Message.state).where(
            Message.conversation_id == question.conversation_id
        )
    )
    assert messages.all() == [(answers, False, "pending")]
    shown = answers.replace("<!here> & go", "&lt;!here&gt; &amp; go")
    assert started.await_args_list == [
        call(deliver, question.conversation_id),
        call(
            replace_buttons,
            question.id,
            channel="C1",
            message_ts=_MESSAGE_TS,
            outcome=f"<@U7> answered:\n{shown}",
        ),
    ]


async def test_a_submit_that_its_fields_refuse_tells_the_person_why_and_keeps_the_form(
    tmp_path, druks_db, slack_card, started
):
    question = await _form_question(druks_db)
    state = {**_STATE, "budget": {"a3": {"type": "plain_text_input", "value": "a lot"}}}
    body = _form_body(_submit(question), state)

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": False}
    started.assert_awaited_once_with(
        explain_reason,
        "C1",
        user="U7",
        reason='Write a number in "Budget". The minimum is 0. The maximum is 100. The step is 5.',
        thread_ts=_THREAD_TS,
    )
    ambient_db_session().expunge_all()
    assert (await ambient_db_session().get(Notification, question.id)).state == "pending"


_REVIEW_ASK = {
    **_IN_APP_ASK,
    "questions": [
        {
            "id": "database",
            "prompt": "Which database?",
            "options": [{"id": "postgres", "label": "Postgres"}, {"id": "mysql", "label": "MySQL"}],
        },
        {"id": "cache", "prompt": "Which cache?", "options": [{"id": "redis", "label": "Redis"}]},
    ],
    "reply_fields": ["action", "answers", "note"],
}


async def _approval(
    druks_db, *blocks: ui.Block, is_in_a_chat: bool = False
) -> tuple[Run, Notification]:
    """A run parked on _REVIEW_ASK with ``blocks`` as its context, and the notification that
    its park made. The notification is for the run's account, or in the thread of its Slack
    chat with U7. On an installation with an endpoint, the notification links to the run's
    page."""
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation_id = None
    if is_in_a_chat:
        conversation = Conversation(
            account_id=account.id,
            source=ConversationSource.SLACK,
            user_id="U7",
            thread_id=f"C1:{_THREAD_TS}",
        )
        druks_db.add(conversation)
        await druks_db.flush()
        conversation_id = conversation.id
    run_id = str(uuid7())
    run = Run(
        account_id=account.id,
        id=run_id,
        kind=Summarize.kind,
        input_gate="review",
        input_request={
            **_REVIEW_ASK,
            # Each action holds the page of its run, as ``review()`` stores it.
            "context": [
                block.model_dump(
                    mode="json", by_alias=True, context={"action_url": f"/runs/{run_id}"}
                )
                for block in blocks
            ],
        },
        input_requested_at=Base.utc_now(),
        conversation_id=conversation_id,
    )
    druks_db.add(run)
    await druks_db.flush()
    await seed_dbos_status(druks_db, run.id, "parked")
    notification_id = await create_park_notification(
        druks_db,
        run,
        {"type": "notification_probe", "id": 1},
        account_id=None if is_in_a_chat else account.id,
        conversation_id=conversation_id,
        deep_link=run.get_link(),
    )
    return run, await druks_db.get(Notification, notification_id)


def _select(option: dict | None) -> dict:
    return {"a1": {"type": "static_select", "selected_option": option}}


def _text(value: str | None) -> dict:
    return {"a2": {"type": "plain_text_input", "value": value}}


@pytest.mark.parametrize("is_in_a_chat", [False, True], ids=["account", "chat"])
@pytest.mark.parametrize(
    ("control", "state", "reply", "outcome"),
    [
        (
            "approve",
            {
                "question:database": _select(_option("Postgres", "postgres")),
                "question:cache": _select(_option("Redis", "redis")),
                "note": _text(None),
                # The inputs that the approval does not offer.
                "question:ghost": _select(_option("Ghost", "ghost")),
                "database": _text("mysql"),
            },
            {"action": "approve", "answers": {"database": "postgres", "cache": "redis"}},
            "<@U7> chose Approve.\nWhich database?: Postgres\nWhich cache?: Redis\n"
            "Note: (no answer)",
        ),
        (
            "request_changes",
            {
                "question:database": _select(_option("MySQL", "mysql")),
                "question:cache": _select(None),
                "note": _text("Fix <!here> & go"),
            },
            {
                "action": "request_changes",
                "answers": {"database": "mysql"},
                "note": "Fix <!here> & go",
            },
            "<@U7> chose Request changes.\nWhich database?: MySQL\nWhich cache?: (no answer)\n"
            "Note: Fix &lt;!here&gt; &amp; go",
        ),
    ],
    ids=["every-answer", "some-answers-and-a-note"],
)
@pytest.mark.usefixtures("slack_card")
async def test_a_click_answers_the_run_with_the_inputs_and_shows_them_in_their_place(
    tmp_path, druks_db, resume_spy, started, is_in_a_chat, control, state, reply, outcome
):
    run, approval = await _approval(druks_db, is_in_a_chat=is_in_a_chat)
    button = {"type": "button", "action_id": encode_button(approval.correlation_token, control)}
    body = _form_body(button, state)

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.json() == {"accepted": True}
    # U7 has no linked account, and the person of a chat answers as the chat's account.
    answered_by_id = run.account_id if is_in_a_chat else ""
    assert resume_spy == [{"id": run.id, "note": "", "answered_by_id": answered_by_id, **reply}]
    # Druks does not ask the chat's agent.
    assert started.await_args_list == [
        call(replace_buttons, approval.id, channel="C1", message_ts=_MESSAGE_TS, outcome=outcome)
    ]


@pytest.mark.parametrize("is_in_a_chat", [False, True], ids=["account", "chat"])
@pytest.mark.parametrize("note", [None, "   "], ids=["empty", "blank"])
@pytest.mark.parametrize("database", [None, _option("MySQL", "mysql")], ids=["no-answer", "answer"])
@pytest.mark.usefixtures("slack_card")
async def test_a_click_that_needs_a_note_tells_only_the_person_when_the_note_is_empty(
    tmp_path, druks_db, resume_spy, started, is_in_a_chat, note, database
):
    _, approval = await _approval(druks_db, is_in_a_chat=is_in_a_chat)
    button = {
        "type": "button",
        "action_id": encode_button(approval.correlation_token, "request_changes"),
    }
    body = _form_body(button, {"question:database": _select(database), "note": _text(note)})

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert (response.status_code, response.json()) == (200, {"accepted": False})
    assert resume_spy == []
    reason = "This button needs a note. Write the note, then click the button again."
    assert started.await_args_list == [
        call(explain_reason, "C1", user="U7", reason=reason, thread_ts=_THREAD_TS)
    ]
    ambient_db_session().expunge_all()
    assert (await ambient_db_session().get(Notification, approval.id)).state == "pending"


async def test_malformed_payloads_400_never_500_never_resume(
    tmp_path, druks_db, resume_spy, slack_card
):
    _, notification = await _parked_notification(druks_db)
    malformed = [
        b"not-a-form",
        urlencode({"payload": "not json"}).encode(),
        urlencode({"payload": json.dumps({"actions": [{"action_id": "x.y"}]})}).encode(),
        urlencode({"payload": json.dumps({"type": "block_actions"})}).encode(),
        urlencode({"payload": json.dumps({"type": "block_actions", "actions": []})}).encode(),
        _interactivity_body("not-an-encoded-button!!"),
    ]
    # Each payload has a real button for the parked round. The loop deletes one field that
    # the handler needs, a different one each time.
    for field in ("user", "channel", "message"):
        payload = _interactivity_payload(encode_button(notification.correlation_token, "approve"))
        del payload[field]
        malformed.append(urlencode({"payload": json.dumps(payload)}).encode())

    with _client(tmp_path) as client:
        for body in malformed:
            response = client.post(
                "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
            )
            assert response.status_code == 400

    assert resume_spy == []
    assert (await ambient_db_session().get(Notification, notification.id)).state == "pending"


async def test_unknown_interactivity_type_is_acknowledged_unhandled(
    tmp_path, druks_db, resume_spy, slack_card
):
    await _parked_notification(druks_db)
    body = urlencode({"payload": json.dumps({"type": "view_submission"})}).encode()

    with _client(tmp_path) as client:
        response = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "handled": False}
    assert resume_spy == []


async def _link_slack_user(druks_db, account, user_id):
    await VaultSecret.connect(
        druks_db,
        Audience.service("slack"),
        account_id=account.id,
        refresh_token=f"xoxe-{user_id}",
        scopes=["users:read"],
        identity={"authority": SLACK_AUTHORITY.format(team_id="T1"), "subject": user_id},
    )


async def test_a_team_s_app_takes_a_click_only_from_its_team(
    tmp_path, druks_db, resume_spy, slack_card, started
):
    run, notification = await _parked_notification(druks_db)
    member = await Account.get_or_create(druks_db, "member@example.com")
    outsider = await Account.get_or_create(druks_db, "outsider@example.com")
    await Team.create(druks_db, name="Notes", member_ids=[member.id], apps=["field_notes"])
    await _link_slack_user(druks_db, member, "U8")
    await _link_slack_user(druks_db, outsider, "U7")
    await druks_db.commit()
    button = encode_button(notification.correlation_token, "approve")

    with _client(tmp_path) as client:
        refused = client.post(
            "/_external/slack/interactivity/",
            content=(body := _interactivity_body(button, user="U7")),
            headers=_signed_headers(body),
        )
        assert resume_spy == []
        answered = client.post(
            "/_external/slack/interactivity/",
            content=(body := _interactivity_body(button, user="U8")),
            headers=_signed_headers(body),
        )

    assert refused.json() == {"accepted": False}
    assert answered.json() == {"accepted": True}
    assert resume_spy == [
        {
            "id": run.id,
            "action": "approve",
            "answers": {},
            "note": "",
            "answered_by_id": member.id,
        }
    ]
    explained = started.await_args_list[0]
    assert (explained.args[0], explained.kwargs["user"]) == (explain_reason, "U7")


async def test_a_click_with_no_account_answers_only_an_app_that_no_team_lists(
    tmp_path, druks_db, resume_spy, slack_card, started
):
    open_run, open_notification = await _parked_notification(druks_db)
    open_body = _interactivity_body(encode_button(open_notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        opened = client.post(
            "/_external/slack/interactivity/", content=open_body, headers=_signed_headers(open_body)
        )
    await Team.create(druks_db, name="Notes", member_ids=[], apps=["field_notes"])
    _, listed_notification = await _parked_notification(druks_db)
    await druks_db.commit()
    body = _interactivity_body(encode_button(listed_notification.correlation_token, "approve"))
    with _client(tmp_path) as client:
        refused = client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )

    assert opened.json() == {"accepted": True}
    assert refused.json() == {"accepted": False}
    assert [call["id"] for call in resume_spy] == [open_run.id]


_ENDPOINT = "https://druks.example"


def _archive(note: Note) -> ui.Action:
    return ui.Action(label="Archive", operation="archive_note", arguments={"note_id": note.id})


def _note_form() -> ui.Form:
    return ui.Form(
        title="Rack 3",
        fields=[ui.TextAreaField(name="body", label="Note", is_required=True)],
        action=ui.Action(label="Write a note", operation="write_note"),
        extra_actions=[ui.Action(label="Write another note", operation="write_note")],
    )


@pytest.fixture
def slack_api(monkeypatch):
    """Slack's Web API, answered locally. It records each call as its method and its
    arguments. Each item of ``refusals`` refuses the next call."""
    fake = SimpleNamespace(calls=[], refusals=[])

    async def api_call(client, api_method, *, json=None, **request):
        fake.calls.append({"method": api_method, **(json or {})})
        if fake.refusals:
            raise SlackApiError("refused", {"ok": False, "error": fake.refusals.pop(0)})
        return SimpleNamespace(data={"ok": True, "channel": "C1", "ts": _MESSAGE_TS})

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    return fake


@pytest.fixture
def called(monkeypatch):
    """The accounts that Druks called the route of an action as, in order."""
    accounts = []

    async def call(method, path, *, json, account_id):
        accounts.append(account_id)
        return await call_route(method, path, json=json, account_id=account_id)

    monkeypatch.setattr("druks.core.webhooks.slack.call_route", call)
    return accounts


@pytest.fixture
async def member(druks_db):
    """The account that Slack user U7 linked."""
    account = await Account.get_or_create(druks_db, "member@example.com")
    await _link_slack_user(druks_db, account, "U7")
    return account


async def _click(action_id: str, *, user: str = "U7", state: dict | None = None) -> httpx.Response:
    """Click the button ``action_id`` as Slack user ``user``, with ``state``, the inputs of
    the message keyed by the ids of their blocks. Slack stamps each click with its own time. A
    second click is thus not a retry of the first. The API overrides no dependency. The
    route of an action thus checks the credential of its call itself."""
    api = configure_app_for_test(settings=get_settings(), authenticated=False)
    payload = _interactivity_payload(action_id, user=user)
    payload["actions"][0]["action_ts"] = str(time.time_ns())
    if state:
        payload["state"] = {"values": state}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    async with asgi_client(api) as client:
        return await client.post(
            "/_external/slack/interactivity/", content=body, headers=_signed_headers(body)
        )


async def _run(started_call) -> None:
    """Run what a click started, as DBOS runs it after the click has its answer."""
    await inspect.unwrap(started_call.args[0])(*started_call.args[1:], **started_call.kwargs)


async def test_a_click_on_an_action_runs_its_route_as_the_clicker_and_names_them_in_its_place(
    druks_db, slack_card, member, started, resume_spy, slack_api
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()

    clicked = await _click(encode_action(approval.correlation_token, 1))
    action_run = started.await_args
    # Slack has its answer while the action waits to run.
    assert (clicked.json(), action_run.args[:3]) == (
        {"accepted": True},
        (run_action, approval.id, [1]),
    )
    await _run(action_run)

    druks_db.expunge_all()
    assert (await druks_db.get(Note, note.id)).archived_by == "member@example.com"
    stored = await druks_db.get(Notification, approval.id)
    line = ui.Text("member@example.com ran Archive.").model_dump(mode="json", by_alias=True)
    assert stored.blocks == [approval.blocks[0], line, *approval.blocks[2:]]
    assert stored.body.startswith("# Review\n\nmember@example.com ran Archive.\n\nWhich database?")
    # The action neither answers the run nor closes its question.
    assert (resume_spy, stored.is_acknowledged) == ([], False)
    (edit,) = slack_api.calls
    assert (edit["method"], edit["ts"]) == ("chat.update", _MESSAGE_TS)
    assert edit["blocks"][0]["text"].startswith("# Review\n\nmember@example.com ran Archive.")
    assert [button["action_id"] for button in edit["blocks"][-1]["elements"]] == [
        encode_button(approval.correlation_token, "approve"),
        encode_button(approval.correlation_token, "request_changes"),
    ]


async def test_a_click_on_a_row_s_action_runs_it_on_the_note_of_that_row_as_the_clicker(
    druks_db, slack_card, member, started, resume_spy, slack_api
):
    notes = [
        await Note.create(body="Fan noise on rack 3."),
        await Note.create(body="Rack 4 is quiet."),
    ]
    table = ui.Table(
        columns=[ui.TableColumn("Note"), ui.TableColumn("")],
        rows=[ui.TableRow([note.body, ui.ControlsValue([_archive(note)])]) for note in notes],
    )
    _, approval = await _approval(druks_db, table)
    await druks_db.commit()
    token, blocks = approval.correlation_token, approval.blocks

    clicked = await _click(encode_action(token, 1, 1, 1))
    action_run = started.await_args
    assert (clicked.json(), action_run.args[:3]) == (
        {"accepted": True},
        (run_action, approval.id, [1, 1, 1]),
    )
    await _run(action_run)

    druks_db.expunge_all()
    assert [(await druks_db.get(Note, note.id)).archived_by for note in notes] == [
        None,
        "member@example.com",
    ]
    stored = await druks_db.get(Notification, approval.id)
    # The line replaces the cell. Every other part keeps its place.
    line = ui.TextValue("member@example.com ran Archive.").model_dump(mode="json", by_alias=True)
    first_row, second_row = blocks[1]["rows"]
    ran_row = {**second_row, "cells": [second_row["cells"][0], line]}
    assert stored.blocks == [blocks[0], {**blocks[1], "rows": [first_row, ran_row]}, *blocks[2:]]
    assert "| Rack 4 is quiet. | member@example.com ran Archive. |" in stored.body
    # The action neither answers the run nor closes its question.
    assert (resume_spy, stored.is_acknowledged) == ([], False)
    (edit,) = slack_api.calls
    button = {
        "type": "button",
        "text": {"type": "plain_text", "text": "Archive"},
        "action_id": encode_action(token, 1, 0, 1),
    }
    assert (edit["method"], edit["text"], edit["blocks"][1]["rows"][1:]) == (
        "chat.update",
        stored.body,
        [
            [
                {"type": "raw_text", "text": "Fan noise on rack 3."},
                {"type": "action_cell", "element": button},
            ],
            [
                {"type": "raw_text", "text": "Rack 4 is quiet."},
                {"type": "raw_text", "text": "member@example.com ran Archive."},
            ],
        ],
    )

    await _click(encode_action(token, 1, 1, 1))
    assert started.await_args == call(
        explain_reason, "C1", user="U7", reason="This action already ran.", thread_ts=_THREAD_TS
    )
    await _click(encode_action(token, 1, 0, 1))
    await _run(started.await_args)

    druks_db.expunge_all()
    assert [(await druks_db.get(Note, note.id)).archived_by for note in notes] == [
        "member@example.com",
        "member@example.com",
    ]


async def test_an_action_button_whose_position_is_no_number_runs_nothing(
    druks_db, slack_card, started
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()

    response = await _click(encode_action(approval.correlation_token, -1))

    assert response.status_code == 400
    started.assert_not_awaited()


async def _answered(druks_db, note: Note) -> Notification:
    _, approval = await _approval(druks_db, _archive(note))
    await approval.mark_acknowledged()
    return approval


async def _ran(druks_db, note: Note) -> Notification:
    _, approval = await _approval(druks_db, _archive(note))
    await approval.mark_action_run([1], username="member@example.com")
    return approval


async def _form_ran(druks_db, note: Note) -> Notification:
    _, approval = await _approval(druks_db, _note_form())
    await approval.mark_action_run([1, 0], username="member@example.com")
    return approval


async def _with_no_route(druks_db, note: Note) -> Notification:
    shred = ui.Action(label="Shred", operation="shred_note", arguments={"note_id": note.id})
    _, approval = await _approval(druks_db, shred)
    return approval


async def _with_no_path_value(druks_db, note: Note) -> Notification:
    _, approval = await _approval(druks_db, ui.Action(label="Archive", operation="archive_note"))
    return approval


async def _with_a_dot_as_its_path_value(druks_db, note: Note) -> Notification:
    # A client reads "/notes/./archive" as "/notes/archive", which is another route.
    archive = ui.Action(label="Archive", operation="archive_note", arguments={"note_id": "."})
    _, approval = await _approval(druks_db, archive)
    return approval


async def _in_a_chat(druks_db, note: Note) -> Notification:
    _, approval = await _approval(druks_db, _archive(note), is_in_a_chat=True)
    return approval


@pytest.mark.parametrize(
    ("make", "user", "position", "reason"),
    [
        (_answered, "U7", [1], "This notification was already acknowledged."),
        (_ran, "U7", [1], "This action already ran."),
        # The line of a form replaces every action of the form.
        (_form_ran, "U7", [1, 1], "This action already ran."),
        (
            _with_no_route,
            "U7",
            [1],
            "The app field_notes has no route for this action at this time. Ask an admin to "
            "add the route to the app again.",
        ),
        (
            _with_no_path_value,
            "U7",
            [1],
            "This action carries no path value for note_id. Give the action an argument or "
            "a required field with each of these names.",
        ),
        (
            _with_a_dot_as_its_path_value,
            "U7",
            [1],
            "This action carries no path value for note_id. Give the action an argument or "
            "a required field with each of these names.",
        ),
        (
            _in_a_chat,
            "U8",
            [1],
            "Only the person of this conversation can run its actions. Ask that person to "
            "run this action.",
        ),
    ],
    ids=[
        "answered",
        "ran",
        "form-that-ran",
        "no-route",
        "no-path-value",
        "dot-path-value",
        "another-persons-chat",
    ],
)
async def test_an_action_that_cannot_run_runs_nothing_and_only_the_clicker_reads_why(
    druks_db, slack_card, member, started, make, user, position, reason
):
    note = await Note.create(body="Fan noise on rack 3.")
    approval = await make(druks_db, note)
    await druks_db.commit()

    response = await _click(encode_action(approval.correlation_token, *position), user=user)

    assert response.json() == {"accepted": False}
    (explained,) = started.await_args_list
    assert (explained.args, explained.kwargs["user"]) == ((explain_reason, "C1"), user)
    assert reason in explained.kwargs["reason"]


async def test_a_click_on_an_action_of_a_past_round_takes_the_buttons_off_its_message(
    druks_db, slack_card, member, started
):
    note = await Note.create(body="Fan noise on rack 3.")
    run, approval = await _approval(druks_db, _archive(note))
    approval.run_parked_at = run.input_requested_at - timedelta(minutes=1)
    await druks_db.commit()

    response = await _click(encode_action(approval.correlation_token, 1))

    assert response.json() == {"accepted": False}
    started.assert_awaited_once_with(
        replace_buttons, approval.id, channel="C1", message_ts=_MESSAGE_TS, outcome=CLOSED_MESSAGE
    )


@pytest.mark.parametrize(
    ("endpoint", "get_reason"),
    [
        (
            _ENDPOINT,
            lambda run: CONNECT_MESSAGE.format(
                url=f"{_ENDPOINT}/api/oauth/slack/connect?next=%2Fruns%2F{run.id}"
            ),
        ),
        ("", lambda run: CONNECT_IN_DRUKS_MESSAGE),
    ],
    ids=["endpoint", "no-endpoint"],
)
async def test_a_person_with_no_linked_account_gets_the_way_to_connect_it_by_direct_message(
    tmp_path, druks_db, slack_card, started, endpoint, get_reason
):
    configure_settings(make_settings(tmp_path, urls={"endpoint": endpoint}))
    note = await Note.create(body="Fan noise on rack 3.")
    run, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()

    response = await _click(encode_action(approval.correlation_token, 1))

    assert response.json() == {"accepted": False}
    started.assert_awaited_once_with(
        explain_reason, channel="U7", user="", reason=get_reason(run), thread_ts=""
    )


async def test_an_action_runs_only_for_a_person_whose_account_can_use_its_app(
    druks_db, slack_card, member, started, slack_api
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    outsider = await Account.get_or_create(druks_db, "outsider@example.com")
    await _link_slack_user(druks_db, outsider, "U8")
    await Team.create(druks_db, name="Notes", member_ids=[member.id], apps=["field_notes"])
    await druks_db.commit()
    action_id = encode_action(approval.correlation_token, 1)

    await _click(action_id, user="U8")
    await _run(started.await_args)
    druks_db.expunge_all()
    archived_by_outsider = (await druks_db.get(Note, note.id)).archived_by
    await _click(action_id)
    await _run(started.await_args)

    druks_db.expunge_all()
    assert not archived_by_outsider
    assert (await druks_db.get(Note, note.id)).archived_by == "member@example.com"
    reply, edit = slack_api.calls
    assert (reply["method"], reply["user"], reply["text"]) == (
        "chat.postEphemeral",
        "U8",
        "outsider@example.com cannot use the app field_notes. Ask an admin to add "
        "outsider@example.com to a team that lists the app.",
    )
    assert edit["method"] == "chat.update"


async def test_a_value_that_the_route_refuses_is_named_to_the_clicker_alone(
    druks_db, slack_card, member, started, slack_api
):
    # A "?" that Druks did not encode ends the path. No route then answers it.
    archive = ui.Action(label="Archive", operation="archive_note", arguments={"note_id": "7?draft"})
    _, approval = await _approval(druks_db, archive)
    await druks_db.commit()

    await _click(encode_action(approval.correlation_token, 1))
    await _run(started.await_args)

    (reply,) = slack_api.calls
    assert (reply["method"], reply["user"], reply["text"]) == (
        "chat.postEphemeral",
        "U7",
        "note_id: Input should be a valid integer, unable to parse string as an integer",
    )


async def test_an_action_sends_its_other_arguments_as_the_body_of_its_request(
    druks_db, slack_card, member, started, slack_api
):
    write = ui.Action(label="Write", operation="write_note", arguments={"body": "Rack 3 is quiet."})
    _, approval = await _approval(druks_db, write)
    await druks_db.commit()

    await _click(encode_action(approval.correlation_token, 1))
    await _run(started.await_args)

    assert await druks_db.scalar(select(Note.id).where(Note.body == "Rack 3 is quiet."))
    (edit,) = slack_api.calls
    assert edit["blocks"][0]["text"].startswith("# Review\n\nmember@example.com ran Write.")


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (Response(status_code=409), "The route refused the request with status 409."),
        (
            JSONResponse({"message": "No."}, status_code=400),
            "The route refused the request with status 400.",
        ),
    ],
    ids=["no-body", "a-shape-of-its-own"],
)
async def test_a_refusal_with_no_reason_that_druks_reads_names_its_status_to_the_clicker(
    druks_db, slack_card, member, started, slack_api, monkeypatch, answer, reason
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()

    await _click(encode_action(approval.correlation_token, 1))
    # The API that the action calls answers every request with ``answer``.
    monkeypatch.setattr("druks.api.server.app", answer)
    await _run(started.await_args)

    (reply,) = slack_api.calls
    assert (reply["method"], reply["user"], reply["text"]) == ("chat.postEphemeral", "U7", reason)


async def test_a_click_while_an_action_of_its_message_runs_runs_nothing(
    druks_db, slack_card, member, started, slack_api, called
):
    note = await Note.create(body="Fan noise on rack 3.")
    run, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()
    await _click(encode_action(approval.correlation_token, 1))

    # Another action of the message runs.
    async with lock(NOTIFICATION_LOCK.format(run=run.id, notification=approval.id)):
        await _run(started.await_args)

    assert called == []
    (reply,) = slack_api.calls
    assert (reply["method"], reply["user"], reply["text"]) == (
        "chat.postEphemeral",
        "U7",
        ACTION_RUNNING_MESSAGE,
    )


async def test_a_second_click_on_an_action_runs_nothing_once_the_first_click_ran_it(
    druks_db, slack_card, member, started, slack_api, called
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()
    action_id = encode_action(approval.correlation_token, 1)
    # Both clicks come before the action runs. Each starts a run of it.
    await _click(action_id)
    await _click(action_id)

    for action_run in started.await_args_list:
        await _run(action_run)

    assert called == [member.id]
    edit, reply = slack_api.calls
    assert (edit["method"], reply["method"], reply["text"]) == (
        "chat.update",
        "chat.postEphemeral",
        "This action already ran.",
    )


async def test_an_action_s_line_is_stored_before_its_edit_so_a_failed_edit_runs_it_no_more(
    druks_db, slack_card, member, started, slack_api, called
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()
    action_id = encode_action(approval.correlation_token, 1)
    slack_api.refusals = ["ratelimited"]

    await _click(action_id)
    with pytest.raises(DeliveryError):
        await _run(started.await_args)
    # DBOS tries the edit again. It does not try the action again.
    await replace_buttons(approval.id, channel="C1", message_ts=_MESSAGE_TS, outcome="")
    await _click(action_id)

    assert called == [member.id]
    _, edit = slack_api.calls
    assert edit["blocks"][0]["text"].startswith("# Review\n\nmember@example.com ran Archive.")
    assert started.await_args == call(
        explain_reason, "C1", user="U7", reason="This action already ran.", thread_ts=_THREAD_TS
    )


async def test_an_answer_while_an_action_runs_keeps_its_outcome_and_the_action_s_line(
    druks_db, slack_card, member, started, resume_spy, slack_api
):
    note = await Note.create(body="Fan noise on rack 3.")
    _, approval = await _approval(druks_db, _archive(note))
    await druks_db.commit()
    await _click(encode_action(approval.correlation_token, 1))

    # The route answers. Then the edit for the approval occurs before the action's own edit.
    await call_action(approval.id, [1], values={}, account_id=member.id, user="U7")
    await _click(encode_button(approval.correlation_token, "approve"))
    await _run(started.await_args)
    await replace_buttons(approval.id, channel="C1", message_ts=_MESSAGE_TS, outcome="")

    (edit,) = slack_api.calls
    assert edit["blocks"][0]["text"].startswith("# Review\n\nmember@example.com ran Archive.")
    assert edit["blocks"][-1]["text"]["text"].startswith("<@U7> chose Approve.")


# The inputs of a message that holds _note_form at position 1 and an action at 2.
_NOTES = {
    "1.body": _text("Fan 3 is fixed."),
    "2.body": _text("Rack 4 hums."),
    # The inputs of no block: the approval's own note, and the name of a field with no position.
    "note": _text("Keep the queue."),
    "body": _text("Not a note."),
}


@pytest.mark.parametrize(
    ("position", "label", "body", "kept"),
    [
        ([1, 0], "Write a note", "Fan 3 is fixed.", "2.body"),
        ([1, 1], "Write another note", "Fan 3 is fixed.", "2.body"),
        ([2], "Write it here", "Rack 4 hums.", "1.body"),
    ],
    ids=["form", "extra-action-of-the-form", "action-with-fields"],
)
async def test_a_click_sends_the_inputs_of_its_block_to_the_route_as_the_clicker(
    druks_db, slack_card, member, started, slack_api, position, label, body, kept
):
    write = ui.Action(
        label="Write it here",
        operation="write_note",
        fields=[ui.TextAreaField(name="body", label="Note", is_required=True)],
    )
    _, approval = await _approval(druks_db, _note_form(), write)
    await druks_db.commit()

    assert (
        await _click(encode_action(approval.correlation_token, *position), state=_NOTES)
    ).json() == {"accepted": True}
    action_run = started.await_args
    assert action_run.args[1:3] == (approval.id, position)
    assert action_run.kwargs["values"] == {"body": body}
    await _run(action_run)

    druks_db.expunge_all()
    assert (await druks_db.execute(select(Note.body, Note.written_by))).all() == [
        (body, "member@example.com")
    ]
    assert (await druks_db.get(Notification, approval.id)).blocks[position[0]] == ui.Text(
        f"member@example.com ran {label}."
    ).model_dump(mode="json", by_alias=True)
    (edit,) = slack_api.calls
    # Druks removes the inputs of the block that ran. The inputs of the other block and of the
    # approval stay.
    assert [block["block_id"] for block in edit["blocks"] if block["type"] == "input"] == [
        kept,
        "question:database",
        "question:cache",
        "note",
    ]


async def test_a_click_with_an_empty_required_input_names_the_field_and_saves_nothing(
    druks_db, slack_card, member, started
):
    _, approval = await _approval(druks_db, _note_form())
    await druks_db.commit()

    assert (
        await _click(encode_action(approval.correlation_token, 1, 0), state={"1.body": _text("  ")})
    ).json() == {"accepted": False}
    started.assert_awaited_once_with(
        explain_reason, "C1", user="U7", reason='Answer "Note".', thread_ts=_THREAD_TS
    )
    assert not (await druks_db.scalars(select(Note))).all()


# The answers to the form of the test that follows, in the shapes that Slack's inputs
# give them.
_ANSWERS = {
    "title": "Fix it",
    "notes": "",
    "budget": "15",
    "where": "production",
    "tags": ["shelf", "rack"],
    "size": "l",
    "notify": True,
    # No field of the form takes it.
    "note": "Keep the queue.",
}
# The same answers as the dashboard sends them.
_SENT = {
    "title": "Fix it",
    "notes": "",
    "budget": 15,
    "where": "production",
    "tags": ["shelf", "rack"],
    "size": "l",
    "notify": True,
}


@pytest.mark.parametrize(
    ("position", "answers", "path", "body"),
    [
        ([1, 0], {}, "/api/field_notes/notes", {"to": "rack 3", **_SENT}),
        ([1, 1], {}, "/api/field_notes/notes/7/archive", _SENT),
        (
            [1, 0],
            {"budget": " "},
            "/api/field_notes/notes",
            {"to": "rack 3", **_SENT, "budget": None},
        ),
    ],
    ids=["action", "extra-action", "no-number"],
)
async def test_a_form_sends_its_arguments_then_its_answers_as_the_dashboard_does(
    druks_db, position, answers, path, body
):
    ship = ui.Form(
        fields=parse_fields(FORM_TEXT),
        action=ui.Action(label="Ship", operation="write_note", arguments={"to": "rack 3"}),
        extra_actions=[ui.Action(label="Hold", operation="archive_note", arguments={"note_id": 7})],
    )
    _, approval = await _approval(druks_db, ship)

    request = await get_action_request(
        druks_db,
        approval.correlation_token,
        position,
        user_id="U7",
        values={**_ANSWERS, **answers},
    )

    assert (request["method"], request["path"], request["body"]) == ("POST", path, body)


@pytest.mark.parametrize(
    ("field", "answer", "path"),
    [
        (
            ui.TextField(name="note_id", label="Note"),
            "7?draft",
            "/api/field_notes/notes/7%3Fdraft/archive",
        ),
        (ui.NumberField(name="note_id", label="Note"), "0", "/api/field_notes/notes/0/archive"),
        (ui.NumberField(name="note_id", label="Note"), "7.5", "/api/field_notes/notes/7.5/archive"),
    ],
    ids=["encoded", "zero", "decimal"],
)
async def test_an_answer_fills_the_path_of_the_route_and_stays_out_of_its_body(
    druks_db, field, answer, path
):
    archive = ui.Form(fields=[field], action=ui.Action(label="Archive", operation="archive_note"))
    _, approval = await _approval(druks_db, archive)

    request = await get_action_request(
        druks_db, approval.correlation_token, [1, 0], user_id="U7", values={"note_id": answer}
    )

    assert (request["path"], request["body"]) == (path, {})
