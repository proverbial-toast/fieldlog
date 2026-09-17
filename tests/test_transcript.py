"""The System tab is written to disk as well as to the screen.

`write_system_log` is the one choke point every System line goes through, so
appending there puts the operator's decisions — kills, detaches, scope changes,
reloads — into `<workspace>/fieldlog.log` beside the runs they were made about.
The file is the same transcript with a local timestamp on each event; a
workspace that cannot be written is reported once and then let be.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List

import pytest

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession
from fieldlog.transcript import TRANSCRIPT_FILE, append_transcript, transcript_line

STAMPED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} ")


def _entries(workspace: Path) -> List[str]:
    """The transcript's events with their stamps removed.

    A line that opens with a stamp starts an event; anything else continues the
    one before it, which is how a multi-line event round-trips.
    """
    text = (workspace / TRANSCRIPT_FILE).read_text(encoding="utf-8")
    events: List[str] = []
    for line in text.splitlines():
        if STAMPED.match(line):
            events.append(line[20:])
        elif events:
            events[-1] += "\n" + line
    return events


# ---- The file format -------------------------------------------------------

def test_a_line_is_its_stamp_then_its_text():
    line = transcript_line("[scope] target set 10.0.0.1")
    assert STAMPED.match(line)
    assert line[20:] == "[scope] target set 10.0.0.1"


def test_the_stamp_is_the_moment_passed_in():
    when = 1_600_000_000.0
    assert transcript_line("x", when=when) == (
        datetime.fromtimestamp(when).isoformat(timespec="seconds") + " x"
    )


def test_append_creates_the_workspace_and_keeps_order(tmp_path: Path):
    workspace = tmp_path / "not-yet" / "targets"
    append_transcript(workspace, "first")
    append_transcript(workspace, "second")
    assert _entries(workspace) == ["first", "second"]


# ---- The app writes through it ---------------------------------------------

@pytest.mark.asyncio
async def test_the_boot_preflight_lands_in_the_file(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        events = _entries(tmp_workspace)
        assert any("harness up" in e for e in events)
        assert "[scope] target set 10.0.0.1 (address)" in events

        app.write_system_log("[test] hello")
        await pilot.pause()

    lines = (tmp_workspace / TRANSCRIPT_FILE).read_text(encoding="utf-8").splitlines()
    assert STAMPED.match(lines[-1])
    assert lines[-1].endswith("[test] hello")
    assert app.system_log_lines[-1] == "[test] hello"   # the in-memory copy stays unstamped


@pytest.mark.asyncio
async def test_the_file_and_the_system_tab_say_the_same_thing(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.write_system_log("[kill] job #01 · SIGTERM")
        app.write_system_log("[args] reset to the variant default")
        await pilot.pause()

        assert _entries(tmp_workspace) == app.system_log_lines


@pytest.mark.asyncio
async def test_an_unwritable_workspace_is_said_once(tmp_workspace: Path):
    # A directory where the transcript goes: every append fails, and nothing the
    # operator does will fix it mid-session.
    (tmp_workspace / TRANSCRIPT_FILE).mkdir()

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.write_system_log("[test] one")
        app.write_system_log("[test] two")
        app.write_system_log("[test] three")
        await pilot.pause()

        complaints = [ln for ln in app.system_log_lines if ln.startswith("[transcript] not written")]
        assert len(complaints) == 1
        assert TRANSCRIPT_FILE in complaints[0]
        assert app.system_log_lines[-1] == "[test] three"   # the harness carried on
