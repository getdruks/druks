import contextlib
from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from druks import ui
from druks.chat.enums import ConversationSource
from druks.chat.models import Conversation
from druks.durable.dbos_state import workflow_status
from druks.durable.models import Run
from druks.notifications.models import Notification
from druks.settings import configure_settings, get_settings
from druks.testing import seed_run
from druks.ui.exceptions import NoTextFormError
from druks.workflows import YesNo, _execute_run
from sqlalchemy import update


async def _get_status(druks_db, run_id: str) -> Notification | None:
    druks_db.expunge_all()
    return await Notification.get_status(druks_db, run_id)


async def _draw_status(druks_db, run_id: str) -> ui.Stack:
    """The status of the run as its message shows it."""
    status = await _get_status(druks_db, run_id)
    return await status.draw(await druks_db.get(Run, run_id))


def _work(status: ui.StatusValue) -> ui.Section:
    """A report with a step deep in its blocks, after a step that settled."""
    progress = ui.Progress(
        "Summarize",
        steps=[
            ui.ProgressStep(label="Read", status=ui.StatusValue("read", tone="success")),
            ui.ProgressStep(label="Gist", status=status),
        ],
    )
    return ui.Section(title="Work", blocks=[ui.Card(blocks=[progress])])


@pytest.fixture
def endpoint():
    """The installation answers at https://druks.example, so a status links to its run."""
    settings = get_settings()
    urls = settings.urls.model_copy(update={"endpoint": "https://druks.example"})
    configure_settings(settings.model_copy(update={"urls": urls}))


async def test_the_status_of_a_run_that_no_chat_started_goes_to_its_account(
    druks_db, summarize, endpoint
):
    run = await druks_db.get(Run, summarize.workflow_id)

    await summarize.report([ui.Markdown("Writing the gist.")])

    status = await _get_status(druks_db, run.id)
    assert (status.account_id, status.conversation_id) == (run.account_id, None)
    assert status.deep_link == f"https://druks.example/runs/{run.id}"


async def test_the_status_of_a_run_that_a_chat_started_goes_to_that_chat(druks_db, summarize):
    run = await druks_db.get(Run, summarize.workflow_id)
    conversation = Conversation(
        account_id=run.account_id,
        source=ConversationSource.SLACK,
        user_id="U1",
        thread_id="C9:1700000000.000100",
    )
    druks_db.add(conversation)
    await druks_db.flush()
    run.conversation_id = conversation.id
    await druks_db.flush()

    await summarize.report([ui.Markdown("Writing the gist.")])

    status = await _get_status(druks_db, run.id)
    assert (status.conversation_id, status.account_id) == (conversation.id, None)


async def test_a_report_with_a_part_that_has_no_text_form_fails_before_its_step(
    summarize, monkeypatch
):
    step = AsyncMock()
    monkeypatch.setattr(DBOS, "run_step_async", step)

    with pytest.raises(NoTextFormError):
        await summarize.report([ui.Form(action=ui.Action(label="Save", operation="write_note"))])

    step.assert_not_awaited()


@pytest.mark.parametrize(
    ("wait", "reply", "callout"),
    [
        (
            lambda workflow: workflow.review(),
            {"action": "approve"},
            "Waiting for an answer: Review",
        ),
        (lambda workflow: YesNo.wait(), {"action": "yes"}, "Waiting for an answer."),
    ],
    ids=["review", "gate with no ask"],
)
async def test_a_park_names_what_the_run_waits_for_and_a_resume_shows_the_report_again(
    druks_db, summarize, monkeypatch, wait, reply, callout
):
    report = _work(ui.StatusValue("writing", tone="active"))
    await summarize.report([report])
    parked = {}

    async def answer(*args, **kwargs):
        parked["shown"] = await _draw_status(druks_db, summarize.workflow_id)
        return reply

    monkeypatch.setattr(DBOS, "recv_async", answer)

    await wait(summarize)

    assert parked["shown"] == ui.Stack(
        [_work(ui.StatusValue("waiting", tone="active")), ui.Callout(callout, tone="warning")]
    )
    assert await _draw_status(druks_db, summarize.workflow_id) == ui.Stack([report])


@pytest.mark.parametrize(
    ("failure", "callout", "step"),
    [
        (None, ui.Callout("Finished.", tone="success"), ui.StatusValue("finished", tone="success")),
        (
            RuntimeError("The disk is full."),
            ui.Callout("Failed.", tone="danger"),
            ui.StatusValue("failed", tone="danger"),
        ),
    ],
    ids=["finished", "failed"],
)
async def test_the_end_of_a_run_gives_each_step_in_progress_its_outcome(
    druks_db, summarize, monkeypatch, failure, callout, step
):
    # A failed run that no chat started queues a notice to its account.
    monkeypatch.setattr(DBOS, "enqueue_workflow_async", AsyncMock())
    run = await druks_db.get(Run, summarize.workflow_id)

    async def body():
        await summarize.report([_work(ui.StatusValue("writing", tone="active"))])
        if failure:
            raise failure

    with pytest.raises(RuntimeError) if failure else contextlib.nullcontext():
        await _execute_run(
            run.id,
            kind=run.kind,
            subject=summarize._subject,
            account_id=run.account_id,
            body=body,
        )

    # DBOS records the end after the body returns, so the run still reads as running here.
    assert await _draw_status(druks_db, run.id) == ui.Stack([_work(step), callout])


async def test_a_run_that_retries_another_takes_its_status(druks_db, summarize, endpoint):
    await summarize.report([ui.Markdown("Writing the gist.")])
    first = await _get_status(druks_db, summarize.workflow_id)
    run = await druks_db.get(Run, summarize.workflow_id)
    retry = await seed_run(druks_db, kind=run.kind)
    await druks_db.execute(
        update(workflow_status)
        .where(workflow_status.c.workflow_uuid == retry.id)
        .values(forked_from=summarize.workflow_id)
    )
    summarize._workflow_id = retry.id

    await summarize.report([ui.Markdown("Writing the gist again.")])

    status = await _get_status(druks_db, retry.id)
    assert (status.id, status.run_id, status.body) == (
        first.id,
        retry.id,
        "Writing the gist again.",
    )
    assert status.deep_link == f"https://druks.example/runs/{retry.id}"
