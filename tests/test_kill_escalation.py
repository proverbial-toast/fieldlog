"""A kill the operator asked for is not dropped because the app exited first.

`kill_job` sends SIGINT and schedules SIGKILL for after the grace. If the app
quits inside that grace, the worker is cancelled and the loop that would have
delivered the SIGKILL goes down with it. The cancellation path hangs up the
pty and sends SIGTERM, which is the end of most tools; one that ignores those
too used to survive the kill it was given.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import kill_job, run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


@pytest.mark.asyncio
async def test_a_kill_cut_short_by_exit_still_kills(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    # A tool that ignores every polite signal, the pty hangup included — a
    # daemon, in effect. sleep inherits the ignored signals from sh.
    plan = plan_launch(session, SH_TOOL, {"id": "t", "flags": "-c 'trap \"\" INT TERM HUP; sleep 30'"})
    task = asyncio.create_task(run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env))
    while plan.job.process is None:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)                    # let sh install the trap
    proc = plan.job.process
    try:
        assert kill_job(plan.job, grace=10.0)
        task.cancel()                           # the app going down inside the grace
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        assert proc.returncode == -signal.SIGKILL
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
