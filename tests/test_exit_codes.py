"""An interrupted run records the tool's own exit code, plus the operator's intent.

EXIT is never fabricated: `interrupted` says the operator sent SIGINT, and the
code says what the tool did about it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.report import render_report
from fieldlog.runner import interrupt_job, run_job
from fieldlog.state import TargetSession

SH_TOOL = {"id": "sh", "bin": "sh"}


async def _run(
    session: TargetSession,
    flags: str,
    interrupt_after: float | None = None,
    bin_override: str = "",
):
    """Run one sh preset, optionally SIGINTing it mid-flight. Returns (code, job, record)."""
    preset = {"id": "t", "flags": flags}
    if bin_override:
        preset["bin"] = bin_override
    plan = plan_launch(session, SH_TOOL, preset)
    if interrupt_after is not None:
        # run_job sets job.process early, so the timer finds a job to signal.
        asyncio.get_running_loop().call_later(interrupt_after, interrupt_job, plan.job)
    code = await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    record = json.loads((session.target_dir / "session.json").read_text())[-1]
    return code, plan.job, record


@pytest.mark.asyncio
async def test_a_tool_that_handles_sigint_keeps_its_own_exit_code(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    # No `exec` in the preset: run_job's exec_form makes the tool the process
    # it waits on. Otherwise /bin/sh (dash) forks the child and dies of the
    # same group SIGINT, reporting 130 whatever the tool returned.
    code, job, record = await _run(
        session,
        """-c 'trap "echo caught; exit 0" INT; sleep 5'""",
        interrupt_after=0.3,
    )

    assert code == 0
    assert job.interrupted is True
    assert record["exit_code"] == 0
    assert record["interrupted"] is True


@pytest.mark.asyncio
async def test_a_tool_that_ignores_sigint_dies_and_shows_130(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.2", workspace_dir=tmp_workspace)
    code, job, record = await _run(session, "-c 'sleep 5'", interrupt_after=0.3)

    assert code == 130            # 128 + SIGINT, not asyncio's -2
    assert job.interrupted is True
    assert record["exit_code"] == 130
    assert record["interrupted"] is True


@pytest.mark.asyncio
async def test_an_untouched_run_carries_no_interrupted_key(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.3", workspace_dir=tmp_workspace)
    code, job, record = await _run(session, "-c true")

    assert code == 0
    assert job.interrupted is False
    assert "interrupted" not in record


def _one_run_report(tmp_workspace: Path, record: dict) -> str:
    return render_report(tmp_workspace / "10.0.0.1", [{"id": "01", "recipe": "ping/quick", **record}])


def test_report_annotates_an_interrupted_clean_exit_without_bolding_it(tmp_workspace: Path):
    out = _one_run_report(tmp_workspace, {"exit_code": 0, "interrupted": True})
    assert "| 0 (interrupted) |" in out
    assert "exit 0 (interrupted)" in out


def test_report_bolds_an_interrupted_failure(tmp_workspace: Path):
    out = _one_run_report(tmp_workspace, {"exit_code": 1, "interrupted": True})
    assert "| **1 (interrupted)** |" in out


@pytest.mark.parametrize(
    "command, execd",
    [
        ("ping -c 4 -W 1 10.0.0.1", True),
        ("curl -o /dev/null -s -w \"dns=%{time_namelookup} code=%{http_code}\" https://h/", True),
        ("tcpdump -i eth0 -w /tmp/x.pcap host 10.0.0.1 2>&1", True),   # redirections are fine
        ("sudo tcpdump -i eth0", True),
        ("timeout -k 5 5s sh -c 'ping -c 4 10.0.0.1'", True),
        ("ping -c 1 h | grep rtt", False),
        ("ss -tulpn; ss -tn", False),
        ("ping -c 1 h && echo up", False),
        ("(cd /tmp && ls)", False),
        ("ping $(cat targets)", False),                                 # `(` is enough to keep the shell
        ("ping -c 1 h\necho done", False),
        ("echo 'unbalanced", False),
        ("exec ping -c 1 h", False),                                    # already exec'd: unchanged
    ],
)
def test_exec_form_only_wraps_a_single_simple_command(command, execd):
    from fieldlog.runner import exec_form

    assert exec_form(command) == (f"exec {command}" if execd else command)


def test_timeout_wrapper_execs_the_inner_command(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.9", workspace_dir=tmp_workspace)
    plan = plan_launch(session, {"id": "true", "bin": "true"}, {"id": "t", "flags": ""}, timeout=5, dry_run=True)
    assert plan.command == "timeout -k 5 5s sh -c 'exec true'"
