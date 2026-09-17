"""What `history` reads back out of the archive.

A record carries meaning — a summary, the fields its `parse:` rule found, a
verdict, a note — and these are the surfaces that show it: `--recipe` narrows
the timeline to one thing, `--fields` turns it sideways into a trend, and the
no-argument overview says what each folder's last record was rather than only
how many there are.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Tuple

import pytest

from fieldlog.cli import build_parser, handle_history, overview_line

NOTE = "customer confirmed the outage at 14:10"

RECORDS = [
    {"id": "01", "recipe": "ping/quick", "command": "ping -c 4 10.0.0.1", "exit_code": 0,
     "duration_sec": 1.2, "start_time": "2026-09-17T09:00:00.1",
     "summary": "4 replies · 0% loss", "fields": {"rx": "4", "loss": "0"}, "artifacts": []},
    {"id": "02", "recipe": "grep/find", "command": "grep -c x", "exit_code": 1, "success": [0, 1],
     "duration_sec": 0.1, "start_time": "2026-09-17T09:01:00.1",
     "fields": {"hits": "0"}, "artifacts": []},
    {"id": "03", "recipe": "ping/quick", "command": "ping -c 4 10.0.0.1", "exit_code": 0,
     "duration_sec": 1.1, "start_time": "2026-09-17T09:02:00.1",
     "expect": {"pattern": "0% packet loss", "found": False},
     "fields": {"rx": "3", "loss": "25"}, "artifacts": []},
    {"id": "04", "recipe": "note", "note": NOTE, "start_time": "2026-09-17T09:03:00.1"},
]


@pytest.fixture
def workspace(tmp_workspace: Path) -> Path:
    """One folder holding two recipes' runs and a note, in that order."""
    target = tmp_workspace / "box.htb"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps(RECORDS, indent=2), encoding="utf-8")
    return tmp_workspace


@pytest.fixture
def wide(monkeypatch):
    """Rich wraps at 80 columns when it is printing to a pipe, and a table of
    fields is wider than that."""
    monkeypatch.setenv("COLUMNS", "200")


def _history(workspace: Path, *extra: str) -> int:
    return handle_history(build_parser().parse_args(
        ["history", "box.htb", "-w", str(workspace), *extra]
    ))


def _table(out: str) -> Tuple[List[str], List[List[str]]]:
    """(header names, rows) from a printed `--fields` table.

    Every cell is sliced at the header's own offsets rather than split on
    whitespace, which is the only way a blank cell reads as blank.
    """
    lines = [ln for ln in out.splitlines() if ln.startswith("  ")]
    header = lines[0]
    bounds = [m.start() for m in re.finditer(r"\S+", header)] + [len(header) + 999]
    rows = [[line[a:b].strip() for a, b in zip(bounds, bounds[1:])] for line in lines[1:]]
    return header.split(), rows


# ---- 1. The listing --------------------------------------------------------


def test_the_listing_gives_a_note_its_own_line(workspace: Path, capsys):
    assert _history(workspace) == 0
    out = capsys.readouterr().out

    line = next(ln for ln in out.splitlines() if "#04" in ln)
    assert "note" in line
    # Nothing ran, so there is no code and no duration to print.
    assert "exit" not in line and "0.1s" not in line
    assert f"✎ {NOTE}" in out


# ---- 2. --recipe -----------------------------------------------------------


def test_recipe_keeps_only_that_recipes_records(workspace: Path, capsys):
    assert _history(workspace, "--recipe", "ping/quick") == 0
    out = capsys.readouterr().out
    assert "#01" in out and "#03" in out
    assert "#02" not in out and "#04" not in out


def test_recipe_filters_the_json_too(workspace: Path, capsys):
    assert _history(workspace, "--recipe", "note", "--json") == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["04"]


def test_a_filter_that_matches_nothing_is_an_answer_not_an_error(workspace: Path, capsys):
    assert _history(workspace, "--recipe", "nmap/fast") == 0
    out = capsys.readouterr().out
    assert "Run History for box.htb" in out
    assert "no runs of nmap/fast" in out


def test_a_folder_with_no_records_says_no_runs(tmp_workspace: Path, capsys):
    bare = tmp_workspace / "bare"
    bare.mkdir()
    (bare / "session.json").write_text("[]", encoding="utf-8")

    assert handle_history(build_parser().parse_args(
        ["history", "bare", "-w", str(tmp_workspace)])) == 0
    out = capsys.readouterr().out
    assert "Run History for bare" in out
    assert "no runs" in out


# ---- 3. --fields -----------------------------------------------------------


def test_fields_gives_every_field_a_column(workspace: Path, capsys, wide):
    assert _history(workspace, "--fields") == 0
    names, rows = _table(capsys.readouterr().out)

    # First seen, not sorted: ping's fields, then grep's.
    assert names == ["#", "started", "exit", "rx", "loss", "hits"]
    assert rows[0] == ["01", "2026-09-17 09:00:00", "0", "4", "0", ""]
    # A run of another recipe leaves the columns it has nothing for blank.
    assert rows[1] == ["02", "2026-09-17 09:01:00", "1 (ok)", "", "", "0"]
    assert rows[2] == ["03", "2026-09-17 09:02:00", "0 (expect not met)", "3", "25", ""]
    # A note has no code to label and no fields of its own.
    assert rows[3] == ["04", "2026-09-17 09:03:00", "—", "", "", ""]


def test_fields_narrows_with_the_recipe_filter(workspace: Path, capsys, wide):
    assert _history(workspace, "--recipe", "ping/quick", "--fields") == 0
    names, rows = _table(capsys.readouterr().out)

    # grep's `hits` is not a column of a table that holds no grep run.
    assert names == ["#", "started", "exit", "rx", "loss"]
    assert [row[0] for row in rows] == ["01", "03"]


def test_fields_changes_nothing_about_the_json(workspace: Path, capsys):
    assert _history(workspace, "--fields", "--json") == 0
    records = json.loads(capsys.readouterr().out)

    assert [r["id"] for r in records] == ["01", "02", "03", "04"]
    assert records[0]["fields"] == {"rx": "4", "loss": "0"}


# ---- 4. The workspace overview ---------------------------------------------


def test_the_overview_says_what_each_folder_last_did(tmp_workspace: Path, capsys, wide):
    ran = tmp_workspace / "box.htb"
    ran.mkdir()
    (ran / "session.json").write_text(json.dumps(RECORDS[:1]), encoding="utf-8")

    noted = tmp_workspace / "10.0.0.9"
    noted.mkdir()
    (noted / "session.json").write_text(json.dumps([
        RECORDS[0],
        {"id": "02", "recipe": "note", "note": f"{NOTE}\nand a second line",
         "start_time": "2026-09-17T09:03:00.1"},
    ]), encoding="utf-8")

    # A folder someone made, or one whose runs are all dry runs: no manifest.
    (tmp_workspace / "empty").mkdir()

    assert handle_history(build_parser().parse_args(["history", "-w", str(tmp_workspace)])) == 0
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ")]

    assert rows == [
        # The names are padded to the widest, and a note's first line stands in
        # for the summary a run would have.
        f"  10.0.0.9 2 runs · last 2026-09-17 09:03:00 note · {NOTE}",
        "  box.htb  1 run · last 2026-09-17 09:00:00 ping/quick · 4 replies · 0% loss",
        "  empty    0 runs",
    ]


def test_a_long_note_is_clipped_in_the_overview(tmp_path: Path):
    line = overview_line(tmp_path / "box.htb", [
        {"id": "01", "recipe": "note", "note": "x" * 90, "start_time": "2026-09-17T09:00:00"},
    ])
    assert "x" * 59 + "…" in line
    assert "x" * 60 not in line


def test_a_long_chain_summary_is_clipped_in_the_overview(tmp_path: Path):
    """A chain's summary is its steps' joined, so it is the one most likely to
    run past the row; the same rule clips it as clips a note."""
    line = overview_line(tmp_path / "box.htb", [
        {"id": "01", "recipe": "chain/sweep", "command": "chain sweep",
         "steps": [{"recipe": "ping/quick", "exit_code": 0}], "exit_code": 0,
         "summary": "y" * 90, "start_time": "2026-09-17T09:00:00"},
    ])
    assert "y" * 59 + "…" in line
    assert "y" * 60 not in line


# ---- 5. One wording for an exit --------------------------------------------


def test_the_listing_reads_an_exit_the_way_fields_does(tmp_workspace: Path, capsys, wide):
    """`history`'s listing, `--fields` and `report` all go through `exit_label`,
    so a timeout is a timeout in all three rather than a bare 124 in one."""
    target = tmp_workspace / "box.htb"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "nmap/full", "command": "nmap", "exit_code": 124,
         "duration_sec": 30.0, "start_time": "2026-09-17T09:00:00.1", "artifacts": []},
        {"id": "02", "recipe": "nmap/full", "command": "nmap", "exit_code": 130,
         "interrupted": True, "duration_sec": 2.0,
         "start_time": "2026-09-17T09:01:00.1", "artifacts": []},
        {"id": "03", "recipe": "grep/find", "command": "grep", "exit_code": 1,
         "success": [0, 1], "duration_sec": 0.1,
         "start_time": "2026-09-17T09:02:00.1", "artifacts": []},
    ]), encoding="utf-8")

    assert _history(tmp_workspace) == 0
    listing = capsys.readouterr().out
    assert "exit 124 (timeout)" in listing
    assert "exit 130 (interrupted)" in listing
    assert "exit 1 (ok)" in listing
    # And the start time is the one `--fields` and `report` print, not the raw stamp.
    assert "2026-09-17 09:00:00" in listing and "2026-09-17T09:00:00" not in listing

    assert _history(tmp_workspace, "--fields") == 0
    _, rows = _table(capsys.readouterr().out)
    assert [row[2] for row in rows] == ["124 (timeout)", "130 (interrupted)", "1 (ok)"]


def test_a_field_value_is_one_line_and_never_none(tmp_workspace: Path, capsys, wide):
    """A field is raw tool output: a newline in it would break the row, and a
    JSON null is a value the tool did not give, not the word `None`."""
    target = tmp_workspace / "box.htb"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "ssh/banner", "command": "ssh", "exit_code": 0,
         "duration_sec": 0.2, "start_time": "2026-09-17T09:00:00.1", "artifacts": [],
         "fields": {"banner": "line one\nline two", "loss": None}},
    ]), encoding="utf-8")

    assert _history(tmp_workspace, "--fields") == 0
    out = capsys.readouterr().out
    names, rows = _table(out)

    assert names == ["#", "started", "exit", "banner", "loss"]
    assert rows == [["01", "2026-09-17 09:00:00", "0", "line one line two", ""]]
    # One row and one header, and nowhere the word a missing value is not.
    assert len([ln for ln in out.splitlines() if ln.startswith("  ")]) == 2
    assert "None" not in out


def test_a_very_long_field_is_clipped_to_the_cell(tmp_workspace: Path, capsys, wide):
    target = tmp_workspace / "box.htb"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "ssh/banner", "command": "ssh", "exit_code": 0,
         "duration_sec": 0.2, "start_time": "2026-09-17T09:00:00.1", "artifacts": [],
         "fields": {"banner": "z" * 200}},
    ]), encoding="utf-8")

    assert _history(tmp_workspace, "--fields") == 0
    out = capsys.readouterr().out
    assert "z" * 59 + "…" in out
    assert "z" * 60 not in out
