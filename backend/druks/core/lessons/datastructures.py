import re
from collections.abc import Iterable
from dataclasses import dataclass

from druks.core.lessons import constants
from druks.sandbox.datastructures import HomeCopy, HomeFile

# What a tool call reads: the path of Claude's Read tool, and the shell command of Codex. A
# command can be a string or a list of strings.
_READ = re.compile(r'"(?:file_path|path|command)"\s*:\s*(\[[^\]]*\]|"(?:[^"\\]|\\.)*")')


@dataclass(frozen=True)
class Vault:
    """The lessons as a box holds them: the files that Druks writes into it, and the
    index that the prompt carries. An installation with no lesson has neither."""

    files: tuple[HomeFile, ...] = ()
    index: str = ""

    @staticmethod
    def list_delivered(home: Iterable[HomeFile | HomeCopy]) -> list[str]:
        """The ids of the lessons that a call writes into its box."""
        return [
            file.path.removeprefix(f"{constants.VAULT_DIR}/").removesuffix(".md")
            for file in home
            if file.path.startswith(f"{constants.VAULT_DIR}/")
            and not file.path.endswith(constants.VAULT_INDEX)
        ]

    @staticmethod
    def list_opened(delivered: list[str], lines: Iterable[str]) -> list[str]:
        """The delivered lessons that a tool call of the transcript names by path. ``lines``
        are the lines of the transcript, each one a tool call or a message. The prompt names
        each file too, but it is not in the transcript, and text that an agent writes is not
        a tool call."""
        reads = [text for line in lines for text in _READ.findall(line)]
        return [
            lesson
            for lesson in delivered
            if any(f"{constants.VAULT_DIR}/{lesson}.md" in text for text in reads)
        ]
