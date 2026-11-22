import ast
from typing import get_args

from druks import ui
from druks.exceptions import ValidationError
from druks.ui.expressions import Expression

from . import constants

# ui.Block holds the union of the blocks and the discriminator that picks one of them.
DISPLAY_BLOCKS = {
    block.__name__: block
    for block in get_args(get_args(ui.Block)[0])
    if block.__name__ not in constants.REFUSED_BLOCKS
}


class Blocks(Expression):
    """The blocks of a reply, as the agent writes them. A call inside a block names a block
    or a part of one: a value, a fact, a metric, a series of a chart, a column or a row of a
    table, an item of a timeline, a step of a progress, or a file."""

    error = ValidationError
    noun = "blocks"
    text_limit = constants.BLOCKS_TEXT_LIMIT
    depth_limit = constants.BLOCKS_DEPTH_LIMIT
    models = {
        **DISPLAY_BLOCKS,
        **{
            part.__name__: part
            for part in (
                ui.TextValue,
                ui.NumberValue,
                ui.StatusValue,
                ui.TimeValue,
                ui.ControlsValue,
                ui.Fact,
                ui.Metric,
                ui.ChartSeries,
                ui.TableColumn,
                ui.TableRow,
                ui.TimelineItem,
                ui.ProgressStep,
                ui.FileSummary,
            )
        },
    }
    refused = constants.REFUSED_BLOCKS


def parse_blocks(text: str) -> list[dict]:
    """The blocks that ``text`` declares, as a reply keeps them: one Python list of block
    calls, as in ``[Facts([Fact("Owner", "Ana")])]``. Druks reads the text with ``ast`` and
    runs none of it, and it checks each block as an app page does."""
    match Blocks.get_expression(text):
        case ast.List(elts=[_, *_] as calls):
            blocks = [Blocks.get_object(call, DISPLAY_BLOCKS, depth=2) for call in calls]
        case ast.List():
            raise ValidationError("The list has no blocks. Give at least one block.")
        case _:
            raise ValidationError(
                'Write the blocks as one list, such as [Facts([Fact("Owner", "Ana")])].'
            )
    reply = ui.Stack(blocks)
    try:
        reply.check_placement(followed=False, regions=set())
    except ValueError as error:
        raise ValidationError(str(error)) from error
    # A reply of blocks that show nothing would reach its person empty.
    if not reply.to_markdown():
        raise ValidationError(
            "The blocks show nothing. Give them words, numbers, or links to show."
        )
    return [block.model_dump(mode="json", by_alias=True) for block in blocks]
