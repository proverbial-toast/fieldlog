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
    assert set(out) == {"scope", "tools", "chains", "summary"}
    assert out["summary"]["recipes_total"] == 3
    assert any(
        m["bin"] == "definitely-not-a-real-binary-xyz"
        for m in out["summary"]["missing_binaries"]
    )


def test_human_output_flags_missing_binary(catalog, capsys):
    assert handle_doctor(_args(["doctor"]), catalog) == 0
    out = capsys.readouterr().out
    assert "install:" in out
    assert "definitely-not-a-real-binary-xyz" in out


def test_dispatch_check_alias():
    assert dispatch_argv(["check", "10.0.0.1"]) == ("cli", ["doctor", "10.0.0.1"])
    assert dispatch_argv(["doctor"]) == ("cli", ["doctor"])
