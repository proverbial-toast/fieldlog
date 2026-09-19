"""The RECIPES highlight and the VARIANTS pane always name the same thing.

Reported as: "select a chain, press `[!]` — the cursor lands on the bash tool
row while the pane still shows `https-check`, and Enter runs the chain." The
highlight is the only promise the TUI makes about what `[Enter] Run` will run,
and it came apart in both directions. `[!]` and the filter box hid the row the
cursor was on and moved it without telling VARIANTS; the palette and the recipe
manager moved the selection without moving the cursor. Either way the operator
read one recipe off the screen and ran another.

The invariant every test here asserts is one line: the row under the cursor is
the row that names what the panes are painting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession

# `true` and `false` are on every box these tests can run on; the third name is
# not, which is what makes its rows disappear under `[!] runnable`. Two distinct
# present binaries, because the filter box searches the binary name — one shared
# `bin` would make every query match everything or nothing.
ABSENT = "fieldlog-no-such-binary"

TOOLS = [
    {
        "id": "alpha", "bin": "true", "name": "Alpha",
        "presets": [{"id": "one", "name": "one", "flags": ""}, {"id": "two", "name": "two", "flags": ""}],
    },
    {
        "id": "beta", "bin": "false", "name": "Beta",
        "presets": [{"id": "only", "name": "only", "flags": ""}],
    },
    {
        "id": "absent", "bin": ABSENT, "name": "Absent",
        "presets": [{"id": "x", "name": "x", "flags": ""}],
    },
]

CHAINS = [
    {"id": "needs-absent", "name": "needs the missing tool",
     "steps": [{"recipe": "absent/x", "continue": False}]},
]


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1", interface="eth0")
    session.workspace_dir = workspace
    app = FieldlogApp(session)
    # Both, not one: the tree reads `recipes` while chain steps resolve against
    # the catalog, and a chain whose step is only in one of them is unrunnable
    # for a reason that has nothing to do with these tests.
    app.catalog.tools = TOOLS
    app.catalog.chains = CHAINS
    app._recipes = TOOLS
    return app


def _agree(app: FieldlogApp) -> None:
    """The row under the cursor names what VARIANTS and ARGS are painting."""
    assert app._rows, "the tree is empty"
    row = app._rows[app.cursor]
    assert row.kind != "header", "the cursor is parked on a section heading"
    assert app._row_shows(row), (
        f"cursor on {row.kind} {row.bin}/{row.preset_id}, "
        f"panes on chain={app.selected_chain_id} "
        f"{app.selected_tool_id}/{app.selected_preset_id}"
    )


def _row_at(app: FieldlogApp, kind: str, **fields) -> int:
    return next(
        i for i, r in enumerate(app._rows)
        if r.kind == kind and all(getattr(r, k) == v for k, v in fields.items())
    )


async def test_hiding_the_row_under_the_cursor_takes_the_panes_with_it(tmp_workspace: Path):
    """The reported bug, in its own words: a chain, then `[!]`.

    The chain's only step wants a binary that is not installed, so turning
    runnable-only back on deletes the row the operator is standing on. The
    cursor has to go somewhere, and wherever it goes the panes follow — before
    this, VARIANTS kept painting the chain and Enter kept running it.
    """
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.action_toggle_hide_missing()            # [!] all — the blocked chain is listed
        await pilot.pause()
        assert app.hide_missing is False

        app.cursor = _row_at(app, "chain", bin="needs-absent")
        app._select_row(app._rows[app.cursor])
        await pilot.pause()
        assert app.selected_chain_id == "needs-absent"
        _agree(app)

        app.action_toggle_hide_missing()            # [!] runnable — the chain row goes
        await pilot.pause()

        assert all(r.bin != "needs-absent" for r in app._rows), "the blocked chain is still listed"
        assert app.selected_chain_id is None, "VARIANTS is still painting the hidden chain"
        _agree(app)


async def test_a_surviving_row_keeps_the_cursor_across_the_toggle(tmp_workspace: Path):
    """`[!]` used to send the cursor to row 0 whatever was under it.

    Showing the unrunnable tools is a question about the list, not about the
    operator's place in it: a recipe that is listed either way keeps the cursor.
    """
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.cursor = _row_at(app, "tool", tool_id="beta")
        app._select_row(app._rows[app.cursor])
        await pilot.pause()

        app.action_toggle_hide_missing()            # now showing everything
        await pilot.pause()

        assert app._rows[app.cursor].tool_id == "beta"
        assert app.selected_tool_id == "beta"
        _agree(app)


async def test_filtering_away_the_selected_recipe_takes_the_panes_with_it(tmp_workspace: Path):
    """Typing in the filter box is the same hazard as `[!]`, by another route."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.cursor = _row_at(app, "tool", tool_id="beta")
        app._select_row(app._rows[app.cursor])
        await pilot.pause()
        assert app.selected_tool_id == "beta"

        app.filter_text = "tru"
        app._rebuild_tree()
        await pilot.pause()

        assert all(r.tool_id != "beta" for r in app._rows), "the filter kept a non-matching tool"
        assert app.selected_tool_id == "alpha", "VARIANTS is still painting the filtered-out tool"
        _agree(app)


async def test_the_cursor_follows_a_recipe_picked_somewhere_else(tmp_workspace: Path):
    """The recipe manager and the palette select without touching the tree.

    They left the cursor on whatever it was on, so the next arrow key stepped
    off a row nobody had highlighted and the highlight disagreed with the pane
    in the meantime.
    """
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.cursor = _row_at(app, "tool", tool_id="alpha")
        app._select_row(app._rows[app.cursor])
        await pilot.pause()

        app.select_tool("beta")                     # what the recipe manager calls
        await pilot.pause()

        assert app._rows[app.cursor].tool_id == "beta"
        _agree(app)


async def test_the_cursor_and_the_panes_agree_from_the_first_paint(tmp_workspace: Path):
    """Boot is the same invariant: `ping/sweep` is the built-in default and it
    is not in this catalog, so something has to give way before the first
    keypress rather than on it."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)):
        _agree(app)


@pytest.mark.parametrize(
    "tool_id, preset_id, expected",
    [
        ("alpha", "two", ("alpha", "two")),      # both name something: left alone
        ("alpha", "gone", ("alpha", "one")),     # a preset the tool lost: its first
        ("ping", "sweep", ("alpha", "one")),     # neither: the first recipe there is
    ],
)
def test_the_selection_always_names_a_recipe_the_catalog_has(
    tmp_workspace: Path, tool_id, preset_id, expected
):
    """`selected_recipe` writes its answer back, so no reader has to guess.

    Each pane used to fall back on its own — one to `recipes[0]`, another to
    `presets[0]` — and leave the ids untouched, so the app could hold
    `ping/sweep` while showing `alpha/one`. Nothing could then match a tree row
    against the selection, which is what `_row_shows` has to do.
    """
    app = _app(tmp_workspace)
    app.selected_tool_id, app.selected_preset_id = tool_id, preset_id

    tool, preset = app.selected_recipe()

    assert (tool["id"], preset["id"]) == expected
    assert (app.selected_tool_id, app.selected_preset_id) == expected


async def test_a_filter_matching_nothing_leaves_nothing_highlighted(tmp_workspace: Path):
    """The one case the invariant cannot hold, held deliberately.

    With no row to stand on the cursor parks on the `Results` heading, which
    every caller refuses to act on. The panes keep the last recipe rather than
    blanking: the run button still describes what `[Enter] Run` would run, so
    nothing on screen is untrue — and a palette pick the filter hides relies on
    the selection surviving a tree that cannot show it.
    """
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.cursor = _row_at(app, "tool", tool_id="beta")
        app._select_row(app._rows[app.cursor])
        await pilot.pause()

        app.filter_text = "no-such-recipe-anywhere"
        app._rebuild_tree()
        await pilot.pause()

        assert [r.kind for r in app._rows] == ["header"], "the filter matched something after all"
        assert app._rows[app.cursor].kind == "header"
        assert app.selected_tool_id == "beta", "the selection was dropped with nothing to replace it"
