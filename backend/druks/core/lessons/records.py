import json
from collections.abc import Iterator

from druks.core.lessons.constants import VAULT_DIR, VAULT_INDEX
from druks.sandbox.datastructures import HomeFile

# The keys that hold what a tool call reads: Claude's Read tool, and the shell command of Codex.
_READ_KEYS = ("file_path", "path", "command")


def get_delivered_lessons(home: tuple[HomeFile, ...]) -> list[str]:
    """The ids of the lessons that a call writes into its box."""
    return [
        file.path.removeprefix(f"{VAULT_DIR}/").removesuffix(".md")
        for file in home
        if file.path.startswith(f"{VAULT_DIR}/") and not file.path.endswith(VAULT_INDEX)
    ]


def get_opened_lessons(delivered: list[str], transcript: str) -> list[str]:
    """The delivered lessons that a tool call of the transcript names by path. The index
    names each file, so a path that only the prompt carries does not count."""
    reads = [text for line in transcript.splitlines() for text in _list_reads(_load(line))]
    return [
        lesson for lesson in delivered if any(f"{VAULT_DIR}/{lesson}.md" in text for text in reads)
    ]


def _load(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _list_reads(event: object) -> Iterator[str]:
    if isinstance(event, dict):
        for key, value in event.items():
            if key in _READ_KEYS and isinstance(value, str):
                yield value
            elif key in _READ_KEYS and isinstance(value, list):
                yield from (part for part in value if isinstance(part, str))
            else:
                yield from _list_reads(value)
    elif isinstance(event, list):
        for item in event:
            yield from _list_reads(item)
