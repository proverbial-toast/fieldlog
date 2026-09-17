"""`run --json` prints the run record, not a second rendering of it.

README says so, and the chain path always did. The single-run path used to
hand-build a smaller dict with different key names — `primary_log` for what the
archive calls `artifact_log`, and no times, environment, summary or verdict.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from fieldlog.cli import build_parser, handle_run
from fieldlog.recipes import load_catalog


def _catalog(tmp_path: Path, yaml_text: str):
    """A catalog from one base file, with nothing else on disk scanned."""
    base = tmp_path / "base.yaml"
    base.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


def test_the_json_a_run_prints_is_the_record_it_archived(tmp_path: Path, tmp_workspace: Path, capsys):
    cat = _catalog(tmp_path, """
        recipes:
          - id: say
            bin: echo
            presets:
              - id: hi
                flags: 'hello'
        """)
    args = build_parser().parse_args(
        ["run", "say/hi", "-t", "10.0.0.1", "-w", str(tmp_workspace), "--json"]
    )
    assert handle_run(args, cat) == 0

    printed = json.loads(capsys.readouterr().out)
    archived = json.loads((tmp_workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))[-1]
    assert printed == archived
    assert printed["artifact_log"] and "primary_log" not in printed


def test_a_run_that_never_reached_the_archive_says_so(tmp_path: Path, tmp_workspace: Path, capsys, monkeypatch):
    """The record is what the archive holds. When the write fails there is no
    record, and the short error shape is all a consumer gets."""
    import fieldlog.runner as runner_mod

    def boom(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(runner_mod, "append_record", boom)

    cat = _catalog(tmp_path, """
        recipes:
          - id: say
            bin: echo
            presets:
              - id: hi
                flags: 'hello'
        """)
    args = build_parser().parse_args(
        ["run", "say/hi", "-t", "10.0.0.1", "-w", str(tmp_workspace), "--json"]
    )
    assert handle_run(args, cat) == 127            # the execution-error code

    printed = json.loads(capsys.readouterr().out)
    assert printed["error"] is True
    assert "artifact_log" not in printed and "artifacts" not in printed
    assert not (tmp_workspace / "10.0.0.1" / "session.json").exists()
