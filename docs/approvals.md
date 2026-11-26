---
title: "Approvals"
description: "How a run waits for your yes, and where you give it."
icon: "circle-check"
---

An app decides where a person must make the call. At that point the run stops
and asks you. Nothing changes until you answer.

## What you see

- **The question,** in one sentence. For example: "I'd like to upgrade 3
  packages in acme/api."
- **Why,** with the facts: what it fixes and whether the tests pass.
- **The plan,** when you want the details: the diff or the list of changes.
- **The cost so far.**
- **Your choices.** Usually Approve, Change or Stop. Some apps ask a short
  question instead.

## While the run waits

- **No agent runs.** Druks releases the sandboxes of the run while it waits.
- **It survives restarts and deploys.** After your answer, the run continues
  from the same point.
- **It waits up to 14 days.** After 14 days without an answer, the run fails
  with a gate-timeout failure. See [troubleshooting](troubleshooting.md).

## Where you answer

You answer in the same places on Druks Cloud and on a self-hosted Druks.

| Where | How |
| --- | --- |
| Dashboard | Open the run. The waiting step shows the question and the buttons. |
| Slack | Druks posts the question with buttons to the Slack channel that you chose, or to your direct message. See [notifications](configuration.md#notifications). A run that you started from Slack gets them in that thread or direct message instead, where only you can click them. The message shows the context, each question with its options, and the start of the document that you review. The recommended option has a mark. When the document is too long for Slack, the message shows its start, and the run's page shows all of it. Below the text, each question has a menu of its options, or a box for your own words when it has no options. Another box takes your note. A click sends your choices and your note with the button. A button that needs a note, such as Request changes, takes no empty note: only you see why, and the message stays open. After a click, the message shows who chose what, with your answers and your note, in place of the inputs and the buttons. When the question ends another way, such as an answer in the dashboard or a cancelled run, Druks replaces the inputs and the buttons with "This question is closed." |
| Chat | Ask what waits for you, and answer in the [chat](chat.md). |
| Claude Code or Codex | Connect the Druks MCP server. Your agent lists what waits, reads the question and answers it. See [connect your agent](connect-your-agent.md). |

Druks accepts one answer for each question, from whichever place answers
first.

## After you answer

- **Approve:** the run continues and makes the change, for example it opens a
  pull request.
- **Change:** you write what to change. The agent tries again and asks you
  again.
- **Stop:** the run ends. Nothing changes.

Each question, answer and cost stays in the run history, with who answered and
when.

## Keep a correction as a lesson

In Software Factory, the Lessons page lists the notes and requests for changes
that people wrote at a gate, newest first. Nothing waits on this page.

- **Make a lesson:** edit the text of a correction and accept it. The lesson
  shows who accepted it and where it comes from.
- **Add a lesson:** write one without a correction. You can name the run that
  it comes from.
- **Repeats a lesson:** mark a correction that repeats an accepted lesson.
- **Retire:** take a lesson out of use.

The page counts the corrections of the last 30 days, the corrections for each
build, and the marks. Any signed-in account can accept a lesson. The lessons
index holds 100 accepted lessons. When it is full, retire a lesson or merge
two before you accept another.

## For app builders

In code, an approval is a gate. See
[wait for input](writing-an-app.md#wait-for-input) in the app guide, and
[waiting for people and systems](concepts.md#waiting-for-people-and-systems).
