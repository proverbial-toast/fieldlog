"""Output is written in batches, and still arrives as it happens.

The run log used to be line-buffered and the CLI flushed stdout after every
line: two write calls per line, which was most of the CPU of a run that
printed a million. The log is now flushed whenever the runner waits for the
tool, and the CLI writes once per turn of its loop. What must not change is
what a reader outside sees: a line the tool printed before going quiet is on
disk and through the pipe while the tool is still running.
"""

from __future__ import annotations

import asyncio
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from fieldlog.launch import plan_launch
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


async def _log_while_running(tmp_workspace: Path, script: str, ready) -> str:
    """The log's text at the moment `ready(job)` first holds, the tool still running."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH_TOOL, {"id": "t", "flags": f"-c {script!r}"})
    task = asyncio.create_task(
        run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    )
    deadline = time.monotonic() + 10
    while not ready(plan.job):
        assert time.monotonic() < deadline, "the job never got there"
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    seen = plan.job.log_path.read_text()
    assert plan.job.running
    plan.job.process.kill()
    await task
    return seen


async def test_a_line_before_a_pause_is_on_disk_during_the_pause(tmp_workspace: Path):
    seen = await _log_while_running(
        tmp_workspace, "echo first; echo second; sleep 5", lambda job: job.lines_count >= 2,
    )
    assert seen == "first\nsecond\n"


async def test_a_prompt_is_on_disk_while_it_waits(tmp_workspace: Path):
    seen = await _log_while_running(
        tmp_workspace, "printf 'Password: '; sleep 5", lambda job: job.await_prompt is not None,
    )
    assert seen == "Password: \n"


DROPIN = """recipes:
  - id: s
    bin: sh
    presets:
      - id: slow
        flags: "-c 'echo first; sleep 5; echo last'"
"""


def test_piped_output_arrives_before_the_tool_ends(tmp_path: Path):
    (tmp_path / "recipes.d").mkdir()
    (tmp_path / "recipes.d" / "s.yaml").write_text(DROPIN)
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", "run", "s/slow", "10.0.0.1", "-q"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
    )
    try:
        # The tool sleeps 5 s after its first line; the line must not wait for it.
        ready, _, _ = select.select([proc.stdout], [], [], 4)
        assert ready, "no output while the tool was still running"
        assert os.read(proc.stdout.fileno(), 4096).decode() == "first\n"
    finally:
        proc.kill()
        proc.communicate(timeout=30)
