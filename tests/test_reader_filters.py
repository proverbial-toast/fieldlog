"""`history` and `report` read one archive, so they take one set of filters.

`history` had `--recipe` and `report` had `--since`, each written where it
happened to be wanted. An operator could narrow a listing to the nmap runs and
then had no way to make a report of them; they could report from run #12 and
then had no way to list what that window held. Both flags now work on both
readers, through one `filter_runs`, so the two cannot drift on what a filter
means.

A filtered report also says so in its own summary line: the document is read
away from the command that produced it, and a report of one recipe's runs must
not read as a report of everything run against the host.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.cli import build_parser, filter_note, filter_runs, handle_history, handle_report


def _record(run_id: str, recipe: str, **extra) -> dict:
    return {
        "id": run_id,
        "recipe": recipe,
        "command": f"{recipe} 10.0.0.1",
        "exit_code": 0,
        "start_time": f"2026-09-19T09:0{run_id[-1]}:00",
        "duration_sec": 1.0,
        "artifacts": [],
        **extra,
    }


RUNS = [
    _record("01", "ping/quick"),
    _record("02", "nmap/fast"),
    _record("03", "ping/quick"),
    _record("04", "chain/reach"),
    {"id": "05", "recipe": "note", "note": "handover", "start_time": "2026-09-19T09:05:00"},
]


@pytest.fixture
def workspace(tmp_workspace: Path) -> Path:
    folder = tmp_workspace / "box.htb"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.json").write_text(json.dumps(RUNS), encoding="utf-8")
    return tmp_workspace


def _run(command: str, workspace: Path, *rest) -> int:
    args = build_parser().parse_args([command, "box.htb", *rest, "-w", str(workspace)])
    return (handle_history if command == "history" else handle_report)(args)


# ---- the filter itself ---------------------------------------------------


@pytest.mark.parametrize(
    "recipe, since, expected",
    [
        ("", "", ["01", "02", "03", "04", "05"]),
        ("ping/quick", "", ["01", "03"]),
        ("", "03", ["03", "04", "05"]),
        ("ping/quick", "02", ["03"]),              # both at once, and they compose
        ("chain/reach", "", ["04"]),               # a chain summary is a record like any other
        ("note", "", ["05"]),
        ("ping", "", []),                          # exact, never a prefix: `ping` is not a recipe
    ],
)
def test_the_filter_keeps_exactly_what_was_asked_for(recipe, since, expected):
    kept, error = filter_runs(RUNS, recipe, since)

    assert error == ""
    assert [r["id"] for r in kept] == expected


def test_a_since_that_is_not_a_number_is_refused_not_guessed():
    kept, error = filter_runs(RUNS, since="yesterday")

    assert kept == []
    assert "--since expects a run number" in error


def test_the_note_says_what_was_narrowed():
    assert filter_note("", "") == ""
    assert filter_note("ping/quick", "") == "recipe `ping/quick`"
    assert filter_note("ping/quick", "3") == "recipe `ping/quick` · from #3"


# ---- both readers take both flags ---------------------------------------


def test_history_takes_since(workspace: Path, capsys):
    assert _run("history", workspace, "--since", "04") == 0

    out = capsys.readouterr().out
    assert "#04" in out and "#05" in out
    assert "#01" not in out


def test_report_takes_recipe(workspace: Path, capsys):
    assert _run("report", workspace, "--recipe", "ping/quick") == 0

    out = capsys.readouterr().out
    assert "## #01 · ping/quick" in out
    assert "## #03 · ping/quick" in out
    assert "nmap/fast" not in out


def test_a_filtered_report_says_it_is_a_slice_of_the_archive(workspace: Path, capsys):
    assert _run("report", workspace, "--recipe", "ping/quick", "--since", "02") == 0

    out = capsys.readouterr().out
    # `#02` as the operator typed it: the note quotes the flag, it does not renumber it.
    assert "1 run · recipe `ping/quick` · from #02" in out


def test_an_unfiltered_report_says_nothing_extra(workspace: Path, capsys):
    assert _run("report", workspace) == 0

    summary = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Workspace"))
    assert summary.endswith("2026-09-19 09:01:00 to 2026-09-19 09:05:00")   # the window, nothing else


@pytest.mark.parametrize("command", ["history", "report"])
def test_both_readers_refuse_the_same_bad_since(workspace: Path, command, capsys):
    assert _run(command, workspace, "--since", "lastweek") == 1
    assert "--since expects a run number" in capsys.readouterr().err
