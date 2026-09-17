"""`--note` records why a run was made, and the surfaces that read the archive
show it back."""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

from fieldlog.cli import build_parser, handle_history, handle_run
from fieldlog.recipes import load_catalog
from fieldlog.report import render_report

NOTE = "asked to confirm the vpn route before the scan"


def _catalog(tmp_path: Path, yaml_text: str):
    """A catalog from one base file, with nothing else on disk scanned."""
    base = tmp_path / "base.yaml"
    base.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


RECIPES = """
    recipes:
      - id: say
        bin: echo
        presets:
          - id: hi
            flags: 'hello'
          - id: bye
            flags: 'goodbye'
    chains:
      - id: both
        steps:
          - say/hi
          - say/bye
    """


def _runs(workspace: Path) -> list:
    return json.loads((workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))


def _run(cat, workspace: Path, recipe: str, *extra: str) -> int:
    args = build_parser().parse_args(
        ["run", recipe, "-t", "10.0.0.1", "-w", str(workspace), "-q", *extra]
    )
    return handle_run(args, cat)


def test_a_run_record_carries_the_note(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, RECIPES)
    assert _run(cat, tmp_workspace, "say/hi", "--note", NOTE) == 0
    assert _runs(tmp_workspace)[-1]["note"] == NOTE


def test_a_run_without_the_flag_writes_no_note_key(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, RECIPES)
    assert _run(cat, tmp_workspace, "say/hi") == 0
    assert "note" not in _runs(tmp_workspace)[-1]


def test_a_chain_notes_the_chain_and_not_each_step(tmp_path: Path, tmp_workspace: Path):
    """The note is about the run the operator asked for, which is the chain."""
    cat = _catalog(tmp_path, RECIPES)
    assert _run(cat, tmp_workspace, "both", "--note", NOTE) == 0

    runs = _runs(tmp_workspace)
    summary = next(r for r in runs if r["recipe"] == "chain/both")
    steps = [r for r in runs if r["recipe"] != "chain/both"]
    assert summary["note"] == NOTE
    assert all("note" not in r for r in steps)


def test_history_prints_the_note_under_its_run(tmp_workspace: Path, capsys):
    target = tmp_workspace / "10.0.0.1"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "ping/quick", "exit_code": 0, "duration_sec": 1.0,
         "start_time": "2026-09-16T08:12:36", "note": NOTE, "artifacts": []},
    ]), encoding="utf-8")

    args = argparse.Namespace(
        target="10.0.0.1", target_flag="", workspace=str(tmp_workspace), json=False
    )
    assert handle_history(args) == 0
    assert NOTE in capsys.readouterr().out


def _report(tmp_path: Path, note: str) -> str:
    target = tmp_path / "10.0.0.1"
    target.mkdir(parents=True, exist_ok=True)
    return render_report(target, [{
        "id": "01", "recipe": "ping/quick", "command": "ping -c 4 10.0.0.1",
        "exit_code": 0, "duration_sec": 1.0, "start_time": "2026-09-16T08:12:36",
        "summary": "4 replies · 0% loss", "note": note, "artifact_log": "", "artifacts": [],
    }])


def test_report_quotes_the_note(tmp_path: Path):
    markdown = _report(tmp_path, NOTE)
    assert f"> {NOTE}" in markdown
    # It reads under the summary, where the run's own words belong.
    assert markdown.index("Summary:") < markdown.index(f"> {NOTE}")


def test_a_multi_line_note_stays_one_quote(tmp_path: Path):
    markdown = _report(tmp_path, "first line\n\nsecond line")
    quoted = [line for line in markdown.splitlines() if line.startswith(">")]
    assert quoted == ["> first line", ">", "> second line"]


def test_a_blank_note_is_not_stored(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, RECIPES)
    assert _run(cat, tmp_workspace, "say/hi", "--note", "   ") == 0
    assert "note" not in _runs(tmp_workspace)[-1]


def test_a_note_is_stored_without_its_surrounding_space(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, RECIPES)
    assert _run(cat, tmp_workspace, "say/hi", "--note", f"  {NOTE}  ") == 0
    assert _runs(tmp_workspace)[-1]["note"] == NOTE
