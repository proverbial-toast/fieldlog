"""A job tab tails its output, like a terminal, instead of stopping at line 500.

It used to paint the first 500 lines and then nothing, so a long-running
recipe — a capture, a server, a continuous ping — went quiet on screen while
it was still working. It now keeps the newest TAB_TAIL_LINES and repaints in
batches, so a burst of output cannot starve the UI either.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from textual.widgets import RichLog

from fieldlog.app import FieldlogApp
from fieldlog.state import TargetSession
from fieldlog.tui import jobs as jobs_mod

SEQ = {"id": "seq", "bin": "seq"}


def _lines(rlog: RichLog) -> list[str]:
    return [strip.text.rstrip() for strip in rlog.lines]


def _values(lines: list[str]) -> list[str]:
    """What `seq` printed, read off the rows shaped `<gutter no>  <value>`."""
    return [m[1] for m in (re.fullmatch(r"\s*\d+  (\d+)", line) for line in lines) if m]


async def _run_in_tab(tmp_workspace: Path, flags: str) -> tuple[FieldlogApp, RichLog, float, list[str]]:
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test(size=(140, 40)) as pilot:
        started = time.monotonic()
        app._spawn_job(SEQ, {"id": "t", "flags": flags}, "seq/t")
        await app.workers.wait_for_complete()
        elapsed = time.monotonic() - started
        await pilot.pause()
        tab = app.active_tab()
        rlog = app.query_one(f"#log-{tab.id}", RichLog)
        return app, rlog, elapsed, _lines(rlog)


async def test_the_tab_shows_the_newest_lines_not_the_first_500(tmp_workspace: Path):
    total = jobs_mod.TAB_TAIL_LINES + 1500
    _app, _rlog, _elapsed, lines = await _run_in_tab(tmp_workspace, f"1 {total}")

    assert _values(lines)[-1] == str(total)                 # the last line is there
    assert len(lines) <= jobs_mod.TAB_TAIL_LINES
    assert f"showing the last {jobs_mod.TAB_TAIL_LINES} of {total} lines" in " ".join(lines)
    assert lines[-1].startswith("[Runner] exit 0")


async def test_a_short_run_is_shown_whole_with_no_note(tmp_workspace: Path):
    _app, _rlog, _elapsed, lines = await _run_in_tab(tmp_workspace, "1 50")
    assert _values(lines) == [str(i) for i in range(1, 51)]
    assert not any("showing the last" in line for line in lines)


async def test_a_burst_of_output_does_not_stall_the_tab(tmp_workspace: Path):
    # 300k lines. Painted one by one this took the UI minutes; batched, the
    # tab renders at most TAB_TAIL_LINES rows per tick.
    # ~1.5 s on a laptop; a slow CI Mac once took 11.5. The bound only has to
    # tell seconds from minutes.
    _app, _rlog, elapsed, lines = await _run_in_tab(tmp_workspace, "1 300000")
    assert elapsed < 30
    # The ticks skipped most of the flood; the finished tab is the tail, whole.
    tail = jobs_mod.TAB_TAIL_LINES
    values = _values(lines)
    assert values[-(tail - 5):] == [str(i) for i in range(300000 - tail + 6, 300001)]


async def test_the_copy_fallback_is_bounded_too(tmp_workspace: Path):
    app, _rlog, _elapsed, _lines_ = await _run_in_tab(tmp_workspace, "1 20000")
    job = next(iter(app.jobs.values()))
    assert len(job.log_lines) <= 2 * jobs_mod.TAB_TAIL_LINES + 1
    assert job.log_lines[-2] == "20000"                     # then the exit line


async def test_a_huge_line_is_clipped_on_screen_not_in_the_log(tmp_workspace: Path):
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    tool = {"id": "sh", "bin": "sh"}
    preset = {"id": "t", "flags": """-c 'head -c 50000 /dev/zero | tr "\\000" a; echo'"""}
    async with app.run_test(size=(140, 40)) as pilot:
        app._spawn_job(tool, preset, "sh/t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        rlog = app.query_one(f"#log-{app.active_tab().id}", RichLog)
        shown = " ".join(_lines(rlog))
        job = next(iter(app.jobs.values()))
    assert f"{50000 - jobs_mod.TAB_LINE_CHARS} more characters in the log" in shown
    assert Path(job.log_path).read_text().count("a") == 50000
