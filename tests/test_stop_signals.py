"""SIGHUP and SIGTERM end a run the way Ctrl+C does, and the run is archived.

Unanswered, either signal killed fieldlog outright: the tool lost its
controlling terminal mid-write (or, ignoring SIGHUP, ran on as an orphan) and
no record reached session.json. The ssh session to a jump box dropping is the
everyday case.

Also here: an interrupt that lands before the process exists (run_job reads
the vantage first) is delivered once it does, instead of being dropped.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fieldlog import runner as runner_mod
from fieldlog.app import FieldlogApp
from fieldlog.launch import plan_launch
from fieldlog.runner import interrupt_job, run_job
from fieldlog.state import TargetSession

DROPIN = """recipes:
  - id: p
    bin: sh
    presets:
      - id: forever
        flags: "-c 'while true; do echo tick; sleep 0.1; done'"
      - id: deaf
        # ignores SIGHUP, as a nohup'd or daemon-ish tool does
        flags: "-c 'trap \\"\\" HUP; while true; do echo tock; sleep 0.1; done'"
"""


@pytest.fixture
def box(tmp_path: Path) -> Path:
    (tmp_path / "recipes.d").mkdir()
    (tmp_path / "recipes.d" / "p.yaml").write_text(DROPIN)
    return tmp_path


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _tool_pid(parent: int) -> int:
    """The tool fieldlog spawned: its only child, once it exists."""
    for _ in range(100):
        kids = subprocess.run(["pgrep", "-P", str(parent)], capture_output=True, text=True).stdout.split()
        if kids:
            return int(kids[0])
        time.sleep(0.05)
    raise AssertionError("fieldlog never spawned the tool")


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
@pytest.mark.parametrize("preset", ["p/forever", "p/deaf"])
def test_the_cli_archives_the_run_and_leaves_no_orphan(box: Path, sig, preset):
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", "run", preset, "10.0.0.1", "-q"],
        cwd=box, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    tool = _tool_pid(proc.pid)
    time.sleep(0.5)

    proc.send_signal(sig)
    proc.wait(timeout=20)
    time.sleep(0.3)

    alive = _group_alive(tool)
    if alive:
        os.killpg(tool, signal.SIGKILL)
    assert not alive, "the tool outlived fieldlog"
    record = json.loads((box / "targets" / "10.0.0.1" / "session.json").read_text())[-1]
    assert record["recipe"] == preset
    assert record["interrupted"] is True


async def test_an_interrupt_before_the_spawn_is_delivered_not_dropped(tmp_workspace: Path, monkeypatch):
    def slow_vantage(target):
        time.sleep(0.4)
        return {}
    monkeypatch.setattr(runner_mod, "vantage", slow_vantage)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, {"id": "sh", "bin": "sh"}, {"id": "t", "flags": "-c 'sleep 30'"})

    task = asyncio.create_task(run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env))
    await asyncio.sleep(0.1)
    assert plan.job.process is None                 # still reading the vantage
    assert interrupt_job(plan.job) is True          # accepted, not dropped

    started = time.monotonic()
    await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 5           # not the 30 s the tool asked for
    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    assert record["interrupted"] is True


async def test_the_tui_stops_its_jobs_keeps_their_records_and_exits(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.2", workspace_dir=tmp_workspace))
    tool = {"id": "p", "bin": "sh"}
    preset = {"id": "forever", "flags": "-c 'while true; do echo tick; sleep 0.1; done'"}
    async with app.run_test() as pilot:
        app._spawn_job(tool, preset, "p/forever")
        app._spawn_job(tool, preset, "p/forever")
        await pilot.pause(0.8)
        assert sum(job.running for job in app.jobs.values()) == 2

        app._on_stop_signal(signal.SIGHUP)
        for _ in range(100):
            if not app.is_running:
                break
            await asyncio.sleep(0.05)

    records = json.loads((app.session.target_dir / "session.json").read_text())
    assert [r["interrupted"] for r in records] == [True, True]
    assert not any(job.running for job in app.jobs.values())
