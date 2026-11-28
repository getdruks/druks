import itertools
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from conftest import FORM_TEXT, Answer, bind_ambient_session, install_service, set_endpoint
from druks import ui
from druks.accounts.models import Account
from druks.chat.channels.slack.channel import SlackChannel
from druks.chat.channels.slack.constants import (
    IMAGE_BYTES_LIMIT,
    LAYOUT_LABEL,
    LINK_BUTTON_ACTION_ID,
    MARKDOWN_TEXT_LIMIT,
    MESSAGE_BLOCKS_LIMIT,
    SECTION_TEXT_LIMIT,
    TIMELINE_TITLE,
)
from druks.chat.channels.slack.delivery import (
    OMISSION_LINK,
    OMISSION_MESSAGE,
    PLAN_OMISSION_MESSAGE,
    TABLE_OMISSION_MESSAGE,
    cut_markdown,
    deliver,
    get_blocks,
    get_image_block,
)
from druks.chat.enums import ConversationSource
from druks.chat.exceptions import FormError
from druks.chat.forms import parse_fields
from druks.chat.models import Conversation
from druks.core.apis.slack import SlackClient
from druks.core.webhooks.slack import replace_buttons
from druks.durable.models import AgentCall, Artifact, Run
from druks.files.datastructures import File
from druks.files.models import FileRecord
from druks.files.storage import LocalFileStorage, get_file_storage
from druks.notifications.buttons import (
    decode_action,
    decode_button,
    encode_action,
    encode_button,
)
from druks.notifications.constants import CLOSED_MESSAGE
from druks.notifications.exceptions import (
    ChannelUnavailableError,
    DeliveryError,
    MalformedButtonError,
)
from druks.notifications.models import Notification
from druks.notifications.outbox import remove_buttons
from druks.notifications.services import (
    create_park_notification,
    get_rendered_ask,
    respond_to_notification,
)
from druks.teams.models import Team
from druks.testing import seed_call, seed_run
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from slack_sdk.errors import SlackApiError, SlackRequestError

_BOT_TOKEN = "xoxb-delivery"
# Where Slack takes the bytes of an upload.
_UPLOAD_URL = "https://files.slack.example/upload/{file_id}"


@pytest.fixture
def slack(monkeypatch):
    """Answer Slack's Web API and its upload host locally. The fake records each post in
    ``posts``, and each request in ``requests``, in order: the posts, and the steps of each
    upload. Slack answers that the message is in ``channel``. It names the files of the
    uploads F1, F2, and so on.

    ``error`` makes Slack refuse every post. Each item of ``refusals`` refuses the next post.
    Each item of ``upload_refusals`` refuses the address of the next upload. Each error of
    ``transfer_failures`` fails the next transfer of bytes."""
    fake = SimpleNamespace(
        posts=[],
        requests=[],
        error="",
        refusals=[],
        upload_refusals=[],
        transfer_failures=[],
        channel="C1",
    )
    file_ids = (f"F{number}" for number in itertools.count(1))

    async def api_call(client, api_method, **request):
        # Slack gets no argument that is None: the SDK leaves each one out.
        arguments = request.get("json") or request.get("params") or {}
        sent = {"bot_token": client.token, "method": api_method}
        sent |= {key: value for key, value in arguments.items() if value is not None}
        fake.requests.append(sent)
        if api_method == "files.getUploadURLExternal":
            if fake.upload_refusals:
                raise SlackApiError("refused", {"ok": False, "error": fake.upload_refusals.pop(0)})
            file_id = next(file_ids)
            return Answer(ok=True, file_id=file_id, upload_url=_UPLOAD_URL.format(file_id=file_id))
        if api_method == "files.completeUploadExternal":
            return Answer(ok=True, files=json.loads(sent["files"]))
        fake.posts.append(sent)
        error = fake.refusals.pop(0) if fake.refusals else fake.error
        if error:
            raise SlackApiError("refused", {"ok": False, "error": error})
        return Answer(ok=True, channel=fake.channel, ts="1.0")

    async def upload_file(client, *, url, data, **request):
        fake.requests.append({"method": "POST", "url": url, "data": data})
        if fake.transfer_failures:
            raise fake.transfer_failures.pop(0)
        return SimpleNamespace(status=200, body="")

    monkeypatch.setattr(SlackClient, "api_call", api_call)
    # The SDK sends the bytes of an upload to the address that Slack gives, not by api_call.
    monkeypatch.setattr(SlackClient, "_upload_file", upload_file)
    return fake


@pytest.fixture
async def operator(druks_db) -> Account:
    """The account that a message is for. It can use each app that no team lists."""
    return await Account.get_or_create(druks_db, "op@example.com")


async def test_plain_delivery_posts_the_body_and_the_link(druks_db, slack):
    client = SlackClient(token=_BOT_TOKEN)

    await deliver(
        client,
        "C1",
        "Review the PR",
        session=druks_db,
        account=None,
        deep_link="https://github.com/acme/app/7",
    )

    (post,) = slack.posts
    assert (post["bot_token"], post["channel"]) == (_BOT_TOKEN, "C1")
    assert post["text"] == "Review the PR\nhttps://github.com/acme/app/7"
    assert post["blocks"] == [{"type": "markdown", "text": post["text"]}]


async def test_a_body_too_long_for_slack_is_cut_and_keeps_its_link(druks_db, slack):
    link = "https://druks.example/runs/run-1"
    body = "Your run failed: " + "x" * 20_000

    await deliver(
        SlackClient(token=_BOT_TOKEN), "C1", body, session=druks_db, account=None, deep_link=link
    )

    (post,) = slack.posts
    assert len(post["text"]) == MARKDOWN_TEXT_LIMIT
    assert post["text"].startswith("Your run failed: xxx")
    assert post["text"].endswith(f"x\n\n…\n{link}")
    assert post["blocks"] == [{"type": "markdown", "text": post["text"]}]


CHOICES = [{"id": "1", "label": "Postgres"}, {"id": "2", "label": "MySQL"}]


async def test_a_long_reply_before_a_question_loses_its_end_and_the_question_stays(druks_db, slack):
    conversation = await _slack_chat(druks_db)
    reply = "I compared both.\n" * 1_000
    question = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body=f"{reply}\n\nWhich database?",
        question_offset=len(reply),
        actions=CHOICES,
    )

    await SlackChannel.post_buttons(druks_db, conversation, question)
    await replace_buttons(question.id, channel="C1", message_ts="1.0", outcome="<@U1> chose MySQL.")
    question.message_id = "C1:1.0"
    await SlackChannel.remove_buttons(druks_db, conversation, question)

    first_post, answer_edit, close_edit = slack.posts
    for post in slack.posts:
        assert len(post["text"]) <= MARKDOWN_TEXT_LIMIT
        assert post["text"].endswith("I compared both.\n\n…\n\nWhich database?")
        assert post["blocks"][0] == {"type": "markdown", "text": post["text"]}
    assert [button["text"]["text"] for button in first_post["blocks"][1]["elements"]] == [
        "Postgres",
        "MySQL",
    ]
    for edit, outcome in ((answer_edit, "<@U1> chose MySQL."), (close_edit, CLOSED_MESSAGE)):
        assert edit["blocks"][1:] == [
            {"type": "section", "text": {"type": "mrkdwn", "text": outcome}}
        ]


async def _slack_chat(druks_db) -> Conversation:
    """A chat with U1 in a thread of Slack room C1."""
    bind_ambient_session(druks_db)
    await install_service(
        "slack",
        access_token=_BOT_TOKEN,
        identity={"client_id": "c1"},
        secrets={"client_secret": "s"},
    )
    account = await Account.get_or_create(druks_db, "op@example.com")
    conversation = Conversation(
        account_id=account.id,
        source=ConversationSource.SLACK,
        user_id="U1",
        thread_id="C1:1700000000.000100",
    )
    druks_db.add(conversation)
    await druks_db.flush()
    return conversation


async def _form_question(druks_db) -> tuple[Conversation, Notification]:
    """A Slack chat, and the form of FORM_TEXT that its agent sent."""
    conversation = await _slack_chat(druks_db)
    question = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body="Where does it ship?",
        question_offset=0,
        fields=parse_fields(FORM_TEXT),
    )
    return conversation, question


def _label(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _option(label: str, value: str) -> dict:
    return {"text": _label(label), "value": value}


async def test_a_form_posts_an_input_for_each_field_and_one_submit_button(druks_db, slack):
    conversation, question = await _form_question(druks_db)

    await SlackChannel.post_buttons(druks_db, conversation, question)

    (post,) = slack.posts
    staging, production = _option("Staging", "staging"), _option("Production", "production")
    rack, shelf = _option("Rack", "rack"), _option("Shelf", "shelf")
    box = _option("Yes", "yes")
    assert post["blocks"] == [
        {"type": "markdown", "text": "Where does it ship?"},
        {
            "type": "input",
            "block_id": "title",
            "label": _label("Title"),
            "optional": False,
            "element": {
                "type": "plain_text_input",
                "multiline": False,
                "placeholder": _label("A short title"),
            },
        },
        {
            "type": "input",
            "block_id": "notes",
            "label": _label("Notes"),
            "hint": _label("Anything else to know."),
            "optional": True,
            "element": {"type": "plain_text_input", "multiline": True},
        },
        {
            "type": "input",
            "block_id": "budget",
            "label": _label("Budget"),
            "optional": True,
            "element": {"type": "plain_text_input", "initial_value": "10"},
        },
        {
            "type": "input",
            "block_id": "where",
            "label": _label("Where"),
            "optional": True,
            "element": {
                "type": "static_select",
                "options": [staging, production],
                "initial_option": staging,
            },
        },
        {
            "type": "input",
            "block_id": "tags",
            "label": _label("Tags"),
            "optional": True,
            "element": {
                "type": "multi_static_select",
                "options": [rack, shelf],
                "initial_options": [rack],
            },
        },
        {
            "type": "input",
            "block_id": "size",
            "label": _label("Size"),
            "optional": False,
            "element": {
                "type": "radio_buttons",
                "options": [_option("Small", "s"), _option("Large", "l")],
            },
        },
        {
            "type": "input",
            "block_id": "notify",
            "label": _label("Notify the owner"),
            "optional": True,
            "element": {"type": "checkboxes", "options": [box], "initial_options": [box]},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": _label("Submit"),
                    "action_id": encode_button(question.correlation_token, "submit"),
                }
            ],
        },
    ]


async def test_a_closed_or_answered_form_shows_its_question_without_inputs_or_buttons(
    druks_db, slack
):
    conversation, question = await _form_question(druks_db)
    await question.mark_acknowledged()

    # An answer closed the form before its post. The same answer closes it after the post.
    # Then the person answers it.
    await SlackChannel.post_buttons(druks_db, conversation, question)
    question.message_id = "C1:1.0"
    await SlackChannel.remove_buttons(druks_db, conversation, question)
    await replace_buttons(question.id, channel="C1", message_ts="1.0", outcome="<@U1> answered:")

    outcomes = [CLOSED_MESSAGE, CLOSED_MESSAGE, "<@U1> answered:"]
    assert [post["blocks"] for post in slack.posts] == [
        [
            {"type": "markdown", "text": "Where does it ship?"},
            {"type": "section", "text": {"type": "mrkdwn", "text": outcome}},
        ]
        for outcome in outcomes
    ]


async def test_slack_takes_a_form_of_48_fields_in_one_message_and_refuses_49(druks_db, slack):
    fields = [ui.TextField(name=f"f{index}", label="F").model_dump() for index in range(49)]

    SlackChannel.validate_fields(fields[:48])
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "Which ones?",
        session=druks_db,
        account=None,
        fields=fields[:48],
        token="t",
    )

    assert len(slack.posts[0]["blocks"]) == 50
    with pytest.raises(FormError, match="A Slack message holds 50 blocks"):
        SlackChannel.validate_fields(fields)


def _text_field(**arguments) -> list[dict]:
    return [ui.TextField(**{"name": "title", "label": "Title", **arguments}).model_dump()]


def _choice_field(field: type, options: list) -> list[dict]:
    return [field(name="choice", label="Choice", options=options).model_dump()]


def _options(count: int) -> list:
    return [ui.Option(f"Option {index}", value=f"option-{index}") for index in range(count)]


@pytest.mark.parametrize(
    ("make", "limit", "refusal"),
    [
        (lambda size: _text_field(name="n" * size), 255, "for the name of a field"),
        (lambda size: _text_field(label="L" * size), 2000, "for the label of a field"),
        (lambda size: _text_field(help_text="h" * size), 2000, "for the help text of a field"),
        (lambda size: _text_field(placeholder="p" * size), 150, "for the placeholder of a field"),
        (lambda size: _text_field(value="v" * size), 3000, "for the value of a field"),
        (
            lambda size: _choice_field(ui.SelectField, [ui.Option("O" * size, value="o")]),
            75,
            "for the option label of a field",
        ),
        (
            lambda size: _choice_field(ui.RadioField, [ui.Option("O", value="o" * size)]),
            150,
            "for the option value of a field",
        ),
        (
            lambda size: _choice_field(ui.SelectField, _options(size)),
            100,
            "has 101 options. Slack shows 100",
        ),
        (
            lambda size: _choice_field(ui.MultiSelectField, _options(size)),
            100,
            "has 101 options. Slack shows 100",
        ),
        (
            lambda size: _choice_field(ui.RadioField, _options(size)),
            10,
            "has 11 options. Slack shows 10",
        ),
    ],
    ids=[
        "name",
        "label",
        "help-text",
        "placeholder",
        "default",
        "option-label",
        "option-value",
        "select-options",
        "multi-select-options",
        "radio-options",
    ],
)
def test_slack_takes_a_form_at_each_limit_and_refuses_it_one_past(make, limit, refusal):
    SlackChannel.validate_fields(make(limit))

    with pytest.raises(FormError, match=re.escape(refusal)):
        SlackChannel.validate_fields(make(limit + 1))


@pytest.mark.parametrize(
    ("text", "ending"),
    [
        ("Run this:\n```bash\n" + "make test\n" * 100, "make test\n```\n\n…"),
        ("1. Run this:\n   ```bash\n" + "   make test\n" * 100, "   make test\n   ```\n\n…"),
        (
            "````markdown\n```python\nprint()\n```\n" + "more of the example\n" * 100,
            "more of the example\n````\n\n…",
        ),
        ("```inline``` code\n" + "a line\n" * 100, "a line\n\n…"),
    ],
    ids=["fence", "fence-in-a-list-item", "fence-in-a-fence", "inline-code"],
)
def test_a_cut_closes_a_fence_only_as_markdown_closes_it(text, ending):
    assert cut_markdown(text, 300).endswith(ending)


def test_a_cut_with_no_room_to_close_a_fence_leaves_the_fence_out():
    assert cut_markdown("```bash\n" + "make test\n" * 100, 6) == "\n\n…"


async def test_button_delivery_posts_one_button_per_action(druks_db, slack):
    actions = [{"id": "approve", "label": "Approve"}, {"id": "reject", "label": "Reject"}]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "review the plan",
        session=druks_db,
        account=None,
        actions=actions,
        token="tok_123",
    )

    (post,) = slack.posts
    assert post["text"] == "review the plan"
    markdown, action_block = post["blocks"]
    assert markdown == {"type": "markdown", "text": "review the plan"}
    assert [button["text"]["text"] for button in action_block["elements"]] == [
        "Approve",
        "Reject",
    ]
    for button, action in zip(action_block["elements"], actions, strict=True):
        assert decode_button(button["action_id"]) == ("tok_123", action["id"])


async def test_delivery_to_a_channel_the_bot_is_not_in_says_to_invite_it(druks_db, slack):
    slack.error = "not_in_channel"

    with pytest.raises(ChannelUnavailableError) as raised:
        await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello", session=druks_db, account=None)

    assert "Invite the bot to the channel" in str(raised.value)
    # The Slack error keeps the request headers. They carry the bot token.
    assert not raised.value.__cause__
    assert raised.value.__suppress_context__


async def test_a_refused_delivery_names_slack_s_reason(druks_db, slack):
    slack.error = "restricted_action"

    with pytest.raises(DeliveryError) as raised:
        await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello", session=druks_db, account=None)

    assert str(raised.value) == "Delivery failed: restricted_action."
    assert not raised.value.__cause__


async def test_a_delivery_slack_does_not_answer_names_the_failure(druks_db, monkeypatch):
    async def api_call(client, api_method, **request):
        raise TimeoutError

    monkeypatch.setattr(SlackClient, "api_call", api_call)

    with pytest.raises(DeliveryError) as raised:
        await deliver(SlackClient(token=_BOT_TOKEN), "C1", "hello", session=druks_db, account=None)

    assert str(raised.value) == "Delivery failed: TimeoutError."
    assert not raised.value.__cause__


async def test_delivery_in_a_thread_replies_there(druks_db, slack):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "hello",
        session=druks_db,
        account=None,
        thread_ts="1700000000.000100",
    )

    (post,) = slack.posts
    assert (post["channel"], post["thread_ts"]) == ("C1", "1700000000.000100")


def test_button_encoding_round_trips():
    for token, choice_id in [
        ("tok_123", "approve"),
        ("with.dot", "choice.with.dots"),
        ("unicode-✓", "weird/chars?&="),
        ("", "empty-token-still-round-trips"),
    ]:
        action_id = encode_button(token, choice_id)
        assert decode_button(action_id) == (token, choice_id)
        assert "/" not in action_id
        assert "?" not in action_id


def test_button_decode_rejects_malformed_input():
    for action_id in ("no-separator", "bad!chars.x", "a.b!!", "tok.choice.extra"):
        with pytest.raises(MalformedButtonError) as raised:
            decode_button(action_id)
        assert action_id not in str(raised.value)


def test_button_decode_rejects_non_string_input():
    # A tampered webhook payload can put null, a number, a list, or an object where the
    # action_id belongs. The typed error must cover those too.
    for value in (None, 123, ["a.b"], {"action_id": "a.b"}):
        with pytest.raises(MalformedButtonError):
            decode_button(value)


def test_the_id_of_an_action_at_the_top_names_its_block():
    # An id of this shape must decode, because a message in Slack keeps the ids that Druks posted.
    assert decode_action(f"{encode_button('t', '1')}.action") == ("t", [1])


async def test_delivery_with_a_message_ts_edits_that_message_and_shows_the_outcome(druks_db, slack):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "review the plan",
        session=druks_db,
        account=None,
        message_ts="1700000000.000200",
        outcome="Answer: Approve",
    )

    (post,) = slack.posts
    assert (post["method"], post["channel"], post["ts"]) == (
        "chat.update",
        "C1",
        "1700000000.000200",
    )
    assert post["text"] == "review the plan"
    assert post["blocks"] == [
        {"type": "markdown", "text": "review the plan"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Answer: Approve"}},
    ]


async def test_delivery_to_a_user_shows_the_message_to_that_person_only(druks_db, slack):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "This notification was already acknowledged.",
        session=druks_db,
        account=None,
        user="U7",
        thread_ts="1700000000.000100",
    )

    (post,) = slack.posts
    assert (post["method"], post["channel"], post["user"], post["thread_ts"]) == (
        "chat.postEphemeral",
        "C1",
        "U7",
        "1700000000.000100",
    )
    assert post["text"] == "This notification was already acknowledged."
    assert post["blocks"] == [{"type": "markdown", "text": post["text"]}]


async def _parked_review(druks_db, *, document: str = "", subject=None, **ask) -> Run:
    """A run parked on an in-app review. Its call wrote ``document`` as the run's artifact."""
    run = await seed_run(
        druks_db,
        kind=Summarize.kind,
        subject=subject,
        state="parked",
        input_gate="review",
        input_request={
            "presentation": "in_app",
            "controls": [{"id": "approve", "note_required": False}],
            "questions": [],
            "reply_fields": ["action", "answers", "note"],
            **ask,
        },
    )
    if document:
        call = await seed_call(druks_db, run, "summarize")
        await Artifact.record(
            druks_db,
            call_dir=call.call_dir,
            call_id=call.id,
            kind="markdown",
            title="Plan",
            content=document,
            event={},
        )
    return run


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_approval_writes_its_label_context_questions_and_document_in_order(druks_db):
    run = await _parked_review(
        druks_db,
        document="## Steps\nShip it.",
        context="The plan changes the schema.",
        questions=[
            {
                "id": "database",
                "prompt": "Which database?",
                "options": [
                    {"id": "postgres", "label": "Postgres", "recommended": True},
                    {"id": "mysql", "label": "MySQL", "recommended": False},
                ],
            },
            # An app's question can have no flag.
            {
                "id": "cache",
                "prompt": "Which cache?",
                "options": [{"id": "redis", "label": "Redis"}],
            },
        ],
    )

    ask = await get_rendered_ask(druks_db, run)

    assert ask["body"] == (
        "# Review: Plan\n\n"
        "The plan changes the schema.\n\n"
        "Which database?\n- Postgres (recommended)\n- MySQL\n\n"
        "Which cache?\n- Redis\n\n"
        "## Steps\nShip it."
    )
    # A context of text is one block of its own.
    assert ask["blocks"] == [
        {"block": "markdown", "text": "# Review: Plan"},
        {"block": "markdown", "text": "The plan changes the schema."},
        {"block": "markdown", "text": "Which database?\n- Postgres (recommended)\n- MySQL"},
        {"block": "markdown", "text": "Which cache?\n- Redis"},
        {"block": "markdown", "text": "## Steps\nShip it."},
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_approval_whose_document_is_gone_goes_out_without_it(druks_db):
    run = await _parked_review(druks_db, document="Ship it.", context="Check the schema.")
    artifact = await Artifact.get_latest_for_run(druks_db, run.id)
    call = await AgentCall.get(druks_db, artifact.agent_call_id)
    (call.call_dir / artifact.path).unlink()

    ask = await get_rendered_ask(druks_db, run)

    assert ask["body"] == "# Review: Plan\n\nCheck the schema."


async def test_an_approval_writes_a_block_context_as_a_list_and_a_table(druks_db, slack):
    set_endpoint("https://druks.example")
    note = await Note.create(body="Fan noise on rack 3.")
    context = [
        ui.Facts(
            [
                ui.Fact("Note", ui.TextValue("Rack 3", link=ui.Link(subject=note))),
                ui.Fact("Work", ui.SubjectStatus(note)),
            ]
        ),
        ui.Table(
            columns=[ui.TableColumn("Fan"), ui.TableColumn("Speed", align="end")],
            rows=[ui.TableRow(["front", ui.NumberValue(1200, unit="rpm")])],
        ),
    ]
    # The ask stores the blocks as ``review()`` writes them. Druks reads the status each time it
    # reads the ask.
    run = await _parked_review(
        druks_db,
        subject=note,
        context=_stored(*context),
    )

    ask = await get_rendered_ask(druks_db, run)
    await deliver(SlackClient(token=_BOT_TOKEN), "C1", ask["body"], session=druks_db, account=None)

    body = (
        "# Review\n\n"
        f"- **Note**: [Rack 3](https://druks.example/field_notes/notes/{note.id})\n"
        "- **Work**: needs you\n\n"
        "| Fan | Speed |\n| --- | ---: |\n| front | 1,200 rpm |"
    )
    assert ask["body"] == body
    (post,) = slack.posts
    assert post["blocks"] == [{"type": "markdown", "text": body}]


def _stored(*blocks) -> list[dict]:
    """The blocks as a notification stores them."""
    return [block.model_dump(mode="json", by_alias=True) for block in blocks]


def _action(*, operation: str = "archive_note", **data) -> ui.Action:
    """An action as ``review()`` stores it: with the page of its run."""
    return ui.Action(operation=operation, url="/runs/run-1", **data)


def _mrkdwn(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _url_button(label: str, url: str, action_id: str = LINK_BUTTON_ACTION_ID) -> dict:
    """The button that opens ``url``."""
    return {"type": "button", "text": _label(label), "url": url, "action_id": action_id}


def _link_button(label: str, url: str) -> dict:
    """The actions block of a button that opens ``url``."""
    return {"type": "actions", "elements": [_url_button(label, url)]}


def _image_block(url: str, alternative_text: str) -> dict:
    return {"type": "image", "image_url": url, "alt_text": alternative_text}


def _slack_file_block(file_id: str, alternative_text: str) -> dict:
    return {"type": "image", "slack_file": {"id": file_id}, "alt_text": alternative_text}


def _step(label: str, words: str = "to do", tone: str = "neutral") -> ui.ProgressStep:
    """A step of a progress whose status says ``words`` in ``tone``. By default, the step waits
    to start."""
    return ui.ProgressStep(label=label, status=ui.StatusValue(words, tone=tone))


def _task(number: int, title: str, status: str, details: str = "") -> dict:
    """The task of a plan at position ``number``, with ``details`` as rich text when it has
    some."""
    task = {"task_id": str(number), "title": title, "status": status}
    if details:
        task["details"] = _rich_text({"type": "text", "text": details})
    return task


def _item(title: str, minutes: int = 0, **data) -> ui.TimelineItem:
    """An item of a timeline that happened ``minutes`` after 09:00 UTC on 29 August 2026."""
    return ui.TimelineItem(
        when=datetime(2026, 8, 29, 9, tzinfo=UTC) + timedelta(minutes=minutes), title=title, **data
    )


def _list_uploads(slack) -> list[str]:
    """The name of each file whose upload Druks started, in order."""
    return [
        request["filename"]
        for request in slack.requests
        if request["method"] == "files.getUploadURLExternal"
    ]


async def _druks_file(name: str = "rack.png", content_type: str = "image/png") -> File:
    """A file that an agent made, such as a screenshot."""
    return await File.create(
        name=name,
        content_type=content_type,
        content=b"image bytes",
        app="field_notes",
        uploaded_by=None,
    )


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_approval_keeps_its_blocks_beside_their_text_form(druks_db):
    set_endpoint("https://druks.example")
    note = await Note.create(body="Fan noise on rack 3.")
    context = [
        ui.Facts(
            [
                ui.Fact("Note", ui.TextValue("Rack 3", link=ui.Link(subject=note))),
                ui.Fact("Work", ui.SubjectStatus(note)),
            ]
        ),
        ui.Link("All notes", page="notes"),
        ui.Link("Nowhere", page="nowhere"),
    ]
    run = await _parked_review(
        druks_db,
        document="## Steps\nShip it.",
        subject=note,
        context=_stored(*context),
        questions=[
            {"id": "rack", "prompt": "Which rack?", "options": [{"id": "3", "label": "Rack 3"}]}
        ],
    )

    notification_id = await create_park_notification(
        druks_db, run, note.identity, account_id=run.account_id
    )
    notification = await druks_db.get(Notification, notification_id)
    await druks_db.refresh(notification)

    assert notification.body == (
        f"### Field Notes\n\nnote {note.id}\n\n"
        "# Review: Plan\n\n"
        f"- **Note**: [Rack 3](https://druks.example/field_notes/notes/{note.id})\n"
        "- **Work**: needs you\n\n"
        "[All notes](https://druks.example/field_notes)\n\n"
        "Nowhere\n\n"
        "Which rack?\n- Rack 3\n\n"
        "## Steps\nShip it."
    )
    assert ui.Stack(notification.blocks).to_markdown() == notification.body
    head, facts, notes, nowhere_link, question, document = notification.blocks
    card, label = head["blocks"]
    assert (card["title"], card["description"]) == ("Field Notes", f"note {note.id}")
    assert (label["text"], question["text"], document["text"]) == (
        "# Review: Plan",
        "Which rack?\n- Rack 3",
        "## Steps\nShip it.",
    )
    # A link keeps only the address that it resolved to. A link with no address keeps its
    # page.
    rack, _ = facts["facts"]
    destinations = [
        (link["url"], link["page"], link["subject"])
        for link in (rack["value"]["link"], notes, nowhere_link)
    ]
    assert destinations == [
        (f"/field_notes/notes/{note.id}", "", None),
        ("/field_notes", "", None),
        ("", "nowhere", None),
    ]


async def test_facts_metrics_a_divider_and_a_link_go_out_as_slack_s_own_blocks(druks_db, slack):
    facts = [ui.Fact(f"Fan {number}", ui.NumberValue(number, unit="rpm")) for number in range(12)]
    facts += [
        ui.Fact("State", ui.StatusValue("Stalled", tone="danger")),
        ui.Fact(
            "Rack", ui.TextValue("Rack <3>", link=ui.Link(url="https://racks.example/3?a=1&b=2"))
        ),
    ]
    blocks = _stored(
        ui.Markdown("# Review"),
        ui.Facts(facts, title="Fans"),
        ui.Divider(),
        ui.Metrics([ui.Metric("Noise", ui.NumberValue(72, unit="dB"))]),
        ui.Link("Pull request", url="https://github.com/acme/app/pull/7"),
        ui.List(["front"]),
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "the text form",
        session=druks_db,
        account=None,
        blocks=blocks,
    )

    (post,) = slack.posts
    fields = [_mrkdwn(f"*Fan {number}*\n{number} rpm") for number in range(12)]
    fields += [
        _mrkdwn("*State*\nStalled"),
        _mrkdwn("*Rack*\n<https://racks.example/3?a=1&amp;b=2|Rack &lt;3&gt;>"),
    ]
    assert post["text"] == "the text form"
    assert post["blocks"] == [
        {"type": "markdown", "text": "# Review"},
        {"type": "section", "text": _mrkdwn("*Fans*"), "fields": fields[:10]},
        {"type": "section", "fields": fields[10:]},
        {"type": "divider"},
        {"type": "section", "fields": [_mrkdwn("*Noise*\n72 dB")]},
        _link_button("Pull request", "https://github.com/acme/app/pull/7"),
        {"type": "markdown", "text": "- front"},
    ]


@pytest.mark.parametrize(
    ("make", "limit"),
    [
        # The field writes the label in bold on a line of its own: "*Log*\n".
        (lambda size: ui.Facts([ui.Fact("Log", "x" * (size - 6))]), 2000),
        # The section writes the title in bold: "*" around it.
        (lambda size: ui.Facts([ui.Fact("Log", "x")], title="T" * (size - 2)), 3000),
        (lambda size: ui.Link("L" * size, url="https://docs.example/a"), 75),
        (lambda size: ui.Link("Docs", url="https://docs.example/" + "a" * (size - 21)), 3000),
        (
            lambda size: ui.Table(
                columns=[ui.TableColumn(f"Fan {number}") for number in range(size)],
                rows=[ui.TableRow(["front"] * size)],
            ),
            20,
        ),
        (
            lambda size: ui.Chart(
                title="Speeds",
                categories=[f"Day {day}" for day in range(20)],
                series=[ui.ChartSeries(label=str(fan), points=[fan] * 20) for fan in range(size)],
            ),
            12,
        ),
        (
            lambda size: ui.Chart(
                title="Speeds",
                categories=[f"Day {day}" for day in range(size)],
                series=[ui.ChartSeries(label=str(fan), points=[fan] * size) for fan in range(12)],
            ),
            20,
        ),
        (
            lambda size: ui.Image(
                url="https://shots.example/" + "a" * (size - 26) + ".png",
                alternative_text="Rack 3.",
            ),
            3000,
        ),
        (
            lambda size: ui.Image(
                url="https://shots.example/rack.png", alternative_text="A" * size
            ),
            2000,
        ),
        (
            lambda size: ui.Image(
                url="https://shots.example/rack.png", alternative_text="Rack 3.", caption="C" * size
            ),
            2000,
        ),
    ],
    ids=[
        "field",
        "section-text",
        "button-text",
        "button-address",
        "table-columns",
        "chart-series",
        "chart-points",
        "image-address",
        "image-alt-text",
        "image-title",
    ],
)
async def test_a_block_takes_slack_s_blocks_up_to_a_limit_and_its_text_form_past_it(
    druks_db, slack, make, limit
):
    client = SlackClient(token=_BOT_TOKEN)
    block_past_limit = make(limit + 1)

    await deliver(client, "C1", "text", session=druks_db, account=None, blocks=_stored(make(limit)))
    await deliver(
        client, "C1", "text", session=druks_db, account=None, blocks=_stored(block_past_limit)
    )

    native_post, text_post = slack.posts
    assert native_post["blocks"][0]["type"] in (
        "section",
        "actions",
        "data_table",
        "data_visualization",
        "image",
    )
    assert text_post["blocks"] == [{"type": "markdown", "text": block_past_limit.to_markdown()}]


async def test_blocks_past_the_room_of_a_message_go_out_as_text_in_its_last_block(druks_db, slack):
    links = [
        ui.Link(f"Link {number}", url=f"https://docs.example/{number}") for number in range(60)
    ]
    page = "https://druks.example/runs/run-1"

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(*links),
        actions=[{"id": "approve", "label": "Approve"}],
        token="t",
        deep_link=page,
    )

    (post,) = slack.posts
    *buttons, rest, actions = post["blocks"]
    # The Approve button takes a block of the room. One more block stays for the link.
    assert len(post["blocks"]) == MESSAGE_BLOCKS_LIMIT
    assert buttons == [
        _link_button(f"Link {number}", f"https://docs.example/{number}") for number in range(48)
    ]
    assert rest == {
        "type": "markdown",
        "text": "\n\n".join(link.to_markdown() for link in links[48:]) + f"\n{page}",
    }
    assert actions["elements"][0]["text"] == _label("Approve")


@pytest.mark.parametrize(
    ("deep_link", "line"),
    [
        (
            "https://druks.example/runs/run-1",
            f"{OMISSION_MESSAGE} Read all of it in Druks: https://druks.example/runs/run-1",
        ),
        (None, OMISSION_MESSAGE),
    ],
    ids=["endpoint", "no-endpoint"],
)
async def test_markdown_past_slack_s_limit_loses_its_end_to_a_line_that_says_so(
    druks_db, slack, deep_link, line
):
    document = "Run this:\n```bash\n" + "make test\n" * 2_000
    blocks = _stored(
        ui.Markdown("# Review"),
        ui.Divider(),
        ui.Markdown(document),
        ui.Link("Docs", url="https://docs.example/a"),
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=blocks,
        actions=[{"id": "approve", "label": "Approve"}],
        token="t",
        deep_link=deep_link,
    )

    (post,) = slack.posts
    # The cut removes the end of the document and the link after it.
    label, divider, cut, actions = post["blocks"]
    assert (label, divider) == ({"type": "markdown", "text": "# Review"}, {"type": "divider"})
    assert cut["text"].endswith(f"make test\n```\n\n…\n{line}")
    assert len(label["text"]) + len(cut["text"]) <= MARKDOWN_TEXT_LIMIT
    assert actions["type"] == "actions"


async def test_markdown_at_slack_s_limit_with_no_link_goes_out_whole(druks_db, slack):
    text = "x" * MARKDOWN_TEXT_LIMIT

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(ui.Markdown(text)),
    )

    (post,) = slack.posts
    assert post["blocks"] == [{"type": "markdown", "text": text}]


@pytest.mark.parametrize(
    ("arguments", "place"),
    [
        (
            {"thread_ts": "1700000000.000100", "actions": [{"id": "approve", "label": "Approve"}]},
            ("chat.postMessage", "C1", "1700000000.000100", None),
        ),
        (
            {"message_ts": "1700000000.000200", "outcome": "<@U1> chose Approve."},
            ("chat.update", "C1", None, "1700000000.000200"),
        ),
    ],
    ids=["post", "edit"],
)
async def test_a_refusal_of_the_blocks_sends_their_text_form_in_the_same_place(
    druks_db, slack, arguments, place
):
    slack.refusals = ["invalid_blocks"]
    blocks = _stored(
        ui.Facts([ui.Fact("Fan", "front")]), ui.Link("Docs", url="https://docs.example/a")
    )

    answer = await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "the text form",
        session=druks_db,
        account=None,
        blocks=blocks,
        token="t",
        deep_link="https://druks.example/runs/run-1",
        **arguments,
    )

    refused_post, text_post = slack.posts
    assert refused_post["blocks"][:2] == [
        {"type": "section", "fields": [_mrkdwn("*Fan*\nfront")]},
        _link_button("Docs", "https://docs.example/a"),
    ]
    # The text form keeps the buttons, or the outcome in their place.
    assert text_post["blocks"] == [
        {"type": "markdown", "text": "the text form\nhttps://druks.example/runs/run-1"},
        refused_post["blocks"][-1],
    ]
    for post in (refused_post, text_post):
        assert (post["method"], post["channel"], post.get("thread_ts"), post.get("ts")) == place
    assert answer == {"ok": True, "channel": "C1", "ts": "1.0"}


@pytest.mark.parametrize(
    ("reason", "error", "attempts"),
    [
        ("invalid_blocks", DeliveryError, ["divider", "markdown"]),
        ("not_in_channel", ChannelUnavailableError, ["divider"]),
    ],
    ids=["refused-text-form", "channel"],
)
async def test_a_failed_post_of_blocks_tries_their_text_form_once_and_only_on_a_refusal(
    druks_db, slack, reason, error, attempts
):
    slack.error = reason

    with pytest.raises(error):
        await deliver(
            SlackClient(token=_BOT_TOKEN),
            "C1",
            "text",
            session=druks_db,
            account=None,
            blocks=_stored(ui.Divider()),
        )

    assert [post["blocks"][0]["type"] for post in slack.posts] == attempts


async def test_an_answer_or_a_close_keeps_the_facts_and_the_links_of_an_approval(druks_db, slack):
    conversation = await _slack_chat(druks_db)
    approval = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="gate.parked",
        body="# Review\n\n- **Fan**: front\n\n[Docs](https://docs.example/a)",
        blocks=_stored(
            ui.Markdown("# Review"),
            ui.Facts([ui.Fact("Fan", "front")]),
            ui.Link("Docs", url="https://docs.example/a"),
        ),
        actions=[{"id": "approve", "label": "Approve"}],
    )

    await SlackChannel.post_buttons(druks_db, conversation, approval)
    await replace_buttons(
        approval.id, channel="C1", message_ts="1.0", outcome="<@U1> chose Approve."
    )
    approval.message_id = "C1:1.0"
    await SlackChannel.remove_buttons(druks_db, conversation, approval)

    first_post, answer_edit, close_edit = slack.posts
    native_blocks = [
        {"type": "markdown", "text": "# Review"},
        {"type": "section", "fields": [_mrkdwn("*Fan*\nfront")]},
        _link_button("Docs", "https://docs.example/a"),
    ]
    assert first_post["blocks"][:-1] == native_blocks
    assert first_post["blocks"][-1]["elements"][0]["text"] == _label("Approve")
    for edit, outcome in ((answer_edit, "<@U1> chose Approve."), (close_edit, CLOSED_MESSAGE)):
        assert edit["blocks"] == [*native_blocks, {"type": "section", "text": _mrkdwn(outcome)}]


async def test_a_link_button_opens_its_page_and_a_link_with_no_address_reads_as_words(
    druks_db, slack
):
    note = await Note.create(body="Fan noise on rack 3.")
    context = [
        ui.Link("All notes", page="notes"),
        ui.Link("The note", subject=note),
        ui.Link("Docs", url="https://docs.example/a"),
        ui.Link("Nowhere", page="nowhere"),
    ]
    run = await _parked_review(druks_db, context=_stored(*context))
    blocks = (await get_rendered_ask(druks_db, run))["blocks"]
    client = SlackClient(token=_BOT_TOKEN)

    set_endpoint("https://druks.example")
    await deliver(client, "C1", "text", session=druks_db, account=None, blocks=blocks)
    set_endpoint("")
    await deliver(client, "C1", "text", session=druks_db, account=None, blocks=blocks)

    post_with_endpoint, post_without_endpoint = slack.posts
    assert post_with_endpoint["blocks"] == [
        {"type": "markdown", "text": "# Review"},
        _link_button("All notes", "https://druks.example/field_notes"),
        _link_button("The note", f"https://druks.example/field_notes/notes/{note.id}"),
        _link_button("Docs", "https://docs.example/a"),
        {"type": "markdown", "text": "Nowhere"},
    ]
    # An installation with no endpoint has no address for a page of the dashboard.
    assert post_without_endpoint["blocks"] == [
        {"type": "markdown", "text": "# Review\n\nAll notes\n\nThe note"},
        _link_button("Docs", "https://docs.example/a"),
        {"type": "markdown", "text": "Nowhere"},
    ]


async def test_an_action_goes_out_as_a_button_that_names_its_position_and_slack_confirms_it(
    druks_db, slack
):
    blocks = _stored(
        ui.Markdown("# Review"),
        ui.Action(
            label="Archive",
            operation="archive_note",
            arguments={"note_id": 7},
            tone="danger",
            confirm="Archive the note?",
        ),
        ui.Action(label="Clear gist", operation="clear_gist", arguments={"note_id": 7}),
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=blocks,
        token="t",
    )

    (post,) = slack.posts
    confirm = {
        "title": _label("Archive"),
        "text": _label("Archive the note?"),
        "confirm": _label("Archive"),
        "deny": _label("Back"),
    }
    archive = {"type": "button", "text": _label("Archive"), "action_id": encode_action("t", 1)}
    clear = {"type": "button", "text": _label("Clear gist"), "action_id": encode_action("t", 2)}
    assert post["blocks"] == [
        {"type": "markdown", "text": "# Review"},
        {"type": "actions", "elements": [{**archive, "confirm": confirm}]},
        {"type": "actions", "elements": [clear]},
    ]


@pytest.mark.parametrize(
    ("make", "limit"),
    [
        (lambda size: _action(label="A" * size), 75),
        (lambda size: _action(label="A" * size, confirm="Sure?"), 30),
        (lambda size: _action(label="Archive", confirm="C" * size), 300),
    ],
    ids=["label", "label-that-confirms", "confirm"],
)
async def test_an_action_takes_a_button_up_to_a_limit_and_its_label_linked_to_the_run_past_it(
    druks_db, slack, make, limit
):
    set_endpoint("https://druks.example")
    client = SlackClient(token=_BOT_TOKEN)
    page = "https://druks.example/runs/run-1"

    await deliver(
        client,
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(make(limit)),
        token="t",
        deep_link=page,
    )
    await deliver(
        client,
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(make(limit + 1)),
        token="t",
        deep_link=page,
    )

    button_post, text_post = slack.posts
    assert button_post["blocks"][0]["type"] == "actions"
    label = make(limit + 1).label
    assert text_post["blocks"] == [{"type": "markdown", "text": f"[{label}]({page})\n{page}"}]


async def test_actions_past_the_room_of_a_message_go_out_as_their_labels_linked_to_the_run(
    druks_db, slack
):
    set_endpoint("https://druks.example")
    actions = [_action(label=f"Archive {number}") for number in range(60)]
    page = "https://druks.example/runs/run-1"

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(*actions),
        token="t",
        deep_link=page,
    )

    (post,) = slack.posts
    *buttons, rest = post["blocks"]
    assert [button["elements"][0]["action_id"] for button in buttons] == [
        encode_action("t", number) for number in range(49)
    ]
    labels = [f"[Archive {number}]({page})" for number in range(49, 60)]
    assert rest == {"type": "markdown", "text": "\n\n".join(labels) + f"\n{page}"}


async def test_a_table_that_selects_rows_goes_out_as_its_text(druks_db, slack):
    set_endpoint("https://druks.example")
    table = ui.Table(
        columns=[ui.TableColumn("Note")],
        rows=[ui.TableRow(["Fan noise"], key="7")],
        select="note_ids",
        actions=[_action(label="Archive selected", operation="archive_notes")],
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(table),
        token="t",
    )

    (post,) = slack.posts
    page = "https://druks.example/runs/run-1"
    text = f"[Archive selected]({page})\n\n| Note |\n| --- |\n| Fan noise |"
    assert post["blocks"] == [{"type": "markdown", "text": text}]


def _form(**data) -> ui.Form:
    """A form as ``review()`` stores it. Each of its actions holds the page of its run."""
    return ui.Form(
        **{
            "title": "Ship it",
            "fields": [ui.TextField(name="title", label="Title", is_required=True)],
            "action": _action(label="Ship", operation="write_note"),
            **data,
        }
    )


async def test_a_form_and_an_action_with_fields_go_out_as_inputs_above_their_buttons(
    druks_db, slack
):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(
            ui.Markdown("# Review"),
            _form(
                extra_actions=[
                    _action(label="Hold", operation="clear_gist", confirm="Hold the note?")
                ]
            ),
            # The action shares the name of a field with the form, and with the approval's note.
            _action(
                label="Rename",
                operation="write_note",
                fields=[
                    ui.TextField(name="title", label="New title", value="Rack 3"),
                    ui.TextAreaField(name="note", label="Why", is_required=True),
                ],
            ),
        ),
        actions=[_APPROVE],
        fields=[_NOTE],
        token="t",
    )

    (post,) = slack.posts
    assert post["blocks"] == [
        {"type": "markdown", "text": "# Review"},
        {
            "type": "input",
            "block_id": "1.title",
            "label": _label("Title"),
            "optional": False,
            "element": {"type": "plain_text_input", "multiline": False},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": _label("Ship"),
                    "action_id": encode_action("t", 1, 0),
                },
                {
                    "type": "button",
                    "text": _label("Hold"),
                    "action_id": encode_action("t", 1, 1),
                    "confirm": {
                        "title": _label("Hold"),
                        "text": _label("Hold the note?"),
                        "confirm": _label("Hold"),
                        "deny": _label("Back"),
                    },
                },
            ],
        },
        {
            "type": "input",
            "block_id": "2.title",
            "label": _label("New title"),
            "optional": True,
            "element": {"type": "plain_text_input", "multiline": False, "initial_value": "Rack 3"},
        },
        {
            "type": "input",
            "block_id": "2.note",
            "label": _label("Why"),
            "optional": False,
            "element": {"type": "plain_text_input", "multiline": True},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": _label("Rename"), "action_id": encode_action("t", 2)}
            ],
        },
        {
            "type": "input",
            "block_id": "note",
            "label": _label("Note"),
            "optional": True,
            "element": {"type": "plain_text_input", "multiline": True},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": _label("Approve"),
                    "action_id": encode_button("t", "approve"),
                }
            ],
        },
    ]


@pytest.mark.parametrize(
    "block",
    [
        _form(submit="change"),
        _form(fields=[ui.RadioField(name="size", label="Size", options=_options(11))]),
        _form(extra_actions=[_action(label="H" * 76, operation="clear_gist")]),
        # Slack has no input for a file.
        _form(fields=[ui.UploadField(name="photo", label="Photo")]),
    ],
    ids=["sends-on-change", "input-past-a-limit", "button-past-a-limit", "field-with-no-input"],
)
async def test_a_form_that_slack_cannot_draw_whole_goes_out_as_its_text_with_no_input(
    druks_db, slack, block
):
    set_endpoint("https://druks.example")

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(block),
        token="t",
    )

    (post,) = slack.posts
    assert post["blocks"] == [{"type": "markdown", "text": block.to_markdown()}]
    assert "https://druks.example/runs/run-1" in block.to_markdown()


@pytest.mark.parametrize(
    ("actions", "types"),
    [(43, ["input"] * 5 + ["actions"]), (44, ["markdown"])],
    ids=["room", "no-room"],
)
async def test_a_form_takes_inputs_only_when_the_message_has_room_for_all_of_them(
    druks_db, slack, actions, types
):
    # Five inputs and the buttons take six blocks. One block stays free for the link.
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(
            *(_action(label=f"Archive {number}") for number in range(actions)),
            _form(fields=[ui.TextField(name=f"f{number}", label="F") for number in range(5)]),
        ),
        token="t",
    )

    (post,) = slack.posts
    assert [block["type"] for block in post["blocks"][actions:]] == types


@pytest.mark.parametrize(
    ("endpoint", "label"),
    [("https://druks.example", "[Archive](https://druks.example/runs/run-1)"), ("", "Archive")],
    ids=["endpoint", "no-endpoint"],
)
async def test_an_approval_keeps_its_action_and_its_text_links_the_label_to_the_run(
    druks_db, slack, endpoint, label
):
    set_endpoint(endpoint)
    # The action runs on the run's page, and not on the page that it opens after it runs.
    archive = _action(
        label="Archive",
        arguments={"note_id": 7},
        link=ui.Link(url="https://notes.example/archived"),
    )
    run = await _parked_review(druks_db, context=_stored(archive))
    notification_id = await create_park_notification(
        druks_db,
        run,
        {"type": "note", "id": 7},
        account_id=run.account_id,
        deep_link=run.get_link(),
    )
    notification = await druks_db.get(Notification, notification_id)
    slack.refusals = ["invalid_blocks"]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        notification.body,
        session=druks_db,
        account=None,
        blocks=notification.blocks,
        token=notification.correlation_token,
        deep_link=notification.deep_link,
    )

    text = f"### Field Notes\n\n# Review\n\n{label}"
    assert notification.body == text
    assert notification.blocks[1] == archive.model_dump(mode="json", by_alias=True)
    refused_post, text_post = slack.posts
    # The card of the run comes before the label, and the action keeps its position.
    assert refused_post["blocks"][2]["elements"][0]["action_id"] == encode_action(
        notification.correlation_token, 1
    )
    # Slack refuses the blocks. Their text goes in their place.
    assert text_post["blocks"][0]["text"].startswith(text)


CONTROLS = [
    {"id": "approve", "note_required": False},
    {"id": "request_changes", "note_required": True},
]
DATABASE = {
    "id": "database",
    "prompt": "Which database?",
    "options": [
        {"id": "postgres", "label": "Postgres", "recommended": True},
        {"id": "mysql", "label": "MySQL", "recommended": False},
    ],
}


async def _question_after_blocks(
    druks_db, *, reply: str = "I compared both."
) -> tuple[Conversation, Notification]:
    """A Slack chat, and the question that its agent asked after a reply and its blocks, as
    the end of the turn makes it."""
    conversation = await _slack_chat(druks_db)
    facts = ui.Facts([ui.Fact("Postgres", "16"), ui.Fact("MySQL", "8")], title="Versions")
    blocks = _stored(ui.Markdown(reply), facts)
    text = ui.Stack(blocks).to_markdown()
    question = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="question.asked",
        body=f"{text}\n\nWhich database?",
        blocks=blocks,
        question_offset=len(text),
        actions=CHOICES,
    )
    return conversation, question


async def test_a_question_after_blocks_comes_in_their_message_above_its_buttons(druks_db, slack):
    conversation, question = await _question_after_blocks(druks_db)

    await SlackChannel.post_buttons(druks_db, conversation, question)

    (post,) = slack.posts
    blocks = post["blocks"]
    assert [block["type"] for block in blocks] == ["markdown", "section", "markdown", "actions"]
    assert blocks[2] == {"type": "markdown", "text": "Which database?"}


async def test_a_question_after_blocks_too_long_for_slack_stays_whole_with_its_buttons(
    druks_db, slack
):
    conversation, question = await _question_after_blocks(
        druks_db, reply="I compared both.\n" * 1_000
    )

    await SlackChannel.post_buttons(druks_db, conversation, question)

    (post,) = slack.posts
    # The cut removes the end of the reply and the facts after it.
    cut, question_block, buttons = post["blocks"]
    assert cut["text"].endswith(f"I compared both.\n\n…\n{OMISSION_MESSAGE}")
    assert len(cut["text"]) + len(question_block["text"]) <= MARKDOWN_TEXT_LIMIT
    assert question_block == {"type": "markdown", "text": "Which database?"}
    assert buttons["type"] == "actions"


async def _approval(druks_db, *, is_in_a_chat: bool = False, **ask) -> Notification:
    """The notification that a run's park on a review made. It goes to the run's account, or
    to the thread of a Slack chat with U1."""
    bind_ambient_session(druks_db)
    await install_service(
        "slack",
        access_token=_BOT_TOKEN,
        identity={"client_id": "c1"},
        secrets={"client_secret": "s"},
    )
    run = await _parked_review(druks_db, controls=CONTROLS, **ask)
    destination = {"account_id": run.account_id}
    if is_in_a_chat:
        conversation = Conversation(
            account_id=run.account_id,
            source=ConversationSource.SLACK,
            user_id="U1",
            thread_id="C1:1700000000.000100",
        )
        druks_db.add(conversation)
        await druks_db.flush()
        destination = {"conversation_id": conversation.id}
    notification_id = await create_park_notification(
        druks_db, run, {"type": "note", "id": 1}, **destination
    )
    return await druks_db.get(Notification, notification_id)


async def test_an_approval_shows_its_inputs_above_every_control_until_its_answer(druks_db, slack):
    approval = await _approval(druks_db, questions=[DATABASE])
    token = approval.correlation_token
    outcome = "<@U1> chose Request changes.\nWhich database?: MySQL\nNote: Keep the queue."
    outcome += "\nThen retry it." * 400

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        approval.body,
        session=druks_db,
        account=None,
        actions=approval.actions,
        fields=approval.fields,
        token=token,
    )
    await replace_buttons(approval.id, channel="C1", message_ts="1.0", outcome=outcome)
    approval.message_id = "C1:1.0"
    await remove_buttons(druks_db, approval)

    first_post, answer_edit, close_edit = slack.posts
    assert first_post["blocks"][1:] == [
        {
            "type": "input",
            "block_id": "question:database",
            "label": _label("Which database?"),
            "optional": True,
            "element": {
                "type": "static_select",
                "options": [_option("Postgres", "postgres"), _option("MySQL", "mysql")],
            },
        },
        {
            "type": "input",
            "block_id": "note",
            "label": _label("Note"),
            "optional": True,
            "element": {"type": "plain_text_input", "multiline": True},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": _label("Approve"),
                    "action_id": encode_button(token, "approve"),
                },
                {
                    "type": "button",
                    "text": _label("Request changes"),
                    "action_id": encode_button(token, "request_changes"),
                },
            ],
        },
    ]
    # The first post has no blocks. The edits draw the stored blocks, where the card of the
    # run is a block of its own.
    content = [
        {"type": "card", "title": _label("Field Notes")},
        {"type": "markdown", "text": approval.body.removeprefix("### Field Notes\n\n")},
    ]
    (*answer_content, section) = answer_edit["blocks"]
    assert answer_content == content
    assert len(section["text"]["text"]) <= SECTION_TEXT_LIMIT
    assert section["text"]["text"].startswith(outcome[:80])
    assert section["text"]["text"].endswith("Then retry it.\n\n…")
    assert close_edit["blocks"] == [
        *content,
        {"type": "section", "text": {"type": "mrkdwn", "text": CLOSED_MESSAGE}},
    ]


async def test_a_thread_approval_shows_its_inputs_with_every_control_until_it_closes(
    druks_db, slack
):
    approval = await _approval(druks_db, is_in_a_chat=True, questions=[DATABASE])
    conversation = await druks_db.get(Conversation, approval.conversation_id)

    await SlackChannel.post_buttons(druks_db, conversation, approval)
    approval.message_id = "C1:1.0"
    await SlackChannel.remove_buttons(druks_db, conversation, approval)
    # An answer elsewhere closed the approval before its post.
    await approval.mark_acknowledged()
    await SlackChannel.post_buttons(druks_db, conversation, approval)

    first_post, *close_edits = slack.posts
    assert (first_post["channel"], first_post["thread_ts"]) == ("C1", "1700000000.000100")
    assert [block.get("block_id") for block in first_post["blocks"]] == [
        None,
        None,
        "question:database",
        "note",
        None,
    ]
    assert [button["text"]["text"] for button in first_post["blocks"][-1]["elements"]] == [
        "Approve",
        "Request changes",
    ]
    for post in close_edits:
        assert post["blocks"] == [
            {"type": "card", "title": _label("Field Notes")},
            {"type": "markdown", "text": approval.body.removeprefix("### Field Notes\n\n")},
            {"type": "section", "text": {"type": "mrkdwn", "text": CLOSED_MESSAGE}},
        ]


async def test_an_approval_s_actions_are_buttons_until_it_closes_and_the_line_of_one_that_ran_stays(
    druks_db, slack
):
    archive = _action(label="Archive", arguments={"note_id": 7})
    clear = _action(label="Clear gist", operation="clear_gist", arguments={"note_id": 7})
    approval = await _approval(druks_db, is_in_a_chat=True, context=_stored(archive, clear))
    conversation = await druks_db.get(Conversation, approval.conversation_id)
    token = approval.correlation_token

    await SlackChannel.post_buttons(druks_db, conversation, approval)
    await approval.mark_action_run([1], username="member@example.com")
    await druks_db.commit()
    await replace_buttons(approval.id, channel="C1", message_ts="1.0", outcome="")
    await replace_buttons(
        approval.id, channel="C1", message_ts="1.0", outcome="<@U1> chose Approve."
    )
    approval.message_id = "C1:1.0"
    await SlackChannel.remove_buttons(druks_db, conversation, approval)

    post, redraw, answer_edit, close_edit = slack.posts
    card = {"type": "card", "title": _label("Field Notes")}
    clear_button = {
        "type": "actions",
        "elements": [
            {"type": "button", "text": _label("Clear gist"), "action_id": encode_action(token, 2)}
        ],
    }
    assert post["blocks"][:4] == [
        card,
        {"type": "markdown", "text": "# Review"},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": _label("Archive"), "action_id": encode_action(token, 1)}
            ],
        },
        clear_button,
    ]
    # The approval keeps its inputs and buttons beside the action that did not run.
    assert redraw["blocks"][:3] == [
        card,
        {"type": "markdown", "text": "# Review\n\nmember@example.com ran Archive."},
        clear_button,
    ]
    assert [block.get("block_id") for block in redraw["blocks"][3:]] == ["note", None]
    # An outcome replaces every button. With no endpoint, an action shows its label.
    for edit, outcome in ((answer_edit, "<@U1> chose Approve."), (close_edit, CLOSED_MESSAGE)):
        assert edit["blocks"] == [
            card,
            {
                "type": "markdown",
                "text": "# Review\n\nmember@example.com ran Archive.\n\nClear gist",
            },
            {"type": "section", "text": _mrkdwn(outcome)},
        ]


async def test_an_approval_input_shows_the_start_of_a_long_label_and_keeps_each_id(
    druks_db, slack, monkeypatch
):
    # The longest question and options that a plan takes. The labels differ past the cut.
    question, first_id, second_id = "q" * 64, "a" * 64, "b" * 64
    prompt, first_label, second_label = "P" * 2048, "O" * 255 + "1", "O" * 255 + "2"
    approval = await _approval(
        druks_db,
        questions=[
            {
                "id": question,
                "prompt": prompt,
                "options": [
                    {"id": first_id, "label": first_label, "recommended": True},
                    {"id": second_id, "label": second_label, "recommended": False},
                ],
            }
        ],
    )
    replies = []

    async def resume(self, **reply):
        replies.append(reply)

    monkeypatch.setattr(Run, "resume", resume)

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        approval.body,
        session=druks_db,
        account=None,
        actions=approval.actions,
        fields=approval.fields,
        token=approval.correlation_token,
    )
    (post,) = slack.posts
    markdown, select, _, _ = post["blocks"]
    # Slack returns the block's id and the chosen option's value.
    await respond_to_notification(
        druks_db,
        approval.correlation_token,
        {"control": "approve"},
        user_id="U1",
        account_id=None,
        values={select["block_id"]: select["element"]["options"][1]["value"]},
        has_inputs=True,
    )

    assert f"{prompt}\n- {first_label} (recommended)\n- {second_label}" in markdown["text"]
    assert select["label"] == _label("P" * 1999 + "…")
    cut = "O" * 74 + "…"
    assert select["element"]["options"] == [_option(cut, first_id), _option(cut, second_id)]
    assert replies == [
        {"action": "approve", "answers": {question: second_id}, "note": "", "answered_by": ""}
    ]


def _question(name: str, *, label: str = "Which?", options: int = 1) -> dict:
    return ui.SelectField(
        name=f"question:{name}", label=label, options=_options(options)
    ).model_dump()


_APPROVE = {"id": "approve", "label": "Approve"}
_NOTE = ui.TextAreaField(name="note", label="Note").model_dump()


def test_an_approval_goes_out_without_an_input_that_slack_refuses():
    fields = [
        _question("database"),
        # Slack takes one block of an id, a label of 1 character or more, and 100 options.
        _question("database", label="Which database again?"),
        _question("unnamed", label=""),
        _question("crowded", options=101),
        _NOTE,
    ]

    _, *inputs, _ = get_blocks("Review", actions=[_APPROVE], fields=fields, token="t")

    assert [(block["block_id"], block["label"]) for block in inputs] == [
        ("question:database", _label("Which?")),
        ("note", _label("Note")),
    ]


def test_an_approval_with_more_inputs_than_a_message_holds_keeps_its_note():
    fields = [*(_question(str(number)) for number in range(60)), _NOTE]

    blocks = get_blocks("Review", actions=[_APPROVE], fields=fields, token="t")

    assert len(blocks) == 50
    assert [block["block_id"] for block in blocks[-3:-1]] == ["question:46", "note"]


def _data_table(caption: str, *rows: list[str]) -> dict:
    """The data table of ``rows`` of text cells, the header first."""
    cell_rows = [[{"type": "raw_text", "text": text} for text in row] for row in rows]
    return {"type": "data_table", "caption": caption, "rows": cell_rows}


def test_a_table_goes_out_under_its_title_with_a_number_a_link_text_or_a_dash_in_each_cell():
    table = ui.Table(
        title="Fans",
        columns=[ui.TableColumn(label) for label in ("Fan", "Speed", "Seen", "")],
        rows=[
            ui.TableRow(
                [
                    "front",
                    ui.NumberValue(1200, unit="rpm"),
                    ui.TimeValue(None),
                    ui.ControlsValue(
                        [
                            ui.Link("Rack [3]", url="https://racks.example/3"),
                            ui.Link("Log", page="logs"),
                        ]
                    ),
                ]
            )
        ],
    )

    (block,) = get_blocks("text", blocks=[table])

    rack = {"type": "link", "url": "https://racks.example/3", "text": "Rack [3]"}
    # Each cell of Speed is a number. Slack then sorts the column as numbers.
    assert block == {
        "type": "data_table",
        "caption": "Fans",
        "rows": [
            [{"type": "raw_text", "text": label} for label in ("Fan", "Speed", "Seen", "—")],
            [
                {"type": "raw_text", "text": "front"},
                {"type": "raw_number", "value": 1200, "text": "1,200 rpm"},
                {"type": "raw_text", "text": "—"},
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [rack, {"type": "text", "text": " · Log"}],
                        }
                    ],
                },
            ],
        ],
    }


def test_a_table_of_300_rows_shows_200_and_a_line_that_says_how_many_it_left_out():
    page = "https://druks.example/runs/run-1"
    table = ui.Table(
        columns=[ui.TableColumn("Fan")],
        rows=[ui.TableRow([f"Fan {number}"]) for number in range(300)],
    )

    line, data_table, link = get_blocks("text", blocks=[table], deep_link=page)

    notice = TABLE_OMISSION_MESSAGE.format(left=100, total=300) + OMISSION_LINK.format(link=page)
    assert line == {"type": "markdown", "text": notice}
    assert data_table == _data_table("Fan", ["Fan"], *([f"Fan {number}"] for number in range(200)))
    assert link == {"type": "markdown", "text": page}


def test_the_tables_of_a_message_share_slack_s_limit_for_the_text_of_their_cells():
    # With their headers, the first table and two rows of the second take all 20,000.
    first_table = ui.Table(columns=[ui.TableColumn("Log")], rows=[ui.TableRow(["x" * 15_997])])
    second_table = ui.Table(
        columns=[ui.TableColumn("Log")],
        rows=[ui.TableRow(["y" * 1_998]), ui.TableRow(["y" * 1_999]), ui.TableRow(["z"])],
    )
    third_table = ui.Table(columns=[ui.TableColumn("Log")], rows=[ui.TableRow(["z"])])

    # The message has no room left for a row of the third table. Druks sends that table as
    # its text form.
    assert get_blocks("text", blocks=[first_table, second_table, third_table]) == [
        _data_table("Log", ["Log"], ["x" * 15_997]),
        {"type": "markdown", "text": TABLE_OMISSION_MESSAGE.format(left=1, total=3)},
        _data_table("Log", ["Log"], ["y" * 1_998], ["y" * 1_999]),
        {"type": "markdown", "text": third_table.to_markdown()},
    ]


def _action_cell(label: str, action_id: str) -> dict:
    """The cell of a data table that holds the button ``action_id``."""
    button = {"type": "button", "text": _label(label), "action_id": action_id}
    return {"type": "action_cell", "element": button}


def _rich_text(*elements: dict) -> dict:
    """The rich text cell of ``elements``."""
    section = {"type": "rich_text_section", "elements": list(elements)}
    return {"type": "rich_text", "elements": [section]}


def test_a_cell_of_one_action_is_a_button_and_a_cell_of_more_controls_keeps_its_text():
    set_endpoint("https://druks.example")
    log = ui.Link("Log", url="https://logs.example/7")
    table = ui.Table(
        columns=[ui.TableColumn(label) for label in ("Note", "", "Both", "Mixed")],
        rows=[
            ui.TableRow(
                [
                    body,
                    ui.ControlsValue([_action(label="Archive", arguments=arguments)]),
                    ui.ControlsValue(
                        [
                            _action(label="Archive"),
                            _action(label="Clear gist", operation="clear_gist"),
                        ]
                    ),
                    ui.ControlsValue([_action(label="Archive"), log]),
                ]
            )
            for body, arguments in (("Fan noise", {"note_id": 7}), ("Rack 4", {"note_id": 8}))
        ],
    )

    _, data_table = get_blocks("text", blocks=[ui.Markdown("# Review"), table], token="t")

    page = "https://druks.example/runs/run-1"
    archive = {"type": "link", "url": page, "text": "Archive"}
    clear = {"type": "link", "url": page, "text": "Clear gist"}
    log_link = {"type": "link", "url": "https://logs.example/7", "text": "Log"}
    dot = {"type": "text", "text": " · "}
    # A button names the table, the row, and the cell.
    assert data_table["rows"][1:] == [
        [
            {"type": "raw_text", "text": body},
            _action_cell("Archive", encode_action("t", 1, row, 1)),
            _rich_text(archive, dot, clear),
            _rich_text(archive, dot, log_link),
        ]
        for row, body in enumerate(("Fan noise", "Rack 4"))
    ]


def test_a_cell_action_that_confirms_asks_first_and_one_slack_cannot_draw_keeps_its_text():
    rename = _action(
        label="Rename", operation="write_note", fields=[ui.TextField(name="title", label="Title")]
    )
    actions = [
        _action(label="Archive", confirm="Archive the note?"),
        _action(label="A" * 76),
        rename,
    ]
    table = ui.Table(
        columns=[ui.TableColumn("Action")],
        rows=[ui.TableRow([ui.ControlsValue([action])]) for action in actions],
    )

    (data_table,) = get_blocks("text", blocks=[table], token="t")

    archive = _action_cell("Archive", encode_action("t", 0, 0, 0))
    archive["element"]["confirm"] = {
        "title": _label("Archive"),
        "text": _label("Archive the note?"),
        "confirm": _label("Archive"),
        "deny": _label("Back"),
    }
    # With no endpoint, the text form of an action is its label.
    assert data_table["rows"][1:] == [
        [archive],
        [{"type": "raw_text", "text": "A" * 76}],
        [{"type": "raw_text", "text": "Rename"}],
    ]


def test_a_cell_is_a_button_only_while_its_approval_is_open():
    table = ui.Table(
        columns=[ui.TableColumn("Note"), ui.TableColumn("")],
        rows=[ui.TableRow(["Fan noise", ui.ControlsValue([_action(label="Archive")])])],
    )
    outcome = "<@U1> chose Approve."

    opened = get_blocks("text", blocks=[table], token="t")
    closed = get_blocks("text", blocks=[table], token="t", outcome=outcome)

    header = [{"type": "raw_text", "text": "Note"}, {"type": "raw_text", "text": "—"}]
    row = [
        {"type": "raw_text", "text": "Fan noise"},
        _action_cell("Archive", encode_action("t", 0, 0, 1)),
    ]
    assert opened == [{"type": "data_table", "caption": "Note", "rows": [header, row]}]
    assert closed == [
        _data_table("Note", ["Note", "—"], ["Fan noise", "Archive"]),
        {"type": "section", "text": _mrkdwn(outcome)},
    ]


def test_a_button_counts_its_label_toward_the_limit_of_the_cells():
    set_endpoint("https://druks.example")
    archive = ui.ControlsValue([_action(label="Archive")])
    # With their headers, the first table and two rows of buttons take all 20,000.
    log = ui.Table(columns=[ui.TableColumn("Log")], rows=[ui.TableRow(["x" * 19_976])])
    notes = ui.Table(
        columns=[ui.TableColumn("Note"), ui.TableColumn("")],
        rows=[ui.TableRow([name, archive]) for name in "yzw"],
    )

    blocks = get_blocks("text", blocks=[log, notes], token="t")

    header = [{"type": "raw_text", "text": "Note"}, {"type": "raw_text", "text": "—"}]
    rows = [
        [{"type": "raw_text", "text": name}, _action_cell("Archive", encode_action("t", 1, row, 1))]
        for row, name in enumerate("yz")
    ]
    # The button takes the 7 characters of its label, not the 43 of its linked text form.
    assert blocks[1:] == [
        {"type": "markdown", "text": TABLE_OMISSION_MESSAGE.format(left=1, total=3)},
        {"type": "data_table", "caption": "Note", "rows": [header, *rows]},
    ]


@pytest.mark.parametrize("kind", ["line", "bar", "area"])
def test_a_chart_goes_out_as_slack_s_chart_of_its_kind_with_a_point_for_each_category(kind):
    # Neither the categories nor the series are in the order of their names.
    chart = ui.Chart(
        kind=kind,
        title="Answers per day",
        categories=["Wed", "Thu", "Fri"],
        series=[
            ui.ChartSeries(label="web", points=[3, 5, 4]),
            ui.ChartSeries(label="api", points=[0.5, 0, -2]),
        ],
        category_label="Day",
        value_label="Answers",
    )

    (block,) = get_blocks("text", blocks=[chart])

    assert block == {
        "type": "data_visualization",
        "title": "Answers per day",
        "chart": {
            "type": kind,
            "series": [
                {
                    "name": "web",
                    "data": [
                        {"label": "Wed", "value": 3},
                        {"label": "Thu", "value": 5},
                        {"label": "Fri", "value": 4},
                    ],
                },
                {
                    "name": "api",
                    "data": [
                        {"label": "Wed", "value": 0.5},
                        {"label": "Thu", "value": 0},
                        {"label": "Fri", "value": -2},
                    ],
                },
            ],
            "axis_config": {
                "categories": ["Wed", "Thu", "Fri"],
                "x_label": "Day",
                "y_label": "Answers",
            },
        },
    }


def test_a_pie_goes_out_as_slack_s_pie_with_a_segment_for_each_category():
    # The categories are not in the order of their names. A pie has no axes. The block holds
    # neither its category label nor its value label.
    chart = ui.Chart(
        kind="pie",
        title="Answers by source",
        categories=["Web", "API", "Mail"],
        series=[ui.ChartSeries(label="Answers", points=[12, 5, 3])],
        category_label="Source",
        value_label="Answers",
    )

    (block,) = get_blocks("text", blocks=[chart])

    assert block == {
        "type": "data_visualization",
        "title": "Answers by source",
        "chart": {
            "type": "pie",
            "segments": [
                {"label": "Web", "value": 12},
                {"label": "API", "value": 5},
                {"label": "Mail", "value": 3},
            ],
        },
    }


def test_a_chart_cuts_a_label_past_20_characters_and_a_title_past_50():
    chart = ui.Chart(
        title="T" * 51,
        categories=["C" * 20, "D" * 21],
        series=[
            ui.ChartSeries(label="S" * 20, points=[3, 5]),
            ui.ChartSeries(label="R" * 21, points=[4, 2]),
        ],
        category_label="X" * 50,
        value_label="Y" * 51,
    )

    (block,) = get_blocks("text", blocks=[chart])

    labels = ["C" * 20, "D" * 19 + "…"]
    assert block == {
        "type": "data_visualization",
        "title": "T" * 49 + "…",
        "chart": {
            "type": "line",
            "series": [
                {
                    "name": "S" * 20,
                    "data": [{"label": labels[0], "value": 3}, {"label": labels[1], "value": 5}],
                },
                {
                    "name": "R" * 19 + "…",
                    "data": [{"label": labels[0], "value": 4}, {"label": labels[1], "value": 2}],
                },
            ],
            "axis_config": {"categories": labels, "x_label": "X" * 50, "y_label": "Y" * 49 + "…"},
        },
    }


def test_a_chart_with_no_title_takes_its_value_label_and_an_empty_axis_title_stays_out():
    series = [ui.ChartSeries(label="web", points=[3])]
    untitled_chart = ui.Chart(categories=["Mon"], series=series, value_label="V" * 51)
    titled_chart = ui.Chart(
        title="Answers", categories=["Mon"], series=series, category_label="Day"
    )

    first_block, second_block = get_blocks("text", blocks=[untitled_chart, titled_chart])

    assert first_block["title"] == "V" * 49 + "…"
    assert first_block["chart"]["axis_config"] == {"categories": ["Mon"], "y_label": "V" * 49 + "…"}
    assert second_block["title"] == "Answers"
    assert second_block["chart"]["axis_config"] == {"categories": ["Mon"], "x_label": "Day"}


def test_a_chart_shows_a_dash_for_an_empty_category_or_series_label():
    series = [ui.ChartSeries(label="", points=[3, 5])]
    chart = ui.Chart(title="Answers", categories=["Mon", ""], series=series)
    pie = ui.Chart(kind="pie", title="Answers", categories=["Mon", ""], series=series)

    chart_block, pie_block = get_blocks("text", blocks=[chart, pie])

    assert chart_block["chart"]["axis_config"] == {"categories": ["Mon", "—"]}
    assert chart_block["chart"]["series"] == [
        {"name": "—", "data": [{"label": "Mon", "value": 3}, {"label": "—", "value": 5}]}
    ]
    assert pie_block["chart"]["segments"] == [
        {"label": "Mon", "value": 3},
        {"label": "—", "value": 5},
    ]


def test_a_message_draws_its_first_two_charts_that_slack_takes_and_the_others_as_tables():
    series = [ui.ChartSeries(label="web", points=[3])]
    # Slack draws no chart of 30 points, with no title, or with two labels that match. These
    # take no room for a chart.
    month = ui.Chart(
        title="Month",
        categories=[f"Day {day}" for day in range(30)],
        series=[ui.ChartSeries(label="web", points=list(range(30)))],
    )
    untitled_chart = ui.Chart(categories=["Mon"], series=series)
    twin_days = ui.Chart(
        title="Twin days",
        categories=["Mon", "Mon"],
        series=[ui.ChartSeries(label="web", points=[3, 5])],
    )
    twin_series = ui.Chart(title="Twin series", categories=["Mon"], series=series * 2)
    first_chart, second_chart, third_chart = (
        ui.Chart(title=title, categories=["Mon"], series=series)
        for title in ("First", "Second", "Third")
    )
    charts = [month, first_chart, untitled_chart, twin_days, twin_series, second_chart, third_chart]

    parts = get_blocks("text", blocks=charts)

    text_forms = "\n\n".join(
        chart.to_markdown() for chart in (untitled_chart, twin_days, twin_series)
    )
    assert [(part["type"], part.get("title") or part["text"]) for part in parts] == [
        ("markdown", month.to_markdown()),
        ("data_visualization", "First"),
        ("markdown", text_forms),
        ("data_visualization", "Second"),
        ("markdown", third_chart.to_markdown()),
    ]


def test_a_pie_takes_a_segment_for_each_of_12_categories_and_a_label_of_20_characters_whole():
    one_fan = ui.Chart(
        kind="pie",
        title="One fan",
        categories=["F" * 20],
        series=[ui.ChartSeries(label="rpm", points=[7])],
    )
    twelve_fans = ui.Chart(
        kind="pie",
        title="Twelve fans",
        categories=[f"Fan {number}" for number in range(12)],
        series=[ui.ChartSeries(label="rpm", points=list(range(1, 13)))],
    )

    one_fan_block, twelve_fans_block = get_blocks("text", blocks=[one_fan, twelve_fans])

    assert one_fan_block["chart"]["segments"] == [{"label": "F" * 20, "value": 7}]
    assert twelve_fans_block["chart"]["segments"] == [
        {"label": f"Fan {number}", "value": number + 1} for number in range(12)
    ]


def test_a_pie_that_slack_cannot_draw_shows_its_table_and_leaves_the_room_for_two_charts():
    speed = ui.ChartSeries(label="rpm", points=[1])
    thirteen_fans = ui.Chart(
        kind="pie",
        title="Thirteen fans",
        categories=[f"Fan {number}" for number in range(13)],
        series=[ui.ChartSeries(label="rpm", points=[1] * 13)],
    )
    long_label = ui.Chart(kind="pie", title="Long label", categories=["F" * 21], series=[speed])
    no_fans = ui.Chart(kind="pie", title="No fans", series=[ui.ChartSeries(label="rpm", points=[])])
    untitled_pie = ui.Chart(kind="pie", categories=["Fan"], series=[speed])
    long_value_label = ui.Chart(
        kind="pie", categories=["Fan"], series=[speed], value_label="V" * 51
    )
    line = ui.Chart(title="Line", categories=["Mon"], series=[speed])
    last_pie = ui.Chart(kind="pie", title="Last pie", categories=["Fan"], series=[speed])
    charts = [thirteen_fans, long_label, no_fans, untitled_pie, long_value_label, line, last_pie]

    parts = get_blocks("text", blocks=charts)

    text_forms = "\n\n".join(
        chart.to_markdown() for chart in (thirteen_fans, long_label, no_fans, untitled_pie)
    )
    assert [(part["type"], part.get("title") or part["text"]) for part in parts] == [
        ("markdown", text_forms),
        ("data_visualization", "V" * 49 + "…"),
        ("data_visualization", "Line"),
        ("markdown", last_pie.to_markdown()),
    ]


async def test_a_status_in_a_dm_edits_the_dm_channel_that_slack_gave(druks_db, slack):
    conversation = await _slack_chat(druks_db)
    conversation.thread_id = ""
    status = await Notification.create(
        druks_db, conversation_id=conversation.id, reason="run.status", body="Writing the gist."
    )
    # A post to a person goes to their DM, which Slack names as a channel of its own.
    slack.channel = "D9"

    status.message_id = await SlackChannel.show_status(
        druks_db, conversation, status, body="Writing the gist.", blocks=[]
    )
    await SlackChannel.show_status(
        druks_db, conversation, status, body="The gist is written.", blocks=[]
    )

    posted, edited = slack.posts
    assert (posted["channel"], posted["text"], status.message_id) == (
        "U1",
        "Writing the gist.",
        "D9:1.0",
    )
    assert (edited["method"], edited["channel"], edited["ts"], edited["text"]) == (
        "chat.update",
        "D9",
        "1.0",
        "The gist is written.",
    )


def test_an_image_goes_out_as_slack_s_image_with_its_caption_as_its_title():
    rack = ui.Image(
        url="https://shots.example/rack.png",
        alternative_text="Rack 3 with its front fan out.",
        caption="Rack 3",
    )

    assert get_blocks("text", blocks=[rack]) == [
        {
            "type": "image",
            "image_url": "https://shots.example/rack.png",
            "alt_text": "Rack 3 with its front fan out.",
            "title": _label("Rack 3"),
        }
    ]


@pytest.mark.parametrize(
    ("url", "block"),
    [
        (
            "HTTPS://shots.example/RACK.JPEG",
            _image_block("HTTPS://shots.example/RACK.JPEG", "Rack 3."),
        ),
        (
            "https://shots.example/rack.gif?size=2#loop",
            _image_block("https://shots.example/rack.gif?size=2#loop", "Rack 3."),
        ),
        (
            "https://shots.example/rack 3 (front).png",
            _image_block("https://shots.example/rack%203%20%28front%29.png", "Rack 3."),
        ),
        ("https://shots.example/rack.svg", {}),
        ("/static/rack.png", {}),
    ],
    ids=["jpeg-in-capitals", "gif-with-query-and-fragment", "space-and-parentheses", "svg", "path"],
)
def test_slack_draws_an_image_only_at_an_outside_address_whose_path_ends_in_png_jpg_or_gif(
    url, block
):
    set_endpoint("https://druks.example")

    assert get_image_block(ui.Image(url=url, alternative_text="Rack 3."), {}) == block


def test_a_gallery_takes_an_image_block_for_each_image_under_its_title():
    shots = [
        ui.Image(url=f"https://shots.example/rack-{number}.png", alternative_text=f"Rack {number}.")
        for number in (1, 2, 3)
    ]
    image_blocks = [
        _image_block(f"https://shots.example/rack-{number}.png", f"Rack {number}.")
        for number in (1, 2, 3)
    ]

    blocks = get_blocks(
        "text", blocks=[ui.ImageGallery(shots, title="Racks"), ui.ImageGallery(shots)]
    )

    assert blocks == [{"type": "markdown", "text": "### Racks"}, *image_blocks, *image_blocks]


def test_a_gallery_keeps_the_line_of_each_image_that_slack_cannot_draw_in_its_place():
    gallery = ui.ImageGallery(
        [
            ui.Image(url="https://shots.example/plan.svg", alternative_text="The plan."),
            ui.Image(url="https://shots.example/front.png", alternative_text="The front."),
            ui.Image(url="https://shots.example/back", alternative_text="The back."),
            ui.Image(url="https://shots.example/side.svg", alternative_text="The side."),
            ui.Image(url="https://shots.example/top.gif", alternative_text="The top."),
        ],
        title="Rack 3",
    )

    assert get_blocks("text", blocks=[gallery]) == [
        {
            "type": "markdown",
            "text": "### Rack 3\n- [The plan.](https://shots.example/plan.svg)",
        },
        _image_block("https://shots.example/front.png", "The front."),
        {
            "type": "markdown",
            "text": "- [The back.](https://shots.example/back)\n"
            "- [The side.](https://shots.example/side.svg)",
        },
        _image_block("https://shots.example/top.gif", "The top."),
    ]


def test_a_gallery_with_no_image_that_slack_can_draw_shows_its_text_form():
    links = ui.ImageGallery(
        [
            ui.Image(url="https://shots.example/plan.svg", alternative_text="The plan."),
            ui.Image(url="https://shots.example/back", alternative_text="The back."),
        ],
        title="Rack 3",
    )
    front = ui.Image(url="https://shots.example/front.png", alternative_text="The front.")

    assert get_blocks("text", blocks=[links, front]) == [
        {"type": "markdown", "text": links.to_markdown()},
        _image_block("https://shots.example/front.png", "The front."),
    ]


def test_a_gallery_that_does_not_fit_whole_shows_its_text_form_with_the_blocks_after_it():
    links = [
        ui.Link(f"Link {number}", url=f"https://docs.example/{number}") for number in range(46)
    ]
    shots = ui.ImageGallery(
        [
            ui.Image(url=f"https://shots.example/{number}.png", alternative_text=f"Shot {number}.")
            for number in range(3)
        ],
        title="Shots",
    )
    documentation = ui.Link("Docs", url="https://docs.example/a")

    blocks = get_blocks("text", blocks=[*links, shots, documentation])

    # The title and two of the images fit.
    assert blocks[46:] == [
        {"type": "markdown", "text": f"{shots.to_markdown()}\n\n{documentation.to_markdown()}"}
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_druks_image_goes_up_as_a_private_slack_file_that_the_message_then_shows(
    druks_db, slack, operator
):
    rack = await _druks_file()
    image = ui.Image(url=rack.url, alternative_text="Rack 3 with its front fan out.")

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(image),
    )

    get_address, transfer, completion, post = slack.requests
    assert (get_address["method"], get_address["filename"]) == (
        "files.getUploadURLExternal",
        "rack.png",
    )
    assert (transfer["url"], transfer["data"]) == (
        "https://files.slack.example/upload/F1",
        b"image bytes",
    )
    # The upload shares the file in no channel. Only the message shows it.
    assert completion["method"] == "files.completeUploadExternal"
    assert "channel_id" not in completion
    assert {get_address["bot_token"], completion["bot_token"], post["bot_token"]} == {_BOT_TOKEN}
    assert (post["method"], post["blocks"]) == (
        "chat.postMessage",
        [_slack_file_block("F1", "Rack 3 with its front fan out.")],
    )


@pytest.mark.parametrize(
    "draw",
    [
        SlackChannel.post_buttons,
        lambda session, conversation, approval: replace_buttons(
            approval.id, channel="C1", message_ts="1.0", outcome="<@U1> chose Approve."
        ),
        SlackChannel.remove_buttons,
        lambda session, conversation, status: SlackChannel.show_status(
            session, conversation, status, body=status.body, blocks=status.blocks
        ),
    ],
    ids=["post", "click", "close", "status"],
)
@pytest.mark.usefixtures("tmp_data_dir")
async def test_each_message_of_a_notification_uploads_its_druks_image_again(druks_db, slack, draw):
    conversation = await _slack_chat(druks_db)
    rack = await _druks_file()
    notification = await Notification.create(
        druks_db,
        conversation_id=conversation.id,
        reason="gate.parked",
        body="Rack 3.",
        blocks=_stored(ui.Image(url=rack.url, alternative_text="Rack 3.")),
        actions=[{"id": "approve", "label": "Approve"}],
    )
    notification.message_id = "C1:1.0"

    await draw(druks_db, conversation, notification)
    await draw(druks_db, conversation, notification)

    first, second = slack.posts
    assert (first["blocks"][0], second["blocks"][0]) == (
        _slack_file_block("F1", "Rack 3."),
        _slack_file_block("F2", "Rack 3."),
    )


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_gallery_shows_each_image_in_its_place_and_links_each_that_slack_cannot_show(
    druks_db, slack, operator
):
    set_endpoint("https://druks.example")
    front = await _druks_file("front.png")
    plan = await _druks_file("plan.svg", content_type="image/svg+xml")
    top = await _druks_file("top.gif", content_type="image/gif")
    shelf = await _druks_file("shelf.png")
    gallery = ui.ImageGallery(
        [
            ui.Image(url=front.url, alternative_text="The front."),
            ui.Image(url="https://shots.example/back.png", alternative_text="The back."),
            ui.Image(url=plan.url, alternative_text="The plan."),
            ui.Image(url=f"https://other.example{top.url}", alternative_text="The side."),
            ui.Image(url=shelf.url, alternative_text="The shelf.", caption="S" * 2001),
            ui.Image(url=f"https://druks.example{top.url}", alternative_text="The top."),
        ],
        title="Rack 3",
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(gallery),
    )

    # Druks fetches no outside image, and no file at an address on another host.
    assert _list_uploads(slack) == ["front.png", "top.gif"]
    (post,) = slack.posts
    assert post["blocks"] == [
        {"type": "markdown", "text": "### Rack 3"},
        _slack_file_block("F1", "The front."),
        _image_block("https://shots.example/back.png", "The back."),
        {
            "type": "markdown",
            "text": f"- [The plan.](https://druks.example{plan.url})\n"
            f"- [The side.](https://other.example{top.url})\n"
            f"- [The shelf.](https://druks.example{shelf.url})",
        },
        _slack_file_block("F2", "The top."),
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_workspace_without_files_write_gets_links_until_it_approves_the_app_again(
    druks_db, slack, operator
):
    set_endpoint("https://druks.example")
    shots = [await _druks_file(f"rack-{number}.png") for number in (1, 2, 3)]
    gallery = ui.ImageGallery(
        [
            ui.Image(url=shot.url, alternative_text=f"Rack {number}.")
            for number, shot in enumerate(shots, start=1)
        ]
    )
    client = SlackClient(token=_BOT_TOKEN)
    slack.upload_refusals = ["missing_scope"]

    await deliver(client, "C1", "text", session=druks_db, account=operator, blocks=_stored(gallery))
    await deliver(client, "C1", "text", session=druks_db, account=operator, blocks=_stored(gallery))

    refused, links, *uploads, images = slack.requests
    assert (refused["method"], refused["filename"]) == ("files.getUploadURLExternal", "rack-1.png")
    assert links["blocks"] == [{"type": "markdown", "text": gallery.to_markdown()}]
    assert [upload["method"] for upload in uploads] == [
        "files.getUploadURLExternal",
        "POST",
        "files.completeUploadExternal",
    ] * 3
    assert images["blocks"] == [
        _slack_file_block(f"F{number}", f"Rack {number}.") for number in (1, 2, 3)
    ]


@pytest.mark.parametrize(
    "fail",
    [
        lambda slack, front: slack.upload_refusals.append("ratelimited"),
        lambda slack, front: slack.transfer_failures.append(SlackRequestError("status: 500")),
        lambda slack, front: slack.transfer_failures.append(aiohttp.ClientConnectionError()),
        lambda slack, front: slack.transfer_failures.append(TimeoutError()),
        lambda slack, front: get_file_storage().delete(front.id),
    ],
    ids=["refused", "transfer-refused", "connection", "timeout", "content-missing"],
)
@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_image_whose_upload_fails_keeps_its_link_and_the_next_image_still_goes_up(
    druks_db, slack, fail, operator
):
    set_endpoint("https://druks.example")
    front, top = await _druks_file("front.png"), await _druks_file("top.png")
    gallery = ui.ImageGallery(
        [
            ui.Image(url=front.url, alternative_text="The front."),
            ui.Image(url=top.url, alternative_text="The top."),
        ]
    )
    fail(slack, front)

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(gallery),
    )

    *_, completion, post = slack.requests
    (completed,) = json.loads(completion["files"])
    assert post["blocks"] == [
        {"type": "markdown", "text": f"- [The front.](https://druks.example{front.url})"},
        _slack_file_block(completed["id"], "The top."),
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_slack_shows_a_druks_image_of_up_to_10_mib_and_druks_reads_no_bigger_one(
    druks_db, slack, monkeypatch, operator
):
    set_endpoint("https://druks.example")
    largest, too_big = await _druks_file("largest.png"), await _druks_file("too-big.png")
    # Druks reads the size from the record of a file.
    (await druks_db.get(FileRecord, largest.id)).size = IMAGE_BYTES_LIMIT
    (await druks_db.get(FileRecord, too_big.id)).size = IMAGE_BYTES_LIMIT + 1
    opened = []
    open_file = LocalFileStorage.open

    def spy(storage, file_id):
        opened.append(file_id)
        return open_file(storage, file_id)

    monkeypatch.setattr(LocalFileStorage, "open", spy)
    gallery = ui.ImageGallery(
        [
            ui.Image(url=largest.url, alternative_text="The largest."),
            ui.Image(url=too_big.url, alternative_text="Too big."),
        ]
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(gallery),
    )

    assert opened == [largest.id]
    (post,) = slack.posts
    assert post["blocks"] == [
        _slack_file_block("F1", "The largest."),
        {"type": "markdown", "text": f"- [Too big.](https://druks.example{too_big.url})"},
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_image_of_a_deleted_file_or_of_no_file_keeps_its_link(druks_db, slack, operator):
    set_endpoint("https://druks.example")
    deleted = await _druks_file("deleted.png")
    await deleted.delete()
    gallery = ui.ImageGallery(
        [
            ui.Image(url=deleted.url, alternative_text="A deleted file."),
            ui.Image(url="/api/files/missing", alternative_text="No file."),
        ]
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(gallery),
    )

    assert not _list_uploads(slack)
    (post,) = slack.posts
    assert post["blocks"] == [{"type": "markdown", "text": gallery.to_markdown()}]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_an_image_of_an_app_that_the_account_cannot_use_keeps_its_link(druks_db, slack):
    set_endpoint("https://druks.example")
    # The first account is the admin, who can use every app.
    await Account.get_or_create(druks_db, "admin@example.com")
    outsider = await Account.get_or_create(druks_db, "outsider@example.com")
    await Team.create(druks_db, name="Field crew", member_ids=[], apps=["field_notes"])
    of_the_team = await _druks_file("notes.png")
    of_no_installed_app = await File.create(
        name="shop.png",
        content_type="image/png",
        content=b"image bytes",
        app="site_builder",
        uploaded_by=None,
    )
    gallery = ui.ImageGallery(
        [
            ui.Image(url=of_the_team.url, alternative_text="A file of the team's app."),
            ui.Image(url=of_no_installed_app.url, alternative_text="A file of no installed app."),
        ]
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=outsider,
        blocks=_stored(gallery),
    )

    assert not _list_uploads(slack)
    (post,) = slack.posts
    assert post["blocks"] == [{"type": "markdown", "text": gallery.to_markdown()}]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_message_uploads_no_image_that_it_has_no_room_to_show(druks_db, slack, operator):
    shots = [await _druks_file(f"{number}.png") for number in range(MESSAGE_BLOCKS_LIMIT)]
    gallery = ui.ImageGallery(
        [ui.Image(url=shot.url, alternative_text=shot.name) for shot in shots]
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(gallery),
    )

    assert not _list_uploads(slack)


def _section(text: str) -> dict:
    """The section of the mrkdwn ``text``."""
    return {"type": "section", "text": _mrkdwn(text)}


_FAN = ui.Facts([ui.Fact("Fan", "front")])
_FAN_SECTION = {"type": "section", "fields": [_mrkdwn("*Fan*\nfront")]}


async def test_an_approval_s_card_with_an_image_and_a_link_goes_out_as_slack_s_card(
    druks_db, slack
):
    card = ui.Card(
        title="Rack 3",
        description="The front fan is out.",
        blocks=[
            ui.Image(
                url="https://shots.example/rack.png",
                alternative_text="Rack 3 with its front fan out.",
                caption="Rack 3",
            ),
            ui.Link("Ticket", url="https://tickets.example/7"),
        ],
    )
    approval = await _approval(druks_db, context=_stored(card))

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        approval.body,
        session=druks_db,
        account=None,
        blocks=approval.blocks,
        actions=approval.actions,
        fields=approval.fields,
        token=approval.correlation_token,
    )

    (post,) = slack.posts
    # Slack's image element has no title. The hero image shows no caption.
    assert post["blocks"][:3] == [
        {"type": "card", "title": _label("Field Notes")},
        {"type": "markdown", "text": "# Review"},
        {
            "type": "card",
            "title": _label("Rack 3"),
            "body": _label("The front fan is out."),
            "hero_image": _image_block(
                "https://shots.example/rack.png", "Rack 3 with its front fan out."
            ),
            "actions": [_url_button("Ticket", "https://tickets.example/7")],
        },
    ]


def test_a_card_has_a_button_for_its_link_for_each_link_block_and_for_each_link_control():
    set_endpoint("https://druks.example")
    card = ui.Card(
        title="Rack 3",
        link=ui.Link(url="/field_notes/notes/3"),
        blocks=[ui.Link("Ticket", url="https://tickets.example/7")],
        controls=[_action(label="Archive"), ui.Link("Logs", url="https://logs.example/3")],
    )

    # Slack runs no action of a card. The link of the card takes its title.
    assert get_blocks("text", blocks=[card], token="t") == [
        {
            "type": "card",
            "title": _label("Rack 3"),
            "actions": [
                _url_button("Rack 3", "https://druks.example/field_notes/notes/3"),
                _url_button("Ticket", "https://tickets.example/7", f"{LINK_BUTTON_ACTION_ID}:1"),
                _url_button("Logs", "https://logs.example/3", f"{LINK_BUTTON_ACTION_ID}:2"),
            ],
        }
    ]


@pytest.mark.parametrize(
    ("make", "limit"),
    [
        (lambda size: ui.Card(title="T" * size), 150),
        (lambda size: ui.Card(title="Rack 3", description="D" * size), 200),
        (
            lambda size: ui.Card(
                title="Rack 3",
                blocks=[
                    ui.Link(f"Log {number}", url=f"https://logs.example/{number}")
                    for number in range(size)
                ],
            ),
            3,
        ),
    ],
    ids=["title", "body", "buttons"],
)
def test_a_card_takes_slack_s_card_up_to_a_limit_and_its_text_form_past_it(make, limit):
    block_past_limit = make(limit + 1)

    (card,) = get_blocks("text", blocks=[make(limit)])

    assert card["type"] == "card"
    assert get_blocks("text", blocks=[block_past_limit]) == [
        {"type": "markdown", "text": block_past_limit.to_markdown()}
    ]


def test_a_card_with_nothing_that_slack_shows_is_no_card():
    set_endpoint("https://druks.example")
    archive_only = ui.Card(controls=[_action(label="Archive")])

    blocks = get_blocks(
        "text", blocks=[ui.Card(title="Rack 3"), ui.Card(), archive_only], token="t"
    )

    assert blocks == [
        {"type": "card", "title": _label("Rack 3")},
        {"type": "markdown", "text": "[Archive](https://druks.example/runs/run-1)"},
    ]


def test_a_card_that_holds_blocks_and_a_section_with_a_title_go_out_as_containers():
    set_endpoint("https://druks.example")
    card = ui.Card(
        title="Rack 3",
        description="Fan noise since Monday.",
        link=ui.Link(url="/field_notes/notes/3"),
        blocks=[ui.Text("Swap the <front> fan & test it."), ui.Divider()],
        controls=[_action(label="Archive"), ui.Link("Logs", url="https://logs.example/3")],
    )
    section = ui.Section(
        title="Next steps",
        blocks=[ui.Link("Runbook", url="https://docs.example/runbook")],
        controls=[
            ui.Link("Docs", url="https://docs.example/a"),
            ui.Link("Logs", url="https://logs.example/3"),
        ],
    )

    card_container, section_container = get_blocks("text", blocks=[card, section], token="t")

    # The links of a card follow its blocks. The links of a section come first.
    assert card_container == {
        "type": "container",
        "title": _label("Rack 3"),
        "subtitle": _label("Fan noise since Monday."),
        "child_blocks": [
            _section("Swap the &lt;front&gt; fan &amp; test it."),
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    _url_button("Rack 3", "https://druks.example/field_notes/notes/3"),
                    _url_button("Logs", "https://logs.example/3", f"{LINK_BUTTON_ACTION_ID}:1"),
                ],
            },
        ],
    }
    assert section_container == {
        "type": "container",
        "title": _label("Next steps"),
        "child_blocks": [
            {
                "type": "actions",
                "elements": [
                    _url_button("Docs", "https://docs.example/a"),
                    _url_button("Logs", "https://logs.example/3", f"{LINK_BUTTON_ACTION_ID}:1"),
                ],
            },
            _link_button("Runbook", "https://docs.example/runbook"),
        ],
    }


@pytest.mark.parametrize(
    ("make", "limit"),
    [
        (lambda size: ui.Section(title="T" * size, blocks=[ui.Divider()]), 150),
        (lambda size: ui.Card(title="Rack 3", description="D" * size, blocks=[ui.Divider()]), 150),
        (lambda size: ui.Section(title="Log", blocks=[ui.Text("x" * size)]), 3000),
    ],
    ids=["title", "subtitle", "section-text"],
)
def test_a_container_takes_slack_s_container_up_to_a_limit_and_its_text_form_past_it(make, limit):
    block_past_limit = make(limit + 1)

    (container,) = get_blocks("text", blocks=[make(limit)])

    assert container["type"] == "container"
    assert get_blocks("text", blocks=[block_past_limit]) == [
        {"type": "markdown", "text": block_past_limit.to_markdown()}
    ]


async def test_a_container_of_14_blocks_or_with_no_title_shows_its_text_form(druks_db, slack):
    fourteen = ui.Section(
        title="Fans",
        blocks=[ui.Facts([ui.Fact(f"Fan {number}", "front")]) for number in range(14)],
    )
    untitled = ui.Card(description="Fan noise.", blocks=[ui.Divider()])

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(fourteen, untitled, ui.Section(title="Racks", blocks=[ui.Divider()])),
    )

    (post,) = slack.posts
    assert post["blocks"] == [
        {"type": "markdown", "text": f"{fourteen.to_markdown()}\n\n{untitled.to_markdown()}"},
        {"type": "container", "title": _label("Racks"), "child_blocks": [{"type": "divider"}]},
    ]


def test_a_container_holds_text_facts_metrics_a_divider_a_link_an_image_and_columns():
    section = ui.Section(
        title="Rack 3",
        blocks=[
            ui.Text("Fan noise."),
            _FAN,
            ui.Metrics([ui.Metric("Noise", ui.NumberValue(72, unit="dB"))]),
            ui.Divider(),
            ui.Link("Docs", url="https://docs.example/a"),
            ui.Image(url="https://shots.example/rack.png", alternative_text="Rack 3."),
            ui.Columns([_FAN, ui.Text("Fan noise.")]),
        ],
    )

    (container,) = get_blocks("text", blocks=[section])

    assert container["child_blocks"] == [
        _section("Fan noise."),
        _FAN_SECTION,
        {"type": "section", "fields": [_mrkdwn("*Noise*\n72 dB")]},
        {"type": "divider"},
        _link_button("Docs", "https://docs.example/a"),
        _image_block("https://shots.example/rack.png", "Rack 3."),
        {
            "type": "layout",
            "label": LAYOUT_LABEL,
            "rows": [{"items": [{"block": _FAN_SECTION}, {"block": _section("Fan noise.")}]}],
        },
    ]


@pytest.mark.parametrize(
    "block",
    [
        ui.Markdown("**Loud** fan."),
        ui.Table(columns=[ui.TableColumn("Fan")], rows=[ui.TableRow(["front"])]),
        ui.Chart(
            title="Speeds", categories=["Mon"], series=[ui.ChartSeries(label="rpm", points=[1])]
        ),
        ui.Card(title="Fan"),
        ui.Cards(cards=[ui.Card(title="Fan")]),
        ui.Section(title="Fans", blocks=[ui.Divider()]),
        ui.ImageGallery([ui.Image(url="https://shots.example/rack.png", alternative_text="Rack.")]),
        _action(label="Archive"),
    ],
    ids=["markdown", "table", "chart", "card", "cards", "titled-section", "gallery", "action"],
)
def test_a_container_with_a_block_that_it_cannot_hold_shows_its_text_form(block):
    # A stack in a container holds what the container holds.
    section = ui.Section(title="Rack 3", blocks=[ui.Divider(), ui.Stack([block])])
    racks = ui.Section(title="Racks", blocks=[ui.Divider()])

    # Only the message holds markdown. No part of the first section shows as a container.
    assert get_blocks("text", blocks=[section, racks], token="t") == [
        {"type": "markdown", "text": section.to_markdown()},
        {"type": "container", "title": _label("Racks"), "child_blocks": [{"type": "divider"}]},
    ]


def test_a_stack_and_a_section_with_no_title_show_their_blocks_in_order():
    stack = ui.Stack([_FAN, ui.Markdown("**Loud** fan."), ui.Divider()])
    section = ui.Section(
        blocks=[ui.Divider()], controls=[ui.Link("Docs", url="https://docs.example/a")]
    )

    assert get_blocks("text", blocks=[ui.Markdown("# Review"), stack, section]) == [
        {"type": "markdown", "text": "# Review"},
        _FAN_SECTION,
        {"type": "markdown", "text": "**Loud** fan."},
        {"type": "divider"},
        _link_button("Docs", "https://docs.example/a"),
        {"type": "divider"},
    ]


async def test_only_an_action_at_the_top_of_the_context_goes_out_with_inputs_and_a_button(
    druks_db, slack
):
    set_endpoint("https://druks.example")
    page = "https://druks.example/runs/run-1"
    rename = _action(
        label="Rename", operation="write_note", fields=[ui.TextField(name="title", label="Title")]
    )
    table = ui.Table(
        columns=[ui.TableColumn("Note"), ui.TableColumn("")],
        rows=[ui.TableRow(["Fan noise", ui.ControlsValue([_action(label="Archive")])])],
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(ui.Stack([rename, _form(), table]), rename),
        token="t",
    )

    (post,) = slack.posts
    # Only the action at the top has inputs and a button. They name its position.
    assert post["blocks"] == [
        {"type": "markdown", "text": f"[Rename]({page})\n\n### Ship it\n\n[Ship]({page})"},
        {
            "type": "data_table",
            "caption": "Note",
            "rows": [
                [{"type": "raw_text", "text": "Note"}, {"type": "raw_text", "text": "—"}],
                [
                    {"type": "raw_text", "text": "Fan noise"},
                    _rich_text({"type": "link", "url": page, "text": "Archive"}),
                ],
            ],
        },
        {
            "type": "input",
            "block_id": "1.title",
            "label": _label("Title"),
            "optional": True,
            "element": {"type": "plain_text_input", "multiline": False},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": _label("Rename"), "action_id": encode_action("t", 1)}
            ],
        },
    ]


async def test_an_approval_s_list_of_four_simple_cards_goes_out_as_a_carousel(druks_db, slack):
    cards = [
        ui.Card(
            title=f"Rack {number}", blocks=[ui.Link("Open", url=f"https://racks.example/{number}")]
        )
        for number in range(4)
    ]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(ui.Cards(title="Racks", cards=cards)),
    )

    (post,) = slack.posts
    assert post["blocks"] == [
        {"type": "markdown", "text": "### Racks"},
        {
            "type": "carousel",
            "elements": [
                {
                    "type": "card",
                    "title": _label(f"Rack {number}"),
                    "actions": [_url_button("Open", f"https://racks.example/{number}")],
                }
                for number in range(4)
            ],
        },
    ]


@pytest.mark.parametrize(
    ("count", "types"),
    [(1, ["carousel"]), (10, ["carousel"]), (11, ["card"] * 11)],
    ids=["one", "ten", "eleven"],
)
def test_a_carousel_takes_1_to_10_cards_and_a_longer_list_shows_each_card(count, types):
    titles = [f"Rack {number}" for number in range(count)]

    blocks = get_blocks("text", blocks=[ui.Cards(cards=[ui.Card(title=title) for title in titles])])

    assert [block["type"] for block in blocks] == types
    cards = [card for block in blocks for card in block.get("elements", [block])]
    assert [card["title"]["text"] for card in cards] == titles


def test_a_list_with_a_card_that_a_carousel_cannot_hold_shows_each_card_in_order():
    holds_text = ui.Card(title="Rack 4", blocks=[ui.Text("Fan noise.")])
    untitled = ui.Card(blocks=[ui.Text("Fan noise.")])
    racks = ui.Cards(title="Racks", cards=[ui.Card(title="Rack 3"), holds_text, untitled])
    shelves = ui.Cards(title="Shelves", empty=ui.EmptyState("No shelves", description="Add one."))

    # An empty list shows its title and its empty state as text, with no carousel.
    assert get_blocks("text", blocks=[racks, shelves]) == [
        {"type": "markdown", "text": "### Racks"},
        {"type": "card", "title": _label("Rack 3")},
        {"type": "container", "title": _label("Rack 4"), "child_blocks": [_section("Fan noise.")]},
        {"type": "markdown", "text": "Fan noise.\n\n### Shelves\n\nNo shelves\n\nAdd one."},
    ]


async def test_columns_of_two_blocks_go_out_side_by_side_in_a_layout(druks_db, slack):
    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=None,
        blocks=_stored(ui.Columns([_FAN, ui.Link("Docs", url="https://docs.example/a")])),
    )

    (post,) = slack.posts
    assert post["blocks"] == [
        {
            "type": "layout",
            "label": LAYOUT_LABEL,
            "rows": [
                {
                    "items": [
                        {"block": _FAN_SECTION},
                        {"block": _link_button("Docs", "https://docs.example/a")},
                    ]
                }
            ],
        }
    ]


def test_columns_of_three_share_a_row_and_more_columns_go_one_under_the_other():
    racks = ui.Section(title="Racks", blocks=[ui.Divider()])
    links = [ui.Link(f"Log {number}", url=f"https://logs.example/{number}") for number in range(4)]

    three, *four = get_blocks(
        "text", blocks=[ui.Columns([ui.Card(title="Rack 3"), racks, links[0]]), ui.Columns(links)]
    )

    assert three["rows"] == [
        {
            "items": [
                {"block": {"type": "card", "title": _label("Rack 3")}},
                {
                    "block": {
                        "type": "container",
                        "title": _label("Racks"),
                        "child_blocks": [{"type": "divider"}],
                    }
                },
                {"block": _link_button("Log 0", "https://logs.example/0")},
            ]
        }
    ]
    assert four == [
        _link_button(f"Log {number}", f"https://logs.example/{number}") for number in range(4)
    ]


def test_a_carousel_a_data_table_and_a_chart_each_take_a_row_of_the_layout():
    chart = ui.Chart(
        title="Speeds", categories=["Mon"], series=[ui.ChartSeries(label="rpm", points=[1])]
    )
    table = ui.Table(columns=[ui.TableColumn("Fan")], rows=[ui.TableRow(["front"])])
    cards = ui.Cards(cards=[ui.Card(title="Rack 3"), ui.Card(title="Rack 4")])

    first, second = get_blocks(
        "text", blocks=[ui.Columns([_FAN, _FAN, chart]), ui.Columns([cards, table, _FAN])]
    )

    assert [[item["block"]["type"] for item in row["items"]] for row in first["rows"]] == [
        ["section", "section"],
        ["data_visualization"],
    ]
    assert [[item["block"]["type"] for item in row["items"]] for row in second["rows"]] == [
        ["carousel"],
        ["data_table"],
        ["section"],
    ]


def test_containers_and_layouts_nest_two_levels_deep_and_a_stack_adds_no_level():
    layout_of_containers = ui.Columns([ui.Section(title="Rack 3", blocks=[ui.Stack([_FAN])]), _FAN])
    container_of_layouts = ui.Section(
        title="Racks", blocks=[ui.Section(blocks=[ui.Columns([_FAN, _FAN])])]
    )
    three_levels = ui.Columns([ui.Section(title="Rack 3", blocks=[ui.Columns([_FAN, _FAN])]), _FAN])

    layout, container, text = get_blocks(
        "text", blocks=[layout_of_containers, container_of_layouts, three_levels]
    )

    assert layout["rows"][0]["items"][0]["block"] == {
        "type": "container",
        "title": _label("Rack 3"),
        "child_blocks": [_FAN_SECTION],
    }
    assert container["child_blocks"] == [
        {
            "type": "layout",
            "label": LAYOUT_LABEL,
            "rows": [{"items": [{"block": _FAN_SECTION}, {"block": _FAN_SECTION}]}],
        }
    ]
    assert text == {"type": "markdown", "text": three_levels.to_markdown()}


def test_columns_with_a_column_of_more_than_one_block_show_their_text_form():
    two_blocks = ui.Columns([ui.Section(blocks=[_FAN, ui.Divider()]), _FAN])

    layout, text = get_blocks("text", blocks=[ui.Columns([_FAN, _FAN]), two_blocks])

    assert layout["type"] == "layout"
    assert text == {"type": "markdown", "text": two_blocks.to_markdown()}


@pytest.mark.parametrize(
    ("dividers", "types"),
    [
        (4, ["container", "markdown", "markdown", "input", "actions"]),
        (5, ["markdown", "markdown", "input", "actions"]),
    ],
    ids=["room", "no-room"],
)
def test_each_block_inside_a_container_takes_room_that_the_approval_leaves(dividers, types):
    links = [
        ui.Link(f"Link {number}", url=f"https://docs.example/{number}") for number in range(41)
    ]
    section = ui.Section(title="Racks", blocks=[ui.Divider()] * dividers)

    # The question, the note, and the button take a block each. One more stays for the link.
    blocks = get_blocks(
        "text",
        blocks=[*links, section],
        question="Which rack?",
        deep_link="https://druks.example/runs/run-1",
        actions=[_APPROVE],
        fields=[_NOTE],
        token="t",
    )

    assert [block["type"] for block in blocks[41:]] == types


def test_a_chart_in_a_layout_takes_a_chart_of_the_message_and_a_text_form_takes_none():
    series = [ui.ChartSeries(label="rpm", points=[1])]
    first, second, third = (
        ui.Chart(title=title, categories=["Mon"], series=series)
        for title in ("First", "Second", "Third")
    )
    # A layout holds no markdown. Its text form takes no room for a chart.
    refused_columns = ui.Columns([first, ui.Markdown("**Loud** fan.")])

    parts = get_blocks(
        "text",
        blocks=[refused_columns, ui.Columns([second, ui.Text("Fan noise.")]), third, first],
    )

    assert [part["type"] for part in parts] == [
        "markdown",
        "layout",
        "data_visualization",
        "markdown",
    ]


def test_a_table_in_a_layout_shows_all_of_its_rows_and_one_in_a_text_form_takes_no_room():
    # With its header, the first table takes 16,000 of the 20,000 characters of the cells.
    log = ui.Table(columns=[ui.TableColumn("Log")], rows=[ui.TableRow(["x" * 15_997])])
    fans = ui.Table(
        columns=[ui.TableColumn("Fan")],
        rows=[ui.TableRow(["y" * 1_998]), ui.TableRow(["y" * 1_999])],
    )
    # The cells of these columns need one character more than the message has left.
    long_columns = ui.Columns([fans.model_copy(update={"rows": [*fans.rows, ui.TableRow(["z"])]})])
    refused_columns = ui.Columns([fans, ui.Markdown("**Loud** fan.")])

    data_table, text, layout = get_blocks(
        "text", blocks=[log, long_columns, refused_columns, ui.Columns([fans])]
    )

    assert data_table == _data_table("Log", ["Log"], ["x" * 15_997])
    # Only the top of the message holds the line that says how many rows Druks did not send.
    assert text == {
        "type": "markdown",
        "text": f"{long_columns.to_markdown()}\n\n{refused_columns.to_markdown()}",
    }
    assert layout["rows"] == [
        {"items": [{"block": _data_table("Fan", ["Fan"], ["y" * 1_998], ["y" * 1_999])}]}
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_druks_image_in_a_card_a_container_or_a_layout_goes_up_once_as_a_private_file(
    druks_db, slack, operator
):
    rack, shelf = await _druks_file("rack.png"), await _druks_file("shelf.png")
    rack_card = ui.Card(title="Rack 3", blocks=[ui.Image(url=rack.url, alternative_text="Rack 3.")])
    shelf_card = ui.Card(
        title="Shelf", blocks=[ui.Image(url=shelf.url, alternative_text="The shelf.")]
    )

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(
            ui.Cards(cards=[rack_card, shelf_card]),
            ui.Section(title="Racks", blocks=[ui.Image(url=rack.url, alternative_text="Rack 3.")]),
            ui.Columns([rack_card, ui.Image(url=shelf.url, alternative_text="The shelf.")]),
        ),
    )

    # The message shows each image more than once. Each goes up once.
    assert _list_uploads(slack) == ["rack.png", "shelf.png"]
    (post,) = slack.posts
    carousel, container, layout = post["blocks"]
    assert [card["hero_image"] for card in carousel["elements"]] == [
        _slack_file_block("F1", "Rack 3."),
        _slack_file_block("F2", "The shelf."),
    ]
    assert container["child_blocks"] == [_slack_file_block("F1", "Rack 3.")]
    assert [item["block"] for item in layout["rows"][0]["items"]] == [
        {
            "type": "card",
            "title": _label("Rack 3"),
            "hero_image": _slack_file_block("F1", "Rack 3."),
        },
        _slack_file_block("F2", "The shelf."),
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_card_whose_druks_image_does_not_go_up_shows_its_text_form_with_the_link(
    druks_db, slack, operator
):
    set_endpoint("https://druks.example")
    front, top = await _druks_file("front.png"), await _druks_file("top.png")
    cards = ui.Cards(
        cards=[
            ui.Card(title="Front", blocks=[ui.Image(url=front.url, alternative_text="The front.")]),
            ui.Card(title="Top", blocks=[ui.Image(url=top.url, alternative_text="The top.")]),
        ]
    )
    slack.upload_refusals = ["ratelimited"]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(cards),
    )

    (post,) = slack.posts
    # The list is no carousel. Each of its cards shows on its own.
    assert post["blocks"] == [
        {
            "type": "markdown",
            "text": f"### Front\n\n[The front.](https://druks.example{front.url})",
        },
        {"type": "card", "title": _label("Top"), "hero_image": _slack_file_block("F1", "The top.")},
    ]


@pytest.mark.usefixtures("tmp_data_dir")
async def test_a_message_uploads_no_druks_image_of_a_block_that_shows_its_text_form(
    druks_db, slack, operator
):
    shown, refused, late = [
        await _druks_file(name) for name in ("shown.png", "refused.png", "late.png")
    ]
    refused_section = ui.Section(
        title="Rack 3",
        blocks=[ui.Image(url=refused.url, alternative_text="Rack 3."), ui.Markdown("**Loud**")],
    )
    links = [
        ui.Link(f"Link {number}", url=f"https://docs.example/{number}") for number in range(47)
    ]

    await deliver(
        SlackClient(token=_BOT_TOKEN),
        "C1",
        "text",
        session=druks_db,
        account=operator,
        blocks=_stored(
            ui.Card(title="Shown", blocks=[ui.Image(url=shown.url, alternative_text="Shown.")]),
            refused_section,
            *links,
            # The message has no room left for this card.
            ui.Card(title="Late", blocks=[ui.Image(url=late.url, alternative_text="Late.")]),
        ),
    )

    assert _list_uploads(slack) == ["shown.png"]


def test_a_progress_goes_out_as_a_plan_with_a_task_for_each_step_in_the_state_of_its_tone():
    # Two steps share a label. Slack refuses a plan with two tasks of one id.
    assert get_blocks(
        "text",
        blocks=[
            ui.Progress(
                "Checks",
                steps=[
                    _step("Lint", "passed", "success"),
                    _step("Test", "failed", "danger"),
                    _step("Test", "running", "active"),
                    _step("Deploy", "waiting", "warning"),
                    _step("Announce"),
                ],
            )
        ],
    ) == [
        {
            "type": "plan",
            "title": "Checks",
            "tasks": [
                _task(0, "Lint", "complete"),
                _task(1, "Test", "error"),
                _task(2, "Test", "in_progress"),
                _task(3, "Deploy", "pending"),
                _task(4, "Announce", "pending"),
            ],
        }
    ]


def test_a_timeline_goes_out_as_a_plan_with_a_task_for_each_item_oldest_first():
    # The app gives the items out of order. An item is complete unless its status says
    # otherwise. A timeline with no title gets one.
    assert get_blocks(
        "text",
        blocks=[
            ui.Timeline(
                [
                    _item("Review", 45, status=ui.StatusValue("running", tone="active")),
                    _item("Draft PR opened", 20, description="Opened #12 against main."),
                    _item("Run started", status=ui.StatusValue("started")),
                    _item("Deploy", 50, status=ui.StatusValue("failed", tone="danger")),
                ]
            )
        ],
    ) == [
        {
            "type": "plan",
            "title": TIMELINE_TITLE,
            "tasks": [
                _task(0, "Run started", "complete", "2026-08-29 09:00 UTC"),
                _task(
                    1,
                    "Draft PR opened",
                    "complete",
                    "Opened #12 against main.\n2026-08-29 09:20 UTC",
                ),
                _task(2, "Review", "in_progress", "2026-08-29 09:45 UTC"),
                _task(3, "Deploy", "error", "2026-08-29 09:50 UTC"),
            ],
        }
    ]


def test_a_plan_keeps_its_last_50_tasks_under_a_line_that_says_how_many_it_left_out():
    page = "https://druks.example/runs/run-1"
    steps = [_step(f"Step {number}") for number in range(53)]

    assert get_blocks("text", blocks=[ui.Progress("Build", steps=steps[:50])], deep_link=page) == [
        {
            "type": "plan",
            "title": "Build",
            "tasks": [_task(number, f"Step {number}", "pending") for number in range(50)],
        },
        {"type": "markdown", "text": page},
    ]
    assert get_blocks("text", blocks=[ui.Progress("Build", steps=steps)], deep_link=page) == [
        {
            "type": "markdown",
            "text": PLAN_OMISSION_MESSAGE.format(left=3, total=53)
            + OMISSION_LINK.format(link=page),
        },
        {
            "type": "plan",
            "title": "Build",
            "tasks": [_task(number, f"Step {number}", "pending") for number in range(3, 53)],
        },
        {"type": "markdown", "text": page},
    ]


def test_a_message_draws_its_first_plan_and_the_text_form_of_each_other_one():
    progress = ui.Progress("Build", steps=[_step("Plan", "planning", "active")])
    timeline = ui.Timeline([_item("Run started")], title="Events")
    checks = ui.Progress("Checks", steps=[_step("Lint")])

    # Slack refuses a message with a second plan.
    assert get_blocks("text", blocks=[progress, timeline, checks]) == [
        {"type": "plan", "title": "Build", "tasks": [_task(0, "Plan", "in_progress")]},
        {"type": "markdown", "text": f"{timeline.to_markdown()}\n\n{checks.to_markdown()}"},
    ]


def test_a_progress_with_no_steps_and_a_timeline_with_no_items_are_no_plan():
    share = ui.Progress("Sweeping peers", completed=3, total=8)

    # A timeline with no items shows nothing, as its text form does.
    assert get_blocks("text", blocks=[share, ui.Timeline(title="Events")]) == [
        {"type": "markdown", "text": share.to_markdown()}
    ]


def test_only_the_message_holds_a_plan_and_a_group_with_one_shows_its_text_form():
    progress = ui.Progress("Build", steps=[_step("Plan", "planning", "active")])
    section = ui.Section(title="Rack 3", blocks=[ui.Divider(), progress])
    columns = ui.Columns([_FAN, ui.Timeline([_item("Run started")], title="Events")])

    # A stack adds no level. Its plan is in the message.
    assert get_blocks("text", blocks=[section, columns, ui.Stack([progress])]) == [
        {"type": "markdown", "text": f"{section.to_markdown()}\n\n{columns.to_markdown()}"},
        {"type": "plan", "title": "Build", "tasks": [_task(0, "Plan", "in_progress")]},
    ]
