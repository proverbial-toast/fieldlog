"""`fieldlog note <target> "text"`: the operator's own words, in the archive.

A note is a record of its own — a run number, a timestamp and the text — and
never an edit of a record already written, which is what keeps the archive
append-only in fact rather than in spirit. `history` and `report` show it in
the timeline, in its place among the runs it was written between.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fieldlog.archive import append_note
from fieldlog.cli import build_parser, dispatch_argv, handle_note, subcommand_hint
from fieldlog.report import record_kind, render_report

NOTE = "customer confirmed the outage at 14:10"


def _runs(target_dir: Path) -> list:
    return json.loads((target_dir / "session.json").read_text(encoding="utf-8"))


def _note(workspace: Path, target: str, text: str, *extra: str) -> int:
    args = build_parser().parse_args(["note", target, text, "-w", str(workspace), *extra])
    return handle_note(args)


def _archive(workspace: Path, folder: str, target: str, host: str = "") -> Path:
    """A folder with one run in it, made against `target` — what the lookup by
    target has to find."""
    d = workspace / folder
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps([{
        "id": "01", "recipe": "ping/quick", "command": "ping -c 1", "exit_code": 0,
        "duration_sec": 0.1, "start_time": "2026-09-17T09:00:00", "artifacts": [],
        "environment": {"TARGET": target, "TARGET_IP": target, "TARGET_HOST": host,
                        "LHOST": "", "IFACE": "eth0", "OUT_DIR": "", "RUN_ID": "01"},
    }]), encoding="utf-8")
    return d


# ---- 1. The subcommand -----------------------------------------------------


def test_note_is_a_subcommand_not_a_recipe():
    # Any unrecognised first token becomes `run <token>`; `note` must not.
    assert dispatch_argv(["note", "box.htb", "text"]) == ("cli", ["note", "box.htb", "text"])
    assert dispatch_argv(["nmap", "10.0.0.1"]) == ("cli", ["run", "nmap", "10.0.0.1"])


def test_the_typo_nudge_knows_the_new_subcommand():
    # `note` joined SUBCOMMANDS, so a near miss for it now nudges too.
    assert subcommand_hint("not") == " Did you mean `fieldlog note`?"
    # And the nudges that were there before are unchanged.
    assert subcommand_hint("hisory") == " Did you mean `fieldlog history`?"
    # A real tool id is not a fat-fingered subcommand.
    assert subcommand_hint("nmap") == ""


# ---- 2. The record ---------------------------------------------------------


def test_a_note_takes_the_next_run_number(tmp_workspace: Path):
    target = _archive(tmp_workspace, "box.htb", "10.0.0.1")
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "ping/quick", "command": "ping", "exit_code": 0},
        {"id": "02", "recipe": "ping/quick", "command": "ping", "exit_code": 0},
    ]), encoding="utf-8")

    assert append_note(target, NOTE)["id"] == "03"
    # And the counter moves on, as it does for a run.
    assert append_note(target, "and another")["id"] == "04"


def test_the_record_is_the_text_a_number_and_a_time(tmp_workspace: Path):
    when = 1_600_000_000.0
    record = append_note(tmp_workspace / "box.htb", NOTE, when=when)

    assert set(record) == {"id", "recipe", "note", "start_time"}
    assert record["id"] == "01"
    assert record["recipe"] == "note"
    assert record["note"] == NOTE
    # Naive local time, exactly as a run record stores it.
    assert record["start_time"] == datetime.fromtimestamp(when).isoformat()
    # What was returned is what was written, and nothing else was.
    assert _runs(tmp_workspace / "box.htb") == [record]


def test_record_kind_tells_the_three_apart():
    assert record_kind({"id": "01", "recipe": "ping/quick", "command": "ping -c 4"}) == "run"
    assert record_kind({"id": "02", "recipe": "chain/reach", "command": "chain reach",
                        "steps": [{"recipe": "ping/quick"}]}) == "chain"
    assert record_kind({"id": "03", "recipe": "note", "note": NOTE}) == "note"
    # A recipe that happens to be called `note` ran a command; a note never does.
    assert record_kind({"id": "04", "recipe": "note", "command": "note --take", "exit_code": 0}) == "run"
    # A chain that ran no step has no step table to render.
    assert record_kind({"id": "05", "recipe": "chain/reach", "command": "chain reach", "steps": []}) == "run"


# ---- 3. The command --------------------------------------------------------


def test_an_unknown_target_starts_its_folder(tmp_workspace: Path, capsys):
    # "starting on 192.168.1.0/24" comes before the first run against it.
    assert _note(tmp_workspace, "192.168.1.0/24", NOTE) == 0

    folder = tmp_workspace / "192.168.1.0_24"
    assert [r["note"] for r in _runs(folder)] == [NOTE]
    assert capsys.readouterr().out.strip() == f"note #01 · {folder.name}"


def test_the_target_the_runs_were_made_against_finds_the_folder(tmp_workspace: Path):
    box = _archive(tmp_workspace, "box.htb", "10.0.0.1", "box.htb")

    assert _note(tmp_workspace, "10.0.0.1", NOTE) == 0

    assert _runs(box)[-1]["note"] == NOTE
    # The address must not start a second folder beside the one it named.
    assert not (tmp_workspace / "10.0.0.1").exists()


def test_a_blank_note_is_refused_and_writes_nothing(tmp_workspace: Path, capsys):
    assert _note(tmp_workspace, "box.htb", "   ") == 1
    assert capsys.readouterr().err == "Error: a note needs some text.\n"
    assert list(tmp_workspace.iterdir()) == []


def test_a_note_without_a_target_is_refused_and_writes_nothing(tmp_workspace: Path, capsys):
    # An empty name would resolve to the workspace root, which is not a target
    # folder: the note would land where no reader ever looks for it.
    assert _note(tmp_workspace, "", NOTE) == 1
    assert capsys.readouterr().err == "Error: a note needs a target.\n"
    assert list(tmp_workspace.iterdir()) == []


def test_a_folder_that_cannot_be_written_is_an_error_not_a_traceback(
    tmp_workspace: Path, capsys
):
    # A regular file sitting where the target folder belongs.
    (tmp_workspace / "box.htb").write_text("not a folder", encoding="utf-8")

    assert _note(tmp_workspace, "box.htb", NOTE) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"Error: could not write the note to {tmp_workspace / 'box.htb'}: ")
    assert err.endswith("\n") and "Traceback" not in err


def test_the_text_is_stored_without_its_surrounding_space(tmp_workspace: Path):
    assert _note(tmp_workspace, "box.htb", f"  {NOTE}  ") == 0
    assert _runs(tmp_workspace / "box.htb")[-1]["note"] == NOTE


def test_json_prints_the_record_that_was_written(tmp_workspace: Path, capsys):
    assert _note(tmp_workspace, "box.htb", NOTE, "--json") == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == _runs(tmp_workspace / "box.htb")[-1]


# ---- 4. The report ---------------------------------------------------------


RUN = {
    "id": "01", "recipe": "ping/quick", "command": "ping -c 4 10.0.0.1", "exit_code": 0,
    "duration_sec": 1.2, "start_time": "2026-09-17T09:00:00", "artifacts": [],
}


def test_the_report_gives_a_note_a_row_and_a_section(tmp_workspace: Path):
    out = render_report(tmp_workspace / "box.htb", [
        RUN,
        {"id": "02", "recipe": "note", "note": NOTE, "start_time": "2026-09-17T14:12:03.123456"},
    ])

    # Nothing ran: no code, no duration, nothing to count — the note itself is
    # the summary.
    assert f"| 02 | note | 2026-09-17 14:12:03 | — | — | — | {NOTE} |" in out
    assert "## #02 · note · 2026-09-17 14:12:03" in out
    assert f"> {NOTE}" in out

    section = out.split("## #02 · note · ")[1]
    assert "```" not in section        # no command block, no output block
    assert "Started" not in section and "Summary:" not in section


def test_a_multi_line_note_is_one_line_in_the_table_and_whole_in_its_section(tmp_workspace: Path):
    out = render_report(tmp_workspace / "box.htb", [
        {"id": "01", "recipe": "note", "note": "first line\n\nsecond line",
         "start_time": "2026-09-17T14:12:03"},
    ])

    table, section = out.split("## #01 · note · ")
    assert "| first line |" in table
    assert "second line" not in table
    assert [ln for ln in section.splitlines() if ln.startswith(">")] == [
        "> first line", ">", "> second line",
    ]
