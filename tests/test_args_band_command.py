"""The ARGS band reads as the command the launcher runs, word for word.

It printed `$ <tool bin>` and then the flags, always. format_command puts
nothing in front of flags that already start with the binary or with a wrapper,
so 18 shipped recipes read `$ openssl openssl s_client …`, `$ ping timeout 60 …`
or `$ tcpdump sudo …`; and a variant with its own `bin` was shown under its
tool's (`dig/vtrace` runs delv, `ping/v6` runs ping6 on darwin). What ran was
right. The band misstated it.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static

from fieldlog.app import FieldlogApp
from fieldlog.launch import plan_launch
from fieldlog.state import TargetSession

TOOL = {
    "id": "alpha", "bin": "true", "name": "Alpha",
    "presets": [
        {"id": "plain", "flags": "-c 4 $TARGET"},
        {"id": "itself", "flags": "true -c 4 $TARGET"},
        {"id": "wrapped", "flags": "timeout 5 sh -c 'true $TARGET'"},
        {"id": "own-bin", "bin": "false", "flags": "-c 4 $TARGET"},
    ],
}
CHAIN = {"id": "c", "name": "one step", "steps": [{"recipe": "alpha/itself", "continue": False}]}


def _app(workspace: Path) -> FieldlogApp:
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=workspace))
    app.catalog.tools = [TOOL]
    app.catalog.chains = [CHAIN]
    app._recipes = [TOOL]
    return app


def _label(app: FieldlogApp) -> str:
    return str(app.query_one("#args-bin-label", Static).render()).strip()


def _band(app: FieldlogApp) -> str:
    """The label and the tokens, read left to right as one line."""
    tokens = " ".join(str(w.render()) for w in app.query("#args-tokens-wrap .arg-pair"))
    return f"{_label(app)} {tokens}".strip()


async def _show(app: FieldlogApp, pilot, preset_id: str) -> None:
    app.selected_chain_id = None
    app.selected_tool_id, app.selected_preset_id = "alpha", preset_id
    app._refresh_args_band()
    await pilot.pause()


async def test_the_band_reads_as_the_launched_command(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        for preset in TOOL["presets"]:
            await _show(app, pilot, preset["id"])
            ran = plan_launch(app.session, TOOL, preset, dry_run=True).command
            assert _band(app) == f"$ {ran}", preset["id"]
        await _show(app, pilot, "wrapped")
        assert _label(app) == "$ timeout", "the program that runs first is the one named"


async def test_beside_the_raw_editor_the_label_is_only_what_goes_in_front(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        await _show(app, pilot, "itself")
        app.action_toggle_args_mode(force_raw=True)
        await pilot.pause()
        assert _label(app) == "$", "the template already starts with `true`"

        app.action_toggle_args_mode(force_raw=False)
        await _show(app, pilot, "plain")
        app.action_toggle_args_mode(force_raw=True)
        await pilot.pause()
        assert _label(app) == "$ true"


async def test_a_chain_step_reads_as_the_command_it_runs(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.selected_chain_id, app.chain_step = "c", 0
        app._refresh_variants()
        app._refresh_args_band()
        await pilot.pause()
        assert _band(app) == "$ true -c 4 10.0.0.1"
        tips = [str(w.tooltip) for w in app.query("#variant-list .variant-row") if w.tooltip]
        assert tips == ["step 1 of 1 · true -c 4 10.0.0.1"]
