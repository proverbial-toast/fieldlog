"""Who gets the keyboard when a job goes quiet.

The runner calls a job "awaiting input" after a partial line and 0.4s of
silence — a timeout, not a prompt parser. The bar goes up for every such block,
because the operator should see it; the keyboard only moves when the text reads
like a question or the block has outlasted STDIN_FOCUS_AFTER. A tool that
prints `working...` and thinks must not swallow the next hotkey.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import ActiveJob, TargetSession
from fieldlog.tui.models import TabDescriptor
from fieldlog.tui.widgets import StdinInput


def _app(workspace: Path) -> FieldlogApp:
    return FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=workspace))


def _awaiting_job(workspace: Path, prompt: str, since: Optional[float] = None) -> ActiveJob:
    """A job blocked on `prompt` without a process behind it: exit_code stays
    None, so `running` and `awaiting` are both true."""
    return ActiveJob(
        id="01",
        recipe_id="x",
        name="x #01",
        log_path=workspace / "x.log",
        await_prompt=prompt,
        await_since=time.time() if since is None else since,
    )


def _attach(app: FieldlogApp, job: ActiveJob, key: str = "7", active: bool = True) -> str:
    """Register `job` under the process-wide job key `key` and give it a tab."""
    tab_id = f"job-{key}"
    app.jobs[key] = job
    app.tabs.append(TabDescriptor(
        id=tab_id, label=job.name, status="active", tool_id="x", job_id=key,
    ))
    if active:
        app.active_tab_id = tab_id
    return tab_id


def _focused_on_stdin(app: FieldlogApp) -> bool:
    return isinstance(app.focused, StdinInput)


def _bar_is_up(app: FieldlogApp) -> bool:
    from textual.containers import Vertical

    return not app.query_one("#stdin-bar", Vertical).has_class("hidden")


@pytest.mark.asyncio
async def test_a_tool_that_paused_raises_the_bar_but_keeps_its_hands_off(tmp_workspace: Path):
    app = _app(tmp_workspace)
    # size: wide enough that the status band never drops a cell for width.
    async with app.run_test(size=(160, 40)) as pilot:
        job = _awaiting_job(tmp_workspace, "working...")
        _attach(app, job)
        job.await_since = time.time()            # the block began just now, whatever setup cost
        app._refresh_stdin_bar()
        await pilot.pause()

        assert _bar_is_up(app)                   # the operator still sees the block
        assert not _focused_on_stdin(app)        # but keeps the keyboard


@pytest.mark.asyncio
async def test_a_coloured_prompt_still_reads_as_one(tmp_workspace: Path):
    """The pty hands the prompt over with its escapes on; a reset after the
    colon must not hide the colon from the shape test."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        job = _awaiting_job(tmp_workspace, "\x1b[1mPassword:\x1b[0m")
        _attach(app, job)
        job.await_since = time.time()            # only the shape can earn focus here
        app._refresh_stdin_bar()
        await pilot.pause()

        assert _focused_on_stdin(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["Password:", "Continue? [y/N]", "(yes/no)"])
async def test_something_that_reads_like_a_prompt_takes_focus(tmp_workspace: Path, prompt: str):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        _attach(app, _awaiting_job(tmp_workspace, prompt))
        app._refresh_stdin_bar()
        await pilot.pause()

        assert _focused_on_stdin(app)


@pytest.mark.asyncio
async def test_a_long_enough_block_takes_focus_whatever_it_said(tmp_workspace: Path):
    """The fallback: past STDIN_FOCUS_AFTER, a slow tool no longer explains it."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        _attach(app, _awaiting_job(tmp_workspace, "working...", since=time.time() - 2))
        app._refresh_stdin_bar()
        await pilot.pause()

        assert _focused_on_stdin(app)


@pytest.mark.asyncio
async def test_esc_keeps_the_keyboard_for_the_rest_of_the_block(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        _attach(app, _awaiting_job(tmp_workspace, "Password:"))
        app._refresh_stdin_bar()
        await pilot.pause()
        assert _focused_on_stdin(app)

        app.dismiss_stdin_focus()
        app._refresh_stdin_bar()
        await pilot.pause()

        assert not _focused_on_stdin(app)        # the next tick must not take it back
        assert _bar_is_up(app)


@pytest.mark.asyncio
async def test_one_targets_esc_does_not_silence_anothers_prompt(tmp_workspace: Path):
    """Run numbers restart per target, so two live jobs can both be #01. The
    dismissal is keyed by the job key, or the second one's prompt would inherit
    a dismissal the operator never gave it."""
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        blocked_at = time.time()
        first = _awaiting_job(tmp_workspace, "Password:", since=blocked_at)
        second = _awaiting_job(tmp_workspace, "Password:", since=blocked_at)
        assert first.id == second.id == "01"
        _attach(app, first, key="7")
        _attach(app, second, key="8", active=False)

        app._refresh_stdin_bar()
        await pilot.pause()
        app.dismiss_stdin_focus()                # Esc on the first target's #01

        app.active_tab_id = "job-8"
        app._refresh_stdin_bar()
        await pilot.pause()

        assert _focused_on_stdin(app)
