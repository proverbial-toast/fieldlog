"""The status band is repainted, not rebuilt.

A running job redraws it once a second and every System transcript line redraws
it again. Only the numbers move, so the cells stay put: the same Static widgets
are updated in place, and the row is remounted only when the band changes shape
(a different tab, an item dropped for width).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

import pytest
from textual.containers import Horizontal
from textual.widgets import Static

from fieldlog.app import FieldlogApp
from fieldlog.state import ActiveJob, TargetSession
from fieldlog.tui.models import TabDescriptor


def _cells(app: FieldlogApp) -> List[Static]:
    return list(app.query_one("#status-items", Horizontal).query(Static))


def _text(cell: Static) -> str:
    return cell.content.plain            # the Text last handed to Static.update


def _running_job(workspace: Path) -> ActiveJob:
    """A job with no process behind it: exit_code stays None, so it is running."""
    return ActiveJob(
        id="01", recipe_id="x", name="x #01", log_path=workspace / "x.log",
        start_time=time.time(),
    )


@pytest.mark.asyncio
async def test_a_second_refresh_reuses_the_cells(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    # size: wide enough that the width budget never drops a cell.
    async with app.run_test(size=(160, 40)) as pilot:
        job = _running_job(tmp_workspace)
        app.jobs["7"] = job
        app.tabs.append(TabDescriptor(
            id="job-7", label="x #01", status="active", tool_id="x", job_id="7",
        ))
        app.active_tab_id = "job-7"

        app._refresh_status_band()               # shape changed: rebuilt
        await pilot.pause()
        first = _cells(app)
        assert first                             # state, exit, elapsed, lines, bytes

        app._refresh_status_band()
        await pilot.pause()
        second = _cells(app)
        assert len(second) == len(first)
        assert all(a is b for a, b in zip(first, second))

        lines_cell = next(c for c in first if _text(c).startswith("LINES"))
        job.lines_count = 42
        app._refresh_status_band()
        await pilot.pause()
        assert all(a is b for a, b in zip(first, _cells(app)))
        assert _text(lines_cell).endswith("42")  # the same widget, a new number


@pytest.mark.asyncio
async def test_a_different_tab_rebuilds_the_band(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test(size=(160, 40)) as pilot:
        app.jobs["7"] = _running_job(tmp_workspace)
        app.tabs.append(TabDescriptor(
            id="job-7", label="x #01", status="active", tool_id="x", job_id="7",
        ))
        app.active_tab_id = "job-7"
        app._refresh_status_band()
        await pilot.pause()
        assert len(_cells(app)) == 5

        app.active_tab_id = "system"
        app._refresh_status_band()
        await pilot.pause()

        labels = [_text(c).split("\n")[0] for c in _cells(app)]
        assert labels == ["STATE", "RECIPES", "LINES"]
