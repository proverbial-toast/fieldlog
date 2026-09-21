"""The shipped catalog is themed files, and a theme can be switched off.

`recipes.d/` used to sit at the repo root, outside the package, so a wheel
carried 15 tools and one chain where a checkout had 19 and six — every tester
would have reviewed a smaller product than the maintainer was using. The
catalog now lives in `fieldlog/recipes.d/`, one file per theme, and ships.

Switching a theme off is the same disappearance `platform:` already performs:
the entries never enter the catalog. What they would have provided is
remembered, so a chain that wanted one of them can say why it is not there
instead of reading as a broken catalog.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fieldlog.recipes import RECIPES_PATH, base_files, load_catalog, read_themes


@pytest.fixture
def dropins(tmp_path: Path) -> Path:
    """An empty drop-in directory, so only the shipped catalog is in play."""
    d = tmp_path / "recipes.d"
    d.mkdir()
    return d


def _themes(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "themes.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _shipped(dropins: Path, themes: Path | None = None, platform: str = "linux"):
    return load_catalog(dropin_dir=dropins, platform=platform, themes_path=themes)


# ---- the catalog ships ---------------------------------------------------


def test_the_themed_catalog_is_inside_the_package(dropins: Path):
    """Not at the repo root: this is exactly what the wheel was missing."""
    assert RECIPES_PATH.is_dir()
    assert RECIPES_PATH.parent.name == "fieldlog"

    names = [p.name for p in base_files(RECIPES_PATH)]
    assert names, "no themed files ship"
    assert all(n.endswith(".yaml") for n in names)
    assert names == sorted(names), "the numeric prefixes are what fix the load order"


def test_every_shipped_file_declares_a_theme(dropins: Path):
    cat = _shipped(dropins)

    assert cat.errors == []
    assert set(cat.themes) == {
        "reach", "dns", "http-tls", "local", "capture", "scan", "throughput",
        "quality", "neighbourhood", "chains",
    }
    assert all(cat.themes.values()), "nothing is off without a themes.yaml"


def test_a_lone_file_is_still_a_valid_base(tmp_path: Path, dropins: Path):
    """`base=` takes a directory or one file. The merge-rule tests hand it a
    file, and so would anyone pointing fieldlog at a catalog of their own."""
    one = tmp_path / "one.yaml"
    one.write_text("recipes:\n  - id: solo\n    bin: true\n    flags: ''\n", encoding="utf-8")

    cat = load_catalog(base=one, dropin_dir=dropins, themes_path=None)

    assert [t["id"] for t in cat.tools] == ["solo"]
    assert base_files(one) == [one]


# ---- switching one off ---------------------------------------------------


def test_a_theme_switched_off_takes_its_recipes_with_it(tmp_path: Path, dropins: Path):
    on = _shipped(dropins)
    off = _shipped(dropins, _themes(tmp_path, "themes:\n  scan: false\n"))

    assert "nmap" in {t["id"] for t in on.tools}
    assert "nmap" not in {t["id"] for t in off.tools}
    assert off.themes["scan"] is False
    assert off.inactive_themes == ["scan"]
    assert off.errors == [], "switching a theme off is not a fault in the catalog"


def test_the_other_themes_are_untouched(tmp_path: Path, dropins: Path):
    off = _shipped(dropins, _themes(tmp_path, "themes:\n  scan: false\n"))

    assert {"ping", "dig", "curl"} <= {t["id"] for t in off.tools}
    assert off.themes["reach"] is True


def test_a_chain_needing_a_switched_off_step_says_which_theme(tmp_path: Path, dropins: Path):
    """The costly version of this is an `unknown step` error, which reads as a
    broken catalog rather than as the operator's own setting."""
    off = _shipped(dropins, _themes(tmp_path, "themes:\n  scan: false\n"))

    assert "tls-audit" not in {c["id"] for c in off.chains}
    assert off.errors == []
    assert any(
        "tls-audit" in line and "nmap/ciphers" in line and "theme scan is off" in line
        for line in off.overrides
    ), off.overrides


def test_a_chain_whose_steps_all_survive_still_runs(tmp_path: Path, dropins: Path):
    off = _shipped(dropins, _themes(tmp_path, "themes:\n  scan: false\n"))

    assert "reach" in {c["id"] for c in off.chains}


def test_switching_the_chains_theme_off_leaves_the_recipes(tmp_path: Path, dropins: Path):
    off = _shipped(dropins, _themes(tmp_path, "themes:\n  chains: false\n"))

    assert off.chains == []
    assert len(off.tools) == len(_shipped(dropins).tools)


# ---- reading themes.yaml -------------------------------------------------


def test_no_themes_file_switches_nothing_off(tmp_path: Path):
    switched, problem = read_themes(tmp_path / "nothing-here.yaml")

    assert switched == {}
    assert problem == ""


@pytest.mark.parametrize(
    "body, expected",
    [
        ("themes:\n  scan: false\n", {"scan": False}),
        ("themes:\n  scan: true\n", {"scan": True}),        # written on, same as absent
        ("themes:\n  a: false\n  b: false\n", {"a": False, "b": False}),
        ("", {}),                                            # empty file says nothing
        ("themes:\n", {}),                                   # written and left blank
    ],
)
def test_the_file_is_a_list_of_exceptions(tmp_path: Path, body, expected):
    switched, problem = read_themes(_themes(tmp_path, body))

    assert switched == expected
    assert problem == ""


@pytest.mark.parametrize(
    "body",
    [
        "themes: [scan, capture]\n",      # a list, not a mapping
        "themes:\n  scan: maybe\n",       # not true or false
        "themes:\n  scan: 0\n",           # 0 is not false here; say so rather than guess
        "{{{ not yaml\n",
    ],
)
def test_an_unusable_themes_file_turns_nothing_off_and_says_so(tmp_path: Path, dropins: Path, body):
    """Failing the other way would hide recipes over a typo, and a catalog that
    quietly shrinks is the one thing this loader is written to avoid."""
    path = _themes(tmp_path, body)

    switched, problem = read_themes(path)
    assert switched == {}
    assert problem, "an unusable file must say something"

    cat = _shipped(dropins, path)
    assert len(cat.tools) == len(_shipped(dropins).tools), "recipes went missing over a bad file"
    assert any(problem == e for e in cat.errors), cat.errors


# ---- what the operator is told -------------------------------------------


def test_doctor_names_the_themes_and_the_file_that_switched_them(tmp_path: Path, dropins: Path, capsys):
    from fieldlog import recipes as recipes_mod
    from fieldlog.cli import build_parser, handle_doctor

    path = _themes(tmp_path, "themes:\n  scan: false\n  capture: false\n")
    cat = _shipped(dropins, path)

    # doctor reads the path from the module, as the real command does.
    original = recipes_mod.THEMES_PATH
    recipes_mod.THEMES_PATH = path
    try:
        args = build_parser().parse_args(["doctor", "10.0.0.1"])
        assert handle_doctor(args, cat) == 0
    finally:
        recipes_mod.THEMES_PATH = original

    out = capsys.readouterr().out
    assert "themes off:" in out
    assert "scan" in out and "capture" in out
    assert "themes.yaml" in out


def test_doctor_json_carries_every_theme_and_its_state(tmp_path: Path, dropins: Path, capsys):
    import json

    from fieldlog.cli import build_parser, handle_doctor

    cat = _shipped(dropins, _themes(tmp_path, "themes:\n  scan: false\n"))
    assert handle_doctor(build_parser().parse_args(["doctor", "--json"]), cat) == 0

    themes = json.loads(capsys.readouterr().out)["themes"]
    assert themes["scan"] is False
    assert themes["reach"] is True


def test_doctor_says_nothing_about_themes_when_none_are_off(dropins: Path, capsys):
    from fieldlog.cli import build_parser, handle_doctor

    assert handle_doctor(build_parser().parse_args(["doctor"]), _shipped(dropins)) == 0

    assert "themes off:" not in capsys.readouterr().out
