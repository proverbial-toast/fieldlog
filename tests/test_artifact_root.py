"""A --artifact-root log destination still gets its artifacts accounted for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


@pytest.mark.asyncio
async def test_artifacts_outside_the_archive_are_recorded_absolute(tmp_path: Path, tmp_workspace: Path):
    elsewhere = tmp_path / "elsewhere"
    session = TargetSession(
        target="10.0.0.1", workspace_dir=tmp_workspace, artifact_root=str(elsewhere)
    )
    preset = {"id": "write", "flags": """-c 'echo hi; echo x > "$OUTDIR/x.txt"'"""}

    plan = plan_launch(session, SH_TOOL, preset)
    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    assert code == 0

    scope_dir = elsewhere / "10.0.0.1"
    paths = [a.path for a in plan.job.artifact_delta.artifacts]
    assert paths[0] == str(plan.job.log_path)                       # primary log sorts first
    assert set(paths) == {str(plan.job.log_path), str(scope_dir / plan.job.stamp / "x.txt")}
    assert all(Path(p).is_absolute() and scope_dir in Path(p).parents for p in paths)

    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    assert len(record["artifacts"]) == 2
