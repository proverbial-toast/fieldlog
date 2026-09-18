"""Tab moves between panes; the arrow keys move *within* the focused pane.

The contract the maintainer specified: Tab goes RECIPES → VARIANTS → RECIPES,
up/down step the rows of whichever pane has the keyboard, and Enter runs. The
arrow keys must never change which pane is focused, and the mouse must never
quietly take the keyboard somewhere else — a click on a recipe puts the cursor
on that row and leaves the arrows stepping recipes.

Two things could break that, so both are guarded here: the pane containers are
`VerticalScroll`, which binds up/down to its own scrolling and would shadow the
app's cursor keys if it ever took focus; and `tree_row_clicked` used to call
`_focus_variants()`, which left the arrows stepping variants after any click on
a recipe.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.containers import VerticalScroll

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1")
    session.workspace_dir = workspace
    return FieldlogApp(session)


def _park_on_a_multi_variant_tool(app: FieldlogApp) -> None:
    """Put the RECIPES cursor on a tool that has more than one variant.

    A single-variant tool makes the VARIANTS arrows a legitimate no-op, which
    would prove nothing either way.
    """
    for index, row in enumerate(app._rows):
        if row.kind == "header":
            continue
        tool = app.get_tool(row.tool_id) if row.tool_id else None
        if tool and len(tool.get("presets", [])) > 1:
            app.cursor = index
            app._select_row(row)
            app._paint_rows()
            return
    pytest.skip("no catalogued tool has more than one variant")


@pytest.mark.parametrize("size", [(140, 45), (140, 20), (70, 30)], ids=["split", "short", "stacked"])
async def test_tab_cycles_panes_and_arrows_stay_inside_one(tmp_workspace: Path, size):
    """The same contract in every layout, including the stacked one."""
    app = _app(tmp_workspace)
    async with app.run_test(size=size) as pilot:
        assert app.focus_pane() == "recipes"

        first = app.cursor
        await pilot.press("down")
        assert app.cursor > first, "down must step the RECIPES cursor"
        assert app.focus_pane() == "recipes", "an arrow key must not change pane"
        await pilot.press("up")
        assert app.cursor == first
        assert app.focus_pane() == "recipes"

        await pilot.press("tab")
        assert app.focus_pane() == "variants"

        _park_on_a_multi_variant_tool(app)
        cursor_before = app.cursor
        variant_before = app.selected_preset_id
        await pilot.press("down")
        assert app.selected_preset_id != variant_before, "down must step the VARIANTS list"
        assert app.cursor == cursor_before, "stepping variants must not move the recipe cursor"
        assert app.focus_pane() == "variants"

        await pilot.press("up")
        assert app.selected_preset_id == variant_before
        assert app.focus_pane() == "variants"

        await pilot.press("tab")
        assert app.focus_pane() == "recipes"
        back_in_recipes = app.cursor
        await pilot.press("down")
        assert app.cursor > back_in_recipes, "tabbing back must hand the arrows to RECIPES again"


async def test_the_list_panes_never_take_textual_focus(tmp_workspace: Path):
    """A focused VerticalScroll would eat up/down for scrolling."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 20)) as pilot:
        tree = app.query_one("#recipe-tree", VerticalScroll)
        variants = app.query_one("#variants-scroll", VerticalScroll)
        assert tree.allow_vertical_scroll, "this size should overflow, or the test proves nothing"
        assert not tree.focusable
        assert not variants.focusable

        tree.focus()
        await pilot.pause()
        assert app.focused is not tree

        cursor_before = app.cursor
        await pilot.press("down")
        assert app.cursor > cursor_before, "the app's cursor keys must win over container scrolling"


async def test_clicking_a_recipe_leaves_the_keyboard_in_recipes(tmp_workspace: Path):
    """The regression the maintainer hit: after a click the arrows stepped variants."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        rows = [w for w in app.query(".recipe-row") if getattr(w, "index", None) is not None]
        clickable = [w for w in rows if app._rows[w.index].kind != "header"]
        assert clickable, "no selectable recipe rows were rendered"
        target = clickable[1] if len(clickable) > 1 else clickable[0]

        app.tree_row_clicked(target.index)
        await pilot.pause()
        assert app.cursor == target.index
        assert app.focus_pane() == "recipes", "a click must not hand the arrows to VARIANTS"

        cursor_before = app.cursor
        await pilot.press("down")
        assert app.cursor > cursor_before, "arrows must still step recipes after a click"
        assert app.focus_pane() == "recipes"
