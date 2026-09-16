"""A pane that fails to repaint degrades — but says so.

The harness deliberately survives a bad repaint rather than taking a running
scan down with it. What it must not do is survive *silently*: an ARGS band that
quietly keeps the previous recipe's flags, with Enter still armed, is the one
failure that costs an operator a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession


@pytest.mark.asyncio
async def test_a_missing_widget_is_not_worth_a_line(tmp_workspace: Path):
    """Normal: a pane queried before it is mounted, or after its tab went."""
    from textual.css.query import NoMatches

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test():
        before = len(app.system_log_lines)
        with app._repaint("test pane"):
            raise NoMatches("#nothing")
        assert len(app.system_log_lines) == before


@pytest.mark.asyncio
async def test_a_real_failure_reaches_the_transcript(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test():
        with app._repaint("ARGS band"):
            raise ValueError("token layout went wrong")

        assert any(
            "[ui] ARGS band failed" in line and "ValueError" in line
            and "token layout went wrong" in line
            for line in app.system_log_lines
        )


@pytest.mark.asyncio
async def test_the_app_keeps_running_through_a_bad_repaint(tmp_workspace: Path, monkeypatch):
    """The point of the guard: a broken pane must not end the session."""
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        monkeypatch.setattr(
            app, "resolve_flags",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        app._refresh_args_band()                 # must not raise
        await pilot.pause()

        assert app.is_running
        assert any("[ui] ARGS band failed" in line for line in app.system_log_lines)
