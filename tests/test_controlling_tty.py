"""A job owns its pty as a controlling terminal, so /dev/tty prompts work."""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import BLOCK_GRACE, interrupt_job, run_job, send_stdin
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


def _session(tmp_workspace: Path) -> TargetSession:
    return TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)


def _preset(script: str) -> dict:
    return {"id": "tty", "flags": f"-c {shlex.quote(script)}"}


@pytest.mark.asyncio
async def test_write_to_dev_tty_reaches_the_log(tmp_workspace: Path):
    session = _session(tmp_workspace)
    plan = plan_launch(session, SH_TOOL, _preset("echo probe > /dev/tty"))

    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)

    assert code == 0
    assert "probe" in plan.job.log_path.read_text()


@pytest.mark.asyncio
async def test_prompt_read_from_dev_tty_is_answerable(tmp_workspace: Path):
    """The sudo / ssh shape: the prompt reads the terminal, not stdin."""
    session = _session(tmp_workspace)
    plan = plan_launch(
        session, SH_TOOL, _preset('printf "pw: " > /dev/tty; read -r x < /dev/tty; echo "got:$x"')
    )

    task = asyncio.create_task(
        run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    )
    deadline = time.time() + 2.0
    while not plan.job.awaiting and time.time() < deadline:
        await asyncio.sleep(BLOCK_GRACE / 4)
    assert plan.job.awaiting, "job never reported a blocked prompt"

    assert send_stdin(plan.job, "hello")
    code = await asyncio.wait_for(task, timeout=5)

    assert code == 0
    assert "got:hello" in plan.job.log_path.read_text()


@pytest.mark.asyncio
async def test_child_is_still_its_own_session_leader(tmp_workspace: Path):
    """killpg(pid) stays valid: setsid in preexec replaces start_new_session."""
    session = _session(tmp_workspace)
    plan = plan_launch(session, SH_TOOL, _preset("ps -o sid= -o pgid= -p $$"))

    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)

    assert code == 0
    sid, pgid = plan.job.log_path.read_text().split()
    # The spawned shell leads both its session and its group, so killpg(job.pid)
    # in interrupt_job / kill_job still reaches the whole run.
    assert int(sid) == int(pgid) == plan.job.pid


@pytest.mark.asyncio
async def test_interrupt_still_stops_the_group(tmp_workspace: Path):
    session = _session(tmp_workspace)
    plan = plan_launch(session, SH_TOOL, _preset("sleep 5"))

    task = asyncio.create_task(
        run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    )
    await asyncio.sleep(0.3)  # let the shell exec sleep before signalling
    assert interrupt_job(plan.job)

    started = time.time()
    code = await asyncio.wait_for(task, timeout=5)
    assert time.time() - started < 2.0
    assert code != 0
