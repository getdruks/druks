import base64
import binascii

from druks.notifications.exceptions import MalformedButtonError


def encode_button(token: str, choice_id: str) -> str:
    # base64url keeps the id opaque and URL-safe for any token/choice alphabet,
    # and "." can never appear inside an encoded part, so the split point is
    # unambiguous and distinct pairs can't collide.
    return f"{_encode_part(token)}.{_encode_part(choice_id)}"


def decode_button(action_id: str) -> tuple[str, str]:
    # The action_id arrives in an untrusted webhook payload; a non-string is
    # malformed input, not a crash.
    if not isinstance(action_id, str):
        raise MalformedButtonError()
    token_part, separator, choice_part = action_id.partition(".")
    if not separator:
        raise MalformedButtonError()
    try:
        return _decode_part(token_part), _decode_part(choice_part)
    except (binascii.Error, UnicodeDecodeError) as error:
        raise MalformedButtonError() from error


def encode_action(token: str, position: int) -> str:
    """The id of the button that runs the action at ``position`` in the blocks of the
    notification of ``token``. It carries no operation and no arguments."""
    return f"{encode_button(token, str(position))}.action"


def decode_action(action_id: str) -> tuple[str, int]:
    """The token and the position that the id of an action's button names. Raises
    MalformedButtonError for a position that is not a number."""
    token, position = decode_button(action_id.rpartition(".")[0])
    if position.isascii() and position.isdigit():
        return token, int(position)
    raise MalformedButtonError()


def is_action_button(action_id: str) -> bool:
    """Whether the id has the three parts of an action's button. The id of a choice has
    two, so a choice of a gate never reads as an action."""
    return isinstance(action_id, str) and action_id.count(".") == 2


def _encode_part(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_part(part: str) -> str:
    padded = part + "=" * (-len(part) % 4)
    # validate=True: without it b64decode silently discards foreign characters
    # instead of rejecting a tampered id.
    return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
