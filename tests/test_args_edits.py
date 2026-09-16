"""A TUI args edit is a template: it follows the scope, never bakes it in."""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.state import TargetSession

NOOP_TOOL = {"id": "true", "bin": "true"}
NOOP_PRESET = {"id": "noop", "flags": ""}


@pytest.mark.asyncio
async def test_args_edit_follows_a_later_scope_change(tmp_workspace: Path):
    from fieldlog.app import FieldlogApp

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test():
        app.flag_edits["true/noop"] = "-x $TARGET"
        app.session.target = "10.0.0.2"          # T → scope, after the edit
        plan = plan_launch(
            app.session, NOOP_TOOL, NOOP_PRESET,
            flags_override=app.flag_edits["true/noop"],
        )
    assert plan.command == "true -x 10.0.0.2"


@pytest.mark.asyncio
async def test_raw_editor_holds_the_template_not_the_resolved_text(tmp_workspace: Path):
    from fieldlog.app import ArgsTextArea, FieldlogApp

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._recipes = [{"id": "true", "bin": "true", "presets": [{"id": "noop", "flags": "-x $TARGET"}]}]
        app.selected_tool_id, app.selected_preset_id = "true", "noop"
        app._refresh_args_band()
        await pilot.pause()

        assert app.current_flags()[3] == "-x $TARGET"
        assert app.query_one("#args-raw-area", ArgsTextArea).text == "-x $TARGET"


def test_override_outdir_resolves_to_the_reserved_run_dir(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, NOOP_TOOL, NOOP_PRESET, flags_override="-w $OUTDIR/a.txt")

    assert plan.command == f"true -w {plan.job.out_dir}/a.txt"
    assert plan.job.out_dir.is_dir()


# ---- The gate reads the edit, not the preset -------------------------------


def test_an_edit_that_needs_a_scope_value_blocks_the_recipe():
    """The preset's own flags name no `$HOST`; the edit does. Judging the preset
    would let it launch with `$HOST` resolving to nothing."""
    from fieldlog.recipes import is_blocked

    tool = {"id": "true", "bin": "true"}
    preset = {"id": "p", "flags": "-c 1"}
    session = TargetSession(target="10.0.0.1")          # no dns name

    assert is_blocked(tool, preset, session)[0] is False
    blocked, reason = is_blocked(tool, preset, session, flags="-c 1 $HOST")
    assert blocked and "needs a dns name" in reason
    # And with one set it is runnable again.
    assert is_blocked(tool, preset, TargetSession(target="10.0.0.1", hostname="box.htb"),
                      flags="-c 1 $HOST")[0] is False


def test_an_edit_cannot_smuggle_an_unchecked_scope_value():
    """An unsafe target is refused once the edit is what brings `$TARGET` in."""
    from fieldlog.recipes import is_blocked

    tool = {"id": "true", "bin": "true"}
    preset = {"id": "p", "flags": "-c 1"}
    session = TargetSession(target="10.0.0.1; id")

    assert is_blocked(tool, preset, session)[0] is False        # preset never uses $TARGET
    blocked, reason = is_blocked(tool, preset, session, flags="-c 1 $TARGET")
    # Every distinct offender is named — here the `;` and the space after it.
    assert blocked and "target has unsafe characters (; )" in reason
