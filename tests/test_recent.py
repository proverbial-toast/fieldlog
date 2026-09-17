"""Recent is the last six things launched, not the first six ever launched."""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession

TOOLS = [
    {"id": "one", "bin": "true", "name": "first", "presets": [{"id": "a", "name": "a", "flags": ""}]},
    {"id": "two", "bin": "true", "name": "second", "presets": [{"id": "b", "name": "b", "flags": ""}]},
]


def _app(workspace: Path) -> FieldlogApp:
    # No run_test: _remember reads and writes app state and the workspace file,
    # and touches no widget.
    return FieldlogApp(TargetSession(workspace_dir=workspace))


def test_relaunching_something_moves_it_back_to_the_front(tmp_workspace: Path):
    app = _app(tmp_workspace)
    for key in ("a", "b", "a"):
        app._remember(key)
    assert app.recent == ["a", "b"]


def test_only_the_last_six_are_kept(tmp_workspace: Path):
    app = _app(tmp_workspace)
    for key in "abcdefg":
        app._remember(key)
    assert app.recent == ["g", "f", "e", "d", "c", "b"]


@pytest.mark.asyncio
async def test_the_cursor_keeps_its_recipe_when_recent_re_orders(tmp_workspace: Path):
    """Recent re-orders under the cursor. Holding the index instead of the row
    leaves the cursor on whichever recipe slid into that line."""
    app = _app(tmp_workspace)
    app._recipes = TOOLS

    async with app.run_test():
        app._remember("one/a")
        app._remember("two/b")                      # recent: two/b, one/a

        # The second Recent row — the recipe the operator put the cursor on.
        second = next(
            i for i, r in enumerate(app._rows)
            if r.kind == "entry" and r.preset_id == "a"
        )
        app.cursor = second

        # Launching it again moves it to the front of Recent.
        app._remember("one/a")

        assert app._rows[app.cursor].preset_id == "a"
        assert app.cursor != second                 # the row moved; the cursor went with it
