"""`success:` says which exit codes a recipe calls a success.

A tool that reports "nothing found" as 1, or a `--help` that exits 2, has not
failed. The record keeps the tool's own code either way; what changes is the
verdict every reader draws from it.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from fieldlog.recipes import load_catalog, run_succeeded, success_codes


def _catalog(tmp_path: Path, yaml_text: str):
    base = tmp_path / "base.yaml"
    base.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


def _run(cat, recipe: str, workspace: Path) -> int:
    from fieldlog.cli import build_parser, handle_run

    return handle_run(
        build_parser().parse_args(
            ["run", recipe, "-t", "10.0.0.1", "-w", str(workspace), "-q"]
        ),
        cat,
    )


def _runs(workspace: Path) -> list:
    return json.loads((workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))


# ---- 1. The rule -----------------------------------------------------------


def test_the_default_is_zero_alone():
    assert run_succeeded(0) is True
    assert run_succeeded(1) is False
    assert success_codes({"id": "x"}) is None


def test_a_declared_code_counts_as_a_success():
    assert run_succeeded(1, [0, 1]) is True
    assert run_succeeded(2, [0, 1]) is False
    # Nothing is implied: a recipe listing only 1 has said only 1.
    assert run_succeeded(0, [1]) is False


def test_a_code_that_is_not_a_number_never_succeeds():
    assert run_succeeded(None) is False
    assert run_succeeded("x", [0]) is False


# ---- 2. Load-time validation ----------------------------------------------


@pytest.mark.parametrize("value", ["true", "'0,1'", "[0, 999]", "[]", "[0, 'x']", "-1"])
def test_a_success_that_is_not_an_exit_code_is_dropped(tmp_path: Path, value: str):
    cat = _catalog(tmp_path, f"""
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: "-v"
                success: {value}
        """)
    preset = cat.tools[0]["presets"][0]
    assert "success" not in preset        # the declaration is dropped
    assert preset["flags"] == "-v"        # the preset still runs
    assert any("ok/x success:" in e for e in cat.errors), cat.errors


def test_a_single_code_is_normalised_to_a_list(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: ""
                success: 2
        """)
    assert cat.errors == []
    assert success_codes(cat.tools[0]["presets"][0]) == [2]


# ---- 3. The run ------------------------------------------------------------


CATALOG = """
    recipes:
      - id: nomatch
        bin: false                    # always exits 1
        presets:
          - id: ok
            flags: ""
            success: [0, 1]
          - id: strict
            flags: ""
      - id: done
        bin: true
        presets:
          - id: x
            flags: ""
    chains:
      - id: c1
        steps:
          - nomatch/ok
          - done/x
    """


def test_a_declared_success_makes_fieldlog_exit_zero(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CATALOG)

    assert _run(cat, "nomatch/ok", tmp_workspace) == 0      # 1 is declared a success
    assert _run(cat, "nomatch/strict", tmp_workspace) == 1  # same tool, no declaration

    declared, strict = _runs(tmp_workspace)
    # The archive keeps the tool's own code in both cases; only the verdict moved.
    assert [declared["exit_code"], strict["exit_code"]] == [1, 1]
    assert declared["success"] == [0, 1]
    assert "success" not in strict


def test_a_chain_walks_past_a_declared_success(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CATALOG)
    assert _run(cat, "c1", tmp_workspace) == 0

    chain = next(r for r in _runs(tmp_workspace) if r["recipe"] == "chain/c1")
    assert [s["recipe"] for s in chain["steps"]] == ["nomatch/ok", "done/x"]
    assert chain["stopped_at"] is None
    assert chain["exit_code"] == 0


# ---- 4. The readers --------------------------------------------------------


def test_the_report_does_not_flag_a_declared_success(tmp_workspace: Path):
    from fieldlog.report import render_report

    out = render_report(tmp_workspace / "10.0.0.1", [
        {"id": "01", "recipe": "grep/find", "exit_code": 1, "success": [0, 1]},
        {"id": "02", "recipe": "grep/find", "exit_code": 2, "success": [0, 1]},
    ])
    assert "| 1 (ok) |" in out              # inside the rule: not a failure
    assert "| **2** |" in out               # outside it: still bold
    assert "## #01 · grep/find · exit 1 (ok)" in out


def test_a_record_without_the_key_still_reads_as_before(tmp_workspace: Path):
    from fieldlog.report import record_ok

    assert record_ok({"exit_code": 0}) is True
    assert record_ok({"exit_code": 1}) is False
    # A junk value is ignored rather than trusted.
    assert record_ok({"exit_code": 1, "success": "everything"}) is False


@pytest.mark.asyncio
async def test_the_tui_tab_reads_a_declared_success_as_done(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession

    cat = _catalog(tmp_path, CATALOG)
    tool = next(t for t in cat.tools if t["id"] == "nomatch")
    presets = {p["id"]: p for p in tool["presets"]}

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app.catalog = cat
        app._recipes = app.catalog.tools
        app._spawn_job(tool, presets["ok"], "nomatch/ok")
        app._spawn_job(tool, presets["strict"], "nomatch/strict")
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Same binary, same exit 1; only the declaration differs.
        assert [t.status for t in app.tabs if t.id != "system"] == ["done", "failed"]


def test_history_says_ok_rather_than_relying_on_colour(tmp_workspace: Path, capsys):
    import argparse

    from fieldlog.cli import handle_history

    target = tmp_workspace / "10.0.0.1"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "grep/find", "exit_code": 1, "success": [0, 1],
         "duration_sec": 0.1, "start_time": "2026-09-16T08:30:41", "artifacts": []},
        {"id": "02", "recipe": "grep/find", "exit_code": 1, "duration_sec": 0.1,
         "start_time": "2026-09-16T08:30:42", "artifacts": []},
    ]), encoding="utf-8")

    args = argparse.Namespace(
        target="10.0.0.1", target_flag="", workspace=str(tmp_workspace), json=False
    )
    assert handle_history(args) == 0
    out = capsys.readouterr().out
    assert "exit 1 (ok)" in out
    assert out.count("(ok)") == 1
