"""`expect:` is a regex the finished log has to match for the run to pass.

An exit code is not a verdict for every tool: `openssl s_client` reports a chain
it could not verify and exits 0 either way. A run passes when its code is one
the recipe declared a success *and* its expectation, when it has one, was found.
The record keeps the tool's own code regardless; what changes is the reading.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from fieldlog.recipes import expect_found, expect_rule, load_catalog, run_passed

LOG = """\
CONNECTED(00000003)
depth=0 CN = box.htb
Verify return code: 21 (unable to verify the first certificate)
closed
"""


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


def test_a_preset_without_a_rule_expects_nothing():
    assert expect_rule({"id": "x", "flags": ""}) is None
    assert expect_rule({"id": "x", "expect": ""}) is None
    # No rule is no claim, which is not the same as a failed one.
    assert expect_found(None, LOG) is None
    assert expect_found("", LOG) is None


def test_the_pattern_is_looked_for_anywhere_in_the_log():
    # The line that matters is in the middle of the log, not at either end.
    assert expect_found(r"Verify return code: 21", LOG) is True
    assert expect_found(r"^depth=0", LOG) is True        # MULTILINE, as parse: is
    assert expect_found(r"Verify return code: 0 \(ok\)", LOG) is False


def test_a_pattern_that_cannot_compile_makes_no_claim():
    assert expect_found("(unclosed", LOG) is None


@pytest.mark.parametrize("code, codes, found, passed", [
    (0, None, None, True),        # the old world: exit 0, nothing expected
    (0, None, False, False),      # the tool was happy, the run was not
    (0, None, True, True),
    (1, [0, 1], False, False),    # a declared success still has to meet its rule
    (1, [0, 1], True, True),
    (1, None, None, False),
])
def test_a_run_passes_on_its_code_and_its_expectation(code, codes, found, passed):
    assert run_passed(code, codes, found) is passed


# ---- 2. Load-time validation ----------------------------------------------


def test_a_bad_regex_costs_the_check_not_the_recipe(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: "-v"
                expect: '(unclosed'
        """)
    preset = cat.tools[0]["presets"][0]
    assert "expect" not in preset         # the rule is dropped
    assert preset["flags"] == "-v"        # the preset still runs
    assert any("ok/x expect: invalid regex" in e for e in cat.errors), cat.errors


def test_a_sound_rule_survives_the_load(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: ""
                expect: 'return code: 0'
        """)
    assert cat.errors == []
    assert expect_rule(cat.tools[0]["presets"][0]) == "return code: 0"


def test_an_empty_rule_is_dropped_without_a_word(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: ""
                expect: ''
        """)
    # Nothing was written, so there is nothing to report and nothing to check.
    assert cat.errors == []
    assert "expect" not in cat.tools[0]["presets"][0]


# ---- 3. The run ------------------------------------------------------------


CATALOG = """
    recipes:
      - id: say
        bin: echo
        presets:
          - id: met
            flags: "hello world"
            expect: hello
          - id: missed
            flags: "hello world"
            expect: nope
          - id: plain
            flags: "hello world"
    """


def test_a_met_expectation_leaves_the_run_as_it_was(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CATALOG)
    assert _run(cat, "say/met", tmp_workspace) == 0

    record = _runs(tmp_workspace)[-1]
    assert record["expect"] == {"pattern": "hello", "found": True}
    assert record["exit_code"] == 0


def test_a_missed_expectation_fails_a_run_the_tool_called_fine(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CATALOG)
    # echo exits 0 and always will; the verdict is fieldlog's, not the tool's.
    assert _run(cat, "say/missed", tmp_workspace) == 1

    record = _runs(tmp_workspace)[-1]
    assert record["expect"] == {"pattern": "nope", "found": False}
    assert record["exit_code"] == 0       # the archive never rewrites the code


def test_the_run_status_line_says_what_was_missed(
    tmp_path: Path, tmp_workspace: Path, capsys, monkeypatch
):
    """The code says the tool was happy; the line has to say the run was not,
    or an exit status of 1 on a `[FAIL:0]` reads as a fieldlog bug."""
    from fieldlog.cli import build_parser, handle_run

    monkeypatch.setenv("COLUMNS", "200")        # rich wraps a piped line at 80
    cat = _catalog(tmp_path, CATALOG)
    # Not quiet: the status line is what `-q` suppresses.
    assert handle_run(
        build_parser().parse_args(["run", "say/missed", "-t", "10.0.0.1", "-w", str(tmp_workspace)]),
        cat,
    ) == 1
    assert "[FAIL:0] (expect not met)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_an_interrupted_run_makes_no_claim_about_its_expectation(
    tmp_workspace: Path, capsys, monkeypatch
):
    """The tool never got to print its closing line, so there is nothing to
    judge. A `false` here would archive an operator's Ctrl+C as a failed check,
    and every reader would then repeat it."""
    import asyncio

    from fieldlog.cli import build_parser, handle_history
    from fieldlog.launch import plan_launch
    from fieldlog.report import render_report
    from fieldlog.runner import interrupt_job, run_job
    from fieldlog.state import TargetSession

    monkeypatch.setenv("COLUMNS", "200")
    session = TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace)
    plan = plan_launch(
        session, {"id": "sh", "bin": "sh"},
        {"id": "t", "flags": "-c 'echo start; sleep 5'", "expect": "never"},
    )
    # run_job sets job.process early, so the timer finds a job to signal.
    asyncio.get_running_loop().call_later(0.3, interrupt_job, plan.job)
    await run_job(plan.command, plan.job, session, lambda t, s: None, env=plan.env)

    assert plan.job.interrupted is True
    assert plan.job.expect_found is None
    record = _runs(tmp_workspace)[-1]
    # The pattern is still recorded — what was checked — with no verdict on it.
    assert record["expect"] == {"pattern": "never", "found": None}

    for extra in ([], ["--fields"]):
        assert handle_history(build_parser().parse_args(
            ["history", "10.0.0.1", "-w", str(tmp_workspace), *extra])) == 0
        out = capsys.readouterr().out
        assert "expect not met" not in out
        assert "(interrupted)" in out

    report = render_report(tmp_workspace / "10.0.0.1", _runs(tmp_workspace))
    assert "expect not met" not in report
    assert "Expected:" not in report          # nothing was checked, so nothing missed


def test_a_preset_without_a_rule_writes_no_expect_key(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CATALOG)
    assert _run(cat, "say/plain", tmp_workspace) == 0
    assert "expect" not in _runs(tmp_workspace)[-1]


# ---- 4. Chains -------------------------------------------------------------


CHAIN_CATALOG = """
    recipes:
      - id: say
        bin: echo
        presets:
          - id: bad
            flags: "hello world"
            parse: '(?P<w>hello)'
            summary: "saw {w}"
            expect: nope
      - id: ok
        bin: true
        presets:
          - id: x
            flags: ""
    chains:
      - id: stops
        steps:
          - say/bad
          - ok/x
      - id: walks
        steps:
          - recipe: say/bad
            continue: true
          - ok/x
    """


def test_a_chain_stops_at_a_missed_expectation(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CHAIN_CATALOG)
    assert _run(cat, "stops", tmp_workspace) == 1

    chain = next(r for r in _runs(tmp_workspace) if r["recipe"] == "chain/stops")
    assert chain["stopped_at"] == "say/bad"
    # The step exited 0, so the chain cannot claim 0 for a chain that failed.
    assert chain["exit_code"] == 1
    assert [s["recipe"] for s in chain["steps"]] == ["say/bad"]

    step = chain["steps"][0]
    assert step["exit_code"] == 0
    assert step["expect"] == {"pattern": "nope", "found": False}
    assert step["summary"] == "saw hello"


def test_a_chain_walks_past_a_missed_expectation_when_told_to(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, CHAIN_CATALOG)
    assert _run(cat, "walks", tmp_workspace) == 1

    chain = next(r for r in _runs(tmp_workspace) if r["recipe"] == "chain/walks")
    assert [s["recipe"] for s in chain["steps"]] == ["say/bad", "ok/x"]
    assert chain["stopped_at"] is None
    # Walked past, not forgiven: the chain still reports the failure.
    assert chain["exit_code"] == 1


def test_the_cli_stop_line_names_the_missed_expectation(
    tmp_path: Path, tmp_workspace: Path, capsys, monkeypatch
):
    """A step that exited 0 and stopped the chain reads as a fieldlog bug
    unless the line says what it was that failed."""
    from fieldlog.cli import build_parser, handle_run

    monkeypatch.setenv("COLUMNS", "200")        # rich wraps a piped line at 80
    cat = _catalog(tmp_path, CHAIN_CATALOG)
    # Not quiet: the stop line is the summary `-q` suppresses.
    assert handle_run(
        build_parser().parse_args(["run", "stops", "-t", "10.0.0.1", "-w", str(tmp_workspace)]),
        cat,
    ) == 1
    assert "stopped at step 1 (say/bad exit 0, expect not met)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_the_tui_stop_line_names_the_missed_expectation(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession

    cat = _catalog(tmp_path, CHAIN_CATALOG)
    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app.catalog = cat
        app._recipes = app.catalog.tools
        app.select_chain("stops")
        app.action_run_task()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # The one wording, the same one the CLI prints (report.chain_outcome).
        stopped = [line for line in app.system_log_lines if "stopped at step" in line]
        assert stopped == ["[chain] stops · stopped at step 1 (say/bad exit 0, expect not met)"]


SUMMARY_CATALOG = """
    recipes:
      - id: say
        bin: echo
        presets:
          - id: one
            flags: "hello"
            parse: '(?P<w>\\S+)'
            summary: "{w}"
          - id: two
            flags: "goodbye"
            parse: '(?P<w>\\S+)'
            summary: "{w}"
          - id: mute
            flags: "nothing to parse"
    chains:
      - id: both
        steps:
          - say/one
          - say/two
      - id: half
        steps:
          - say/one
          - say/mute
      - id: silent
        steps:
          - say/mute
    """


def test_a_chain_summary_joins_its_steps(tmp_path: Path, tmp_workspace: Path):
    cat = _catalog(tmp_path, SUMMARY_CATALOG)
    for chain_id in ("both", "half", "silent"):
        assert _run(cat, chain_id, tmp_workspace) == 0

    runs = _runs(tmp_workspace)
    both = next(r for r in runs if r["recipe"] == "chain/both")
    half = next(r for r in runs if r["recipe"] == "chain/half")
    silent = next(r for r in runs if r["recipe"] == "chain/silent")

    assert both["summary"] == "say: hello → say: goodbye"
    # A step without a rule contributes nothing rather than an empty slot.
    assert half["summary"] == "say: hello"
    # And a chain that found nothing to say carries no key, as a run does not.
    assert "summary" not in silent


# ---- 5. The readers --------------------------------------------------------


def test_record_ok_reads_the_expectation_beside_the_code():
    from fieldlog.report import record_ok

    assert record_ok({"exit_code": 0, "expect": {"pattern": "x", "found": False}}) is False
    assert record_ok({"exit_code": 0, "expect": {"pattern": "x", "found": True}}) is True
    # Junk is ignored rather than trusted, as a junk `success` is.
    assert record_ok({"exit_code": 0, "expect": "yes"}) is True
    assert record_ok({"exit_code": 0}) is True


def test_exit_label_says_what_was_missed():
    from fieldlog.report import exit_label

    assert exit_label(0, expect_found=False) == "0 (expect not met)"
    # An interrupted run is first of all interrupted; the check never ran.
    assert exit_label(0, interrupted=True, expect_found=False) == "0 (interrupted)"


def test_the_report_shows_the_summary_and_flags_a_missed_expectation(tmp_workspace: Path):
    from fieldlog.report import render_report

    out = render_report(tmp_workspace / "10.0.0.1", [
        {"id": "01", "recipe": "ping/quick", "exit_code": 0, "artifacts": [],
         "summary": "4 replies · 0% loss"},
        {"id": "02", "recipe": "openssl/chain", "exit_code": 0, "artifacts": [],
         "summary": "verify: unable to verify the first certificate",
         "expect": {"pattern": r"Verify return code: 0 \(ok\)", "found": False}},
    ])
    assert "| # | Recipe | Started | Duration | Exit | Files | Summary |" in out
    assert "| 4 replies · 0% loss |" in out
    assert "| **0 (expect not met)** |" in out
    assert "## #02 · openssl/chain · exit 0 (expect not met)" in out
    # What was looked for, so a false failure is readable as one.
    assert r"Expected: `Verify return code: 0 \(ok\)`" in out


def test_the_report_keeps_quiet_about_an_expectation_that_was_met(tmp_workspace: Path):
    from fieldlog.report import render_report

    out = render_report(tmp_workspace / "10.0.0.1", [
        {"id": "01", "recipe": "openssl/chain", "exit_code": 0, "artifacts": [],
         "expect": {"pattern": r"Verify return code: 0 \(ok\)", "found": True}},
    ])
    # Only a miss earns the line: a check that was met is the heading's exit 0.
    assert "## #01 · openssl/chain · exit 0" in out
    assert "Expected:" not in out


def test_the_report_step_table_reads_as_a_checklist(tmp_workspace: Path):
    from fieldlog.report import render_report

    out = render_report(tmp_workspace / "10.0.0.1", [
        {"id": "03", "recipe": "chain/c1", "exit_code": 1, "artifacts": [],
         "summary": "ping: 4 replies · 0% loss", "steps": [
             {"id": "01", "recipe": "ping/quick", "exit_code": 0,
              "summary": "4 replies · 0% loss"},
             {"id": "02", "recipe": "openssl/chain", "exit_code": 0, "summary": "verify: bad",
              "expect": {"pattern": "0 ok", "found": False}},
         ]},
    ])
    assert "| Step | Recipe | Exit | Summary |" in out
    assert "| 1 | ping/quick | 0 | 4 replies · 0% loss |" in out
    assert "| 2 | openssl/chain | **0 (expect not met)** | verify: bad |" in out


def test_history_says_the_expectation_was_missed(tmp_workspace: Path, capsys):
    import argparse

    from fieldlog.cli import handle_history

    target = tmp_workspace / "10.0.0.1"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "openssl/chain", "exit_code": 0, "duration_sec": 0.3,
         "start_time": "2026-09-17T09:10:11", "artifacts": [],
         "expect": {"pattern": "0 ok", "found": False}},
        {"id": "02", "recipe": "openssl/chain", "exit_code": 0, "duration_sec": 0.3,
         "start_time": "2026-09-17T09:10:12", "artifacts": [],
         "expect": {"pattern": "0 ok", "found": True}},
    ]), encoding="utf-8")

    args = argparse.Namespace(
        target="10.0.0.1", target_flag="", workspace=str(tmp_workspace), json=False
    )
    assert handle_history(args) == 0
    out = capsys.readouterr().out
    assert "exit 0 (expect not met)" in out
    assert out.count("(expect not met)") == 1      # the run that met it says nothing


@pytest.mark.asyncio
async def test_the_tui_tab_reads_a_missed_expectation_as_failed(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.app import FieldlogApp
    from fieldlog.state import TargetSession

    cat = _catalog(tmp_path, CATALOG)
    tool = next(t for t in cat.tools if t["id"] == "say")
    presets = {p["id"]: p for p in tool["presets"]}

    app = FieldlogApp(TargetSession(target="10.0.0.1", workspace_dir=tmp_workspace))
    async with app.run_test() as pilot:
        app.catalog = cat
        app._recipes = app.catalog.tools
        app._spawn_job(tool, presets["met"], "say/met")
        app._spawn_job(tool, presets["missed"], "say/missed")
        await app.workers.wait_for_complete()
        await pilot.pause()

        # Same binary, same exit 0; only the expectation differs.
        assert [t.status for t in app.tabs if t.id != "system"] == ["done", "failed"]


# ---- 6. show ---------------------------------------------------------------


CONTRACT_CATALOG = """
    recipes:
      - id: say
        bin: echo
        presets:
          - id: full
            flags: "hello"
            success: [0, 1]
            parse: '(?P<w>hello)'
            summary: "saw {w}"
            expect: '^hello$'
          - id: bare
            flags: "hello"
    """


def test_show_prints_the_contract_a_preset_declares(tmp_path: Path, capsys):
    from fieldlog.cli import build_parser, handle_show

    cat = _catalog(tmp_path, CONTRACT_CATALOG)
    assert handle_show(build_parser().parse_args(["show", "say/full"]), cat) == 0
    out = capsys.readouterr().out
    assert "Success:" in out and "0, 1" in out
    assert "Parse:" in out and "(?P<w>hello)" in out
    assert "Summary:" in out and "saw {w}" in out
    assert "Expect:" in out and "^hello$" in out


def test_show_says_nothing_about_a_preset_that_declares_nothing(tmp_path: Path, capsys):
    from fieldlog.cli import build_parser, handle_show

    cat = _catalog(tmp_path, CONTRACT_CATALOG)
    assert handle_show(build_parser().parse_args(["show", "say/bare"]), cat) == 0
    out = capsys.readouterr().out
    for label in ("Success:", "Parse:", "Summary:", "Expect:"):
        assert label not in out
