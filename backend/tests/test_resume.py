import pytest
from druks.accounts.models import Account
from druks.api.runs import resume_run
from druks.api.schemas import ResumeRequest
from druks.durable import Run
from druks.testing import seed_dbos_status
from druks_field_notes.workflows import Summarize
from fastapi import HTTPException

# The ask that review() parks with. _park adds the time of the park, which the answer
# service echoes.
_ASK = {
    "presentation": "in_app",
    "controls": [
        {"id": "approve", "note_required": False},
        {"id": "request_changes", "note_required": True},
    ],
    "questions": [{"id": "q1", "prompt": "which?", "options": [{"id": "a", "label": "A"}]}],
}


async def _park(druks_db, *, controls: list[dict] | None = None) -> None:
    ask = dict(_ASK)
    if controls:
        ask["controls"] = controls
    druks_db.add(
        Run(
            account_id=(await Account.get_or_create(druks_db, "op@example.com")).id,
            id="r1",
            kind=Summarize.kind,
            input_gate="review_plan",
            input_request=ask,
            input_requested_at=Run.utc_now(),
        )
    )
    await druks_db.flush()
    await seed_dbos_status(druks_db, "r1", "parked")


async def test_resume_sends_the_offered_control_as_the_action(druks_db, monkeypatch):
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    await _park(druks_db)

    await resume_run(druks_db, "r1", ResumeRequest(control="approve", answers={"q1": "a"}))
    assert captured == {
        "id": "r1",
        "action": "approve",
        "answers": {"q1": "a"},
        "note": "",
        "answered_by_id": "",
    }


async def test_resume_passes_free_text_answers_and_note_as_content(druks_db, monkeypatch):
    # An answer in the operator's own words and their note stay unchanged in the reply.
    # They are content for the next pass of the plan. Druks takes an answer only to a
    # question of the ask, and never maps it to a control.
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    await _park(druks_db)

    await resume_run(
        druks_db,
        "r1",
        ResumeRequest(
            control="approve",
            answers={"q1": "neither — use the existing cache"},
            note="keep the migration out of this PR",
        ),
    )
    assert captured == {
        "id": "r1",
        "action": "approve",
        "answers": {"q1": "neither — use the existing cache"},
        "note": "keep the migration out of this PR",
        "answered_by_id": "",
    }


async def test_resume_rejects_an_answer_to_a_question_that_was_not_asked(druks_db, monkeypatch):
    # Free text is only an answer. It must answer a question that the ask posed.
    async def fake_resume(self, **fields):
        raise AssertionError("must not resume on an invalid answer")

    monkeypatch.setattr(Run, "resume", fake_resume)
    await _park(druks_db)

    with pytest.raises(HTTPException) as refused:
        await resume_run(
            druks_db, "r1", ResumeRequest(control="approve", answers={"q9": "whatever"})
        )
    assert refused.value.status_code == 422


async def test_resume_accepts_empty_request_changes_when_it_needs_no_note(druks_db, monkeypatch):
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    await _park(
        druks_db,
        controls=[
            {"id": "approve", "note_required": False},
            {"id": "request_changes", "note_required": False},
        ],
    )

    await resume_run(druks_db, "r1", ResumeRequest(control="request_changes", answers={}, note=""))

    assert captured == {
        "id": "r1",
        "action": "request_changes",
        "answers": {},
        "note": "",
        "answered_by_id": "",
    }


async def test_resume_request_changes_with_a_note_passes(druks_db, monkeypatch):
    captured: dict = {}

    async def fake_resume(self, **fields):
        captured.update(id=self.id, **fields)

    monkeypatch.setattr(Run, "resume", fake_resume)
    await _park(druks_db)

    await resume_run(
        druks_db, "r1", ResumeRequest(control="request_changes", note="split the migration")
    )
    assert captured == {
        "id": "r1",
        "action": "request_changes",
        "answers": {},
        "note": "split the migration",
        "answered_by_id": "",
    }


async def test_resume_rejects_a_blank_answer(druks_db, monkeypatch):
    async def fake_resume(self, **fields):
        raise AssertionError("must not resume on a blank answer")

    monkeypatch.setattr(Run, "resume", fake_resume)
    await _park(druks_db)

    with pytest.raises(HTTPException) as refused:
        await resume_run(druks_db, "r1", ResumeRequest(control="approve", answers={"q1": "   "}))
    assert refused.value.status_code == 422
