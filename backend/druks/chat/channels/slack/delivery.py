import asyncio
import contextlib
import itertools
import logging
import re
from collections.abc import Sequence
from functools import partial
from typing import Literal

import aiohttp
from slack_sdk.errors import SlackApiError, SlackRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from druks import ui
from druks.accounts.models import Account
from druks.chat.channels.slack import constants as slack
from druks.chat.exceptions import FormError
from druks.core.apis.slack import SlackClient
from druks.files.constants import FILE_URL_PREFIX
from druks.files.models import FileRecord
from druks.files.storage import get_file_storage
from druks.notifications.buttons import encode_action, encode_button
from druks.notifications.exceptions import (
    BlocksRefusedError,
    ChannelUnavailableError,
    DeliveryError,
)
from druks.registry import app_registry
from druks.settings import get_settings
from druks.ui.blocks import get_absolute_url, iter_parts

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
_BLOCK_REFUSALS = {
    "invalid_blocks",
    "invalid_blocks_format",
    "msg_blocks_too_long",
    "posting_to_channel_denied",
}

# A line that opens or closes a code fence: its indent, three or more backticks or
# tildes, then the rest.
_FENCE = re.compile(r"(\s*)(`{3,}|~{3,})(.*)")

# A link as a text form writes it: its words, each bracket escaped, then its address.
_MARKDOWN_LINK = re.compile(r"\[((?:\\.|[^\\\]])*)\]\(([^)\s]+)\)")

# An outside http(s) address. Its path ends at the query or the fragment.
_OUTSIDE_ADDRESS = re.compile(r"https?://[^/?#]*(?P<path>[^?#]*)", re.IGNORECASE)

SUBMIT_ACTION = {"id": "submit", "label": "Submit"}

# A number field takes text. Slack shows its number input only in a modal.
FIELD_ELEMENTS = {
    "text": "plain_text_input",
    "text_area": "plain_text_input",
    "number": "plain_text_input",
    "select": "static_select",
    "multi_select": "multi_static_select",
    "radio": "radio_buttons",
    "checkbox": "checkboxes",
}

# The last line of a message that Druks cut to fit Slack. It says what Druks cut. When the
# installation has an address, a link to all of it follows.
OMISSION_MESSAGE = "Druks cut the end of this message because Slack limits its length."
OMISSION_LINK = " Read all of it in Druks: {link}"
TABLE_OMISSION_MESSAGE = (
    "Druks did not send {left:,} of the {total:,} rows of this table because Slack limits its size."
)


def cut_markdown(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` characters. Otherwise, the most whole lines of
    ``text`` that fit with the marker after them. A code fence that those lines leave open
    closes before the marker. The marker and what follows it then stay outside the fence.
    When not even the first line fits, its start replaces the lines. A first
    line that opens a fence keeps no start, because a part of a fence closes nothing."""
    if len(text) <= limit:
        return text
    room = limit - len(slack.TRUNCATION_MARKER)
    lines: list[str] = []
    # Each line except the first has a newline before it.
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
                # The closer takes the opener's indent. A fence in a list item then closes in it.
                fence_after = indent + fence
        closer = f"\n{fence_after}" if fence_after else ""
        if size + 1 + len(line) + len(closer) > room:
            break
        lines.append(line)
        size += 1 + len(line)
        open_fence = fence_after
    if lines:
        kept_text = "\n".join(lines).rstrip("\n")
        return kept_text + (f"\n{open_fence}" if open_fence else "") + slack.TRUNCATION_MARKER
    # The loop stopped at the first line. ``closer`` then says whether that line opens a fence.
    if closer:
        return slack.TRUNCATION_MARKER
    return text[:room] + slack.TRUNCATION_MARKER


def get_input(field: dict) -> dict:
    """The input block that shows a form's ``field``. It holds the label, the help text as
    the hint, whether the field is optional, and an element with its default. The id of the
    block is the field's name. Slack sends the answer under the id of the block. Raises
    FormError for a kind of field that Slack has no input for."""
    if field["field"] not in FIELD_ELEMENTS:
        raise FormError(
            f"Slack has no input for field {field['name']!r} of kind {field['field']!r}. "
            "Ask for the field on a page of the app."
        )
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
            if chosen_options := [option for option in options if option["value"] in value]:
                element["initial_options"] = chosen_options
        case {"field": "select" | "radio", "value": value}:
            element["options"] = options
            if chosen_options := [option for option in options if option["value"] == value]:
                element["initial_option"] = chosen_options[0]
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
    limits. The id of the block names its field."""
    name, element = block["block_id"], block["element"]
    options = element.get("options", [])
    options_limit = slack.OPTIONS_LIMITS[element["type"]]
    if len(options) > options_limit:
        raise FormError(
            f"Field {name!r} has {len(options)} options. Slack shows {options_limit} at most "
            f"in this field. Give it {options_limit} options or fewer."
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
                f"{part} of field {name!r} has {len(text)}. Give it 1 to {limit} characters."
            )


def cut_text(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` characters. Otherwise, its start and an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def get_image_block(image: ui.Image, slack_file_ids: dict[str, str] | None) -> dict:
    """The image block that draws ``image``: the Slack file in ``slack_file_ids`` for its
    address, or else its outside address of a png, jpg, or gif. Empty when it has neither,
    or when its alternative text or its caption is too long for Slack."""
    outside_address = _OUTSIDE_ADDRESS.match(image.url)
    address = get_absolute_url(image.url)
    source = {}
    if slack_file_ids and image.url in slack_file_ids:
        source = {"slack_file": {"id": slack_file_ids[image.url]}}
    elif (
        outside_address
        and outside_address["path"].lower().endswith(slack.IMAGE_EXTENSIONS)
        and len(address) <= slack.IMAGE_URL_LIMIT
    ):
        source = {"image_url": address}
    if (
        source
        and len(image.alternative_text) <= slack.IMAGE_ALT_TEXT_LIMIT
        and len(image.caption) <= slack.IMAGE_TITLE_LIMIT
    ):
        block = {"type": "image", **source, "alt_text": image.alternative_text}
        if image.caption:
            block["title"] = {"type": "plain_text", "text": image.caption}
        return block
    return {}


def get_button(action: ui.Action, action_id: str) -> dict:
    """The button that runs ``action``, with Slack's dialog when it asks to confirm. Empty
    when its label is empty, or when its label or its confirm text is too long for Slack."""
    label, confirm = action.label, action.confirm
    if (
        label
        and len(label) <= slack.BUTTON_TEXT_LIMIT
        and len(confirm) <= slack.CONFIRM_TEXT_LIMIT
        and (not confirm or len(label) <= slack.CONFIRM_BUTTON_LIMIT)
    ):
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": action_id,
        }
        if confirm:
            button["confirm"] = {
                "title": {"type": "plain_text", "text": label},
                "text": {"type": "plain_text", "text": confirm},
                "confirm": {"type": "plain_text", "text": label},
                "deny": {"type": "plain_text", "text": slack.CONFIRM_DENY_TEXT},
            }
        return button
    return {}


def get_form(
    position: int, fields: Sequence[ui.Field], actions: dict[str, ui.Action]
) -> list[dict]:
    """An input for each of ``fields``, then a button for each of ``actions``. ``actions``
    holds each action under the id of its button. The id of each input starts with
    ``position``, the position of the block of the fields in the blocks of the notification.
    A button asks with Slack's dialog when its action asks to confirm. When Slack cannot take
    one of the inputs or buttons, the function returns no block. A button then never sends a form
    without all of its inputs."""
    buttons = [get_button(action, action_id) for action_id, action in actions.items()]
    with contextlib.suppress(FormError):
        inputs = [
            {
                **get_input(field.model_dump()),
                "block_id": slack.INPUT_ID_PREFIX.format(position=position) + field.name,
            }
            for field in fields
        ]
        for block in inputs:
            check_input(block)
        if all(buttons):
            return [*inputs, {"type": "actions", "elements": buttons}]
    return []


def escape_mrkdwn(text: str) -> str:
    """``text`` as Slack's mrkdwn shows it. Slack reads &, < and > as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_link_buttons(links: Sequence[ui.Link]) -> list[dict]:
    """A button that opens each of ``links``, in order. A button is empty when Slack cannot
    draw it: its link has no address, or its label is empty, or its label or its address is too
    long for Slack. Each button after the first adds its number to its id. A block then holds no
    two buttons of one id."""
    buttons = []
    for number, link in enumerate(links):
        address = get_absolute_url(link.url)
        action_id = slack.LINK_BUTTON_ACTION_ID
        if number:
            action_id = f"{action_id}:{number}"
        button = {}
        if (
            address
            and link.label
            and len(link.label) <= slack.BUTTON_TEXT_LIMIT
            and len(address) <= slack.BUTTON_URL_LIMIT
        ):
            button = {
                "type": "button",
                "text": {"type": "plain_text", "text": link.label},
                "url": address,
                "action_id": action_id,
            }
        buttons.append(button)
    return buttons


def get_link_actions(links: Sequence[ui.Link]) -> list[dict]:
    """The actions block of a button for each of ``links``. When Slack cannot draw one of the
    buttons, a markdown block holds the text forms of the links in its place. With no links,
    there is no block."""
    buttons = get_link_buttons(links)
    if buttons and all(buttons):
        return [{"type": "actions", "elements": buttons}]
    if text := " · ".join(link.to_markdown() for link in links):
        return [{"type": "markdown", "text": text}]
    return []


def is_simple_card(card: ui.Card) -> bool:
    """Whether ``card`` holds only links and one image at most. A card block of Slack shows no
    other block."""
    kinds = [child.block for child in card.blocks]
    return set(kinds) <= {"link", "image"} and kinds.count("image") <= 1


def get_card_block(card: ui.Card, slack_file_ids: dict[str, str] | None) -> dict:
    """The card block that draws ``card``: its title, its description as the body, its image as
    the hero image, and a button for each of its links. The buttons open the link of the card,
    each link among its blocks, then each link among its controls. The link of the card takes
    the title as its label, as in its text form. Slack runs no action of the controls. Empty
    when the card is not simple, when it shows nothing, or when Slack cannot draw it."""
    images = [child for child in card.blocks if child.block == "image"]
    links = [card.link.model_copy(update={"label": card.title})] if card.link and card.title else []
    links += [child for child in card.blocks if child.block == "link"]
    links += [control for control in card.controls if control.block == "link"]
    buttons = get_link_buttons(links)
    # Slack's image element has no title. A hero image thus shows no caption.
    hero_images = [
        get_image_block(image.model_copy(update={"caption": ""}), slack_file_ids)
        for image in images
    ]
    if (
        is_simple_card(card)
        and (card.title or card.description or images or links)
        and len(card.title) <= slack.CARD_TITLE_LIMIT
        and len(card.description) <= slack.CARD_BODY_LIMIT
        and all(hero_images)
        and all(buttons)
        and len(buttons) <= slack.CARD_BUTTONS_LIMIT
    ):
        card_block = {"type": "card"}
        if card.title:
            card_block["title"] = {"type": "plain_text", "text": card.title}
        if card.description:
            card_block["body"] = {"type": "plain_text", "text": card.description}
        if hero_images:
            card_block["hero_image"] = hero_images[0]
        if buttons:
            card_block["actions"] = buttons
        return card_block
    return {}


def get_container(title: str, subtitle: str, child_blocks: list[dict]) -> dict:
    """The container that holds ``child_blocks`` under ``title`` and ``subtitle``. Empty when
    Slack cannot draw it: with no title, with a title or a subtitle that is too long, with no
    child or more children than Slack takes, or with a child in a markdown block. Only the
    message holds markdown."""
    if (
        0 < len(title) <= slack.CONTAINER_TITLE_LIMIT
        and len(subtitle) <= slack.CONTAINER_TITLE_LIMIT
        and 0 < len(child_blocks) <= slack.CONTAINER_BLOCKS_LIMIT
        and all(child["type"] != "markdown" for child in child_blocks)
    ):
        container = {
            "type": "container",
            "title": {"type": "plain_text", "text": title},
            "child_blocks": child_blocks,
        }
        if subtitle:
            container["subtitle"] = {"type": "plain_text", "text": subtitle}
        return container
    return {}


def list_blocks(parts: list[dict]) -> list[dict]:
    """Each of ``parts`` and each block inside them, at any depth: the children of a
    container, the cards of a carousel, and the block of each item of a layout."""
    blocks = []
    for part in parts:
        blocks.append(part)
        match part:
            case {"type": "container", "child_blocks": children}:
                blocks += list_blocks(children)
            case {"type": "carousel", "elements": cards}:
                blocks += list_blocks(cards)
            case {"type": "layout", "rows": rows}:
                blocks += list_blocks([item["block"] for row in rows for item in row["items"]])
    return blocks


def get_parts(
    block: ui.Block,
    *,
    room: dict[str, int],
    slack_file_ids: dict[str, str] | None,
    deep_link: str | None,
    token: str | None = None,
    index: int = 0,
    parent: Literal["message", "container", "layout"] = "message",
    depth: int = 0,
) -> list[dict]:
    """The blocks of Slack that show ``block`` in ``parent``: the message, a container, or an
    item of a layout. ``depth`` is how many containers and layouts hold ``block``.

    Each kind of Druks block has a branch of its own. Facts and metrics become a section of
    fields for each ten, with the title on the first. A divider becomes a divider. A link
    becomes a button that opens it. In a container or a layout, text becomes a section.

    A table becomes a data table of its first rows that fit. When rows do not fit, a line
    above it says how many rows Druks did not send, with the link. In a layout, a table becomes
    a data table only when all of its rows fit. A table that selects rows becomes its text
    form, which holds its actions.

    A chart becomes a chart of its kind under its title, or under its value label when it has
    no title. Druks cuts each long title of a chart, and each long label of a line, bar, or
    area chart. A pie has a segment for each category. Druks cuts no label of a pie. A
    message draws the first two charts that Slack can draw.

    Only a block at the top of the message has ``token``, and only until its approval has an
    outcome. Such an action becomes an input for each of its fields and a button that
    carries ``token`` and ``index``, the position of the block. An action with no fields,
    alone in a cell of a table, also becomes such a button. A form that sends on a button
    becomes an input for each of its fields and a button for each of its actions. A button
    asks with Slack's dialog when its action asks to confirm.

    An image at an outside address of a png, a jpg, or a gif becomes an image block. So does
    an image whose address has a Slack file in ``slack_file_ids``. A gallery becomes one image
    block for each such image, under its title. Each other image of a gallery keeps its line
    of the text form, in its place.

    A card that holds only links and one image at most becomes a card block. Any other card
    becomes a container of its blocks, under its title and its description. So does a section
    with a title, under its title. The links of the controls of a card or a section become
    buttons. Slack runs no action of the controls. A list of cards becomes a carousel when
    each of its cards becomes a card block. Otherwise, in the message, each of its cards shows
    on its own. Columns become a layout with each column side by side. A carousel, a data
    table, and a chart each take a row of the layout. A stack, a section with no title, and
    more than three columns show their blocks in order.

    Any other block, and a block past a limit of Slack, shows its text form in a markdown
    block. A block whose text form is empty shows nothing. Only the message holds markdown. A
    container or a layout that holds a text form thus shows its own text form.

    ``room`` holds what the message has left for the text of the cells of its data tables,
    under ``cells``, and for its charts, under ``charts``. A block that shows its text form
    takes nothing from it."""
    draw = partial(get_parts, room=room, slack_file_ids=slack_file_ids, deep_link=deep_link)
    room_before = dict(room)
    parts: list[dict] = []
    match block:
        case ui.Facts(title=title, facts=items) | ui.Metrics(title=title, metrics=items):
            texts = [f"*{title}*"]
            texts += [f"*{item.label}*\n{item.value.to_markdown()}" for item in items]
            texts = [escape_mrkdwn(text) for text in texts]
            # Slack writes a link as <address|words>.
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
        case ui.Link():
            parts = get_link_actions([block])
        case ui.Text(text=text) if parent != "message":
            mrkdwn = escape_mrkdwn(text)
            if 0 < len(mrkdwn) <= slack.SECTION_TEXT_LIMIT:
                parts = [{"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}]
        case ui.Table(title=title, columns=columns, rows=rows, actions=[]) if (
            parent != "container" and 0 < len(columns) <= slack.TABLE_COLUMNS_LIMIT
        ):
            labels = [column.label or slack.EMPTY_CELL_TEXT for column in columns]
            size = len("".join(labels))
            body = []

            for row_index, row in enumerate(rows[: slack.TABLE_ROWS_LIMIT]):
                cells, texts = [], []

                for cell_index, value in enumerate(row.cells):
                    text = value.to_markdown() or slack.EMPTY_CELL_TEXT
                    elements, end = [], 0

                    for link in _MARKDOWN_LINK.finditer(text):
                        escaped_words, address = link.groups()
                        words = escaped_words.replace("\\[", "[").replace("\\]", "]")
                        elements.append({"type": "text", "text": text[end : link.start()]})
                        elements.append({"type": "link", "url": address, "text": words})
                        end = link.end()
                    match value:
                        case ui.ControlsValue(controls=[ui.Action(fields=[]) as action]) if (
                            token
                            and (
                                button := get_button(
                                    action, encode_action(token, index, row_index, cell_index)
                                )
                            )
                        ):
                            cells.append({"type": "action_cell", "element": button})
                            # Slack counts the text of a button toward the limit of the cells.
                            text = action.label
                        case ui.NumberValue(number=number):
                            # Slack sorts a column of number cells as numbers.
                            cells.append({"type": "raw_number", "value": number, "text": text})
                        case _ if elements:
                            elements.append({"type": "text", "text": text[end:]})
                            # Slack takes no text element without text.
                            section = {
                                "type": "rich_text_section",
                                "elements": [element for element in elements if element["text"]],
                            }
                            cells.append({"type": "rich_text", "elements": [section]})
                        case _:
                            cells.append({"type": "raw_text", "text": text})
                    texts.append(text)

                if size + len("".join(texts)) > room["cells"]:
                    break
                size += len("".join(texts))
                body.append(cells)

            # Slack draws a data table only with a row under its header.
            if body and (parent == "message" or len(body) == len(rows)):
                room["cells"] -= size
                header = [{"type": "raw_text", "text": label} for label in labels]
                caption = title or labels[0]
                parts = [{"type": "data_table", "caption": caption, "rows": [header, *body]}]
            if parts and len(body) < len(rows):
                notice = TABLE_OMISSION_MESSAGE.format(left=len(rows) - len(body), total=len(rows))

                if deep_link:
                    notice += OMISSION_LINK.format(link=deep_link)
                # Above the table, its link never comes right before the link of the message.
                parts.insert(0, {"type": "markdown", "text": notice})
        case ui.Chart(
            kind="line" | "bar" | "area" as kind,
            title=title,
            categories=categories,
            series=series,
            category_label=category_label,
            value_label=value_label,
        ) if (
            parent != "container"
            and room["charts"]
            and 0 < len(series) <= slack.CHART_SERIES_LIMIT
            and 0 < len(categories) <= slack.CHART_POINTS_LIMIT
        ):
            heading = cut_text(title or value_label, slack.CHART_TITLE_LIMIT)
            labels = [
                cut_text(category or slack.EMPTY_CELL_TEXT, slack.CHART_LABEL_LIMIT)
                for category in categories
            ]
            names = [
                cut_text(one.label or slack.EMPTY_CELL_TEXT, slack.CHART_LABEL_LIMIT)
                for one in series
            ]

            if heading and len(set(labels)) == len(labels) and len(set(names)) == len(names):
                room["charts"] -= 1
                axes: dict = {"categories": labels}

                if category_label:
                    axes["x_label"] = cut_text(category_label, slack.CHART_AXIS_TITLE_LIMIT)
                if value_label:
                    axes["y_label"] = cut_text(value_label, slack.CHART_AXIS_TITLE_LIMIT)
                chart = {
                    "type": kind,
                    "series": [
                        {
                            "name": name,
                            "data": [
                                {"label": label, "value": point}
                                for label, point in zip(labels, one.points, strict=True)
                            ],
                        }
                        for name, one in zip(names, series, strict=True)
                    ],
                    "axis_config": axes,
                }
                parts = [{"type": "data_visualization", "title": heading, "chart": chart}]
        case ui.Chart(
            kind="pie",
            title=title,
            categories=categories,
            series=[one],
            value_label=value_label,
        ) if (
            parent != "container"
            and room["charts"]
            and 0 < len(categories) <= slack.CHART_SEGMENTS_LIMIT
            # A cut label can hide the category of a share. Druks cuts no label of a pie.
            and all(len(category) <= slack.CHART_LABEL_LIMIT for category in categories)
        ):
            heading = cut_text(title or value_label, slack.CHART_TITLE_LIMIT)

            if heading:
                room["charts"] -= 1
                chart = {
                    "type": "pie",
                    "segments": [
                        {"label": category or slack.EMPTY_CELL_TEXT, "value": point}
                        for category, point in zip(categories, one.points, strict=True)
                    ],
                }
                parts = [{"type": "data_visualization", "title": heading, "chart": chart}]
        case ui.Action() if token:
            parts = get_form(index, block.fields, {encode_action(token, index): block})
        case ui.Form(submit="button") if token:
            parts = get_form(
                index,
                block.fields,
                {
                    encode_action(token, index, number): action
                    for number, action in enumerate([block.action, *block.extra_actions])
                },
            )
        case ui.Image():
            if image_block := get_image_block(block, slack_file_ids):
                parts = [image_block]
        case ui.ImageGallery(title=title, images=images) if parent == "message" and any(
            get_image_block(image, slack_file_ids) for image in images
        ):
            parts = [{"type": "markdown", "text": f"### {title}"}] if title else []
            for image in images:
                if image_block := get_image_block(image, slack_file_ids):
                    parts.append(image_block)
                    continue
                line = f"- {image.to_markdown()}"
                if parts and parts[-1]["type"] == "markdown":
                    parts[-1]["text"] += f"\n{line}"
                else:
                    parts.append({"type": "markdown", "text": line})
        case ui.Card() if parent != "container" and (
            card_block := get_card_block(block, slack_file_ids)
        ):
            parts = [card_block]
        case ui.Card(title=title, description=description, link=link) if (
            not is_simple_card(block) and parent != "container" and depth < slack.NESTING_LIMIT
        ):
            # The link of the card takes its title as its label, as in its text form.
            links = [link.model_copy(update={"label": title})] if link else []
            links += [control for control in block.controls if control.block == "link"]
            child_blocks = [
                part
                for child in block.blocks
                for part in draw(child, parent="container", depth=depth + 1)
            ]
            if container := get_container(
                title, description, [*child_blocks, *get_link_actions(links)]
            ):
                parts = [container]
        case ui.Cards(title=title, cards=cards) if cards and parent != "container":
            heading = [{"type": "markdown", "text": f"### {title}"}] if title else []
            card_blocks = [get_card_block(card, slack_file_ids) for card in cards]
            if (
                all(card_blocks)
                and len(card_blocks) <= slack.CAROUSEL_CARDS_LIMIT
                # Only the message holds the title of the list.
                and (parent == "message" or not title)
            ):
                parts = [*heading, {"type": "carousel", "elements": card_blocks}]
            elif parent == "message":
                card_parts = [
                    part for card in cards for part in draw(card, parent=parent, depth=depth)
                ]
                if card_parts:
                    parts = [*heading, *card_parts]
        case ui.Section(title=title, blocks=children, controls=controls):
            # The links of the section come first, as in its text form.
            actions = get_link_actions([control for control in controls if control.block == "link"])
            if not title:
                parts = [
                    *actions,
                    *(
                        part
                        for child in children
                        for part in draw(child, parent=parent, depth=depth)
                    ),
                ]
            elif parent != "container" and depth < slack.NESTING_LIMIT:
                child_blocks = [
                    part
                    for child in children
                    for part in draw(child, parent="container", depth=depth + 1)
                ]
                if container := get_container(title, "", [*actions, *child_blocks]):
                    parts = [container]
        case ui.Columns(blocks=columns) if len(columns) <= slack.LAYOUT_ITEMS_LIMIT:
            # Columns that cannot be a layout here show their text form.
            if parent != "layout" and depth < slack.NESTING_LIMIT:
                # Each column is one item of the layout. An empty column is none.
                items = [draw(column, parent="layout", depth=depth + 1) for column in columns]
                items = [item for item in items if item]
                if items and all(
                    len(item) == 1 and item[0]["type"] != "markdown" for item in items
                ):
                    rows = []
                    for is_wide, group in itertools.groupby(
                        (item for (item,) in items),
                        key=lambda item: item["type"] in slack.WIDE_BLOCK_TYPES,
                    ):
                        row_items = list(group)
                        rows += [[item] for item in row_items] if is_wide else [row_items]
                    layout_rows = [{"items": [{"block": item} for item in row]} for row in rows]
                    parts = [{"type": "layout", "label": slack.LAYOUT_LABEL, "rows": layout_rows}]
        case ui.Stack(blocks=children) | ui.Columns(blocks=children):
            parts = [part for child in children for part in draw(child, parent=parent, depth=depth)]
    if not parts:
        room.update(room_before)
        if text_form := block.to_markdown():
            parts = [{"type": "markdown", "text": text_form}]
    return parts


def get_blocks(
    text: str,
    *,
    blocks: Sequence[ui.Block] = (),
    slack_file_ids: dict[str, str] | None = None,
    question: str = "",
    deep_link: str | None = None,
    actions: list[dict] | None = None,
    fields: list[dict] | None = None,
    token: str | None = None,
    outcome: str = "",
) -> list[dict]:
    """The blocks of a notification's message, in order. First come ``text`` as markdown, or
    ``blocks`` as Slack draws them, with ``deep_link`` on the line after them. A chat agent's
    ``question`` comes after that. Then come an input for each of ``fields``, a button that
    carries ``token`` for each action, and ``outcome``.

    Fields without actions are a form. Its one button is its Submit button. Fields with
    actions are a run's questions and note.

    An input shows the start of a label that is too long for it. The content holds the
    whole label. The message holds no input that Slack refuses. Druks still sends the message,
    with its question in the content.

    ``get_parts`` draws each of ``blocks``. While there is no ``outcome``, the actions of the
    blocks carry ``token``. Text that follows text joins it in one markdown block.

    ``blocks`` use the room that the rest leaves, the question included. Each block inside a
    container, a carousel, or a layout takes room too. When no room is left, the last block
    holds the text forms of the other blocks. A block that does not fit whole, such as a
    gallery, a form, or a container, is one of them. When the markdown is longer than Slack
    accepts, Druks cuts the end of the blocks. A line in place of the link then says so, with
    the link."""
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
            with contextlib.suppress(FormError):
                check_input(block)
                # Slack refuses a message with two blocks of one id.
                inputs_by_id.setdefault(block["block_id"], block)
        inputs = list(inputs_by_id.values())
        # The content and the buttons take a block each. The note is the last input. It stays
        # when the message has no room for every input.
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
    # The question stays whole above its inputs and buttons.
    if question:
        controls.insert(0, {"type": "markdown", "text": question})
    content: list[dict] = []
    draw = partial(
        get_parts,
        room={"cells": slack.MESSAGE_CELL_TEXT_LIMIT, "charts": slack.MESSAGE_CHARTS_LIMIT},
        slack_file_ids=slack_file_ids,
        deep_link=deep_link,
        token=None if outcome else token,
    )

    for index, block in enumerate(blocks):
        parts = draw(block, index=index)
        # One block of the room stays free for the link. When no room is left, its last
        # block holds the other blocks as their text forms.
        is_full = len(list_blocks([*content, *parts])) >= slack.MESSAGE_BLOCKS_LIMIT - len(controls)
        if is_full:
            parts = [{"type": "markdown", "text": ui.Stack(blocks[index:]).to_markdown()}]
        for part in parts:
            if part["type"] == "markdown" and content and content[-1]["type"] == "markdown":
                content[-1] = {
                    "type": "markdown",
                    "text": f"{content[-1]['text']}\n\n{part['text']}",
                }
            else:
                content.append(part)
        if is_full:
            break
    line = deep_link or ""
    markdown_size = sum(len(part["text"]) for part in content if part["type"] == "markdown")
    # The line adds one newline before it.
    if markdown_size + bool(line) + len(line) + len(question) > slack.MARKDOWN_TEXT_LIMIT:
        line = OMISSION_MESSAGE + (OMISSION_LINK.format(link=deep_link) if deep_link else "")
        room = slack.MARKDOWN_TEXT_LIMIT - len(line) - 1 - len(question)
        kept_parts = []
        # The content keeps its start, as a cut body does. Druks removes what follows the cut.
        for part in content:
            if part["type"] == "markdown" and len(part["text"]) > room:
                if room > len(slack.TRUNCATION_MARKER):
                    kept_parts.append(
                        {"type": "markdown", "text": cut_markdown(part["text"], room)}
                    )
                break
            if part["type"] == "markdown":
                room -= len(part["text"])
            kept_parts.append(part)
        content = kept_parts
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
    session: AsyncSession,
    account: Account | None,
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
    username: str | None = None,
    icon_url: str | None = None,
) -> dict:
    """Post ``body`` as markdown, with ``deep_link`` on the line after it. ``blocks`` are
    the Druks blocks whose text form is ``body`` up to its question. Slack draws them in its
    place. When Slack refuses them, Druks sends the body in their place once.

    Each action becomes a button that carries ``token``. A click then answers this
    notification. Each of ``fields`` becomes an input above the buttons. Without actions,
    the button below the inputs is a Submit button that carries ``token``.

    With ``message_ts``, Druks edits that posted message. ``outcome`` then replaces its
    buttons and inputs. With ``user``, only that person sees the message.
    ``question_offset`` is where a chat agent's question starts in ``body``. The question
    keeps its room when Druks cuts the rest to fit, with blocks and without.

    Before the post, Druks uploads to Slack the Druks file of each image that the message
    shows: in an image block, at any depth, or as the hero image of a card. The file is a
    png, a jpg, or a gif of at most 10 MiB, of an app that ``account`` can use. ``account``
    is the account that the message is for. Only a message with no blocks has none. Druks
    reads the record of the file in ``session``. Slack keeps each file private.

    The uploads stop at Slack's answer missing_scope, because each upload after it fails
    too. An image whose upload fails keeps its link. Druks uploads nothing again when it
    sends the body in place of the blocks.

    ``username`` and ``icon_url`` replace the app's name and picture on a new message.
    Returns Slack's answer, which names the message's channel and ts."""
    link = f"\n{deep_link}" if deep_link else ""
    reply, question = body, ""
    if question_offset:
        reply, question = body[:question_offset], body[question_offset:]
    # Slack refuses markdown longer than this. A long body loses its end and keeps the link.
    # The link opens the full text.
    text = (
        cut_markdown(reply, slack.MARKDOWN_TEXT_LIMIT - len(question) - len(link)) + question + link
    )
    message = partial(
        get_blocks, text, actions=actions, fields=fields, token=token, outcome=outcome
    )

    async def post(parts: list[dict]) -> dict:
        # Each raise is from None, because these errors can keep the request. Its headers
        # carry the bot token.
        try:
            if message_ts:
                response = await client.chat_update(
                    channel=channel, ts=message_ts, text=text, blocks=parts
                )
            elif user:
                response = await client.chat_postEphemeral(
                    channel=channel, user=user, text=text, blocks=parts, thread_ts=thread_ts or None
                )
            else:
                response = await client.chat_postMessage(
                    channel=channel,
                    text=text,
                    blocks=parts,
                    thread_ts=thread_ts or None,
                    username=username,
                    icon_url=icon_url,
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
        druks_blocks = ui.Stack(blocks).blocks
        # In a block of its own, the question needs no blank line before it.
        draw = partial(
            message, blocks=druks_blocks, question=question.lstrip("\n"), deep_link=deep_link
        )
        # An image names a Druks file by the address of the file, alone or after the endpoint.
        endpoint = get_settings().urls.endpoint.rstrip("/")
        files = {}
        for image in iter_parts(druks_blocks, ui.Image):
            path = image.url.removeprefix(endpoint)
            if path.startswith(FILE_URL_PREFIX):
                record = await session.get(FileRecord, path.removeprefix(FILE_URL_PREFIX))
                if (
                    record
                    and not record.deleted_at
                    and record.content_type in slack.IMAGE_CONTENT_TYPES
                    and record.size <= slack.IMAGE_BYTES_LIMIT
                    and (app := app_registry.get(record.app))
                    and await account.can_use_app(app)
                ):
                    files[image.url] = record
        # This layout uses the address of each file in place of a Slack file id. An image
        # that keeps its block then names its address.
        drawn = draw(slack_file_ids={url: url for url in files})
        shown_images = [part.get("hero_image", part) for part in list_blocks(drawn)]
        shown_urls = dict.fromkeys(
            image["slack_file"]["id"] for image in shown_images if "slack_file" in image
        )
        slack_file_ids = {}
        for url in shown_urls:
            record = files[url]
            try:
                upload = await client.files_upload_v2(
                    filename=record.name,
                    content=await asyncio.to_thread(get_file_storage().open, record.id),
                )
            except SlackApiError as error:
                reason = error.response.get("error")
                logger.warning(
                    "Slack refused the upload of file %s. Its image goes as a link. %s",
                    record.id,
                    reason,
                )
                if reason == "missing_scope":
                    break
            except (
                FileNotFoundError,
                SlackRequestError,
                aiohttp.ClientError,
                TimeoutError,
            ) as error:
                logger.warning(
                    "The upload of file %s to Slack failed. Its image goes as a link. %s",
                    record.id,
                    type(error).__name__,
                )
            else:
                slack_file_ids[url] = upload["file"]["id"]
        parts = draw(slack_file_ids=slack_file_ids)
        try:
            return await post(parts)
        except BlocksRefusedError as error:
            # Slack can refuse a block that Druks draws. Druks then sends the text form in its
            # place.
            logger.warning(
                "Slack refused the blocks of a message. Druks sends it as text. %s", error
            )
    return await post(message())
