"""Every reader of session.json gives the same answer about a damaged one.

The archive is the product, so "nothing has been run here" and "what was run
here cannot be read" must never come out as the same sentence. They did:
`report` rendered `No runs recorded.` and exited 0, the workspace overview
printed `0 runs`, and `history` printed a bare `Failed to read session
manifest: Expecting value: line 1 column 11 (char 10)` and exited 1 — three
readers of one file, three answers, and two of them indistinguishable from an
empty folder.

`report.read_manifest` is now the one reader. A folder with no manifest is not
a problem; a manifest that will not parse is, and stops the command with exit
2; a manifest that parses but carries something that is not a run record keeps
the records it has and says what it skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.cli import build_parser, handle_history, handle_report
from fieldlog.report import read_manifest

RECORD = {
    "id": "01",
    "recipe": "ping/quick",
    "command": "ping -c 4 10.0.0.1",
    "exit_code": 0,
    "start_time": "2026-09-19T09:00:00",
    "duration_sec": 3.1,
    "artifacts": [],
}


def _target(workspace: Path, name: str, body: str) -> Path:
    folder = workspace / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.json").write_text(body, encoding="utf-8")
    return folder


def _run(command: str, workspace: Path, *rest) -> int:
    args = build_parser().parse_args([command, *rest, "-w", str(workspace)])
    return (handle_history if command == "history" else handle_report)(args)


# ---- read_manifest, on its own ------------------------------------------


def test_a_folder_with_no_manifest_is_not_a_problem(tmp_workspace: Path):
    """Nothing has been run against it yet, which is an answer, not a fault."""
    empty = tmp_workspace / "fresh"
    empty.mkdir()

    manifest = read_manifest(empty)

    assert manifest.runs == []
    assert manifest.problem == ""
    assert manifest.readable is True


@pytest.mark.parametrize(
    "body, expected",
    [
        ("{not json", "is not valid JSON"),
        ('{"runs": []}', "is not a list of run records"),   # never a shape fieldlog writes
        ("", "is not valid JSON"),
    ],
)
def test_a_manifest_that_cannot_be_read_says_so(tmp_workspace: Path, body, expected):
    manifest = read_manifest(_target(tmp_workspace, "broken", body))

    assert manifest.readable is False
    assert expected in manifest.problem
    assert manifest.runs == []


def test_entries_that_are_not_records_are_kept_out_and_counted(tmp_workspace: Path):
    """A half-written manifest still holds real runs; they are the answer, and
    what was dropped is the footnote — not the other way round."""
    folder = _target(tmp_workspace, "partial", json.dumps([RECORD, "junk", 7]))

    manifest = read_manifest(folder)

    assert manifest.runs == [RECORD]
    assert manifest.readable is True
    assert "2 entries are not a run record" in manifest.problem


# ---- the readers, on the same folder ------------------------------------


@pytest.mark.parametrize("command", ["history", "report"])
def test_both_readers_refuse_a_manifest_they_cannot_read(tmp_workspace: Path, command, capsys):
    _target(tmp_workspace, "broken", "{not json")

    assert _run(command, tmp_workspace, "broken") == 2

    captured = capsys.readouterr()
    assert "is not valid JSON" in captured.err
    assert captured.out.strip() == "", "a command with no answer printed one anyway"


@pytest.mark.parametrize("command", ["history", "report"])
def test_both_readers_warn_and_carry_on_over_a_partial_manifest(tmp_workspace: Path, command, capsys):
    _target(tmp_workspace, "partial", json.dumps([RECORD, "junk"]))

    assert _run(command, tmp_workspace, "partial") == 0

    captured = capsys.readouterr()
    assert "Warning:" in captured.err
    assert "ping/quick" in captured.out, "the records that survived were not shown"


def test_history_json_refuses_too_rather_than_printing_an_empty_list(tmp_workspace: Path, capsys):
    """`--json` is the form most likely to be piped into something that
    believes it, so an empty array here is the costliest wrong answer."""
    _target(tmp_workspace, "broken", "{not json")

    assert _run("history", tmp_workspace, "broken", "--json") == 2
    assert capsys.readouterr().out.strip() == ""


def test_the_workspace_overview_marks_a_folder_it_could_not_read(tmp_workspace: Path, capsys):
    """`0 runs` reads as an empty folder; the overview is where an operator
    chooses which target to open, so it has to tell the two apart."""
    _target(tmp_workspace, "broken", "{not json")
    _target(tmp_workspace, "fine", json.dumps([RECORD]))

    assert _run("history", tmp_workspace) == 0

    out = capsys.readouterr().out
    assert "unreadable session.json" in out
    assert "1 run" in out, "the readable folder stopped being listed"
