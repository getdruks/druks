import logging
import re
from collections.abc import Sequence
from contextlib import suppress
from functools import partial

import aiohttp
from slack_sdk.errors import SlackApiError

from druks import ui
from druks.chat.channels.slack import constants as slack
from druks.chat.exceptions import FormError
from druks.core.apis.slack import SlackClient
from druks.notifications.buttons import encode_button
from druks.notifications.constants import MARKDOWN_TEXT_LIMIT, TRUNCATION_MARKER
from druks.notifications.exceptions import (
    BlocksRefusedError,
    ChannelUnavailableError,
    DeliveryError,
)
from druks.ui.blocks import get_absolute_url

logger = logging.getLogger(__name__)

# What Slack's answer means when the bot cannot post to the channel, and the fix.
_CHANNEL_REASONS = {
    "not_in_channel": "the bot is not in channel {channel}. Invite the bot to the channel in Slack",
    "channel_not_found": (
        "Slack finds no channel {channel} for the bot. Check the channel ID. "
        "If the channel is private, invite the bot to it"
    ),
    "is_archived": "channel {channel} is archived. Choose another channel",
}

# What Slack answers when it refuses the blocks of a message.
_BLOCK_REFUSALS = {"invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"}

# A line that opens or closes a code fence: its indent, three or more backticks or
# tildes, then the rest.
_FENCE = re.compile(r"(\s*)(`{3,}|~{3,})(.*)")

# A link as a text form writes it: its words, each bracket escaped, then its address.
_MARKDOWN_LINK = re.compile(r"\[((?:\\.|[^\\\]])*)\]\(([^)\s]+)\)")

SUBMIT_ACTION = {"id": "submit", "label": "Submit"}

# A number field takes text: Slack's number input works only in a modal.
FIELD_ELEMENTS = {
    "text": "plain_text_input",
    "text_area": "plain_text_input",
    "number": "plain_text_input",
    "select": "static_select",
    "multi_select": "multi_static_select",
    "radio": "radio_buttons",
    "checkbox": "checkboxes",
}

# The line that ends a message that Druks cut to fit Slack: what Druks left out, then the
# link to all of it when the installation has an address.
OMISSION_MESSAGE = "Druks left out the end of this message because Slack limits its length."
OMISSION_LINK = " Read all of it in Druks: {link}"


def cut_markdown(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` characters. Otherwise, the most whole lines of
    ``text`` that fit with the marker after them. A code fence that those lines leave open
    closes before the marker, so the marker and what follows it stay outside the fence.
    When not even the first line fits, its start takes the place of the lines. A first
    line that opens a fence keeps no start, because a part of a fence closes nothing."""
    if len(text) <= limit:
        return text
    room = limit - len(TRUNCATION_MARKER)
    lines: list[str] = []
    # A newline goes before each line but the first.
    size = -1
    open_fence = ""
    for line in text.split("\n"):
        fence_after = open_fence
        if match := _FENCE.match(line):
            indent, fence, rest = match.groups()
            if open_fence:
                if fence.startswith(open_fence.lstrip()) and not rest.strip():
                    fence_after = ""
            # Backticks with another backtick after them on the line are inline code.
            elif fence[0] == "~" or "`" not in rest:
                # The closer takes the opener's indent, so a fence in a list item closes in it.
                fence_after = indent + fence
        closing = f"\n{fence_after}" if fence_after else ""
        if size + 1 + len(line) + len(closing) > room:
            break
        lines.append(line)
        size += 1 + len(line)
        open_fence = fence_after
    if lines:
        kept = "\n".join(lines).rstrip("\n")
        return kept + (f"\n{open_fence}" if open_fence else "") + TRUNCATION_MARKER
    # The loop stopped at the first line, so ``closing`` says whether that line opens a fence.
    if closing:
        return TRUNCATION_MARKER
    return text[:room] + TRUNCATION_MARKER


def get_input(field: dict) -> dict:
    """The input block that shows a form's ``field``: its label, its help text as the hint,
    whether it is optional, and an element that holds its default. The block's id is the
    field's name, so the answer in the element comes back under that name."""
    element: dict = {"type": FIELD_ELEMENTS[field["field"]]}
    options = [
        {"text": {"type": "plain_text", "text": option["label"]}, "value": option["value"]}
        for option in field.get("options", [])
    ]
    match field:
        case {"field": "text" | "text_area", "value": value, "placeholder": placeholder}:
            element["multiline"] = field["field"] == "text_area"
            if value:
                element["initial_value"] = value
            if placeholder:
                element["placeholder"] = {"type": "plain_text", "text": placeholder}
        case {"field": "number", "value": value} if value is not None:  # ast-grep-ignore: rule-2
            element["initial_value"] = str(value).removesuffix(".0")
        case {"field": "multi_select", "value": value}:
            element["options"] = options
            if chosen := [option for option in options if option["value"] in value]:
                element["initial_options"] = chosen
        case {"field": "select" | "radio", "value": value}:
            element["options"] = options
            if chosen := [option for option in options if option["value"] == value]:
                element["initial_option"] = chosen[0]
        case {"field": "checkbox", "value": value}:
            box = {"text": {"type": "plain_text", "text": "Yes"}, "value": "yes"}
            element["options"] = [box]
            if value:
                element["initial_options"] = [box]
    block = {
        "type": "input",
        "block_id": field["name"],
        "label": {"type": "plain_text", "text": field["label"]},
        "optional": not field["is_required"],
        "element": element,
    }
    if field["help_text"]:
        block["hint"] = {"type": "plain_text", "text": field["help_text"]}
    return block


def check_input(block: dict) -> None:
    """Refuse an input block that Slack refuses: a name, a text, or options past their Slack
    limits. A field's name is the id of its block."""
    name, element = block["block_id"], block["element"]
    options = element.get("options", [])
    if len(options) > slack.OPTIONS_LIMITS[element["type"]]:
        raise FormError(
            f"Field {name!r} has {len(options)} options. Slack shows "
            f"{slack.OPTIONS_LIMITS[element['type']]} at most in this field."
        )
    texts = [
        ("name", name, slack.BLOCK_ID_LIMIT),
        ("label", block["label"]["text"], slack.LABEL_LIMIT),
    ]
    if hint := block.get("hint"):
        texts.append(("help text", hint["text"], slack.HINT_LIMIT))
    if placeholder := element.get("placeholder"):
        texts.append(("placeholder", placeholder["text"], slack.PLACEHOLDER_LIMIT))
    if "initial_value" in element:
        texts.append(("value", element["initial_value"], slack.INITIAL_VALUE_LIMIT))
    for option in options:
        texts.append(("option label", option["text"]["text"], slack.OPTION_TEXT_LIMIT))
        texts.append(("option value", option["value"], slack.OPTION_VALUE_LIMIT))
    for part, text, limit in texts:
        if not 0 < len(text) <= limit:
            raise FormError(
                f"Slack takes 1 to {limit} characters for the {part} of a field. The "
                f"{part} of field {name!r} has {len(text)}."
            )


def cut_text(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` characters. Otherwise, its start and an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def get_blocks(
    text: str,
    *,
    blocks: Sequence[ui.Block] = (),
    deep_link: str | None = None,
    actions: list[dict] | None = None,
    fields: list[dict] | None = None,
    token: str | None = None,
    outcome: str = "",
) -> list[dict]:
    """The blocks of a notification's message: ``text`` as markdown, or ``blocks`` as Slack
    draws them with ``deep_link`` on the line after them, then an input for each of
    ``fields``, a button that carries ``token`` for each action, then ``outcome``. Fields
    without actions are a form, and its one button is its Submit button. Fields with actions
    are a run's questions and note: an input shows the start of a label that is too long for
    it, and the content holds the whole label. An input that Slack refuses stays out, so the
    message still goes out with its question in the content.

    Each Druks block takes a branch of its own. Facts and metrics take a section of fields
    for each ten, with the title on the first. A divider takes a divider, and a link takes a
    button that opens it. Any other block, and a block past a limit of Slack, takes its text
    form, and text that follows text joins it in one markdown block. ``blocks`` take the
    room that the rest leaves, and when it runs out, its last block holds the text forms of
    the blocks left. When the markdown is longer than Slack takes, Druks cuts its end, and a
    line in place of the link says so, with the link."""
    controls: list[dict] = []
    if fields and actions:
        inputs_by_id = {}
        for field in fields:
            block = get_input(
                {
                    **field,
                    "label": cut_text(field["label"], slack.LABEL_LIMIT),
                    "options": [
                        {
                            **option,
                            "label": cut_text(option["label"], slack.OPTION_TEXT_LIMIT),
                        }
                        for option in field.get("options", [])
                    ],
                }
            )
            with suppress(FormError):
                check_input(block)
                # Slack refuses a message with two blocks of one id.
                inputs_by_id.setdefault(block["block_id"], block)
        inputs = list(inputs_by_id.values())
        # The content and the buttons take a block each. The note is the last input, and it
        # stays when the message has no room for every input.
        room = slack.MESSAGE_BLOCKS_LIMIT - 2
        controls += inputs if len(inputs) <= room else [*inputs[: room - 1], inputs[-1]]
    elif fields:
        controls += [get_input(field) for field in fields]
        actions = [SUBMIT_ACTION]
    if actions:
        controls.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": action["label"]},
                        "action_id": encode_button(token, action["id"]),
                    }
                    for action in actions
                ],
            }
        )
    if outcome:
        section = {"type": "mrkdwn", "text": cut_markdown(outcome, slack.SECTION_TEXT_LIMIT)}
        controls.append({"type": "section", "text": section})
    if not blocks:
        return [{"type": "markdown", "text": text}, *controls]
    content: list[dict] = []
    for index, block in enumerate(blocks):
        parts: list[dict] = []
        match block:
            case ui.Facts(title=title, facts=items) | ui.Metrics(title=title, metrics=items):
                texts = [f"*{title}*"]
                texts += [f"*{item.label}*\n{item.value.to_markdown()}" for item in items]
                # Slack reads &, < and > as markup, and writes a link as <address|words>.
                texts = [
                    text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    for text in texts
                ]
                heading, *cells = [
                    _MARKDOWN_LINK.sub(r"<\2|\1>", text).replace("\\[", "[").replace("\\]", "]")
                    for text in texts
                ]
                if (
                    cells
                    and len(heading) <= slack.SECTION_TEXT_LIMIT
                    and all(len(cell) <= slack.FIELD_TEXT_LIMIT for cell in cells)
                ):
                    parts = [
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": cell}
                                for cell in cells[start : start + slack.SECTION_FIELDS_LIMIT]
                            ],
                        }
                        for start in range(0, len(cells), slack.SECTION_FIELDS_LIMIT)
                    ]
                    if title:
                        parts[0]["text"] = {"type": "mrkdwn", "text": heading}
            case ui.Divider():
                parts = [{"type": "divider"}]
            case ui.Link(label=label, url=url):
                address = get_absolute_url(url)
                if (
                    address
                    and label
                    and len(label) <= slack.BUTTON_TEXT_LIMIT
                    and len(address) <= slack.BUTTON_URL_LIMIT
                ):
                    button = {
                        "type": "button",
                        "text": {"type": "plain_text", "text": label},
                        "url": address,
                        "action_id": slack.LINK_BUTTON_ACTION_ID,
                    }
                    parts = [{"type": "actions", "elements": [button]}]
        written = "" if parts else block.to_markdown()
        # One block of the room stays free for the link. When the room runs out, its last
        # block takes the blocks left as their text forms.
        count = len(content) + len(parts) + bool(written)
        is_full = count >= slack.MESSAGE_BLOCKS_LIMIT - len(controls)
        if is_full:
            parts, written = [], ui.Stack(blocks[index:]).to_markdown()
        if written and content and content[-1]["type"] == "markdown":
            content[-1] = {"type": "markdown", "text": f"{content[-1]['text']}\n\n{written}"}
        elif written:
            content.append({"type": "markdown", "text": written})
        content += parts
        if is_full:
            break
    line = deep_link or ""
    markdown = sum(len(part["text"]) for part in content if part["type"] == "markdown")
    # A line takes a newline before it.
    if markdown + bool(line) + len(line) > MARKDOWN_TEXT_LIMIT:
        line = OMISSION_MESSAGE + (OMISSION_LINK.format(link=deep_link) if deep_link else "")
        room = MARKDOWN_TEXT_LIMIT - len(line) - 1
        kept = []
        # The content keeps its start, as a cut body does, and what follows the cut goes.
        for part in content:
            if part["type"] == "markdown" and len(part["text"]) > room:
                if room > len(TRUNCATION_MARKER):
                    kept.append({"type": "markdown", "text": cut_markdown(part["text"], room)})
                break
            if part["type"] == "markdown":
                room -= len(part["text"])
            kept.append(part)
        content = kept
    if line and content and content[-1]["type"] == "markdown":
        content[-1] = {"type": "markdown", "text": f"{content[-1]['text']}\n{line}"}
    elif line:
        content.append({"type": "markdown", "text": line})
    return [*content, *controls]


async def deliver(
    client: SlackClient,
    channel: str,
    body: str,
    *,
    blocks: list[dict] | None = None,
    thread_ts: str = "",
    message_ts: str = "",
    user: str = "",
    actions: list[dict] | None = None,
    fields: list[dict] | None = None,
    token: str | None = None,
    deep_link: str | None = None,
    outcome: str = "",
    question_offset: int | None = None,
) -> dict:
    """Post ``body`` as markdown, with ``deep_link`` on the line after it. ``blocks`` are
    the Druks blocks whose text form is ``body``: Slack draws them in its place, and when
    Slack refuses them, the body goes out in their place once. Each action becomes a button
    that carries ``token``, so a click answers this notification. Each of ``fields``
    becomes an input above the buttons. Without actions, the button below the inputs is a
    Submit button that carries ``token``. With
    ``message_ts``, Druks edits that posted message, and ``outcome`` takes the place of its
    buttons and inputs. With ``user``, only that person sees the message.
    ``question_offset`` is where a chat agent's question starts in ``body``. Returns
    Slack's answer, which names the message's channel and ts."""
    link = f"\n{deep_link}" if deep_link else ""
    reply, question = body, ""
    if question_offset:
        reply, question = body[:question_offset], body[question_offset:]
    # Slack refuses markdown longer than this, so a long body loses its end and
    # keeps the link, which opens the full text.
    text = cut_markdown(reply, MARKDOWN_TEXT_LIMIT - len(question) - len(link)) + question + link
    message = partial(
        get_blocks, text, actions=actions, fields=fields, token=token, outcome=outcome
    )

    async def post(drawn: list[dict]) -> dict:
        # from None: these errors can keep the request, and its headers carry the bot token.
        try:
            if message_ts:
                response = await client.chat_update(
                    channel=channel, ts=message_ts, text=text, blocks=drawn
                )
            elif user:
                response = await client.chat_postEphemeral(
                    channel=channel, user=user, text=text, blocks=drawn, thread_ts=thread_ts or None
                )
            else:
                response = await client.chat_postMessage(
                    channel=channel, text=text, blocks=drawn, thread_ts=thread_ts or None
                )
        except SlackApiError as error:
            reason = error.response.get("error") or f"HTTP {error.response.status_code}"
            if reason in _CHANNEL_REASONS:
                raise ChannelUnavailableError(
                    _CHANNEL_REASONS[reason].format(channel=channel)
                ) from None
            if reason in _BLOCK_REFUSALS:
                raise BlocksRefusedError(reason) from None
            raise DeliveryError(reason) from None
        except (aiohttp.ClientError, TimeoutError) as error:
            raise DeliveryError(type(error).__name__) from None
        return response.data

    if blocks:
        try:
            return await post(message(blocks=ui.Stack(blocks).blocks, deep_link=deep_link))
        except BlocksRefusedError as error:
            # Slack can refuse a block that Druks draws. The text form then goes in its place.
            logger.warning("Slack refused the blocks of a message, so it goes as text. %s", error)
    return await post(message())
