"""Esc walks back out of VARIANTS, and Ctrl+W closes a tab.

Two things an operator reached for and did not find. `Esc` already meant "back
to recipes" — but only in the stacked layout, where VARIANTS is an accordion
pane covering RECIPES; in split, which is the default at 120 columns and up, it
did nothing, though `Tab` had taken the arrow keys into VARIANTS just the same.
And closing a tab was a bare `w`, where every tabbed thing an operator already
uses closes with `Ctrl+W`. `w` is gone rather than kept alongside: a single
letter that closes something is the convention broken, and it sat next to the
`W` that closes every finished tab at once.

Neither the escape chain nor tab closing had any test before this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from textual.widgets import Input

from fieldlog.app import FieldlogApp
from fieldlog.state import ActiveJob, TargetSession
from fieldlog.tui.models import TabDescriptor

SPLIT, STACKED = (140, 45), (70, 30)


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1", interface="eth0")
    session.workspace_dir = workspace
    return FieldlogApp(session)


def _job_tab(app: FieldlogApp, tab_id: str = "job-1", status: str = "done") -> None:
    app.tabs.append(TabDescriptor(tab_id, f"ping/sweep #{tab_id[-1]}", status, "ping", job_id=tab_id[-1]))
    app._refresh_tab_strip()


# ---- Esc: back out of VARIANTS ------------------------------------------


@pytest.mark.parametrize("size", [SPLIT, STACKED], ids=["split", "stacked"])
async def test_escape_in_variants_goes_back_to_recipes(tmp_workspace: Path, size):
    """The same answer in both layouts. Split is the one that used to do nothing."""
    app = _app(tmp_workspace)
    async with app.run_test(size=size) as pilot:
        await pilot.press("tab")
        assert app.focus_pane() == "variants"

        await pilot.press("escape")

        assert app.focus_pane() == "recipes"


@pytest.mark.parametrize("size", [SPLIT, STACKED], ids=["split", "stacked"])
async def test_escape_in_recipes_does_nothing_at_all(tmp_workspace: Path, size):
    """Esc never quits, and with nothing to back out of it is not a no-op that
    costs something — the pane, the filter and the selection all stay put."""
    app = _app(tmp_workspace)
    async with app.run_test(size=size) as pilot:
        before = (app.focus_pane(), app.cursor, app.selected_tool_id, app.selected_preset_id)

        await pilot.press("escape")
        await pilot.press("escape")

        assert app.is_running, "Esc must never quit the app"
        assert (app.focus_pane(), app.cursor, app.selected_tool_id, app.selected_preset_id) == before


async def test_a_filter_is_cleared_before_the_pane_is_left(tmp_workspace: Path):
    """The chain's order is unchanged: state first, then focus.

    A filter is a visible state over the whole RECIPES list, so it is the
    nearer thing to undo; the pane move is the step after it.
    """
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        app.filter_text = "ping"
        app.query_one("#filter-input", Input).value = "ping"
        app._rebuild_tree()
        await pilot.press("tab")
        assert app.focus_pane() == "variants"

        await pilot.press("escape")
        assert app.filter_text == "", "the first Esc should have cleared the filter"
        assert app.focus_pane() == "variants", "and left the pane where it was"

        await pilot.press("escape")
        assert app.focus_pane() == "recipes"


async def test_escape_leaves_the_raw_args_editor_first(tmp_workspace: Path):
    """The innermost thing Esc can close still wins, from either pane."""
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        await pilot.press("tab")
        app.action_toggle_args_mode(force_raw=True)
        await pilot.pause()
        assert app.args_raw_mode is True

        await pilot.press("escape")

        assert app.args_raw_mode is False
        assert app.focus_pane() == "variants", "closing the editor must not also leave the pane"


# ---- Ctrl+W: close the active tab ---------------------------------------


async def test_ctrl_w_closes_the_active_tab(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        _job_tab(app)
        app.action_select_tab("job-1")
        await pilot.pause()

        await pilot.press("ctrl+w")
        await pilot.pause()

        assert [t.id for t in app.tabs] == ["system"]
        assert app.active_tab_id == "system"


async def test_a_bare_w_closes_nothing(tmp_workspace: Path):
    """The letter is unbound, not rebound: nothing else may quietly inherit a
    key that used to destroy a tab."""
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        _job_tab(app)
        app.action_select_tab("job-1")
        await pilot.pause()
        before = (list(app.tabs), app.active_tab_id, app.selected_tool_id, app.filter_text)

        await pilot.press("w")
        await pilot.pause()

        assert [t.id for t in app.tabs] == ["system", "job-1"]
        assert (list(app.tabs), app.active_tab_id, app.selected_tool_id, app.filter_text) == before


async def test_shift_w_still_closes_the_finished_tabs(tmp_workspace: Path):
    """`w` going must not take `W` with it — they were separate actions and
    the capital is still the one that clears finished tabs in a sweep."""
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        _job_tab(app, "job-1", status="done")
        _job_tab(app, "job-2", status="failed")
        await pilot.pause()

        await pilot.press("W")
        await pilot.pause()

        assert [t.id for t in app.tabs] == ["system"]


async def test_ctrl_w_cannot_close_the_system_tab(tmp_workspace: Path):
    """It holds the harness transcript and has no × for the same reason."""
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        _job_tab(app)
        app.action_select_tab("system")
        await pilot.pause()

        await pilot.press("ctrl+w")
        await pilot.pause()

        assert [t.id for t in app.tabs] == ["system", "job-1"]


async def test_ctrl_w_deletes_a_word_while_typing_rather_than_closing_a_tab(tmp_workspace: Path):
    """`Input` binds ctrl+w to delete-word-left and is asked before the app.

    That ordering is the whole reason this binding is safe to add: filtering
    for a recipe must not shut the tab behind the filter box.
    """
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        _job_tab(app)
        await pilot.pause()
        app.action_focus_filter()
        await pilot.pause()
        box = app.query_one("#filter-input", Input)
        box.value = "ping quick"
        box.cursor_position = len(box.value)

        await pilot.press("ctrl+w")
        await pilot.pause()

        assert box.value == "ping "
        assert [t.id for t in app.tabs] == ["system", "job-1"], "a tab closed under the filter box"


async def test_closing_a_tab_whose_job_is_running_still_asks_first(tmp_workspace: Path):
    """Closing is one action however it is reached — the key, the tab's ×, or
    the palette — so a running job is asked about rather than taken down."""
    app = _app(tmp_workspace)
    async with app.run_test(size=SPLIT) as pilot:
        _job_tab(app, status="active")
        app.jobs["1"] = ActiveJob(
            id="01", recipe_id="ping", name="ping/sweep #01",
            log_path=tmp_workspace / "ping.log", variant_id="sweep", command="ping",
        )
        assert app.jobs["1"].running, "a job with no exit code is a running one"
        app.action_select_tab("job-1")
        await pilot.pause()

        await pilot.press("ctrl+w")
        await pilot.pause()

        assert [t.id for t in app.tabs] == ["system", "job-1"], "the tab went without a prompt"
        assert app.screen is not app.screen_stack[0], "no kill / detach screen was raised"
