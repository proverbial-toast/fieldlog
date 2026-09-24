"""Findings from the 2026-09-23 hostile review, each reproduced before the fix.

1. Closing a running job's tab (detach, or kill before it died) dropped the job
   from `self.jobs`, so the scope form let the target change under it: the run
   was archived in the new target's folder, and a chain went on to run its
   later steps against the new target, unchecked.
2. The same missing job was killed on quit with no confirmation and no record,
   and a hang-up or a kill neither stopped it nor waited for its record.
3. Quitting mid-chain: `_run` swallowed the cancel, so the chain read it as a
   failed step and planned the next one while the app was going down.
4. A preset id YAML reads as a number (`id: 443`) crashed `run` and `list`, and
   with a chain naming its tool, every command that loads the catalog.
5. A launch into a target folder fieldlog cannot write (root-owned after a
   `sudo fieldlog`, a full disk) crashed the TUI with every other job in it,
   and gave the CLI a "this is a bug" report.
6. `--timeout` with a multi-line command: exec was skipped, coreutils timeout
   moved into its own process group, and Ctrl+C never reached the tool.
7. With stdout piped (`| tee`), nothing the operator typed reached the tool.
8. The CLI resized the pty 20 ms after the spawn began, but the spawn waits on
   the vantage lookup first, so the tool always saw 24x200.
9. A signal between two chain steps met Python's defaults; `--since 999999d`
   raised OverflowError.
"""

from __future__ import annotations

import asyncio
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fieldlog import runner as runner_mod
from fieldlog.app import FieldlogApp, QuitConfirm, TargetModal
from fieldlog.chain import run_chain
from fieldlog.cli import execute_cli_job, filter_runs, handle_list, handle_run
from fieldlog.launch import plan_launch
from fieldlog.recipes import find_chain, find_recipe, load_catalog, timeout_binary
from fieldlog.runner import exec_form, run_job
from fieldlog.state import TargetSession

SH = {"id": "sh", "bin": "sh"}
SLOW = {"id": "slow", "flags": "-c 'echo up; sleep 30'"}


def _records(folder: Path) -> list:
    manifest = folder / "session.json"
    return json.loads(manifest.read_text()) if manifest.exists() else []


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.05)


def _catalog(tmp_path: Path, text: str):
    base = tmp_path / "catalog.yaml"
    base.write_text(text)
    empty = tmp_path / "no-dropins"
    empty.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=empty)


# ---- 1 & 2. a detached job is still a running job ---------------------------


async def test_a_detached_job_still_blocks_a_target_change(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._spawn_job(SH, SLOW, "sh/slow")
        await pilot.pause(0.3)
        tab = next(t for t in app.tabs if t.id != "system")
        job = app.jobs[tab.job_id]
        app._drop_tab(tab.id)                        # what "detach" does
        assert app.jobs.get(tab.job_id) is job, "a running job outlives its tab"

        app.action_target_scope()
        await pilot.pause(0.3)
        assert isinstance(app.screen, TargetModal)
        from textual.widgets import Input
        app.screen.query_one("#in-target", Input).value = "10.0.0.2"
        app.screen.action_save()
        await pilot.pause(0.1)
        assert app.session.target == "10.0.0.1", "the target moved under a running job"

        app._stop_and_exit("test")
        await _until(lambda: not app.is_running)
    assert not app.jobs.get(tab.job_id, job).running


async def test_a_finished_detached_job_leaves_the_registry(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._spawn_job(SH, {"id": "brief", "flags": "-c 'sleep 0.5'"}, "sh/brief")
        await pilot.pause(0.2)
        tab = next(t for t in app.tabs if t.id != "system")
        app._drop_tab(tab.id)
        assert tab.job_id in app.jobs
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert tab.job_id not in app.jobs


async def test_a_run_is_archived_where_it_was_planned_whatever_the_scope_says_later(tmp_workspace: Path):
    """The second line of defence: the runner reads the plan's copy of the scope."""
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._spawn_job(SH, {"id": "brief", "flags": "-c 'sleep 0.5'"}, "sh/brief")
        await pilot.pause(0.2)
        app.session.target = "10.0.0.2"              # past the form's guard
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert [r["recipe"] for r in _records(tmp_workspace / "10.0.0.1")] == ["sh/brief"]
    assert not (tmp_workspace / "10.0.0.2" / "session.json").exists()


async def test_a_chain_keeps_the_scope_and_edits_it_was_launched_with(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, """
recipes:
  - id: say
    bin: sh
    presets:
      - id: t
        flags: "-c 'echo target=$TARGET'"
chains:
  - id: c
    steps: [say/t, say/t]
""")
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    edits: dict = {}

    async def run_step(plan):
        code = await run_job(plan.command, plan.job, plan.session, lambda t, s: None, env=plan.env)
        # Between steps, the live scope and edits change, as the TUI allows.
        session.target = "10.0.0.2"
        edits["say/t"] = "-c 'echo edited'"
        return code

    await run_chain(session, cat, find_chain(cat, "c"), run_step=run_step, flags_overrides=edits)

    records = _records(tmp_workspace / "10.0.0.1")
    assert [r["environment"]["TARGET"] for r in records] == ["10.0.0.1"] * 3
    assert all("edited" not in r["command"] for r in records)
    assert not (tmp_workspace / "10.0.0.2").exists()


async def test_quitting_with_only_a_detached_job_asks_and_then_archives_it(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.3", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._spawn_job(SH, SLOW, "sh/slow")
        await pilot.pause(0.3)
        app._drop_tab(next(t.id for t in app.tabs if t.id != "system"))
        app.action_quit()
        await pilot.pause(0.2)
        assert isinstance(app.screen, QuitConfirm), "a detached job is still a running one"
        app.screen.action_confirm()
        await _until(lambda: not app.is_running)
    [record] = _records(tmp_workspace / "10.0.0.3")
    assert record["interrupted"] is True


async def test_a_hang_up_stops_and_archives_a_detached_job(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.4", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app._spawn_job(SH, SLOW, "sh/slow")
        await pilot.pause(0.3)
        app._drop_tab(next(t.id for t in app.tabs if t.id != "system"))
        app._on_stop_signal(signal.SIGHUP)
        await _until(lambda: not app.is_running)
    [record] = _records(tmp_workspace / "10.0.0.4")
    assert record["interrupted"] is True


# ---- 3. quitting mid-chain stops the chain ----------------------------------

CHAIN_WITH_CONTINUE = """
recipes:
  - id: slow
    bin: sleep
    presets:
      - id: a
        flags: "30"
  - id: mark
    bin: sh
    presets:
      - id: b
        flags: "-c 'echo ran'"
chains:
  - id: c
    steps:
      - recipe: slow/a
        continue: true
      - mark/b
"""


async def test_a_confirmed_quit_mid_chain_archives_the_step_and_the_chain(tmp_path: Path, tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.5", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app.catalog = _catalog(tmp_path, CHAIN_WITH_CONTINUE)
        app._recipes = app.catalog.tools
        app.select_chain("c")
        app.action_run_task()
        await pilot.pause(0.5)
        app.action_quit()
        await pilot.pause(0.2)
        app.screen.action_confirm()
        await _until(lambda: not app.is_running)
    folder = tmp_workspace / "10.0.0.5"
    records = _records(folder)
    assert [(r["recipe"], r["exit_code"]) for r in records] == [("slow/a", 130), ("chain/c", 130)]
    assert not list((folder / "raw").glob("*mark_b*")), "the next step was planned after quit"


async def test_a_cancelled_chain_plans_no_further_step(tmp_path: Path, tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.6", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app.catalog = _catalog(tmp_path, CHAIN_WITH_CONTINUE)
        app._recipes = app.catalog.tools
        app.select_chain("c")
        app.action_run_task()
        await pilot.pause(0.5)
        app.exit()                                   # the workers are cancelled
    # Leaving run_test without a WorkerFailed is half the assertion.
    assert not list((tmp_workspace / "10.0.0.6" / "raw").glob("*mark_b*"))


# ---- 4. a preset id YAML reads as a number ----------------------------------

NUMERIC_IDS = """
recipes:
  - id: port
    bin: echo
    presets:
      - id: 443
        flags: "tls"
      - id: 8080
        name: 8080
        flags: 8080
chains:
  - id: ports
    steps: [port/443]
"""


def test_a_numeric_preset_id_is_a_recipe_like_any_other(tmp_path: Path, capsys):
    cat = _catalog(tmp_path, NUMERIC_IDS)               # this line used to raise
    assert find_chain(cat, "ports") is not None
    tool, preset, err = find_recipe(cat, "port/443")
    assert err is None and preset["id"] == "443"
    _, _, err = find_recipe(cat, "port/nope")
    assert "443, 8080" in err

    import argparse
    assert handle_list(argparse.Namespace(query="port", verbose=True, runnable=False, names=False, json=False), cat) == 0
    assert "port/8080" in capsys.readouterr().out


def test_a_numeric_preset_id_runs(tmp_path: Path, tmp_workspace: Path):
    import argparse
    cat = _catalog(tmp_path, NUMERIC_IDS)
    args = argparse.Namespace(
        recipe="port/443", target="10.0.0.1", target_flag="", host="", interface="", lhost="",
        workspace=str(tmp_workspace), artifact_root="", dry_run=False, extra_args="", note="",
        quiet=True, json=False, timeout=None,
    )
    assert handle_run(args, cat) == 0


# ---- 5. an archive fieldlog cannot write -------------------------------------

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root writes through 0555")


@needs_non_root
async def test_the_tui_survives_a_launch_it_cannot_archive(tmp_workspace: Path):
    folder = tmp_workspace / "10.0.0.7"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        app = FieldlogApp(TargetSession(target="10.0.0.7", workspace_dir=tmp_workspace))
        async with app.run_test() as pilot:
            app._spawn_job(SH, SLOW, "sh/slow")
            await pilot.pause(0.2)
            assert app.is_running
            assert not app.jobs
            assert any("cannot write to the archive" in line for line in app.system_log_lines)
    finally:
        folder.chmod(0o755)


@needs_non_root
def test_the_cli_says_it_cannot_write_rather_than_reporting_a_bug(tmp_path: Path, tmp_workspace: Path, capsys):
    import argparse
    cat = _catalog(tmp_path, NUMERIC_IDS)
    folder = tmp_workspace / "10.0.0.7"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        args = argparse.Namespace(
            recipe="port/443", target="10.0.0.7", target_flag="", host="", interface="", lhost="",
            workspace=str(tmp_workspace), artifact_root="", dry_run=False, extra_args="", note="",
            quiet=True, json=False, timeout=None,
        )
        assert handle_run(args, cat) == 1
        assert "cannot write to the archive" in capsys.readouterr().err
    finally:
        folder.chmod(0o755)


async def test_a_failed_archive_write_keeps_the_tools_own_exit_code(tmp_workspace: Path, monkeypatch):
    def full_disk(*_a, **_k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(runner_mod, "append_record", full_disk)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH, {"id": "three", "flags": "-c 'exit 3'"})
    assert await execute_cli_job(plan, session, quiet=True) == 3


# ---- 6. a quoted newline does not stop exec ----------------------------------


def test_exec_is_kept_for_a_newline_inside_quotes_only():
    assert exec_form("sh -c 'a\nb'") == "exec sh -c 'a\nb'"
    assert exec_form("timeout -k 5 9s sh -c 'a\nb'").startswith("exec timeout")
    assert exec_form("a\nb") == "a\nb"
    assert exec_form("sh -c 'a'\nb") == "sh -c 'a'\nb"


MULTILINE = """recipes:
  - id: ml
    bin: sh
    presets:
      - id: two
        flags: |
          -c 'echo start
          exec sleep 1000'
"""


@pytest.mark.skipif(timeout_binary() is None, reason="needs coreutils timeout")
def test_ctrl_c_reaches_a_multi_line_command_under_timeout(tmp_path: Path):
    (tmp_path / "recipes.d").mkdir()
    (tmp_path / "recipes.d" / "ml.yaml").write_text(MULTILINE)
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", "run", "ml/two", "10.0.0.9", "-q", "--timeout", "600"],
        cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log = None
    for _ in range(200):
        logs = list((tmp_path / "targets" / "10.0.0.9" / "raw").glob("*.log"))
        if logs and "start" in logs[0].read_text():
            log = logs[0]
            break
        time.sleep(0.05)
    assert log is not None, "the tool never started"
    proc.send_signal(signal.SIGINT)
    try:
        assert proc.wait(timeout=10) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
    [record] = _records(tmp_path / "targets" / "10.0.0.9")
    assert record["interrupted"] is True


# ---- 7. stdin is forwarded whenever it is a terminal -------------------------

ASK = """recipes:
  - id: probe
    bin: sh
    presets:
      - id: ask
        flags: "-c 'printf \\"answer: \\"; read x; echo got=$x'"
"""


def test_a_prompt_can_be_answered_with_stdout_piped(tmp_path: Path):
    (tmp_path / "recipes.d").mkdir()
    (tmp_path / "recipes.d" / "ask.yaml").write_text(ASK)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", "run", "probe/ask", "10.0.0.1", "-q"],
        cwd=tmp_path, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    os.close(slave)
    try:
        seen = b""
        deadline = time.monotonic() + 10
        while b"answer:" not in seen and time.monotonic() < deadline:
            if select.select([proc.stdout], [], [], 0.1)[0]:
                seen += os.read(proc.stdout.fileno(), 1024)
        assert b"answer:" in seen
        os.write(master, b"hello\n")
        out, _ = proc.communicate(timeout=10)
        assert b"got=hello" in seen + out
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)


# ---- 8. the tool sees the size it is given -----------------------------------


@pytest.mark.parametrize("winsize, shown", [((30, 100), "30 100"), (None, "24 200")])
async def test_the_pty_is_sized_before_the_tool_starts(tmp_workspace: Path, winsize, shown):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH, {"id": "size", "flags": "-c 'stty size'"})
    seen: list = []
    await run_job(plan.command, plan.job, session, lambda t, s: seen.append(t), env=plan.env, winsize=winsize)
    assert seen == [shown]


# ---- 9. between chain steps, and --since --------------------------------------


async def test_a_signal_between_chain_steps_stops_the_next_step(tmp_workspace: Path):
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH, SLOW)
    latch = [signal.SIGINT]                          # it landed while no step ran
    loop = asyncio.get_running_loop()
    try:
        started = time.monotonic()
        code = await execute_cli_job(plan, session, quiet=True, latch=latch)
        assert code == 130 and plan.job.interrupted
        assert time.monotonic() - started < 10
        assert latch == []
        # Leaving, the step handed Ctrl+C back to the latch, not to Python.
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.1)
        assert latch == [signal.SIGINT]
    finally:
        for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            loop.remove_signal_handler(signum)


@pytest.mark.parametrize("span", ["999999d", "99999999999999999999h"])
def test_a_since_too_far_back_is_an_error_not_a_crash(span: str):
    kept, error = filter_runs([{"start_time": "2026-09-23T10:00:00"}], since=span)
    assert kept == [] and error.startswith("Error: --since")
