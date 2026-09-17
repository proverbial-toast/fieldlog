"""CLI output shows commands as written: `[x]` in flags is text, not Rich markup."""

from __future__ import annotations

from pathlib import Path

from fieldlog.cli import build_parser, handle_run, handle_show
from fieldlog.recipes import Catalog, normalize_recipe

FLAGS = "'tcp[tcpflags]' '{t[int(NR*0.5)]}'"


def _catalog() -> Catalog:
    return Catalog(tools=[
        normalize_recipe({"id": "probe", "bin": "true", "presets": [{"id": "brackets", "flags": FLAGS}]}),
        normalize_recipe({"id": "dig", "presets": [{"id": "v", "bin": "delv", "flags": "+vtrace $HOST"}]}),
    ])


def test_show_keeps_brackets_in_flags(capsys):
    assert handle_show(build_parser().parse_args(["show", "probe/brackets"]), _catalog()) == 0
    out = capsys.readouterr().out
    assert "tcp[tcpflags]" in out and "t[int(NR*0.5)]" in out


def test_dry_run_keeps_brackets_in_flags(tmp_workspace: Path, capsys):
    args = build_parser().parse_args(["run", "probe/brackets", "-t", "10.0.0.1", "-w", str(tmp_workspace), "--dry-run"])
    assert handle_run(args, _catalog()) == 0
    out = capsys.readouterr().out
    assert "tcp[tcpflags]" in out and "t[int(NR*0.5)]" in out


def test_run_banner_keeps_its_prefix_and_brackets(tmp_workspace: Path, capsys):
    args = build_parser().parse_args(["run", "probe/brackets", "-t", "10.0.0.1", "-w", str(tmp_workspace)])
    assert handle_run(args, _catalog()) == 0
    out = capsys.readouterr().out
    assert "[fieldlog] Spawning" in out and "[fieldlog] [DONE:0]" in out
    assert "tcp[tcpflags]" in out


def test_show_uses_the_presets_own_bin(capsys):
    args = build_parser().parse_args(["show", "dig/v", "-H", "example.com"])
    assert handle_show(args, _catalog()) == 0
    out = capsys.readouterr().out
    assert "delv +vtrace example.com" in out and "dig +vtrace" not in out


def test_show_previews_the_per_run_outdir(capsys):
    """`show` knows no workspace, so it has no run number: `NN` stands where one
    would, rather than the old shared-by-the-second directory."""
    cat = Catalog(tools=[normalize_recipe(
        {"id": "cap", "bin": "true", "presets": [{"id": "w", "flags": "-w $OUTDIR/x.pcap"}]}
    )])
    assert handle_show(build_parser().parse_args(["show", "cap/w"]), cat) == 0
    assert "_NN/x.pcap" in capsys.readouterr().out
