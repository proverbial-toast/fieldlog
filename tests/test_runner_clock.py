"""Verify session manifest records use naive local ISO 8601 timestamps."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


@pytest.mark.asyncio
async def test_session_records_use_naive_localtime(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    preset = {"id": "echo", "flags": "-c 'echo hello'"}
    plan = plan_launch(session, SH_TOOL, preset)

    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    assert code == 0

    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    start_time = record["start_time"]
    end_time = record["end_time"]

    # Must not contain timezone offset or Z
    assert "+" not in start_time and "Z" not in start_time
    assert "+" not in end_time and "Z" not in end_time
    # Must be valid ISO format with date and time
    assert "T" in start_time and "T" in end_time
