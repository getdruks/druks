import base64
import binascii

from druks.notifications.exceptions import MalformedButtonError


def encode_button(token: str, choice_id: str) -> str:
    # base64url keeps the id opaque and URL-safe for any token/choice alphabet. "."
    # can never appear inside an encoded part. The split point is then unambiguous.
    # Distinct pairs cannot collide.
    return f"{_encode_part(token)}.{_encode_part(choice_id)}"


def decode_button(action_id: str) -> tuple[str, str]:
    # The action_id arrives in an untrusted webhook payload. A non-string is
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


def encode_action(token: str, *position: int) -> str:
    """The id of the button that runs the action at ``position`` in the blocks of the
    notification of ``token``. The position is the index of a block, or the indexes of a
    table, its row, and the cell. The id carries no operation and no arguments."""
    indexes = ",".join(str(index) for index in position)
    return f"{encode_button(token, indexes)}.action"


def decode_action(action_id: str) -> tuple[str, list[int]]:
    """The token and the position that the id of an action's button names. Raises
    MalformedButtonError for a position that is not a list of numbers."""
    token, indexes = decode_button(action_id.rpartition(".")[0])
    position = indexes.split(",")
    if all(index.isascii() and index.isdigit() for index in position):
        return token, [int(index) for index in position]
    raise MalformedButtonError()


def is_action_button(action_id: str) -> bool:
    """Whether the id has the three parts of an action's button. A choice of a gate never
    reads as an action, because the id of a choice has two."""
    return isinstance(action_id, str) and action_id.count(".") == 2


def _encode_part(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_part(part: str) -> str:
    padded = part + "=" * (-len(part) % 4)
    # Without validate=True, b64decode drops foreign characters and accepts a tampered id.
    return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
