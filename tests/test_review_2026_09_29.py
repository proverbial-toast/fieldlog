"""Guards for the 2026-09-29 review. Each one failed on the code before it.

C: the runner and the CLI. T: the TUI. Numbered as the review numbered them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest
from rich.cells import cell_len
from textual.widgets import Input, RichLog, TextArea

from fieldlog import archive as archive_mod
from fieldlog import runner as runner_mod
from fieldlog.app import FieldlogApp, TargetModal
from fieldlog.archive import append_record
from fieldlog.cli import build_parser, handle_note
from fieldlog.launch import plan_launch
from fieldlog.runner import RulesTooSlow, color_only, exec_form, run_job, strip_ansi, within_deadline
from fieldlog.state import ActiveJob, TargetSession, load_pinned_recent, save_pinned_recent
from fieldlog.tui.jobs import TAB_LINE_CHARS, pane_line
from fieldlog.tui.models import TabDescriptor
from fieldlog.tui.widgets import StdinInput

SH = {"id": "sh", "bin": "sh"}

# Catches SIGINT and carries on, as a tool mid-cleanup or a careless trap does.
DEAF = """recipes:
  - id: d
    bin: sh
    presets:
      - id: deaf
        flags: "-c 'trap \\"\\" INT; while true; do echo tick; sleep 0.1; done'"
"""


@pytest.fixture
def deaf_box(tmp_path: Path) -> Path:
    (tmp_path / "recipes.d").mkdir()
    (tmp_path / "recipes.d" / "d.yaml").write_text(DEAF)
    return tmp_path


def _tool_pid(parent: int) -> int:
    for _ in range(100):
        kids = subprocess.run(["pgrep", "-P", str(parent)], capture_output=True, text=True).stdout.split()
        if kids:
            return int(kids[0])
        time.sleep(0.05)
    raise AssertionError("fieldlog never spawned the tool")


def _last_record(box: Path) -> dict:
    return json.loads((box / "targets" / "10.0.0.1" / "session.json").read_text())[-1]


def _reap(proc: subprocess.Popen, tool: Optional[int]) -> None:
    """Leave nothing behind when an assertion failed with the run still going."""
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    if tool is not None:
        try:
            os.killpg(tool, signal.SIGKILL)
        except OSError:
            pass


# ---- C1. every stop has a SIGKILL behind it ---------------------------------


def test_c1_ctrl_c_ends_a_tool_that_ignores_sigint(deaf_box: Path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", "run", "d/deaf", "10.0.0.1", "-q"],
        cwd=deaf_box, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    tool = None
    try:
        tool = _tool_pid(proc.pid)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=20)              # SIGINT, then SIGKILL after STOP_SIGNAL_GRACE
    finally:
        _reap(proc, tool)
    record = _last_record(deaf_box)
    assert record["interrupted"] is True
    assert record["exit_code"] == 137


def test_c1_a_closed_pipe_ends_a_tool_that_ignores_sigint(deaf_box: Path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "fieldlog", "run", "d/deaf", "10.0.0.1", "-q"],
        cwd=deaf_box, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    tool = None
    try:
        tool = _tool_pid(proc.pid)
        proc.stdout.readline()
        proc.stdout.close()                 # `| head -1`
        proc.wait(timeout=20)
    finally:
        _reap(proc, tool)
    record = _last_record(deaf_box)
    assert record["interrupted"] is True
    assert record["exit_code"] == 137


# ---- C2. a `#` is not a comment to the exec decision ------------------------


def test_c2_a_hash_in_the_flags_does_not_hide_the_rest_of_the_command():
    command = "false http://10.0.0.1/health#probe || echo FALLBACK-RAN"
    assert exec_form(command) == command
    out = subprocess.run(["sh", "-c", exec_form(command)], capture_output=True, text=True)
    assert (out.returncode, out.stdout.strip()) == (0, "FALLBACK-RAN")
    # A plain comment still leaves one simple command, and still gets its exec.
    assert exec_form("ping -c 1 x # a note") == "exec ping -c 1 x # a note"


# ---- C3. the run ends with the tool, not with the pty -----------------------


async def test_c3_a_process_left_holding_the_pty_does_not_hold_the_run(tmp_workspace: Path, tmp_path: Path):
    daemon = tmp_path / "daemon.py"
    daemon.write_text(
        "import os, sys, time\n"
        "os.setsid()\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    pidfile = tmp_path / "daemon.pid"
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    flags = f"-c '{sys.executable} {daemon} {pidfile} & sleep 0.3; echo started'"
    plan = plan_launch(session, SH, {"id": "t", "flags": flags})
    seen: list = []
    started = time.monotonic()
    try:
        code = await asyncio.wait_for(
            run_job(plan.command, plan.job, session, lambda t, s: seen.append(t), env=plan.env), 10
        )
    finally:
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    assert code == 0
    assert "started" in seen
    assert time.monotonic() - started < 5
    assert plan.job.record is not None


# ---- C4. a runaway parse:/expect: rule is stopped, and the run archived -----


def test_c4_a_runaway_rule_is_stopped():
    started = time.monotonic()
    with pytest.raises(RulesTooSlow):
        within_deadline(re.search, r"(a+)+b", "a" * 64, deadline=0.2)
    assert time.monotonic() - started < 5


async def test_c4_a_run_with_a_runaway_parse_rule_is_still_archived(tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(runner_mod, "RULES_DEADLINE", 0.3)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    preset = {"id": "t", "flags": "-c 'printf " + "a" * 40 + "'", "parse": r"(a+)+b"}
    plan = plan_launch(session, SH, preset)
    seen: list = []
    code = await asyncio.wait_for(
        run_job(plan.command, plan.job, session, lambda t, s: seen.append(t), env=plan.env), 20
    )
    assert code == 0
    assert any("not applied" in line for line in seen)
    assert plan.job.record is not None and "summary" not in plan.job.record


# ---- C6. the manifest is on disk before it is swapped in --------------------


def test_c6_an_append_is_fsynced_before_the_swap(tmp_workspace: Path, monkeypatch):
    synced: list = []
    real = os.fsync
    monkeypatch.setattr(archive_mod.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    append_record(tmp_workspace / "box", {"id": "01", "recipe": "x"})
    assert synced


# ---- C7. a note says when it set an unreadable manifest aside ---------------


def test_c7_a_note_says_it_set_the_manifest_aside(tmp_workspace: Path, capsys):
    target = tmp_workspace / "box"
    target.mkdir(parents=True)
    (target / "session.json").write_text("{not json")
    args = build_parser().parse_args(["note", "box", "hello", "-w", str(tmp_workspace)])
    assert handle_note(args) == 0
    assert "session.json could not be read · kept as session.json.unreadable-" in capsys.readouterr().err


# ---- C10. log fidelity ------------------------------------------------------


async def test_c10_a_piece_cut_at_a_line_end_is_not_followed_by_a_blank_line(tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(runner_mod, "LINE_MAX", 10)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH, {"id": "t", "flags": """-c 'printf aaaaaaaaaaaa; sleep 0.2; printf "\\nnext\\n"'"""})
    seen: list = []
    assert await run_job(plan.command, plan.job, session, lambda t, s: seen.append(t), env=plan.env) == 0
    assert seen == ["aaaaaaaaaaaa", "next"]


def test_c10_escapes_that_used_to_leak_into_the_log_are_stripped():
    assert strip_ansi("a\x1b(B\x1b[mb") == "ab"          # `tput sgr0`
    assert strip_ansi("x\x1b]0;a title cut short") == "x"
    assert strip_ansi("cut \x1b[3") == "cut "
    assert color_only("\x1b[31mred\x1b[0m\x1b[K") == "\x1b[31mred\x1b[0m"


# ---- TUI helpers ------------------------------------------------------------


def _app(workspace: Path) -> FieldlogApp:
    return FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=workspace))


def _job(workspace: Path, prompt: Optional[str] = None) -> ActiveJob:
    """A running job with no process behind it; blocked on `prompt` if given."""
    return ActiveJob(
        id="01", recipe_id="x", name="x #01", log_path=workspace / "x.log",
        await_prompt=prompt, await_since=time.time() if prompt is not None else None,
    )


def _attach(app: FieldlogApp, job: ActiveJob, key: str = "7") -> str:
    tab_id = f"job-{key}"
    app.jobs[key] = job
    app.tabs.append(TabDescriptor(id=tab_id, label=job.name, status="active", tool_id="x", job_id=key))
    app.active_tab_id = tab_id
    return tab_id


# ---- T1. a job tab draws colour, and no other escape ------------------------


def test_t1_a_pane_line_draws_colour_and_drops_every_other_escape():
    line = pane_line("\x1b[31mred\x1b[0m \x1b[Kok\x1b[1A")
    assert line.plain == "red ok"
    assert cell_len(line.plain) == 6
    assert any("color(1)" in str(span.style) for span in line.spans)
    long = pane_line("\x1b[32m" + "x" * (TAB_LINE_CHARS + 50))
    assert "\x1b" not in long.plain
    assert long.plain.endswith("50 more characters in the log")


# ---- T2. the reply bar takes the keyboard once, and never from a field ------


async def test_t2_a_prompt_does_not_take_the_keyboard_from_the_filter(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("slash", "n", "m")
        _attach(app, _job(tmp_workspace, "Overwrite? [y/N]"))
        app._refresh_stdin_bar()
        await pilot.pause()
        assert getattr(app.focused, "id", None) == "filter-input"
        await pilot.press("a")
        assert app.query_one("#filter-input", Input).value == "nma"


async def test_t2_the_keyboard_is_not_taken_back_once_given_away(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        _attach(app, _job(tmp_workspace, "Password:"))
        app._refresh_stdin_bar()
        await pilot.pause()
        assert isinstance(app.focused, StdinInput)
        app.set_focus(None)                 # a click somewhere else
        await pilot.pause()
        app._refresh_stdin_bar()            # the next tick
        await pilot.pause()
        assert not isinstance(app.focused, StdinInput)


# ---- T3. Ctrl+C in the reply field is SIGINT, as the hint says --------------


async def test_t3_ctrl_c_in_the_reply_field_interrupts_the_job(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        job = _job(tmp_workspace, "Password:")
        _attach(app, job)
        app._refresh_stdin_bar()
        await pilot.pause()
        assert isinstance(app.focused, StdinInput)
        # With nothing selected Textual's copy steps aside; with a selection it won.
        await pilot.press("y", "e", "s", "ctrl+shift+a")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert job.interrupted


# ---- T4. Tab under a modal is Tab -------------------------------------------


async def test_t4_tab_moves_between_the_scope_forms_fields(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        pane = app.kbd_pane
        modal = TargetModal(app.session)
        app.push_screen(modal)
        await pilot.pause()
        modal.query_one("#in-target", Input).focus()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is modal.query_one("#in-hostname", Input)
        assert app.kbd_pane == pane


# ---- T5. the System log is bounded ------------------------------------------


async def test_t5_the_system_log_keeps_what_the_transcript_keeps(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)):
        assert app.query_one("#log-system", RichLog).max_lines == app.SYSTEM_LOG_MAX


# ---- T6. looking at the raw args edits nothing ------------------------------


async def test_t6_opening_the_raw_editor_marks_nothing_edited(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("e")
        await pilot.pause()
        assert app.args_raw_mode
        await pilot.press("escape")
        await pilot.pause()
        assert app.flag_edits == {}

        await pilot.press("e")
        await pilot.pause()
        app.query_one("#args-raw-area", TextArea).insert(" -v")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert "-v" in list(app.flag_edits.values())[0]      # a real edit is kept


# ---- T7. Enter takes a coloured prompt's advertised default -----------------


async def test_t7_enter_sends_the_default_of_a_coloured_prompt(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        try:
            job = _job(tmp_workspace, "Overwrite? [y/N]\x1b[0m ")
            job.pty_fd = write_end
            _attach(app, job)
            app._refresh_stdin_bar()
            await pilot.pause()
            app.send_stdin_reply("")
            assert os.read(read_end, 16) == b"\n"
        finally:
            os.close(read_end)
            os.close(write_end)


# ---- T8. a refused target change keeps the rest of the form -----------------


async def test_t8_a_refused_target_change_keeps_the_log_destination(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        job = _job(tmp_workspace)
        app.jobs["9"] = job                  # running
        modal = TargetModal(app.session)
        app.push_screen(modal)
        await pilot.pause()
        modal.query_one("#in-target", Input).value = "10.0.0.9"
        modal.query_one("#in-log-dest", Input).value = str(tmp_workspace / "logs")
        modal.action_save()
        await pilot.pause()
        assert app.session.target == "10.0.0.1"
        assert app.session.artifact_root == str(tmp_workspace / "logs")
        job.exit_code = 0


# ---- T9. a detached job that stops at a prompt says so ----------------------


async def test_t9_a_detached_job_waiting_for_input_is_said_in_the_system_log(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        app._spawn_job(SH, {"id": "asks", "flags": """-c 'sleep 0.5; printf "Password: "; read x'"""}, "sh/asks")
        await pilot.pause(0.2)
        tab = next(t for t in app.tabs if t.id != "system")
        app._drop_tab(tab.id)                # what "detach" does
        deadline = time.monotonic() + 10
        while not any("(detached) is waiting for input" in line for line in app.system_log_lines):
            assert time.monotonic() < deadline, app.system_log_lines
            await asyncio.sleep(0.05)
        app._stop_and_exit("test")
        while app.is_running:
            await asyncio.sleep(0.05)


# ---- T10. a pin the catalog cannot show is kept on disk ---------------------


def test_t10_pins_the_catalog_lacks_survive_a_save(tmp_workspace: Path):
    tmp_workspace.mkdir(parents=True, exist_ok=True)
    stored = tmp_workspace / ".pinned-recent.json"
    stored.write_text(json.dumps({"pinned": ["gone/x", "ping/quick"], "recent": ["gone/x"]}))
    pinned, recent = load_pinned_recent(tmp_workspace, valid_keys={"ping/quick"})
    assert (pinned, recent) == (["ping/quick"], [])
    save_pinned_recent(tmp_workspace, [], ["ping/quick"])     # unpinned one, launched it
    assert json.loads(stored.read_text()) == {"pinned": ["gone/x"], "recent": ["ping/quick", "gone/x"]}


# ---- T11. the System log does not claim a kill that was not sent ------------


async def test_t11_killing_a_job_that_already_ended_is_not_logged_as_a_kill(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(160, 40)) as pilot:
        job = _job(tmp_workspace)
        tab_id = _attach(app, job)
        app.action_close_tab(tab_id)
        await pilot.pause()
        job.exit_code = 0                    # it ended while the question was open
        await pilot.press("k")
        await pilot.pause()
        said = "\n".join(app.system_log_lines)
        assert "had already ended" in said
        assert "killed by operator" not in said
