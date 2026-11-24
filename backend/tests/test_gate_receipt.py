from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from dbos import error as dbos_error
from druks import ui
from druks.accounts.models import Account
from druks.contrib.software_factory.workflows import PullRequestReview
from druks.durable.exceptions import GateTimeout, WorkflowError
from druks.durable.models import Run
from druks.durable.reads import read_ask
from druks.events.models import Event
from druks.exceptions import ObjectNotFound, PermissionDenied
from druks.teams.models import Team
from druks.testing import seed_run
from druks.ui.blocks import iter_parts
from druks.ui.exceptions import NoTextFormError
from druks.workflows import OperatorReply, YesNo, current_workflow
from druks_field_notes.models import Note
from druks_field_notes.workflows import Summarize
from pydantic import ValidationError
from sqlalchemy import select

_ASK = {"presentation": "in_app", "controls": ["approve", "request_changes"], "questions": []}
_STORED_ASK = {
    **_ASK,
    "controls": [
        {"id": "approve", "note_required": False},
        {"id": "request_changes", "note_required": True},
    ],
    "reply_fields": ["action", "answers", "note"],
}
_NOTE = ui.Follows(subject_type="note", subject_id="7")


@pytest.fixture(params=["gate", "review"])
async def request_reply(request, druks_db, direct_steps):
    note = await Note.create(body="A request to review")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    token = current_workflow.set(workflow)
    try:
        call = (
            OperatorReply.wait(input_request=_ASK) if request.param == "gate" else workflow.review()
        )
        yield run.id, call
    finally:
        current_workflow.reset(token)


async def test_valid_reply_records_the_request_round_before_current_fields_clear(
    druks_db, request_reply, monkeypatch
):
    run_id, call = request_reply
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value={"action": "approve"}))

    reply = await call

    assert reply == OperatorReply(action="approve")
    druks_db.expunge_all()
    run = await druks_db.get(Run, run_id)
    assert run.answer_parked_at == run.input_requested_at
    assert not run.input_gate
    assert not run.input_request
    events = list(await druks_db.scalars(select(Event).order_by(Event.id)))
    assert [event.type for event in events] == ["workflow.parked", "workflow.running"]
    request, receipt = events
    for event in events:
        assert event.payload["run"] == run_id
        assert event.payload["gate"] == "review"
        assert event.payload["input_requested_at"] == run.input_requested_at.isoformat()
    assert request.payload["input_request"] == _STORED_ASK
    assert receipt.payload["result"] == {
        "action": "approve",
        "answers": {},
        "note": "",
        "answered_by": "",
    }


async def test_yes_no_parks_with_its_own_controls(druks_db, direct_steps, monkeypatch):
    note = await Note.create(body="A quote")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    token = current_workflow.set(workflow)
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value={"action": "yes", "note": "go"}))
    try:
        reply = await YesNo.wait(input_request={"presentation": "in_app", "label": "Keep it?"})
    finally:
        current_workflow.reset(token)

    assert reply == YesNo(action="yes", note="go")
    druks_db.expunge_all()
    parked = await druks_db.scalar(select(Event).where(Event.type == "workflow.parked"))
    assert parked.payload["input_request"] == {
        "presentation": "in_app",
        "label": "Keep it?",
        "questions": [],
        "controls": [
            {"id": "yes", "note_required": False},
            {"id": "no", "note_required": False},
        ],
        "reply_fields": ["action", "note"],
    }


async def test_an_in_app_ask_checks_its_approver(druks_db, direct_steps, monkeypatch):
    paulo = await Account.get_or_create(druks_db, "paulo@example.com")
    await Account.get_or_create(druks_db, "outsider@example.com")
    await Team.create(druks_db, name="Notes", member_ids=[paulo.id], apps=[Summarize.app])
    note = await Note.create(body="A quote")
    run = await seed_run(druks_db, kind=Summarize.kind, subject=note)
    workflow = Summarize()
    workflow._workflow_id = run.id
    workflow._subject = note.identity
    token = current_workflow.set(workflow)
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value={"action": "yes"}))
    ask = {"presentation": "in_app", "label": "Keep it?"}
    try:
        with pytest.raises(ObjectNotFound):
            await YesNo.wait(input_request={**ask, "approver": "nobody@example.com"})
        with pytest.raises(PermissionDenied):
            await YesNo.wait(input_request={**ask, "approver": "outsider@example.com"})
        await YesNo.wait(input_request={**ask, "approver": "Paulo@Example.com"})
    finally:
        current_workflow.reset(token)

    druks_db.expunge_all()
    parked = await druks_db.scalar(select(Event).where(Event.type == "workflow.parked"))
    # The ask keeps the name as the account spells it.
    assert parked.payload["input_request"]["approver"] == "paulo@example.com"


async def test_an_app_cannot_name_who_answered_a_gate(druks_db):
    note = await Note.create(body="A quote")

    with pytest.raises(WorkflowError, match="sets answered_by, and only Druks"):
        await OperatorReply.answer(
            note, workflow=Summarize, action="approve", answered_by="paulo@example.com"
        )


@pytest.mark.parametrize(
    "context, stored, note_required",
    [
        ("Keep the scope small.", "Keep the scope small.", False),
        ("   ", "   ", True),
        (
            [ui.Facts([ui.Fact("Work", ui.SubjectStatus(_NOTE))], title="The note")],
            [
                {
                    "block": "facts",
                    "title": "The note",
                    "facts": [
                        {
                            "label": "Work",
                            "value": {
                                "value": "subject_status",
                                "subject": {"subjectType": "note", "subjectId": "7"},
                                "working": "working",
                            },
                        }
                    ],
                }
            ],
            False,
        ),
        ([], None, True),
    ],
    ids=["text", "blank text", "blocks", "no blocks"],
)
async def test_the_ask_keeps_its_context_and_a_guiding_one_spares_request_changes_a_note(
    druks_db, summarize, monkeypatch, context, stored, note_required
):
    asks = []

    async def answer(*args, **kwargs):
        parked = await druks_db.get(Run, summarize.workflow_id, populate_existing=True)
        asks.append(parked.input_request)
        return {"action": "approve"}

    monkeypatch.setattr(DBOS, "recv_async", answer)

    await summarize.review(context=context)

    druks_db.expunge_all()
    parked = await druks_db.scalar(select(Event).where(Event.type == "workflow.parked"))
    for ask in [*asks, parked.payload["input_request"]]:
        assert ask.get("context") == stored
        assert ask["controls"] == [
            {"id": "approve", "note_required": False},
            {"id": "request_changes", "note_required": note_required},
        ]


async def test_a_block_context_reads_where_the_work_stands_each_time(
    druks_db, summarize, monkeypatch
):
    note = await summarize.subject
    other = await Note.create(body="Another plan")
    asks = []
    stored = []

    async def answer(*args, **kwargs):
        parked = await druks_db.get(Run, summarize.workflow_id, populate_existing=True)
        asks.append(await read_ask(druks_db, parked))
        # A run of another app is about the other note. That run is not this app's work.
        await seed_run(druks_db, kind=PullRequestReview.kind, subject=other, state="running")
        asks.append(await read_ask(druks_db, parked))
        await seed_run(druks_db, kind=Summarize.kind, subject=other, state="failed")
        asks.append(await read_ask(druks_db, parked))
        stored.append(parked.input_request)
        return {"action": "approve"}

    monkeypatch.setattr(DBOS, "recv_async", answer)

    await summarize.review(
        context=[
            ui.Facts(
                [ui.Fact("This", ui.SubjectStatus(note)), ui.Fact("That", ui.SubjectStatus(other))]
            )
        ]
    )

    statuses = [[fact["value"]["status"] for fact in ask["context"][0]["facts"]] for ask in asks]
    assert [(this["state"], this["gate"]) for this, _ in statuses] == [("parked", "review")] * 3
    assert [that["state"] for _, that in statuses] == [None, None, "failed"]
    assert "status" not in stored[0]["context"][0]["facts"][0]["value"]


async def test_review_refuses_a_part_with_no_text_form_before_it_parks(druks_db, summarize):
    with pytest.raises(NoTextFormError) as raised:
        await summarize.review(context=[ui.Section(blocks=[ui.GateControls(_NOTE)])])

    assert raised.value.block == "GateControls"
    assert not list(await druks_db.scalars(select(Event)))


async def test_review_refuses_a_secret_field_before_it_parks(druks_db, summarize):
    form = ui.Form(
        fields=[ui.SecretField(name="token", label="Token")],
        action=ui.Action(label="Save", operation="write_note"),
    )

    with pytest.raises(ValueError) as refused:
        await summarize.review(context=[form])

    assert str(refused.value) == (
        "review takes no SecretField in its context. Field 'token' is one. A chat message "
        "that shows the review holds no secret input and no file input. Remove the field from "
        "the review. Ask for it on a page of the app."
    )
    assert not list(await druks_db.scalars(select(Event)))


async def test_review_refuses_an_action_that_names_no_write_of_its_app_before_it_parks(
    druks_db, summarize
):
    with pytest.raises(ValueError, match="names operation 'shred_note', which none of its routes"):
        await summarize.review(context=[ui.Action(label="Shred", operation="shred_note")])

    assert not list(await druks_db.scalars(select(Event)))


async def test_review_stores_an_action_inside_a_block_with_the_page_of_its_run(
    druks_db, summarize, monkeypatch
):
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value={"action": "approve"}))
    go = ui.Action(label="Go", operation="write_note")
    table = ui.Table(columns=[ui.TableColumn("Do")], rows=[ui.TableRow([ui.ControlsValue([go])])])

    await summarize.review(context=[table])

    parked = await druks_db.scalar(select(Event).where(Event.type == "workflow.parked"))
    stored = ui.Stack.from_context(parked.payload["input_request"]["context"])
    (action,) = iter_parts(stored.blocks, ui.Action)
    assert action.url == f"/runs/{summarize.workflow_id}"
    # The app's own action stays as the app wrote it.
    assert not go.url


async def test_review_refuses_a_part_that_is_not_a_block_before_it_parks(druks_db, summarize):
    with pytest.raises(ValidationError):
        await summarize.review(context=[ui.Fact("Owner", "Ada")])

    assert not list(await druks_db.scalars(select(Event)))


@pytest.mark.parametrize(
    "context, refusal",
    [([ui.Stack([ui.GateControls(_NOTE)])], NoTextFormError), (None, ValidationError)],
    ids=["part with no text form", "nothing"],
)
async def test_a_gate_refuses_an_in_app_context_that_review_refuses(
    druks_db, summarize, context, refusal
):
    with pytest.raises(refusal):
        await OperatorReply.wait(input_request={**_ASK, "context": context})

    assert not list(await druks_db.scalars(select(Event)))


@pytest.mark.parametrize("payload", [{"action": "merge"}, {}, None])
async def test_invalid_reply_or_timeout_records_no_receipt(
    druks_db, request_reply, monkeypatch, payload
):
    run_id, call = request_reply
    monkeypatch.setattr(DBOS, "recv_async", AsyncMock(return_value=payload))

    with pytest.raises(GateTimeout if payload is None else ValidationError):
        await call

    druks_db.expunge_all()
    run = await druks_db.get(Run, run_id)
    assert not run.answer_parked_at
    assert run.input_gate == "review"
    assert run.input_request == _STORED_ASK
    assert run.input_requested_at
    events = list(await druks_db.scalars(select(Event)))
    assert [event.type for event in events] == ["workflow.parked"]


async def test_cancel_records_no_receipt(druks_db, request_reply, monkeypatch):
    run_id, call = request_reply
    monkeypatch.setattr(
        DBOS, "recv_async", AsyncMock(side_effect=dbos_error.DBOSWorkflowCancelledError(run_id))
    )

    with pytest.raises(dbos_error.DBOSWorkflowCancelledError):
        await call

    druks_db.expunge_all()
    run = await druks_db.get(Run, run_id)
    assert not run.answer_parked_at
    events = list(await druks_db.scalars(select(Event)))
    assert [event.type for event in events] == ["workflow.parked"]
