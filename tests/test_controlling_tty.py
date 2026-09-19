"""A job owns its pty as a controlling terminal, so /dev/tty prompts work."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from fieldlog.launch import plan_launch
from fieldlog.runner import BLOCK_GRACE, interrupt_job, loggable_reply, run_job, send_stdin
from fieldlog.state import ActiveJob, TargetSession

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
    log = plan.job.log_path.read_text()
    assert "got:hello" in log
    # "pw: " offers no choices, so the reply itself stays out of the log.
    assert "› (reply hidden)" in log
    assert "› hello" not in log


@pytest.mark.parametrize(
    "prompt, reply, logged",
    [
        ("Are you sure you want to continue connecting (yes/no/[fingerprint])? ", "yes", "yes"),
        ("Continue? [y/N] ", "n", "n"),
        ("Continue? [y/N] ", "", ""),
        ("Continue? [y/N] ", "yesplease", None),
        ("Enter passphrase for key '/home/operator/.ssh/id_ed25519': ", "operator", None),  # path words are no choice
        ("[sudo] password for operator: ", "sudo", None),
        ("interface: ", "eth1", None),
    ],
)
def test_only_offered_choices_are_logged(prompt, reply, logged):
    assert loggable_reply(prompt, reply) == logged


@pytest.mark.asyncio
async def test_child_is_still_its_own_session_leader(tmp_workspace: Path):
    """killpg(pid) stays valid: setsid in preexec replaces start_new_session.

    The ids come from Python rather than `ps -o sid=`: `sid` is a Linux
    keyword, and BSD `ps` on macOS exits 1 on it, which failed this test on
    every macOS leg of CI for a reason that had nothing to do with sessions."""
    session = _session(tmp_workspace)
    probe = f"{shlex.quote(sys.executable)} -c {shlex.quote('import os; print(os.getsid(0), os.getpgid(0))')}"
    plan = plan_launch(session, SH_TOOL, _preset(probe))

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


@pytest.mark.asyncio
async def test_cancelling_a_running_job_reaps_the_whole_group(tmp_workspace: Path):
    """Quitting cancels the worker; run_job's finally must signal the group, not
    just the leader. A pipeline leaves children a bare terminate() would orphan."""
    session = _session(tmp_workspace)
    plan = plan_launch(session, SH_TOOL, _preset("sleep 30 | sleep 30"))

    task = asyncio.create_task(
        run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)
    )
    while plan.job.process is None:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)  # let the shell fork the pipeline children
    pgid = os.getpgid(plan.job.process.pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shell and both sleeps must be gone — not left running for 30s.
    #
    # ESRCH is the answer on Linux, but a macOS runner also reports EPERM here:
    # signal 0 against a group mid-teardown, or whose id has been recycled, is
    # refused rather than reported missing. Both mean the group this test
    # created is no longer ours to signal, which is what "reaped" is being
    # measured by — and it is the same pair `run_job`'s own finally suppresses
    # when it signals. Catching ESRCH alone made this a macOS flake: it passed
    # on the 3.11 and 3.13 legs and failed on 3.12 in the same run.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("process group survived cancellation")


# ---- Who shows the operator's reply ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("echo", [False, True], ids=["tui", "cli"])
async def test_echo_is_the_front_ends_choice(tmp_workspace: Path, echo: bool):
    """The TUI renders its own `› reply` note, so the pty must not echo. The CLI
    renders nothing, so the pty's echo is what makes a reply visible and what
    puts it in the log."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    job = ActiveJob(id="01", recipe_id="sh", name="sh #01",
                    log_path=tmp_workspace / "echo.log")

    lines: list[str] = []
    task = asyncio.create_task(
        run_job("""sh -c 'read answer < /dev/tty; echo "got:$answer"'""",
                job, session, lambda t, _s: lines.append(t), echo=echo)
    )
    for _ in range(100):                       # wait for the pty to exist
        await asyncio.sleep(0.02)
        if job.pty_fd is not None:
            break
    await asyncio.sleep(BLOCK_GRACE + 0.2)
    os.write(job.pty_fd, b"yes\n")
    assert await task == 0

    text = "\n".join(lines)
    assert "got:yes" in text                   # the tool read it either way
    typed = [ln for ln in lines if ln.strip() == "yes"]
    assert bool(typed) is echo                 # only an echoing pty shows it back


@pytest.mark.asyncio
async def test_a_tool_that_hides_its_prompt_still_hides_it_under_echo(tmp_workspace: Path):
    """Echo on hands the choice back to the tool, it does not force it. A tool
    that turns echo off for a secret — as sudo does — still gets its way."""
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    job = ActiveJob(id="01", recipe_id="sh", name="sh #01",
                    log_path=tmp_workspace / "secret.log")

    lines: list[str] = []
    # `read -s`-alike: stty turns echo off on the controlling tty first.
    command = """sh -c 'stty -echo < /dev/tty; read pw < /dev/tty; stty echo < /dev/tty; echo "len:${#pw}"'"""
    task = asyncio.create_task(
        run_job(command, job, session, lambda t, _s: lines.append(t), echo=True)
    )
    for _ in range(100):
        await asyncio.sleep(0.02)
        if job.pty_fd is not None:
            break
    await asyncio.sleep(BLOCK_GRACE + 0.2)
    os.write(job.pty_fd, b"hunter2\n")
    assert await task == 0

    text = "\n".join(lines)
    assert "len:7" in text                     # the tool got the whole secret
    assert "hunter2" not in text               # and it never reached the log
    assert "hunter2" not in job.log_path.read_text(encoding="utf-8")
