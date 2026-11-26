import pytest
from druks.accounts.models import Account
from druks.core.lessons import constants
from druks.core.lessons.exceptions import LessonIndexFull
from druks.core.lessons.models import Lesson, LessonRepeat
from druks.durable.models import Run
from druks.events.models import Event
from druks.exceptions import ValidationError
from druks.sandbox.datastructures import HomeFile
from sqlalchemy import select


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


async def test_a_lesson_names_a_run_that_exists(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")

    with pytest.raises(ValidationError, match="No run has the id run-9"):
        await Lesson.accept(
            druks_db, text="Keep tests small.", account=account, source_run_id="run-9"
        )


async def test_a_lesson_names_an_event_that_exists(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")

    with pytest.raises(ValidationError, match="No event has the id 99999"):
        await Lesson.accept(
            druks_db, text="Keep tests small.", account=account, source_event_id=99999
        )


async def test_a_mark_names_an_event_that_exists(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    lesson = await Lesson.accept(druks_db, text="Keep tests small.", account=account)

    with pytest.raises(ValidationError, match="No event has the id 99999"):
        await LessonRepeat.mark(druks_db, event_id=99999, lesson=lesson, account=account)


async def test_a_second_mark_on_a_correction_replaces_the_first(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    correction = Event(type="workflow.running")
    druks_db.add(correction)
    await druks_db.flush()
    first = await Lesson.accept(druks_db, text="First.", account=account)
    second = await Lesson.accept(druks_db, text="Second.", account=account)

    await LessonRepeat.mark(druks_db, event_id=correction.id, lesson=first, account=account)
    await LessonRepeat.mark(druks_db, event_id=correction.id, lesson=second, account=account)

    (repeat,) = (await druks_db.scalars(select(LessonRepeat))).all()
    assert repeat.lesson_id == second.id


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
    monkeypatch.setattr(constants, "LESSON_INDEX_MAX_LINES", 2)
    account = await Account.get_or_create(druks_db, "owner@example.com")
    first = await Lesson.accept(druks_db, text="First.", account=account)
    await Lesson.accept(druks_db, text="Second.", account=account)

    with pytest.raises(LessonIndexFull, match="Retire a lesson or merge two"):
        await Lesson.accept(druks_db, text="Third.", account=account)

    await first.retire(druks_db)
    await Lesson.accept(druks_db, text="Third.", account=account)


async def test_the_vault_holds_an_index_and_a_file_for_each_accepted_lesson(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    kept = await Lesson.accept(
        druks_db, text="Say why, never what.\nThe code says what.", account=account
    )
    retired = await Lesson.accept(druks_db, text="Keep tests small.", account=account)
    await retired.retire(druks_db)

    vault = await Lesson.get_vault(druks_db)

    index = f"- Say why, never what. — {kept.id}.md"
    assert vault.index == index
    assert vault.files == (
        HomeFile("work/memory/MEMORY.md", index, clears="work/memory"),
        HomeFile(f"work/memory/{kept.id}.md", "Say why, never what.\nThe code says what."),
    )


async def test_a_box_is_cleared_once_every_lesson_is_retired(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    lesson = await Lesson.accept(druks_db, text="Keep tests small.", account=account)
    await lesson.retire(druks_db)

    vault = await Lesson.get_vault(druks_db)

    assert vault.files == (HomeFile("", "", clears="work/memory"),)
    assert vault.index == ""


async def test_an_installation_with_no_lesson_gets_an_empty_vault(druks_db):
    vault = await Lesson.get_vault(druks_db)

    assert vault.files == ()
    assert vault.index == ""


async def test_a_title_with_brackets_stays_one_plain_line_of_the_index(druks_db):
    account = await Account.get_or_create(druks_db, "owner@example.com")
    lesson = await Lesson.accept(
        druks_db, text="Avoid f(x) when it ) returns [early]\nBecause.", account=account
    )

    vault = await Lesson.get_vault(druks_db)

    assert vault.index == f"- Avoid f(x) when it ) returns [early] — {lesson.id}.md"
