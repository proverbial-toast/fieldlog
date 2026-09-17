"""The runner's "awaiting input" flag is a timeout on a partial line, and it
has to clear again the moment the tool writes more — whatever the partial
line was.

A partial line of only whitespace (an indent printed ahead of a slow value)
is stripped to '' as the prompt, and a truthiness test on it would never see
the resume: the job would read as blocked until it exited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


async def _transitions(tmp_workspace: Path, script: str) -> list:
    """`job.awaiting` at each on_state call for one sh script."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH_TOOL, {"id": "t", "flags": f"-c {script!r}"})
    seen: list = []
    code = await run_job(
        plan.command, plan.job, session, lambda t, s: None,
        on_state=lambda: seen.append(plan.job.awaiting), env=plan.env,
    )
    assert code == 0
    return seen


@pytest.mark.asyncio
async def test_a_pause_on_a_partial_line_blocks_then_resumes(tmp_workspace: Path):
    seen = await _transitions(tmp_workspace, "printf 'working...'; sleep 0.7; echo done")
    assert seen == [True, False]


@pytest.mark.asyncio
async def test_a_whitespace_only_pause_resumes_like_any_other(tmp_workspace: Path):
    seen = await _transitions(tmp_workspace, "printf '    '; sleep 0.7; echo done")
    assert seen == [True, False]
