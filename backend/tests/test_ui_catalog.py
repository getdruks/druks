import json
from pathlib import Path
from typing import get_args

import pytest
from druks import ui
from druks.ui.blocks import PageBlock
from druks.ui.exceptions import NoTextFormError

# The page that the dashboard tests render: one of every block that the contract carries.
CATALOG = json.loads((Path(__file__).parents[2] / "frontend/src/druksui/catalog.json").read_text())
BLOCKS = {model.model_fields["block"].default: model for model in get_args(get_args(ui.Block)[0])}
VALUES = {model.model_fields["value"].default: model for model in get_args(get_args(ui.Value)[0])}
# A block with no text form defines no ``to_markdown`` of its own. An action and a form have
# a text form only when they hold the address of their page. No action or form of the
# catalog holds it.
REFUSED = {"action", "form"} | {
    kind for kind, model in BLOCKS.items() if model.to_markdown is PageBlock.to_markdown
}


def entries(value):
    """Every block and value in ``value``, at any depth."""
    if isinstance(value, list):
        for item in value:
            yield from entries(item)
    if isinstance(value, dict):
        if value.get("block") in BLOCKS or (
            isinstance(value.get("value"), str) and value["value"] in VALUES
        ):
            yield value
        for item in value.values():
            yield from entries(item)


def kind(entry) -> str:
    return entry.get("block") or entry["value"]


def without_refused(value):
    """``value`` without the blocks that the operator acts through, and without the
    selection that their actions ran on."""
    if isinstance(value, list):
        return [without_refused(item) for item in value if not is_refused(item)]
    if is_refused(value):
        return None
    if isinstance(value, dict):
        kept = {key: without_refused(item) for key, item in value.items()}
        if "select" in kept and not kept["actions"]:
            kept["select"] = ""
        return kept
    return value


def is_refused(value) -> bool:
    return isinstance(value, dict) and value.get("block") in REFUSED


def read(entry):
    # A catalog value carries the status that Druks served its page with. The value keeps it.
    return (BLOCKS.get(entry.get("block")) or VALUES[entry["value"]]).model_validate(entry)


ENTRIES = list(entries(CATALOG["blocks"]))


def test_only_the_blocks_the_operator_acts_through_have_no_text_form():
    assert {BLOCKS[refused] for refused in REFUSED} == {ui.Action, ui.Form, ui.GateControls}


def test_the_catalog_carries_every_block_and_value():
    # A block that someone adds to the contract must then be in the catalog. The tests that
    # follow then check its text form.
    assert {kind(entry) for entry in ENTRIES} == set(BLOCKS) | set(VALUES)


@pytest.mark.parametrize(
    "entry",
    [entry for entry in ENTRIES if not is_refused(entry) and without_refused(entry) == entry],
    ids=kind,
)
def test_every_catalog_entry_with_nothing_refused_writes_markdown(entry):
    assert isinstance(read(entry).to_markdown(), str)


@pytest.mark.parametrize(
    "entry",
    [entry for entry in ENTRIES if not is_refused(entry) and without_refused(entry) != entry],
    ids=kind,
)
def test_a_catalog_block_that_holds_a_refused_one_writes_markdown_without_it(entry):
    with pytest.raises(NoTextFormError):
        read(entry).to_markdown()

    assert isinstance(read(without_refused(entry)).to_markdown(), str)
