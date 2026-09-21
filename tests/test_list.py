"""`fieldlog list`: one row per tool by default, a tool's recipes when named."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fieldlog.cli import build_parser, handle_list
from fieldlog.recipes import load_catalog, score, search

BASE = """
recipes:
  - id: ok
    name: "Always Fine"
    bin: true
    presets:
      - id: x
        name: "the x one"
        flags: "--x-flag $TARGET"
      - id: y
        flags: "--y-flag"
  - id: gone
    name: "Not Installed"
    bin: fieldlog-no-such-binary
    presets:
      - id: z
        flags: "--z-flag"
chains:
  - id: c1
    name: "both"
    steps:
      - ok/x
      - ok/y
  - id: a-long-chain-id
    name: "a chain name well past the thirty-four column default"
    steps:
      - recipe: ok/y
        continue: true
"""


@pytest.fixture
def catalog(tmp_path: Path):
    base = tmp_path / "base.yaml"
    base.write_text(BASE, encoding="utf-8")
    dropins = tmp_path / "recipes.d"
    dropins.mkdir()
    return load_catalog(base=base, dropin_dir=dropins)


def _list(catalog, capsys, *argv: str) -> str:
    assert handle_list(build_parser().parse_args(["list", *argv]), catalog) == 0
    return capsys.readouterr().out


def test_overview_is_one_row_per_tool(catalog, capsys):
    out = _list(catalog, capsys)
    assert "Always Fine" in out and "2v" in out
    assert "Not Installed" in out and "n/a" in out
    assert "c1" in out and "2 steps" in out
    assert "ok/x" not in out and "--x-flag" not in out   # recipes wait for `list ok`
    assert out.index("Always Fine") < out.index("Not Installed")   # installed first, as in the TUI


def test_naming_a_tool_lists_its_recipes_without_flags(catalog, capsys):
    out = _list(catalog, capsys, "ok")
    assert "ok/x" in out and "the x one" in out and "ok/y" in out
    assert "gone/z" not in out
    assert "--x-flag" not in out


def test_naming_a_chain_lists_its_steps(catalog, capsys):
    out = _list(catalog, capsys, "c1")
    assert "ok/x" in out and "ok/y" in out and "2 steps" in out
    assert "--x-flag" not in out
    assert "ok/y?" in _list(catalog, capsys, "a-long-chain-id")   # continue-on-failure mark


def test_chain_rows_line_up_whatever_their_length(catalog, capsys):
    out = _list(catalog, capsys)
    rows = [line for line in out.splitlines() if re.search(r"\d+ steps?$", line.rstrip())]
    assert len(rows) == 2
    assert len({re.search(r"\d+ steps?$", line.rstrip()).start() for line in rows}) == 1
    assert "1 step" in out and "1 steps" not in out


def test_verbose_shows_flags(catalog, capsys):
    assert "--x-flag" in _list(catalog, capsys, "ok", "-V")
    everything = _list(catalog, capsys, "-V")
    assert "gone/z" in everything and "--z-flag" in everything
    assert "ok/x → ok/y" in everything


def test_search_lists_matching_recipes(catalog, capsys):
    out = _list(catalog, capsys, "y-flag")
    assert "ok/y" in out and "ok/x" not in out


# Two tools on one binary, neither preset named after its id: the TUI's filter
# found `/captive` nowhere, and `list banner` answered with iperf3/bloat.
SHARED_BIN = [
    {"id": "ping", "bin": "ping", "name": "ICMP Reachability",
     "presets": [{"id": "quick", "name": "4 probes", "flags": "-c 4 $TARGET"}]},
    {"id": "rtt", "bin": "ping", "name": "Latency Quality",
     "presets": [{"id": "jitter", "name": "20 probes, spread not average", "flags": "-c 20 $TARGET"}]},
]


def _hits(query: str) -> list:
    return [f"{t['id']}/{p['id']}" for t, p, _ in search(SHARED_BIN, query, lambda t, p: False)]


def test_search_finds_a_recipe_by_its_id_whatever_binary_it_runs():
    assert _hits("rtt") == ["rtt/jitter"]
    assert _hits("rtt/jitter") == ["rtt/jitter"]
    assert _hits("jitter") == ["rtt/jitter"]
    assert _hits("ping")[0] == "ping/quick"    # the binary still finds both


def test_search_finds_a_recipe_by_the_tool_name_the_tree_shows():
    assert _hits("latency") == ["rtt/jitter"]


def test_ids_and_tool_names_stay_out_of_the_loose_match():
    rtt, jitter = SHARED_BIN[1], SHARED_BIN[1]["presets"][0]
    assert score(rtt, jitter, "lqy") is None     # a subsequence of "latency quality" only


def test_runnable_hides_missing_tools(catalog, capsys):
    out = _list(catalog, capsys, "-r")
    assert "Always Fine" in out and "Not Installed" not in out


def test_category_is_gone(catalog, capsys):
    entries = json.loads(_list(catalog, capsys, "--json"))
    assert all("category" not in e for e in entries)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["list", "-c", "dns"])
