"""`fieldlog doctor` reports which recipes and chains can run against a scope.

Every verdict comes from is_blocked / chain_blocked, so doctor and a real run
agree; these tests pin the roll-up counts and the human/JSON surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fieldlog.cli import build_parser, dispatch_argv, doctor_scan, handle_doctor
from fieldlog.recipes import load_catalog
from fieldlog.state import TargetSession

DROPIN = """
recipes:
  - id: alive
    bin: sh
    presets:
      - id: t
        flags: "-c 'echo $TARGET'"
      - id: plain
        flags: "-c true"
  - id: ghosttool
    bin: definitely-not-a-real-binary-xyz
    presets:
      - id: go
        flags: "-x"
chains:
  - id: reachy
    steps:
      - alive/plain
  - id: needy
    steps:
      - alive/t
"""


@pytest.fixture
def catalog(tmp_path: Path):
    d = tmp_path / "recipes.d"
    d.mkdir()
    (d / "doc.yaml").write_text(DROPIN)
    # A base that does not exist keeps the built-in catalog out of the scan.
    return load_catalog(base=tmp_path / "no-base.yaml", dropin_dir=d)


def _args(argv):
    return build_parser().parse_args(argv)


def test_scan_no_scope_counts_and_reasons(catalog):
    scan = doctor_scan(catalog, TargetSession())
    assert scan["recipes_total"] == 3
    assert scan["recipes_ready"] == 1              # only alive/plain needs no scope
    assert scan["needs"]["target"] == 1            # alive/t is blocked on $TARGET
    assert "definitely-not-a-real-binary-xyz" in scan["missing"]
    assert scan["tools_installed"] == 1            # sh yes, ghosttool no
    ready = {c["chain"]["id"]: not c["blocked"] for c in scan["chains"]}
    assert ready == {"reachy": True, "needy": False}


def test_scan_with_target_unblocks(catalog):
    scan = doctor_scan(catalog, TargetSession(target="10.0.0.1"))
    assert scan["recipes_ready"] == 2              # alive/t now runnable
    assert scan["needs"]["target"] == 0
    ready = {c["chain"]["id"]: not c["blocked"] for c in scan["chains"]}
    assert ready["needy"] is True


def test_json_output(catalog, capsys):
    assert handle_doctor(_args(["doctor", "--json"]), catalog) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"scope", "tools", "chains", "summary", "themes", "environment"}
    # The box the scan ran on, so one paste answers a bug report.
    assert out["environment"]["version"] and out["environment"]["catalog"]
    assert out["summary"]["recipes_total"] == 3
    assert "definitely-not-a-real-binary-xyz" in out["summary"]["missing_binaries"]


def test_human_output_flags_missing_binary(catalog, capsys):
    assert handle_doctor(_args(["doctor"]), catalog) == 0
    out = capsys.readouterr().out
    assert "missing:" in out
    assert "definitely-not-a-real-binary-xyz" in out
    assert "apt install" not in out               # no distro-specific install syntax


def test_scope_blocked_chain_reads_cleanly(catalog, capsys):
    """A chain blocked only by scope is a terse 'needs a target', not the TUI's
    'set one in T → scope' and not the internal 'variant' wording."""
    assert handle_doctor(_args(["doctor"]), catalog) == 0
    out = capsys.readouterr().out
    assert "needs a target" in out               # the `needy` chain, cleanly phrased
    assert "set one in T" not in out
    assert "variant" not in out


def test_a_refused_target_says_why_rather_than_needs_a_target(catalog, capsys):
    """The terse note is for a value that is missing. A target that is set and
    refused has a reason of its own, and reading 'needs a target' sends the
    operator to set what is already set."""
    assert handle_doctor(_args(["doctor", "-t", "10.0.0.1;id", "-v"]), catalog) == 0
    out = capsys.readouterr().out
    assert "target has unsafe characters" in out
    assert "needs a target" not in out


def test_dispatch_check_alias():
    assert dispatch_argv(["check", "10.0.0.1"]) == ("cli", ["doctor", "10.0.0.1"])
    assert dispatch_argv(["doctor"]) == ("cli", ["doctor"])


def test_a_refused_target_is_not_counted_as_a_missing_one(catalog, capsys):
    """The footer tells the operator what to do next. "set a target" is wrong
    advice for a target that is set and rejected."""
    assert handle_doctor(_args(["doctor", "--target=-f"]), catalog) == 0
    out = capsys.readouterr().out
    assert "set a target" not in out
    assert "refused" in out


def test_the_json_summary_counts_refused_values(catalog, capsys):
    assert handle_doctor(_args(["doctor", "--target=-f", "--json"]), catalog) == 0
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["refused"] == 1                 # alive/t, the one $TARGET preset
    assert summary["needs"]["target"] == 0
