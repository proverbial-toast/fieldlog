"""Chains: loading and validation, the driver's stop policy, the CLI and the TUI."""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from fieldlog.recipes import chain_blocked, find_chain, load_catalog
from fieldlog.state import TargetSession

# `true` and `false` are real binaries, so a step's exit code is the point of the
# tool; `gone` is the one that is genuinely missing from $PATH.
TOOLS = """
recipes:
  - id: ok
    bin: true
    presets:
      - id: x
        flags: ""
      - id: y
        flags: ""
  - id: bad
    bin: false
    presets:
      - id: x
        flags: ""
  - id: gone
    bin: fieldlog-no-such-binary
    presets:
      - id: x
        flags: ""
"""


def _catalog(root: Path, chains_yaml: str = "", dropins: dict | None = None):
    """A catalog of TOOLS plus `chains_yaml`, from one base file plus optional
    drop-ins, with nothing else on disk scanned."""
    root.mkdir(parents=True, exist_ok=True)
    base = root / "base.yaml"
    base.write_text(TOOLS + textwrap.dedent(chains_yaml), encoding="utf-8")
    dropin_dir = root / "recipes.d"
    dropin_dir.mkdir(exist_ok=True)
    for name, text in (dropins or {}).items():
        (dropin_dir / name).write_text(textwrap.dedent(text), encoding="utf-8")
    return load_catalog(base=base, dropin_dir=dropin_dir)


# ---- 1. Loading ------------------------------------------------------------


def test_chain_steps_normalise_and_resolve(tmp_path: Path):
    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok
              - recipe: bad/x
                continue: true
        """)
    chain = find_chain(cat, "c1")
    assert chain is not None
    assert chain["steps"] == [
        {"recipe": "ok/x", "continue": False},   # bare tool id resolved to its first preset
        {"recipe": "bad/x", "continue": True},
    ]
    assert chain["name"] == "c1"                 # defaults to the id
    assert chain["category"] == "Chains"
    assert cat.errors == []


def test_chain_with_an_unknown_step_is_dropped(tmp_path: Path):
    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok/x
              - ok/nope
        """)
    assert cat.chains == []
    assert any("c1" in e and "ok/nope" in e for e in cat.errors), cat.errors


def test_chain_id_colliding_with_a_tool_is_dropped(tmp_path: Path):
    cat = _catalog(tmp_path, """
        chains:
          - id: ok
            steps:
              - ok/x
        """)
    assert cat.chains == []
    assert any("chain ok" in e for e in cat.errors), cat.errors


def test_dropin_overrides_a_chain_and_says_so(tmp_path: Path):
    cat = _catalog(
        tmp_path,
        """
        chains:
          - id: c1
            steps:
              - ok/x
        """,
        dropins={"x.yaml": """
        chains:
          - id: c1
            name: "from the drop-in"
            steps:
              - ok/y
        """},
    )
    chain = find_chain(cat, "c1")
    assert chain["name"] == "from the drop-in"
    assert [s["recipe"] for s in chain["steps"]] == ["ok/y"]
    assert "chain c1 overridden by recipes.d/x.yaml" in cat.overrides


# ---- 2. Blocking -----------------------------------------------------------


def test_chain_blocked_names_the_first_blocked_step(tmp_path: Path):
    cat = _catalog(tmp_path, """
        chains:
          - id: broken
            steps:
              - ok/x
              - gone/x
              - ok/y
          - id: fine
            steps:
              - ok/x
              - ok/y
        """)
    session = TargetSession(target="10.0.0.1")

    blocked, reason, hint = chain_blocked(cat, find_chain(cat, "broken"), session)
    assert blocked and reason.startswith("step 2 gone/x: ") and "not found in $PATH" in reason
    assert hint

    assert chain_blocked(cat, find_chain(cat, "fine"), session) == (False, "2 steps ready", "")


# ---- 3-5. CLI --------------------------------------------------------------


def _run_args(chain_id: str, workspace: Path, *extra: str):
    from fieldlog.cli import build_parser

    return build_parser().parse_args(
        ["run", chain_id, "-t", "10.0.0.1", "-w", str(workspace), *extra]
    )


def test_cli_dry_run_previews_every_step_and_touches_nothing(tmp_path: Path, tmp_workspace: Path, capsys):
    from fieldlog.cli import handle_run

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok/x
              - ok/y
        """)
    assert handle_run(_run_args("c1", tmp_workspace, "--dry-run"), cat) == 0
    out = capsys.readouterr().out
    assert "step 1/2" in out and "step 2/2" in out
    assert list(tmp_workspace.iterdir()) == []


def _session_runs(workspace: Path) -> list:
    return json.loads((workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))


def test_cli_chain_stops_at_the_first_failure(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.cli import handle_run

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok/x
              - bad/x
              - ok/y
        """)
    assert handle_run(_run_args("c1", tmp_workspace, "-q"), cat) == 1

    runs = _session_runs(tmp_workspace)
    steps = [r for r in runs if r["recipe"] != "chain/c1"]
    assert [(r["id"], r["recipe"], r["exit_code"]) for r in steps] == [
        ("01", "ok/x", 0),
        ("02", "bad/x", 1),
    ]

    chain_record = next(r for r in runs if r["recipe"] == "chain/c1")
    assert chain_record["stopped_at"] == "bad/x"
    assert chain_record["exit_code"] == 1
    assert len(chain_record["steps"]) == 2
    # One $OUTDIR for the whole chain, summary record included.
    assert {r["out_dir"] for r in steps} == {chain_record["out_dir"]}
    assert [r["chain"]["step"] for r in steps] == [1, 2]


def test_cli_chain_continues_past_a_failure_when_told_to(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.cli import handle_run

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok/x
              - recipe: bad/x
                continue: true
              - ok/y
        """)
    assert handle_run(_run_args("c1", tmp_workspace, "-q"), cat) == 1

    runs = _session_runs(tmp_workspace)
    chain_record = next(r for r in runs if r["recipe"] == "chain/c1")
    assert [s["recipe"] for s in chain_record["steps"]] == ["ok/x", "bad/x", "ok/y"]
    assert chain_record["stopped_at"] is None
    assert chain_record["exit_code"] == 1


def test_chain_summary_times_are_local_like_its_steps(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.cli import handle_run

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok/x
        """)
    assert handle_run(_run_args("c1", tmp_workspace, "-q"), cat) == 0

    runs = _session_runs(tmp_workspace)
    step = next(r for r in runs if r["recipe"] == "ok/x")
    chain_record = next(r for r in runs if r["recipe"] == "chain/c1")
    for stamp in (chain_record["start_time"], chain_record["end_time"]):
        assert "+" not in stamp and "Z" not in stamp
    # One clock: the summary spans its step instead of sitting an offset away.
    assert chain_record["start_time"] <= step["start_time"] <= chain_record["end_time"]


def test_report_reads_an_old_utc_chain_stamp_in_local_time():
    from datetime import datetime, timezone

    from fieldlog.report import format_time

    utc = datetime(2026, 9, 13, 21, 8, 35, tzinfo=timezone.utc)
    assert format_time(utc.isoformat()) == utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert format_time("2026-09-13T22:08:35.123456") == "2026-09-13 22:08:35"


# ---- 6. list ---------------------------------------------------------------


def test_cli_list_reports_chains(tmp_path: Path, capsys):
    from fieldlog.cli import build_parser, handle_list

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            name: "two steps"
            steps:
              - ok/x
              - recipe: ok/y
                continue: true
        """)

    assert handle_list(build_parser().parse_args(["list", "--json"]), cat) == 0
    entries = json.loads(capsys.readouterr().out)
    assert all(e["kind"] == "tool" for e in entries if e["id"] != "c1")
    chain_entry = next(e for e in entries if e["kind"] == "chain")
    assert chain_entry["id"] == "c1" and chain_entry["name"] == "two steps"
    assert chain_entry["steps"] == [
        {"recipe": "ok/x", "continue": False},
        {"recipe": "ok/y", "continue": True},
    ]

    assert handle_list(build_parser().parse_args(["list", "-q"]), cat) == 0
    assert "c1" in capsys.readouterr().out.splitlines()


# ---- 7. TUI ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_opens_a_tab_per_chain_step(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.app import FieldlogApp

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - ok/x
              - ok/y
        """)
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app.catalog = cat
        app._recipes = app.catalog.tools
        app.select_chain("c1")
        assert app.selected_chain_id == "c1"

        app.action_run_task()
        await app.workers.wait_for_complete()
        await pilot.pause()

        job_tabs = [t for t in app.tabs if t.id != "system"]
        assert [t.label for t in job_tabs] == ["true/x #01", "true/y #02"]

    runs = _session_runs(tmp_workspace)
    chain_record = next(r for r in runs if r["recipe"] == "chain/c1")
    assert chain_record["stopped_at"] is None and chain_record["exit_code"] == 0
    assert [s["recipe"] for s in chain_record["steps"]] == ["ok/x", "ok/y"]


# ---- 8. Interrupt ----------------------------------------------------------


def test_an_interrupt_stops_a_chain_even_on_a_continue_step(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.chain import run_chain

    cat = _catalog(tmp_path, """
        chains:
          - id: c1
            steps:
              - recipe: ok/x
                continue: true
              - ok/y
        """)
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)

    async def run_step(plan) -> int:
        plan.job.interrupted = True      # Ctrl+C landed on this step
        return 0

    result = asyncio.run(
        run_chain(session, cat, find_chain(cat, "c1"), run_step=run_step)
    )
    assert result.exit_code == 130
    assert [s["recipe"] for s in result.record["steps"]] == ["ok/x"]
    assert result.record["stopped_at"] == "ok/x"
