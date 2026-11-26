import io
import json

from druks.core.lessons.datastructures import Vault
from druks.sandbox.datastructures import HomeFile


def _transcript(*lines: str) -> io.StringIO:
    # The shape of stdout.jsonl: one event on each line, each line ending in a newline.
    return io.StringIO("\n".join(lines) + "\n")


def _read(path: str) -> str:
    event = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": path}}]
        },
    }
    return json.dumps(event)


def test_the_delivered_lessons_are_the_files_of_the_vault_but_its_index():
    home = (
        HomeFile(".claude.json", "{}"),
        HomeFile("work/memory/MEMORY.md", "- A — abc.md", clears="work/memory"),
        HomeFile("work/memory/abc.md", "A."),
        HomeFile("work/memory/def.md", "D."),
    )

    assert Vault.list_delivered(home) == ["abc", "def"]


def test_a_lesson_is_opened_when_a_tool_call_names_its_file():
    transcript = _transcript(
        _read("/home/exedev/work/memory/MEMORY.md"), _read("/home/exedev/work/memory/abc.md")
    )

    assert Vault.list_opened(["abc", "def"], transcript) == ["abc"]


def test_a_shell_command_that_reads_a_lesson_opens_it():
    event = {
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "sed -n 1,80p ~/work/memory/def.md"},
    }

    assert Vault.list_opened(["abc", "def"], _transcript(json.dumps(event))) == ["def"]


def test_a_path_that_only_the_message_text_names_opens_nothing():
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "I read work/memory/abc.md"}]},
    }

    assert Vault.list_opened(["abc"], _transcript(json.dumps(event), "not json")) == []
