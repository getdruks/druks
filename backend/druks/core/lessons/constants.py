# One line of MEMORY.md for each accepted lesson. Claude Code loads 200 lines or 25 KB of the
# index, so the cap leaves room for the rest of the file.
LESSON_INDEX_MAX_LINES = 100
# The directory of the lessons in a box, relative to its home. The repo's clone sits beside it
# at work/repo, so the clear names this directory and no other.
VAULT_DIR = "work/memory"
VAULT_INDEX = "MEMORY.md"
LESSON_TITLE_MAX_CHARS = 120
# The note that opens the prompt of a call when the installation has lessons.
VAULT_PROMPT = """People at this installation taught the lessons below from earlier corrections. \
Before you start, read the lesson files in {path} that apply to your task. Do not change them.

{index}"""
