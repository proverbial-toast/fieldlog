"""`parse:` turns a finished log into a one-line summary on the run record."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from fieldlog.recipes import load_catalog, parse_fields, parse_rule, parse_summary

PING_TAIL = """\
64 bytes from 10.0.0.1: icmp_seq=3 ttl=64 time=0.09 ms
64 bytes from 10.0.0.1: icmp_seq=4 ttl=64 time=0.04 ms

--- 10.0.0.1 ping statistics ---
4 packets transmitted, 4 received, 0% packet loss, time 3070ms
"""


def _preset(**extra) -> dict:
    return {"id": "quick", "flags": "-c 4 $TARGET", **extra}


def _catalog(tmp_path: Path, yaml_text: str):
    """A catalog from one base file, with nothing else on disk scanned."""
    base = tmp_path / "base.yaml"
    base.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


# ---- 1. Extraction ---------------------------------------------------------


def test_a_preset_without_a_rule_has_no_summary():
    assert parse_rule(_preset()) is None
    assert parse_summary(None, PING_TAIL) == ""


def test_the_template_fills_from_named_groups():
    rule = parse_rule(_preset(
        parse=r"(?P<rx>\d+) received, (?P<loss>[\d.]+)% packet loss",
        summary="{rx} replies · {loss}% loss",
    ))
    assert parse_summary(rule, PING_TAIL) == "4 replies · 0% loss"


def test_without_a_template_the_whole_match_is_the_summary():
    rule = parse_rule(_preset(parse=r"\d+ received, [\d.]+% packet loss"))
    assert parse_summary(rule, PING_TAIL) == "4 received, 0% packet loss"


def test_the_last_match_wins():
    # A per-probe line repeats; the closing stats are what the summary is after.
    rule = parse_rule(_preset(parse=r"time=(?P<t>\S+)", summary="{t}"))
    assert parse_summary(rule, PING_TAIL) == "0.04"


def test_no_match_is_empty_rather_than_an_error():
    rule = parse_rule(_preset(parse=r"never matches this"))
    assert parse_summary(rule, PING_TAIL) == ""


def test_a_summary_is_one_capped_line():
    from fieldlog.recipes import PARSE_SUMMARY_MAX

    rule = parse_rule(_preset(parse=r"(?s)ping.*ms"))
    summary = parse_summary(rule, PING_TAIL)
    assert "\n" not in summary
    assert len(summary) <= PARSE_SUMMARY_MAX


def test_the_named_groups_are_kept_as_fields():
    rule = parse_rule(_preset(
        parse=r"(?P<rx>\d+) received, (?P<loss>[\d.]+)% packet loss",
        summary="{rx} replies · {loss}% loss",
    ))
    assert parse_fields(rule, PING_TAIL) == {"rx": "4", "loss": "0"}


def test_a_rule_without_named_groups_has_no_fields():
    rule = parse_rule(_preset(parse=r"\d+ received"))
    assert parse_fields(rule, PING_TAIL) == {}


def test_a_rule_that_does_not_match_has_no_fields():
    rule = parse_rule(_preset(parse=r"(?P<rx>\d+) sent"))
    assert parse_fields(rule, PING_TAIL) == {}
    assert parse_fields(None, PING_TAIL) == {}


def test_the_shipped_curl_rule_reads_a_real_timing_line(tmp_path: Path):
    """The rule that ships with `curl/timing80`, against the line its own `-w`
    format writes."""
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    cat = load_catalog(dropin_dir=dropins)
    curl = next(t for t in cat.tools if t["id"] == "curl")
    rule = parse_rule(next(p for p in curl["presets"] if p["id"] == "timing80"))

    line = "dns=0.004 tcp=0.020 ttfb=0.310 total=0.311 code=200\n"
    assert parse_summary(rule, line) == "HTTP 200 in 0.311s"
    assert parse_fields(rule, line) == {"total": "0.311", "code": "200"}


# ---- 2. Load-time validation ----------------------------------------------


def test_a_bad_regex_costs_the_summary_not_the_recipe(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: "-v"
                parse: '(unclosed'
        """)
    preset = cat.tools[0]["presets"][0]
    assert "parse" not in preset          # the rule is dropped
    assert preset["flags"] == "-v"        # the preset still runs
    assert any("ok/x parse:" in e and "invalid regex" in e for e in cat.errors), cat.errors


def test_a_template_naming_an_unknown_group_is_refused(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: ""
                parse: '(?P<rx>\\d+) received'
                summary: "{tx} sent"
        """)
    assert "parse" not in cat.tools[0]["presets"][0]
    assert any("no group named tx" in e for e in cat.errors), cat.errors


def test_a_sound_rule_survives_the_load(tmp_path: Path):
    cat = _catalog(tmp_path, """
        recipes:
          - id: ok
            bin: true
            presets:
              - id: x
                flags: ""
                parse: '(?P<rx>\\d+) received'
                summary: "{rx} replies"
        """)
    assert cat.errors == []
    assert parse_summary(parse_rule(cat.tools[0]["presets"][0]), PING_TAIL) == "4 replies"


# ---- 3. The record ---------------------------------------------------------


def test_a_run_record_carries_the_summary(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.cli import build_parser, handle_run

    cat = _catalog(tmp_path, """
        recipes:
          - id: say
            bin: echo
            presets:
              - id: stats
                flags: '4 packets transmitted, 4 received, 0% packet loss'
                parse: '(?P<rx>\\d+) received, (?P<loss>[\\d.]+)% packet loss'
                summary: '{rx} replies · {loss}% loss'
        """)
    args = build_parser().parse_args(
        ["run", "say/stats", "-t", "10.0.0.1", "-w", str(tmp_workspace), "-q"]
    )
    assert handle_run(args, cat) == 0

    runs = json.loads((tmp_workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))
    assert runs[-1]["summary"] == "4 replies · 0% loss"
    # The groups ride along raw, so a value can be trended without re-parsing.
    assert runs[-1]["fields"] == {"rx": "4", "loss": "0"}


def test_a_preset_without_a_rule_writes_no_summary_key(tmp_path: Path, tmp_workspace: Path):
    from fieldlog.cli import build_parser, handle_run

    cat = _catalog(tmp_path, """
        recipes:
          - id: say
            bin: echo
            presets:
              - id: plain
                flags: 'nothing to parse here'
        """)
    args = build_parser().parse_args(
        ["run", "say/plain", "-t", "10.0.0.1", "-w", str(tmp_workspace), "-q"]
    )
    assert handle_run(args, cat) == 0

    runs = json.loads((tmp_workspace / "10.0.0.1" / "session.json").read_text(encoding="utf-8"))
    assert "summary" not in runs[-1]
    assert "fields" not in runs[-1]


# ---- 4. The surfaces -------------------------------------------------------


def test_history_prints_the_summary_under_its_run(tmp_workspace: Path, capsys):
    import argparse

    from fieldlog.cli import handle_history

    target = tmp_workspace / "10.0.0.1"
    target.mkdir(parents=True)
    (target / "session.json").write_text(json.dumps([
        {"id": "01", "recipe": "ping/quick", "exit_code": 0, "duration_sec": 3.07,
         "start_time": "2026-09-16T08:12:36", "summary": "4 replies · 0% loss",
         "artifacts": []},
        {"id": "02", "recipe": "ss/listen", "exit_code": 0, "duration_sec": 0.1,
         "start_time": "2026-09-16T08:13:00", "artifacts": []},
    ]), encoding="utf-8")

    args = argparse.Namespace(
        target="10.0.0.1", target_flag="", workspace=str(tmp_workspace), json=False
    )
    assert handle_history(args) == 0
    out = capsys.readouterr().out
    assert "4 replies · 0% loss" in out
    # The run without a rule contributes no summary line.
    assert out.count("replies") == 1


@pytest.mark.asyncio
async def test_the_tui_status_band_carries_the_summary(tmp_path: Path):
    from fieldlog.app import FieldlogApp, TabDescriptor
    from fieldlog.state import ActiveJob, TargetSession

    app = FieldlogApp(session=TargetSession(workspace_dir=tmp_path))
    job = ActiveJob(
        id="01", recipe_id="ping", name="ping/quick #01",
        log_path=tmp_path / "01.log", exit_code=0, summary="4 replies · 0% loss",
    )
    tab = TabDescriptor(
        id="job-01", label="ping/quick #01", status="done", tool_id="ping", job_id="01",
    )

    async with app.run_test():
        app.jobs["01"] = job
        app.tabs.append(tab)
        app.active_tab_id = "job-01"
        assert dict((k, v) for k, v, _ in app._status_items())["summary"] == "4 replies · 0% loss"

        # A preset without a rule adds no item rather than an empty one.
        job.summary = ""
        assert "summary" not in [k for k, _, _ in app._status_items()]
