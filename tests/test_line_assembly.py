"""How the runner turns what a pty delivers into lines, when the timing is bad.

The 0.4 s prompt heuristic commits a partial line early. That used to leave
the newline that followed to become a blank line of its own, which showed up
on slow CI runners (a `\\r` held at the end of one read, the `\\n` late) and
after every answered prompt. A line committed early now owns the newline that
follows it. And a line with no end in sight is written out in bounded pieces
instead of being re-split on every read, which made it quadratic.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fieldlog import runner as runner_mod
from fieldlog.launch import plan_launch
from fieldlog.runner import run_job, send_stdin
from fieldlog.state import TargetSession

SH = {"id": "sh", "bin": "sh"}


async def _lines(tmp_workspace: Path, flags: str, reply: str | None = None) -> list[str]:
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(session, SH, {"id": "t", "flags": flags})
    seen: list[str] = []
    task = asyncio.create_task(
        run_job(plan.command, plan.job, session, lambda t, s: seen.append(t), env=plan.env)
    )
    if reply is not None:
        for _ in range(100):
            if plan.job.awaiting:
                break
            await asyncio.sleep(0.05)
        assert plan.job.awaiting, seen
        send_stdin(plan.job, reply)
    assert await task == 0
    return seen


async def test_a_late_newline_after_a_carriage_return_is_not_a_blank_line(tmp_workspace: Path):
    # onlcr off, so the bytes are exactly these: `abc\r`, a pause past the
    # prompt grace, then `\nxyz\n`.
    flags = """-c 'stty -onlcr; printf "abc\\r"; sleep 1; printf "\\nxyz\\n"'"""
    assert await _lines(tmp_workspace, flags) == ["abc", "xyz"]


async def test_cr_then_a_late_crlf_is_one_line_end(tmp_workspace: Path):
    # With onlcr on, the late `\n` arrives as `\r\n`: `abc\r` + `\r\n`.
    flags = """-c 'printf "abc\\r"; sleep 1; printf "\\nxyz\\n"'"""
    assert await _lines(tmp_workspace, flags) == ["abc", "xyz"]


async def test_an_answered_prompt_is_not_followed_by_a_blank_line(tmp_workspace: Path):
    # The tool prints its own newline after a hidden reply, as sudo and ssh do.
    flags = """-c 'printf "Password: "; read x; echo; echo "got it"'"""
    seen = await _lines(tmp_workspace, flags, reply="hunter2")
    assert seen == ["Password: ", "› (reply hidden)", "got it"]


async def test_a_prompt_answered_with_more_output_keeps_every_line(tmp_workspace: Path):
    flags = """-c 'printf "Continue? [y/N] "; read x; echo "answer: $x"'"""
    seen = await _lines(tmp_workspace, flags, reply="y")
    assert seen == ["Continue? [y/N] ", "› y", "answer: y"]


async def test_a_newline_free_flood_is_written_in_bounded_pieces(tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(runner_mod, "LINE_MAX", 100_000)
    flags = """-c 'head -c 3000000 /dev/zero | tr "\\000" a; echo; echo done'"""
    started = time.monotonic()
    seen = await _lines(tmp_workspace, flags)
    assert time.monotonic() - started < 10
    assert seen[-1] == "done"
    assert sum(len(line) for line in seen[:-1]) == 3_000_000
    assert max(len(line) for line in seen) <= 100_000 + 65536    # one read past the cap, at most
