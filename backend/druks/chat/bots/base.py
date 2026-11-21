from dataclasses import dataclass, field

from druks.apps.exceptions import AppBotError
from druks.chat.enums import BotAccess


@dataclass(frozen=True)
class Bot:
    """An app's ``<app>.bot`` agent; access selects who writes and owns the conversation.
    Each tool names the ``operation_id`` of an app route tagged ``bot``."""

    prompt: str
    access: BotAccess = BotAccess.PUBLIC
    user_tools: tuple[str, ...] = ()
    admin_tools: tuple[str, ...] = ()
    name: str | None = None
    description: str = ""
    # The longest a turn may run, in seconds. None inherits the default.
    timeout: int | None = None
    id: str = field(default="", init=False, compare=False)
    app: str = field(default="", init=False, compare=False)

    def __set_name__(self, owner: type, attribute: str) -> None:
        if attribute == "bot":
            object.__setattr__(self, "id", f"{owner.name}.bot")
            object.__setattr__(self, "app", owner.name)
            return
        raise AppBotError(
            f"app {owner.name!r} declares a Bot as {attribute!r}. An app has one Bot, "
            "and Druks reads it from the bot attribute. Name the attribute bot."
        )
