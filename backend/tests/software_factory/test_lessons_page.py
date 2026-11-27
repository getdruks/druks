from datetime import UTC, datetime, timedelta

from druks.contrib.software_factory import pages
from druks.core.lessons.models import Lesson
from druks.events import Event
from sqlalchemy import select

_PAGE = "/api/software_factory/pages/lessons"
_LESSONS = "/api/software_factory/lessons"
_CORRECTIONS = "/api/software_factory/corrections"


async def _reply(
    druks_db, result: dict, *, run: str = "run-1", gate: str = "review_work", at=None
) -> Event:
    event = Event(
        type="workflow.running",
        app="software_factory",
        subject_key="DRU-1 Rename the flag",
        payload={"run": run, "kind": "software_factory.build", "gate": gate, "result": result},
    )
    if at:
        event.created_at = at
    druks_db.add(event)
    await druks_db.flush()
    return event


def _section(page: dict, title: str) -> dict:
    return next(block for block in page["blocks"] if block.get("title") == title)


def _cards(page: dict, title: str) -> list[dict]:
    return _section(page, title)["blocks"][0]["cards"]


def _metric(page: dict, label: str) -> float:
    metrics = page["blocks"][0]["metrics"]
    return next(metric["value"]["number"] for metric in metrics if metric["label"] == label)


async def test_the_page_lists_gate_replies_that_carry_words_newest_first(druks_client, druks_db):
    older = await _reply(druks_db, {"action": "request_changes", "body": "Cut the comments."})
    newer = await _reply(druks_db, {"action": "approve", "note": "Name it for what it is."})
    await _reply(druks_db, {"action": "approve", "body": ""})

    page = (await druks_client.get(_PAGE)).json()

    cards = _cards(page, "Corrections")
    assert [card["blocks"][0]["text"] for card in cards] == [
        "Name it for what it is.",
        "Cut the comments.",
    ]
    assert cards[0]["controls"][0]["arguments"] == {"event_id": newer.id}
    assert cards[1]["controls"][0]["arguments"]["event_id"] == older.id


async def test_a_correction_becomes_a_lesson_that_shows_its_source_and_who_accepted_it(
    druks_client, druks_db
):
    correction = await _reply(druks_db, {"action": "request_changes", "body": "Cut the comments."})

    created = await druks_client.post(
        _LESSONS, json={"text": "Say why, never what.", "event_id": correction.id}
    )

    assert created.status_code == 201
    lesson = (await druks_db.scalars(select(Lesson))).one()
    assert lesson.source_event_id == correction.id
    page = (await druks_client.get(_PAGE)).json()
    (card,) = _cards(page, "Lessons")
    assert card["title"] == "Say why, never what."
    assert lesson.accepted_by.username in card["description"]


async def test_a_retired_lesson_leaves_the_page(druks_client, druks_db):
    created = await druks_client.post(_LESSONS, json={"text": "Keep tests small."})

    retired = await druks_client.post(f"{_LESSONS}/{created.json()['id']}/retire")

    assert retired.status_code == 200
    page = (await druks_client.get(_PAGE)).json()
    assert _cards(page, "Lessons") == []
    assert _metric(page, "Accepted lessons") == 0


async def test_a_correction_marked_as_a_repeat_names_its_lesson_and_is_counted(
    druks_client, druks_db
):
    correction = await _reply(druks_db, {"action": "approve", "note": "Cut the comments."})
    lesson = (await druks_client.post(_LESSONS, json={"text": "Say why, never what."})).json()

    marked = await druks_client.post(
        f"{_CORRECTIONS}/{correction.id}/repeat", json={"lesson_id": lesson["id"]}
    )

    assert marked.status_code == 200
    page = (await druks_client.get(_PAGE)).json()
    (card,) = _cards(page, "Corrections")
    assert card["blocks"][1]["text"] == "Repeats the lesson: Say why, never what."
    assert _metric(page, "Marked as repeats") == 1


async def test_a_correction_cannot_repeat_a_retired_lesson(druks_client, druks_db):
    correction = await _reply(druks_db, {"action": "approve", "note": "Cut the comments."})
    lesson = (await druks_client.post(_LESSONS, json={"text": "Say why, never what."})).json()
    await druks_client.post(f"{_LESSONS}/{lesson['id']}/retire")

    marked = await druks_client.post(
        f"{_CORRECTIONS}/{correction.id}/repeat", json={"lesson_id": lesson["id"]}
    )

    assert marked.status_code == 422


async def test_corrections_are_counted_for_each_build(druks_client, druks_db):
    await _reply(druks_db, {"action": "request_changes", "body": "One."}, run="run-1")
    await _reply(druks_db, {"action": "request_changes", "body": "Two."}, run="run-1")
    await _reply(druks_db, {"action": "approve", "body": ""}, run="run-2")

    page = (await druks_client.get(_PAGE)).json()

    assert _metric(page, "Corrections") == 2
    assert _metric(page, "Per build") == 1.0


async def test_a_correction_whose_lesson_was_retired_says_so(druks_client, druks_db):
    correction = await _reply(druks_db, {"action": "approve", "note": "Cut the comments."})
    lesson = (await druks_client.post(_LESSONS, json={"text": "Say why, never what."})).json()
    await druks_client.post(
        f"{_CORRECTIONS}/{correction.id}/repeat", json={"lesson_id": lesson["id"]}
    )
    await druks_client.post(f"{_LESSONS}/{lesson['id']}/retire")

    page = (await druks_client.get(_PAGE)).json()

    (card,) = _cards(page, "Corrections")
    assert card["blocks"][1]["text"] == "Repeats the lesson: (retired)"


def _judgement(page: dict) -> dict:
    return next(block for block in page["blocks"] if block["block"] == "callout")


async def test_the_page_says_when_to_judge_before_the_first_lesson(druks_client):
    page = (await druks_client.get(_PAGE)).json()

    callout = _judgement(page)
    assert callout["title"] == "When to judge"
    assert "after 30 builds or 30 days from the first accepted lesson" in callout["text"]
    assert callout["text"].endswith("No lesson has been accepted yet.")


async def test_the_judgement_counts_the_builds_since_the_first_lesson(druks_client, druks_db):
    earlier = datetime.now(UTC) - timedelta(days=3)
    await _reply(druks_db, {"action": "request_changes", "body": "Old."}, run="run-0", at=earlier)
    await druks_client.post(_LESSONS, json={"text": "Say why, never what."})
    await _reply(druks_db, {"action": "request_changes", "body": "One."}, run="run-1")
    await _reply(druks_db, {"action": "request_changes", "body": "Two."}, run="run-2")

    callout = _judgement((await druks_client.get(_PAGE)).json())

    assert ": 2 builds, 0 days." in callout["text"]
    assert callout["tone"] == "info"


async def test_the_judgement_point_is_flagged_when_it_has_come(druks_client, druks_db, monkeypatch):
    monkeypatch.setattr(pages, "JUDGEMENT_BUILDS", 1)
    await druks_client.post(_LESSONS, json={"text": "Say why, never what."})
    await _reply(druks_db, {"action": "request_changes", "body": "One."}, run="run-1")

    callout = _judgement((await druks_client.get(_PAGE)).json())

    assert callout["tone"] == "warning"


async def test_an_event_id_of_zero_means_no_event(druks_client, druks_db):
    created = await druks_client.post(_LESSONS, json={"text": "Keep tests small.", "event_id": 0})

    assert created.status_code == 201
    lesson = (await druks_db.scalars(select(Lesson))).one()
    assert lesson.source_event_id is None


async def test_a_run_in_the_address_fills_the_run_of_the_new_lesson(druks_client):
    # The shell keeps ``run`` for a parked decision and never sends it to a page.
    page = (await druks_client.get(_PAGE, params={"run_id": "run-7"})).json()

    form = next(block for block in page["blocks"] if block["block"] == "form")
    field = next(field for field in form["fields"] if field["name"] == "run_id")
    assert field["value"] == "run-7"


async def test_the_corrections_of_other_workflows_are_not_counted_for_each_build(
    druks_client, druks_db
):
    await _reply(druks_db, {"action": "request_changes", "body": "Build."}, run="run-1")
    other = Event(
        type="workflow.running",
        app="software_factory",
        subject_key="DRU-2 Profile",
        payload={
            "run": "run-2",
            "kind": "software_factory.profile",
            "gate": "review",
            "result": {"note": "Not a build."},
        },
    )
    druks_db.add(other)
    await druks_db.flush()

    page = (await druks_client.get(_PAGE)).json()

    assert _metric(page, "Corrections") == 1
    assert [card["blocks"][0]["text"] for card in _cards(page, "Corrections")] == ["Build."]
