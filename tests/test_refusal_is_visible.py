"""A refused launch says why, on the button and in the System tab.

Reported as "the enter button is missing on the variants ... I couldn't
execute them": four of the six shipped chains cannot run on a stock box, and
the TUI's whole answer was the word `Not runnable` on a button, with Enter
doing nothing at all and writing nothing anywhere. The verdict knew precisely
which step of which chain wanted which binary and dropped it.

The button has about twenty cells at the narrowest split, so it carries the
short form and the full reason rides in its tooltip; the System tab gets the
reason whole, because that is the surface that keeps a history. The width
assertion is deliberate: a longer label overflows the VARIANTS header and
pushes `[P] Pin` off the end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession

# The narrowest split layout, where the header budget is tightest.
NARROW = (120, 40)
BUTTON_BUDGET = 20


def _app(workspace: Path, **scope) -> FieldlogApp:
    session = TargetSession(**scope)
    session.workspace_dir = workspace
    return FieldlogApp(session)


def _blocked_chain(app: FieldlogApp):
    for chain in app.chains:
        if app.chain_blocked_flag(chain):
            return chain
    return None


@pytest.mark.parametrize(
    "scope, tool_id, expected",
    [
        (dict(target="10.0.0.1", hostname="e.com"), "arp-scan", "not installed"),
        (dict(target="", hostname="e.com"), "ping", "needs target"),
        (dict(target="10.0.0.1; id", hostname=""), "ping", "bad target"),
    ],
)
async def test_blocked_recipe_button_says_why(tmp_workspace: Path, scope, tool_id, expected):
    app = _app(tmp_workspace, **scope)
    async with app.run_test(size=NARROW) as pilot:
        app.select_tool(tool_id)
        await pilot.pause()
        btn = app.query_one("#btn-run")
        label = str(btn.render())
        assert label == f"✗ {expected}"
        assert len(label) <= BUTTON_BUDGET, "a longer label pushes [P] Pin out of the header"
        assert btn.has_class("-disabled")
        assert btn.tooltip, "the full reason belongs on the tooltip"
        assert expected.split()[-1] in btn.tooltip or "not found" in btn.tooltip


async def test_runnable_recipe_button_offers_the_run(tmp_workspace: Path):
    app = _app(tmp_workspace, target="10.0.0.1", hostname="e.com")
    async with app.run_test(size=NARROW) as pilot:
        app.select_tool("ping")
        await pilot.pause()
        btn = app.query_one("#btn-run")
        assert str(btn.render()) == "[Enter] Run"
        assert not btn.has_class("-disabled")
        assert btn.tooltip is None, "a stale refusal must not linger on a runnable recipe"


async def test_enter_on_a_blocked_recipe_writes_the_reason_to_the_system_tab(tmp_workspace: Path):
    app = _app(tmp_workspace, target="", hostname="")
    async with app.run_test(size=NARROW) as pilot:
        app.select_tool("ping")
        await pilot.pause()
        before = len(app.system_log_lines)
        app.kbd_pane = "variants"
        await pilot.press("enter")
        await pilot.pause()

        written = [ln for ln in app.system_log_lines[before:] if ln.startswith("[blocked]")]
        assert len(written) == 1, "Enter must answer, once"
        assert "ping/" in written[0] and "needs a target" in written[0]
        assert not app.jobs, "a refused launch spawns nothing"


async def test_blocked_chain_names_the_step_it_stopped_at(tmp_workspace: Path):
    """A chain's reason is the step's own, which is the part worth reading."""
    app = _app(tmp_workspace, target="10.0.0.1", hostname="")
    async with app.run_test(size=NARROW) as pilot:
        chain = _blocked_chain(app)
        if chain is None:
            pytest.skip("every catalogued chain is runnable on this box")
        app.select_chain(chain["id"])
        await pilot.pause()

        btn = app.query_one("#btn-run")
        label = str(btn.render())
        assert label.startswith("✗ ")
        assert len(label) <= BUTTON_BUDGET
        assert btn.tooltip.startswith("step "), btn.tooltip

        before = len(app.system_log_lines)
        app.kbd_pane = "variants"
        await pilot.press("enter")
        await pilot.pause()
        written = [ln for ln in app.system_log_lines[before:] if ln.startswith("[blocked]")]
        assert len(written) == 1
        assert f"chain {chain['id']}" in written[0]
        assert "step " in written[0], "the step is the reason a chain refuses"
        assert not app.jobs


async def test_the_refusal_label_fits_beside_the_pin_button(tmp_workspace: Path):
    """The header is title + crumb + run + pin; only the crumb can give ground."""
    app = _app(tmp_workspace, target="", hostname="")
    async with app.run_test(size=NARROW) as pilot:
        app.select_tool("ping")
        await pilot.pause()
        header = app.query_one("#variants-header")
        title = app.query_one("#variants-title")
        btn = app.query_one("#btn-run")
        pin = app.query_one("#btn-pin")
        used = title.size.width + btn.size.width + pin.size.width
        assert used <= header.size.width, (
            f"header {header.size.width} cells, widgets want {used}"
        )
