import re
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar
from urllib.parse import quote

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    StringConstraints,
    computed_field,
    model_serializer,
    model_validator,
)

from druks.schemas import Schema
from druks.settings import get_settings

from .exceptions import NoTextFormError
from .fields import Field as FormField

if TYPE_CHECKING:
    from .page import PageRoute

Part = TypeVar("Part", bound=Schema)

# An address that names its own host, and a path from the root of the dashboard.
_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)
_DASHBOARD_PATH = re.compile(r"/(?!/)")
# quote() leaves the delimiters of an address and its encoded octets as they are.
_ADDRESS_MARKS = ":/?#[]@!$&'*+,;=%"

# The rule under the header of a table, for the alignment of each column.
_ALIGNMENT_RULE = {"start": "---", "end": "---:"}


def _subject_identity(value):
    """``follows=`` takes the subject that a page or a region watches, or a subject
    class for every subject of that type. Druks streams these subjects. It
    rereads the page on each snapshot that it sends."""
    if not value or isinstance(value, dict | Follows):
        return value
    if isinstance(value, type):
        subject_type = getattr(value, "subject_type", "")
        if subject_type:
            return {"subject_type": subject_type, "subject_id": ""}
    else:
        identity = getattr(value, "identity", None)
        if identity:
            # A subject id goes to the stream in a URL, as text.
            return {"subject_type": identity["type"], "subject_id": str(identity["id"])}
    raise ValueError(
        "follows= takes the subject that a page watches, or its class for every subject of "
        f"that type, not {type(value).__name__}. The stream carries subjects, not runs. Give "
        "the subject itself."
    )


class Follows(Schema):
    """The subject that a page or a named region watches. An empty ``subject_id``
    watches every subject of the type."""

    subject_type: str
    subject_id: str = ""


Watched = Annotated[Follows | None, BeforeValidator(_subject_identity)]


def _one_subject(follows: Follows) -> Follows:
    if follows.subject_id:
        return follows
    raise ValueError(
        f"this shows the work on one {follows.subject_type}, not on every one. "
        "Give the subject itself."
    )


Subject = Annotated[Follows, BeforeValidator(_subject_identity), AfterValidator(_one_subject)]


def _get_status(subject: Follows, info: SerializationInfo) -> dict:
    # A read of a page or of an ask puts the status of each subject in the context.
    status = info.context["statuses"][(subject.subject_type, subject.subject_id)]
    return status.model_dump(mode=info.mode, by_alias=info.by_alias)


def _check_field_names(*, owner: str, fields: list[FormField], arguments: dict) -> None:
    names = [field.name for field in fields]
    repeated_names = sorted({name for name in names if names.count(name) > 1})
    if repeated_names:
        raise ValueError(
            f"{owner} has two fields named {repeated_names}. One then takes the value of the "
            "other. Give each field its own name."
        )
    taken = sorted(set(names) & set(arguments))
    if taken:
        raise ValueError(
            f"{owner} has fields named {taken}, which it already carries as arguments. "
            "Send each value once."
        )


def iter_parts(parts: Iterable[Schema], *kinds: type[Part]) -> Iterator[Part]:
    """Every block, value, and control of ``kinds`` among ``parts`` and inside them at
    any depth, in the order that they hold them."""
    for part in parts:
        if isinstance(part, kinds):
            yield part
        for _, value in part:
            children = [value]
            if isinstance(value, list):
                children = value
            yield from iter_parts(
                [child for child in children if isinstance(child, Schema)], *kinds
            )


def _join(parts: Iterable[str]) -> str:
    return "\n\n".join(part for part in parts if part)


def _heading(title: str) -> str:
    if title:
        return f"### {title}"
    return ""


def _titled(title: str, body: str) -> str:
    # An empty block writes nothing, not even its title. The dashboard shows nothing for it.
    if body:
        return _join([_heading(title), body])
    return ""


def _bullets(items: Iterable[str]) -> str:
    # Druks indents the next lines of an item. An item of two paragraphs then stays one item.
    return "\n".join("- " + item.replace("\n", "\n  ") for item in items)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.split("\n"))


def _number(number: float) -> str:
    # The dashboard shows a number grouped, in full, and never in exponent form.
    return f"{Decimal(str(number)).normalize():,f}"


def _time(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _table(header: list[str], rows: Iterable[list[str]], aligns: Sequence[str]) -> str:
    rule = [_ALIGNMENT_RULE[align] for align in aligns]
    # A pipe ends a cell. A line break ends a row. A cell keeps neither.
    return "\n".join(
        "| " + " | ".join(" ".join(cell.replace("|", "\\|").splitlines()) for cell in line) + " |"
        for line in [header, rule, *rows]
    )


def get_absolute_url(path: str) -> str:
    """``path`` as an address that opens outside the dashboard. An outside http(s)
    address stays as written. A path on the dashboard starts with ``urls.endpoint``.
    Druks encodes a space or a parenthesis. Neither can then end a link. Any other
    address, or a path on an installation with no endpoint, gives an empty string."""
    endpoint = get_settings().urls.endpoint.rstrip("/")
    address = ""
    if _ABSOLUTE_URL.match(path):
        address = path
    elif _DASHBOARD_PATH.match(path) and endpoint:
        address = f"{endpoint}{path}"
    return quote(address, safe=_ADDRESS_MARKS)


def _link(words: str, path: str) -> str:
    if address := get_absolute_url(path):
        # A bracket ends the words of a link.
        words = words.replace("[", "\\[").replace("]", "\\]")
        return f"[{words}]({address})"
    return words


def _controls(controls: "Sequence[Action | Link]") -> str:
    return " · ".join(control.to_markdown() for control in controls)


def _linked(words: str, link: "Link | None") -> str:
    # A link on a value or a card takes their words as its label.
    if link and words:
        return link.model_copy(update={"label": words}).to_markdown()
    return words


class PageBlock(Schema):
    """What every block shares. ``block`` names its kind on the wire.
    ``check_placement`` refuses a place that is wrong for the block.
    ``to_markdown`` gives its text form."""

    block: str

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        """Raise when this block cannot be where the page put it. ``followed``
        tells whether an ancestor watches a subject. ``region`` names the nearest
        region around the block. ``regions`` holds the region names already in use."""

    def to_markdown(self) -> str:
        """The block as markdown that names no channel. A link with an address in
        its ``url`` stays a link. A link without one shows only its words. The controls
        of a gate have no text form. They define no ``to_markdown``. An action and a form
        have a text form only when they hold the address of their page."""
        raise NoTextFormError(type(self).__name__)

    def subjects(self) -> list[tuple[str, str]]:
        """The ``(subject_type, subject_id)`` of every status and gate in this block, at
        any depth. A read uses them to get the status of each subject."""
        return [
            (part.subject.subject_type, part.subject.subject_id)
            for part in iter_parts([self], SubjectStatus, GateControls)
        ]


class BlockParent(PageBlock):
    """A block that holds other blocks."""

    blocks: list["Block"] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for block in self.blocks:
            block.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        return _join(block.to_markdown() for block in self.blocks)


class Link(PageBlock):
    """A control that opens another page of this app, the platform page of a
    subject, or an outside address. A link on a value takes the words of the
    value. It needs no label of its own."""

    # A route argument goes in a URL. A number becomes its text.
    model_config = ConfigDict(coerce_numbers_to_str=True)

    block: Literal["link"] = "link"
    label: str = ""
    page: str = ""
    arguments: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    subject: Watched = None

    def __init__(self, label: str = "", **data):
        super().__init__(label=label, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.label:
            return
        raise ValueError(
            "a Link that stands on its own has no label. Give it the words that it shows."
        )

    @model_validator(mode="after")
    def _one_destination(self) -> "Link":
        if self.subject and not self.subject.subject_id:
            raise ValueError(
                f"Link {self.label!r} names the {self.subject.subject_type} type. A link "
                "opens one subject's page. Give the subject itself."
            )
        if [bool(self.page), bool(self.url), bool(self.subject)].count(True) == 1:
            return self
        raise ValueError(
            f"Link {self.label!r} sets none or more than one of page, url, and subject. "
            "Set exactly one of them."
        )

    def get_address(self, app: str, pages: Sequence["PageRoute"]) -> str:
        """Where the link opens. That is its ``url``, or the dashboard path of its page
        or its subject, as ``app`` routes it with ``pages``. A subject opens the page
        that the app declares for it, or else its own page. A page that is not in
        ``pages`` gives an empty string."""
        address = self.url
        if self.subject:
            subject_type, subject_id = self.subject.subject_type, self.subject.subject_id
            address = f"/{app}/{quote(subject_type, safe='')}/{quote(subject_id, safe='')}"
            for page in pages:
                if page.subject and page.subject.subject_type == subject_type:
                    address = f"/{app}{page.get_path(dict.fromkeys(page.parameters, subject_id))}"
        elif self.page:
            for page in pages:
                # A link without an argument of its page's route has no address.
                if page.name == self.page and set(page.parameters) <= set(self.arguments):
                    address = f"/{app}{page.get_path(self.arguments)}".rstrip("/")
        return address

    def to_markdown(self) -> str:
        return _link(self.label, self.url)


class Action(PageBlock):
    """A control that calls one of the app's own operations. ``operation`` is the
    ``operation_id`` of a route. The shell finds the method and the URL of that
    route. The author writes no URL.

    After a send, the shell opens ``link`` when the action sets it. It also opens
    the URL when the operation answers exactly ``{"url": ...}`` with an absolute
    http(s) URL. ``refresh`` applies only when the shell opens neither."""

    block: Literal["action"] = "action"
    label: str
    operation: str
    arguments: dict = Field(default_factory=dict)
    fields: list[FormField] = Field(default_factory=list)
    tone: Literal["default", "primary", "danger"] = "default"
    # Non-empty text asks the operator before the shell sends anything.
    confirm: str = ""
    refresh: Literal["none", "page", "region"] = "page"
    link: Link | None = None
    # The page where a person runs the action. Druks names it in the context that
    # ``review()`` stores.
    url: str = Field(default="", exclude_if=lambda url: not url)

    @model_serializer(mode="wrap")
    def _with_url(self, serialize: SerializerFunctionWrapHandler, info: SerializationInfo) -> dict:
        if info.context and "action_url" in info.context:
            return {**serialize(self), "url": info.context["action_url"]}
        return serialize(self)

    @model_validator(mode="after")
    def _one_shape(self) -> "Action":
        if self.fields and self.confirm:
            raise ValueError(
                f"Action {self.label!r} gives both fields and confirm. Each asks the operator "
                "before the action runs. Give one or the other."
            )
        return self

    @model_validator(mode="after")
    def _one_name_for_each_value(self) -> "Action":
        _check_field_names(
            owner=f"action {self.label!r}",
            fields=self.fields,
            arguments=self.arguments,
        )
        return self

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.refresh != "region" or region:
            return
        raise ValueError(
            f"action {self.label!r} refreshes its region. It is in no region. Put it in a "
            'named Section, or refresh the page with refresh="page".'
        )

    def check_operation(self, app_name: str, operations) -> None:
        """The operation must be one of the app's own writes."""
        operation = operations.get(self.operation)
        if not operation:
            raise ValueError(
                f"app {app_name!r} action {self.label!r} names operation "
                f"{self.operation!r}, which none of its routes declares. This app declares "
                f"{sorted(operations)}. Name one of these operations."
            )
        if operation.method == "GET":
            raise ValueError(
                f"app {app_name!r} action {self.label!r} names {self.operation!r}, a GET "
                "route. A GET route is a read. It can never be an action. Name an operation "
                "that writes."
            )

    def to_markdown(self) -> str:
        # Only an action that holds the address of its page has a text form: its label,
        # linked to the page.
        if self.url:
            return _link(self.label, self.url)
        raise NoTextFormError(type(self).__name__)


class Form(PageBlock):
    """Inputs and the action that submits them. The shell sends the action's
    arguments and the field values as one object. ``extra_actions`` are more
    buttons that send the same fields. ``submit="change"`` sends on blur for
    text and on change for a select, with no button."""

    block: Literal["form"] = "form"
    title: str = ""
    description: str = ""
    fields: list[FormField] = Field(default_factory=list)
    action: Action
    extra_actions: list[Action] = Field(default_factory=list)
    submit: Literal["button", "change"] = "button"
    layout: Literal["stack", "prose", "row"] = "stack"

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        self.action.check_placement(followed=followed, regions=regions, region=region)
        for extra in self.extra_actions:
            extra.check_placement(followed=followed, regions=regions, region=region)

    @model_validator(mode="after")
    def _one_name_for_each_value(self) -> "Form":
        if self.action.fields or any(extra.fields for extra in self.extra_actions):
            raise ValueError(
                f"form {self.title!r} has fields on its action. Put all form fields on the form."
            )
        if self.submit == "change" and self.extra_actions:
            raise ValueError(
                f"form {self.title!r} submits on change and also has extra actions. "
                "An extra action is a button. Give the form a button, or remove extra_actions."
            )
        if self.submit == "change" and self.action.confirm:
            raise ValueError(
                f"form {self.title!r} submits on change and also asks to confirm. "
                "A confirm needs a button. Give the form a button, or remove confirm."
            )
        _check_field_names(
            owner=f"form {self.title!r}",
            fields=self.fields,
            arguments=self.action.arguments,
        )
        for extra in self.extra_actions:
            _check_field_names(
                owner=f"form {self.title!r} action {extra.label!r}",
                fields=self.fields,
                arguments=extra.arguments,
            )
        return self

    def to_markdown(self) -> str:
        # Only a form that holds the address of its page has a text form. Its actions link to
        # that page.
        if self.action.url:
            return _titled(
                self.title,
                _join([self.description, _controls([self.action, *self.extra_actions])]),
            )
        raise NoTextFormError(type(self).__name__)


class Text(PageBlock):
    block: Literal["text"] = "text"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(self) -> str:
        return self.text


class Markdown(PageBlock):
    block: Literal["markdown"] = "markdown"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(self) -> str:
        return self.text


class Quote(PageBlock):
    """The words of someone else, as they arrived, such as a message, a reply,
    or an answer. The shell keeps the line breaks. It reads nothing as markup."""

    block: Literal["quote"] = "quote"
    text: str

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(self) -> str:
        return _quote(self.text)


class Callout(PageBlock):
    """A short message that the reader must not miss. The tone selects the
    presentation. The app writes the words. ``controls`` are the next step
    that the message points at."""

    block: Literal["callout"] = "callout"
    tone: Literal["info", "success", "warning", "danger"] = "info"
    title: str = ""
    text: str
    controls: list[Action | Link] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def __init__(self, text: str, **data):
        super().__init__(text=text, **data)

    def to_markdown(self) -> str:
        title = ""
        if self.title:
            title = f"**{self.title}**"
        return _quote(
            _join(
                [
                    title,
                    self.text,
                    _controls(self.controls),
                ]
            )
        )


class Divider(PageBlock):
    block: Literal["divider"] = "divider"

    def to_markdown(self) -> str:
        return "---"


class EmptyState(PageBlock):
    """What a page shows when it has no content."""

    block: Literal["empty_state"] = "empty_state"
    title: str
    description: str = ""
    controls: list[Action | Link] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def __init__(self, title: str, **data):
        super().__init__(title=title, **data)

    def to_markdown(self) -> str:
        return _join(
            [
                self.title,
                self.description,
                _controls(self.controls),
            ]
        )


class PageValue(Schema):
    """What every value shares."""

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        """Raise when this value cannot be where the page put it."""

    def to_markdown(self) -> str:
        """The value as markdown, the way a fact, a list item, or a table cell
        writes it."""
        raise NotImplementedError(
            f"{type(self).__name__} has no to_markdown(). Define one on {type(self).__name__}."
        )


class TextValue(PageValue):
    """Words. ``link`` opens another page from a table cell, a fact, or a list
    item."""

    def __init__(self, text, **data):
        super().__init__(text=text, **data)

    value: Literal["text"] = "text"
    text: str
    description: str = ""
    link: Link | None = None

    def to_markdown(self) -> str:
        return _linked(self.text, self.link)


class NumberValue(PageValue):
    value: Literal["number"] = "number"
    number: float = Field(allow_inf_nan=False)
    unit: str = ""
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"

    def __init__(self, number, **data):
        super().__init__(number=number, **data)

    def to_markdown(self) -> str:
        if self.unit:
            return f"{_number(self.number)} {self.unit}"
        return _number(self.number)


class StatusValue(PageValue):
    """Where something stands. The app writes the word. The tone selects the
    presentation. ``link`` opens the thing that the word names."""

    value: Literal["status"] = "status"
    label: str
    tone: Literal["neutral", "active", "success", "warning", "danger"] = "neutral"
    link: Link | None = None

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)

    def to_markdown(self) -> str:
        return _linked(self.label, self.link)


class SubjectStatus(PageValue):
    """Where the work on one subject stands. The shell shows "needs you" when
    the subject waits on the operator. It shows ``working`` while a run of the
    subject is scheduled or running. Otherwise it shows what stopped the run, or
    "idle". Druks reads ``status`` each time it serves the value. A stored value
    keeps no status."""

    value: Literal["subject_status"] = "subject_status"
    subject: Subject
    working: str = "working"
    status: dict | None = Field(default=None, exclude_if=lambda status: not status)

    def __init__(self, subject, **data):
        super().__init__(subject=subject, **data)

    @model_serializer(mode="wrap")
    def _with_status(
        self, serialize: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict:
        if info.context and "statuses" in info.context:
            return {**serialize(self), "status": _get_status(self.subject, info)}
        return serialize(self)

    def to_markdown(self) -> str:
        # The word that the dashboard writes. A stored value has no status to read.
        if not self.status:
            return ""
        if self.status["gate"]:
            return "needs you"
        if self.status["state"] in ("failed", "cancelled", "orphaned"):
            return self.status["state"]
        if self.status["state"] in ("scheduled", "running"):
            return self.working
        return "idle"


class GateControls(PageBlock):
    """The operator's answer to what a subject waits on. The shell reads the
    ask, the options, and the artifact. It shows nothing while the subject
    waits on nothing."""

    block: Literal["gate_controls"] = "gate_controls"
    subject: Subject

    def __init__(self, subject, **data):
        super().__init__(subject=subject, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if followed:
            return
        raise ValueError(
            "GateControls is in nothing that follows a subject. An answered gate then stays "
            "on screen. Put it in a Page or Section with follows=."
        )

    @model_serializer(mode="wrap")
    def _with_status(
        self, serialize: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict:
        # Only a read has the statuses. A stored block keeps no status.
        if info.context and "statuses" in info.context:
            return {**serialize(self), "status": _get_status(self.subject, info)}
        return serialize(self)


class TimeValue(PageValue):
    """A moment. ``empty`` is the word that the shell shows when there is none."""

    value: Literal["time"] = "time"
    when: AwareDatetime | None
    empty: str = ""

    def __init__(self, when, **data):
        super().__init__(when=when, **data)

    def to_markdown(self) -> str:
        if self.when:
            return _time(self.when)
        return self.empty


class ControlsValue(PageValue):
    """Actions and links in a cell, a fact, or a list item."""

    def __init__(self, controls=(), **data):
        super().__init__(controls=controls, **data)

    value: Literal["controls"] = "controls"
    controls: list[Action | Link] = Field(default_factory=list)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        return _controls(self.controls)


def _typed_value(value):
    if isinstance(value, str):
        return TextValue(value)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return NumberValue(value)
    if isinstance(value, PageValue | dict):
        return value
    raise ValueError(
        f"this takes a string, a number, or a Value, not {type(value).__name__}. "
        "Give the words to show, or a Value such as a StatusValue or a TimeValue."
    )


Value = Annotated[
    TextValue | NumberValue | StatusValue | SubjectStatus | TimeValue | ControlsValue,
    Discriminator("value"),
    BeforeValidator(_typed_value),
]


class TimelineItem(Schema):
    # An aware time lets Druks order items from different sources.
    when: AwareDatetime
    title: str
    description: str = ""
    status: StatusValue | None = None


class Timeline(PageBlock):
    """What happened, in order."""

    block: Literal["timeline"] = "timeline"
    title: str = ""
    items: list[TimelineItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _oldest_first(self) -> "Timeline":
        # Druks sorts here, at full precision. A browser compares only milliseconds.
        # It treats two times that differ by a few microseconds as the same moment. The sort is
        # stable. Items with the same time keep the order that the app gave them.
        self.items.sort(key=lambda item: item.when)
        return self

    def __init__(self, items=(), **data):
        super().__init__(items=items, **data)

    def to_markdown(self) -> str:
        return _titled(
            self.title, _bullets(f"{_time(item.when)}: {item.title}" for item in self.items)
        )


class ProgressStep(Schema):
    label: str
    status: StatusValue


class Progress(PageBlock):
    """How much of the work is done. For a share of ``total``, set ``completed``. For staged
    work, give ``steps``. When the end is unknown, give neither."""

    block: Literal["progress"] = "progress"
    label: str
    completed: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    total: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    steps: list[ProgressStep] = Field(default_factory=list)

    def __init__(self, label: str, **data):
        super().__init__(label=label, **data)

    @model_validator(mode="after")
    def _one_shape(self) -> "Progress":
        if self.completed is None:
            return self
        if self.steps:
            raise ValueError(
                f"Progress {self.label!r} gives both completed and steps. The shell shows staged "
                "work as its steps. Give one or the other."
            )
        if self.completed > self.total:
            raise ValueError(
                f"Progress {self.label!r} sets completed to {self.completed}, more than its "
                f"total {self.total}. Set completed to at most total."
            )
        return self

    def to_markdown(self) -> str:
        if self.steps:
            steps = ((step.label, step.status.to_markdown()) for step in self.steps)
            return _join([self.label, _bullets(f"{label}: {status}" for label, status in steps)])
        if self.completed is None:
            return f"{self.label}: still running"
        return f"{self.label}: {_number(self.completed)} of {_number(self.total)}"


class Image(PageBlock):
    """One image. ``alternative_text`` is what a reader gets in place of the
    image. It says what the image shows, not that it is an image."""

    block: Literal["image"] = "image"
    url: str
    alternative_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    caption: str = ""

    def to_markdown(self) -> str:
        return _link(self.alternative_text, self.url)


class FileSummary(Schema):
    """One file, as the shell shows it. Druks builds the download address from
    the id. A download then always uses the platform's own route and its
    identity check."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    content_type: str
    size: int

    @computed_field
    @property
    def url(self) -> str:
        return f"/api/files/{self.id}"


class Files(PageBlock):
    """Files that an operator can preview or download. ``files`` takes
    ``druks.files.File`` objects."""

    block: Literal["files"] = "files"
    title: str = ""
    files: list[FileSummary] = Field(default_factory=list)

    def __init__(self, files=(), **data):
        super().__init__(files=files, **data)

    def to_markdown(self) -> str:
        return _titled(self.title, _bullets(_link(file.name, file.url) for file in self.files))


class ChartSeries(Schema):
    label: str
    points: list[Annotated[float, Field(allow_inf_nan=False)]]


class Chart(PageBlock):
    """Numbers over categories. Every series carries one point for each
    category. The shell then reads the same table that the chart draws. A pie
    draws each point of its one series as a share of the whole. Each point of a
    pie is more than zero."""

    block: Literal["chart"] = "chart"
    kind: Literal["line", "bar", "area", "pie"] = "line"
    title: str = ""
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    category_label: str = ""
    value_label: str = ""

    @model_validator(mode="after")
    def _series_match_the_categories(self) -> "Chart":
        wrong_series = [one.label for one in self.series if len(one.points) != len(self.categories)]
        if wrong_series:
            raise ValueError(
                f"chart series {wrong_series} do not carry one point for each of the "
                f"{len(self.categories)} categories. Give each series one point for each "
                "category."
            )
        return self

    @model_validator(mode="after")
    def _pie_is_one_series_above_zero(self) -> "Chart":
        if self.kind == "pie":
            name = repr(self.title) if self.title else f"of categories {self.categories}"
            if len(self.series) != 1:
                raise ValueError(
                    f"pie chart {name} has {len(self.series)} series. A pie draws one "
                    "series. Give it one series."
                )
            points = zip(self.categories, self.series[0].points, strict=True)
            categories_at_or_below_zero = [category for category, point in points if point <= 0]
            if categories_at_or_below_zero:
                raise ValueError(
                    f"pie chart {name} has a point of zero or less for the categories "
                    f"{categories_at_or_below_zero}. A pie draws each point as a share of "
                    "the whole. Give each point a number more than zero."
                )
        return self

    def to_markdown(self) -> str:
        # The same table that the dashboard gives a reader who gets no picture.
        title = " — ".join(part for part in [self.title, self.value_label] if part)
        header = [self.category_label or "category", *(one.label for one in self.series)]
        points = zip(self.categories, *(one.points for one in self.series), strict=True)
        rows = [[category, *map(_number, values)] for category, *values in points]
        return _join([_heading(title), _table(header, rows, ["start"] * len(header))])


class ImageGallery(PageBlock):
    block: Literal["image_gallery"] = "image_gallery"
    title: str = ""
    images: list[Image] = Field(default_factory=list)

    def __init__(self, images=(), **data):
        super().__init__(images=images, **data)

    def to_markdown(self) -> str:
        return _titled(
            self.title,
            _bullets(image.to_markdown() for image in self.images),
        )


class Metric(Schema):
    label: str
    value: Value
    description: str = ""

    def __init__(self, label, value, **data):
        super().__init__(label=label, value=value, **data)


class Metrics(PageBlock):
    block: Literal["metrics"] = "metrics"
    title: str = ""
    metrics: list[Metric] = Field(default_factory=list)

    def __init__(self, metrics=(), **data):
        super().__init__(metrics=metrics, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for metric in self.metrics:
            metric.value.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        metrics = ((metric.label, metric.value.to_markdown()) for metric in self.metrics)
        return _titled(self.title, _bullets(f"**{label}**: {value}" for label, value in metrics))


class Fact(Schema):
    label: str
    value: Value

    def __init__(self, label, value, **data):
        super().__init__(label=label, value=value, **data)


class Facts(PageBlock):
    """The label-and-value list."""

    block: Literal["facts"] = "facts"
    title: str = ""
    facts: list[Fact] = Field(default_factory=list)

    def __init__(self, facts=(), **data):
        super().__init__(facts=facts, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for fact in self.facts:
            fact.value.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        facts = ((fact.label, fact.value.to_markdown()) for fact in self.facts)
        return _titled(self.title, _bullets(f"**{label}**: {value}" for label, value in facts))


class TableColumn(Schema):
    label: str
    align: Literal["start", "end"] = "start"

    def __init__(self, label, **data):
        super().__init__(label=label, **data)


class TableRow(Schema):
    cells: list[Value] = Field(default_factory=list)
    detail: str = ""
    # The identity that the shell sends when the operator selects this row.
    key: str = ""

    def __init__(self, cells=(), **data):
        super().__init__(cells=cells, **data)


class Table(PageBlock):
    """Rows of values under named columns. Every row carries one cell for each
    column. With no rows, the shell shows ``empty``. ``select`` names the
    argument that the selected keys fill. ``actions`` run on those keys."""

    block: Literal["table"] = "table"
    title: str = ""
    columns: list[TableColumn] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)
    empty: EmptyState | None = None
    select: str = ""
    actions: list[Action] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rows_match_the_columns(self) -> "Table":
        wrong_counts = [len(row.cells) for row in self.rows if len(row.cells) != len(self.columns)]
        if wrong_counts:
            raise ValueError(
                f"table {self.title!r} has rows of {sorted(set(wrong_counts))} cells under "
                f"{len(self.columns)} columns. Give each row one cell for each column."
            )
        return self

    @model_validator(mode="after")
    def _select_and_actions_go_together(self) -> "Table":
        if self.actions and not self.select:
            raise ValueError(
                f"table {self.title!r} has actions and no select. Name the argument "
                "that the selected keys fill."
            )
        if self.select and not self.actions:
            raise ValueError(
                f"table {self.title!r} names select and has no actions. Give it "
                "actions, or remove select."
            )
        if not self.select:
            return self
        if not all(row.key for row in self.rows):
            raise ValueError(
                f"table {self.title!r} can select rows. One of its rows has no key. Give each "
                "row a key."
            )
        keys = [row.key for row in self.rows]
        repeated_keys = sorted({key for key in keys if keys.count(key) > 1})
        if repeated_keys:
            raise ValueError(
                f"table {self.title!r} has two rows keyed {repeated_keys}. The shell "
                "cannot identify the row that the operator picked. Give each row its own key."
            )
        for action in self.actions:
            if action.fields:
                raise ValueError(
                    f"table {self.title!r} action {action.label!r} collects fields. "
                    "A table action sends only the selected keys. Remove its fields."
                )
            if self.select in action.arguments:
                raise ValueError(
                    f"table {self.title!r} select {self.select!r} is already an "
                    f"argument on action {action.label!r}. Send each value once."
                )
        return self

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for action in self.actions:
            action.check_placement(followed=followed, regions=regions, region=region)
        for row in self.rows:
            for cell in row.cells:
                cell.check_placement(followed=followed, regions=regions, region=region)
        if self.empty:
            self.empty.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        # An action has a text form only when it holds the address of its page. A table
        # that selects rows has one only then.
        actions = _controls(self.actions)
        body = ""
        if self.rows:
            cells = [[cell.to_markdown() for cell in row.cells] for row in self.rows]
            body = _table(
                [column.label for column in self.columns],
                cells,
                [column.align for column in self.columns],
            )
        elif self.empty:
            body = self.empty.to_markdown()
        return _titled(self.title, _join([actions, body]))


class List(PageBlock):
    block: Literal["list"] = "list"
    title: str = ""
    items: list[Value] = Field(default_factory=list)

    def __init__(self, items=(), **data):
        super().__init__(items=items, **data)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        for item in self.items:
            item.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        return _titled(
            self.title,
            _bullets(item.to_markdown() for item in self.items),
        )


class Stack(BlockParent):
    """Blocks down the page. A stack that holds no block is false."""

    block: Literal["stack"] = "stack"
    gap: Literal["small", "medium", "large"] = "medium"

    def __init__(self, blocks=(), **data):
        super().__init__(blocks=blocks, **data)

    def __bool__(self) -> bool:
        return bool(self.blocks)

    @classmethod
    def from_context(cls, context: "str | list[Block] | list[dict]") -> "Stack":
        """The blocks of a review's context, as the app gives them or as the ask stores
        them. A context of text holds none. Raises ``ValidationError`` for a part that is
        not a block."""
        if isinstance(context, str):
            return cls()
        return cls(context)


class Columns(BlockParent):
    """Blocks across the page. ``even`` shares the width. ``sidebar`` keeps the last
    column a rail. The columns stack on a narrow screen.

    ``split`` fills the page with two panes that scroll on their own. The first holds a
    list. The second holds what the list opened."""

    block: Literal["columns"] = "columns"
    layout: Literal["even", "sidebar", "split"] = "even"

    def __init__(self, blocks=(), **data):
        super().__init__(blocks=blocks, **data)

    @model_validator(mode="after")
    def _split_is_two_panes(self) -> "Columns":
        if self.layout == "split" and len(self.blocks) != 2:
            raise ValueError(
                f"Columns layout='split' takes two blocks, not {len(self.blocks)}. Give it a "
                "list and what the list opened."
            )
        return self


class Card(BlockParent):
    """A titled panel. ``link`` is its destination. The shell makes the whole
    panel the control when ``controls`` is empty. ``drag`` is what a drop
    action receives. An empty ``drag`` means the card does not move."""

    block: Literal["card"] = "card"
    title: str = ""
    description: str = ""
    controls: list[Action | Link] = Field(default_factory=list)
    link: Link | None = None
    drag: dict = Field(default_factory=dict)

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        super().check_placement(followed=followed, regions=regions, region=region)
        for control in self.controls:
            control.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        return _join(
            [
                _heading(_linked(self.title, self.link)),
                self.description,
                super().to_markdown(),
                _controls(self.controls),
            ]
        )


class Cards(PageBlock):
    """One card for each of a set of things. ``wrap`` lets the shell put as many
    cards in a row as the screen holds. ``stack`` is one column. ``tiles`` is a
    wrap of squares. An image fills each card. The shell crops the image to the
    square.

    ``drop`` is the action that a card submits when a person drags it onto this
    list."""

    block: Literal["cards"] = "cards"
    title: str = ""
    cards: list[Card] = Field(default_factory=list)
    empty: EmptyState | None = None
    layout: Literal["wrap", "stack", "tiles"] = "wrap"
    drop: Action | None = None

    @model_validator(mode="after")
    def _drop_is_immediate(self) -> "Cards":
        if self.drop and (self.drop.fields or self.drop.confirm):
            raise ValueError(
                "Cards.drop collects fields or asks to confirm. The drop is the submit. "
                "Remove its fields and its confirm."
            )
        return self

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.drop:
            self.drop.check_placement(followed=followed, regions=regions, region=region)
        for card in self.cards:
            card.check_placement(followed=followed, regions=regions, region=region)
        if self.empty:
            self.empty.check_placement(followed=followed, regions=regions, region=region)

    def to_markdown(self) -> str:
        # The drop is an action. It has a text form only when it holds the address of its page.
        drop = ""
        if self.drop:
            drop = self.drop.to_markdown()
        body = ""
        if self.cards:
            body = _join(card.to_markdown() for card in self.cards)
        elif self.empty:
            body = self.empty.to_markdown()
        return _titled(self.title, _join([drop, body]))


class Section(BlockParent):
    """A titled part of a page. A ``name`` makes it a region that the shell can
    replace on its own. ``follows`` makes the shell replace it."""

    block: Literal["section"] = "section"
    title: str = ""
    name: str = ""
    controls: list[Action | Link] = Field(default_factory=list)
    follows: Watched = None

    def check_placement(self, *, followed: bool, regions: set[str], region: str = "") -> None:
        if self.name in regions:
            raise ValueError(
                f"this page has two regions named {self.name!r}. The shell replaces a region "
                "by name. It cannot select one of the two. Give each region its own name."
            )
        if self.name:
            regions.add(self.name)
        nearest_region = self.name or region
        for control in self.controls:
            control.check_placement(
                followed=followed or bool(self.follows),
                regions=regions,
                region=nearest_region,
            )
        super().check_placement(
            followed=followed or bool(self.follows),
            regions=regions,
            region=nearest_region,
        )

    @model_validator(mode="after")
    def _named_when_followed(self) -> "Section":
        if self.follows and not self.name:
            raise ValueError(
                "a Section that follows a subject needs a name. The shell replaces it by "
                "name. Give it a name."
            )
        return self

    def to_markdown(self) -> str:
        heading = ""
        if self.title:
            heading = f"## {self.title}"
        return _join(
            [
                heading,
                _controls(self.controls),
                super().to_markdown(),
            ]
        )


Block = Annotated[
    Text
    | Markdown
    | Quote
    | Section
    | Card
    | Cards
    | Callout
    | Divider
    | EmptyState
    | Link
    | GateControls
    | Timeline
    | Progress
    | Image
    | Files
    | Chart
    | ImageGallery
    | Metrics
    | Facts
    | Table
    | List
    | Stack
    | Columns
    | Action
    | Form,
    Field(discriminator="block"),
]

Card.model_rebuild()
Cards.model_rebuild()
Section.model_rebuild()
Stack.model_rebuild()
Columns.model_rebuild()
BlockParent.model_rebuild()
