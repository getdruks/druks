import re

from druks.core.lessons.constants import VAULT_DIR, VAULT_INDEX
from druks.sandbox.datastructures import HomeFile

# What a tool call reads: the path of Claude's Read tool, and the shell command of Codex. A
# command can be a string or a list of strings.
_READ = re.compile(r'"(?:file_path|path|command)"\s*:\s*(\[[^\]]*\]|"(?:[^"\\]|\\.)*")')


def get_delivered_lessons(home: tuple[HomeFile, ...]) -> list[str]:
    """The ids of the lessons that a call writes into its box."""
    return [
        file.path.removeprefix(f"{VAULT_DIR}/").removesuffix(".md")
        for file in home
        if file.path.startswith(f"{VAULT_DIR}/") and not file.path.endswith(VAULT_INDEX)
    ]


def get_opened_lessons(delivered: list[str], transcript: str) -> list[str]:
    """The delivered lessons that a tool call of the transcript names by path. The prompt
    names each file too, but it is not in the transcript, and text that an agent writes is
    not a tool call."""
    reads = _READ.findall(transcript)
    return [
        lesson for lesson in delivered if any(f"{VAULT_DIR}/{lesson}.md" in text for text in reads)
    ]
