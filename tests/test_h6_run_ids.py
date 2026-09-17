"""H6: run numbers never collide, in the TUI or across processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOOP_TOOL = {"id": "true", "bin": "true"}
NOOP_PRESET = {"id": "noop", "flags": ""}


def test_reserve_continues_past_archive_and_issued_numbers(tmp_path: Path):
    from fieldlog.archive import next_run_number, reserve_run_number

    target_dir = tmp_path / "10.0.0.1"
    target_dir.mkdir()
    (target_dir / "session.json").write_text(json.dumps([{"id": "01"}, {"id": "03"}]))

    assert next_run_number(target_dir) == 4
    assert next_run_number(target_dir) == 4  # a preview claims nothing
    assert reserve_run_number(target_dir) == 4
    # #04 is issued but not yet in session.json (still running): never reissued.
    assert reserve_run_number(target_dir) == 5
    assert next_run_number(target_dir) == 6


def test_dry_run_previews_without_reserving(tmp_workspace: Path):
    from fieldlog.launch import plan_launch
    from fieldlog.state import TargetSession

    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    first = plan_launch(session, NOOP_TOOL, NOOP_PRESET, dry_run=True)
    second = plan_launch(session, NOOP_TOOL, NOOP_PRESET, dry_run=True)
    assert first.job.id == second.job.id == "01"
    assert not session.target_dir.exists()


_RACER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from fieldlog.launch import plan_launch
    from fieldlog.runner import _append_manifest
    from fieldlog.archive import ArtifactDelta
    from fieldlog.state import TargetSession

    workspace, start, count = Path(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
    session = TargetSession(target="10.0.0.1", workspace_dir=workspace)
    tool, preset = {"id": "true", "bin": "true"}, {"id": "noop", "flags": ""}
    time.sleep(max(0.0, start - time.time()))
    for _ in range(count):
        plan = plan_launch(session, tool, preset)
        _append_manifest(session, plan.job, plan.command, plan.env, 0.0, 0.0, 0, ArtifactDelta([], 0, 0, 0))
    """
)


def test_concurrent_processes_get_unique_ids_and_keep_every_record(tmp_workspace: Path):
    """Separate processes (a CLI run beside the TUI) launching against one target."""
    procs, per_proc = 6, 15
    start = time.time() + 1.0
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    racers = [
        subprocess.Popen([sys.executable, "-c", _RACER, str(tmp_workspace), str(start), str(per_proc)], env=env)
        for _ in range(procs)
    ]
    assert [r.wait(timeout=60) for r in racers] == [0] * procs

    runs = json.loads((tmp_workspace / "10.0.0.1" / "session.json").read_text())
    assert sorted(int(r["id"]) for r in runs) == list(range(1, procs * per_proc + 1))


@pytest.mark.asyncio
async def test_tui_tabs_stay_unique_when_run_numbers_restart(tmp_workspace: Path):
    """A new target restarts run numbers at #01 while the old #01 tab is still open."""
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._spawn_job(NOOP_TOOL, NOOP_PRESET, "true/noop")
        await app.workers.wait_for_complete()
        await pilot.pause()

        app.session.target = "10.0.0.2"
        app.resume_session()
        app._spawn_job(NOOP_TOOL, NOOP_PRESET, "true/noop")
        await app.workers.wait_for_complete()
        await pilot.pause()

        job_tabs = [t for t in app.tabs if t.id != "system"]
        assert [t.label for t in job_tabs] == ["true/noop #01", "true/noop #01"]
        assert len({t.id for t in job_tabs}) == 2
        assert len(app.jobs) == 2
        # Each job's log sits under the target folder it was spawned for.
        assert {app.jobs[t.job_id].log_path.parent.parent.name for t in job_tabs} == {"10.0.0.1", "10.0.0.2"}
