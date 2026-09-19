"""`R` reloads the catalog from disk without disturbing what is on screen.

The reload path had no test at all, though it is the one action that can pull
the operator's selection out from under them: it re-reads every drop-in, so a
recipe can be renamed, gain a variant or disappear between one keypress and
the next. Its own fixup for that used to jump to the *first tool in the file*
whenever the selected `tool/preset` key went missing, so renaming one variant
of one tool threw the operator to the top of the catalog. It now settles the
selection the way every pane does, through `selected_recipe`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession

TWO_TOOLS = """\
recipes:
  - id: aaa
    name: First
    bin: true
    presets:
      - id: p1
        name: p one
        flags: ""
  - id: zzz
    name: Last
    bin: true
    presets:
      - id: keep
        name: keeper
        flags: ""
      - id: {second}
        name: second
        flags: ""
"""


@pytest.fixture
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """An empty config dir and a cwd whose `recipes.d` is ours alone.

    The base catalog still loads; these drop-ins are what the test rewrites.
    """
    from fieldlog import recipes as recipes_mod

    config_dir = tmp_path / "config" / "recipes.d"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(recipes_mod, "DROPIN_DIR", config_dir)

    work = tmp_path / "work"
    local = work / "recipes.d"
    local.mkdir(parents=True)
    (local / "under-test.yaml").write_text(TWO_TOOLS.format(second="two"), encoding="utf-8")
    monkeypatch.chdir(work)
    return local


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1")
    session.workspace_dir = workspace
    return FieldlogApp(session)


async def test_a_renamed_variant_keeps_the_operator_on_its_tool(
    tmp_workspace: Path, catalog_dir: Path
):
    """`zzz/two` becomes `zzz/renamed`. The tool is still there, so the
    selection stays on it and takes its first variant — it does not fall back
    to whatever recipe happens to sort first in the whole catalog."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.selected_chain_id = None
        app.selected_tool_id, app.selected_preset_id = "zzz", "two"
        assert app.selected_recipe()[1]["id"] == "two", "fixture did not load"

        (catalog_dir / "under-test.yaml").write_text(
            TWO_TOOLS.format(second="renamed"), encoding="utf-8"
        )
        app.action_reload_recipes()
        await pilot.pause()

        assert app.selected_tool_id == "zzz", "a renamed variant moved the operator to another tool"
        assert app.selected_preset_id == "keep"
        assert app._row_shows(app._rows[app.cursor])


async def test_a_tool_that_the_reload_removes_gives_up_the_selection(
    tmp_workspace: Path, catalog_dir: Path
):
    """With the whole tool gone there is nothing to hold on to, and the panes
    must land on something real rather than paint a recipe that no longer
    exists."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.selected_tool_id, app.selected_preset_id = "zzz", "keep"
        app.selected_recipe()

        (catalog_dir / "under-test.yaml").write_text(
            "recipes:\n  - id: aaa\n    bin: true\n    flags: \"\"\n", encoding="utf-8"
        )
        app.action_reload_recipes()
        await pilot.pause()

        assert app.get_tool("zzz") is None, "the reload did not take"
        assert app.get_tool(app.selected_tool_id) is not None
        assert app._row_shows(app._rows[app.cursor])


async def test_a_reload_that_adds_a_variant_says_so(tmp_workspace: Path, catalog_dir: Path):
    """The System tab is where a reload is accounted for; a new variant is the
    one thing an operator reloads to see."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        (catalog_dir / "under-test.yaml").write_text(
            TWO_TOOLS.format(second="two") + "      - id: three\n        name: third\n        flags: \"\"\n",
            encoding="utf-8",
        )
        app.action_reload_recipes()
        await pilot.pause()

        assert app.added_variants == ["zzz/three"]
        assert any("+1 new (zzz/three)" in line for line in app.system_log_lines)
        assert app.get_preset(app.get_tool("zzz"), "three")["id"] == "three"


async def test_a_reload_that_breaks_a_drop_in_keeps_the_rest_of_the_catalog(
    tmp_workspace: Path, catalog_dir: Path
):
    """One unparseable operator file must not empty the tree under a running
    TUI — the catalog fails soft, and the reload has to inherit that."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        before = len(app.recipes)
        assert before > 1

        (catalog_dir / "under-test.yaml").write_text("recipes: [ {", encoding="utf-8")
        app.action_reload_recipes()
        await pilot.pause()

        assert app.recipes, "a bad drop-in emptied the catalog"
        assert any("invalid" in e for e in app.catalog.errors)
        assert app._row_shows(app._rows[app.cursor])
