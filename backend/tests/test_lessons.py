import pytest
from druks.accounts.models import Account
from druks.durable.models import Run
from druks.events.models import Event
from druks.exceptions import ValidationError
from druks.lessons import models
from druks.lessons.exceptions import LessonIndexFull
from druks.lessons.models import Lesson


async def test_an_accepted_lesson_records_its_source_and_who_accepted_it(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    druks_db.add(Run(id="run-1", kind="build", account_id=account.id))
    correction = Event(type="workflow.running", payload={"run": "run-1"})
    druks_db.add(correction)
    await druks_db.flush()

    lesson = await Lesson.accept(
        druks_db,
        text="  Say why, not what.  ",
        account=account,
        source_event_id=correction.id,
        source_run_id="run-1",
    )

    assert lesson.text == "Say why, not what."
    assert lesson.accepted_by_id == account.id
    assert (lesson.source_event_id, lesson.source_run_id) == (correction.id, "run-1")
    assert await Lesson.list_accepted(druks_db) == [lesson]


async def test_a_retired_lesson_leaves_the_accepted_list(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    kept = await Lesson.accept(druks_db, text="Keep tests small.", account=account)
    retired = await Lesson.accept(druks_db, text="Name a value with a noun.", account=account)

    await retired.retire(druks_db)

    assert await Lesson.list_accepted(druks_db) == [kept]


async def test_a_lesson_needs_text(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")

    with pytest.raises(ValidationError, match="needs text"):
        await Lesson.accept(druks_db, text="   ", account=account)


async def test_accepting_past_the_cap_fails_until_a_lesson_is_retired(druks_db, monkeypatch):
    monkeypatch.setattr(models, "LESSON_INDEX_MAX_LINES", 2)
    account = await Account.get_or_create(druks_db, "owner@example.com")
    first = await Lesson.accept(druks_db, text="First.", account=account)
    await Lesson.accept(druks_db, text="Second.", account=account)

    with pytest.raises(LessonIndexFull, match="Retire a lesson or merge two"):
        await Lesson.accept(druks_db, text="Third.", account=account)

    await first.retire(druks_db)
    await Lesson.accept(druks_db, text="Third.", account=account)
