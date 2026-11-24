import re

import pytest
from druks import ui
from druks.chat.blocks import parse_blocks
from druks.exceptions import ValidationError

TICKET_CHART = (
    '[Chart(kind="bar", title="Builds this week", categories=["Mon", "Tue"], '
    'series=[ChartSeries(label="Builds", points=[4, 7])])]'
)


def stored(*blocks) -> list[dict]:
    """The blocks as a reply keeps them."""
    return [block.model_dump(mode="json", by_alias=True) for block in blocks]


def test_the_ticket_s_chart_reads_as_the_block_that_its_call_makes():
    chart = ui.Chart(
        kind="bar",
        title="Builds this week",
        categories=["Mon", "Tue"],
        series=[ui.ChartSeries(label="Builds", points=[4, 7])],
    )

    # The chart nests six levels deep. A form nests five.
    assert parse_blocks(TICKET_CHART) == stored(chart)


def test_blocks_read_nested_blocks_their_parts_and_plain_values_as_pages_write_them():
    calls = [
        'Section(title="Rack 3", blocks=[Card(title="Fans", blocks=[Facts(['
        'Fact("Owner", "Ana"), Fact("Fans", 12), '
        'Fact("State", StatusValue("Stalled", tone="danger")), '
        'Fact("Docs", ControlsValue([Link("Runbook", url="https://docs.example/a")]))'
        "])])])",
        'Metrics([Metric("Noise", NumberValue(72, unit="dB"))], title="Sound")',
        'Table(columns=[TableColumn("Fan"), TableColumn("Speed", align="end")], '
        'rows=[TableRow(["front", -1200.5])])',
        'List([TextValue("Docs", link=Link(url="https://docs.example/a")), '
        'TimeValue("2026-11-22T10:00:00Z")])',
        'Link("Pull request", url="https://github.com/acme/app/pull/7")',
    ]

    blocks = parse_blocks("[" + ", ".join(calls) + "]")

    assert blocks == stored(
        ui.Section(
            title="Rack 3",
            blocks=[
                ui.Card(
                    title="Fans",
                    blocks=[
                        ui.Facts(
                            [
                                ui.Fact("Owner", "Ana"),
                                ui.Fact("Fans", 12),
                                ui.Fact("State", ui.StatusValue("Stalled", tone="danger")),
                                ui.Fact(
                                    "Docs",
                                    ui.ControlsValue(
                                        [ui.Link("Runbook", url="https://docs.example/a")]
                                    ),
                                ),
                            ]
                        )
                    ],
                )
            ],
        ),
        ui.Metrics([ui.Metric("Noise", ui.NumberValue(72, unit="dB"))], title="Sound"),
        ui.Table(
            columns=[ui.TableColumn("Fan"), ui.TableColumn("Speed", align="end")],
            rows=[ui.TableRow(["front", -1200.5])],
        ),
        ui.List(
            [
                ui.TextValue("Docs", link=ui.Link(url="https://docs.example/a")),
                ui.TimeValue("2026-11-22T10:00:00Z"),
            ]
        ),
        ui.Link("Pull request", url="https://github.com/acme/app/pull/7"),
    )


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ('[Form(action=Action(label="Ship", operation="ship"))]', "Form"),
        ('[Action(label="Ship", operation="ship")]', "Action"),
        ('[Section(blocks=[GateControls("note-1")])]', "GateControls"),
        ('[Facts([Fact("Work", SubjectStatus("note-1"))])]', "SubjectStatus"),
        ('[Link("The note", subject="note-1")]', "subject="),
        ('[Link("Settings", page="settings")]', "page="),
        ('[Section(name="notes", follows="note-1", blocks=[Text("Notes")])]', "follows="),
    ],
    ids=[
        "form",
        "action",
        "gate-controls",
        "subject-status",
        "subject",
        "page",
        "follows",
    ],
)
def test_a_reply_refuses_an_operation_or_a_subject_at_every_depth_with_its_reason(text, refused):
    with pytest.raises(ValidationError) as raised:
        parse_blocks(text)

    assert str(raised.value).startswith(f"A reply takes no {refused}")


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ('Text(__import__("os"))', "__import__ is not one of Text, Markdown, Quote,"),
        ('__import__("os").system({command!r})', "Call each druks.ui class by its bare name"),
    ],
    ids=["import", "block"],
)
def test_the_blocks_run_none_of_their_text(tmp_path, call, reason):
    ran = tmp_path / "ran"

    with pytest.raises(ValidationError, match=re.escape(reason)):
        parse_blocks("[" + call.format(command=f"touch {ran}") + "]")

    assert not ran.exists()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("[]", "The list has no blocks. Give at least one block."),
        ('Text("Done.")', "Write the blocks as one list"),
        ('[Fact("Owner", "Ana")]', "Fact is not one of Text, Markdown,"),
        (
            '[Chart(categories=["Mon", "Tue"], series=[ChartSeries(label="Builds", points=[4])])]',
            "Chart: Value error, chart series ['Builds'] do not carry one point for each of the 2 "
            "categories.",
        ),
        (
            '[Section(blocks=[Link(url="https://example.com/a")])]',
            "a Link that stands on its own has no label. Give it the words that it shows.",
        ),
        ('[Facts(title="Owners"), Text("")]', "The blocks show nothing."),
    ],
    ids=[
        "empty-list",
        "not-a-list",
        "part-as-a-block",
        "check-of-the-whole-block",
        "check-of-the-page",
        "nothing-to-show",
    ],
)
def test_a_block_of_a_wrong_shape_gets_the_reason(text, reason):
    with pytest.raises(ValidationError, match=re.escape(reason)):
        parse_blocks(text)
